import copy
from datetime import timedelta
import io
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
from core import Block, instant
from journal import Journal, reconcile, main as journal_main
from portfolio import save_opening, load_opening, correct_opening, reconcile_portfolio, main
from live_state import normalize
from test_execution import sample, NOW as LIVE_NOW

NOW = instant("2026-11-06T10:00:00+05:30")


class PortfolioTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.w = Path(self.tmp.name)
        self.j = Journal(self.w / "journal.sqlite")
        self.i = {"broker": "kite", "exchange": "NSE", "symbol": "DEMO", "token": "123", "kind": "equity", "lot_size": 1, "tick_size": 0.05, "master_date": "2026-11-06", "expiry": None}
        self.ident = {"exchange": "NSE", "symbol": "DEMO", "token": "123", "product": "CNC"}
        self.header = {"broker": "kite", "account_id": "DEMO", "source": "synthetic_statement", "date": "2026-11-06", "as_of": NOW.isoformat(), "complete": True}
        self.opening = {**self.header, "schema": "portfolio_opening_v1", "previous_session": "2026-11-05", "adjustments_reviewed": True, "inventory_events": [],
                        "positions": [{**self.ident, "quantity": 10, "average_price": 100, "previous_close": 110, "price_basis": "per_unit_inr", "cost_source": "synthetic_cost", "mark_source": "synthetic_close", "mark_session": "2026-11-05"}]}
        self.snap = {**self.header, "schema": "portfolio_snapshot_v1", "inventory_events": [], "orders": [], "positions": [self.position(10)]}

    def tearDown(self): self.j.close(); self.tmp.cleanup()

    def position(self, q, mark=120):
        return {**self.ident, "quantity": q, "mark_price": mark, "mark_source": "synthetic_quote", "mark_as_of": NOW.isoformat(), "price_basis": "per_unit_inr"}

    def save(self): return save_opening(self.j, self.opening, [self.i], NOW)
    def report(self): return reconcile_portfolio(self.j, self.snap, [self.i], NOW)

    def fill(self, q, side, price, oid="o1", at="2026-11-06T09:59:00+05:30", final=True, fee=1, **extra):
        f = {**self.ident, "fill_id": "f" + oid, "order_id": oid, "source": "synthetic_trade", "at": at, "side": side, "quantity": q, "price": price,
             "fees": {"total": fee} if final else {}, "fees_final": final, **extra}
        self.j.import_fills({**self.header, "fills": [f]}, NOW, allow_carry=True)
        self.snap["orders"].append({**self.ident, "order_id": oid, "side": side, "quantity": q, "filled_quantity": q, "status": "FILLED"})
        return f

    def derivative(self, q=10):
        self.i.update(exchange="NFO", kind="future", expiry="2026-11-26", underlying_type="index", lot_size=5)
        self.ident.update(exchange="NFO", product="NRML")
        self.opening["positions"][0].update(self.ident, quantity=q)
        self.snap["positions"] = [self.position(q)]

    def test_unchanged_carry_distinguishes_daily_and_acquisition_pnl(self):
        self.save(); r = self.report()
        self.assertEqual(r["status"], "RECONCILED")
        self.assertEqual(r["totals"]["daily_gross_inr"], 100)
        self.assertEqual(r["totals"]["unrealized_gross_inr"], 200)
        self.assertFalse(r["execution_eligible"]); self.assertFalse(r["submitted"])
        self.assertNotIn("reconciled", r)

    def test_partial_delivery_sale_and_today_fees(self):
        self.save(); self.fill(4, "SELL", 115); self.snap["positions"] = [self.position(6)]
        r = self.report()
        self.assertTrue(r["inventory_reconciled"])
        self.assertEqual(r["totals"], {"daily_gross_inr": 80, "realized_gross_inr": 60, "unrealized_gross_inr": 120, "recorded_today_fees_inr": 1, "daily_net_inr": 79})

    def test_complete_sale_accepts_absent_or_explicit_zero_position(self):
        self.save(); self.fill(10, "SELL", 115); self.snap["positions"] = []
        self.assertEqual(self.report()["totals"]["daily_net_inr"], 49)
        self.snap["positions"] = [{**self.ident, "quantity": 0}]
        self.assertTrue(self.report()["inventory_reconciled"])

    def test_purchase_recomputes_pooled_average_without_double_counting_opening(self):
        self.save(); self.fill(10, "BUY", 120); self.snap["positions"] = [self.position(20, 125)]
        r = self.report()
        self.assertEqual(r["positions"][0]["pooled_average_price"], "110")
        self.assertEqual(r["totals"]["daily_gross_inr"], 200)
        self.assertEqual(r["totals"]["unrealized_gross_inr"], 300)

    def test_short_future_reduction(self):
        self.derivative(-10); self.save(); self.fill(5, "BUY", 105); self.snap["positions"] = [self.position(-5, 90)]
        r = self.report()
        self.assertEqual(r["totals"]["daily_gross_inr"], 125)
        self.assertEqual(r["totals"]["realized_gross_inr"], -25)
        self.assertEqual(r["totals"]["unrealized_gross_inr"], 50)

    def test_future_reversal_opens_new_cost_basis(self):
        self.derivative(); self.save(); self.fill(15, "SELL", 115); self.snap["positions"] = [self.position(-5, 110)]
        r = self.report()
        self.assertEqual(r["positions"][0]["pooled_average_price"], "115")
        self.assertEqual(r["totals"]["daily_gross_inr"], 75)
        self.assertEqual(r["totals"]["realized_gross_inr"], 150)

    def test_delivery_oversell_never_becomes_a_short(self):
        self.save(); self.fill(11, "SELL", 115); self.snap["positions"] = []
        with self.assertRaisesRegex(Block, "exceeds"): self.report()

    def test_cnc_and_mis_same_symbol_do_not_net_together(self):
        self.save(); self.ident["product"] = "MIS"; self.fill(2, "SELL", 120)
        self.snap["positions"].append(self.position(-2, 115))
        r = self.report()
        self.assertEqual(len(r["positions"]), 2); self.assertTrue(r["inventory_reconciled"])
        self.assertEqual(r["totals"]["daily_gross_inr"], 110)

    def test_mismatched_inventory_suppresses_pnl(self):
        self.save(); self.snap["positions"][0]["quantity"] = 9
        r = self.report()
        self.assertEqual(r["status"], "BLOCKED"); self.assertIsNone(r["totals"])
        self.assertIsNone(r["positions"][0]["daily_gross_inr"])

    def test_unknown_missing_and_partial_order_evidence_blocks(self):
        self.save(); self.fill(4, "SELL", 115); self.snap["positions"] = [self.position(6)]
        for orders in ([], [{**self.snap["orders"][0], "status": "UNKNOWN"}], [{**self.snap["orders"][0], "quantity": 5, "filled_quantity": 5}]):
            with self.subTest(orders=orders):
                snap = {**self.snap, "orders": orders}
                self.assertFalse(reconcile_portfolio(self.j, snap, [self.i], NOW)["inventory_reconciled"])

    def test_duplicate_and_contradictory_orders_are_rejected(self):
        self.save(); self.fill(4, "SELL", 115)
        self.snap["orders"] *= 2
        with self.assertRaises(Block): self.report()
        self.snap["orders"] = self.snap["orders"][:1]; self.snap["orders"][0]["status"] = "OPEN"
        with self.assertRaises(Block): self.report()

    def test_working_order_never_counts_as_inventory(self):
        self.save(); self.snap["orders"] = [{**self.ident, "order_id": "o1", "side": "SELL", "quantity": 10, "filled_quantity": 0, "status": "OPEN"}]
        r = self.report()
        self.assertTrue(r["inventory_reconciled"]); self.assertEqual(r["working_orders"], 1)
        self.assertEqual(r["positions"][0]["expected_quantity"], 10)

    def test_partial_fill_and_cancelled_remainder(self):
        self.save(); self.fill(4, "SELL", 115); self.snap["positions"] = [self.position(6)]
        for status in ("PARTIAL", "CANCELLED"):
            self.snap["orders"][0].update(quantity=10, status=status)
            self.assertTrue(self.report()["inventory_reconciled"])

    def test_provisional_fees_keep_net_unknown_until_contract_note(self):
        self.save(); f = self.fill(4, "SELL", 115, final=False); self.snap["positions"] = [self.position(6)]
        r = self.report(); self.assertEqual(r["status"], "PARTIAL"); self.assertIsNone(r["totals"]["daily_net_inr"])
        self.j.contract_fees({**self.header, "fill_id": f["fill_id"], "fees": {"total": 2}}, NOW)
        self.assertEqual(self.report()["totals"]["daily_net_inr"], 78)

    def test_same_time_fills_require_sourced_sequence(self):
        self.save(); self.fill(10, "SELL", 115); self.fill(10, "BUY", 120, oid="o2")
        with self.assertRaisesRegex(Block, "sequence"): self.report()

    def test_same_time_sequence_is_economic_order_not_fill_id(self):
        self.save(); self.fill(10, "BUY", 120, oid="a", sequence=2, sequence_source="synthetic_exchange")
        self.fill(10, "SELL", 115, oid="z", sequence=1, sequence_source="synthetic_exchange")
        r = self.report(); self.assertEqual(r["positions"][0]["pooled_average_price"], "120")
        self.assertEqual(r["totals"]["realized_gross_inr"], 150)

    def test_timestamp_offsets_sort_by_instant(self):
        self.opening["positions"] = []; self.snap["positions"] = []; self.ident["product"] = "MIS"; self.save()
        self.fill(10, "BUY", 100, oid="a", at="2026-11-06T04:20:00+00:00")
        self.fill(10, "SELL", 105, oid="b", at="2026-11-06T09:55:00+05:30")
        self.assertEqual(self.report()["totals"]["realized_gross_inr"], 50)

    def test_opening_is_idempotent_immutable_and_survives_restart(self):
        self.assertTrue(self.save()["saved"]); self.assertTrue(self.save()["duplicate"])
        self.opening["positions"][0]["quantity"] = 11
        with self.assertRaisesRegex(Block, "conflicting"): self.save()
        self.j.close(); self.j = Journal(self.w / "journal.sqlite")
        self.assertEqual(load_opening(self.j, "kite", "DEMO", "2026-11-06")["positions"][0]["quantity"], 10)
        self.assertTrue(self.report()["inventory_reconciled"])

    def test_opening_database_tampering_is_detected(self):
        self.save()
        with self.j.db: self.j.db.execute("UPDATE portfolio_openings SET hash='bad'")
        with self.assertRaisesRegex(Block, "integrity"): self.report()

    def test_missing_cross_account_or_new_day_opening_blocks(self):
        with self.assertRaises(Block): self.report()
        self.save(); self.snap["account_id"] = "OTHER"
        with self.assertRaises(Block): self.report()
        with self.assertRaises(Block): load_opening(self.j, "kite", "DEMO", "2026-11-09")

    def test_incomplete_future_stale_snapshot_and_missing_arrays_block(self):
        self.save()
        for change in ({"complete": False}, {"as_of": (NOW + timedelta(seconds=1)).isoformat()}, {"as_of": (NOW - timedelta(seconds=61)).isoformat()}, {"positions": None}, {"orders": None}):
            with self.subTest(change=change), self.assertRaises(Block): reconcile_portfolio(self.j, {**self.snap, **change}, [self.i], NOW)

    def test_corporate_action_and_conversion_differences_are_never_guessed(self):
        for change in ({"inventory_events": [{"type": "split"}]}, {"adjustments_reviewed": False}, {"inventory_events": None}):
            with self.assertRaises(Block): save_opening(self.j, {**self.opening, **change}, [self.i], NOW)
        self.save(); self.snap["inventory_events"] = [{"type": "conversion"}]
        with self.assertRaises(Block): self.report()

    def test_invalid_previous_session_mark_source_or_price_basis_blocks(self):
        for change in ({"mark_session": "2026-11-04"}, {"mark_source": ""}, {"price_basis": "per_lot"}, {"previous_close": 0}, {"average_price": -1}):
            op = copy.deepcopy(self.opening); op["positions"][0].update(change)
            with self.assertRaises(Block): save_opening(self.j, op, [self.i], NOW)
        with self.assertRaises(Block): save_opening(self.j, {**self.opening, "previous_session": "2026-11-06"}, [self.i], NOW)

    def test_missing_stale_or_future_marks_block(self):
        self.save()
        for change in ({"mark_as_of": (NOW - timedelta(seconds=61)).isoformat()}, {"mark_as_of": (NOW + timedelta(seconds=1)).isoformat()}, {"mark_source": ""}, {"mark_price": 0}):
            snap = copy.deepcopy(self.snap); snap["positions"][0].update(change)
            with self.assertRaises(Block): reconcile_portfolio(self.j, snap, [self.i], NOW)

    def test_duplicate_instrument_product_and_fractional_quantity_block(self):
        self.save(); self.snap["positions"] *= 2
        with self.assertRaises(Block): self.report()
        self.snap["positions"] = self.snap["positions"][:1]; self.snap["positions"][0]["quantity"] = 1.5
        with self.assertRaises(Block): self.report()

    def test_stale_expired_cross_broker_or_mismatched_master_blocks(self):
        for change in ({"master_date": "2026-11-05"}, {"broker": "dhan"}, {"token": "other"}):
            with self.assertRaises(Block): save_opening(self.j, self.opening, [{**self.i, **change}], NOW)
        self.derivative(); self.i["expiry"] = "2026-11-05"
        with self.assertRaises(Block): self.save()

    def test_carry_intraday_and_short_delivery_openings_block(self):
        for change in ({"product": "MIS"}, {"quantity": -1}, {"quantity": 0}, {"product": "NRML"}):
            op = copy.deepcopy(self.opening); op["positions"][0].update(change)
            with self.assertRaises(Block): save_opening(self.j, op, [self.i], NOW)

    def test_derivative_lots_are_units_not_inferred_lot_counts(self):
        self.derivative(); self.opening["positions"][0]["quantity"] = 2
        with self.assertRaisesRegex(Block, "lot"): self.save()

    def test_carry_fill_import_is_explicit_and_never_unblocks_intraday_ledger(self):
        f = self.fill(4, "SELL", 115)
        other = Journal(self.w / "other.sqlite")
        try:
            with self.assertRaises(Block): other.import_fills({**self.header, "fills": [f]}, NOW)
        finally: other.close()
        snapshot = {**self.snap, "opening_positions": []}
        reservations = {**self.header, "entries": []}
        with self.assertRaisesRegex(Block, "carry fills"): reconcile(self.j, snapshot, reservations, NOW)

    def test_live_normalization_still_rejects_carry_product(self):
        i, raw = sample("kite")
        raw["orders"][0]["product"] = "CNC"
        with self.assertRaisesRegex(Block, "PRODUCT"): normalize(raw, [i], LIVE_NOW)

    def test_final_empty_fees_are_not_evidence_of_zero_cost(self):
        f = self.fill(4, "SELL", 115)
        f.update(fill_id="empty", fees={})
        with self.assertRaisesRegex(Block, "explicit components"): self.j.import_fills({**self.header, "fills": [f]}, NOW, allow_carry=True)

    def correction(self):
        digest = self.save()["opening_hash"]
        replacement = copy.deepcopy(self.opening); replacement["positions"][0]["average_price"] = 105
        return {**self.header, "schema": "portfolio_opening_correction_v1", "expected_opening_hash": digest,
                "reason": "Synthetic provider corrected acquisition price", "replacement": replacement}

    def test_opening_correction_is_audited_and_requires_fresh_report(self):
        c = self.correction(); before = self.report()
        result = correct_opening(self.j, c, [self.i], NOW)
        self.assertTrue(result["updated"]); self.assertTrue(result["prior_reports_require_reconciliation"])
        after = self.report(); self.assertNotEqual(before["opening_hash"], after["opening_hash"])
        self.assertEqual(after["totals"]["unrealized_gross_inr"], 150)
        self.assertEqual(after["totals"]["daily_gross_inr"], before["totals"]["daily_gross_inr"])
        record = self.j.db.execute("SELECT before_body,after_body,correction_body FROM portfolio_opening_audit").fetchone()
        self.assertEqual(json.loads(record[0])["positions"][0]["average_price"], 100)
        self.assertEqual(json.loads(record[1])["positions"][0]["average_price"], 105)
        self.assertEqual(json.loads(record[2])["reason"], c["reason"])

    def test_correction_reimport_is_idempotent(self):
        c = self.correction(); correct_opening(self.j, c, [self.i], NOW)
        self.assertTrue(correct_opening(self.j, c, [self.i], NOW)["duplicate"])
        self.assertEqual(self.j.db.execute("SELECT COUNT(*) FROM portfolio_opening_audit").fetchone()[0], 1)

    def test_stale_correction_does_not_overwrite_newer_opening(self):
        c = self.correction(); correct_opening(self.j, c, [self.i], NOW)
        c["replacement"]["positions"][0]["average_price"] = 106
        with self.assertRaisesRegex(Block, "stale"): correct_opening(self.j, c, [self.i], NOW)
        self.assertEqual(self.report()["totals"]["unrealized_gross_inr"], 150)
        self.assertEqual(self.j.db.execute("SELECT COUNT(*) FROM portfolio_opening_audit").fetchone()[0], 1)

    def test_correction_requires_reason_account_and_source(self):
        c = self.correction()
        for change in ({"reason": ""}, {"source": ""}, {"account_id": "OTHER"}, {"expected_opening_hash": "bad"}, {"as_of": (NOW + timedelta(seconds=1)).isoformat()}):
            with self.assertRaises(Block): correct_opening(self.j, {**c, **change}, [self.i], NOW)
        self.assertEqual(self.j.db.execute("SELECT COUNT(*) FROM portfolio_opening_audit").fetchone()[0], 0)

    def test_invalid_replacement_is_atomic(self):
        c = self.correction(); c["replacement"]["positions"][0]["quantity"] = -1
        with self.assertRaises(Block): correct_opening(self.j, c, [self.i], NOW)
        self.assertEqual(load_opening(self.j, "kite", "DEMO", "2026-11-06"), self.opening)
        self.assertEqual(self.j.db.execute("SELECT COUNT(*) FROM portfolio_opening_audit").fetchone()[0], 0)

    def test_correction_cli_is_explicit(self):
        c = self.correction()
        (self.w / "correction.json").write_text(json.dumps(c)); (self.w / "master.json").write_text(json.dumps({"instruments": [self.i]}))
        with patch("sys.stdout", new_callable=io.StringIO):
            self.assertEqual(main(["correct-opening", "--db", str(self.w / "journal.sqlite"), "--input", str(self.w / "correction.json"), "--master", str(self.w / "master.json"), "--now", NOW.isoformat()]), 0)
        self.assertEqual(self.report()["totals"]["unrealized_gross_inr"], 150)

    def test_cli_saves_opening_imports_carry_and_reports_without_overwrite(self):
        for name, obj in (("opening", self.opening), ("snapshot", self.snap), ("master", {"instruments": [self.i]})):
            (self.w / (name + ".json")).write_text(json.dumps(obj), encoding="utf-8")
        common = ["--db", str(self.w / "journal.sqlite"), "--master", str(self.w / "master.json"), "--now", NOW.isoformat()]
        with patch("sys.stdout", new_callable=io.StringIO):
            self.assertEqual(main(["opening", "--input", str(self.w / "opening.json"), *common]), 0)
            out = self.w / "report.json"
            args = ["reconcile", "--input", str(self.w / "snapshot.json"), "--out", str(out), *common]
            self.assertEqual(main(args), 0); self.assertEqual(main(args), 2)
            f = self.fill(4, "SELL", 115)
            (self.w / "fills.json").write_text(json.dumps({**self.header, "fills": [f]}))
            self.assertEqual(journal_main(["import", "--db", str(self.w / "import.sqlite"), "--input", str(self.w / "fills.json"), "--portfolio", "--now", NOW.isoformat()]), 0)
        self.assertEqual(json.loads(out.read_text())["status"], "RECONCILED")


if __name__ == "__main__": unittest.main()
