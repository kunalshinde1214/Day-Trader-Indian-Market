"""Human-operated live broker tool. Never invoke mutations on behalf of a user.

Run snapshot/reconcile/status for read-only acceptance. entry/exit/stop/cancel/protection-step
require two exact interactive confirmations and repeat all checks before sending.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import sqlite3
import sys

from broker import adapter
from broker_data import normalize_quote, quote_key
from core import Block, instant, fingerprint
from execution import ExecutionStore, TerminalApproval, execute, prepare
from journal import Journal, reconcile
from live_broker import LiveBroker, wire_payload
from live_state import normalize, consistent, TERMINAL
from trader import read, write, runtime_context


def load_bundle(w):
    context = runtime_context(w, read(w / "context.json"))
    if (w / "replay.json").exists():
        context["replay"] = read(w / "replay.json")
    result = {"config": read(w / "config.json"), "instrument": read(w / "instrument.json"),
              "context": context, "charges": read(w / "charges.json")}
    if (w / "orders.json").exists():
        orders = read(w / "orders.json")
        if not isinstance(orders, list) or len(orders) != 1:
            raise Block("ORDER: select exactly one sized order in orders.json")
        result["order"] = orders[0]
    return result


def instruments(w, selected):
    # Include every instrument appearing in this intraday account's order book.
    path = w / "universe.json"
    return read(path)["instruments"] if path.exists() else [selected]


def snapshot(client, w, bundle):
    raw = client.account()
    result = normalize(raw, instruments(w, bundle["instrument"]), instant())
    c = bundle["config"]
    if result["broker"] != c["broker"] or result["account_id"] != c["account_id"]:
        raise Block("ACCOUNT: connected account differs from the selected configuration")
    return result


def reconcile_account(w, snap):
    """Bind the existing fill/fee journal to a complete, broker-observed flat account."""
    consistent(snap)
    if any(p["quantity"] for p in snap["positions"]) or any(o["status"] not in TERMINAL for o in snap["orders"]):
        raise Block("RECONCILE: live entry ledger refresh requires flat positions and terminal orders")
    canonical = {**snap, "complete": True, "opening_positions": [],
                 "cash_available": max(0, snap["available_inr"]), "margin_available": max(0, snap["available_inr"]),
                 "positions": [{**p, "position_value_inr": 0} for p in snap["positions"]]}
    rpath = w / "reservations.json"
    if rpath.exists():
        reservations = read(rpath)
    elif (w / "state/execution.sqlite").exists():
        executions = ExecutionStore(w / "state/execution.sqlite")
        try:
            executions.observe(snap, instant())
            reservations = executions.reservations(snap)
        finally: executions.close()
    else:
        reservations = {"broker": snap["broker"], "account_id": snap["account_id"], "date": snap["date"], "entries": []}
    journal = Journal(w / "state" / "journal.sqlite")
    try:
        profile = read(w / "live-profile.json") if (w / "live-profile.json").exists() else {}
        policy = profile.get("fee_policy")
        selected = read(w / "instrument.json") if policy is not None else None
        account = reconcile(journal, canonical, reservations, instant(), 30, fee_policy=policy,
                            instruments=instruments(w, selected) if policy is not None else None,
                            rate_card=read(w / "charges.json") if policy is not None else None)
    finally:
        journal.close()
    if account["reconciled"] is not True:
        return account
    account["execution_state_hash"] = snap["state_hash"]
    context = read(w / "context.json")
    context["account"] = account
    write(w / "context.json", context)
    return account


def protection(snapshot):
    """Only confirmed, waiting stop-limit coverage counts; an ACK never does."""
    result = []
    for p in snapshot["positions"]:
        if not p["quantity"]:
            continue
        side = "SELL" if p["quantity"] > 0 else "BUY"
        working = [r for r in snapshot["orders"]
                   if all(r[k] == p[k] for k in ("exchange", "symbol", "token", "product"))
                   and r["status"] not in TERMINAL]
        exits = [r for r in working if r["side"] == side]
        rows = [r for r in exits if r["status"] in {"OPEN", "PARTIAL"} and r["order_type"] == "SL"]
        covered = sum(r["quantity"] - r["filled_quantity"] for r in rows if r["raw_status"] in {"TRIGGER PENDING", "PENDING"})
        # Broker PENDING can mean a working limit, so only explicit trigger-pending
        # is guaranteed waiting protection. Others require broker UI inspection.
        verified = sum(r["quantity"] - r["filled_quantity"] for r in rows if r["raw_status"] == "TRIGGER PENDING")
        exit_units = sum(r["quantity"] - r["filled_quantity"] for r in exits)
        entry_units = sum(r["quantity"] - r["filled_quantity"] for r in working if r["side"] != side)
        excess = max(0, exit_units - abs(p["quantity"]))
        unknown = any(r["status"] not in {"OPEN", "PARTIAL"} for r in working)
        state = "UNPROTECTED_OR_UNVERIFIED"
        if unknown: state = "ORDER_STATE_UNVERIFIED"
        elif excess: state = "OVER_EXIT_RISK"
        elif entry_units: state = "POSITION_CAN_INCREASE"
        elif verified == abs(p["quantity"]): state = "STOP_WAITING"
        result.append({**p, "stop_quantity_reported": covered, "waiting_stop_quantity_verified": verified,
                       "working_exit_quantity": exit_units, "excess_exit_quantity": excess,
                       "working_entry_quantity": entry_units,
                       "uncovered_quantity": max(0, abs(p["quantity"]) - verified),
                       "state": state})
    return result


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("command", choices=["snapshot", "reconcile-account", "status", "preview", "entry", "exit", "stop", "cancel", "bind", "panic-preview", "protection-preview", "protection-step"])
    p.add_argument("--workdir", required=True)
    p.add_argument("--kind", choices=["entry", "exit", "stop", "cancel"], default="entry")
    p.add_argument("--quantity", type=int); p.add_argument("--price"); p.add_argument("--trigger")
    p.add_argument("--order-id"); p.add_argument("--intent-id")
    a = p.parse_args(argv)
    store = None
    try:
        w = Path(a.workdir)
        bundle = load_bundle(w)
        c = bundle["config"]
        client = adapter(c["broker"])
        store = ExecutionStore(w / "state" / "execution.sqlite")
        approval = TerminalApproval()
        if a.command in {'protection-preview', 'protection-step'}:
            from protection import preview as protection_preview
            def protection_observation():
                b = load_bundle(w)
                if any(b['config'][k] != c[k] for k in ('broker', 'account_id')):
                    raise Block('ACCOUNT: protection configuration changed')
                i = b['instrument']
                b['context']['quote'] = normalize_quote(client.quotes([quote_key(i)]), i, instant(), 30)
                snap = snapshot(client, w, b)
                records = store.observe(snap, instant())
                decision = protection_preview(w / 'state/protection.sqlite', b, snap,
                                              read(w / 'live-profile.json'), read(w / 'protection-policy.json'), instant(), records)
                return decision, snap
            result, _ = protection_observation()
            if a.command == 'protection-step' and result['review'] is not None:
                def refresh_protection():
                    decision, snap = protection_observation()
                    if decision['review'] is None:
                        raise Block('REVIEW_CHANGED: protection no longer proposes an action')
                    return decision['review'], snap
                result = execute(store, LiveBroker(c['broker'], client), approval, refresh_protection)
        elif a.command in {"snapshot", "reconcile-account", "status", "bind", "panic-preview"}:
            snap = snapshot(client, w, bundle)
            write(w / "state" / "live-snapshot.json", snap)
            if a.command == "reconcile-account":
                result = reconcile_account(w, snap)
            elif a.command == "bind":
                result = store.bind(a.intent_id, a.order_id, snap, approval)
            else:
                records = store.observe(snap, instant())
                result = {"snapshot": snap, "intents": records, "protection": protection(snap), "submitted": False}
                if a.command == "panic-preview":
                    write(w / "state" / "halt.json", {"halted": True, "as_of": instant().isoformat()})
                    result["cancel_first"] = [r["order_id"] for r in snap["orders"] if r["status"] not in TERMINAL]
                    result["remaining_positions"] = [r for r in snap["positions"] if r["quantity"]]
                    result["next_step"] = "Approve each cancellation; confirm terminal states; refresh positions; approve each reducing limit exit. No blanket flatten."
        else:
            kind = a.kind if a.command == "preview" else a.command
            def refresh():
                b = load_bundle(w)
                profile = read(w / "live-profile.json")
                if b["config"]["broker"] != c["broker"] or b["config"]["account_id"] != c["account_id"]:
                    raise Block("ACCOUNT: configuration changed; start a new review")
                i = b["instrument"]
                if kind in {"entry", "stop"}:
                    q = client.quotes([quote_key(i)])
                    b["context"]["quote"] = normalize_quote(q, i, instant(), 30)
                    # The core prefers a keyed quote if present; do not leave an older override.
                    b["context"].get("quotes", {}).pop(i["exchange"] + ":" + i["symbol"], None)
                if kind == "entry":
                    from core import payload
                    b["live_margin"] = client.margin(payload(b["order"]))
                    if i["kind"] != "equity":
                        b["context"]["margin"] = b["live_margin"]
                        b["context"].get("margins", {}).pop(b["live_margin"]["payload_hash"], None)
                snap = snapshot(client, w, b)
                review = prepare(kind, b, snap, profile, instant(), quantity=a.quantity, price=a.price, trigger=a.trigger, order_id=a.order_id)
                return review, snap
            if a.command == "preview":
                result, _ = refresh()
                result["submitted"] = False
            else:
                result = execute(store, LiveBroker(c["broker"], client), approval, refresh)
        print(json.dumps(result, indent=2, allow_nan=False))
        return 2 if isinstance(result, dict) and result.get("reconciled") is False else 0
    except Block as exc:
        print(json.dumps({"status": "BLOCK_OR_UNKNOWN", "reason": str(exc),
                          "next_step": "Inspect the durable intent log and broker order book; never retry an unresolved action."}))
        return 2
    except (ValueError, KeyError, TypeError, IndexError, OSError, sqlite3.Error, KeyboardInterrupt):
        print(json.dumps({"status": "BLOCK_OR_UNKNOWN", "reason": "Action did not finish cleanly. Inspect the durable intent log and broker order book; do not retry an unresolved action. Private response content suppressed."}))
        return 2
    finally:
        if store:
            store.close()


if __name__ == "__main__":
    sys.exit(main())
