"""Carry-aware inventory accounting from sourced canonical evidence. No broker calls.

Reports cannot substitute for an intraday risk ledger or authorize execution.
Opening inventory changes only through audited corrections. No settlement or corporate
action is guessed from a difference in quantities.
"""
from collections import defaultdict
from datetime import date
from decimal import Decimal
import argparse
import json
from pathlib import Path
import sqlite3
import sys

from core import Block, fresh, instant, integer, number, positive, money, fingerprint, validate_instrument
from journal import Journal, identity
from trader import read

FIELDS = ("exchange", "symbol", "token", "product")
PRODUCTS = {"MIS", "CNC", "NRML"}
STATUSES = {"OPEN", "PARTIAL", "FILLED", "CANCELLED", "REJECTED", "UNKNOWN"}


def quantity(value):
    v = number(value, minimum="-1e30")
    if v != v.to_integral_value(): raise Block("PORTFOLIO: fractional quantity")
    return int(v)


def array(value):
    if not isinstance(value, list) or any(not isinstance(r, dict) for r in value):
        raise Block("PORTFOLIO: explicit object array required")
    return value


def evidence(value, now, day, broker_account=None, max_age=60):
    if not isinstance(value, dict): raise Block("PORTFOLIO: evidence object required")
    pair = identity(value)
    if broker_account is not None and pair != broker_account: raise Block("PORTFOLIO: account mismatch")
    if not isinstance(value.get("source"), str) or not value["source"].strip() or value.get("date") != day:
        raise Block("PORTFOLIO: sourced session-specific evidence required")
    fresh(value.get("as_of"), now, max_age, "portfolio evidence")
    if instant(value["as_of"]).date().isoformat() != day or value.get("complete") is not True:
        raise Block("PORTFOLIO: complete same-session evidence required")
    return pair


def master_map(instruments, broker, now):
    out = {}
    for i in array(instruments):
        validate_instrument(i, now)
        if i["broker"] != broker: raise Block("PORTFOLIO: foreign broker master")
        key = tuple(i[k] for k in FIELDS[:3])
        if key in out or any(k[0] == key[0] and k[2] == key[2] for k in out):
            raise Block("PORTFOLIO: duplicate master identity")
        out[key] = i
    return out


def item_key(row, master):
    if any(not isinstance(row.get(k), str) or not row[k].strip() for k in FIELDS):
        raise Block("PORTFOLIO: full string instrument/product identity required")
    key = tuple(row[k] for k in FIELDS)
    i = master.get(key[:3])
    if i is None or key[3] not in PRODUCTS: raise Block("PORTFOLIO: unmapped instrument/product")
    if key[3] == "CNC" and i["kind"] != "equity" or key[3] == "NRML" and i["kind"] == "equity":
        raise Block("PORTFOLIO: product/instrument mismatch")
    return key


def validate_opening(opening, instruments, now):
    day = now.date().isoformat()
    pair = evidence(opening, now, day, max_age=86400)
    if opening.get("schema") != "portfolio_opening_v1": raise Block("PORTFOLIO: opening schema required")
    if date.fromisoformat(opening["previous_session"]) >= now.date(): raise Block("PORTFOLIO: invalid previous session")
    if opening.get("adjustments_reviewed") is not True or opening.get("inventory_events") != []:
        raise Block("PORTFOLIO: unresolved corporate actions, conversions or inventory transfers")
    master = master_map(instruments, pair[0], now)
    seen = set()
    for row in array(opening["positions"]):
        key = item_key(row, master)
        q = quantity(row["quantity"])
        if key in seen or q == 0 or key[3] == "MIS" or key[3] == "CNC" and q < 0:
            raise Block("PORTFOLIO: duplicate, zero or unsupported opening position")
        seen.add(key)
        if abs(q) % integer(master[key[:3]]["lot_size"]): raise Block("PORTFOLIO: opening quantity violates master lot")
        positive(row["average_price"]); positive(row["previous_close"])
        if row.get("price_basis") != "per_unit_inr" or any(not isinstance(row.get(k), str) or not row[k].strip() for k in ("cost_source", "mark_source")):
            raise Block("PORTFOLIO: sourced per-unit opening cost and previous mark required")
        if row.get("mark_session") != opening["previous_session"]:
            raise Block("PORTFOLIO: previous mark session mismatch")
    return pair, master


def opening_table(journal):
    journal.db.execute("CREATE TABLE IF NOT EXISTS portfolio_openings (broker TEXT, account TEXT, day TEXT, hash TEXT, body TEXT, recorded_at TEXT, PRIMARY KEY(broker,account,day))")
    journal.db.execute("CREATE TABLE IF NOT EXISTS portfolio_opening_audit (id INTEGER PRIMARY KEY, broker TEXT, account TEXT, day TEXT, before_body TEXT, after_body TEXT, correction_body TEXT, recorded_at TEXT)")


def save_opening(journal, opening, instruments, now):
    pair, _ = validate_opening(opening, instruments, now)
    opening_table(journal)
    digest = fingerprint(opening)
    with journal.db:
        row = journal.db.execute("SELECT hash FROM portfolio_openings WHERE broker=? AND account=? AND day=?", (*pair, opening["date"])).fetchone()
        if row:
            if row[0] != digest: raise Block("PORTFOLIO: conflicting opening evidence; existing inventory preserved")
            return {"saved": False, "duplicate": True, "opening_hash": digest}
        journal.db.execute("INSERT INTO portfolio_openings VALUES (?,?,?,?,?,?)", (*pair, opening["date"], digest, json.dumps(opening, allow_nan=False), now.isoformat()))
    return {"saved": True, "duplicate": False, "opening_hash": digest}


def load_opening(journal, broker, account, day):
    opening_table(journal)
    row = journal.db.execute("SELECT body,hash FROM portfolio_openings WHERE broker=? AND account=? AND day=?", (broker, account, day)).fetchone()
    if row is None: raise Block("PORTFOLIO: no saved opening evidence for account/session")
    result = json.loads(row[0])
    if fingerprint(result) != row[1]: raise Block("PORTFOLIO: opening evidence integrity failed")
    return result


def correct_opening(journal, correction, instruments, now):
    pair = evidence(correction, now, now.date().isoformat())
    if correction.get("schema") != "portfolio_opening_correction_v1" or not isinstance(correction.get("reason"), str) or not correction["reason"].strip():
        raise Block("PORTFOLIO: explicit sourced correction reason required")
    replacement = correction["replacement"]
    replacement_pair, _ = validate_opening(replacement, instruments, now)
    if replacement_pair != pair or instant(replacement["as_of"]) > instant(correction["as_of"]):
        raise Block("PORTFOLIO: correction replacement identity/time mismatch")
    expected = correction["expected_opening_hash"]
    if not isinstance(expected, str) or len(expected) != 64: raise Block("PORTFOLIO: expected prior opening hash required")
    opening_table(journal)
    new_hash = fingerprint(replacement)
    journal.db.execute("BEGIN IMMEDIATE")
    try:
        original = load_opening(journal, *pair, correction["date"])
        old_hash = fingerprint(original)
        if old_hash == new_hash:
            # Only the exact previously recorded correction is a duplicate.
            previous = journal.db.execute("SELECT correction_body FROM portfolio_opening_audit WHERE broker=? AND account=? AND day=?", (*pair, correction["date"])).fetchall()
            if any(fingerprint(json.loads(row[0])) == fingerprint(correction) for row in previous):
                journal.db.rollback(); return {"updated": False, "duplicate": True, "opening_hash": new_hash}
            raise Block("PORTFOLIO: correction does not change opening evidence")
        if old_hash != expected or instant(replacement["as_of"]) < instant(original["as_of"]):
            raise Block("PORTFOLIO: stale correction; inspect current opening evidence")
        journal.db.execute("INSERT INTO portfolio_opening_audit(broker,account,day,before_body,after_body,correction_body,recorded_at) VALUES (?,?,?,?,?,?,?)",
                           (*pair, correction["date"], json.dumps(original, allow_nan=False), json.dumps(replacement, allow_nan=False), json.dumps(correction, allow_nan=False), now.isoformat()))
        journal.db.execute("UPDATE portfolio_openings SET hash=?,body=?,recorded_at=? WHERE broker=? AND account=? AND day=?",
                           (new_hash, json.dumps(replacement, allow_nan=False), now.isoformat(), *pair, correction["date"]))
        journal.db.commit()
    except BaseException:
        journal.db.rollback(); raise
    return {"updated": True, "duplicate": False, "previous_opening_hash": expected, "opening_hash": new_hash, "prior_reports_require_reconciliation": True}


def reconcile_portfolio(journal, snapshot, instruments, now):
    day = now.date().isoformat()
    pair = evidence(snapshot, now, day)
    if snapshot.get("schema") != "portfolio_snapshot_v1" or snapshot.get("inventory_events") != []:
        raise Block("PORTFOLIO: canonical snapshot with explicit no-unresolved-inventory-events required")
    opening = load_opening(journal, *pair, day)
    _, master = validate_opening(opening, instruments, now)
    if instant(opening["as_of"]) > instant(snapshot["as_of"]): raise Block("PORTFOLIO: snapshot predates opening evidence")
    fills = journal.fills(*pair, day)
    orders, positions, by_order, by_key = {}, {}, defaultdict(list), defaultdict(list)
    issues = []
    for order in array(snapshot["orders"]):
        key = item_key(order, master)
        oid = order["order_id"]
        if not isinstance(oid, str) or not oid or oid in orders: raise Block("PORTFOLIO: duplicate/missing order ID")
        q, f = integer(order["quantity"]), integer(order["filled_quantity"], minimum=0)
        status = order["status"]
        if order["side"] not in {"BUY", "SELL"} or status not in STATUSES or f > q:
            raise Block("PORTFOLIO: invalid order state")
        if q % integer(master[key[:3]]["lot_size"]) or f % integer(master[key[:3]]["lot_size"]):
            raise Block("PORTFOLIO: order units violate lot size")
        if (status == "FILLED" and f != q or status == "REJECTED" and f or
                status == "OPEN" and f or status == "PARTIAL" and not 0 < f < q):
            raise Block("PORTFOLIO: contradictory order status")
        if status == "UNKNOWN": issues.append("ambiguous order " + oid)
        orders[oid] = order
    for fill in fills:
        key = item_key(fill, master)
        oid = fill["order_id"]
        at = instant(fill["at"])
        if at > instant(snapshot["as_of"]): raise Block("PORTFOLIO: fill after account snapshot")
        if integer(fill["quantity"]) % integer(master[key[:3]]["lot_size"]): raise Block("PORTFOLIO: fill units violate lot size")
        if oid not in orders or any(fill[k] != orders[oid][k] for k in (*FIELDS, "side")):
            issues.append("unmatched fill " + fill["fill_id"])
        by_order[oid].append(fill); by_key[key].append(fill)
    for oid, order in orders.items():
        if sum(integer(f["quantity"]) for f in by_order[oid]) != integer(order["filled_quantity"], minimum=0):
            issues.append("fill quantity mismatch " + oid)
    for row in array(snapshot["positions"]):
        key = item_key(row, master)
        q = quantity(row["quantity"])
        if key in positions: raise Block("PORTFOLIO: duplicate current position")
        if abs(q) % integer(master[key[:3]]["lot_size"]): raise Block("PORTFOLIO: current units violate lot size")
        if key[3] == "CNC" and q < 0: raise Block("PORTFOLIO: short delivery inventory unsupported")
        if q:
            positive(row["mark_price"])
            fresh(row.get("mark_as_of"), now, 60, "portfolio mark")
            if (instant(row["mark_as_of"]) > instant(snapshot["as_of"]) or instant(row["mark_as_of"]).date().isoformat() != day or
                    not isinstance(row.get("mark_source"), str) or not row["mark_source"].strip() or row.get("price_basis") != "per_unit_inr"):
                raise Block("PORTFOLIO: sourced per-unit current mark required")
        positions[key] = row
    opens = {tuple(r[k] for k in FIELDS): r for r in opening["positions"]}
    output = []
    total_day, total_realized, total_unrealized, total_fees = (Decimal(0) for _ in range(4))
    provisional = 0
    for key in sorted(opens.keys() | positions.keys() | by_key.keys()):
        initial = opens.get(key)
        start_q = quantity(initial["quantity"]) if initial else 0
        q = start_q
        avg = positive(initial["average_price"]) if initial else Decimal(0)
        previous = positive(initial["previous_close"]) if initial else Decimal(0)
        realized, cashflow, fees = Decimal(0), Decimal(0), Decimal(0)
        group = by_key[key]
        times = defaultdict(list)
        for f in group: times[instant(f["at"])].append(f)
        for same_time in times.values():
            if len(same_time) > 1:
                sequences = [integer(f["sequence"], minimum=0) for f in same_time if "sequence" in f]
                if len(sequences) != len(same_time) or len(set(sequences)) != len(sequences):
                    raise Block("PORTFOLIO: same-time fills require unique sourced execution sequence")
                if any(not isinstance(f.get("sequence_source"), str) or not f["sequence_source"].strip() for f in same_time):
                    raise Block("PORTFOLIO: execution sequence provenance required")
        for f in sorted(group, key=lambda f: (instant(f["at"]), integer(f.get("sequence", 0), minimum=0))):
            fq = integer(f["quantity"]) * (1 if f["side"] == "BUY" else -1)
            price = positive(f["price"])
            cashflow -= fq * price
            fees += sum((number(v) for v in f["fees"].values()), Decimal(0))
            provisional += f["fees_final"] is not True
            if q == 0 or q * fq > 0:
                avg = (abs(q) * avg + abs(fq) * price) / (abs(q) + abs(fq))
            else:
                realized += min(abs(q), abs(fq)) * (price - avg) * (1 if q > 0 else -1)
                if abs(fq) > abs(q): avg = price
                elif abs(fq) == abs(q): avg = Decimal(0)
            q += fq
            if key[3] == "CNC" and q < 0: raise Block("PORTFOLIO: delivery sell exceeds evidenced inventory")
        observed = quantity(positions[key]["quantity"]) if key in positions else 0
        if q != observed: issues.append("inventory quantity mismatch " + ":".join(key))
        mark = positive(positions[key]["mark_price"]) if observed else Decimal(0)
        unrealized = q * (mark - avg) if q and observed else Decimal(0)
        day_pnl = cashflow + q * mark - start_q * previous
        total_day += day_pnl; total_realized += realized; total_unrealized += unrealized; total_fees += fees
        output.append({**dict(zip(FIELDS, key)), "opening_quantity": start_q, "expected_quantity": q, "observed_quantity": observed,
                       "pooled_average_price": str(avg), "realized_gross_inr": money(realized), "unrealized_gross_inr": money(unrealized),
                       "daily_gross_inr": money(day_pnl), "recorded_today_fees_inr": money(fees)})
    inventory_ok = not issues
    # Do not publish plausible P&L totals when quantities/orders are inconsistent.
    if not inventory_ok:
        for r in output:
            for field in ("pooled_average_price", "realized_gross_inr", "unrealized_gross_inr", "daily_gross_inr"):
                r[field] = None
    totals = {"daily_gross_inr": money(total_day), "realized_gross_inr": money(total_realized),
              "unrealized_gross_inr": money(total_unrealized), "recorded_today_fees_inr": money(total_fees),
              "daily_net_inr": money(total_day - total_fees) if provisional == 0 else None} if inventory_ok else None
    return {"schema": "portfolio_report_v1", "broker": pair[0], "account_id": pair[1], "date": day, "as_of": snapshot["as_of"],
            "source": "portfolio_journal+" + snapshot["source"], "opening_source": opening["source"], "previous_session": opening["previous_session"],
            "status": "BLOCKED" if not inventory_ok else "PARTIAL" if provisional else "RECONCILED",
            "inventory_reconciled": inventory_ok, "fees_final": provisional == 0, "provisional_fill_count": provisional,
            "issues": issues, "positions": output, "totals": totals, "working_orders": sum(o["status"] in {"OPEN", "PARTIAL", "UNKNOWN"} for o in orders.values()),
            "opening_hash": fingerprint(opening), "snapshot_hash": fingerprint(snapshot), "fills_hash": fingerprint(fills),
            "accounting_basis": "Pooled average price; daily change uses previous-session marks. Excludes historical fees and tax/settlement computation.",
            "execution_eligible": False, "submitted": False}


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("command", choices=["opening", "correct-opening", "reconcile"])
    p.add_argument("--db", required=True); p.add_argument("--input", required=True); p.add_argument("--master", required=True)
    p.add_argument("--out"); p.add_argument("--now")
    a = p.parse_args(argv); journal = None
    try:
        if a.command != "reconcile" and a.out: raise Block("PORTFOLIO: opening evidence is saved in the journal")
        if a.out and Path(a.out).exists(): raise Block("PORTFOLIO: choose a new report path")
        now = instant(a.now); master = read(a.master)["instruments"]
        journal = Journal(a.db)
        operation = {"opening": save_opening, "correct-opening": correct_opening, "reconcile": reconcile_portfolio}[a.command]
        result = operation(journal, read(a.input), master, now)
        body = json.dumps(result, indent=2, allow_nan=False)
        if a.out:
            path = Path(a.out); path.parent.mkdir(parents=True, exist_ok=True)
            with path.open("x", encoding="utf-8") as stream: stream.write(body)
        else: print(body)
        return 2 if result.get("status") == "BLOCKED" else 0
    except (Block, ValueError, KeyError, TypeError, ArithmeticError, OSError, sqlite3.Error):
        print(json.dumps({"status": "BLOCKED", "reason": "Portfolio evidence or storage validation failed; private inputs suppressed", "submitted": False})); return 2
    finally:
        if journal: journal.close()


if __name__ == "__main__": sys.exit(main())
