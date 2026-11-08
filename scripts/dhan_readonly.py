"""Dhan v2 read-only snapshots and margin estimates; no execution endpoints.

Contracts, unit conventions and integration limits: references/dhan.md.
"""
from __future__ import annotations

import csv
import io
import json
import os
import re
import urllib.error
import urllib.request
from datetime import date, datetime, time, timedelta
from email.utils import parsedate_to_datetime

from core import Block, IST, fingerprint, instant, integer, number, positive

MASTER_URL = "https://images.dhan.co/api-data/api-scrip-master-detailed.csv"
MAX_BYTES = 64 * 1024 * 1024
SEGMENTS = {"NSE": "NSE_EQ", "BSE": "BSE_EQ", "NFO": "NSE_FNO", "BFO": "BSE_FNO"}
EXCHANGES = {v: k for k, v in SEGMENTS.items()}
KINDS = {"EQUITY": "equity", "FUTIDX": "future", "FUTSTK": "future", "OPTIDX": "option", "OPTSTK": "option"}


def security_id(value):
    if not isinstance(value, str) or not re.fullmatch(r"[1-9][0-9]*", value):
        raise Block("DHAN_ID: use the security ID string from the Dhan master")
    return value


def segment_for(exchange):
    if exchange not in SEGMENTS:
        raise Block("DHAN_SEGMENT: supported NSE/BSE cash or F&O segment required")
    return SEGMENTS[exchange]


def quote_key(value):
    if not isinstance(value, str) or ":" not in value:
        raise Block("DHAN_QUOTE_KEY: use SEGMENT:SECURITY_ID from the master")
    segment, token = value.split(":", 1)
    if segment not in EXCHANGES:
        raise Block("DHAN_QUOTE_KEY: unsupported segment")
    return segment, security_id(token)


def trade_time(value):
    # The v2 quote's documented local clock has no offset; attach IST explicitly.
    if not isinstance(value, str) or not value.strip():
        raise Block("DHAN_TIMESTAMP: last trade time is missing")
    try:
        return datetime.strptime(value, "%d/%m/%Y %H:%M:%S").replace(tzinfo=IST)
    except ValueError:
        raise Block("DHAN_TIMESTAMP: invalid last trade time") from None


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise Block("DHAN_REDIRECT: refused to forward credentials")


class DhanReadOnly:
    def __init__(self, opener=None):
        self.opener = opener

    def _read(self, request):
        try:
            opener = self.opener or urllib.request.build_opener(NoRedirect()).open
            with opener(request, timeout=20) as response:
                raw, headers = response.read(MAX_BYTES + 1), response.headers
            if len(raw) > MAX_BYTES:
                raise Block("DHAN_RESPONSE: response too large")
            return raw, headers
        except urllib.error.HTTPError as exc:
            raise Block(f"DHAN_HTTP_{exc.code}: read failed; check local session and entitlement; no retry") from None
        except (urllib.error.URLError, TimeoutError, OSError):
            raise Block("DHAN_NETWORK: data unavailable; no automatic retry") from None

    @staticmethod
    def credentials():
        client_id = os.environ.get("DHAN_CLIENT_ID")
        token = os.environ.get("DHAN_ACCESS_TOKEN")
        if not client_id or not token:
            raise Block("DHAN_AUTH: configure DHAN_CLIENT_ID and DHAN_ACCESS_TOKEN locally, never in chat")
        if not re.fullmatch(r"[0-9]+", client_id):
            raise Block("DHAN_AUTH: invalid local client ID")
        return client_id, token

    def request(self, method, path, body=None):
        allowed = ((method == "GET" and path in {"/fundlimit", "/positions", "/orders", "/trades"}) or
                   (method == "POST" and path in {"/charts/historical", "/charts/intraday", "/marketfeed/quote", "/margincalculator"}))
        if not allowed:
            raise Block("READ_ONLY: endpoint/method not allowed; live execution unavailable")
        client_id, token = self.credentials()
        req = urllib.request.Request("https://api.dhan.co/v2" + path, method=method,
            data=json.dumps(body, allow_nan=False).encode() if body is not None else None,
            headers={"access-token": token, "client-id": client_id, "Accept": "application/json", "Content-Type": "application/json"})
        raw, _ = self._read(req)
        def invalid_constant(value):
            raise ValueError("non-finite JSON")
        try:
            result = json.loads(raw, parse_constant=invalid_constant)
        except (ValueError, UnicodeError):
            raise Block("DHAN_RESPONSE: invalid JSON; body suppressed") from None
        if not isinstance(result, (dict, list)):
            raise Block("DHAN_RESPONSE: expected object or array")
        if isinstance(result, dict) and (any(k in result for k in ("errorType", "errorCode", "errorMessage")) or
                                        ("status" in result and result["status"] != "success")):
            raise Block("DHAN_RESPONSE: broker failure; body suppressed")
        return result

    def instruments(self):
        # Public host has no token/client-ID headers.
        raw, headers = self._read(urllib.request.Request(MASTER_URL, headers={"Accept": "text/csv"}))
        try:
            modified = parsedate_to_datetime(headers["Last-Modified"])
            if modified.tzinfo is None or modified > instant():
                raise ValueError("invalid source timestamp")
            reader = csv.DictReader(io.StringIO(raw.decode("utf-8-sig")))
            required = {"EXCH_ID", "SEGMENT", "SECURITY_ID", "INSTRUMENT", "SYMBOL_NAME", "LOT_SIZE", "TICK_SIZE",
                        "SM_EXPIRY_DATE", "STRIKE_PRICE", "OPTION_TYPE", "UNDERLYING_SYMBOL"}
            if not reader.fieldnames or not required <= set(reader.fieldnames):
                raise ValueError("missing detailed columns")
            rows = list(reader)
        except (ValueError, KeyError, TypeError, UnicodeError, csv.Error):
            raise Block("DHAN_MASTER: detailed CSV and source Last-Modified timestamp required") from None
        return self.normalize_instruments(rows, modified.astimezone(IST).date().isoformat(), modified.isoformat())

    @staticmethod
    def normalize_instruments(rows, master_date, source_updated_at):
        date.fromisoformat(master_date)
        result, seen, names = [], set(), set()
        if not isinstance(rows, list):
            raise Block("DHAN_MASTER: expected instrument rows")
        try:
            for r in rows:
                kind = KINDS.get(r["INSTRUMENT"])
                if kind is None or r["EXCH_ID"] not in {"NSE", "BSE"} or r["SEGMENT"] not in {"E", "D"}:
                    continue
                if (kind == "equity") != (r["SEGMENT"] == "E"):
                    raise Block("DHAN_MASTER: kind/segment mismatch")
                exchange = r["EXCH_ID"] if kind == "equity" else {"NSE": "NFO", "BSE": "BFO"}[r["EXCH_ID"]]
                token = security_id(r["SECURITY_ID"])
                key = (exchange, token)
                if key in seen:
                    raise Block("DHAN_MASTER: duplicate segment/security ID")
                seen.add(key)
                underlying = r["UNDERLYING_SYMBOL"]
                if not isinstance(underlying, str) or not underlying.strip() or underlying == "NA":
                    raise Block("DHAN_MASTER: missing underlying/symbol")
                expiry, strike, option_type = None, None, None
                if kind == "equity":
                    symbol = underlying
                else:
                    expiry = date.fromisoformat(r["SM_EXPIRY_DATE"]).isoformat()
                    symbol = f"{underlying}-{expiry}-FUT"
                    if kind == "option":
                        strike = positive(r["STRIKE_PRICE"], "strike")
                        option_type = r["OPTION_TYPE"]
                        if option_type not in {"CE", "PE"}:
                            raise Block("DHAN_MASTER: missing option type")
                        symbol = f"{underlying}-{expiry}-{format(strike.normalize(), 'f')}-{option_type}"
                # Equity series can share a symbol; retain the security ID to keep every master row unambiguous.
                symbol = f"{symbol}@{token}"
                if (exchange, symbol) in names:
                    raise Block("DHAN_MASTER: duplicate canonical symbol")
                names.add((exchange, symbol))
                result.append({"broker": "dhan", "symbol": symbol, "token": token, "exchange": exchange,
                    "broker_segment": segment_for(exchange), "broker_instrument": r["INSTRUMENT"],
                    "broker_symbol": r["SYMBOL_NAME"], "display_name": r.get("DISPLAY_NAME"),
                    "underlying": underlying, "kind": kind, "lot_size": integer(r["LOT_SIZE"]),
                    "tick_size": float(positive(r["TICK_SIZE"]) / 100), "raw_tick_size": r["TICK_SIZE"],
                    "tick_conversion": "detailed master paise / 100 to INR", "master_date": master_date,
                    "expiry": expiry, "strike": float(strike) if strike is not None else None, "option_type": option_type,
                    "underlying_type": None if kind == "equity" else ("index" if r["INSTRUMENT"].endswith("IDX") else "stock"),
                    "raw_surveillance_flag": r.get("ASM_GSM_FLAG"), "series": r.get("SERIES")})
        except (KeyError, TypeError, AttributeError):
            raise Block("DHAN_MASTER: malformed instrument row") from None
        if not result:
            raise Block("DHAN_MASTER: no supported instruments")
        return {"source": "dhan", "as_of": instant().isoformat(), "source_updated_at": source_updated_at,
                "instruments": result, "note": "Canonical symbols include security IDs; membership and full restrictions require separate evidence."}

    def candles(self, token, symbol, exchange, start, end, interval="5minute", instrument_type=None):
        token = security_id(token)
        segment = segment_for(exchange)
        if instrument_type not in KINDS or (KINDS[instrument_type] == "equity") != segment.endswith("_EQ") or not symbol:
            raise Block("DHAN_HISTORY: verified broker_instrument and matching exchange required")
        if interval == "5minute":
            if not start or not end:
                raise Block("DHAN_HISTORY: explicit offset-bearing bounds required")
            first, last = instant(start), instant(end)
            if first >= last or last > instant() or last-first > timedelta(days=90):
                raise Block("DHAN_HISTORY: invalid range; at most 90 days ending no later than now")
            path = "/charts/intraday"
            bounds = {"fromDate": first.strftime("%Y-%m-%d %H:%M:%S"), "toDate": last.strftime("%Y-%m-%d %H:%M:%S"), "interval": "5"}
        elif interval == "day":
            first = datetime.combine(date.fromisoformat(start), time(), IST)
            last = datetime.combine(date.fromisoformat(end), time(), IST)
            if first >= last or last.date() > instant().date():
                raise Block("DHAN_HISTORY: daily range must contain prior days; end date is exclusive")
            path = "/charts/historical"
            bounds = {"fromDate": first.date().isoformat(), "toDate": last.date().isoformat()}
        else:
            raise Block("DHAN_HISTORY: unsupported interval")
        data = self.request("POST", path, {"securityId": token, "exchangeSegment": segment,
            "instrument": instrument_type, "oi": instrument_type != "EQUITY", **bounds})
        fields = ("timestamp", "open", "high", "low", "close", "volume")
        if not isinstance(data, dict) or not all(isinstance(data.get(k), list) for k in fields):
            raise Block("DHAN_CANDLES: missing candle arrays")
        length = len(data["timestamp"])
        if any(len(data[k]) != length for k in fields) or ("open_interest" in data and
                (not isinstance(data["open_interest"], list) or len(data["open_interest"]) != length)):
            raise Block("DHAN_CANDLES: unequal parallel arrays")
        bars, seen, boundary_count = [], set(), 0
        for n in range(length):
            try:
                at = datetime.fromtimestamp(integer(data["timestamp"][n], "epoch seconds"), IST)
            except (ValueError, OverflowError, OSError):
                raise Block("DHAN_CANDLES: invalid Unix-second timestamp") from None
            if at in seen or at < first or at > last:
                raise Block("DHAN_CANDLES: duplicate/out-of-range timestamp")
            seen.add(at)
            # Normalize both endpoints to a half-open range even if the server includes the final intraday bar.
            if at == last:
                boundary_count += 1
                continue
            if interval == "5minute" and (at.minute % 5 or at.second or at.microsecond):
                raise Block("DHAN_CANDLES: invalid five-minute grid")
            o, h, l, c = [positive(data[k][n]) for k in ("open", "high", "low", "close")]
            if not l <= min(o, c) <= max(o, c) <= h:
                raise Block("DHAN_CANDLES: inconsistent OHLC")
            bar = {"at": at.isoformat(), "open": float(o), "high": float(h), "low": float(l),
                   "close": float(c), "volume": float(number(data["volume"][n]))}
            if "open_interest" in data:
                bar["open_interest"] = float(number(data["open_interest"][n]))
            bars.append(bar)
        return {"source": "dhan", "broker": "dhan", "as_of": instant().isoformat(), "symbol": symbol, "exchange": exchange,
                "token": token, "interval": interval, "bars": sorted(bars, key=lambda b: b["at"]), "empty": not bars,
                "range_start": first.isoformat(), "range_end_exclusive": last.isoformat(), "excluded_end_boundary": boundary_count}

    def quotes(self, symbols):
        if not isinstance(symbols, list) or not 1 <= len(symbols) <= 1000 or len(set(symbols)) != len(symbols):
            raise Block("DHAN_QUOTES: provide 1-1000 distinct SEGMENT:SECURITY_ID values")
        grouped = {}
        for key in symbols:
            segment, token = quote_key(key)
            grouped.setdefault(segment, []).append(int(token))
        result = self.request("POST", "/marketfeed/quote", grouped)
        if not isinstance(result, dict) or result.get("status") != "success" or not isinstance(result.get("data"), dict):
            raise Block("DHAN_QUOTES: expected successful quote envelope")
        quotes = {}
        for segment, records in result["data"].items():
            if segment not in grouped or not isinstance(records, dict):
                raise Block("DHAN_QUOTES: unexpected segment or malformed records")
            for token, row in records.items():
                key = f"{segment}:{security_id(token)}"
                if key not in symbols or not isinstance(row, dict):
                    raise Block("DHAN_QUOTES: unexpected instrument or malformed quote")
                quotes[key] = {"token": token, "exchange": EXCHANGES[segment], "price": float(positive(row.get("last_price"))),
                    "source": "dhan", "as_of": trade_time(row.get("last_trade_time")).isoformat(),
                    "timestamp_basis": "last_trade_time interpreted in IST", "last_trade_time": row.get("last_trade_time")}
        return {"source": "dhan", "fetched_at": instant().isoformat(), "requested": symbols,
                "missing": [key for key in symbols if key not in quotes], "quotes": quotes}

    def _funds(self):
        data = self.request("GET", "/fundlimit")
        expected, _ = self.credentials()
        if not isinstance(data, dict) or not isinstance(data.get("dhanClientId"), str) or data["dhanClientId"] != expected:
            raise Block("DHAN_ACCOUNT: observed account identity does not match local client ID")
        number(data["availabelBalance"], "available balance", minimum="-1e30")
        return data

    def account_id(self):
        return self._funds()["dhanClientId"]

    def account(self):
        funds = self._funds()
        positions = self.request("GET", "/positions")
        orders = self.request("GET", "/orders")
        if not isinstance(positions, list) or not isinstance(orders, list):
            raise Block("DHAN_ACCOUNT: malformed positions/orders")
        for row in positions + orders:
            if not isinstance(row, dict) or row.get("dhanClientId") != funds["dhanClientId"]:
                raise Block("DHAN_ACCOUNT: row has missing or mismatched account identity")
        return {"source": "dhan", "broker": "dhan", "account_id": funds["dhanClientId"], "as_of": instant().isoformat(),
                "margins": funds, "positions": positions, "orders": orders, "reconciled": False}

    def margin(self, order_payload):
        p = order_payload
        required = {"dhanClientId", "transactionType", "exchangeSegment", "productType", "orderType", "validity",
                    "securityId", "quantity", "price", "triggerPrice", "disclosedQuantity", "afterMarketOrder"}
        if not isinstance(p, dict) or set(p) != required:
            raise Block("MARGIN_PAYLOAD: use exact Dhan payload.json from size")
        if (p["productType"] != "INTRADAY" or p["orderType"] != "LIMIT" or p["validity"] != "DAY" or
                p["transactionType"] not in {"BUY", "SELL"} or p["exchangeSegment"] not in EXCHANGES or
                p["afterMarketOrder"] is not False or number(p["triggerPrice"]) != 0 or number(p["disclosedQuantity"]) != 0):
            raise Block("MARGIN_PAYLOAD: only regular intraday LIMIT/DAY previews supported")
        security_id(p["securityId"])
        integer(p["quantity"])
        positive(p["price"])
        expected, _ = self.credentials()
        if p["dhanClientId"] != expected:
            raise Block("DHAN_ACCOUNT: payload belongs to another account")
        identity = self.account_id()
        body = {key: p[key] for key in ("dhanClientId", "exchangeSegment", "transactionType", "quantity", "productType", "securityId", "price", "triggerPrice")}
        data = self.request("POST", "/margincalculator", body)
        if not isinstance(data, dict):
            raise Block("DHAN_MARGIN: expected margin object")
        return {"source": "dhan", "account_id": identity, "as_of": instant().isoformat(),
                "payload_hash": fingerprint(p), "required_inr": float(positive(data.get("totalMargin"), "total margin"))}
