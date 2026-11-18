"""Offline tests for the simple position sizer."""
import io
import json
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
import sys
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
from core import Block
import position_size as ps


class PositionSizeTests(unittest.TestCase):
    def test_loss_at_stop_including_charges_fits_the_budget_and_one_more_does_not(self):
        r = ps.size(entry=1344.40, stop=1340.64, risk=2000)
        self.assertLessEqual(r["loss_at_stop"], 2000)
        card, seg = ps.load_card(ps.DEFAULT_CARD)
        q = r["quantity"] + 1
        over = q * (1344.40 - 1340.64) + ps.charges(1344.40, 1340.64, q, "BUY", "MIS", card, seg)["total"]
        self.assertGreater(over, 2000)
        self.assertLess(r["quantity"], int(2000 // 3.76))  # charges reduce the naive size

    def test_capital_caps_quantity_with_margin_or_full_value(self):
        mis = ps.size(entry=1000, stop=990, risk=10000, capital=50000)
        self.assertEqual(mis["quantity"], 250)  # 50,000 / (1,000 x 20%)
        self.assertTrue(mis["capital_limited"])
        cnc = ps.size(entry=1000, stop=990, risk=10000, capital=50000, product="CNC")
        self.assertEqual(cnc["quantity"], 50)
        self.assertEqual(cnc["capital_needed"], 50000)

    def test_delivery_charges_include_stt_on_both_sides(self):
        card, seg = ps.load_card(ps.DEFAULT_CARD)
        c = ps.charges(1000, 1000, 100, "BUY", "CNC", card, seg)
        self.assertAlmostEqual(c["stt"], 200.0)  # 0.1% on 1 lakh bought and 1 lakh sold
        self.assertAlmostEqual(c["stamp"], 15.0)
        intraday = ps.charges(1000, 1000, 100, "BUY", "MIS", card, seg)
        self.assertAlmostEqual(intraday["stt"], 25.0)  # 0.025% on the sell side only

    def test_lots_round_down(self):
        r = ps.size(entry=200, stop=190, risk=6000, lot=250)
        self.assertEqual(r["quantity"] % 250, 0)

    def test_short_side_and_target(self):
        r = ps.size(entry=500, stop=510, risk=3000, side="SELL", r_multiple=1.5)
        self.assertEqual(r["target"], 485.0)
        self.assertGreater(r["gain_at_target"], 0)

    def test_invalid_inputs_block(self):
        for kwargs in ({"entry": 100, "stop": 105, "risk": 1000}, {"entry": 100, "stop": 95, "risk": 0},
                       {"entry": 100, "stop": 105, "risk": 1000, "side": "SELL", "product": "CNC"},
                       {"entry": 100, "stop": 90, "risk": 5}):
            with self.subTest(kwargs=kwargs), self.assertRaises(Block):
                ps.size(**kwargs)

    def test_cli_markdown_and_json(self):
        out = io.StringIO()
        with redirect_stdout(out):
            self.assertEqual(ps.main(["--entry", "1344.4", "--stop", "1340.64", "--risk", "2000"]), 0)
        self.assertIn("Loss at stop, including charges", out.getvalue())
        out = io.StringIO()
        with redirect_stdout(out):
            ps.main(["--entry", "1344.4", "--stop", "1340.64", "--risk", "2000", "--format", "json"])
        self.assertFalse(json.loads(out.getvalue())["submitted"])
        with redirect_stderr(io.StringIO()):
            self.assertEqual(ps.main(["--entry", "100", "--stop", "105", "--risk", "1000"]), 2)


if __name__ == "__main__":
    unittest.main()
