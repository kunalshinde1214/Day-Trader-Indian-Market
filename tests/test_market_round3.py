"""Round-3 market fixes: transient network errors degrade instead of crashing (IncompleteRead and friends), a robust
time-boxed NSE 20-day history with an on-disk cache, the Yahoo fallback note for every index, one Sensex figure when the
quote and the bar are the same moment, Sensex drivers in Sensex points, filtered headlines, the broader market line,
one 'as of' time for the Nifty headline block and the ORB position line. Offline fakes only."""
import contextlib
from datetime import date, datetime, timedelta
import http.client
import io
import json
from pathlib import Path
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch
import urllib.error

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT / "tests"))
from core import Block, IST, instant
import closing_context
import daily
import india_news
import market_movers as mm
import nse_live
import public_market
from test_daily_enrich import FakeClosing, FakeNSE as EnrichNSE, yahoo_quote_fetcher
from test_daily_reports import INTRADAY, public_bundle
from test_daily_round2 import build, fake_drivers, fake_records
from test_market_movers import FLOWS, Market, NOW as MOVERS_NOW
from test_nse_live import FakeNSE as BaseNSE, all_indices_json, close_value, index_csv, sessions_until


def incomplete():
    return http.client.IncompleteRead(b"partial", 1000)


class Response(io.BytesIO):
    def __enter__(self): return self
    def __exit__(self, *a): return False


class ScriptedOpener:
    """build_opener() stand-in: each open() takes the next scripted outcome (bytes body, or an exception to raise,
    either at open time or, for IncompleteRead, while reading the body)."""
    def __init__(self, outcomes):
        self.outcomes, self.calls = list(outcomes), 0

    def open(self, req, timeout):
        self.calls += 1
        item = self.outcomes.pop(0)
        if isinstance(item, http.client.IncompleteRead):
            class Cut(Response):
                def read(self, *a): raise item
            return Cut(b"")
        if isinstance(item, BaseException): raise item
        return Response(item)


def http_error(code):
    return urllib.error.HTTPError("https://query1.finance.yahoo.com/x", code, "x", {}, None)


YAHOO_URL = "https://query1.finance.yahoo.com/v8/finance/chart/%5ENSEI?range=1d&interval=1d"


# ---------------------------------------------------------------- fix 1: degrade, never crash

class RequestRetryTests(unittest.TestCase):
    def run_request(self, outcomes, **kw):
        opener, waits = ScriptedOpener(outcomes), []
        with patch("urllib.request.build_opener", return_value=opener):
            try:
                body = public_market.request(YAHOO_URL, sleep=waits.append, **kw)
            except Block as exc:
                body = exc
        return body, opener.calls, waits

    def test_incomplete_read_is_retried_with_backoff_then_succeeds(self):
        body, calls, waits = self.run_request([incomplete(), b"{}"])
        self.assertEqual((body, calls, waits), (b"{}", 2, [1.0]))

    def test_reset_timeout_429_and_5xx_are_retried(self):
        for first in (ConnectionResetError("reset"), TimeoutError("slow"), urllib.error.URLError("dns"), http_error(429), http_error(503),
                      http.client.RemoteDisconnected("gone")):
            body, calls, waits = self.run_request([first, b"ok"])
            self.assertEqual((body, calls, waits), (b"ok", 2, [1.0]), type(first).__name__)

    def test_two_retries_at_most_then_block(self):
        body, calls, waits = self.run_request([incomplete(), incomplete(), incomplete(), b"never"])
        self.assertIsInstance(body, Block); self.assertEqual((calls, waits), (3, [1.0, 3.0]))

    def test_other_4xx_is_final_without_retry(self):
        for code in (403, 404):
            body, calls, waits = self.run_request([http_error(code), b"never"])
            self.assertIsInstance(body, Block); self.assertEqual((calls, waits), (1, []))

    def test_retries_stop_when_the_time_budget_would_be_exceeded(self):
        ticks = iter([0.0, 21.0, 44.0, 70.0])
        body, calls, waits = self.run_request([TimeoutError(), TimeoutError(), b"late"], clock=lambda: next(ticks))
        self.assertIsInstance(body, Block)
        self.assertEqual((calls, waits), (2, [1.0]))  # 21 s + 3 s wait + a 20 s timeout would pass the 45 s budget

    def test_signature_still_takes_only_the_url(self):
        with patch("urllib.request.build_opener", return_value=ScriptedOpener([b"x"])):
            self.assertEqual(public_market.request(YAHOO_URL), b"x")


class FetchConversionTests(unittest.TestCase):
    def test_nse_closing_and_news_fetches_turn_incomplete_read_into_block(self):
        cases = ((nse_live.fetch, nse_live.ALL_INDICES), (closing_context.fetch, closing_context.FLOWS),
                 (india_news.fetch, india_news.google_news_url("Sensex Nifty when:1d")))
        for fetch, url in cases:
            with patch("urllib.request.build_opener", return_value=ScriptedOpener([incomplete()])):
                with self.assertRaises(Block, msg=fetch.__module__): fetch(url)

    def test_context_errors_cover_http_exceptions_and_timeouts(self):
        for exc in (incomplete(), http.client.BadStatusLine("x"), ConnectionError(), TimeoutError()):
            self.assertIsInstance(exc, daily.CONTEXT_ERRORS)
        self.assertTrue(issubclass(http.client.IncompleteRead, mm.ERRORS))


class DegradeTests(unittest.TestCase):
    def test_yahoo_charts_marks_one_cut_symbol_missing_and_keeps_the_rest(self):
        def fetch(url):
            if "INFY" in url: raise incomplete()
            sym = url.split("/chart/")[1].split("?")[0]
            return json.dumps({"chart": {"result": [{"meta": {"symbol": sym}}]}}).encode()
        out = mm.yahoo_charts(["INFY.NS", "TCS.NS"], fetcher=fetch)
        self.assertIsNone(out["INFY.NS"]); self.assertEqual(out["TCS.NS"]["meta"]["symbol"], "TCS.NS")

    def test_yahoo_charts_stops_waiting_after_its_budget(self):
        release = threading.Event()
        def fetch(url):
            if "SLOW" in url: release.wait(5)
            sym = url.split("/chart/")[1].split("?")[0]
            return json.dumps({"chart": {"result": [{"meta": {"symbol": sym}}]}}).encode()
        t = time.monotonic()
        out = mm.yahoo_charts(["SLOW.NS", "FAST.NS"], fetcher=fetch, budget=0.3)
        release.set()
        self.assertLess(time.monotonic() - t, 2.0)
        self.assertIsNone(out["SLOW.NS"]); self.assertIsNotNone(out["FAST.NS"])

    def test_sensex_report_survives_incomplete_read_in_every_optional_context(self):
        def boom(*a, **k): raise incomplete()
        nse_live.clear_cache()
        bundle = public_bundle(INTRADAY, ("sensex",))
        daily.enrich_public(bundle, ["sensex"], INTRADAY, nse_fetch=EnrichNSE(), yahoo_fetch=yahoo_quote_fetcher(INTRADAY),
                            closing=boom, drivers=boom, records=boom)
        errors = bundle["context_errors"]
        self.assertEqual(errors["drivers"], "Sector and stock drivers unavailable this run")
        self.assertIn("previous_context", errors)
        text = daily.markdown(daily.report("sensex", bundle, INTRADAY))
        self.assertIn("Sector and stock drivers unavailable this run.", text)

    def test_nse_fetch_cut_short_everywhere_still_gives_a_yahoo_report(self):
        def nse(url): raise incomplete()
        nse_live.clear_cache()
        bundle = public_bundle(INTRADAY, ("nifty",))
        daily.enrich_public(bundle, ["nifty"], INTRADAY, nse_fetch=nse, yahoo_fetch=yahoo_quote_fetcher(INTRADAY), closing=FakeClosing(),
                            drivers=fake_drivers(), records=fake_records())
        r = daily.report("nifty", bundle, INTRADAY)
        self.assertNotEqual(r["status"], "BLOCKED")
        self.assertIn("nse_history", bundle["context_errors"])

    def test_cli_turns_an_incomplete_read_into_a_blocked_status_not_a_traceback(self):
        with patch("public_market.collect_daily", side_effect=incomplete()), contextlib.redirect_stdout(io.StringIO()) as out:
            self.assertEqual(daily.main(["sensex", "--collect-public"]), 2)
        self.assertIn("IncompleteRead", json.loads(out.getvalue())["reason"])


# ---------------------------------------------------------------- fix 2: NSE history

class FlakyNSE(BaseNSE):
    """Fails the listed dates' files (always, or only the first time with ``once``) and can stall one date."""
    def __init__(self, bad=(), once=False, stall=None, **kw):
        super().__init__(**kw)
        self.bad, self.once, self.stall, self.seen = set(bad), once, stall, {}
        self.release = threading.Event()

    def __call__(self, url, **kw):
        if "ind_close_all_" in url:
            code = url[-12:-4]
            self.seen[code] = self.seen.get(code, 0) + 1
            if self.stall and code == self.stall: self.release.wait(5)
            if code in self.bad and (not self.once or self.seen[code] == 1): raise Block("NSE: source unavailable")
        return super().__call__(url)


def holiday_cal():
    nse_live.clear_cache()
    return nse_live.holiday_calendar(fetcher=BaseNSE(), clock=lambda: INTRADAY)


class HistoryTests(unittest.TestCase):
    END = date(2026, 11, 17)

    def test_one_failing_file_is_skipped_and_the_window_walks_back(self):
        nse_live.clear_cache()
        fake = FlakyNSE(bad={"05112026"})
        h = nse_live.index_history(["NIFTY 50"], self.END, 21, cal=holiday_cal(), fetcher=fake)
        self.assertEqual(len(h["sessions"]), 21); self.assertNotIn("2026-11-05", h["sessions"])
        self.assertEqual(h["dates_failed"], ["2026-11-05"])
        self.assertEqual(fake.seen["05112026"], 2)  # retried once before giving up
        self.assertEqual(h["sessions"][0], sessions_until(self.END, 22)[0].isoformat())  # one session further back

    def test_a_file_that_fails_once_is_recovered_by_the_retry(self):
        nse_live.clear_cache()
        h = nse_live.index_history(["NIFTY 50"], self.END, 21, cal=holiday_cal(), fetcher=FlakyNSE(bad={"05112026"}, once=True))
        self.assertEqual(h["dates_failed"], []); self.assertIn("2026-11-05", h["sessions"])
        self.assertEqual(h["sessions"], [d.isoformat() for d in sessions_until(self.END, 21)])

    def test_a_stalled_file_is_abandoned_when_the_budget_runs_out(self):
        nse_live.clear_cache()
        fake = FlakyNSE(stall="12112026")
        t = time.monotonic()
        h = nse_live.index_history(["NIFTY 50"], self.END, 21, cal=holiday_cal(), fetcher=fake, budget=0.5)
        fake.release.set()
        self.assertLess(time.monotonic() - t, 2.5)
        self.assertIn("2026-11-12", h["dates_failed"])

    def test_live_history_uses_the_short_timeout(self):
        calls = []
        def live(url, timeout=None):
            calls.append(timeout); return index_csv(datetime.strptime(url[-12:-4], "%d%m%Y").date())
        nse_live.clear_cache()
        with patch.object(nse_live, "_LIVE_FETCH", live):
            nse_live.index_history(["NIFTY 50"], self.END, 3, cal=None, fetcher=live, cache=False)
        self.assertTrue(calls); self.assertEqual(set(calls), {nse_live.HISTORY_TIMEOUT})
        self.assertLessEqual(nse_live.HISTORY_TIMEOUT, 8); self.assertLessEqual(nse_live.HISTORY_BUDGET, 30)

    def test_disk_cache_serves_published_files_and_skips_today(self):
        with tempfile.TemporaryDirectory() as folder:
            nse_live.clear_cache()
            first = nse_live.index_history(["NIFTY 50"], self.END, 21, cal=holiday_cal(), fetcher=BaseNSE(), cache=folder)
            files = sorted(p.name for p in (Path(folder) / "nse_ind_close_all").glob("*.csv"))
            self.assertGreaterEqual(len(files), 21)
            cal = holiday_cal()
            def down(url): raise Block("NSE: source unavailable")
            again = nse_live.index_history(["NIFTY 50"], self.END, 21, cal=cal, fetcher=down, cache=folder)
            self.assertEqual(again["dates_failed"], [])
            self.assertEqual(again["series"], first["series"])
            # a damaged cached copy is ignored and fetched again
            (Path(folder) / "nse_ind_close_all" / "ind_close_all_17112026.csv").write_bytes(b"garbage")
            nse_live.clear_cache()
            rows = nse_live.cached_index_closes(self.END, fetcher=BaseNSE(), folder=folder)
            self.assertEqual(rows["NIFTY 50"]["close"], close_value("NIFTY 50", self.END))
            # today's file is never written (it can still be revised the same day)
            nse_live.clear_cache()
            nse_live.cached_index_closes(date(2026, 11, 18), fetcher=BaseNSE(today_file=True), folder=folder, today=date(2026, 11, 18))
            self.assertFalse((Path(folder) / "nse_ind_close_all" / "ind_close_all_18112026.csv").exists())

    def test_injected_fetchers_never_touch_the_user_cache(self):
        with patch.object(nse_live, "cache_dir", side_effect=AssertionError("user cache used")):
            nse_live.clear_cache()
            nse_live.index_history(["NIFTY 50"], self.END, 3, cal=None, fetcher=BaseNSE())

    def test_cache_folder_is_outside_the_plugin_and_can_be_switched_off(self):
        with patch.dict("os.environ", {nse_live.CACHE_ENV: "off"}): self.assertIsNone(nse_live.cache_dir())
        with patch.dict("os.environ", {nse_live.CACHE_ENV: "X:/elsewhere"}): self.assertEqual(nse_live.cache_dir(), Path("X:/elsewhere"))
        with patch.dict("os.environ", {nse_live.CACHE_ENV: ""}):
            folder = nse_live.cache_dir()
            self.assertNotIn(str(ROOT.resolve()).lower(), str(folder).lower()); self.assertIn("india-day-trader", str(folder))

    def test_daily_notes_a_skipped_file_in_the_window(self):
        nse_live.clear_cache()
        bundle = public_bundle(INTRADAY, ("nifty",))
        nse = FlakyNSE(bad={"05112026"}, snapshot=all_indices_json(stamp="18-Nov-2026 09:35"))
        daily.enrich_public(bundle, ["nifty"], INTRADAY, nse_fetch=nse, yahoo_fetch=yahoo_quote_fetcher(INTRADAY), closing=FakeClosing(),
                            drivers=fake_drivers(), records=fake_records())
        basis = bundle["indices"]["nifty"]["history"]["basis"]
        self.assertIn("the NSE file for 5 Nov could not be read this run, so the window starts 1 session earlier", basis)

    def test_unreadable_latest_file_falls_back_to_yahoo_with_the_special_session_note(self):
        nse_live.clear_cache()
        bundle = public_bundle(INTRADAY, ("nifty",))
        nse = FlakyNSE(bad={"17112026"}, snapshot=all_indices_json(stamp="18-Nov-2026 09:35"))
        daily.enrich_public(bundle, ["nifty"], INTRADAY, nse_fetch=nse, yahoo_fetch=yahoo_quote_fetcher(INTRADAY), closing=FakeClosing(),
                            drivers=fake_drivers(), records=fake_records())
        self.assertEqual(bundle["session"]["previous_session"], "2026-11-17")  # never moved back to an older NSE file
        self.assertIn("nse_history", bundle["context_errors"])


# ---------------------------------------------------------------- fix 3: Yahoo fallback basis for every index

class YahooBasisTests(unittest.TestCase):
    def test_nifty_and_bank_nifty_on_yahoo_carry_the_special_session_note(self):
        nse_live.clear_cache()
        bundle = public_bundle(INTRADAY, ("nifty", "banknifty"))
        nse = BaseNSE(snapshot=all_indices_json(stamp="18-Nov-2026 09:35"), fail=("ind_close_all",))
        daily.enrich_public(bundle, ["nifty", "banknifty"], INTRADAY, nse_fetch=nse, yahoo_fetch=yahoo_quote_fetcher(INTRADAY),
                            closing=FakeClosing(), drivers=fake_drivers(), records=fake_records())
        text = daily.markdown(daily.report("nifty", bundle, INTRADAY))
        self.assertIn("Nifty 50 20-day average: Yahoo daily closes (unofficial), NSE daily files unavailable this run; "
                      "Yahoo omits the 8 Nov special session, so the window starts one session earlier.", text)
        self.assertIn("special session", bundle["indices"]["banknifty"]["history"]["basis"])


# ---------------------------------------------------------------- fix 4: one Sensex figure

class SameMomentTests(unittest.TestCase):
    def result(self, quote_at, bar_at):
        sx = {"index_id": "sensex", "status": "READY", "as_of": "2026-11-18T12:00:00+05:30", "value_at": quote_at,
              "change_from_previous_close_pct": -0.51}
        return {"indices": [sx], "same_moment": {"at": bar_at, "sensex_pct": -0.47, "nifty_pct": -0.49, "sensex_quote_at": quote_at,
                                                  "nifty_now_at": "2026-11-18T12:00:00+05:30", "nifty_now_pct": -0.64}}

    def test_quote_and_bar_at_the_same_moment_show_one_sensex_figure(self):
        line = daily.same_moment_line(self.result("2026-11-18T11:45:40+05:30", "2026-11-18T11:45:00+05:30"))
        self.assertIn("compare like for like at the same 5-minute bar, ending 11:45 IST (Yahoo, both indices): Sensex -0.51%, Nifty 50 -0.49%", line)
        self.assertNotIn("-0.47%", line)

    def test_bars_further_apart_are_labelled_by_their_end_time(self):
        line = daily.same_moment_line(self.result("2026-11-18T11:51:00+05:30", "2026-11-18T11:45:00+05:30"))
        self.assertIn("compare like for like at the same 5-minute bar, ending 11:45 IST (Yahoo, both indices): Sensex -0.47%, Nifty 50 -0.49%", line)

    def test_live_sensex_report_has_one_sensex_percentage_for_that_moment(self):
        text = daily.markdown(daily.report("sensex", build(("sensex",)), INTRADAY))
        line = next(l for l in text.splitlines() if l.startswith("- Like-for-like check: Sensex's free feed"))  # round 5: a data note
        self.assertIn("Sensex -0.31%, Nifty 50", line); self.assertIn("bar, ending 09:20 IST", line)
        self.assertEqual(line.count("Sensex -"), 1)


# ---------------------------------------------------------------- fix 5: Sensex points

class SensexMarket(Market):
    """Sensex = cap-weighted 30 of the 50 synthetic stocks (not the first 30), on Yahoo ^BSESN."""
    MEMBERS = [f"S{k:02d}" for k in range(34) if k not in (3, 7, 11, 15)]

    def __init__(self):
        super().__init__()
        self.sensex = [sum(sh * self.prices[s][t] for sh, s in zip(self.shares, self.symbols) if s in self.MEMBERS) * 3.3
                       for t in range(len(self.stamps))]

    def chart(self, symbol):
        if symbol != "^BSESN": return super().chart(symbol)
        meta = {"symbol": symbol, "currency": "INR", "regularMarketPrice": self.sensex[-4], "regularMarketTime": self.stamps[-4] + 299}
        return {"chart": {"result": [{"meta": meta, "timestamp": self.stamps[:-3], "indicators": {"quote": [{"close": self.sensex[:-3]}]}}]}}


class SensexDriverTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        nse_live.clear_cache()
        cls.m = SensexMarket()
        snap = nse_live.parse_all_indices(cls.m.snapshot(), MOVERS_NOW)
        cls.d = mm.live_drivers(["SENSEX"], snap, nse_fetch=cls.m.nse, yahoo_fetch=cls.m.yahoo, sensex_previous=cls.m.sensex[cls.m.prev_t])
        cls.sx = cls.d["indices"]["SENSEX"]

    def test_the_30_members_are_identified_from_sensex_moves(self):
        self.assertEqual(self.sx["members"], sorted(SensexMarket.MEMBERS))
        self.assertNotIn("NIFTY 50", self.d["indices"])

    def test_points_are_sensex_points_checked_at_the_same_delayed_bar(self):
        sx, m = self.sx, self.m
        self.assertEqual(sx["actual_at"], datetime.fromtimestamp(m.stamps[-5] + 300, IST).isoformat())  # last complete delayed bar
        self.assertAlmostEqual(sx["check_estimated_points"], sx["actual_points"], delta=abs(sx["actual_points"]) * 0.05 + 1)
        self.assertTrue(daily.reliable(sx))
        self.assertTrue(all(r["symbol"] in SensexMarket.MEMBERS for r in sx["negative"] + sx["positive"]))

    def test_sensex_report_prints_sensex_points_never_nifty_points(self):
        result = {"drivers": self.d, "indices": [{"index_id": "sensex", "status": "READY", "change_from_previous_close_points": -50.0}]}
        line = daily.driver_line(result, "sensex")
        self.assertIn("(estimated Sensex points)", line); self.assertNotIn("Nifty 50 points", line)
        nifty_only = {**self.d, "indices": {"NIFTY 50": self.sx}}
        self.assertNotIn("points", daily.driver_line({**result, "drivers": nifty_only}, "sensex"))

    def test_failed_check_shows_member_moves_without_points(self):
        bad = {**self.sx, "check_estimated_points": self.sx["actual_points"] + 500}
        result = {"drivers": {**self.d, "indices": {"SENSEX": bad}}, "indices": [{"index_id": "sensex", "status": "READY", "change_from_previous_close_points": -5}]}
        line = daily.driver_line(result, "sensex")
        self.assertIn("largest Sensex-member moves", line); self.assertNotIn("pts", line)

    def test_daily_asks_for_sensex_drivers_on_a_sensex_report(self):
        calls = []
        def rec(indices, snap, **kw):
            calls.append((list(indices), kw.get("sensex_previous"))); return fake_drivers()(indices, snap)
        build(("sensex",), drivers=rec)
        self.assertEqual(calls[0][0], ["SENSEX"]); self.assertAlmostEqual(calls[0][1], 82532.19)
        calls.clear(); build(drivers=rec)
        self.assertEqual(calls[0][0], ["NIFTY 50", "NIFTY BANK"])


# ---------------------------------------------------------------- fix 6: headlines

class HeadlineFilterTests(unittest.TestCase):
    RESULT = {"contributors": {"negative": [{"symbol": "TITAN", "name": "Titan Company Ltd."}, {"symbol": "HDFCBANK", "name": "HDFC Bank Ltd."}],
                               "positive": [{"symbol": "SBIN", "name": "State Bank of India"}]},
              "sectors": [{"name": "Nifty Pharma", "change_pct": 0.5}, {"name": "Nifty IT", "change_pct": 0.1}, {"name": "Nifty Metal", "change_pct": -2.0}]}

    def test_only_mover_sector_or_macro_headlines_are_kept(self):
        terms = mm.mover_terms(self.RESULT)
        keep = ["Titan shares slump 4% after Q2 margin miss", "Sensex falls as HDFC Bank drags", "Metal stocks slide on China demand",
                "FIIs sell Rs 4,000 crore", "Crude jumps 3%", "Rupee hits record low", "Wall Street ends lower", "Profit booking drags Nifty",
                "SBI gains on loan growth", "IT stocks rise"]
        drop = ["Stock markets open lower", "Sensex, Nifty: 5 things to know", "Vedanta Shares Today", "Nifty ends lower; it was a weak day"]
        for h in keep: self.assertTrue(mm.relevant(h, terms), h)
        for h in drop: self.assertFalse(mm.relevant(h, terms), h)

    def test_stock_pick_pieces_are_not_causes(self):
        h = "HDFC Bank vs Axis Bank: Both shares recover after Q2 sell-off; which banking stock offers higher upside? Check targets"
        self.assertTrue(mm.OUTLOOK_RE.search(h))
        self.assertFalse(mm.OUTLOOK_RE.search("Titan shares fall 4% as gold prices crash"))

    def test_old_dated_stock_news_is_dropped_but_upcoming_dates_are_not(self):
        self.assertTrue(mm.dated_before("Titan launches QIP on Nov 11, sets floor price", "2026-11-18"))
        self.assertTrue(mm.dated_before("Board approved the issue on 11th November", "2026-11-18"))
        self.assertFalse(mm.dated_before("Record date fixed as 25 Nov", "2026-11-18"))
        self.assertFalse(mm.dated_before("Titan shares fall 4% today", "2026-11-18"))

    def test_stock_headlines_since_the_last_close_only(self):
        def feed(url):
            items = [("Titan QIP launched on Nov 11 at floor price - Mint", "Wed, 18 Nov 2026 04:00:00 GMT"),
                     ("Titan shares slump after margin warning - ET", "Tue, 17 Nov 2026 08:00:00 GMT"),
                     ("Titan shares slump 4% on weak guidance - Reuters", "Wed, 18 Nov 2026 05:00:00 GMT")]
            return ("<rss><channel>" + "".join(f"<item><title>{t}</title><link>https://news.google.com/rss/articles/{k}</link><pubDate>{d}</pubDate></item>"
                                               for k, (t, d) in enumerate(items)) + "</channel></rss>").encode()
        now = instant("2026-11-18T12:00:00+05:30")
        got = mm.stock_headlines([{"symbol": "TITAN", "name": "Titan Company Ltd."}], now, news_fetch=feed,
                                 since=mm.session_close("2026-11-17"), session="2026-11-18")
        self.assertEqual([h["headline"] for h in got], ["Titan shares slump 4% on weak guidance"])

    def test_collect_filters_market_headlines_to_drivers(self):
        def feed(url):
            items = [("Sensex drops 300 pts; metal stocks struggle", "Wed, 18 Nov 2026 04:20:00 GMT"),
                     ("Stock market today: Sensex, Nifty open lower", "Wed, 18 Nov 2026 04:00:00 GMT"),
                     ("Dow plunges 850 points as AI selloff deepens", "Tue, 17 Nov 2026 13:07:00 GMT"),
                     ("Gold slips as dollar firms", "Tue, 17 Nov 2026 06:00:00 GMT")]  # before the 17 Nov close
            return ("<rss><channel>" + "".join(f"<item><title>{t}</title><link>https://economictimes.indiatimes.com/{k}</link><pubDate>{d}</pubDate></item>"
                                               for k, (t, d) in enumerate(items)) + "</channel></rss>").encode()
        nse_live.clear_cache(); m = Market()
        r = mm.collect(nse_fetch=m.nse, flow_fetch=lambda url: FLOWS, yahoo_fetch=m.yahoo, clock=lambda: MOVERS_NOW, news=True, news_fetch=feed)
        self.assertEqual([h["headline"] for h in r["headlines"]], ["Sensex drops 300 pts; metal stocks struggle", "Dow plunges 850 points as AI selloff deepens"])
        self.assertIn("Headlines: Kept only headlines naming a moving stock or sector", mm.markdown(r))


# ---------------------------------------------------------------- fix 7: broader market

def broad_snapshot():
    extra = (("NIFTY MIDCAP 150", 21000.0, 21200.0, "BROAD MARKET INDICES", (30, 120, 0)),
             ("NIFTY SMALLCAP 250", 16000.0, 16250.0, "BROAD MARKET INDICES", (50, 200, 0)),
             ("NIFTY 500", 23000.0, 23200.0, "BROAD MARKET INDICES", (110, 385, 5)))
    return all_indices_json(stamp="18-Nov-2026 09:35", extra=extra)


class BroaderMarketTests(unittest.TestCase):
    def test_india_daily_shows_midcaps_smallcaps_and_nifty_500_breadth(self):
        r = daily.report("india-daily", build(snapshot=broad_snapshot()), INTRADAY)
        text = daily.markdown(r)
        self.assertIn("Broader market (NSE, 09:35 IST): Nifty Midcap 150 -0.94%, Nifty Smallcap 250 -1.54%; "
                      "Nifty 500 stocks 110 up, 385 down, 5 unchanged.", text)
        self.assertIn("Broader market (NSE, 09:35 IST)", daily.markdown(r, brief=True))

    def test_single_index_reports_do_not_add_it(self):
        self.assertNotIn("Broader market", daily.markdown(daily.report("nifty", build(("nifty",), snapshot=broad_snapshot()), INTRADAY)))


# ---------------------------------------------------------------- fix 8: one time, plain words

class NiftyHeadlineTests(unittest.TestCase):
    def test_nse_live_headline_block_carries_one_time(self):
        text = daily.markdown(daily.report("nifty", build(("nifty",)), INTRADAY))
        self.assertTrue(text.startswith("# Nifty 50 - Wed 18 Nov 2026, market hours, data as of 09:35 IST"))
        self.assertIn("At 09:35 IST (NSE live index level)", text)
        head = text.split("| Level | Points |")[0]
        self.assertEqual(head.count("09:35") - head.count("met at 09:35"), 2)  # title and headline only (plus any event time)
        self.assertIn("What's moving it: ", head); self.assertIn("India VIX (same time, provisional)", head)
        self.assertIn("Nifty 50 members (same time, NSE)", text)

    def test_lines_from_another_moment_keep_their_time(self):
        result = {"drivers": {"at": "2026-11-18T09:31:00+05:30", "sectors_up": [{"name": "Nifty IT", "change_pct": 1.0}], "sectors_down": [], "indices": {}},
                  "indices": [{"index_id": "nifty", "status": "READY", "change_from_previous_close_points": 5.0}]}
        self.assertIn("(09:31 IST)", daily.driver_line(result, "nifty", ref="2026-11-18T09:35:00+05:30"))

    def test_plain_words_for_opening_range_and_conditions(self):
        bundle = build(("nifty",))
        bars = bundle["indices"]["nifty"]["candles"]["rows"]
        low = bundle["indices"]["nifty"]["previous"]["low"]
        bars[3].update(open=low - 50, high=low - 40, low=low - 60, close=low - 45)
        bundle["indices"]["nifty"].pop("quote", None)
        text = daily.markdown(daily.report("nifty", bundle, INTRADAY))
        self.assertIn("the previous session's low - a sign of weakness", text)
        self.assertIn("How to read this: the opening range is the high and low of the first 15 minutes (09:15-09:30 IST).", text)
        self.assertIn("the bearish condition no longer applies; the bullish signal needs a completed close above", text)


# ---------------------------------------------------------------- fix 9 and docs

class SkillDocTests(unittest.TestCase):
    def test_bank_nifty_skill_routes_tomorrow_levels_to_the_option_chain(self):
        text = (ROOT / "skills" / "banknifty" / "SKILL.md").read_text(encoding="utf-8")
        self.assertIn('scripts/fno.py" chain BANKNIFTY --summary', text)
        self.assertIn("next session", text); self.assertIn("not a line for today's session", text); self.assertIn("OI walls", text)

    def test_skill_descriptions_have_no_colon_space(self):
        for name in ("nifty", "sensex", "banknifty", "india-daily", "market"):
            text = (ROOT / "skills" / name / "SKILL.md").read_text(encoding="utf-8")
            desc = next(l for l in text.splitlines() if l.startswith("description:"))
            self.assertNotIn(": ", desc.split(": ", 1)[1], name)


# ---------------------------------------------------------------- ORB entry (public_market) position line

class OrbLineTests(unittest.TestCase):
    def view(self, price, state="waiting", side="BUY"):
        return {"opening_range": {"complete": True, "high": 1372.30, "low": 1360.0}, "last_close": {"price": price, "completed_at": "10:05"},
                "trigger_level": 1372.30 if side == "BUY" else 1360.0, "state": state, "side": side}

    def test_where_price_sits_and_how_far_the_trigger_is(self):
        line = public_market.position_line(self.view(1358.60))
        self.assertIn("₹1,358.60 now (last completed close, 10:05 IST) — below the range low", line)
        self.assertIn("the long trigger (close above ₹1,372.30) is 1.0% away.", line)
        self.assertIn("inside the range", public_market.position_line(self.view(1365.0)))
        self.assertIn("short trigger (close below ₹1,360.00) is 0.4% away", public_market.position_line(self.view(1365.0, side="SELL")))
        self.assertIsNone(public_market.position_line({**self.view(1365.0), "opening_range": {"complete": False}}))

    def test_history_states_its_entry_window(self):
        t = public_market.orb_tally([], instant("2026-11-18T12:00:00+05:30"))
        self.assertEqual(t["entry_window"], ["09:30", "14:30"])


if __name__ == "__main__":
    unittest.main()
