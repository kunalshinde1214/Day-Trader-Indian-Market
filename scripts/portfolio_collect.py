"""Read-only Kite carry accounting against reviewed opening inventory.

Never infers opening costs, corporate actions, settlement or execution sequence.
The bundle is accounting evidence, not an execution ledger or order approval.
"""
from collections import defaultdict
import argparse
import json
from pathlib import Path
import sqlite3
import sys

from core import Block, fingerprint, fresh, instant, integer, positive
from journal import Journal
from live_broker import identifier
from live_state import match_instrument, STATUS
from portfolio import FIELDS, array, evidence, item_key, quantity, validate_opening, save_opening, reconcile_portfolio
from trade_import import normalize_trades, trade_product, trade_time
from trader import read

ROUTES = {"orders": "/orders", "trades": "/trades", "holdings": "/portfolio/holdings", "positions": "/portfolio/positions"}
HOLDING_FIELDS = ("exchange", "tradingsymbol", "instrument_token", "product", "isin", "quantity", "used_quantity",
                  "opening_quantity", "t1_quantity", "realised_quantity", "short_quantity", "collateral_quantity", "discrepancy", "mtf")
POSITION_FIELDS = ("exchange", "tradingsymbol", "instrument_token", "product", "quantity", "overnight_quantity",
                   "multiplier", "day_buy_quantity", "day_sell_quantity")


def stable_book(book):
    """Compare inventory and all order/trade fields, excluding changing position marks."""
    def rows(values, fields=None):
        return sorted(fingerprint({k: r[k] for k in fields} if fields else r) for r in array(values))
    return {"orders": rows(book["orders"]), "trades": rows(book["trades"]),
            "holdings": rows(book["holdings"], HOLDING_FIELDS),
            "net": rows(book["positions"]["net"], POSITION_FIELDS),
            "day": rows(book["positions"]["day"], POSITION_FIELDS)}


def canonical_order(row, instruments, master, day, as_of, account_id):
    if "user_id" in row and row["user_id"] != account_id:
        raise Block("PORTFOLIO_COLLECT: cross-account order")
    ident = match_instrument("kite", row, instruments)
    product = trade_product("kite", row, True)
    item_key({**ident, "product": product}, master)
    at = trade_time(row["order_timestamp"], day, "kite")
    if at > as_of or row["variety"] != "regular":
        raise Block("PORTFOLIO_COLLECT: unsupported order date/variety")
    q, filled = integer(row["quantity"]), integer(row["filled_quantity"], minimum=0)
    status = STATUS.get(row["status"], "UNKNOWN")
    if status in {"OPEN", "PARTIAL"} and 0 < filled < q: status = "PARTIAL"
    if status == "UNKNOWN" or row["transaction_type"] not in {"BUY", "SELL"} or filled > q:
        raise Block("PORTFOLIO_COLLECT: ambiguous order")
    if (status == "FILLED" and filled != q or status == "REJECTED" and filled or
            status == "OPEN" and filled or status == "PARTIAL" and not 0 < filled < q):
        raise Block("PORTFOLIO_COLLECT: contradictory order")
    return {**ident, "product": product, "order_id": identifier(row["order_id"]), "quantity": q,
            "filled_quantity": filled, "side": row["transaction_type"], "status": status}


def normalize(raw, opening, instruments, now, *, execution_details=False):
    day = now.date().isoformat()
    pair, master = validate_opening(opening, instruments, now)
    evidence(raw, now, day, pair)
    if pair[0] != "kite" or raw.get("schema") != "kite_portfolio_read_v1" or raw.get("source") != "kite":
        raise Block("PORTFOLIO_COLLECT: complete Kite source required")
    started, ended = instant(raw["started_at"]), instant(raw["as_of"])
    fresh(raw["started_at"], now, 60, "account collection start")
    if started.date() != now.date() or started > ended or instant(opening["as_of"]) > started:
        raise Block("PORTFOLIO_COLLECT: invalid collection window")
    if stable_book(raw) != stable_book(raw["verification"]):
        raise Block("PORTFOLIO_COLLECT: account changed during reads; refresh")
    header = {"broker": "kite", "account_id": pair[1], "source": "kite", "date": day,
              "as_of": raw["as_of"], "complete": True}
    orders = [canonical_order(r, instruments, master, day, ended, pair[1]) for r in array(raw["orders"])]
    snapshot = {**header, "schema": "portfolio_snapshot_v1", "inventory_events": [], "orders": orders, "positions": []}
    batch = normalize_trades(raw, snapshot, instruments, now, allow_carry=True)
    opens = {tuple(r[k] for k in FIELDS): r for r in opening["positions"]}
    traded = defaultdict(lambda: {"BUY": 0, "SELL": 0})
    for fill in batch["fills"]: traded[tuple(fill[k] for k in FIELDS)][fill["side"]] += fill["quantity"]

    def key_for(row):
        return item_key({**match_instrument("kite", row, instruments), "product": trade_product("kite", row, True)}, master)

    holdings, isins = {}, set()
    for row in array(raw["holdings"]):
        key = key_for(row)
        isin = row["isin"]
        if key[3] != "CNC" or key in holdings or not isinstance(isin, str) or not isin or isin in isins:
            raise Block("PORTFOLIO_COLLECT: duplicate/cross-exchange/unsupported holding")
        isins.add(isin)
        # The first adapter supports settled, unpledged delivery holdings only.
        if any(integer(row[k], minimum=0) for k in ("t1_quantity", "short_quantity", "collateral_quantity")) or row["discrepancy"] is not False:
            raise Block("PORTFOLIO_COLLECT: unsettled/pledged/discrepant holding unsupported")
        mtf = row["mtf"]
        if not isinstance(mtf, dict) or any(integer(mtf[k], minimum=0) for k in ("quantity", "used_quantity")):
            raise Block("PORTFOLIO_COLLECT: MTF unsupported")
        q, used, carried = (integer(row[k], minimum=0) for k in ("quantity", "used_quantity", "opening_quantity"))
        if integer(row["realised_quantity"], minimum=0) != q or q + used != carried or carried != quantity(opens.get(key, {}).get("quantity", 0)):
            raise Block("PORTFOLIO_COLLECT: holdings/opening mismatch; review settlement/adjustments")
        if used > traded[key]["SELL"]: raise Block("PORTFOLIO_COLLECT: used holdings have no matching sales")
        holdings[key] = carried

    net, activity = {}, {}
    for name, dest in (("net", net), ("day", activity)):
        for row in array(raw["positions"][name]):
            key = key_for(row)
            if key in dest or positive(row["multiplier"]) != 1:
                raise Block("PORTFOLIO_COLLECT: duplicate position or non-unit multiplier")
            q, overnight = quantity(row["quantity"]), quantity(row["overnight_quantity"])
            buys, sells = integer(row["day_buy_quantity"], minimum=0), integer(row["day_sell_quantity"], minimum=0)
            if (buys, sells) != (traded[key]["BUY"], traded[key]["SELL"]):
                raise Block("PORTFOLIO_COLLECT: position activity/trades mismatch")
            expected_open = quantity(opens.get(key, {}).get("quantity", 0)) if key[3] == "NRML" and name == "net" else 0
            if overnight != expected_open or q != overnight + buys - sells:
                raise Block("PORTFOLIO_COLLECT: carry/position mismatch or product conversion")
            dest[key] = q
    if any(key not in net or key not in activity for key in traded if any(traded[key].values())):
        raise Block("PORTFOLIO_COLLECT: missing position activity")
    for key in opens:
        if key[3] == "CNC" and key not in holdings or key[3] == "NRML" and key not in net:
            raise Block("PORTFOLIO_COLLECT: opening inventory missing from broker")

    for key in sorted(holdings.keys() | net.keys()):
        # Holdings quantity is already reduced by sales. Restore used_quantity
        # through verified opening_quantity before applying the CNC net delta.
        q = holdings.get(key, 0) + net.get(key, 0) if key[3] == "CNC" else net[key]
        row = {**dict(zip(FIELDS, key)), "quantity": q}
        if q:
            quote = raw["quotes"][key[0] + ":" + key[1]]
            if str(quote["instrument_token"]) != key[2]: raise Block("PORTFOLIO_COLLECT: quote token mismatch")
            quoted, traded_at = (trade_time(quote[k], day, "kite") for k in ("timestamp", "last_trade_time"))
            if traded_at > quoted or quoted > ended: raise Block("PORTFOLIO_COLLECT: future quote")
            fresh(traded_at.isoformat(), now, 60, "portfolio last trade")
            row.update(mark_price=float(positive(quote["last_price"])), mark_source="kite_quote",
                       mark_as_of=traded_at.isoformat(), price_basis="per_unit_inr")
        snapshot["positions"].append(row)

    # Reuse the complete accounting gate in an isolated in-memory journal.
    # Invalid evidence never mutates the user's journal or opening inventory.
    journal = Journal(":memory:")
    try:
        save_opening(journal, opening, instruments, now)
        journal.import_fills(batch, now, allow_carry=True)
        report = reconcile_portfolio(journal, snapshot, instruments, now)
    finally: journal.close()
    if not report["inventory_reconciled"]: raise Block("PORTFOLIO_COLLECT: inventory did not reconcile")
    result = {"schema": "portfolio_collection_v1", "opening": opening, "snapshot": snapshot, "fills": batch, "report": report,
            "raw_hash": fingerprint(raw), "opening_hash": fingerprint(opening), "execution_eligible": False,
            "submitted": False, "authenticated_acceptance": False}
    if execution_details:
        from live_state import normalize_order
        result["execution_orders"]=[normalize_order("kite",row,instruments,allow_carry=True) for row in raw["orders"]]
    return result


def fetch(client, account_id, opening, instruments, clock=instant, *, execution_details=False):
    started = clock()
    if validate_opening(opening, instruments, started)[0] != ("kite", account_id) or client.account_id() != account_id:
        raise Block("PORTFOLIO_COLLECT: connected/opening account mismatch")
    book = {k: client.request("GET", route) for k, route in ROUTES.items()}
    stable_book(book)  # Fail malformed responses before further reads.
    symbols = sorted({r["exchange"] + ":" + r["tradingsymbol"] for r in book["holdings"] + book["positions"]["net"]})
    quotes = {}
    for offset in range(0, len(symbols), 500):
        quotes.update(client.quotes(symbols[offset:offset + 500])["data"])
    verification = {k: client.request("GET", route) for k, route in ROUTES.items()}
    if client.account_id() != account_id: raise Block("PORTFOLIO_COLLECT: connected account changed")
    ended = clock()
    raw = {**book, "verification": verification, "quotes": quotes, "schema": "kite_portfolio_read_v1", "broker": "kite",
           "account_id": account_id, "source": "kite", "date": ended.date().isoformat(), "started_at": started.isoformat(),
           "as_of": ended.isoformat(), "complete": True}
    return normalize(raw, opening, instruments, ended, execution_details=execution_details)


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--account-id", required=True); p.add_argument("--opening", required=True); p.add_argument("--master", required=True)
    mode = p.add_mutually_exclusive_group(required=True)
    mode.add_argument("--fetch", action="store_true"); mode.add_argument("--input")
    p.add_argument("--now"); p.add_argument("--out", required=True)
    a = p.parse_args(argv)
    try:
        path = Path(a.out)
        if path.exists() or a.fetch and a.now: raise Block("PORTFOLIO_COLLECT: new output/live clock required")
        opening, instruments = read(a.opening), read(a.master)["instruments"]
        if opening.get("account_id") != a.account_id: raise Block("PORTFOLIO_COLLECT: selected account mismatch")
        if a.fetch:
            from kite_readonly import KiteReadOnly
            result = fetch(KiteReadOnly(), a.account_id, opening, instruments)
        else: result = normalize(read(a.input), opening, instruments, instant(a.now))
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("x", encoding="utf-8") as stream: json.dump(result, stream, indent=2, allow_nan=False)
        print(json.dumps({"status": "saved", "report_status": result["report"]["status"], "submitted": False})); return 0
    except (Block, ValueError, KeyError, TypeError, OSError, sqlite3.Error):
        print(json.dumps({"status": "BLOCK", "reason": "Kite portfolio evidence failed validation; private response suppressed", "submitted": False})); return 2


if __name__ == "__main__": sys.exit(main())
