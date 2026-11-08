import copy
from datetime import timedelta
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
from core import Block
from test_execution import sample, NOW
from live_state import normalize
from trade_import import normalize_trades, fetch, trade_time, ROUTES
from broker import adapter
from journal import Journal, reconcile
from fee_policy import reserve_fees
from delivery import deliver, destination, NoRedirect
from service import run, ProcessLock
from trader import read, write


def trade_sample(broker):
    i, raw_account = sample(broker, filled=4)
    snap = normalize(raw_account, [i], NOW)
    row = copy.deepcopy(raw_account["orders"][0])
    if broker == "kite": row.update(trade_id="T1", quantity=4, average_price=102, fill_timestamp="2026-10-30 09:34:00")
    elif broker == "upstox": row.update(trade_id="T1", quantity=4, average_price=102, exchange_timestamp="30-Oct-2026 09:34:00")
    elif broker == "dhan": row.update(exchangeTradeId="T1", tradedQuantity=4, tradedPrice=102, exchangeTime="2026-10-30 09:34:00")
    elif broker == "angel": row.update(fillid="T1", fillsize="4", fillprice="102", filltime="09:34:00"); row.pop("symboltoken")
    else: row.update(orderNumber="O1", tradeNumber="T1", tradedQty=4, tradePrice=102, orderDateTime="30-Oct-2026 09:34:00")
    raw = {"broker": broker, "account_id": "123456", "source": broker, "date": "2026-10-30", "as_of": NOW.isoformat(), "trades": [row]}
    return i, raw_account, snap, raw


class TradeImportTests(unittest.TestCase):
    def test_five_broker_books_normalize_real_fill_fields(self):
        for b in ROUTES:
            i, _, snap, raw = trade_sample(b)
            result = normalize_trades(raw, snap, [i], NOW)
            fill = result["fills"][0]
            self.assertEqual(fill["quantity"], 4); self.assertEqual(fill["price"], 102)
            self.assertEqual(fill["token"], i["token"]); self.assertFalse(fill["fees_final"]); self.assertEqual(fill["fees"], {})

    def test_readonly_fetch_observes_identity_and_snapshot(self):
        for b in ROUTES:
            i, acct, _, raw = trade_sample(b)
            client = Mock(); client.account_id.return_value = "123456"; client.account.return_value = acct
            client.request.return_value = {"tradeBook": raw["trades"]} if b == "fyers" else raw["trades"]
            self.assertEqual(len(fetch(client, b, "123456", [i], lambda: NOW)["fills"]), 1)
            client.request.assert_called_once_with("GET", ROUTES[b])

    def test_trade_get_routes_never_allow_mutations(self):
        with patch.dict("os.environ", {}, clear=True):
            for b, path in ROUTES.items():
                with self.assertRaisesRegex(Block, "READ_ONLY"): adapter(b).request("POST", path)
                with self.assertRaises(Block) as caught: adapter(b).request("GET", path)
                self.assertNotIn("READ_ONLY", str(caught.exception))

    def test_duplicate_trade_is_idempotent_but_conflict_blocks(self):
        i, _, snap, raw = trade_sample("kite")
        raw["trades"] *= 2
        self.assertEqual(len(normalize_trades(raw, snap, [i], NOW)["fills"]), 1)
        raw["trades"][1] = {**raw["trades"][1], "average_price": 103}
        with self.assertRaisesRegex(Block, "conflicting"): normalize_trades(raw, snap, [i], NOW)

    def test_split_fills_must_sum_to_observed_order(self):
        i, _, snap, raw = trade_sample("kite")
        raw["trades"][0]["quantity"] = 2
        raw["trades"].append({**raw["trades"][0], "trade_id": "T2"})
        self.assertEqual(len(normalize_trades(raw, snap, [i], NOW)["fills"]), 2)
        raw["trades"].pop()
        with self.assertRaisesRegex(Block, "quantities disagree"): normalize_trades(raw, snap, [i], NOW)

    def test_missing_trade_book_never_infers_fills_from_order(self):
        i, _, snap, raw = trade_sample("kite"); raw["trades"] = []
        with self.assertRaises(Block): normalize_trades(raw, snap, [i], NOW)

    def test_cross_account_cross_side_and_cross_token_block(self):
        for key, value in (("clientcode", "different"), ("transaction_type", "SELL"), ("instrument_token", "888")):
            i, _, snap, raw = trade_sample("kite"); raw["trades"][0][key] = value
            with self.assertRaises(Block): normalize_trades(raw, snap, [i], NOW)

    def test_future_stale_or_prior_day_trade_blocks(self):
        i, _, snap, raw = trade_sample("kite")
        for value in ("2026-10-30 09:36:00", "2026-10-29 09:34:00"):
            raw["trades"][0]["fill_timestamp"] = value
            with self.assertRaises(Block): normalize_trades(raw, snap, [i], NOW)
        with self.assertRaises(Block): normalize_trades(raw, snap, [i], NOW + timedelta(seconds=61))

    def test_composite_fill_ids_are_stable_for_reimport(self):
        i, _, snap, raw = trade_sample("angel")
        batch = normalize_trades(raw, snap, [i], NOW)
        with tempfile.TemporaryDirectory() as d:
            j = Journal(Path(d) / "j.sqlite")
            try:
                self.assertEqual(j.import_fills(batch, NOW)["imported"], 1)
                self.assertEqual(j.import_fills(batch, NOW)["duplicates"], 1)
            finally: j.close()

    def test_documented_datetime_forms_and_missing_dates(self):
        for value in ("30-Oct-2026 09:34:00", "30-10-2026 09:34:00", "2026-10-30T09:34:00+05:30"):
            self.assertEqual(trade_time(value, "2026-10-30", "fyers").hour, 9)
        with self.assertRaises(Block): trade_time("09:34:00", "2026-10-30", "kite")


class FeePolicyTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.j = Journal(Path(self.tmp.name) / "j.sqlite")
        self.i, _, self.snap, raw = trade_sample("kite")
        self.batch = normalize_trades(raw, self.snap, [self.i], NOW)
        self.j.import_fills(self.batch, NOW)
        self.snap.update(complete=True, opening_positions=[], cash_available=100000, margin_available=100000)
        self.snap["positions"][0]["position_value_inr"] = 408
        self.res = {"broker": "kite", "account_id": "123456", "date": "2026-10-30", "entries": [{"entry_order_id": "O1", "exit_order_ids": [], "planned_loss_inr": 1}]}
        self.card = json.loads((ROOT / "assets/charges.example.json").read_text())
        self.policy = {"mode": "per_fill_roundtrip_reserve", "source": "test_operator_policy", "broker": "kite", "account_id": "123456", "as_of": NOW.isoformat(), "multiplier": 1.5, "per_fill_buffer_inr": 50}

    def tearDown(self): self.j.close(); self.tmp.cleanup()

    def rec(self, policy): return reconcile(self.j, self.snap, self.res, NOW, fee_policy=policy, instruments=[self.i], rate_card=self.card)

    def test_default_still_requires_final_fees(self):
        self.assertFalse(self.rec(None)["reconciled"])

    def test_explicit_policy_reserves_fees_without_changing_journal_truth(self):
        result = self.rec(self.policy)
        self.assertTrue(result["reconciled"])
        self.assertGreater(result["daily_risk_used"], 50)
        self.assertEqual(result["provisional_fill_count"], 1)
        fill = self.j.fills("kite", "123456", "2026-10-30")[0]
        self.assertFalse(fill["fees_final"]); self.assertEqual(fill["fees"], {})

    def test_rate_policy_and_account_mismatches_fail_closed(self):
        for key, value in (("account_id", "wrong"), ("multiplier", 0.5), ("per_fill_buffer_inr", -1), ("as_of", "2026-10-28T09:35:00+05:30")):
            p = {**self.policy, key: value}
            with self.assertRaises(Block): self.rec(p)
        self.card["review_after"] = "2026-10-29"
        with self.assertRaises(Block): self.rec(self.policy)

    def test_final_correction_restores_strict_reconciliation(self):
        f = self.batch["fills"][0]
        self.j.contract_fees({"broker": "kite", "account_id": "123456", "fill_id": f["fill_id"], "fees": {"total": 2}, "source": "test_contract_note", "as_of": NOW.isoformat()}, NOW)
        result = self.rec(None)
        self.assertTrue(result["reconciled"]); self.assertEqual(result["provisional_fill_count"], 0)

    def test_estimate_never_reduces_larger_recorded_cost(self):
        f = copy.deepcopy(self.batch["fills"]); f[0]["fees"] = {"known": 1000}
        values, _ = reserve_fees(f, self.policy, [self.i], self.card, "kite", "123456", NOW)
        self.assertEqual(values[0]["fees"]["provisional_reserve"], 1000)


class ServiceDeliveryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.w = Path(self.tmp.name)
        self.j = Journal(self.w / "state/journal.sqlite")
        self.id = self.j.enqueue({"at": NOW.isoformat(), "message": "STOP_LEVEL_REACHED", "workdir": "private", "account_id": "private"})
    def tearDown(self): self.j.close(); self.tmp.cleanup()

    def test_delivery_sends_only_selected_fields_and_acknowledges_once(self):
        sender = Mock(return_value=True)
        result = deliver(self.j, "https://example.com/hook", sender=sender, clock=lambda: NOW)
        self.assertEqual(result["accepted"], 1); self.assertEqual(self.j.pending(), [])
        self.assertEqual(set(sender.call_args.args[2]), {"id", "message", "at"})
        deliver(self.j, "https://example.com/hook", sender=sender, clock=lambda: NOW)
        sender.assert_called_once()

    def test_timeout_no_auto_retry_explicit_retry_keeps_id(self):
        sender = Mock(side_effect=TimeoutError("private"))
        self.assertEqual(deliver(self.j, "https://example.com/hook", sender=sender, clock=lambda: NOW)["unknown"], 1)
        self.assertEqual(deliver(self.j, "https://example.com/hook", sender=sender, clock=lambda: NOW)["skipped_unresolved"], 1)
        self.assertEqual(sender.call_count, 1)
        sender.side_effect = None; sender.return_value = True
        self.assertEqual(deliver(self.j, "https://example.com/hook", sender=sender, clock=lambda: NOW, retry_unknown=True)["accepted"], 1)
        self.assertEqual(sender.call_args.args[2]["id"], self.id)

    def test_interrupted_delivery_is_not_silently_replayed(self):
        sender = Mock(side_effect=KeyboardInterrupt())
        with self.assertRaises(KeyboardInterrupt): deliver(self.j, "https://example.com/hook", sender=sender, clock=lambda: NOW)
        self.assertEqual(deliver(self.j, "https://example.com/hook", sender=sender, clock=lambda: NOW)["skipped_unresolved"], 1)

    def test_redirects_and_invalid_destinations(self):
        self.assertIsNone(NoRedirect().redirect_request(None, None, 302, "", {}, "https://other.example"))
        for url in ("http://example.com", "https://user:password@example.com", "https://example.com/#fragment"):
            with self.assertRaises(Block): destination(url)

    def test_supervisor_stops_and_halts_after_bounded_failures(self):
        sleeps = []; worker = Mock(return_value={"state": "disconnected", "entry_allowed": False})
        result = run(self.w, self.j, cycles=20, max_failures=3, worker=worker, clock=lambda: NOW, sleep=sleeps.append)
        self.assertEqual(result["completed_cycles"], 3); self.assertEqual(result["reason"], "failure_limit")
        self.assertEqual(sleeps, [10, 20]); self.assertFalse(read(self.w / "state/monitor.json")["entry_allowed"])
        self.assertTrue(read(self.w / "state/halt.json")["halted"])

    def test_recovery_resets_failure_streak(self):
        worker = Mock(side_effect=[{"state": "disconnected"}, {"state": "running"}, {"state": "disconnected"}])
        sleeps = []
        result = run(self.w, self.j, cycles=3, max_failures=2, worker=worker, clock=lambda: NOW, sleep=sleeps.append)
        self.assertEqual(result["reason"], "cycle_limit"); self.assertEqual(sleeps, [10, 5])

    def test_lock_prevents_duplicate_process_and_releases_after_exception(self):
        path = self.w / "monitor.lock"
        with ProcessLock(path):
            with self.assertRaises(Block):
                with ProcessLock(path): pass
        with ProcessLock(path): pass
        worker = Mock(side_effect=RuntimeError("test crash"))
        with self.assertRaises(RuntimeError): run(self.w, self.j, worker=worker, clock=lambda: NOW)
        self.assertEqual(read(self.w / "state/monitor.json")["state"], "stopped")
        with ProcessLock(self.w / "state/monitor.lock"): pass

    def test_stop_request_checked_between_cycles(self):
        def stop(_): write(self.w / "state/service-stop.json", {"at": NOW.isoformat()})
        result = run(self.w, self.j, cycles=10, worker=Mock(return_value={"state": "running"}), clock=lambda: NOW, sleep=stop)
        self.assertEqual(result["completed_cycles"], 1); self.assertEqual(result["reason"], "requested_stop")

    def test_invalid_loop_settings_block_without_work(self):
        worker = Mock()
        for kwargs in ({"cycles": 0}, {"interval": 0}, {"max_failures": 0}):
            with self.assertRaises(Block): run(self.w, self.j, worker=worker, **kwargs)
        worker.assert_not_called()


if __name__ == "__main__": unittest.main()
