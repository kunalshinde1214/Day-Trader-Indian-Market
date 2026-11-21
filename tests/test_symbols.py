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
    """Demerged parents keep their whole history: unadjusted prices are scaled, never dropped (5-year P/E band, 52-week range)."""

    def setUp(self):
        self.patch = sa.load_demergers
        sa.load_demergers = lambda path=None: DEMERGERS

    def tearDown(self):
        sa.load_demergers = self.patch

    def bars(self, closes):
        return {"bars": [{"at": d.isoformat(), "close": c, "high": c, "low": c, "open": c} for d, c in zip(DAYS, closes)]}

    def test_unadjusted_large_demerger_is_scaled_by_the_days_price_ratio(self):
        history = self.bars(demerged_closes())
        cut = sa.trim_for_demerger(history, "TMPV.NS")
        self.assertEqual((cut["record_date"], cut["mode"], cut["minor"]), ("2025-10-14", "scaled", False))
        self.assertEqual(history["bars"][0]["at"][:10], DAYS[0].date().isoformat())  # nothing dropped
        k = next(i for i, d in enumerate(DAYS) if d.date() >= date(2025, 10, 14))
        self.assertAlmostEqual(history["bars"][k - 1]["close"], history["bars"][k]["close"], places=6)  # no jump left
        self.assertLess(cut["day_move"], -0.35)
        self.assertIn("scaled by 0.57", cut["note"])  # 395 / 1.0003^k against 660 / 1.0005^k in the fixture
        self.assertIn("that day's own price change", cut["note"])
        # former symbol counts too; unrelated symbols are left alone
        self.assertIsNotNone(sa.trim_for_demerger(self.bars(demerged_closes()), "TATAMOTORS.NS"))
        history = self.bars(demerged_closes())
        self.assertIsNone(sa.trim_for_demerger(history, "TMCV.NS"))
        self.assertEqual(history["bars"][0]["close"], 660.0)

    def test_history_yahoo_already_adjusted_is_left_alone(self):
        # Reliance 2023: Yahoo's series has no jump on the record date, so scaling again would double-count it.
        sa.load_demergers = lambda path=None: [{**DEMERGERS[0], "cost_split_pct": {"parent": 95.32, "resulting": 4.68}}]
        history = self.bars(rising(len(DAYS)))
        before = [b["close"] for b in history["bars"]]
        cut = sa.trim_for_demerger(history, "TMPV.NS")
        self.assertEqual((cut["mode"], cut["factor"]), ("provider", 1.0))
        self.assertEqual([b["close"] for b in history["bars"]], before)
        self.assertIn("already reflect it", cut["note"])

    def test_small_demerger_with_a_jump_uses_the_published_cost_split(self):
        sa.load_demergers = lambda path=None: [{**DEMERGERS[0], "cost_split_pct": {"parent": 98.09, "resulting": 1.91}}]
        history = self.bars(demerged_closes(before=1000.0, after=950.0))
        cut = sa.trim_for_demerger(history, "TMPV.NS")
        self.assertEqual((cut["mode"], cut["minor"]), ("scaled", True))
        self.assertAlmostEqual(cut["factor"], 0.9809)
        self.assertAlmostEqual(history["bars"][0]["close"], 980.9)

    def test_report_keeps_full_history_without_the_demerger_jump(self):
        feed = Feed({"TMPV": lambda s: chart(s, demerged_closes(), name="Tata Motors Passenger Vehicles Limited")})
        r = run("TMPV", feed)
        t, o = r["technical"], r["outlook"]
        self.assertEqual(t["history_start"], DAYS[0].date().isoformat())
        self.assertLess(t["high_52w"], 500)  # pre-demerger ₹660 prices are scaled to the post-demerger level
        self.assertGreater(t["returns"]["1Y"], -0.1)  # not the -40% demerger jump
        self.assertIn("scaled by", o["method"])
        self.assertIn("Yahoo does not adjust prices before the 2025-10-14 demerger", sa.markdown(r))
        self.assertIn("Yahoo does not adjust prices before the 2025-10-14 demerger", sa.summary(r))
        self.assertEqual(r["history_warnings"], [])  # the demerger day is explained, not an unexplained gap
        recent = DAYS[-60].date()  # a demerger within the last year is a headline caution, at the top
        sa.load_demergers = lambda path=None: [{**DEMERGERS[0], "record_date": recent.isoformat()}]
        r = run("TMPV", Feed({"TMPV": lambda s: chart(s, demerged_closes(recent))}))
        self.assertFalse(r["history_note_old"])
        self.assertIn(f"> ⚠ Yahoo does not adjust prices before the {recent.isoformat()} demerger", sa.markdown(r))
        self.assertTrue(sa.summary(r).splitlines()[1].startswith("⚠ Yahoo does not adjust"))

    def test_old_demerger_note_moves_below_the_answer(self):
        record = DAYS[100].date()
        feed = Feed({"TMPV": lambda s: chart(s, demerged_closes(record))})
        sa.load_demergers = lambda path=None: [{**DEMERGERS[0], "record_date": record.isoformat()}]
        r = run("TMPV", feed)
        self.assertTrue(r["history_note_old"])
        lines = sa.summary(r).splitlines()
        note = next(i for i, x in enumerate(lines) if "Yahoo does not adjust" in x)
        self.assertGreater(note, next(i for i, x in enumerate(lines) if x.startswith("Valuation")))
        self.assertTrue(lines[note].startswith("Note: "))
        self.assertEqual(lines[-1], "Research only; not investment advice.")
        self.assertIn("## Notes", sa.markdown(r))

    def test_unexplained_single_day_fall_is_warned(self):
        sa.load_demergers = lambda path=None: []
        feed = Feed({"TMPV": lambda s: chart(s, demerged_closes())})
        r = run("TMPV", feed)
        self.assertEqual(len(r["history_warnings"]), 1)
        self.assertIn("on 2025-10-14 with no split recorded", r["history_warnings"][0])
        bars = [{"at": d.isoformat(), "close": c} for d, c in zip(DAYS, demerged_closes())]
        self.assertEqual(sa.unexplained_gaps(bars, {"2025-10-14"}), [])


class CorporateActionGapTests(unittest.TestCase):
    def bars(self, day, move):
        closes = [100.0 if d.date() < day else 100.0 * (1 + move) for d in DAYS]
        return [{"at": d.isoformat(), "close": c} for d, c in zip(DAYS, closes)]

    def test_fall_on_an_nse_ex_date_is_flagged_even_below_35_percent(self):
        day = date(2026, 6, 15)
        bars = self.bars(day, -0.08)
        self.assertEqual(sa.unexplained_gaps(bars), [])  # the old 35% rule misses it
        out = sa.action_gaps(bars, [("2026-06-15", "Rights 1:5 @ Premium Rs 80/-")])
        self.assertEqual(len(out), 1)
        self.assertIn("Close fell -8.0% on 2026-06-15, the ex-date of 'Rights 1:5", out[0])
        self.assertIn("not like-for-like", out[0])
        self.assertEqual(sa.action_gaps(bars, [("2026-06-15", "Bonus 1:1")], split_dates={"2026-06-15"}), [])  # Yahoo adjusted it

    def test_dividend_that_explains_the_fall_is_named_and_ordinary_dividends_are_quiet(self):
        day = date(2026, 6, 15)
        out = sa.action_gaps(self.bars(day, -0.06), [("2026-06-15", "Special Dividend - Rs 5 Per Share")])
        self.assertIn("most of the fall is the dividend", out[0])
        self.assertEqual(sa.action_gaps(self.bars(day, -0.06), [("2026-06-15", "Interim Dividend - Rs 1 Per Share")]), [])

    def test_report_checks_nse_actions_only_when_a_big_fall_exists(self):
        day = DAYS[-30].date()
        actions = json.dumps([{"exDate": day.strftime("%d-%b-%Y"), "subject": "Bonus 2:1"}]).encode()
        closes = [100.0 if d.date() < day else 92.0 for d in DAYS]

        class WithNse(Feed):
            def __call__(self, url):
                if "corporates-corporateActions" in url:
                    self.urls.append(url)
                    return actions
                return super().__call__(url)
        feed = WithNse({"GAPPY": lambda s: chart(s, closes)})
        r = run("GAPPY", feed)
        self.assertTrue(any("ex-date of 'Bonus 2:1'" in w for w in r["history_warnings"]))
        quiet = WithNse({"FLAT": lambda s: chart(s, rising(520))})
        run("FLAT", quiet)
        self.assertFalse(any("corporates-corporateActions" in u for u in quiet.urls))


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
        # Only the part above a regular payment is special: ₹57 - ₹12 (median of the others; the January a year
        # earlier was itself flagged) = ₹45, as TCS's ₹57 was an ₹11 interim plus a ₹46 special.
        self.assertAlmostEqual(d["specials"][0]["special_part"], 45.0)
        self.assertAlmostEqual(d["recurring_yield"], 67 / 2400)
        self.assertIn("Possible special dividend", d["flags"][0])
        self.assertIn("about ₹45.00 above a regular ₹12.00 payment", d["flags"][0])

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


class SpecialPartTests(unittest.TestCase):
    def test_only_the_amount_above_a_regular_payment_is_special(self):
        # ABB-like: the August interim a year earlier (₹9.77) is the regular amount, so ₹80.23 of the ₹90 is special.
        abb = [(date(2025, 5, 2), 33.5), (date(2025, 8, 8), 9.77), (date(2026, 4, 30), 29.59), (date(2026, 8, 7), 90.0)]
        d = sa.trailing_dividends(history_with_dividends(abb), 6000.0)
        self.assertAlmostEqual(d["specials"][0]["special_part"], 90.0 - 9.77)
        self.assertAlmostEqual(d["specials"][0]["regular_part"], 9.77)
        self.assertAlmostEqual(d["recurring_yield"], (29.59 + 9.77) / 6000.0)
        self.assertIn("about ₹80.23 above a regular ₹9.77 payment", d["flags"][0])

    def test_latest_payment_and_its_age_are_reported(self):
        d = sa.trailing_dividends(history_with_dividends([(date(2026, 6, 1), 2.0), (date(2026, 11, 3), 8.0)]), 1000.0)
        self.assertEqual(d["latest"], {"date": "2026-11-03", "amount": 8.0, "days_ago": (DAYS[-1].date() - date(2026, 11, 3)).days})


class SummaryHistoryNoteTests(unittest.TestCase):
    def setUp(self):
        self.patch = sa.load_demergers

    def tearDown(self):
        sa.load_demergers = self.patch

    def test_old_demerger_yahoo_already_adjusted_is_dropped_from_the_summary(self):
        # Reliance 2023 / Jio Financial: no jump in Yahoo's prices and more than a year old, so nothing in the
        # summary depends on it; the full report keeps it under Notes.
        record = DAYS[100].date()
        sa.load_demergers = lambda path=None: [{**DEMERGERS[0], "record_date": record.isoformat(), "cost_split_pct": {"parent": 95.32, "resulting": 4.68}}]
        r = run("TMPV", Feed({"TMPV": lambda s: chart(s, rising(520))}))
        self.assertEqual(r["demerger"]["mode"], "provider")
        self.assertTrue(r["history_note_old"])
        self.assertNotIn("demerger", sa.summary(r))
        self.assertIn("already reflect it", sa.markdown(r))

    def test_scaling_note_is_one_plain_sentence(self):
        sa.load_demergers = lambda path=None: DEMERGERS
        r = run("TMPV", Feed({"TMPV": lambda s: chart(s, demerged_closes())}))
        text = sa.summary(r)
        line = next(x for x in text.splitlines() if "Yahoo does not adjust" in x)
        self.assertIn("Yahoo does not adjust prices before the 2025-10-14 demerger of Tata Motors Limited (TMCV); they are scaled down", line)
        for jargon in ("tax apportionment", "scaled by 0.", "cost split"):
            self.assertNotIn(jargon, text)
        self.assertIn("scaled by 0.", sa.markdown(r))  # the full report keeps the detail


if __name__ == "__main__":
    unittest.main()
