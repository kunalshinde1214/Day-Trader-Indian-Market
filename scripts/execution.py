"""Durable, human-approved execution state machine; no network on import."""
from __future__ import annotations

import copy
import json
from pathlib import Path
import sqlite3
import sys
import uuid

from core import (Block, check_order, fingerprint, fresh, instant, integer, number,
                  on_tick, positive, reducing_exit, validate_instrument)
from live_broker import Ambiguous, identifier, wire_payload
from live_state import TERMINAL, consistent, state_hash

UNRESOLVED = {"SUBMITTING", "UNKNOWN", "ACKNOWLEDGED"}


def review_data(value):
    """Refresh timestamps may advance; economic inputs and evidence identities may not."""
    if isinstance(value, dict):
        return {k: review_data(v) for k, v in value.items() if k not in {"as_of", "fetched_at"}}
    if isinstance(value, list):
        return [review_data(v) for v in value]
    return value


def profile_check(profile, broker, account, now, entry=False):
    if profile.get("broker") != broker or profile.get("account_id") != account:
        raise Block("LIVE_PROFILE: exact broker/account required")
    if profile.get("enabled") is not True:
        raise Block("LIVE_DISABLED: live profile is disabled")
    if profile.get("approval_mode") != "human_terminal_two_stage":
        raise Block("APPROVAL: interactive human broker tool required")
    if entry:
        acceptance = profile.get("acceptance", {})
        if (acceptance.get("broker") != broker or acceptance.get("account_id") != account or
                acceptance.get("status") != "PASS" or not acceptance.get("source")):
            raise Block("ACCEPTANCE: account-specific operating acceptance required")
        fresh(acceptance.get("as_of"), now, 86400, "operating acceptance")
        if profile.get("new_entries") is not True:
            raise Block("LIVE_HALT: new entries disabled")


def prepare(kind, bundle, snapshot, profile, now, *, quantity=None, price=None, trigger=None, order_id=None):
    """Recomputed before AND after approval. Inputs cannot be replaced by approved:true."""
    i, c, ctx, card = [bundle[k] for k in ("instrument", "config", "context", "charges")]
    broker, account = c["broker"], c["account_id"]
    profile_check(profile, broker, account, now, entry=kind == "entry")
    validate_instrument(i, now)
    if i["broker"] != broker or snapshot["broker"] != broker or snapshot["account_id"] != account:
        raise Block("ACCOUNT: execution identity mismatch")
    fresh(snapshot["as_of"], now, 30, "execution snapshot")
    if snapshot["date"] != now.date().isoformat() or snapshot["state_hash"] != state_hash(snapshot):
        raise Block("ACCOUNT: invalid snapshot date/hash")
    consistent(snapshot)
    action = {"kind": kind, "broker": broker, "account_id": account, "instrument": copy.deepcopy(i)}
    risk = None
    if kind == "cancel":
        matches = [r for r in snapshot["orders"] if r["order_id"] == identifier(order_id)]
        if len(matches) != 1 or matches[0]["status"] not in {"OPEN", "PARTIAL"}:
            raise Block("CANCEL: exact working order required")
        row = matches[0]
        if any(row[k] != i[k] for k in ("exchange", "symbol", "token")):
            raise Block("CANCEL: instrument does not match order")
        action.update(order_id=order_id, variety=row["variety"], expected_order=copy.deepcopy(row))
    elif kind == "entry":
        evidence = [card.get("source", ""), ctx["account"].get("source", ""), ctx["session"].get("source", ""),
                    ctx["event"].get("source", ""), ctx["restrictions"].get("source", "")]
        if account == "PAPER-DEMO" or any(any(word in s.lower() for word in ("synthetic", "fictional", "demo")) for s in evidence):
            raise Block("DEMO_INPUT: synthetic evidence can never authorize a real entry")
        if any(r["status"] not in TERMINAL for r in snapshot["orders"]) or any(r["quantity"] for r in snapshot["positions"]):
            raise Block("ACCOUNT_BUSY: close/reconcile existing exposure before a new entry")
        a = ctx["account"]
        if a.get("execution_state_hash") != snapshot["state_hash"] or a["orders_today"] != len(snapshot["orders"]):
            raise Block("LEDGER: reconcile the journal to this exact broker state first")
        if max(number(a["cash_available"]), number(a["margin_available"])) > number(max(0, snapshot["available_inr"])):
            raise Block("FUNDS: risk ledger exceeds current broker availability")
        order = bundle["order"]
        risk = check_order(order, i, c, ctx, card, now)
        p = order["plan"]
        action.update(side=p["side"], quantity=integer(order["quantity"]), price=float(positive(p["entry"])),
                      planned_stop=p["stop"], planned_target=p["target"], flat_by=p["flat_by"],
                      order_hash=fingerprint(order), planned_loss_inr=risk["metrics"]["loss_at_stop_inr"])
        if action["quantity"] * number(action["price"]) > positive(profile["max_entry_notional_inr"]):
            raise Block("LIVE_LIMIT: entry exceeds the live profile notional cap")
        if risk["metrics"]["loss_at_stop_inr"] > number(profile["max_entry_loss_inr"]):
            raise Block("LIVE_LIMIT: entry exceeds the live profile loss cap")
        # Require a fresh, exact broker margin estimate even for cash-equity live orders.
        margin = bundle["live_margin"]
        fresh(margin.get("as_of"), now, 30, "live margin")
        if (margin.get("source") != broker or margin.get("account_id") != account or
                margin.get("payload_hash") != fingerprint(wire_payload(action))):
            raise Block("MARGIN: exact live payload/account estimate required")
        if positive(margin["required_inr"]) + number(risk["metrics"]["charges_at_stop"]["total"]) > number(max(0, snapshot["available_inr"])):
            raise Block("MARGIN: insufficient broker availability")
    elif kind in {"exit", "stop"}:
        positions = [p for p in snapshot["positions"] if all(p[k] == i[k] for k in ("exchange", "symbol", "token")) and p["quantity"]]
        if len(positions) != 1:
            raise Block("EXIT: exact nonzero position required")
        position = positions[0]
        side = "SELL" if position["quantity"] > 0 else "BUY"
        qty = integer(quantity)
        reducing_exit(position, side, qty)
        # All working orders in this instrument must finish/cancel before a replacement.
        # This prevents stop + independent target orders selling the same units twice.
        working = [r for r in snapshot["orders"] if r["exchange"] == i["exchange"] and r["token"] == i["token"] and r["status"] not in TERMINAL]
        if working:
            raise Block("WORKING_ORDER: cancel and confirm existing instrument orders before exit/protection")
        action.update(side=side, quantity=qty, price=float(positive(price)))
        if kind == "stop":
            action["trigger_price"] = float(positive(trigger))
            quote = ctx["quote"]
            fresh(quote["as_of"], now, 30, "stop quote")
            if quote.get("source") != broker or quote["symbol"] != i["symbol"] or quote["exchange"] != i["exchange"]:
                raise Block("QUOTE: exact broker quote required")
            current = positive(quote["price"])
            if not (positive(trigger) < current if side == "SELL" else positive(trigger) > current):
                raise Block("STOP: trigger already reached; review an immediate reducing limit exit")
    else:
        raise Block("ACTION: unsupported action")
    if kind != "cancel":
        if action["quantity"] % integer(i["lot_size"]) or action["quantity"] > integer(i["freeze_quantity"]):
            raise Block("LOT_FREEZE: same-day lot and freeze limits required; no slicing")
        if not on_tick(action["price"], i["tick_size"]) or kind == "stop" and not on_tick(action["trigger_price"], i["tick_size"]):
            raise Block("TICK: exact tick-aligned prices required")
    review = {"action": action, "payload": wire_payload(action), "state_hash": snapshot["state_hash"],
              "baseline_order_ids": [r["order_id"] for r in snapshot["orders"]],
              "bundle_hash": fingerprint(review_data(bundle)), "profile_hash": fingerprint(profile)}
    return {**review, "review_hash": fingerprint(review), "risk": risk, "as_of": now.isoformat(),
            "warning": "Real order. Acknowledgement is not a fill. A stop-limit may not execute. No automatic protective order is installed."}


class TerminalApproval:
    """Human-operated broker tool. Not a broker-hosted or cryptographic approval service."""
    def approve(self, review):
        if not sys.stdin.isatty() or not sys.stdout.isatty():
            raise Block("APPROVAL: run in a human-operated interactive terminal; piped approval refused")
        print(json.dumps(review, indent=2, allow_nan=False))
        h = review["review_hash"]
        a = review["action"]
        first = "REVIEW " + h
        second = "SEND " + a["broker"] + ":" + a["account_id"] + " " + h
        try:
            if input("Exact-order authorization: type " + first + "\n> ").strip() != first:
                raise Block("APPROVAL: first authorization declined")
            if input("Broker-tool confirmation: type " + second + "\n> ").strip() != second:
                raise Block("APPROVAL: broker-tool confirmation declined")
        except EOFError:
            raise Block("APPROVAL: terminal closed") from None
        return h


class ExecutionStore:
    def __init__(self, path):
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path, timeout=5)
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA synchronous=FULL")
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS intents (
                id TEXT PRIMARY KEY, broker TEXT NOT NULL, account TEXT NOT NULL,
                review_hash TEXT NOT NULL UNIQUE, body TEXT NOT NULL, state TEXT NOT NULL,
                order_id TEXT, filled INTEGER NOT NULL DEFAULT 0, created_at TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS execution_events (
                sequence INTEGER PRIMARY KEY, intent_id TEXT NOT NULL, at TEXT NOT NULL,
                state TEXT NOT NULL, body TEXT NOT NULL);
        """)

    def close(self):
        self.db.close()

    def records(self, broker, account):
        return [{"id": r[0], "review": json.loads(r[1]), "state": r[2], "order_id": r[3], "filled_quantity": r[4], "created_at": r[5]}
                for r in self.db.execute("SELECT id,body,state,order_id,filled,created_at FROM intents WHERE broker=? AND account=? ORDER BY rowid", (broker, account))]

    def revision(self, broker, account):
        return self.db.execute("SELECT count(*) FROM intents WHERE broker=? AND account=?", (broker, account)).fetchone()[0]

    def reservations(self, snapshot):
        """Reconstruct risk groups from observed, serialized local execution intents.

        Manual/foreign orders are deliberately left unmapped for journal review.
        """
        groups, latest, claimed = [], {}, set()
        for r in self.records(snapshot["broker"], snapshot["account_id"]):
            if instant(r["created_at"]).date().isoformat() != snapshot["date"]: continue
            if r["state"] in UNRESOLVED: raise Block("RESERVATIONS: reconcile unresolved execution first")
            a = r["review"]["action"]
            if a["kind"] == "cancel": continue
            oid = r["order_id"]
            if not oid or oid in claimed: raise Block("RESERVATIONS: missing or duplicate order identity")
            claimed.add(oid)
            key = (a["instrument"]["exchange"], a["instrument"]["token"])
            if a["kind"] == "entry":
                group = {"entry_order_id": oid, "exit_order_ids": [], "planned_loss_inr": a["planned_loss_inr"]}
                latest[key] = group; groups.append(group)
            elif a["kind"] in {"exit", "stop"}:
                if key not in latest: raise Block("RESERVATIONS: reducing order has no locally tracked entry; map manually")
                latest[key]["exit_order_ids"].append(oid)
        return {"broker": snapshot["broker"], "account_id": snapshot["account_id"], "date": snapshot["date"], "entries": groups}

    def _event(self, intent, state, now, body):
        self.db.execute("INSERT INTO execution_events(intent_id,at,state,body) VALUES(?,?,?,?)", (intent, now.isoformat(), state, json.dumps(body, allow_nan=False)))

    def start(self, review, revision, now):
        a = review["action"]
        self.db.execute("BEGIN IMMEDIATE")
        try:
            if self.revision(a["broker"], a["account_id"]) != revision:
                raise Block("CONCURRENT_ACTION: another action started; review again")
            if any(r["state"] in UNRESOLVED for r in self.records(a["broker"], a["account_id"])):
                raise Block("UNRESOLVED: reconcile previous submission before another action")
            key = uuid.uuid4().hex
            self.db.execute("INSERT INTO intents(id,broker,account,review_hash,body,state,created_at,order_id) VALUES(?,?,?,?,?,'SUBMITTING',?,?)",
                            (key, a["broker"], a["account_id"], review["review_hash"], json.dumps(review, allow_nan=False), now.isoformat(),
                             a["order_id"] if a["kind"] == "cancel" else None))
            self._event(key, "SUBMITTING", now, {"approval": "two-stage human confirmation", "review_hash": review["review_hash"]})
            self.db.commit()
            return key
        except BaseException:
            self.db.rollback()
            raise

    def outcome(self, key, state, now, order_id=None):
        with self.db:
            self.db.execute("UPDATE intents SET state=?,order_id=COALESCE(?,order_id) WHERE id=?", (state, order_id, key))
            self._event(key, state, now, {"order_id": order_id})

    @staticmethod
    def matches(action, row):
        if any(action["instrument"][k] != row[k] for k in ("exchange", "symbol", "token")):
            return False
        if action["kind"] == "cancel":
            return action["order_id"] == row["order_id"]
        return (action["side"] == row["side"] and action["quantity"] == row["quantity"] and
                number(action["price"]) == number(row["price"]) and
                number(action.get("trigger_price", 0)) == number(row["trigger_price"]) and
                row["order_type"] == ("SL" if action["kind"] == "stop" else "LIMIT"))

    def observe(self, snapshot, now):
        fresh(snapshot["as_of"], now, 30, "recovery snapshot")
        by_id = {r["order_id"]: r for r in snapshot["orders"]}
        with self.db:
            for record in self.records(snapshot["broker"], snapshot["account_id"]):
                # Day order books disappear overnight; absence never proves rejection.
                if instant(record["created_at"]).date().isoformat() != snapshot["date"]:
                    if record["state"] not in TERMINAL:
                        self.db.execute("UPDATE intents SET state='UNKNOWN' WHERE id=?", (record["id"],))
                    continue
                row = by_id.get(record["order_id"])
                if row is None:
                    if record["state"] not in TERMINAL and record["state"] != "UNKNOWN":
                        self.db.execute("UPDATE intents SET state='UNKNOWN' WHERE id=?", (record["id"],))
                        self._event(record["id"], "UNKNOWN", now, {"reason": "order absent; absence is not rejection"})
                    continue
                if not self.matches(record["review"]["action"], row):
                    raise Block("RECOVERY: broker order differs from approved action")
                if row["filled_quantity"] < record["filled_quantity"] or record["state"] in TERMINAL and row["status"] != record["state"]:
                    raise Block("RECOVERY: regressing broker state")
                state = row["status"]
                if record["review"]["action"]["kind"] == "cancel" and state not in TERMINAL:
                    state = "UNKNOWN"  # cancel accepted but not terminal: retain the lock
                if state != record["state"] or row["filled_quantity"] != record["filled_quantity"]:
                    self.db.execute("UPDATE intents SET state=?,filled=? WHERE id=?", (state, row["filled_quantity"], record["id"]))
                    self._event(record["id"], state, now, {"filled_quantity": row["filled_quantity"]})
        return self.records(snapshot["broker"], snapshot["account_id"])

    def bind(self, intent_id, order_id, snapshot, approval, clock=instant):
        """Explicit human attribution of a timed-out request to a broker order.

        Never discover identity by fuzzy price matching and never clear an absent order.
        """
        records = self.records(snapshot["broker"], snapshot["account_id"])
        found = [r for r in records if r["id"] == intent_id and r["state"] in UNRESOLVED and r["order_id"] is None]
        rows = [r for r in snapshot["orders"] if r["order_id"] == identifier(order_id)]
        if len(found) != 1 or len(rows) != 1:
            raise Block("BIND: unresolved intent and explicit broker order required")
        record, row = found[0], rows[0]
        if (instant(record["created_at"]).date().isoformat() != snapshot["date"] or
                not self.matches(record["review"]["action"], row) or
                order_id in record["review"]["baseline_order_ids"] or any(r["order_id"] == order_id for r in records)):
            raise Block("BIND: date, identity, payload or ownership mismatch")
        review = {"action": {"kind": "bind", "broker": snapshot["broker"], "account_id": snapshot["account_id"],
                              "intent_id": intent_id, "order_id": order_id}, "observed_order": row,
                  "original_review": record["review"]["review_hash"], "as_of": clock().isoformat()}
        review["review_hash"] = fingerprint(review)
        if approval.approve(copy.deepcopy(review)) != review["review_hash"]:
            raise Block("BIND: approval declined")
        fresh(snapshot["as_of"], clock(), 30, "recovery evidence")
        with self.db:
            changed = self.db.execute("UPDATE intents SET order_id=?,state='ACKNOWLEDGED' WHERE id=? AND order_id IS NULL AND state IN ('UNKNOWN','SUBMITTING','ACKNOWLEDGED')", (order_id, intent_id)).rowcount
            if changed != 1:
                raise Block("BIND: concurrent recovery; refresh")
            self._event(intent_id, "BOUND_BY_HUMAN", clock(), {"order_id": order_id, "review_hash": review["review_hash"]})
        return self.observe(snapshot, clock())


def execute(store, transport, approval, refresh, clock=instant):
    """refresh() must rerun all reads, policy/risk checks and exact payload building."""
    review, snapshot = refresh()
    a = review["action"]
    store.observe(snapshot, clock())
    if any(r["state"] in UNRESOLVED for r in store.records(a["broker"], a["account_id"])):
        raise Block("UNRESOLVED: previous request requires reconciliation")
    revision = store.revision(a["broker"], a["account_id"])
    approved = approval.approve(copy.deepcopy(review))
    fresh(review["as_of"], clock(), 90, "human approval")
    current, snapshot = refresh()
    if approved != review["review_hash"] or current["review_hash"] != approved:
        raise Block("REVIEW_CHANGED: inputs/account changed; fresh authorization required")
    # All potentially failing local request construction precedes durable submission.
    request = transport.build(current["action"])
    fresh(current["as_of"], clock(), 5, "pre-submit validation")
    key = store.start(current, revision, clock())
    try:
        ack = transport.send(request, current["action"])
        store.outcome(key, "ACKNOWLEDGED", clock(), ack["order_id"])
    except BaseException:
        # Includes Ctrl-C after write: a process restart must not permit a duplicate.
        store.outcome(key, "UNKNOWN", clock())
        raise Ambiguous("EXECUTION_UNKNOWN: inspect broker orders and reconcile this intent; never retry") from None
    return {"intent_id": key, "state": "ACKNOWLEDGED", "order_id": ack["order_id"],
            "submitted": True, "filled": False, "protected": False}
