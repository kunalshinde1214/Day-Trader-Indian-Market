"""Upstox market/account reads and margin estimates. No trading endpoints.

API contracts and normalization notes: references/brokers.md.
"""
from __future__ import annotations

import gzip
import io
import json
import os
import re
import urllib.error
import urllib.parse
import urllib.request
from datetime import date, datetime
from email.utils import parsedate_to_datetime

from core import Block, IST, fingerprint, instant, integer, number, positive

MASTER_URL = "https://assets.upstox.com/market-quote/instruments/exchange/complete.json.gz"
SEGMENTS = {"NSE_EQ": "NSE", "BSE_EQ": "BSE", "NSE_FO": "NFO", "BSE_FO": "BFO"}
KEY_PATTERN = r"(?:NSE|BSE)_(?:EQ|FO)\|[A-Za-z0-9]+"
MAX_BYTES = 64 * 1024 * 1024


def instrument_key(value):
    if not isinstance(value, str) or not re.fullmatch(KEY_PATTERN, value):
        raise Block("UPSTOX_KEY: use a cash/F&O instrument_key from the Upstox master")
    return value


def source_time(value):
    if not isinstance(value, str) or not value.strip():
        raise Block("UPSTOX_TIMESTAMP: missing source time; fetch time cannot replace it")
    try:
        return instant(value)
    except ValueError:
        raise Block("UPSTOX_TIMESTAMP: invalid source time") from None


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise Block("UPSTOX_REDIRECT: refused redirect; credentials never forwarded")


def decode_json(raw):
    def invalid(value):
        raise ValueError("non-finite JSON")
    try:
        return json.loads(raw, parse_constant=invalid)
    except (ValueError, UnicodeError):
        raise Block("UPSTOX_RESPONSE: invalid JSON; body suppressed") from None


class UpstoxReadOnly:
    def __init__(self, opener=None):
        self.opener = opener

    def _read(self, request):
        try:
            opener = self.opener or urllib.request.build_opener(NoRedirect()).open
            with opener(request, timeout=20) as response:
                raw = response.read(MAX_BYTES + 1)
                headers = response.headers
            if len(raw) > MAX_BYTES:
                raise Block("UPSTOX_RESPONSE: response too large")
            return raw, headers
        except urllib.error.HTTPError as exc:
            raise Block(f"UPSTOX_HTTP_{exc.code}: read failed; check session and entitlement locally; no retry") from None
        except (urllib.error.URLError, TimeoutError, OSError):
            raise Block("UPSTOX_NETWORK: data unavailable; no automatic retry") from None

    def request(self, method, path, query=None, body=None):
        fixed = {"/v2/market-quote/quotes", "/v2/user/profile", "/v2/user/get-funds-and-margin",
                 "/v2/portfolio/short-term-positions", "/v2/portfolio/long-term-holdings", "/v3/portfolio/mtf-positions",
                 "/v2/order/retrieve-all", "/v2/order/trades/get-trades-for-day"}
        encoded_key = r"(?:NSE|BSE)_(?:EQ|FO)%7C[A-Za-z0-9]+"
        history = rf"/v3/historical-candle/{encoded_key}/(?:minutes/[15]|days/1)/\d{{4}}-\d{{2}}-\d{{2}}/\d{{4}}-\d{{2}}-\d{{2}}"
        intraday = rf"/v3/historical-candle/intraday/{encoded_key}/(?:minutes/[15]|days/1)"
        allowed = method == "GET" and (path in fixed or re.fullmatch(history, path) or re.fullmatch(intraday, path))
        if not allowed and not (method == "POST" and path == "/v2/charges/margin"):
            raise Block("READ_ONLY: endpoint/method not allowed; live execution unavailable")
        token = os.environ.get("UPSTOX_ACCESS_TOKEN")
        if not token:
            raise Block("UPSTOX_AUTH: set UPSTOX_ACCESS_TOKEN locally; never send it in chat")
        url = "https://api.upstox.com" + path
        if query:
            url += "?" + urllib.parse.urlencode(query)
        req = urllib.request.Request(url, method=method,
            data=json.dumps(body, allow_nan=False).encode() if body is not None else None,
            headers={"Authorization": "Bearer " + token, "Accept": "application/json", "Content-Type": "application/json"})
        raw, _ = self._read(req)
        data = decode_json(raw)
        if not isinstance(data, dict) or data.get("status") != "success" or "data" not in data:
            raise Block("UPSTOX_RESPONSE: missing/failed response; body suppressed")
        return data["data"]

    def instruments(self):
        # Separate fixed public host: never send the API authorization header here.
        raw, headers = self._read(urllib.request.Request(MASTER_URL, headers={"Accept": "application/gzip"}))
        try:
            modified = parsedate_to_datetime(headers["Last-Modified"])
            if modified.tzinfo is None:
                raise ValueError("missing timezone")
            with gzip.GzipFile(fileobj=io.BytesIO(raw)) as stream:
                decoded = stream.read(MAX_BYTES + 1)
            if len(decoded) > MAX_BYTES:
                raise ValueError("expanded master too large")
            rows = decode_json(decoded)
        except (ValueError, TypeError, KeyError, OSError, EOFError):
            raise Block("UPSTOX_MASTER: invalid archive or missing Last-Modified evidence") from None
        master_date = modified.astimezone(IST).date().isoformat()
        if modified > instant():
            raise Block("UPSTOX_MASTER: future source timestamp")
        return self.normalize_instruments(rows, master_date, modified.isoformat())

    @staticmethod
    def normalize_instruments(rows, master_date, source_updated_at):
        if not isinstance(rows, list) or not rows:
            raise Block("UPSTOX_MASTER: expected a nonempty instrument array")
        date.fromisoformat(master_date)
        result, seen = [], set()
        try:
            for row in rows:
                kind = {"EQ": "equity", "FUT": "future", "CE": "option", "PE": "option"}.get(row["instrument_type"])
                segment = row["segment"]
                if kind is None or segment not in SEGMENTS:
                    continue
                if (kind == "equity") != segment.endswith("_EQ"):
                    raise Block("UPSTOX_MASTER: kind/segment mismatch")
                key = instrument_key(row["instrument_key"])
                if key.split("|")[0] != segment or key in seen:
                    raise Block("UPSTOX_MASTER: duplicate key or segment mismatch")
                seen.add(key)
                if not isinstance(row["trading_symbol"], str) or not row["trading_symbol"].strip():
                    raise Block("UPSTOX_MASTER: missing symbol")
                expiry = None if kind == "equity" else datetime.fromtimestamp(float(positive(row["expiry"])) / 1000, IST).date().isoformat()
                result.append({"broker": "upstox", "symbol": row["trading_symbol"], "token": key,
                    "exchange": SEGMENTS[segment], "segment": segment, "kind": kind,
                    "lot_size": integer(row["lot_size"]), "tick_size": float(positive(row["tick_size"]) / 100),
                    "raw_tick_size": row["tick_size"], "tick_conversion": "master paise / 100 to INR",
                    "expiry": expiry, "master_date": master_date,
                    "underlying_type": {"INDEX": "index", "EQUITY": "stock"}.get(row.get("underlying_type")) if kind != "equity" else None,
                    "underlying": row.get("underlying_symbol"), "strike": float(number(row.get("strike_price", 0))),
                    "option_type": row["instrument_type"] if kind == "option" else None})
        except (KeyError, TypeError, OverflowError, OSError):
            raise Block("UPSTOX_MASTER: malformed instrument row") from None
        if not result:
            raise Block("UPSTOX_MASTER: no supported instruments")
        return {"source": "upstox", "as_of": instant().isoformat(), "source_updated_at": source_updated_at,
                "instruments": result, "note": "Membership, MIS eligibility and surveillance clearance require separate evidence."}

    def candles(self, token, symbol, exchange, start=None, end=None, interval="5minute", intraday=False):
        key = instrument_key(token)
        if SEGMENTS[key.split("|")[0]] != exchange or not symbol:
            raise Block("UPSTOX_IDENTITY: key/exchange and symbol required")
        if interval not in {"minute", "5minute", "day"}:
            raise Block("HISTORY_RANGE: unsupported interval")
        unit = {"minute":"minutes/1", "5minute":"minutes/5", "day":"days/1"}[interval]
        encoded = urllib.parse.quote(key, safe="")
        if intraday:
            path = f"/v3/historical-candle/intraday/{encoded}/{unit}"
            first = last = instant().date()
        else:
            first, last = date.fromisoformat(start), date.fromisoformat(end)
            if first > last or last >= instant().date() or (interval in {"minute", "5minute"} and (last-first).days > 30):
                raise Block("HISTORY_RANGE: use prior dates, <=30 days for five-minute bars; intraday for today")
            path = f"/v3/historical-candle/{encoded}/{unit}/{last}/{first}"
        data = self.request("GET", path)
        bars, seen = [], set()
        try:
            for row in data["candles"]:
                if not isinstance(row, list) or len(row) < 6:
                    raise Block("UPSTOX_CANDLES: incomplete row")
                at = source_time(row[0])
                if at in seen or not first <= at.date() <= last:
                    raise Block("UPSTOX_CANDLES: duplicate or out-of-range timestamp")
                seen.add(at)
                o, h, l, c = [positive(v) for v in row[1:5]]
                if not l <= min(o, c) <= max(o, c) <= h:
                    raise Block("UPSTOX_CANDLES: inconsistent OHLC")
                if interval in {"minute", "5minute"} and (at.second or at.microsecond or at.minute % (1 if interval == "minute" else 5)):
                    raise Block("UPSTOX_CANDLES: invalid configured minute grid")
                bars.append({"at": at.isoformat(), "open": float(o), "high": float(h), "low": float(l),
                             "close": float(c), "volume": float(number(row[5]))})
        except (KeyError, TypeError, IndexError):
            raise Block("UPSTOX_CANDLES: malformed response") from None
        return {"source": "upstox", "broker": "upstox", "as_of": instant().isoformat(), "symbol": symbol,
                "exchange": exchange, "token": key, "interval": interval, "bars": sorted(bars, key=lambda x: x["at"]),
                "empty": not bars}

    def quotes(self, symbols):
        if not 1 <= len(symbols) <= 500 or len(set(symbols)) != len(symbols):
            raise Block("QUOTE_INPUT: provide 1-500 distinct Upstox instrument keys")
        for key in symbols:
            instrument_key(key)
        data = self.request("GET", "/v2/market-quote/quotes", query={"instrument_key": ",".join(symbols)})
        if not isinstance(data, dict):
            raise Block("UPSTOX_QUOTES: expected quote object")
        quotes = {}
        try:
            for row in data.values():
                key = instrument_key(row["instrument_token"])
                if key not in symbols or key in quotes:
                    raise Block("UPSTOX_QUOTES: unexpected or duplicate instrument")
                quotes[key] = {"token": key, "exchange": SEGMENTS[key.split("|")[0]],
                    "price": float(positive(row["last_price"])), "source": "upstox",
                    "as_of": source_time(row["timestamp"]).isoformat(), "last_trade_time": row.get("last_trade_time")}
        except (KeyError, TypeError):
            raise Block("UPSTOX_QUOTES: incomplete quote; fetch time cannot replace quote time") from None
        return {"source": "upstox", "fetched_at": instant().isoformat(), "requested": symbols,
                "missing": [key for key in symbols if key not in quotes], "quotes": quotes}

    def account_id(self):
        data = self.request("GET", "/v2/user/profile")
        value = data.get("user_id") if isinstance(data, dict) else None
        if not isinstance(value, str) or not value.strip():
            raise Block("UPSTOX_PROFILE: missing account identity")
        return value

    def account(self):
        identity = self.account_id()
        funds = self.request("GET", "/v2/user/get-funds-and-margin")
        positions = self.request("GET", "/v2/portfolio/short-term-positions")
        orders = self.request("GET", "/v2/order/retrieve-all")
        if not isinstance(funds, dict) or not isinstance(positions, list) or not isinstance(orders, list):
            raise Block("UPSTOX_ACCOUNT: malformed account snapshot")
        return {"source": "upstox", "broker": "upstox", "account_id": identity, "as_of": instant().isoformat(),
                "margins": funds, "positions": positions, "orders": orders, "reconciled": False}

    def margin(self, order_payload):
        required = {"instrument_token", "transaction_type", "quantity", "product", "order_type", "price",
                    "validity", "disclosed_quantity", "trigger_price", "is_amo", "slice"}
        if set(order_payload) != required:
            raise Block("MARGIN_PAYLOAD: use the exact Upstox payload.json from size")
        p = order_payload
        key = instrument_key(p["instrument_token"])
        if (p["product"] != "I" or p["order_type"] != "LIMIT" or p["validity"] != "DAY" or
                p["transaction_type"] not in {"BUY", "SELL"} or p["is_amo"] is not False or p["slice"] is not False or
                number(p["disclosed_quantity"]) != 0 or number(p["trigger_price"]) != 0):
            raise Block("MARGIN_PAYLOAD: only regular intraday limit previews supported")
        request = {"instruments": [{"instrument_key": key, "quantity": integer(p["quantity"]),
                    "transaction_type": p["transaction_type"], "product": "I", "price": float(positive(p["price"]))}]}
        identity = self.account_id()
        data = self.request("POST", "/v2/charges/margin", body=request)
        if not isinstance(data, dict) or not isinstance(data.get("margins"), list) or len(data["margins"]) != 1:
            raise Block("UPSTOX_MARGIN: expected one margin result")
        # Keep the full preview hash even though this endpoint accepts only a subset of its fields.
        required_margin = max(positive(data["required_margin"]), positive(data["final_margin"]))
        return {"source": "upstox", "account_id": identity, "as_of": instant().isoformat(),
                "payload_hash": fingerprint(p), "required_inr": float(required_margin)}
