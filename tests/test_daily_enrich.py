"""--collect-public with NSE evidence: live levels, NSE history, degrade paths, previous-session context, calendar,
close-phase levels for the next session and the closed-market answer. Offline fakes only."""
import contextlib
from datetime import date
import io
import json
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT / "tests"))
from core import Block, instant
import closing_context
import daily
import nse_live
from public_market import collect_daily
from test_daily_close import AFTER, after_close_fetcher
from test_daily_reports import INTRADAY, PREOPEN, fetcher as yahoo_charts, public_bundle
from test_nse_live import FakeNSE as BaseNSE, all_indices_json, close_value, sessions_until


def FakeNSE(**kw):
    return BaseNSE(snapshot=all_indices_json(stamp="18-Nov-2026 09:35"), **kw)


def yahoo_quote_fetcher(now, price=82278.6, minutes_old=15, high=82578.87, low=82212.29, previous=82532.19):
    def fetch(url):
        if "range=1d&interval=1d" not in url and "range=5d&interval=1d" not in url: return yahoo_charts(now)(url)
        meta = {"symbol": "^BSESN", "currency": "INR", "regularMarketPrice": price, "chartPreviousClose": previous,
                "regularMarketTime": int(now.timestamp()) - minutes_old * 60, "regularMarketDayHigh": high, "regularMarketDayLow": low}
        return json.dumps({"chart": {"result": [{"meta": meta}]}}).encode()
    return fetch


class FakeClosing:
    def __init__(self): self.calls = []

    def __call__(self, session, selected, include_gift):
        self.calls.append((session, tuple(selected), include_gift))
        out = {"schema": "india_closing_context_v1", "session": session,
               "indices": {"nifty": {"status": "OBSERVED", "close": 25387.4, "change_pct": 0.38}},
               "institutional_flows": {"trading_date": session, "values": {"FII/FPI": {"net_crore": 4195.38}, "DII": {"net_crore": 196.93}}},
               "nse_eq_breadth": {"advancing": 1318, "declining": 1404, "unchanged": 38}}
        if include_gift:
            out["gift"] = {"value": 25243.0, "change_points": -103.5, "change_pct": -0.41, "as_of": "2026-11-18T08:29:00+05:30", "freshness": "RECENT_UNVERIFIED_TIMEZONE"}
        return out


def enriched(now, selected=("nifty", "sensex", "banknifty"), bundle=None, nse=None, yahoo=None):
    nse_live.clear_cache()
    bundle = bundle or public_bundle(now, selected)
    closing = FakeClosing()
    daily.enrich_public(bundle, list(selected), now, nse_fetch=nse or FakeNSE(), yahoo_fetch=yahoo or yahoo_quote_fetcher(now), closing=closing)
    return bundle, closing


class IntradayTests(unittest.TestCase):
    def setUp(self):
        self.bundle, self.closing = enriched(INTRADAY)
        self.r = daily.report("india-daily", self.bundle, INTRADAY)
        self.nifty, self.sensex = self.r["indices"][0], self.r["indices"][1]

    def test_nse_live_level_leads_with_source_and_time(self):
        self.assertEqual((self.nifty["value"], self.nifty["value_basis"], self.nifty["quote_provider"]), (25233.5, "fresh_index_quote", "NSE"))
        text = daily.markdown(daily.report("nifty", self.bundle, INTRADAY))
        self.assertIn("At 09:35 IST (NSE live index level): **25,233.50**", text)
        self.assertIn("NSE live index data (www.nseindia.com, 09:35 IST)", text)

    def test_previous_close_and_20_day_average_come_from_nse_files_with_muhurat(self):
        days = sessions_until(date(2026, 11, 17), 21)
        self.assertEqual(self.nifty["levels"]["previous_close"], close_value("NIFTY 50", days[-1]))
        self.assertAlmostEqual(self.nifty["prior_sma20"], sum(close_value("NIFTY 50", d) for d in days[-20:]) / 20, places=2)
        self.assertIn("including the 8 Nov special session", self.nifty["history_basis"])

    def test_vix_breadth_previous_session_and_calendar_without_gift_or_event_boilerplate(self):
        self.assertEqual(self.r["vix"]["close"], 15.81); self.assertEqual(self.r["vix"]["at"], "2026-11-18T09:35:00+05:30")
        self.assertEqual(self.r["market_breadth"]["nifty"]["declines"], 44)
        self.assertEqual(self.closing.calls, [("2026-11-17", ("nifty", "banknifty"), False)])
        text = daily.markdown(daily.report("nifty", self.bundle, INTRADAY))
        for phrase in ("Nifty 50 members (same time, NSE): 6 up, 44 down", "Previous session (17 Nov) close: Nifty 50 25,387.40 (+0.38%)",
                       "FII/FPI +₹4,195.38 cr (net buyers)", "NSE shares (17 Nov, all EQ): 1,318 up", "to Mon 23 Nov", "India VIX (same time, provisional): 15.81"):
            self.assertIn(phrase, text)
        for phrase in ("No reviewed event calendar", "GIFT", "nseindia.in", "BLOCKED"):
            self.assertNotIn(phrase, text)

    def test_delayed_yahoo_sensex_quote_is_used_with_its_age(self):
        self.assertEqual((self.sensex["value"], self.sensex["quote_provider"]), (82278.6, "Yahoo"))
        text = daily.markdown(daily.report("sensex", self.bundle, INTRADAY))
        self.assertIn("(Yahoo quote, about 15 min delayed): **82,278.60**", text)
        self.assertIn("Sensex via Yahoo was about 15 minutes behind", text)

    def test_brief_names_public_sources_not_supplied_data(self):
        text = daily.markdown(daily.report("nifty", self.bundle, INTRADAY), brief=True)
        self.assertNotIn("supplied data", text); self.assertIn("NSE live data (nseindia.com, 09:35 IST)", text)
        self.assertIn("final after 15:30 IST", text)
        plain = daily.markdown(daily.report("nifty", public_bundle(INTRADAY, ("nifty",)), INTRADAY), brief=True)
        self.assertIn("Yahoo public data", plain); self.assertNotIn("supplied data", plain)

    def test_combined_report_does_not_repeat_table_numbers_per_index(self):
        text = daily.markdown(self.r)
        self.assertEqual(text.count("25,233.50"), 1)
        self.assertEqual(text.count("Today's high, low and close become the reference levels for Thu 19 Nov"), 1)

    def test_sensex_without_any_five_minute_bars_still_reports_quote_and_daily_reference(self):
        bundle = public_bundle(INTRADAY); bundle["indices"]["sensex"]["candles"]["rows"] = []
        bundle, _ = enriched(INTRADAY, bundle=bundle)
        r = daily.report("sensex", bundle, INTRADAY); s = r["indices"][0]
        self.assertEqual((s["status"], s["intraday_bars"], s["value"]), ("PARTIAL", "incomplete", 82278.6))
        self.assertEqual(s["levels"]["session_high"], 82578.87)  # day range from the quote, not from missing bars
        text = daily.markdown(r)
        self.assertNotIn("BARS", text); self.assertNotIn("BLOCKED", text)
        self.assertIn("judged on the latest quote because 5-minute bars are incomplete", text)

    def test_nse_failure_falls_back_to_yahoo_and_says_so(self):
        bundle, _ = enriched(INTRADAY, nse=FakeNSE(fail=["allIndices"]))
        r = daily.report("nifty", bundle, INTRADAY)
        self.assertNotEqual(r["indices"][0]["quote_provider"], "NSE")
        self.assertIn("NSE live index data unavailable this run", daily.markdown(r))


class PreopenTests(unittest.TestCase):
    def test_preopen_keeps_gift_nifty_and_previous_session(self):
        bundle, closing = enriched(PREOPEN, ("nifty",))
        self.assertTrue(closing.calls[0][2])
        text = daily.markdown(daily.report("nifty", bundle, PREOPEN))
        self.assertIn("GIFT Nifty futures (18 Nov 08:29 IST): 25,243.00", text)


class CloseTests(unittest.TestCase):
    def setUp(self):
        bundle = collect_daily(["nifty", "sensex"], fetcher=after_close_fetcher(), clock=lambda: AFTER)
        yahoo = yahoo_quote_fetcher(AFTER, price=82450.0, minutes_old=30, high=82600.0, low=82300.0)
        self.bundle, self.closing = enriched(AFTER, ("nifty", "sensex"), bundle=bundle, nse=FakeNSE(today_file=True), yahoo=yahoo)

    def test_nse_official_close_and_next_session_levels(self):
        r = daily.report("nifty", self.bundle, AFTER, "close")
        n = r["indices"][0]
        self.assertEqual(n["value"], close_value("NIFTY 50", date(2026, 11, 18))); self.assertEqual(n["value_basis"], "official_daily_close")
        text = daily.markdown(r)
        self.assertIn("(NSE closing value)", text)
        self.assertIn(f"Levels for Thu 19 Nov (next session): 18 Nov high {n['levels']['session_high']:,.2f}, low {n['levels']['session_low']:,.2f}, close {n['value']:,.2f}.", text)
        self.assertEqual(self.closing.calls[0][0], "2026-11-18")
        h, l, c = n["levels"]["session_high"], n["levels"]["session_low"], n["value"]; p = (h + l + c) / 3
        self.assertIn(f"Classic pivots for Thu 19 Nov (from 18 Nov high/low/close; arithmetic, not a forecast): S2 {p - (h - l):,.2f}, S1 {2 * p - h:,.2f}, P {p:,.2f}", text)

    def test_sensex_uses_yahoo_daily_close_labelled_unofficial(self):
        r = daily.report("sensex", self.bundle, AFTER, "close")
        s = r["indices"][0]
        self.assertEqual(s["value_basis"], "official_daily_close")
        self.assertIn("(Yahoo daily close; unofficial, BSE publishes the official value)", daily.markdown(r))
        self.assertIn("Levels for Thu 19 Nov (next session)", daily.markdown(r, brief=True))


class ClosedMarketTests(unittest.TestCase):
    def test_holiday_answers_with_name_next_day_and_last_close(self):
        nse_live.clear_cache()
        holiday = instant("2026-11-24T10:00:00+05:30")
        closing = FakeClosing()
        result, _ = daily.closed_day(["nifty"], holiday, nse_fetch=FakeNSE(), closing=closing)
        self.assertEqual(closing.calls[0][0], "2026-11-23")
        result["indices"] = {"nifty": {"status": "OBSERVED", "session": "2026-11-23", "close": 25400.0, "points_change": 12.6,
                                       "change_pct": 0.05, "low": 25300.0, "high": 25450.0}}
        result.update(coverage=[], missing_coverage=[], as_of=holiday.isoformat()); result.pop("institutional_flows"); result.pop("gift", None)
        text = daily.closed_markdown(result)
        self.assertIn("Market closed today (Tue 24 Nov 2026: Prakash Gurpurb Sri Guru Nanak Dev) — figures are from the last session, Mon 23 Nov 2026.", text)
        self.assertIn("Next trading day: Wed 25 Nov 2026.", text)

    def test_trading_day_returns_no_closed_result(self):
        nse_live.clear_cache()
        result, summary = daily.closed_day(["nifty"], INTRADAY, nse_fetch=FakeNSE(), closing=FakeClosing())
        self.assertIsNone(result); self.assertTrue(summary["today_is_trading_day"])

    def test_yahoo_session_failure_on_a_trading_day_builds_nse_only_report(self):
        nse_live.clear_cache()
        fake = FakeNSE()
        with patch("public_market.collect_daily", side_effect=Block("PUBLIC: no current provider session")), \
             patch.object(nse_live, "fetch", fake), patch.object(closing_context, "collect", FakeClosing()), \
             patch("daily.collect_vix", side_effect=Block("PUBLIC: down")), \
             patch("public_market.request", side_effect=Block("PUBLIC: offline test")), \
             patch("india_news.fetch", side_effect=Block("NEWS: offline test")), \
             patch("daily.instant", side_effect=lambda value=None: instant(value) if value is not None else INTRADAY), \
             contextlib.redirect_stdout(io.StringIO()) as out:
            self.assertEqual(daily.main(["nifty", "--collect-public", "--format", "json"]), 0)
        item = json.loads(out.getvalue())["indices"][0]
        self.assertEqual((item["value"], item["quote_provider"], item["intraday_bars"]), (25233.5, "NSE", "unavailable"))


if __name__ == "__main__":
    unittest.main()
