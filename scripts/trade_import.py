"""Normalize broker trade books into journal fills; never infer executions from orders."""
from collections import defaultdict
from datetime import datetime
import argparse
import json
import sqlite3
from pathlib import Path
import sys

from core import Block, BROKERS, IST, fresh, instant, integer, positive, validate_instrument
from live_broker import identifier
from live_state import normalize, match_instrument, _product
from trader import read
from journal import Journal

ROUTES = {"kite": "/trades", "upstox": "/v2/order/trades/get-trades-for-day", "dhan": "/trades",
          "angel": "/rest/secure/angelbroking/order/v1/getTradeBook", "fyers": "/api/v3/tradebook"}


def trade_time(value, day, broker):
    if not isinstance(value, str) or not value.strip(): raise Block("TRADE_TIME: timestamp required")
    if broker == "angel" and len(value) == 8:
        value = day + " " + value  # getTradeBook is explicitly a current-day API
    parsed = None
    try: parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        for fmt in ("%d-%b-%Y %H:%M:%S", "%d-%m-%Y %H:%M:%S"):
            try: parsed = datetime.strptime(value, fmt); break
            except ValueError: pass
    if parsed is None: raise Block("TRADE_TIME: unsupported broker timestamp")
    if parsed.tzinfo is None: parsed = parsed.replace(tzinfo=IST)
    parsed = parsed.astimezone(IST)
    if parsed.date().isoformat() != day: raise Block("TRADE_TIME: wrong trade-book session")
    return parsed


def trade_product(broker, row, allow_carry=False):
    if allow_carry:
        if broker != "kite" or row.get("product") not in {"MIS", "CNC", "NRML"}:
            raise Block("TRADES: carry normalization supports Kite MIS/CNC/NRML only")
        return row["product"]
    return _product(broker, row)


def normalize_trades(raw, snapshot, instruments, now, *, allow_carry=False):
    b, account = raw["broker"], raw["account_id"]
    if b not in BROKERS or raw.get("source") != b or any(snapshot[k] != raw[k] for k in ("broker", "account_id", "date")):
        raise Block("TRADES: broker/account/session mismatch")
    fresh(raw["as_of"], now, 60, "trade book"); fresh(snapshot["as_of"], now, 60, "order snapshot")
    if raw["date"] != now.date().isoformat() or not isinstance(raw["trades"], list): raise Block("TRADES: current-day array required")
    for i in instruments:
        validate_instrument(i, now)
        if i["broker"] != b: raise Block("TRADES: master belongs to another broker")
    orders = {r["order_id"]: r for r in snapshot["orders"]}
    if len(orders) != len(snapshot["orders"]): raise Block("TRADES: duplicate order identity")
    out, seen, totals = [], {}, defaultdict(int)
    for r in raw["trades"]:
        if b == "kite" and "user_id" in r and r["user_id"] != account:
            raise Block("TRADES: cross-account fill")
        for key in ("dhanClientId", "clientcode", "clientId", "fy_id"):
            if key in r and r[key] != account: raise Block("TRADES: cross-account fill")
        if b in {"kite", "upstox"}:
            oid, tid, q, price, side = [r[k] for k in ("order_id", "trade_id", "quantity", "average_price", "transaction_type")]
            stamp = r["fill_timestamp"] if b == "kite" else r["exchange_timestamp"]
        elif b == "dhan":
            oid, tid, q, price, side, stamp = [r[k] for k in ("orderId", "exchangeTradeId", "tradedQuantity", "tradedPrice", "transactionType", "exchangeTime")]
        elif b == "angel":
            oid, tid, q, price, side, stamp = [r[k] for k in ("orderid", "fillid", "fillsize", "fillprice", "transactiontype", "filltime")]
        else:
            oid, tid, q, price, direction, stamp = [r[k] for k in ("orderNumber", "tradeNumber", "tradedQty", "tradePrice", "side", "orderDateTime")]
            if type(direction) is not int or direction not in {1, -1}: raise Block("TRADES: invalid side")
            side = "BUY" if direction == 1 else "SELL"
        if isinstance(tid, bool) or tid is None: raise Block("TRADES: invalid trade ID")
        oid, tid = identifier(oid), identifier(str(tid))
        if oid not in orders: raise Block("TRADES: fill has no matching account order")
        order = orders[oid]
        # Angel may omit symboltoken in trade rows; match its exact symbol/exchange
        # through a unique same-day master row, then verify the known order token.
        if b == "angel" and "symboltoken" not in r:
            matches = [i for i in instruments if i["exchange"] == r["exchange"] and i["symbol"] == r["tradingsymbol"]]
            if len(matches) != 1: raise Block("TRADES: ambiguous Angel master identity")
            r = {**r, "symboltoken": matches[0]["token"]}
        ident = match_instrument(b, r, instruments)
        product = trade_product(b, r, allow_carry)
        if any(order[k] != ident[k] for k in ident) or order["side"] != side or product != order["product"]:
            raise Block("TRADES: order/fill identity or direction mismatch")
        at = trade_time(stamp, raw["date"], b)
        if at > instant(raw["as_of"]): raise Block("TRADES: fill after source snapshot")
        key = raw["date"] + ":" + ident["exchange"] + ":" + oid + ":" + tid
        fill = {**ident, "fill_id": key, "broker_trade_id": tid, "order_id": oid, "product": product, "side": side,
                "quantity": integer(q), "price": float(positive(price)), "at": at.isoformat(), "source": b + "_trade_book",
                "timestamp_basis": "broker_trade_book_timestamp", "fees": {}, "fees_final": False}
        if key in seen:
            if seen[key] != fill: raise Block("TRADES: conflicting duplicate execution")
            continue
        seen[key] = fill; out.append(fill); totals[oid] += fill["quantity"]
    for oid, order in orders.items():
        if totals[oid] != order["filled_quantity"]: raise Block("TRADES: trade-book/order-book quantities disagree; refresh")
    return {"broker": b, "account_id": account, "source": b + "_trade_book", "as_of": raw["as_of"], "fills": out}


def fetch(client, broker, account_id, instruments, clock=instant):
    if client.account_id() != account_id: raise Block("TRADES: connected account mismatch")
    started = clock()
    rows = client.request("GET", ROUTES[broker])
    if broker == "fyers": rows = rows["tradeBook"]
    as_of = clock()
    snapshot = normalize(client.account(), instruments, clock())
    if clock().date() != started.date(): raise Block("TRADES: collection crossed midnight; refresh")
    raw = {"broker": broker, "account_id": account_id, "source": broker, "as_of": as_of.isoformat(), "date": as_of.date().isoformat(), "trades": rows}
    return normalize_trades(raw, snapshot, instruments, clock())


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--broker", choices=sorted(BROKERS), required=True); p.add_argument("--account-id", required=True)
    p.add_argument("--master", required=True); p.add_argument("--fetch", action="store_true"); p.add_argument("--input"); p.add_argument("--snapshot")
    p.add_argument("--now"); p.add_argument("--out", required=True); p.add_argument("--journal")
    a = p.parse_args(argv)
    try:
        path = Path(a.out)
        if path.exists() or bool(a.input) == a.fetch: raise Block("INPUT: choose a new output and exactly one input mode")
        master = read(a.master)["instruments"]
        if a.fetch:
            if a.now or a.snapshot: raise Block("FETCH: live clock and observed snapshot required")
            from broker import adapter
            batch = fetch(adapter(a.broker), a.broker, a.account_id, master)
        else:
            raw = read(a.input)
            if raw["broker"] != a.broker or raw["account_id"] != a.account_id: raise Block("ACCOUNT: input differs from selected account")
            batch = normalize_trades(raw, read(a.snapshot), master, instant(a.now))
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("x", encoding="utf-8") as stream: json.dump(batch, stream, indent=2, allow_nan=False)
        result = {"saved": str(path), "fills": len(batch["fills"]), "fees_final": False, "submitted": False}
        if a.journal:
            j = Journal(a.journal)
            try: result["journal"] = j.import_fills(batch, instant(a.now))
            finally: j.close()
        print(json.dumps(result)); return 0
    except (Block, ValueError, KeyError, TypeError, OSError, sqlite3.Error):
        print(json.dumps({"status": "BLOCK", "reason": "Trade import failed validation; no private broker response printed", "submitted": False})); return 2


if __name__ == "__main__": sys.exit(main())
