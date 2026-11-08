"""Opt-in Kite spot-index collection for daily.py. No account reads or orders.

Indices use their own master rows; they are never disguised as tradable equities.
Other providers can supply the same documented normalized daily input contract.
"""
import argparse
import csv
from datetime import timedelta
import io
import json
from pathlib import Path
import sys

from core import Block, IST, instant, integer
from daily import INDICES, session_bounds, ohlc
from kite_readonly import KiteReadOnly
from trader import read

NAMES = {"nifty": ("NSE", "NIFTY 50"), "banknifty": ("NSE", "NIFTY BANK"), "sensex": ("BSE", "SENSEX")}


def index_master(raw, selected):
    rows = list(csv.DictReader(io.StringIO(raw)))
    result = {}
    for key in selected:
        exchange, symbol = NAMES[key]
        matches = [r for r in rows if r.get("exchange") == exchange and r.get("tradingsymbol") == symbol and r.get("segment") == "INDICES"]
        if len(matches) != 1: raise Block("INDEX_MASTER: unique exact spot-index master row required")
        token = str(integer(matches[0]["instrument_token"], "index token"))
        result[key] = {"exchange": exchange, "symbol": symbol, "token": token}
    if len({r["token"] for r in result.values()}) != len(result): raise Block("INDEX_MASTER: duplicate tokens")
    return result


def fetch_candles(client, token, interval, start, end):
    data = client.request("GET", f"/instruments/historical/{integer(token)}/{interval}",
                          query={"from": start.strftime("%Y-%m-%d %H:%M:%S"), "to": end.strftime("%Y-%m-%d %H:%M:%S"), "oi": 0})
    if not isinstance(data["candles"], list): raise Block("INDEX_HISTORY: explicit candles required")
    result = []
    for row in data["candles"]:
        if not isinstance(row, list) or len(row) < 5: raise Block("INDEX_HISTORY: malformed OHLC")
        at = instant(row[0])
        values = ohlc(dict(zip(("open", "high", "low", "close"), row[1:5])))
        result.append({"at": at.isoformat(), **values})
    return result


def collect(client, calendar, selected, clock=instant, events=None, with_context=False, membership=None):
    now = clock(); start, end, phase = session_bounds(calendar, now)
    if any(INDICES[k][1] not in calendar.get("exchanges", []) for k in selected): raise Block("CALENDAR: missing exchange coverage")
    raw_master = client.request("GET", "/instruments", csv_result=True)
    master = index_master(raw_master, selected)
    result = {"session": calendar, "indices": {}, "collection_errors": {}, "collector": "kite_spot_indices", "submitted": False}
    if events is not None: result["events"] = events
    for key, i in master.items():
        try:
            previous_day_end = start.replace(hour=0, minute=0, second=0, microsecond=0) - timedelta(seconds=1)
            rows = fetch_candles(client, i["token"], "day", start - timedelta(days=90), previous_day_end)
            dates = [instant(r["at"]).date().isoformat() for r in rows]
            if dates != sorted(set(dates)) or any(d >= calendar["date"] for d in dates): raise Block("HISTORY: duplicate, unordered or current daily candle")
            prior = [r for r, d in zip(rows, dates) if d == calendar["previous_session"]]
            if len(prior) != 1: raise Block("HISTORY: missing exact previous session")
            fetched = clock()
            item = {"index_id": key, "price_basis": "spot_index_points", "source": "kite", "as_of": fetched.isoformat(),
                    "broker_identity": i, "previous": {**ohlc(prior[0]), "source": "kite_historical_day", "as_of": fetched.isoformat(), "session": calendar["previous_session"]},
                    "history": {"source": "kite_historical_day", "as_of": fetched.isoformat(), "rows": [
                        {"session": d, "close": r["close"], "available_at": fetched.isoformat()} for r, d in zip(rows, dates)]}}
            if phase != "preopen":
                stop = min(clock(), end) - timedelta(seconds=1)
                bars = fetch_candles(client, i["token"], "5minute", start, stop) if stop > start else []
                item["candles"] = {"source": "kite_historical_5minute", "as_of": clock().isoformat(), "interval": "5minute", "rows": bars}
            item["as_of"] = clock().isoformat()
            result["indices"][key] = item
        except (Block, ValueError, TypeError, KeyError, OSError):
            result["collection_errors"][key] = "Broker index history unavailable or inconsistent; no substitute instrument used"
    if with_context:
        from daily_context import enrich
        enrich(result, client, raw_master, selected, supplied=membership, clock=clock)
    result["as_of"] = clock().isoformat()
    return result


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--broker", choices=["kite"], required=True); p.add_argument("--calendar", required=True)
    p.add_argument("--indices", nargs="+", choices=list(INDICES), default=list(INDICES)); p.add_argument("--events"); p.add_argument("--out", required=True)
    p.add_argument("--with-context", action="store_true"); p.add_argument("--membership")
    a = p.parse_args(argv)
    try:
        path = Path(a.out)
        if path.exists(): raise Block("OUTPUT: choose a new snapshot filename")
        if len(a.indices) != len(set(a.indices)): raise Block("INDEX: duplicate requested index")
        if a.membership and not a.with_context: raise Block("INPUT: membership requires --with-context")
        result = collect(KiteReadOnly(), read(a.calendar), a.indices, events=read(a.events) if a.events else None,
                         with_context=a.with_context, membership=read(a.membership) if a.membership else None)
        body = json.dumps(result, indent=2, allow_nan=False); path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("x", encoding="utf-8") as stream: stream.write(body)
        print(json.dumps({"saved": str(path), "indices_received": list(result["indices"]), "missing": list(result["collection_errors"]), "submitted": False}))
        return 0 if result["indices"] else 2
    except (Block, ValueError, KeyError, TypeError, OSError):
        print(json.dumps({"status": "BLOCK", "reason": "Index collection failed; check calendar, session and history entitlement locally", "submitted": False})); return 2


if __name__ == "__main__": sys.exit(main())
