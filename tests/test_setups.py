"""Deterministic rule fixtures, not market data or strategy performance evidence."""
import contextlib
import copy
from datetime import timedelta
import io
import json
from pathlib import Path
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
from core import (Block, instant, orb, vwap_reclaim, setup_hit, plan_from_hit, plan_math,
                  validate_config, fingerprint, size, ticket)
from demo import inputs
from replay import replay
from trader import main, scan, write

KEY = "NSE:DEMO-EQ"


class SetupTests(unittest.TestCase):
    def setUp(self):
        self.c = json.loads((ROOT / "assets/config.example.json").read_text())
        self.c["setup"] = "vwap_reclaim"
        self.card = json.loads((ROOT / "assets/charges.example.json").read_text())
        self.now, self.i, self.ctx, self.h, self.u, self.candles = inputs(self.c)
        self.rows = self.candles["instruments"][KEY]["bars"]

    def hit(self):
        return setup_hit(self.rows, self.now, self.c)

    def plan(self):
        return plan_from_hit(self.hit(), self.i, self.c, self.now)

    def run_cli(self, args):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = main(args)
        return code, json.loads(out.getvalue())

    def test_vwap_crossover_hand_calculated(self):
        hit = self.hit()
        # HLC3 values: 302/3, 298/3, 297/3, 303.5/3. Equal volumes.
        self.assertAlmostEqual(hit["vwap"], 1200.5 / 12)
        self.assertEqual((hit["state"], hit["side"], hit["entry"], hit["trigger_at"]),
                         ("triggered", "BUY", 102, self.now.isoformat()))
        self.assertEqual(hit["turnover_estimate_inr"], 40100000)
        self.assertEqual(hit["rule_version"], 1)
        self.assertEqual(orb(self.rows, self.now)["state"], "waiting")

    def test_vwap_uses_volume_weight_not_simple_mean(self):
        self.rows[0]["volume"] *= 4
        hit = self.hit()
        expected = ((302 / 3) * 4 + 298 / 3 + 297 / 3 + 303.5 / 3) / 7
        self.assertAlmostEqual(hit["vwap"], expected)
        self.assertNotAlmostEqual(hit["vwap"], 1200.5 / 12)

    def test_mirrored_downward_cross(self):
        for b in self.rows:
            b.update(open=200-b["open"], high=200-b["low"], low=200-b["high"], close=200-b["close"])
        hit = self.hit()
        self.assertEqual(hit["side"], "SELL")
        self.assertLess(hit["percent_over_level"], 0)
        p = plan_from_hit(hit, self.i, self.c, self.now)
        self.assertGreater(p["stop"], p["entry"])
        self.assertLess(p["target"], p["entry"])

    def test_forming_bar_never_triggers(self):
        self.assertEqual(vwap_reclaim(self.rows, self.now-timedelta(seconds=1))["state"], "waiting")

    def test_equal_previous_close_is_not_strict_crossover(self):
        for b in self.rows[:3]:
            b.update(open=100, high=100, low=100, close=100)
        self.assertEqual(self.hit()["state"], "waiting")

    def test_session_resets_and_excludes_other_days(self):
        previous = [{**b, "at": (instant(b["at"])-timedelta(days=1)).isoformat(), "volume": 999999999} for b in self.rows]
        self.assertEqual(vwap_reclaim(previous+self.rows, self.now), self.hit())

    def test_unsorted_complete_input_has_same_result(self):
        self.assertEqual(vwap_reclaim(list(reversed(self.rows)), self.now), self.hit())

    def test_zero_volume_before_trigger_blocks(self):
        for index in (0, 2, 3):
            rows = copy.deepcopy(self.rows)
            rows[index]["volume"] = 0
            with self.assertRaisesRegex(Block, "volume"):
                vwap_reclaim(rows, self.now)

    def test_missing_open_intervening_and_latest_block_both_setups(self):
        for detector in (orb, vwap_reclaim):
            for index in (0, 1, 3):
                with self.subTest(detector=detector.__name__, index=index), self.assertRaisesRegex(Block, "missing"):
                    detector([b for k, b in enumerate(self.rows) if k != index], self.now)
            with self.assertRaisesRegex(Block, "missing"):
                detector(self.rows, self.now+timedelta(minutes=5))

    def test_duplicate_or_bad_ohlc_blocks(self):
        with self.assertRaisesRegex(Block, "duplicate"):
            vwap_reclaim(self.rows+[self.rows[0]], self.now)
        self.rows[0]["high"] = 90
        with self.assertRaisesRegex(Block, "OHLC"):
            self.hit()

    def test_no_premarket_or_opening_bar_signal(self):
        self.assertEqual(vwap_reclaim([], self.now.replace(hour=9, minute=10))["state"], "waiting")
        # A cross on the 09:20 bar is before the earliest eligible close of 09:30.
        self.rows[0].update(open=100, high=102, low=98, close=99)
        self.rows[1].update(open=99, high=103, low=99, close=102)
        self.assertEqual(vwap_reclaim(self.rows[:2], self.now.replace(minute=25))["state"], "waiting")

    def test_configured_entry_window_controls_crossover(self):
        self.c["no_entries_before"] = "09:40"
        self.assertEqual(self.hit()["state"], "waiting")
        self.c["no_entries_before"] = "09:30"
        self.c["no_entries_after"] = "09:30"
        self.assertEqual(self.hit()["state"], "waiting")

    def test_signal_evidence_does_not_use_later_bars(self):
        first = self.hit()
        later = {**self.rows[-1], "at": self.now.isoformat(), "open": 102, "high": 110, "low": 101, "close": 109, "volume": 999999999}
        self.assertEqual(vwap_reclaim(self.rows+[later], self.now+timedelta(minutes=5)), first)
        _, _, _, _, _, candles = inputs({**self.c, "setup": "orb"})
        orb_rows = candles["instruments"][KEY]["bars"]
        self.assertEqual(orb(orb_rows, self.now), orb(orb_rows+[later], self.now+timedelta(minutes=5)))

    def test_structural_stop_uses_vwap_and_tick_buffer(self):
        p = self.plan()
        self.assertEqual((p["entry"], p["stop"], p["target"]), (102, 99.9, 104.1))
        self.assertAlmostEqual(p["stop_reference"], self.hit()["vwap"])
        self.assertEqual(p["setup"], "vwap_reclaim")
        self.assertEqual(p["rule_version"], 1)

    def test_malformed_structural_reference_or_side_blocks(self):
        for changes in ({"vwap": 103}, {"vwap": None}, {"side": "HOLD"}, {"rule_version": 99}, {"rule_version": True}):
            with self.assertRaises(Block):
                plan_from_hit({**self.hit(), **changes}, self.i, self.c, self.now)

    def test_unknown_setup_and_mismatched_plan_block(self):
        for setup in ("unsupported", "unknown"):
            with self.assertRaisesRegex(Block, "SETUP"):
                validate_config({**self.c, "setup": setup})
            with self.assertRaisesRegex(Block, "SETUP"):
                plan_math({**self.plan(), "setup": setup}, self.i)
        with self.assertRaisesRegex(Block, "SETUP"):
            plan_from_hit(self.hit(), self.i, {**self.c, "setup": "orb"}, self.now)
        with self.assertRaisesRegex(Block, "SETUP"):
            replay(self.plan(), self.i, self.h, {**self.c, "setup": "orb"}, self.card)

    def test_legacy_orb_config_default_and_plan_compatibility(self):
        del self.c["setup"]
        _, i, _, _, _, candles = inputs(self.c)
        hit = setup_hit(candles["instruments"][KEY]["bars"], self.now, self.c)
        p = plan_from_hit(hit, i, self.c, self.now)
        del p["rule_version"]
        plan_math(p, i)
        self.assertEqual(p["setup"], "orb")
        with self.assertRaisesRegex(Block, "SETUP"):
            plan_math({**p, "setup": "vwap_reclaim"}, i)

    def test_scan_labels_setup_and_excludes_fresh_snapshot_missing_latest(self):
        result = scan(self.u, self.candles, self.ctx, self.c, self.now)
        self.assertEqual((result["setup"], result["processed"], len(result["hits"])), ("vwap_reclaim", 1, 1))
        self.rows.pop()
        result = scan(self.u, self.candles, self.ctx, self.c, self.now)
        self.assertEqual((result["processed"], result["excluded_count"]), (0, 1))
        self.assertIn("missing", result["excluded"][0]["reason"])

    def test_historical_replay_counts_vwap_not_orb(self):
        # Every historical day has the same VWAP cross, but never closes outside its ORB.
        for index, b in enumerate(self.h["bars"]):
            slot = index % 70
            if slot < 4:
                b.update({k: self.rows[slot][k] for k in ("open", "high", "low", "close", "volume")})
            else:
                b.update(open=102, high=102.1, low=101.9, close=102, volume=100000)
        c = {**self.c, "replay_paths": 100}
        p = self.plan()
        result = replay(p, self.i, self.h, c, self.card)
        self.assertEqual(result["historical_setup"], "vwap_reclaim")
        self.assertEqual(result["historical_same_side_firings"], 25)
        self.assertEqual(result["historical_outcomes"], {"still_open": 25})
        self.assertNotIn("historical_same_side_orb_firings", result)
        self.assertEqual(result, replay(p, self.i, self.h, c, self.card))
        self.h["bars"] += self.rows
        no_lookahead = replay(p, self.i, self.h, c, self.card)
        self.assertEqual(no_lookahead["historical_same_side_firings"], 25)
        self.assertEqual(no_lookahead["target_first_pct"], result["target_first_pct"])

    def test_config_switch_invalidates_sizing_and_old_replay(self):
        p = self.plan()
        c = {**self.c, "replay_paths": 100}
        self.ctx["replay"] = replay(p, self.i, self.h, c, self.card)
        order = size(p, self.i, c, self.ctx, self.card, self.now)
        self.assertFalse(ticket(order, self.i, c, self.ctx, self.card, self.now)["submitted"])
        with self.assertRaisesRegex(Block, "SETUP"):
            size(p, self.i, {**c, "setup": "orb"}, self.ctx, self.card, self.now)
        self.ctx["replay"]["plan_hash"] = "old-orb-evidence"
        with self.assertRaisesRegex(Block, "REPLAY"):
            size(p, self.i, c, self.ctx, self.card, self.now)

    def test_cli_entry_plan_and_expiration_use_selected_setup(self):
        with tempfile.TemporaryDirectory() as d:
            w = Path(d)
            for name, data in (("config", self.c), ("charges", self.card), ("context", self.ctx), ("instrument", self.i), ("candles", self.candles)):
                write(w/(name+".json"), data)
            for command in ("entry", "plan"):
                code, result = self.run_cli([command, "--workdir", d, "--now", self.now.isoformat()])
                self.assertEqual(code, 0)
                self.assertEqual(result["setup"], "vwap_reclaim")
            for minutes in (0, 5):
                self.rows.append({**self.rows[-1], "at": (self.now+timedelta(minutes=minutes)).isoformat()})
            write(w/"candles.json", self.candles)
            later = self.now+timedelta(minutes=10)
            self.candles["instruments"][KEY]["as_of"] = later.isoformat()
            write(w/"candles.json", self.candles)
            code, result = self.run_cli(["entry", "--workdir", d, "--now", later.isoformat()])
            self.assertEqual((code, result["state"]), (0, "expired"))

    def test_all_five_vwap_demos_reach_paper_ticket(self):
        for broker in ("kite", "upstox", "dhan", "angel", "fyers"):
            with self.subTest(broker=broker), tempfile.TemporaryDirectory() as d:
                code, result = self.run_cli(["demo", "--broker", broker, "--setup", "vwap_reclaim", "--workdir", d])
                self.assertEqual(code, 0, result)
                self.assertEqual(result["risk"], "PASS")
                p = json.loads((Path(d)/"plan.json").read_text())
                rep = json.loads((Path(d)/"replay.json").read_text())
                self.assertEqual(p["setup"], "vwap_reclaim")
                self.assertEqual(rep["plan_hash"], fingerprint(p))
                self.assertEqual(rep["paths"], 10000)
                self.assertFalse(json.loads((Path(d)/"ticket.json").read_text())["submitted"])

    def test_cli_setup_override_rejected_for_existing_analysis(self):
        with tempfile.TemporaryDirectory() as d:
            code, result = self.run_cli(["scan", "--setup", "vwap_reclaim", "--workdir", d])
            self.assertEqual(code, 2)
            self.assertIn("CONFIG", result["reason"])
            self.assertEqual(list(Path(d).iterdir()), [])


if __name__ == "__main__":
    unittest.main()
