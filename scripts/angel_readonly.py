"""Angel One SmartAPI reads and margin estimates, with no execution routes.

Setup and normalization: references/angel.md. Uses no SDK import or IP-discovery service.
"""
from __future__ import annotations

import ipaddress
import json
import os
import re
import time as monotonic_clock
import urllib.error
import urllib.request
from datetime import date, datetime, time, timedelta
from email.utils import parsedate_to_datetime

from core import Block, IST, fingerprint, instant, integer, number, positive

MASTER_URL = "https://margincalculator.angelone.in/OpenAPI_File/files/OpenAPIScripMaster.json"
PREFIX = "/rest/secure/angelbroking/"
PROFILE = PREFIX + "user/v1/getProfile"
FUNDS = PREFIX + "user/v1/getRMS"
POSITIONS = PREFIX + "order/v1/getPosition"
ORDERS = PREFIX + "order/v1/getOrderBook"
TRADES = PREFIX + "order/v1/getTradeBook"
CANDLES = PREFIX + "historical/v1/getCandleData"
QUOTES = PREFIX + "market/v1/quote"
MARGIN = PREFIX + "margin/v1/batch"
EXCHANGES = {"NSE", "BSE", "NFO", "BFO"}
MAX_BYTES = 64 * 1024 * 1024
MONTHS = {m: n for n, m in enumerate(("JAN", "FEB", "MAR", "APR", "MAY", "JUN", "JUL", "AUG", "SEP", "OCT", "NOV", "DEC"), 1)}


def symbol_token(value):
    if not isinstance(value, str) or not re.fullmatch(r"[1-9][0-9]*", value):
        raise Block("ANGEL_TOKEN: numeric master token string required")
    return value


def exchange_name(value):
    if value not in EXCHANGES:
        raise Block("ANGEL_EXCHANGE: supported NSE/BSE cash or F&O exchange required")
    return value


def expiry_date(value):
    try:
        match = re.fullmatch(r"(\d{1,2})([A-Z]{3})(\d{4})", value)
        if not match:
            raise ValueError("format")
        return date(int(match[3]), MONTHS[match[2]], int(match[1])).isoformat()
    except (TypeError, ValueError, KeyError):
        raise Block("ANGEL_EXPIRY: expected dated DDMMMYYYY contract expiry") from None


def trade_time(value):
    try:
        match = re.fullmatch(r"(\d{2})-([A-Za-z]{3})-(\d{4}) (\d{2}):(\d{2}):(\d{2})", value)
        if not match:
            raise ValueError("format")
        return datetime(int(match[3]), MONTHS[match[2].upper()], int(match[1]),
                        int(match[4]), int(match[5]), int(match[6]), tzinfo=IST)
    except (TypeError, ValueError, KeyError):
        raise Block("ANGEL_TIME: missing/invalid exchange trade timestamp") from None


def source_time(value):
    if not isinstance(value, str) or not value.strip():
        raise Block("ANGEL_TIME: explicit candle timestamp required")
    try:
        return instant(value)
    except ValueError:
        raise Block("ANGEL_TIME: invalid offset-bearing candle timestamp") from None


def decode_json(raw):
    def reject(value):
        raise ValueError("nonfinite JSON")
    try:
        return json.loads(raw, parse_constant=reject)
    except (ValueError, UnicodeError):
        raise Block("ANGEL_RESPONSE: invalid JSON; body suppressed") from None


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise Block("ANGEL_REDIRECT: refused to forward authorization or device headers")


class AngelReadOnly:
    def __init__(self, opener=None):
        self.opener = opener

    def _read(self, request):
        try:
            opener = self.opener or urllib.request.build_opener(NoRedirect()).open
            started = monotonic_clock.monotonic()
            with opener(request, timeout=30) as response:
                headers, chunks, count = response.headers, [], 0
                while True:
                    if monotonic_clock.monotonic() - started > 60:
                        raise Block("ANGEL_NETWORK: download deadline exceeded; no retry")
                    chunk = response.read1(min(65536, MAX_BYTES + 1 - count))
                    if not chunk:
                        break
                    chunks.append(chunk)
                    count += len(chunk)
                    if count > MAX_BYTES:
                        raise Block("ANGEL_RESPONSE: response too large")
                raw = b"".join(chunks)
            return raw, headers
        except urllib.error.HTTPError as exc:
            raise Block(f"ANGEL_HTTP_{exc.code}: read failed; check local session/entitlement; no retry") from None
        except (urllib.error.URLError, TimeoutError, OSError):
            raise Block("ANGEL_NETWORK: data unavailable; no automatic retry") from None

    @staticmethod
    def credentials():
        names = ("ANGEL_API_KEY", "ANGEL_ACCESS_TOKEN", "ANGEL_CLIENT_CODE", "ANGEL_LOCAL_IP", "ANGEL_PUBLIC_IP", "ANGEL_MAC_ADDRESS")
        values = {name: os.environ.get(name) for name in names}
        if any(not value or not value.strip() for value in values.values()):
            raise Block("ANGEL_AUTH: configure API key, access token, client code and local IP/public IP/MAC settings locally; never send credentials in chat")
        if any(any(ch.isspace() for ch in value) for value in values.values()):
            raise Block("ANGEL_AUTH: local settings contain invalid whitespace")
        try:
            ipaddress.ip_address(values["ANGEL_LOCAL_IP"])
            ipaddress.ip_address(values["ANGEL_PUBLIC_IP"])
        except ValueError:
            raise Block("ANGEL_AUTH: valid local/public IP settings required") from None
        if not re.fullmatch(r"(?:[0-9A-Fa-f]{2}:){5}[0-9A-Fa-f]{2}", values["ANGEL_MAC_ADDRESS"]):
            raise Block("ANGEL_AUTH: expected colon-separated MAC setting")
        return values

    def request(self, method, path, body=None):
        if not ((method == "GET" and path in {PROFILE, FUNDS, POSITIONS, ORDERS, TRADES}) or
                (method == "POST" and path in {CANDLES, QUOTES, MARGIN})):
            raise Block("READ_ONLY: endpoint/method not allowed; live execution unavailable")
        settings = self.credentials()
        req = urllib.request.Request("https://apiconnect.angelone.in" + path, method=method,
            data=json.dumps(body, allow_nan=False).encode() if body is not None else None,
            headers={"Authorization": "Bearer " + settings["ANGEL_ACCESS_TOKEN"], "X-PrivateKey": settings["ANGEL_API_KEY"],
                "X-ClientLocalIP": settings["ANGEL_LOCAL_IP"], "X-ClientPublicIP": settings["ANGEL_PUBLIC_IP"],
                "X-MACAddress": settings["ANGEL_MAC_ADDRESS"], "X-UserType": "USER", "X-SourceID": "WEB",
                "Accept": "application/json", "Content-Type": "application/json"})
        raw, _ = self._read(req)
        result = decode_json(raw)
        if (not isinstance(result, dict) or result.get("status") is not True or result.get("errorcode") not in (None, "") or
                result.get("errorCode") not in (None, "") or "data" not in result):
            raise Block("ANGEL_RESPONSE: unsuccessful or malformed response; body suppressed")
        return result["data"]

    def instruments(self):
        raw, headers = self._read(urllib.request.Request(MASTER_URL, headers={"Accept": "application/json"}))
        try:
            modified = parsedate_to_datetime(headers["Last-Modified"])
            if modified.tzinfo is None or modified > instant():
                raise ValueError("source time")
        except (ValueError, TypeError, KeyError):
            raise Block("ANGEL_MASTER: valid source Last-Modified timestamp required") from None
        return self.normalize_instruments(decode_json(raw), modified.astimezone(IST).date().isoformat(), modified.isoformat())

    @staticmethod
    def normalize_instruments(rows, master_date, source_updated_at):
        date.fromisoformat(master_date)
        if not isinstance(rows, list) or not rows:
            raise Block("ANGEL_MASTER: nonempty JSON instrument array required")
        out, seen = [], set()
        try:
            for r in rows:
                exchange, instrument_type = r["exch_seg"], r["instrumenttype"]
                if exchange not in EXCHANGES:
                    continue
                kind = {"FUTIDX": "future", "FUTSTK": "future", "OPTIDX": "option", "OPTSTK": "option"}.get(instrument_type)
                if instrument_type in {"", "EQ"} and exchange in {"NSE", "BSE"}:
                    kind = "equity"
                if kind is None:
                    continue
                if (kind == "equity") != (exchange in {"NSE", "BSE"}):
                    raise Block("ANGEL_MASTER: kind/exchange mismatch")
                token = symbol_token(r["token"])
                if (exchange, token) in seen:
                    raise Block("ANGEL_MASTER: duplicate exchange/token")
                seen.add((exchange, token))
                if not isinstance(r["symbol"], str) or not r["symbol"].strip():
                    raise Block("ANGEL_MASTER: missing trading symbol")
                option_type = r["symbol"][-2:] if kind == "option" else None
                if kind == "option" and option_type not in {"CE", "PE"}:
                    raise Block("ANGEL_MASTER: option symbol lacks CE/PE designation")
                out.append({"broker": "angel", "exchange": exchange, "token": token, "symbol": r["symbol"], "kind": kind,
                    "lot_size": integer(r["lotsize"]), "tick_size": float(positive(r["tick_size"]) / 100),
                    "raw_tick_size": r["tick_size"], "raw_strike": r["strike"], "master_price_divisor": 100,
                    "strike": float(positive(r["strike"]) / 100) if kind == "option" else None,
                    "expiry": expiry_date(r["expiry"]) if kind != "equity" else None, "master_date": master_date,
                    "option_type": option_type, "underlying": r.get("name"),
                    "underlying_type": None if kind == "equity" else ("index" if instrument_type.endswith("IDX") else "stock"),
                    "broker_instrument": instrument_type})
        except (KeyError, TypeError, AttributeError):
            raise Block("ANGEL_MASTER: malformed instrument row") from None
        if not out:
            raise Block("ANGEL_MASTER: no supported instruments")
        return {"source": "angel", "as_of": instant().isoformat(), "source_updated_at": source_updated_at,
                "instruments": out, "note": "Membership, intraday eligibility and surveillance need separate evidence."}

    def candles(self, token, symbol, exchange, start, end, interval="5minute"):
        symbol_token(token); exchange_name(exchange)
        if not isinstance(symbol, str) or not symbol.strip():
            raise Block("ANGEL_IDENTITY: verified master symbol required")
        if interval == "5minute":
            first, last = source_time(start), source_time(end)
            if any(t.second or t.microsecond for t in (first, last)):
                raise Block("ANGEL_RANGE: request bounds need exact minutes")
            max_days, api_interval = 100, "FIVE_MINUTE"
        elif interval == "day":
            first = datetime.combine(date.fromisoformat(start), time(), IST)
            last = datetime.combine(date.fromisoformat(end), time(), IST)
            max_days, api_interval = 2000, "ONE_DAY"
            if last.date() > instant().date():
                raise Block("ANGEL_RANGE: daily end date is exclusive and must not include today")
        else:
            raise Block("ANGEL_RANGE: unsupported interval")
        if first >= last or last > instant() or last-first > timedelta(days=max_days):
            raise Block("ANGEL_RANGE: invalid/future range or exceeds interval request limit")
        rows = self.request("POST", CANDLES, {"exchange": exchange, "symboltoken": token, "interval": api_interval,
            "fromdate": first.strftime("%Y-%m-%d %H:%M"), "todate": last.strftime("%Y-%m-%d %H:%M")})
        if not isinstance(rows, list):
            raise Block("ANGEL_CANDLES: expected candle array; null is not an empty history")
        bars, seen, boundary = [], set(), 0
        for row in rows:
            if not isinstance(row, list) or len(row) != 6:
                raise Block("ANGEL_CANDLES: expected six candle fields")
            at = source_time(row[0])
            if at in seen or at < first or at > last:
                raise Block("ANGEL_CANDLES: duplicate or out-of-range timestamp")
            seen.add(at)
            if at == last:
                boundary += 1
                continue
            if interval == "5minute" and (at.minute % 5 or at.second or at.microsecond):
                raise Block("ANGEL_CANDLES: invalid five-minute grid")
            o, h, l, c = [positive(value) for value in row[1:5]]
            if not l <= min(o, c) <= max(o, c) <= h:
                raise Block("ANGEL_CANDLES: inconsistent OHLC")
            bars.append({"at": at.isoformat(), "open": float(o), "high": float(h), "low": float(l),
                         "close": float(c), "volume": float(number(row[5]))})
        return {"source": "angel", "broker": "angel", "as_of": instant().isoformat(), "symbol": symbol, "exchange": exchange,
                "token": token, "interval": interval, "bars": sorted(bars, key=lambda b: b["at"]), "empty": not bars,
                "range_end_exclusive": last.isoformat(), "excluded_end_boundary": boundary}

    def quotes(self, symbols):
        if not isinstance(symbols, list) or not 1 <= len(symbols) <= 50 or len(set(symbols)) != len(symbols):
            raise Block("ANGEL_QUOTES: provide 1-50 distinct EXCHANGE:TOKEN values")
        grouped = {}
        for key in symbols:
            if not isinstance(key, str) or key.count(":") != 1:
                raise Block("ANGEL_QUOTES: use EXCHANGE:TOKEN from master")
            exchange, token = key.split(":")
            exchange_name(exchange); symbol_token(token)
            grouped.setdefault(exchange, []).append(token)
        data = self.request("POST", QUOTES, {"mode": "FULL", "exchangeTokens": grouped})
        if not isinstance(data, dict) or not isinstance(data.get("fetched"), list) or not isinstance(data.get("unfetched"), list):
            raise Block("ANGEL_QUOTES: expected fetched and unfetched arrays")
        quotes, unavailable = {}, set()
        try:
            for row in data["fetched"]:
                exchange, token = exchange_name(row["exchange"]), symbol_token(row["symbolToken"])
                key = exchange + ":" + token
                if key not in symbols or key in quotes or not isinstance(row["tradingSymbol"], str) or not row["tradingSymbol"].strip():
                    raise Block("ANGEL_QUOTES: duplicate/unrequested instrument or missing symbol")
                quotes[key] = {"token": token, "exchange": exchange, "symbol": row["tradingSymbol"],
                    "price": float(positive(row["ltp"])), "source": "angel", "as_of": trade_time(row["exchTradeTime"]).isoformat(),
                    "exchange_feed_time": trade_time(row["exchFeedTime"]).isoformat(), "timestamp_basis": "exchange trade time in IST"}
            for row in data["unfetched"]:
                key = exchange_name(row["exchange"]) + ":" + symbol_token(row["symbolToken"])
                if key not in symbols or key in quotes or key in unavailable:
                    raise Block("ANGEL_QUOTES: inconsistent unfetched identity")
                unavailable.add(key)
        except (KeyError, TypeError):
            raise Block("ANGEL_QUOTES: malformed quote record") from None
        return {"source": "angel", "fetched_at": instant().isoformat(), "requested": symbols, "quotes": quotes,
                "missing": [key for key in symbols if key not in quotes], "reported_unfetched": sorted(unavailable)}

    def account_id(self):
        data = self.request("GET", PROFILE)
        expected = self.credentials()["ANGEL_CLIENT_CODE"]
        if not isinstance(data, dict) or data.get("clientcode") != expected:
            raise Block("ANGEL_ACCOUNT: observed profile does not match local client code")
        return expected

    def account(self):
        identity = self.account_id()
        funds, positions, orders = self.request("GET", FUNDS), self.request("GET", POSITIONS), self.request("GET", ORDERS)
        if not isinstance(funds, dict) or not isinstance(positions, list) or not isinstance(orders, list):
            raise Block("ANGEL_ACCOUNT: malformed funds/positions/orders; null is not an empty account")
        for row in positions + orders:
            if not isinstance(row, dict) or ("clientcode" in row and row["clientcode"] != identity):
                raise Block("ANGEL_ACCOUNT: invalid row or account identity mismatch")
        return {"source": "angel", "broker": "angel", "account_id": identity, "as_of": instant().isoformat(),
                "margins": funds, "positions": positions, "orders": orders, "reconciled": False}

    def margin(self, order_payload):
        p = order_payload
        required = {"variety", "tradingsymbol", "symboltoken", "transactiontype", "exchange", "ordertype",
                    "producttype", "duration", "price", "quantity", "squareoff", "stoploss"}
        if not isinstance(p, dict) or set(p) != required:
            raise Block("MARGIN_PAYLOAD: use exact Angel payload.json from size")
        if (p["variety"] != "NORMAL" or p["ordertype"] != "LIMIT" or p["producttype"] != "INTRADAY" or
                p["duration"] != "DAY" or p["transactiontype"] not in {"BUY", "SELL"} or
                number(p["squareoff"]) != 0 or number(p["stoploss"]) != 0 or
                not isinstance(p["tradingsymbol"], str) or not p["tradingsymbol"].strip()):
            raise Block("MARGIN_PAYLOAD: only normal intraday LIMIT/DAY previews supported")
        request = {"positions": [{"exchange": exchange_name(p["exchange"]), "qty": integer(p["quantity"]),
                    "price": float(positive(p["price"])), "productType": "INTRADAY", "token": symbol_token(p["symboltoken"]),
                    "tradeType": p["transactiontype"], "orderType": "LIMIT"}]}
        identity = self.account_id()
        data = self.request("POST", MARGIN, request)
        if not isinstance(data, dict):
            raise Block("ANGEL_MARGIN: expected margin object")
        return {"source": "angel", "account_id": identity, "as_of": instant().isoformat(),
                "payload_hash": fingerprint(p), "required_inr": float(positive(data.get("totalMarginRequired"), "required margin"))}
