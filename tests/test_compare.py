"""Offline contracts for the side-by-side comparison (stock_analysis.py --compare) and split-symbol follow-through."""
from contextlib import redirect_stderr, redirect_stdout
import io
import json
from pathlib import Path
import sys
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT / "tests"))
from core import Block
import stock_analysis as sa
from test_screener import Feed
from test_stock_analysis import NOW


def run(symbols, feed=None):
    return sa.compare(symbols, fetcher=feed or Feed(eps={"ALPHA": 20.0, "BETA": 5.0, "GAMMA": 10.0}), clock=lambda: NOW)


class CompareTests(unittest.TestCase):
    def test_one_row_per_stock_with_the_deciding_figures(self):
        r = run(["ALPHA", "BETA", "gamma"])
        self.assertEqual(r["symbols"], ["ALPHA", "BETA", "GAMMA"])
        self.assertEqual([x["symbol"] for x in r["rows"]], ["ALPHA", "BETA", "GAMMA"])
        for row in r["rows"]:
            for key in ("roe", "debt_to_equity", "pe", "pe_band", "revenue_growth", "eps_growth", "quarter_profit_yoy", "return_1y",
                        "dividend_yield_ex_specials"):
                self.assertIn(key, row)
        self.assertFalse(r["execution_eligible"])
        lowest = min(r["rows"], key=lambda x: x["pe"])
        self.assertTrue(any(line.startswith("Value (best first) — P/E today: " + lowest["symbol"]) for line in r["tradeoffs"]))
        self.assertFalse(any(line.startswith("Leads: ") for line in r["tradeoffs"]))  # no tally of wins
        # Identical fake companies: one ranks first everywhere, and the closing line says so without calling it a verdict.
        self.assertRegex(r["tradeoffs"][-1], "No single stock wins|ranks first on every measure shown here; check the cautions")

    def test_markdown_table_and_no_buy_call(self):
        text = sa.compare_markdown(run(["ALPHA", "GAMMA"]))
        # The fixtures are bank-like (no current assets), so P/B and its history are compared too.
        self.assertIn("| Stock | Price | ROE | Op. margin (EBITDA) | Net margin | Debt/eq | P/E | P/E vs own history | P/B | P/B vs own history | Revenue growth | EPS growth | 3-yr CAGR revenue / profit | Latest qtr revenue YoY / QoQ | Latest qtr profit YoY | 1Y return | Div. yield ex-specials |", text)
        self.assertIn("**Trade-offs**", text)
        self.assertIn("days since", text)  # P/E against its own history, said plainly
        for word in ("buy", "Buy", "best pick"):
            self.assertNotIn(word, text)

    def test_usd_reporter_is_labelled(self):
        feed = Feed(eps={"ALPHA": 0.5}, currency={"ALPHA": "USD"})
        text = sa.compare_markdown(run(["ALPHA", "GAMMA"], feed))
        self.assertIn("ALPHA: financials reported in USD", text)

    def test_bad_input_and_failed_stock(self):
        with self.assertRaises(Block):
            run(["ALPHA"])
        r = run(["ALPHA", "NOSUCH"], Feed(down={"NOSUCH"}))
        self.assertTrue(r["rows"][1].get("error"))
        self.assertIn("⚠ NOSUCH:", sa.compare_markdown(r))

    def test_cli_compare(self):
        original = sa.compare
        out = io.StringIO()
        sa.compare = lambda symbols, **k: original(symbols, fetcher=Feed(), clock=lambda: NOW)
        try:
            with redirect_stdout(out):
                self.assertEqual(sa.main(["--compare", "ALPHA", "BETA", "--summary"]), 0)
            with redirect_stdout(io.StringIO()) as raw:
                self.assertEqual(sa.main(["--compare", "ALPHA", "BETA", "--format", "json"]), 0)
        finally:
            sa.compare = original
        self.assertIn("# Comparison — ALPHA, BETA", out.getvalue())
        self.assertEqual(json.loads(raw.getvalue())["schema"], "stock_compare_v1")
        with redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            sa.main([])

    def test_split_symbol_shows_its_successors_side_by_side(self):
        entry = {"old": "SPLITCO", "kind": "demerger", "date": "2025-10-14", "reason": "Split Co demerged its trucks.",
                 "successors": [{"symbol": "ALPHA", "what": "Alpha: cars"}, {"symbol": "BETA", "what": "Beta: trucks"}],
                 "source": "https://example.invalid/splitco"}
        original_analyse, original_compare = sa.analyse, sa.compare

        def analyse(symbol, **k):
            if symbol == "SPLITCO":
                raise sa.SymbolChanged(entry)
            return original_analyse(symbol, **{**k, "fetcher": Feed(), "clock": lambda: NOW})
        sa.analyse = analyse
        sa.compare = lambda symbols, **k: original_compare(symbols, fetcher=Feed(), clock=lambda: NOW)
        out = io.StringIO()
        try:
            with redirect_stdout(out):
                code = sa.main(["SPLITCO", "--summary"])
        finally:
            sa.analyse, sa.compare = original_analyse, original_compare
        self.assertEqual(code, 0)
        text = out.getvalue()
        self.assertTrue(text.startswith("> ⚠ SPLITCO no longer trades"))
        self.assertIn("# Comparison — ALPHA, BETA", text)


class CompareMarginTests(unittest.TestCase):
    def test_margins_are_compared_and_the_pe_window_is_the_real_one(self):
        r = run(["ALPHA", "GAMMA"])
        for row in r["rows"]:
            for key in ("ebitda_margin", "operating_margin", "net_margin", "margin_basis"):
                self.assertIn(key, row)
        text = sa.compare_markdown(r)
        self.assertIn("| Op. margin (EBITDA) | Net margin |", text)
        self.assertIn("with every trading day since May 2023", text)
        self.assertNotIn("five years", text)
        self.assertTrue(any(line.startswith("Quality (best first) — ROE:") and "net margin:" in line for line in r["tradeoffs"]))

    def test_window_note_names_each_start_when_they_differ(self):
        rows = [{"symbol": "A", "pe_band": {"start": "2023-05-02"}}, {"symbol": "B", "pe_band": {"start": "2023-08-01"}}, {"symbol": "C"}]
        note = sa.pe_window_note(rows)
        self.assertIn("since May 2023 (A); Aug 2023 (B)", note)
        self.assertIn("not available", sa.pe_window_note([{"symbol": "C"}]))


if __name__ == "__main__":
    unittest.main()
