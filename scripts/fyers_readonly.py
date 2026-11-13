"""FYERS v3 reads and non-executing margin estimates. See references/fyers.md."""
from __future__ import annotations

import json
import os
import re
import time as clock
import urllib.error
import urllib.parse
import urllib.request
from datetime import date, datetime, time, timedelta
from email.utils import parsedate_to_datetime

from core import Block, IST, fingerprint, instant, integer, number, positive

HOST = "https://api-t1.fyers.in"
PROFILE, FUNDS, POSITIONS, ORDERS = ["/api/v3/" + x for x in ("profile", "funds", "positions", "orders")]
TRADES = "/api/v3/tradebook"
HOLDINGS = "/api/v3/holdings"
HISTORY, QUOTES, DEPTH = ["/data/" + x for x in ("history", "quotes", "depth")]
MARGIN = "/api/v3/multiorder/margin"
SEGMENTS = {"NSE_CM": (10, 10, "NSE"), "BSE_CM": (12, 10, "BSE"),
            "NSE_FO": (10, 11, "NFO"), "BSE_FO": (12, 11, "BFO")}
PREFIXES = {"1010": "NSE", "1210": "BSE", "1011": "NFO", "1211": "BFO"}
MASTER_URLS = {s: f"https://public.fyers.in/sym_details/{s}_sym_master.json" for s in SEGMENTS}
MAX_BYTES = 64 * 1024 * 1024


def token_exchange(value):
    if not isinstance(value, str) or not re.fullmatch(r"(?:10|12)(?:10|11)[0-9]{8,12}", value):
        raise Block("FYERS_TOKEN: supported numeric master fyToken string required")
    return PREFIXES[value[:4]]


def symbol_key(value):
    if not isinstance(value, str) or not re.fullmatch(r"(?:NSE|BSE):[A-Z0-9&_.-]+", value):
        raise Block("FYERS_SYMBOL: exact NSE/BSE symbol ticker from master required")
    return value


def full_symbol(symbol, exchange):
    market = {"NSE": "NSE", "NFO": "NSE", "BSE": "BSE", "BFO": "BSE"}.get(exchange)
    if market is None or not isinstance(symbol, str) or ":" in symbol:
        raise Block("FYERS_IDENTITY: supported exchange and unprefixed master symbol required")
    return symbol_key(market + ":" + symbol)


def epoch(value):
    try:
        seconds = integer(value, "epoch seconds")
        if seconds >= 10000000000:
            raise ValueError("not seconds")
        return datetime.fromtimestamp(seconds, IST)
    except (ValueError, TypeError, OSError, OverflowError):
        raise Block("FYERS_TIME: positive Unix-second source timestamp required") from None


def source_time(value):
    if not isinstance(value, str) or not value.strip():
        raise Block("FYERS_TIME: explicit offset-bearing bound required")
    try:
        return instant(value)
    except ValueError:
        raise Block("FYERS_TIME: invalid offset-bearing bound") from None


def decode_json(raw):
    def reject(value):
        raise ValueError("nonfinite")
    def pairs(values):
        result = {}
        for k, v in values:
            if k in result:
                raise ValueError("duplicate key")
            result[k] = v
        return result
    try:
        return json.loads(raw, parse_constant=reject, object_pairs_hook=pairs)
    except (ValueError, UnicodeError):
        raise Block("FYERS_RESPONSE: invalid JSON; body suppressed") from None


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise Block("FYERS_REDIRECT: refused to forward authorization")


class FyersReadOnly:
    def __init__(self, opener=None):
        self.opener = opener

    def _read(self, request):
        try:
            opener = self.opener or urllib.request.build_opener(NoRedirect()).open
            started = clock.monotonic()
            with opener(request, timeout=20) as response:
                chunks, size, headers = [], 0, response.headers
                while True:
                    if clock.monotonic() - started > 60:
                        raise Block("FYERS_NETWORK: download deadline exceeded; no retry")
                    chunk = response.read1(min(65536, MAX_BYTES + 1 - size))
                    if not chunk:
                        break
                    chunks.append(chunk)
                    size += len(chunk)
                    if size > MAX_BYTES:
                        raise Block("FYERS_RESPONSE: response too large")
                return b"".join(chunks), headers
        except urllib.error.HTTPError as exc:
            raise Block(f"FYERS_HTTP_{exc.code}: read failed; check local session/entitlement; no retry") from None
        except (urllib.error.URLError, TimeoutError, OSError):
            raise Block("FYERS_NETWORK: data unavailable; no automatic retry") from None

    @staticmethod
    def credentials():
        values = {k: os.environ.get(k) for k in ("FYERS_APP_ID", "FYERS_ACCESS_TOKEN", "FYERS_ACCOUNT_ID")}
        if any(not v or any(c.isspace() for c in v) or ":" in v for v in values.values()):
            raise Block("FYERS_AUTH: configure app ID, raw access token and account ID locally; never send secrets in chat")
        return values

    def request(self, method, path, query=None, body=None):
        if not ((method == "GET" and path in {PROFILE, FUNDS, POSITIONS, ORDERS, TRADES, HOLDINGS, HISTORY, QUOTES, DEPTH}) or
                (method == "POST" and path == MARGIN)):
            raise Block("READ_ONLY: endpoint/method not allowed; live execution unavailable")
        settings = self.credentials()
        url = HOST + path + ("?" + urllib.parse.urlencode(query) if query else "")
        req = urllib.request.Request(url, method=method, data=json.dumps(body, allow_nan=False).encode() if body is not None else None,
            headers={"Authorization": settings["FYERS_APP_ID"] + ":" + settings["FYERS_ACCESS_TOKEN"],
                     "Accept": "application/json", "Content-Type": "application/json"})
        raw, _ = self._read(req)
        result = decode_json(raw)
        if not isinstance(result, dict) or result.get("s") != "ok" or ("code" in result and (type(result["code"]) is not int or result["code"] != 200)):
            raise Block("FYERS_RESPONSE: unsuccessful/malformed response; body suppressed")
        return result

    def instruments(self):
        instruments, sources, excluded = [], {}, 0
        for segment, url in MASTER_URLS.items():
            raw, headers = self._read(urllib.request.Request(url, headers={"Accept": "application/json"}))
            try:
                modified = parsedate_to_datetime(headers["Last-Modified"])
                if modified.tzinfo is None or modified > instant():
                    raise ValueError("source time")
            except (KeyError, TypeError, ValueError):
                raise Block("FYERS_MASTER: valid source Last-Modified required for every segment") from None
            result = self.normalize_instruments(decode_json(raw), segment, modified.astimezone(IST).date().isoformat())
            instruments.extend(result["instruments"])
            excluded += result["excluded"]
            sources[segment] = {"url": url, "source_updated_at": modified.isoformat()}
        tokens = [i["token"] for i in instruments]
        if len(tokens) != len(set(tokens)) or not instruments:
            raise Block("FYERS_MASTER: duplicate tokens or no supported active instruments")
        return {"source": "fyers", "as_of": instant().isoformat(), "sources": sources,
                "instruments": instruments, "excluded": excluded,
                "note": "Active master status does not establish membership, intraday eligibility or surveillance clearance."}

    @staticmethod
    def normalize_instruments(rows, segment, master_date):
        date.fromisoformat(master_date)
        if segment not in SEGMENTS or not isinstance(rows, dict) or not rows:
            raise Block("FYERS_MASTER: nonempty symbol dictionary and supported segment required")
        exchange_code, segment_code, exchange = SEGMENTS[segment]
        result, seen, excluded = [], set(), 0
        try:
            for key, row in rows.items():
                if integer(row["exchange"]) != exchange_code or integer(row["segment"]) != segment_code:
                    raise Block("FYERS_MASTER: exchange/segment mismatch")
                kind_code = integer(row["exInstType"], "instrument type", minimum=0)
                kind = "equity" if segment_code == 10 and kind_code == 0 else {11: "future", 13: "future", 14: "option", 15: "option"}.get(kind_code) if segment_code == 11 else None
                status = integer(row["tradeStatus"], "trade status", minimum=0)
                if status not in {0, 1}:
                    raise Block("FYERS_MASTER: invalid trade status")
                if kind is None or status == 0:
                    excluded += 1
                    continue
                token, ticker = row["fyToken"], symbol_key(key)
                symbol = ticker.split(":", 1)[1]
                if token_exchange(token) != exchange or row["symTicker"] != key or full_symbol(symbol, exchange) != key or token in seen:
                    raise Block("FYERS_MASTER: inconsistent or duplicate identity")
                seen.add(token)
                if kind == "option" and row["optType"] not in {"CE", "PE"}:
                    raise Block("FYERS_MASTER: option type required")
                result.append({"broker": "fyers", "token": token, "symbol": symbol, "exchange": exchange,
                    "broker_symbol": ticker, "kind": kind, "lot_size": integer(row["minLotSize"]),
                    "tick_size": float(positive(row["tickSize"])), "master_date": master_date,
                    "expiry": epoch(row["expiryDate"]).date().isoformat() if kind != "equity" else None,
                    "underlying_type": None if kind == "equity" else "index" if kind_code in {11, 14} else "stock",
                    "underlying": row.get("underSym"), "strike": float(positive(row["strikePrice"])) if kind == "option" else None,
                    "option_type": row["optType"] if kind == "option" else None, "broker_instrument": kind_code,
                    "raw_trade_status": status, "raw_surveillance": row.get("asmGsmVal"), "row_last_update": row.get("lastUpdate")})
        except (KeyError, TypeError, AttributeError):
            raise Block("FYERS_MASTER: malformed instrument record") from None
        return {"instruments": result, "excluded": excluded}

    def candles(self, token, symbol, exchange, start, end, interval="5minute"):
        ticker = full_symbol(symbol, exchange)
        if token_exchange(token) != exchange:
            raise Block("FYERS_IDENTITY: token/exchange mismatch")
        if interval in {"minute", "5minute"}:
            first, last = source_time(start), source_time(end)
            if any(t.second or t.microsecond or t.minute % (1 if interval == "minute" else 5) for t in (first, last)):
                raise Block("FYERS_RANGE: five-minute boundaries required")
            resolution, limit, duration = ("1", 100, timedelta(minutes=1)) if interval == "minute" else ("5", 100, timedelta(minutes=5))
        elif interval == "day":
            first, last = [datetime.combine(date.fromisoformat(v), time(), IST) for v in (start, end)]
            resolution, limit, duration = "D", 366, timedelta(days=1)
        else:
            raise Block("FYERS_RANGE: unsupported interval")
        if first >= last or last > instant() or last-first > timedelta(days=limit):
            raise Block("FYERS_RANGE: invalid/future range or request limit exceeded")
        data = self.request("GET", HISTORY, query={"symbol": ticker, "resolution": resolution, "date_format": "0",
            "range_from": str(int(first.timestamp())), "range_to": str(int((last-(duration if interval in {"minute", "5minute"} else timedelta(seconds=1))).timestamp())), "cont_flag": "0", "oi_flag": "0"})
        rows = data.get("candles")
        if not isinstance(rows, list):
            raise Block("FYERS_CANDLES: array required; null is not empty history")
        bars, seen = [], set()
        for row in rows:
            if not isinstance(row, list) or len(row) != 6:
                raise Block("FYERS_CANDLES: six fields required")
            at = epoch(row[0])
            if at in seen or not first <= at < last or (interval in {"minute", "5minute"} and at + duration > last):
                raise Block("FYERS_CANDLES: duplicate, incomplete or out-of-range bar")
            if interval in {"minute", "5minute"} and (at.minute % (1 if interval == "minute" else 5) or at.second or at.microsecond):
                raise Block("FYERS_CANDLES: invalid configured minute grid")
            seen.add(at)
            o, h, l, c = [positive(v) for v in row[1:5]]
            if not l <= min(o, c) <= max(o, c) <= h:
                raise Block("FYERS_CANDLES: inconsistent OHLC")
            bars.append({"at": at.isoformat(), "open": float(o), "high": float(h), "low": float(l), "close": float(c), "volume": float(number(row[5]))})
        return {"source": "fyers", "broker": "fyers", "as_of": instant().isoformat(), "token": token,
                "symbol": symbol, "exchange": exchange, "interval": interval, "bars": sorted(bars, key=lambda b: b["at"]),
                "empty": not bars, "range_end_exclusive": last.isoformat(), "continuous": False}

    def quotes(self, symbols):
        if not isinstance(symbols, list) or not 1 <= len(symbols) <= 50:
            raise Block("FYERS_QUOTES: provide 1-50 distinct master symbol tickers")
        for key in symbols:
            symbol_key(key)
        if len(set(symbols)) != len(symbols):
            raise Block("FYERS_QUOTES: duplicate requested symbols")
        data = self.request("GET", QUOTES, query={"symbols": ",".join(symbols)})
        if not isinstance(data.get("d"), list):
            raise Block("FYERS_QUOTES: quote array required")
        quotes, seen = {}, set()
        try:
            for row in data["d"]:
                key = symbol_key(row["n"])
                if key not in symbols or key in seen or row["s"] not in {"ok", "error"}:
                    raise Block("FYERS_QUOTES: inconsistent response identity/status")
                seen.add(key)
                if row["s"] == "error":
                    continue
                v = row["v"]
                token = v["fyToken"]
                exchange = token_exchange(token)
                symbol = key.split(":", 1)[1]
                if v["symbol"] != key or full_symbol(symbol, exchange) != key:
                    raise Block("FYERS_QUOTES: conflicting identity")
                # tt is documented only as 'today's time'. Use ltp and ltt from the SAME depth snapshot.
                depth = self.request("GET", DEPTH, query={"symbol": key, "ohlcv_flag": "1"}).get("d")
                if not isinstance(depth, dict) or set(depth) != {key} or not isinstance(depth[key], dict):
                    raise Block("FYERS_QUOTES: missing/conflicting depth snapshot")
                q = depth[key]
                quotes[key] = {"token": token, "symbol": symbol, "exchange": exchange, "source": "fyers",
                    "price": float(positive(q["ltp"])), "as_of": epoch(q["ltt"]).isoformat(),
                    "timestamp_basis": "depth last-trade time", "quote_tt": v.get("tt")}
        except (KeyError, TypeError):
            raise Block("FYERS_QUOTES: malformed quote/depth record") from None
        return {"source": "fyers", "fetched_at": instant().isoformat(), "requested": symbols, "quotes": quotes,
                "missing": [key for key in symbols if key not in quotes]}

    def account_id(self):
        data = self.request("GET", PROFILE).get("data")
        expected = self.credentials()["FYERS_ACCOUNT_ID"]
        if not isinstance(data, dict) or data.get("fy_id") != expected:
            raise Block("FYERS_ACCOUNT: observed profile does not match configured account ID")
        return expected

    def account(self):
        identity = self.account_id()
        funds = self.request("GET", FUNDS).get("fund_limit")
        positions = self.request("GET", POSITIONS).get("netPositions")
        orders = self.request("GET", ORDERS).get("orderBook")
        if any(not isinstance(v, list) for v in (funds, positions, orders)) or not funds:
            raise Block("FYERS_ACCOUNT: funds/positions/orders arrays required; null is not empty account")
        for row in funds + positions + orders:
            if not isinstance(row, dict) or ("fy_id" in row and row["fy_id"] != identity):
                raise Block("FYERS_ACCOUNT: malformed or cross-account row")
        return {"source": "fyers", "broker": "fyers", "account_id": identity, "as_of": instant().isoformat(),
                "margins": funds, "positions": positions, "orders": orders, "reconciled": False}

    def margin(self, p):
        required = {"symbol", "qty", "type", "side", "productType", "limitPrice", "stopPrice", "disclosedQty",
                    "validity", "offlineOrder", "stopLoss", "takeProfit", "isSliceOrder"}
        if not isinstance(p, dict) or set(p) != required:
            raise Block("MARGIN_PAYLOAD: use exact FYERS payload.json from size")
        symbol_key(p["symbol"])
        if (type(p["side"]) is not int or p["side"] not in {-1, 1} or type(p["type"]) is not int or p["type"] != 1 or
                p["productType"] != "INTRADAY" or p["validity"] != "DAY" or p["offlineOrder"] is not False or p["isSliceOrder"] is not False or
                any(number(p[k]) != 0 for k in ("stopPrice", "disclosedQty", "stopLoss", "takeProfit"))):
            raise Block("MARGIN_PAYLOAD: only regular intraday LIMIT/DAY previews supported")
        integer(p["qty"]); positive(p["limitPrice"])
        identity = self.account_id()
        body = {"data": [{k: p[k] for k in ("symbol", "qty", "side", "type", "productType", "limitPrice", "stopPrice", "stopLoss", "takeProfit")}]}
        data = self.request("POST", MARGIN, body=body).get("data")
        if not isinstance(data, dict):
            raise Block("FYERS_MARGIN: margin object required")
        return {"source": "fyers", "account_id": identity, "as_of": instant().isoformat(), "payload_hash": fingerprint(p),
                "required_inr": float(positive(data.get("margin_total"), "proposed-order margin"))}
