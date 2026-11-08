"""Offline fault-injection tests: no broker connection or real credentials."""
import copy
from datetime import timedelta
import io
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch, Mock
import urllib.error

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
from core import Block, fingerprint, instant, orb, plan_from_hit, size, payload
from demo import inputs
from replay import replay
from execution import ExecutionStore, TerminalApproval, execute, prepare
from live_broker import LiveBroker, Ambiguous, wire_payload, NoRedirect
from live_state import normalize, state_hash, consistent
from live import load_bundle, reconcile_account, protection, main
from trader import write


NOW = instant("2026-10-30T09:35:00+05:30")


def sample(broker="kite", filled=0, status=None):
    c = json.loads((ROOT / "assets/config.example.json").read_text())
    c.update(broker=broker, account_id="123456")
    _, i, _, _, _, _ = inputs(c)
    i["freeze_quantity"] = 10000
    common = {"broker": broker, "source": broker, "account_id": "123456", "as_of": NOW.isoformat()}
    if broker in {"kite", "upstox"}:
        row = {"order_id": "O1", "exchange": "NSE", "instrument_token": i["token"], "tradingsymbol": i["symbol"], "trading_symbol": i["symbol"],
               "product": "MIS" if broker == "kite" else "I", "transaction_type": "BUY", "quantity": 10,
               "filled_quantity": filled, "status": status or "OPEN", "price": 102, "trigger_price": 0, "order_type": "LIMIT",
               "variety": "regular" if broker == "kite" else "SIMPLE"}
        position = {**row, "quantity": filled, "overnight_quantity": 0}
        funds = {"equity": {"available": {"live_balance": 100000}}} if broker == "kite" else {"equity": {"available_margin": 100000}}
        positions = {"net": [position]} if broker == "kite" else [position]
    elif broker == "dhan":
        row = {"orderId": "O1", "dhanClientId": "123456", "securityId": i["token"], "exchangeSegment": "NSE_EQ", "tradingSymbol": i["symbol"],
               "productType": "INTRADAY", "transactionType": "BUY", "quantity": 10, "filledQty": filled,
               "orderStatus": status or "PENDING", "price": 102, "triggerPrice": 0, "orderType": "LIMIT"}
        positions = [{**row, "netQty": filled, "carryForwardBuyQty": 0, "carryForwardSellQty": 0}]
        funds = {"availabelBalance": 100000}
    elif broker == "angel":
        row = {"orderid": "O1", "symboltoken": i["token"], "tradingsymbol": i["symbol"], "exchange": "NSE", "producttype": "INTRADAY",
               "transactiontype": "BUY", "quantity": "10", "filledshares": str(filled), "orderstatus": status or "open",
               "price": "102", "triggerprice": "0", "ordertype": "LIMIT", "variety": "NORMAL"}
        positions = [{**row, "netqty": str(filled), "cfbuyqty": "0", "cfsellqty": "0"}]
        funds = {"availablecash": "100000"}
    else:
        row = {"id": "O1", "symbol": "NSE:" + i["symbol"], "fyToken": i["token"], "clientId": "123456", "productType": "INTRADAY",
               "side": 1, "qty": 10, "filledQty": filled, "status": status or 6, "limitPrice": 102, "stopPrice": 0, "type": 1}
        positions = [{**row, "netQty": filled}]
        funds = [{"title": "Available Balance", "equityAmount": 100000}]
    return i, {**common, "orders": [row], "positions": positions, "margins": funds}


class NormalizationTests(unittest.TestCase):
    def test_all_five_partial_positions_and_funds(self):
        for broker in ("kite", "upstox", "dhan", "angel", "fyers"):
            i, raw = sample(broker, filled=4)
            snap = normalize(raw, [i], NOW)
            self.assertEqual(snap["orders"][0]["status"], "PARTIAL")
            self.assertEqual(snap["positions"][0]["quantity"], 4)
            self.assertEqual(snap["available_inr"], 100000)
            self.assertTrue(consistent(snap))

    def test_unknown_status_does_not_become_open(self):
        for b, status in (("kite", "CANCEL PENDING"), ("upstox", "validation pending"), ("dhan", "TRANSIT"), ("angel", "unknown"), ("fyers", 4)):
            i, raw = sample(b, status=status)
            snap = normalize(raw, [i], NOW)
            self.assertEqual(snap["orders"][0]["status"], "UNKNOWN")
            with self.assertRaises(Block): consistent(snap)

    def test_terminal_partial_cancel_keeps_exposure(self):
        for b, status in (("kite", "CANCELLED"), ("upstox", "cancelled"), ("dhan", "CANCELLED"), ("angel", "cancelled"), ("fyers", 1)):
            i, raw = sample(b, filled=4, status=status)
            snap = normalize(raw, [i], NOW)
            self.assertEqual(snap["orders"][0]["filled_quantity"], 4)
            self.assertEqual(snap["orders"][0]["status"], "CANCELLED")
            consistent(snap)

    def test_filled_must_have_full_quantity(self):
        for b, status in (("kite", "COMPLETE"), ("upstox", "complete"), ("dhan", "TRADED"), ("angel", "complete"), ("fyers", 2)):
            i, raw = sample(b, filled=4, status=status)
            with self.assertRaises(Block): normalize(raw, [i], NOW)

    def test_duplicate_ids_and_positions(self):
        i, raw = sample()
        raw["orders"] *= 2
        with self.assertRaises(Block): normalize(raw, [i], NOW)
        i, raw = sample()
        raw["positions"]["net"] *= 2
        with self.assertRaises(Block): normalize(raw, [i], NOW)

    def test_cross_account_and_master_mismatch(self):
        for b in ("kite", "upstox", "dhan", "angel", "fyers"):
            i, raw = sample(b)
            raw["orders"][0]["clientcode"] = "wrong"
            with self.assertRaises(Block): normalize(raw, [i], NOW)
            i, raw = sample(b)
            i["token"] = "888888888" if b != "fyers" else "10100000888888"
            with self.assertRaises(Block): normalize(raw, [i], NOW)

    def test_unknown_and_carry_products_not_silently_filtered(self):
        i, raw = sample()
        raw["orders"][0]["product"] = "CNC"
        with self.assertRaises(Block): normalize(raw, [i], NOW)
        i, raw = sample()
        raw["positions"]["net"][0]["overnight_quantity"] = 1
        with self.assertRaises(Block): normalize(raw, [i], NOW)

    def test_null_and_stale_reads_block(self):
        i, raw = sample()
        raw["orders"] = None
        with self.assertRaises(Block): normalize(raw, [i], NOW)
        i, raw = sample()
        with self.assertRaises(Block): normalize(raw, [i], NOW + timedelta(seconds=31))

    def test_position_order_race(self):
        i, raw = sample(filled=4)
        raw["positions"]["net"][0]["quantity"] = 3
        with self.assertRaisesRegex(Block, "RACE"): consistent(normalize(raw, [i], NOW))

    def test_negative_available_funds_are_preserved_for_exits(self):
        i, raw = sample()
        raw["margins"]["equity"]["available"]["live_balance"] = -1
        self.assertEqual(normalize(raw, [i], NOW)["available_inr"], -1)


class TransportTests(unittest.TestCase):
    def setUp(self):
        # These are dummy values. No tool ever reads the user's credential values.
        self.env = patch.dict("os.environ", {"KITE_API_KEY": "fake", "KITE_ACCESS_TOKEN": "fake", "UPSTOX_ACCESS_TOKEN": "fake"}, clear=True)
        self.env.start()

    def tearDown(self): self.env.stop()

    def transport(self, b):
        ro = Mock()
        if b == "dhan": ro.credentials.return_value = ("123456", "fake")
        if b == "fyers": ro.credentials.return_value = {"FYERS_ACCOUNT_ID": "123456", "FYERS_APP_ID": "fake", "FYERS_ACCESS_TOKEN": "fake"}
        if b == "angel": ro.credentials.return_value = {"ANGEL_CLIENT_CODE": "123456", "ANGEL_ACCESS_TOKEN": "fake", "ANGEL_API_KEY": "fake", "ANGEL_LOCAL_IP": "127.0.0.1", "ANGEL_PUBLIC_IP": "192.0.2.1", "ANGEL_MAC_ADDRESS": "00:00:00:00:00:01"}
        return LiveBroker(b, ro)

    def action(self, b, kind="entry"):
        i, _ = sample(b)
        return {"broker": b, "account_id": "123456", "instrument": i, "kind": kind, "quantity": 10, "side": "BUY", "price": 102,
                "trigger_price": 101, "order_id": "O1", "variety": "NORMAL"}

    def test_place_and_cancel_methods_hosts_and_bodies_all_five(self):
        hosts = {"kite": "api.kite.trade", "upstox": "api-hft.upstox.com", "dhan": "api.dhan.co", "angel": "apiconnect.angelone.in", "fyers": "api-t1.fyers.in"}
        for b, host in hosts.items():
            t = self.transport(b)
            req = t.build(self.action(b))
            self.assertEqual(req.host, host); self.assertEqual(req.method, "POST")
            self.assertIn(b"quantity=10" if b == "kite" else b'"qty":10' if b == "fyers" else b'"quantity":10', req.data)
            req = t.build(self.action(b, "cancel"))
            self.assertEqual(req.method, "POST" if b == "angel" else "DELETE")
            self.assertIn("O1", req.full_url + (req.data or b"").decode())

    def test_stop_limit_all_five_and_no_market_conversion(self):
        for b in ("kite", "upstox", "dhan", "angel", "fyers"):
            a = self.action(b, "stop")
            result = wire_payload(a)
            expected = {"kite": ("order_type", "SL"), "upstox": ("order_type", "SL"), "dhan": ("orderType", "STOP_LOSS"), "angel": ("ordertype", "STOPLOSS_LIMIT"), "fyers": ("type", 4)}[b]
            self.assertEqual(result[expected[0]], expected[1])
            a["price"] = 100
            with self.assertRaises(Block): wire_payload(a)

    def test_acknowledgements_are_not_fills(self):
        results = {"kite": {"status": "success", "data": {"order_id": "O1"}}, "upstox": {"status": "success", "data": {"order_ids": ["O1"]}},
                   "dhan": {"orderId": "O1", "orderStatus": "TRANSIT"}, "angel": {"status": True, "data": {"orderid": "O1"}},
                   "fyers": {"s": "ok", "code": 1101, "id": "O1"}}
        for b, response in results.items():
            self.assertFalse(self.transport(b).acknowledgement(response, self.action(b))["filled"])

    def test_upstox_split_and_fyers_unacknowledged_are_ambiguous(self):
        with self.assertRaises(ValueError):
            self.transport("upstox").acknowledgement({"status": "success", "data": {"order_ids": ["O1", "O2"]}}, self.action("upstox"))
        with self.assertRaises(ValueError):
            self.transport("fyers").acknowledgement({"s": "ok", "code": 201, "id": "O1"}, self.action("fyers"))

    def test_no_redirect(self):
        self.assertIsNone(NoRedirect().redirect_request(None, None, 302, "", {}, "https://other.example"))

    def test_network_timeout_and_http_error_attempt_once_and_redact(self):
        t = self.transport("kite"); a = self.action("kite"); req = t.build(a)
        for error in (TimeoutError("secret"), urllib.error.HTTPError(req.full_url, 503, "secret", {}, None)):
            opener = Mock(); opener.open.side_effect = error
            with patch("urllib.request.build_opener", return_value=opener):
                with self.assertRaises(Ambiguous) as caught: t.send(req, a)
                self.assertNotIn("secret", str(caught.exception))
                self.assertEqual(opener.open.call_count, 1)

    def test_malformed_or_oversize_response_never_retries(self):
        t = self.transport("kite"); a = self.action("kite")
        for data in (b"not json secret", b'{"status":"error","message":"secret"}', b"x" * (1024 * 1024 + 1)):
            response = Mock(); response.read.return_value = data
            opener = Mock(); opener.open.return_value.__enter__ = Mock(return_value=response); opener.open.return_value.__exit__ = Mock(return_value=False)
            with patch("urllib.request.build_opener", return_value=opener):
                with self.assertRaises(Ambiguous): t.send(t.build(a), a)
                self.assertEqual(opener.open.call_count, 1)

    def test_missing_credentials_and_path_injection_block_before_send(self):
        with patch.dict("os.environ", {}, clear=True):
            with self.assertRaises(Block): self.transport("kite").build(self.action("kite"))
        a = self.action("kite", "cancel"); a["order_id"] = "../positions"
        with self.assertRaises(Block): self.transport("kite").build(a)


class ExecutionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        c = json.loads((ROOT / "assets/config.example.json").read_text()); c["account_id"] = "123456"
        card = json.loads((ROOT / "assets/charges.example.json").read_text())
        now, i, ctx, history, _, candles = inputs(c)
        i["freeze_quantity"] = 10000
        for section in ("session", "restrictions", "event", "account", "quote"): ctx[section]["source"] = "kite"
        p = plan_from_hit(orb(candles["instruments"]["NSE:DEMO-EQ"]["bars"], now), i, c, now)
        ctx["replay"] = replay(p, i, history, c, card)
        order = size(p, i, c, ctx, card, now)
        snap = {"broker": "kite", "account_id": "123456", "source": "kite", "date": now.date().isoformat(), "as_of": now.isoformat(), "orders": [], "positions": [], "available_inr": 100000}
        snap["state_hash"] = state_hash(snap); ctx["account"]["execution_state_hash"] = snap["state_hash"]
        cls.base = {"config": c, "instrument": i, "context": ctx, "charges": card, "order": order,
                    "live_margin": {"source": "kite", "account_id": "123456", "as_of": now.isoformat(), "payload_hash": fingerprint(payload(order)), "required_inr": order["metrics"]["notional_inr"]}}
        cls.snapshot = snap
        cls.profile = {"broker": "kite", "account_id": "123456", "enabled": True, "new_entries": True, "approval_mode": "human_terminal_two_stage",
                       "max_entry_notional_inr": 100000, "max_entry_loss_inr": 1000,
                       "acceptance": {"broker": "kite", "account_id": "123456", "status": "PASS", "source": "offline_test_acceptance", "as_of": now.isoformat()}}

    def setUp(self):
        self.b, self.s, self.p = copy.deepcopy((self.base, self.snapshot, self.profile))
        self.tmp = tempfile.TemporaryDirectory(); self.w = Path(self.tmp.name)
        self.store = ExecutionStore(self.w / "execution.sqlite")
        self.transport = Mock(); self.transport.send.return_value = {"order_id": "O1"}
        self.approval = Mock(); self.approval.approve.side_effect = lambda r: r["review_hash"]
        self.now = NOW

    def tearDown(self): self.store.close(); self.tmp.cleanup()

    def refresh(self): return prepare("entry", self.b, self.s, self.p, self.now), copy.deepcopy(self.s)
    def records(self): return self.store.records("kite", "123456")
    def run_entry(self): return execute(self.store, self.transport, self.approval, self.refresh, lambda: self.now)

    def filled_snapshot(self, partial=None):
        review, _ = self.refresh(); a = review["action"]
        q = a["quantity"] if partial is None else partial
        s = copy.deepcopy(self.s)
        ident = {k: self.b["instrument"][k] for k in ("exchange", "symbol", "token")}
        s["orders"] = [{**ident, "order_id": "O1", "product": "MIS", "side": a["side"], "quantity": a["quantity"], "filled_quantity": q,
                        "status": "FILLED" if partial is None else "PARTIAL", "raw_status": "COMPLETE" if partial is None else "OPEN", "price": a["price"], "trigger_price": 0, "order_type": "LIMIT", "variety": "regular"}]
        s["positions"] = [{**ident, "product": "MIS", "quantity": q}]
        s["state_hash"] = state_hash(s)
        return s

    def test_durable_submitting_precedes_network_and_ack_not_fill(self):
        def send(*args):
            self.assertEqual(self.records()[0]["state"], "SUBMITTING")
            return {"order_id": "O1"}
        self.transport.send.side_effect = send
        result = self.run_entry()
        self.assertTrue(result["submitted"]); self.assertFalse(result["filled"]); self.assertFalse(result["protected"])
        self.assertEqual(self.records()[0]["state"], "ACKNOWLEDGED")

    def test_timeout_restart_blocks_all_duplicate_requests(self):
        self.transport.send.side_effect = TimeoutError("private")
        with self.assertRaises(Ambiguous): self.run_entry()
        self.store.close(); self.store = ExecutionStore(self.w / "execution.sqlite")
        self.assertEqual(self.records()[0]["state"], "UNKNOWN")
        with self.assertRaises(Block): self.run_entry()
        self.assertEqual(self.transport.send.call_count, 1)

    def test_ctrl_c_after_send_is_durably_unknown(self):
        self.transport.send.side_effect = KeyboardInterrupt()
        with self.assertRaises(Ambiguous): self.run_entry()
        self.assertEqual(self.records()[0]["state"], "UNKNOWN")

    def test_declined_approval_never_creates_intent(self):
        self.approval.approve.return_value = "no"; self.approval.approve.side_effect = None
        with self.assertRaises(Block): self.run_entry()
        self.assertEqual(self.records(), []); self.transport.send.assert_not_called()

    def test_expired_approval(self):
        def approve(r): self.now += timedelta(seconds=91); return r["review_hash"]
        self.approval.approve.side_effect = approve
        with self.assertRaises(Block): self.run_entry()
        self.transport.send.assert_not_called()

    def test_changed_config_after_approval_requires_new_review(self):
        def approve(r): self.b["config"]["max_orders_per_day"] += 1; return r["review_hash"]
        self.approval.approve.side_effect = approve
        with self.assertRaisesRegex(Block, "REVIEW_CHANGED"): self.run_entry()
        self.transport.send.assert_not_called()

    def test_disabled_and_stale_acceptance(self):
        self.p["enabled"] = False
        with self.assertRaises(Block): self.refresh()
        self.p["enabled"] = True; self.p["acceptance"]["as_of"] = (NOW - timedelta(days=2)).isoformat()
        with self.assertRaises(Block): self.refresh()

    def test_synthetic_sources_cannot_unlock_live(self):
        self.b["context"]["session"]["source"] = "synthetic_demo"
        with self.assertRaisesRegex(Block, "DEMO_INPUT"): self.refresh()

    def test_account_hash_funds_and_margin_binding(self):
        self.b["context"]["account"]["execution_state_hash"] = "wrong"
        with self.assertRaisesRegex(Block, "LEDGER"): self.refresh()
        self.b = copy.deepcopy(self.base); self.s["available_inr"] = 1
        with self.assertRaisesRegex(Block, "FUNDS"): self.refresh()
        self.s = copy.deepcopy(self.snapshot); self.b["live_margin"]["payload_hash"] = "wrong"
        with self.assertRaisesRegex(Block, "MARGIN"): self.refresh()

    def test_live_caps_and_freeze_limit(self):
        self.p["max_entry_notional_inr"] = 1
        with self.assertRaisesRegex(Block, "LIVE_LIMIT"): self.refresh()
        self.p = copy.deepcopy(self.profile); self.b["instrument"]["freeze_quantity"] = 1
        with self.assertRaises(Block): self.refresh()

    def test_after_review_risk_is_recomputed(self):
        def approve(r): self.b["context"]["local_halt"] = True; return r["review_hash"]
        self.approval.approve.side_effect = approve
        with self.assertRaisesRegex(Block, "LOCAL_HALT"): self.run_entry()
        self.transport.send.assert_not_called()

    def test_local_build_failure_creates_no_unknown_request(self):
        self.transport.build.side_effect = Block("AUTH")
        with self.assertRaises(Block): self.run_entry()
        self.assertEqual(self.records(), [])

    def test_concurrent_review_cannot_send_twice(self):
        review, _ = self.refresh()
        self.store.start(review, 0, NOW)
        with self.assertRaises(Block): self.store.start(review, 0, NOW)
        self.assertEqual(len(self.records()), 1)

    def test_partial_fill_is_observed_without_claiming_completion(self):
        s = self.filled_snapshot(partial=3)
        self.run_entry(); self.store.observe(s, NOW)
        self.assertEqual(self.records()[0]["state"], "PARTIAL")
        self.assertEqual(self.records()[0]["filled_quantity"], 3)
        self.assertEqual(protection(s)[0]["state"], "UNPROTECTED_OR_UNVERIFIED")

    def test_reservations_rebuild_from_observed_intents(self):
        s = self.filled_snapshot(); self.run_entry()
        with self.assertRaises(Block): self.store.reservations(s)
        self.store.observe(s, NOW)
        result = self.store.reservations(s)
        self.assertEqual(result["entries"][0]["entry_order_id"], "O1")
        self.assertEqual(result["entries"][0]["planned_loss_inr"], self.b["order"]["metrics"]["loss_at_stop_inr"])

    def test_fill_regression_and_payload_change_block_recovery(self):
        s = self.filled_snapshot(partial=3); self.run_entry(); self.store.observe(s, NOW)
        s["orders"][0]["filled_quantity"] = 2
        with self.assertRaises(Block): self.store.observe(s, NOW)
        s["orders"][0]["filled_quantity"] = 3; s["orders"][0]["price"] = 1
        with self.assertRaises(Block): self.store.observe(s, NOW)

    def test_missing_known_order_remains_unresolved(self):
        s = self.filled_snapshot(partial=3); self.run_entry(); self.store.observe(s, NOW)
        self.store.observe(self.s, NOW)
        self.assertEqual(self.records()[0]["state"], "UNKNOWN")

    def test_bind_requires_explicit_exact_identity_and_human_review(self):
        s = self.filled_snapshot()
        self.transport.send.side_effect = TimeoutError()
        with self.assertRaises(Ambiguous): self.run_entry()
        key = self.records()[0]["id"]
        with self.assertRaises(Block): self.store.bind(key, "other", s, self.approval, lambda: NOW)
        result = self.store.bind(key, "O1", s, self.approval, lambda: NOW)
        self.assertEqual(result[0]["state"], "FILLED")
        self.assertEqual(self.transport.send.call_count, 1)

    def test_exit_bypasses_entry_halt_but_cannot_reverse(self):
        s = self.filled_snapshot(); q = s["positions"][0]["quantity"]
        self.p["new_entries"] = False; self.p["acceptance"]["status"] = "NOT_TESTED"; self.b["context"]["local_halt"] = True
        result = prepare("exit", self.b, s, self.p, NOW, quantity=q, price=101)
        self.assertEqual(result["action"]["side"], "SELL")
        with self.assertRaisesRegex(Block, "EXIT"): prepare("exit", self.b, s, self.p, NOW, quantity=q + 1, price=101)

    def test_partial_entry_must_be_cancelled_before_protecting(self):
        s = self.filled_snapshot(partial=3)
        with self.assertRaisesRegex(Block, "WORKING_ORDER"): prepare("stop", self.b, s, self.p, NOW, quantity=3, price=98, trigger=99)
        s["orders"][0]["status"] = "CANCELLED"; s["state_hash"] = state_hash(s)
        result = prepare("stop", self.b, s, self.p, NOW, quantity=3, price=98, trigger=99)
        self.assertEqual(result["payload"]["order_type"], "SL")

    def test_target_exit_cannot_overlap_existing_stop(self):
        s = self.filled_snapshot(); row = copy.deepcopy(s["orders"][0])
        row.update(order_id="S1", side="SELL", filled_quantity=0, status="OPEN", raw_status="TRIGGER PENDING", order_type="SL", trigger_price=99, price=98)
        s["orders"].append(row); s["state_hash"] = state_hash(s)
        self.assertEqual(protection(s)[0]["state"], "STOP_WAITING")
        with self.assertRaisesRegex(Block, "WORKING_ORDER"): prepare("exit", self.b, s, self.p, NOW, quantity=row["quantity"], price=103)

    def test_cancel_ack_never_proves_cancelled(self):
        s = self.filled_snapshot(partial=3)
        review = prepare("cancel", self.b, s, self.p, NOW, order_id="O1")
        key = self.store.start(review, 0, NOW); self.store.outcome(key, "ACKNOWLEDGED", NOW, "O1")
        self.store.observe(s, NOW)
        self.assertEqual(self.records()[0]["state"], "UNKNOWN")
        s["orders"][0]["status"] = "CANCELLED"
        self.store.observe(s, NOW)
        self.assertEqual(self.records()[0]["state"], "CANCELLED")

    def test_stop_tick_and_crossed_trigger_block(self):
        s = self.filled_snapshot(); q = s["positions"][0]["quantity"]
        for price, trigger in ((98.01, 99), (98, 103), (100, 99)):
            with self.assertRaises(Block): prepare("stop", self.b, s, self.p, NOW, quantity=q, price=price, trigger=trigger)

    def test_terminal_approval_refuses_pipe_and_either_decline(self):
        r, _ = self.refresh(); gate = TerminalApproval()
        with patch("sys.stdin.isatty", return_value=False):
            with self.assertRaises(Block): gate.approve(r)
        with patch("sys.stdin.isatty", return_value=True), patch("sys.stdout", new=io.StringIO()) as output, patch.object(output, "isatty", return_value=True):
            for responses in (["no"], ["REVIEW " + r["review_hash"], "no"]):
                with patch("builtins.input", side_effect=responses):
                    with self.assertRaises(Block): gate.approve(r)
            with patch("builtins.input", side_effect=["REVIEW " + r["review_hash"], "SEND kite:123456 " + r["review_hash"]]):
                self.assertEqual(gate.approve(r), r["review_hash"])

    def test_pipeline_files_are_loaded_without_renaming_orders(self):
        for name, data in (("config", self.b["config"]), ("context", self.b["context"]), ("instrument", self.b["instrument"]), ("charges", self.b["charges"]), ("replay", self.b["context"]["replay"]), ("orders", [self.b["order"]])):
            write(self.w / (name + ".json"), data)
        self.assertEqual(load_bundle(self.w)["order"], self.b["order"])

    def test_empty_live_journal_reconciliation_binds_broker_state(self):
        write(self.w / "context.json", self.b["context"])
        with patch("live.instant", return_value=NOW): result = reconcile_account(self.w, self.s)
        self.assertTrue(result["reconciled"])
        self.assertEqual(result["execution_state_hash"], self.s["state_hash"])

    def test_cancel_timeout_keeps_known_id_for_recovery(self):
        s = self.filled_snapshot(partial=3)
        review = prepare("cancel", self.b, s, self.p, NOW, order_id="O1")
        self.transport.send.side_effect = TimeoutError()
        with self.assertRaises(Ambiguous):
            execute(self.store, self.transport, self.approval, lambda: (review, s), lambda: NOW)
        self.assertEqual(self.records()[0]["order_id"], "O1")
        s["orders"][0]["status"] = "CANCELLED"
        self.store.observe(s, NOW)
        self.assertEqual(self.records()[0]["state"], "CANCELLED")

    def test_bind_cannot_claim_preexisting_order(self):
        s = self.filled_snapshot()
        review, _ = self.refresh(); review["baseline_order_ids"] = ["O1"]
        key = self.store.start(review, 0, NOW)
        self.store.outcome(key, "UNKNOWN", NOW)
        with self.assertRaisesRegex(Block, "BIND"):
            self.store.bind(key, "O1", s, self.approval, lambda: NOW)

    def test_reducing_short_exit_buys_exact_units(self):
        s = self.filled_snapshot()
        s["orders"][0]["side"] = "SELL"; s["positions"][0]["quantity"] *= -1; s["state_hash"] = state_hash(s)
        q = -s["positions"][0]["quantity"]
        result = prepare("exit", self.b, s, self.p, NOW, quantity=q, price=103)
        self.assertEqual(result["action"]["side"], "BUY")
        with self.assertRaises(Block): prepare("exit", self.b, s, self.p, NOW, quantity=q + 1, price=103)

    def test_refresh_timestamp_can_advance_without_changing_authorized_economics(self):
        before, _ = self.refresh()
        self.now += timedelta(seconds=1)
        self.b["live_margin"]["as_of"] = self.now.isoformat()
        self.b["context"]["quote"]["as_of"] = self.now.isoformat()
        self.s["as_of"] = self.now.isoformat()
        after, _ = self.refresh()
        self.assertEqual(before["review_hash"], after["review_hash"])
        self.b["context"]["quote"]["price"] = 102.05
        changed, _ = self.refresh()
        self.assertNotEqual(before["review_hash"], changed["review_hash"])

    def test_cli_entry_refreshes_reads_twice_and_records_ack_with_fake_transport(self):
        for name, data in (("config", self.b["config"]), ("context", self.b["context"]), ("instrument", self.b["instrument"]),
                           ("charges", self.b["charges"]), ("replay", self.b["context"]["replay"]), ("orders", [self.b["order"]]), ("live-profile", self.p)):
            write(self.w / (name + ".json"), data)
        _, raw = sample(); raw["orders"] = []; raw["positions"] = {"net": []}
        client = Mock(); client.account.return_value = raw; client.margin.return_value = self.b["live_margin"]
        i = self.b["instrument"]
        client.quotes.return_value = {"source": "kite", "data": {"NSE:" + i["symbol"]: {"instrument_token": i["token"], "last_price": 102, "last_trade_time": NOW.isoformat()}}}
        def clocked(*args): return execute(*args, clock=lambda: NOW)
        with patch("live.adapter", return_value=client), patch("live.LiveBroker", return_value=self.transport), patch("live.TerminalApproval", return_value=self.approval), \
                patch("live.instant", return_value=NOW), patch("live.execute", side_effect=clocked), patch("sys.stdout", new_callable=io.StringIO) as output:
            code = main(["entry", "--workdir", str(self.w)])
        self.assertEqual(code, 0, output.getvalue())
        self.assertEqual(json.loads(output.getvalue())["state"], "ACKNOWLEDGED")
        self.assertEqual(client.account.call_count, 2); self.assertEqual(client.quotes.call_count, 2); self.assertEqual(client.margin.call_count, 2)
        self.transport.send.assert_called_once()

    def test_panic_preview_halts_entries_without_sending(self):
        for name, data in (("config", self.b["config"]), ("context", self.b["context"]), ("instrument", self.b["instrument"]), ("charges", self.b["charges"])):
            write(self.w / (name + ".json"), data)
        _, raw = sample(filled=4); client = Mock(); client.account.return_value = raw
        with patch("live.adapter", return_value=client), patch("live.instant", return_value=NOW), patch("sys.stdout", new_callable=io.StringIO) as output:
            self.assertEqual(main(["panic-preview", "--workdir", str(self.w)]), 0)
        result = json.loads(output.getvalue())
        self.assertEqual(result["cancel_first"], ["O1"])
        self.assertEqual(result["remaining_positions"][0]["quantity"], 4)
        self.assertTrue(json.loads((self.w / "state/halt.json").read_text())["halted"])
        self.transport.send.assert_not_called()


if __name__ == "__main__": unittest.main()
