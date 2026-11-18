"""Offline contracts for renamed/demerged symbols, demerger history cuts, retries and dividend yield; never contacts the network."""
from datetime import date, datetime
import http.client
import io
import json
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
import re
import sys
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT / "tests"))
from core import Block, IST
import stock_analysis as sa
from test_stock_analysis import NOW, sessions, fundamentals_payload, GOOGLE, EMPTY_RSS

DAYS = sessions(520)  # weekdays ending 2026-11-13


def stamp(day):
    return int(datetime.combine(day, datetime.min.time(), IST).replace(hour=9, minute=15).timestamp())


def chart(selected, closes, *, days=None, name="Example Ltd", dividends=(), splits=()):
    days = days or DAYS[-len(closes):]
    result = {"meta": {"symbol": selected, "currency": "INR", "exchangeTimezoneName": "Asia/Kolkata", "dataGranularity": "1d", "longName": name},
              "timestamp": [int(d.timestamp()) for d in days],
              "indicators": {"quote": [{"open": closes, "high": [c * 1.01 for c in closes], "low": [c * 0.99 for c in closes],
                                         "close": closes, "volume": [1000] * len(closes)}]}}
    events = {}
    if dividends:
        events["dividends"] = {str(stamp(d)): {"amount": a, "date": stamp(d)} for d, a in dividends}
    if splits:
        events["splits"] = {str(stamp(d)): {"date": stamp(d), "numerator": n, "denominator": m} for d, n, m in splits}
    if events:
        result["events"] = events
    return json.dumps({"chart": {"error": None, "result": [result]}}).encode()


def rising(n, start=100.0, step=1.001):
    return [start * step ** k for k in range(n)]


class Feed:
    """Charts keyed by bare symbol; anything not listed fails like the live feed does for a retired symbol."""

    def __init__(self, charts, fundamentals=None):
        self.charts = charts
        self.fundamentals = fundamentals or fundamentals_payload()
        self.urls = []

    def __call__(self, url):
        self.urls.append(url)
        if "news.google.com" in url:
            return GOOGLE
        if "/v8/finance/chart/" in url:
            sym = url.split("/chart/")[1].split("?")[0].replace("%5E", "^").replace("%26", "&")
            if sym == "^NSEI":
                return chart("^NSEI", rising(520, step=1.0005), name="NIFTY 50")
            base = sym[:-3]
            if base not in self.charts:
                raise Block("PUBLIC: source unavailable; no login, bypass or automatic retry")
            return self.charts[base](sym)
        if "fundamentals-timeseries" in url:
            return self.fundamentals
        if "/indices/" in url:
            raise Block("PUBLIC: source unavailable")
        return EMPTY_RSS


def run(symbol, feed, **kw):
    return sa.analyse(symbol, fetcher=feed, news_fetcher=feed, clock=lambda: NOW, peers=False, **kw)


ALIASES = {
    "OLDCO": {"old": "OLDCO", "kind": "rename", "date": "2025-04-09", "reason": "Old Co renamed New Co.",
              "successors": [{"symbol": "NEWCO", "what": "New Co Ltd"}], "source": "https://example.invalid/oldco"},
    "SPLITCO": {"old": "SPLITCO", "kind": "demerger", "date": "2025-10-14", "reason": "Split Co demerged its trucks.",
                "successors": [{"symbol": "CARS", "what": "Cars Ltd: passenger cars"}, {"symbol": "TRUCKS", "what": "Trucks Ltd: trucks"}],
                "source": "https://example.invalid/splitco"},
}


class AliasFileTests(unittest.TestCase):
    def test_alias_file_is_complete_and_sourced(self):
        data = json.loads((ROOT / "assets" / "symbol_aliases.json").read_text(encoding="utf-8"))
        self.assertEqual(data["schema"], "symbol_aliases_v1")
        olds = [e["old"] for e in data["aliases"]]
        self.assertEqual(len(olds), len(set(olds)))
        for e in data["aliases"]:
            with self.subTest(old=e["old"]):
                date.fromisoformat(e["date"])
                self.assertTrue(e["reason"].strip())
                self.assertTrue(e["source"].startswith("https://"))
                self.assertTrue(e["successors"])
                for s in e["successors"]:
                    self.assertEqual(sa.normalize(s["symbol"]), s["symbol"] + ".NS")
                    self.assertTrue(s["what"].strip())
                    self.assertNotIn(s["symbol"], olds, "point straight at the current symbol, not at another old one")

    def test_tata_motors_maps_to_both_demerged_companies(self):
        entry = sa.load_aliases()["TATAMOTORS"]
        self.assertEqual([s["symbol"] for s in entry["successors"]], ["TMPV", "TMCV"])
        self.assertEqual(sa.alias_for("TATAMOTORS.NS")["old"], "TATAMOTORS")
        self.assertIsNone(sa.alias_for("TATAMOTORS.BO"))  # BSE codes are not mapped

    def test_bad_alias_file_degrades_to_no_aliases(self):
        self.assertEqual(sa.load_aliases(ROOT / "tests" / "no-such-file.json"), {})


class RenamedSymbolTests(unittest.TestCase):
    def setUp(self):
        self.patch = sa.load_aliases
        sa.load_aliases = lambda path=None: ALIASES

    def tearDown(self):
        sa.load_aliases = self.patch

    def test_single_successor_is_analysed_and_named(self):
        feed = Feed({"NEWCO": lambda s: chart(s, rising(520), name="New Co Ltd")})
        r = run("oldco", feed)
        self.assertEqual(r["symbol"], "NEWCO.NS")
        self.assertEqual(r["requested_symbol"], "OLDCO.NS")
        self.assertIn("OLDCO no longer trades", r["renamed_note"])
        self.assertIn("NEWCO (New Co Ltd)", r["renamed_note"])
        self.assertIn("> ⚠ OLDCO no longer trades", sa.markdown(r))
        self.assertIn("OLDCO no longer trades", sa.summary(r))

    def test_split_symbol_blocks_with_both_successors(self):
        feed = Feed({"CARS": lambda s: chart(s, rising(520)), "TRUCKS": lambda s: chart(s, rising(520))})
        with self.assertRaises(sa.SymbolChanged) as caught:
            run("SPLITCO", feed)
        error = caught.exception
        self.assertEqual([s["symbol"] for s in error.successors], ["CARS", "TRUCKS"])
        for part in ("SPLITCO no longer trades", "CARS: Cars Ltd: passenger cars", "TRUCKS: Trucks Ltd: trucks", "https://example.invalid/splitco"):
            self.assertIn(part, str(error))
        self.assertIsInstance(error, Block)

    def test_symbol_that_still_trades_is_not_redirected(self):
        # NSE can reuse a retired symbol; a live, current history wins over the alias.
        feed = Feed({"OLDCO": lambda s: chart(s, rising(520), name="Reused Ltd"), "NEWCO": lambda s: chart(s, rising(520))})
        r = run("OLDCO", feed)
        self.assertEqual(r["symbol"], "OLDCO.NS")
        self.assertIsNone(r["renamed_note"])

    def test_history_that_stopped_is_redirected(self):
        stale = DAYS[:400]  # last bar months before NOW
        feed = Feed({"OLDCO": lambda s: chart(s, rising(400), days=stale), "NEWCO": lambda s: chart(s, rising(520), name="New Co Ltd")})
        self.assertEqual(run("OLDCO", feed)["symbol"], "NEWCO.NS")

    def test_unknown_symbol_error_has_current_examples(self):
        with self.assertRaises(Block) as caught:
            run("NOSUCH", Feed({}))
        self.assertIn("no price history for NOSUCH.NS", str(caught.exception))
        self.assertNotIn("TATAMOTORS", str(caught.exception))

    def test_cli_json_lists_successors(self):
        out, err = io.StringIO(), io.StringIO()
        original = sa.analyse
        sa.analyse = lambda *a, **k: (_ for _ in ()).throw(sa.SymbolChanged(ALIASES["SPLITCO"]))
        try:
            with redirect_stdout(out), redirect_stderr(err):
                code = sa.main(["SPLITCO", "--format", "json"])
        finally:
            sa.analyse = original
        self.assertEqual(code, 2)
        payload = json.loads(out.getvalue())
        self.assertEqual(payload["status"], "SYMBOL_CHANGED")
        self.assertEqual([s["symbol"] for s in payload["successors"]], ["CARS", "TRUCKS"])
        self.assertIn("BLOCKED", err.getvalue())


def demerged_closes(record=date(2025, 10, 14), before=660.0, after=395.0):
    return [before * 1.0005 ** k if d.date() < record else after * 1.0003 ** k for k, d in enumerate(DAYS)]


DEMERGERS = [{"parent": "TMPV", "parent_aliases": ["TATAMOTORS"], "resulting_symbol": "TMCV",
              "resulting_company": "Tata Motors Limited (formerly TML Commercial Vehicles Limited)", "record_date": "2025-10-14"}]


class DemergerHistoryTests(unittest.TestCase):
    def setUp(self):
        self.patch = sa.load_demergers
        sa.load_demergers = lambda path=None: DEMERGERS

    def tearDown(self):
        sa.load_demergers = self.patch

    def test_history_is_cut_at_the_record_date(self):
        closes = demerged_closes()
        history = {"bars": [{"at": d.isoformat(), "close": c, "high": c, "low": c} for d, c in zip(DAYS, closes)]}
        cut = sa.trim_for_demerger(history, "TMPV.NS")
        self.assertEqual(cut["record_date"], "2025-10-14")
        self.assertEqual(history["bars"][0]["at"][:10], "2025-10-14")
        self.assertLess(cut["day_move"], -0.35)
        self.assertIn("History starts 2025-10-14", cut["note"])
        # former symbol counts too; unrelated symbols and events outside the history are left alone
        history = {"bars": [{"at": d.isoformat(), "close": c} for d, c in zip(DAYS, closes)]}
        self.assertIsNotNone(sa.trim_for_demerger(history, "TATAMOTORS.NS"))
        history = {"bars": [{"at": d.isoformat(), "close": c} for d, c in zip(DAYS, closes)]}
        self.assertIsNone(sa.trim_for_demerger(history, "TMCV.NS"))
        self.assertEqual(len(history["bars"]), len(DAYS))

    def test_report_uses_only_post_demerger_sessions(self):
        feed = Feed({"TMPV": lambda s: chart(s, demerged_closes(), name="Tata Motors Passenger Vehicles Limited")})
        r = run("TMPV", feed)
        t, o = r["technical"], r["outlook"]
        self.assertEqual(t["history_start"], "2025-10-14")
        self.assertLess(t["high_52w"], 500)  # the ₹660 pre-demerger prices are gone
        self.assertGreater(t["returns"]["1Y"], -0.1)  # not the -40% demerger jump
        self.assertLess(o["history_years"], 1.2)
        self.assertIn("after the demerger", o["method"])
        self.assertNotIn("~2 years", o["method"])
        text = sa.markdown(r)
        self.assertIn("> ⚠ History starts 2025-10-14", text)
        self.assertIn("first session on or after the 2025-10-14 record date", sa.summary(r))
        self.assertIn("include the demerged business", text)

    def test_short_post_demerger_history_blocks_with_reason(self):
        record = DAYS[-30].date()
        feed = Feed({"TMPV": lambda s: chart(s, demerged_closes(record))})
        sa.load_demergers = lambda path=None: [{**DEMERGERS[0], "record_date": record.isoformat()}]
        with self.assertRaisesRegex(Block, "only 30 sessions since the .* demerger"):
            run("TMPV", feed)

    def test_unexplained_single_day_fall_is_warned(self):
        sa.load_demergers = lambda path=None: []
        feed = Feed({"TMPV": lambda s: chart(s, demerged_closes())})
        r = run("TMPV", feed)
        self.assertEqual(len(r["history_warnings"]), 1)
        self.assertIn("on 2025-10-14 with no split recorded", r["history_warnings"][0])
        bars = [{"at": d.isoformat(), "close": c} for d, c in zip(DAYS, demerged_closes())]
        self.assertEqual(sa.unexplained_gaps(bars, {"2025-10-14"}), [])


class RetryTests(unittest.TestCase):
    def test_transient_failures_are_retried_then_succeed(self):
        calls, waits = [], []

        def flaky(url):
            calls.append(url)
            if len(calls) < 3:
                raise Block("PUBLIC: source unavailable; no login, bypass or automatic retry")
            return b"ok"
        self.assertEqual(sa.retrying(flaky, sleep=waits.append)("u"), b"ok")
        self.assertEqual(len(calls), 3)
        self.assertEqual(waits, [0.5, 1.5])

    def test_retries_are_bounded_and_permanent_errors_are_not_retried(self):
        calls = []

        def down(url):
            calls.append(url)
            raise Block("PUBLIC: source unavailable")
        with self.assertRaises(Block):
            sa.retrying(down, attempts=3, sleep=lambda _: None)("u")
        self.assertEqual(len(calls), 3)
        calls.clear()

        def refused(url):
            calls.append(url)
            raise Block("PUBLIC: unsupported source")
        with self.assertRaises(Block):
            sa.retrying(refused, sleep=lambda _: None)("u")
        self.assertEqual(len(calls), 1)

    def test_dropped_connection_becomes_a_block(self):
        def cut_off(url):
            raise http.client.IncompleteRead(b"")
        with self.assertRaisesRegex(Block, "source unavailable"):
            sa.retrying(cut_off, sleep=lambda _: None)("u")


def history_with_dividends(payments, closes=None, splits=()):
    days = DAYS
    raw = chart("DIV.NS", closes or [1000.0] * len(days), days=days, dividends=payments, splits=splits)
    return sa.daily_history("DIV.NS", fetcher=lambda url: raw, now=NOW)


class DividendTests(unittest.TestCase):
    def test_trailing_yield_uses_last_twelve_months_per_share(self):
        h = history_with_dividends([(date(2025, 6, 2), 50.0), (date(2026, 1, 15), 10.0), (date(2026, 6, 1), 20.0)])
        d = sa.trailing_dividends(h, 1000.0)
        self.assertAlmostEqual(d["dps_ttm"], 30.0)
        self.assertAlmostEqual(d["dividend_yield"], 0.03)
        self.assertEqual(d["flags"], [])

    def test_quarterly_payer_special_is_flagged(self):
        # TCS-like: ₹11-12 interims, ₹30 finals, ₹76 and ₹57 interim-plus-special each January
        tcs = [(date(2025, 1, 17), 76.0), (date(2025, 6, 4), 30.0), (date(2025, 7, 16), 11.0), (date(2025, 10, 15), 11.0),
               (date(2026, 1, 16), 57.0), (date(2026, 5, 25), 31.0), (date(2026, 7, 15), 12.0), (date(2026, 10, 14), 12.0)]
        d = sa.trailing_dividends(history_with_dividends(tcs), 2400.0)
        self.assertEqual([s["date"] for s in d["specials"]], ["2026-01-16"])
        self.assertAlmostEqual(d["dividend_yield"], 112 / 2400)
        self.assertAlmostEqual(d["recurring_yield"], 55 / 2400)
        self.assertIn("Possible special dividend", d["flags"][0])

    def test_regular_large_final_is_not_special(self):
        paints = [(date(2024, 11, 19), 4.25), (date(2025, 6, 10), 20.55), (date(2025, 11, 18), 4.5), (date(2026, 6, 23), 23.0), (date(2026, 11, 4), 5.75)]
        self.assertEqual(sa.trailing_dividends(history_with_dividends(paints), 2500.0)["specials"], [])
        mrf = [(date(2024, 11, 20), 3.0), (date(2025, 2, 14), 3.0), (date(2025, 7, 18), 229.0), (date(2025, 11, 20), 3.0),
               (date(2026, 2, 13), 3.0), (date(2026, 7, 17), 229.0), (date(2026, 11, 12), 3.0)]
        self.assertEqual(sa.trailing_dividends(history_with_dividends(mrf), 150000.0)["specials"], [])
        one_off = [(date(2025, 5, 2), 33.5), (date(2025, 8, 8), 9.77), (date(2026, 4, 30), 29.59), (date(2026, 8, 7), 90.0)]
        self.assertEqual([s["amount"] for s in sa.trailing_dividends(history_with_dividends(one_off), 6000.0)["specials"]], [90.0])

    def test_no_dividends_is_zero_with_a_full_year_and_unknown_without(self):
        self.assertEqual(sa.trailing_dividends(history_with_dividends([]), 1000.0)["dividend_yield"], 0.0)
        raw = chart("DIV.NS", [1000.0] * 100, days=DAYS[-100:])
        self.assertIsNone(sa.trailing_dividends(sa.daily_history("DIV.NS", fetcher=lambda url: raw, now=NOW), 1000.0))

    def test_report_shows_both_dividend_bases(self):
        feed = Feed({"EXAMPLE": lambda s: chart(s, rising(520), dividends=[(date(2026, 6, 1), 2.0)])})
        r = run("EXAMPLE", feed)
        f = r["fundamentals"]
        self.assertAlmostEqual(f["dividend_yield"], 2.0 / r["technical"]["last_close"])
        self.assertIsNotNone(f["cash_dividend_yield"])
        text = sa.markdown(r)
        self.assertIn("| Dividend yield (trailing 12 months per share ÷ price) |", text)
        self.assertIn("| Cash dividends paid (last FY) / market cap |", text)


if __name__ == "__main__":
    unittest.main()
