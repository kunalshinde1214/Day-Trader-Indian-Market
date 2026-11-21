"""Round-2 daily briefing fixes: what is moving the index (sectors, point drags/lifts, Bank Nifty heavyweights),
52-week/record context, a delayed Sensex compared at the same moment, 'condition met at HH:MM', provisional levels,
Bank Nifty/Sensex calendar noise and short disclaimers. Offline fakes only."""
from datetime import date, datetime, timedelta
import json
from pathlib import Path
import random
import sys
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT / "tests"))
from core import IST, instant
import daily
import market_movers as mm
import nse_live
from test_daily_enrich import FakeClosing, FakeNSE as EnrichNSE, yahoo_quote_fetcher
from test_daily_reports import INTRADAY, public_bundle
from test_market_movers import FLOWS, Market, NOW as MOVERS_NOW
from test_nse_live import FakeNSE as BaseNSE, all_indices_json

AT = "2026-11-18T09:35:00+05:30"
PREV_HIGH = 57500.0  # test_nse_live fixture: Bank Nifty 17 Nov high (close 57,470.00)


def block(rows, est=-150.0, actual=-153.9, r2=0.995):
    neg = sorted([r for r in rows if r["points"] < 0], key=lambda r: r["points"])
    pos = sorted([r for r in rows if r["points"] > 0], key=lambda r: -r["points"])
    return {"positive": pos, "negative": neg, "heavyweights": sorted(rows, key=lambda r: -r["weight_pct"])[:5],
            "estimated_total_points": est, "actual_points": actual, "actual_at": AT, "fit": {"r_squared": r2}}


def stock(symbol, pct, weight, points):
    return {"symbol": symbol, "name": symbol + " Ltd.", "change_pct": pct, "weight_pct": weight, "points": points}


def fake_drivers(nifty_r2=0.995):
    nifty = [stock("TITAN", -3.26, 2.1, -17.3), stock("BAJFINANCE", -1.5, 3.0, -11.3), stock("INFY", -1.09, 3.3, -9.0),
             stock("HDFCBANK", -0.29, 11.0, -8.1), stock("ICICIBANK", 0.23, 10.0, 5.9), stock("LT", 0.30, 4.2, 3.2), stock("SBIN", 0.28, 4.0, 2.8)]
    bank = [stock("HDFCBANK", -0.29, 19.5, -35.2), stock("ICICIBANK", 0.23, 18.0, 25.8), stock("SBIN", 0.28, 9.5, 16.6),
            stock("KOTAKBANK", -0.80, 8.5, -42.3), stock("AXISBANK", -0.40, 8.0, -19.9), stock("PNB", 0.1, 1.0, 0.6)]
    def call(indices, snap, **kw):
        out = {"at": snap["as_of"], "source": snap["page"], "sectors_up": [{"name": "Nifty Media", "change_pct": 0.54}, {"name": "Nifty Pharma", "change_pct": 0.23}],
               "sectors_down": [{"name": "Nifty Consumer Durables", "change_pct": -2.03}, {"name": "Nifty Metal", "change_pct": -1.74}],
               "sector_levels": {"Nifty PSU Bank": 0.12, "Nifty Private Bank": -0.31}, "indices": {}}
        if "NIFTY 50" in indices: out["indices"]["NIFTY 50"] = block(nifty, r2=nifty_r2)
        if "NIFTY BANK" in indices: out["indices"]["NIFTY BANK"] = block(bank, est=-54.4, actual=-56.0)
        return out
    return call


def snapshot_with_year_high(bank_year_high=62442.2, nifty_year_high=26373.2, bank_day=None):
    raw = json.loads(all_indices_json(stamp="18-Nov-2026 09:35"))
    for row in raw["data"]:
        if row["index"] == "NIFTY BANK":
            row.update(yearHigh=bank_year_high, yearLow=49954.85)
            if bank_day:  # (last, previous close, high, low) consistent with the NSE-file previous session
                last, prev, high, low = bank_day; ch = round(last - prev, 2)
                row.update(last=last, previousClose=prev, variation=ch, percentChange=round(ch / prev * 100, 2), open=prev, high=high, low=low)
        if row["index"] == "NIFTY 50": row.update(yearHigh=nifty_year_high, yearLow=22179.9)
    return json.dumps(raw).encode()


def fake_records(best_close=61575.9):
    def call(symbol, previous_session, today, fetcher=None):
        return {"basis": "Yahoo daily history (unofficial)", "first": "2016-11-18", "sessions": 2470,
                "max_close_before_previous": ["2026-11-16", best_close if symbol == "^NSEBANK" else 99999.0],
                "max_high_before_today": ["2026-11-17", 62442.2 if symbol == "^NSEBANK" else 99999.0],
                "fifty_two_week_high": 85978.25 if symbol == "^BSESN" else None}
    return call


def build(selected=("nifty", "sensex", "banknifty"), now=INTRADAY, drivers=None, records=None, snapshot=None, bundle=None):
    nse_live.clear_cache()
    bundle = bundle or public_bundle(now, selected)
    nse = EnrichNSE() if snapshot is None else BaseNSE(snapshot=snapshot)
    daily.enrich_public(bundle, list(selected), now, nse_fetch=nse, yahoo_fetch=yahoo_quote_fetcher(now), closing=FakeClosing(),
                        drivers=drivers or fake_drivers(), records=records or fake_records())
    return bundle


class DriverLineTests(unittest.TestCase):
    def test_nifty_report_says_what_is_moving_it(self):
        text = daily.markdown(daily.report("nifty", build(("nifty",)), INTRADAY))
        self.assertIn("What's moving it: ", text)  # same minute as the NSE headline: one 'as of' time
        for phrase in ("weakest sectors Consumer Durables -2.03%, Metal -1.74%", "strongest sectors Media +0.54%, Pharma +0.23%",
                       "biggest drags TITAN -17.3 pts (-3.26%), BAJFINANCE -11.3 pts (-1.50%), INFY -9.0 pts (-1.09%)",
                       "lifts ICICIBANK +5.9 pts (+0.23%), LT +3.2 pts (+0.30%), SBIN +2.8 pts (+0.28%)", "(estimated Nifty 50 points)."):
            self.assertIn(phrase, text)
        self.assertIn("check: Nifty 50 est. -150.0 vs actual -153.9 pts", text)
        brief = daily.markdown(daily.report("nifty", build(("nifty",)), INTRADAY), brief=True)
        self.assertIn("- What's moving it: ", brief)

    def test_bank_nifty_lists_heavyweights_and_bank_sub_indices(self):
        text = daily.markdown(daily.report("banknifty", build(("banknifty",)), INTRADAY))
        self.assertIn("heavyweights HDFCBANK -0.29% (-35 pts), ICICIBANK +0.23% (+26 pts), SBIN +0.28% (+17 pts), KOTAKBANK -0.80% (-42 pts), "
                      "AXISBANK -0.40% (-20 pts) (estimated Bank Nifty points); bank sub-indices PSU Bank +0.12%, Private Bank -0.31%.", text)
        self.assertNotIn("PNB", text.split("heavyweights")[1].split("\n")[0])

    def test_sensex_and_india_daily_carry_market_drivers(self):
        self.assertIn("What's moving it (09:35 IST)", daily.markdown(daily.report("sensex", build(("sensex",)), INTRADAY)))
        combined = daily.markdown(daily.report("india-daily", build(), INTRADAY), brief=True)
        self.assertIn("- What's moving the market (09:35 IST)", combined); self.assertIn("- Bank Nifty (09:35 IST): heavyweights", combined)

    def test_poor_weight_fit_drops_point_estimates_but_keeps_sectors(self):
        text = daily.markdown(daily.report("nifty", build(("nifty",), drivers=fake_drivers(nifty_r2=0.5)), INTRADAY))
        self.assertIn("weakest sectors Consumer Durables -2.03%", text)
        self.assertNotIn("biggest drags", text)

    def test_the_side_that_explains_the_move_comes_first(self):
        r = daily.report("nifty", build(("nifty",)), INTRADAY)
        drivers = fake_drivers()(["NIFTY 50"], {"as_of": AT, "page": "x"})
        down = {"indices": [{**r["indices"][0], "change_from_previous_close_points": -150.0}], "drivers": drivers}
        up = {"indices": [{**r["indices"][0], "change_from_previous_close_points": 90.0}], "drivers": drivers}
        self.assertTrue(daily.driver_line(down, "nifty").startswith("What's moving it (09:35 IST): weakest sectors"))
        self.assertIn("; biggest drags TITAN", daily.driver_line(down, "nifty"))
        self.assertTrue(daily.driver_line(up, "nifty").startswith("What's moving it (09:35 IST): strongest sectors"))
        self.assertLess(daily.driver_line(up, "nifty").index("lifts"), daily.driver_line(up, "nifty").index("biggest drags"))

    def test_drivers_failure_is_one_note_not_a_block(self):
        def broken(*a, **k): raise nse_live.Missing("NSE: no file")
        r = daily.report("nifty", build(("nifty",), drivers=broken), INTRADAY)
        self.assertNotEqual(r["status"], "BLOCKED")
        self.assertIn("Sector and stock drivers unavailable this run.", daily.markdown(r))


class RecordTests(unittest.TestCase):
    def test_bank_nifty_52_week_high_and_record_close_are_flagged(self):
        snap = snapshot_with_year_high(bank_year_high=PREV_HIGH, bank_day=(57300.0, 57470.0, 57480.0, 57250.0))
        r = daily.report("banknifty", build(("banknifty",), snapshot=snap, records=fake_records(best_close=57000.0)), INTRADAY)
        b = r["indices"][0]
        self.assertTrue(b["records"]["previous_high_is_year_high"]); self.assertTrue(b["records"]["previous_record_close"])
        text = daily.markdown(r)
        self.assertIn("Records: 17 Nov high 57,500.00 is the 52-week high (NSE), now 0.35% below it;", text)
        self.assertIn("17 Nov close 57,470.00 was a record close (highest close in Yahoo daily data from 18 Nov 2016, the start of the data used)", text)
        self.assertIn("52-week highs from NSE (Sensex: Yahoo); record closes from Yahoo daily history (unofficial).", text)

    def test_a_session_high_above_the_52_week_high_is_a_new_high(self):
        r = daily.report("banknifty", build(("banknifty",), snapshot=snapshot_with_year_high(bank_year_high=PREV_HIGH), records=fake_records(best_close=57000.0)), INTRADAY)
        line = daily.record_line(r["indices"][0])
        self.assertIn("18 Nov high 62,278.25 is a new 52-week high (above 57,500.00, the 17 Nov high; NSE)", line)
        self.assertNotIn("17 Nov high 57,500.00 is the 52-week high", line)

    def test_distance_to_a_nearby_52_week_high_is_stated(self):
        r = daily.report("banknifty", build(("banknifty",), snapshot=snapshot_with_year_high(), records=fake_records(best_close=63000.0)), INTRADAY)
        b = r["indices"][0]
        self.assertFalse(b["records"]["previous_record_close"]); self.assertFalse(b["records"]["previous_high_is_year_high"])
        expected = round((1 - b["value"] / 62442.2) * 100, 2)
        self.assertEqual(daily.record_line(b), f"Records: now {expected:.2f}% below the 52-week high 62,442.20 (NSE).")

    def test_no_record_line_far_from_the_high(self):
        r = daily.report("nifty", build(("nifty",), snapshot=snapshot_with_year_high()), INTRADAY)
        self.assertIsNone(daily.record_line(r["indices"][0]))  # Nifty is >2% below its 52-week high and no record close
        r = daily.report("banknifty", build(("banknifty",), snapshot=snapshot_with_year_high(bank_year_high=70000.0), records=fake_records(best_close=63000.0)), INTRADAY)
        self.assertIsNone(daily.record_line(r["indices"][0]))
        self.assertNotIn("Records:", daily.markdown(r))

    def test_record_close_and_high_after_the_close(self):
        from public_market import collect_daily
        from test_daily_close import AFTER, after_close_fetcher
        nse_live.clear_cache()
        bundle = collect_daily(["nifty"], fetcher=after_close_fetcher(), clock=lambda: AFTER)
        low = lambda symbol, previous_session, today, fetcher=None: {"basis": "Yahoo daily history (unofficial)", "first": "2016-11-18",
                                                                     "max_close_before_previous": ["2026-11-10", 100.0], "max_high_before_today": ["2026-11-10", 100.0]}
        daily.enrich_public(bundle, ["nifty"], AFTER, nse_fetch=EnrichNSE(today_file=True), yahoo_fetch=yahoo_quote_fetcher(AFTER, minutes_old=30),
                            closing=FakeClosing(), drivers=fake_drivers(), records=low)
        r = daily.report("nifty", bundle, AFTER, "close"); n = r["indices"][0]
        self.assertTrue(n["records"]["record_close_today"])
        line = daily.record_line(n)
        self.assertIn(f"18 Nov close {n['value']:,.2f} is a record close (highest close in Yahoo daily data from 18 Nov 2016, the start of the data used)", line)
        self.assertIn(f"18 Nov high {n['levels']['session_high']:,.2f} is a record high", line)
        self.assertNotIn("17 Nov close", line)  # after the close, today's record replaces yesterday's

    def test_record_history_summarises_yahoo_daily_chart(self):
        start = datetime(2024, 1, 1, 9, 15, tzinfo=IST)
        days = [start + timedelta(days=k) for k in range(700)]
        closes = [100.0 + k * 0.1 for k in range(700)]
        closes[650] = 500.0  # the best close, before the previous session
        meta = {"symbol": "^NSEBANK", "fiftyTwoWeekHigh": 512.0}
        raw = json.dumps({"chart": {"result": [{"meta": meta, "timestamp": [int(d.timestamp()) for d in days],
                                                "indicators": {"quote": [{"close": closes, "high": [c + 1 for c in closes]}]}}]}}).encode()
        out = daily.record_history("^NSEBANK", days[-1].date().isoformat(), "2026-12-31", fetcher=lambda url: raw)
        self.assertEqual(out["max_close_before_previous"], [days[650].date().isoformat(), 500.0])
        self.assertEqual(out["first"], "2024-01-01"); self.assertEqual(out["fifty_two_week_high"], 512.0)
        with self.assertRaises(Exception):
            daily.record_history("^NSEI", "2026-11-17", "2026-11-18", fetcher=lambda url: raw)  # wrong instrument


class DelayedSensexTests(unittest.TestCase):
    def test_bars_after_a_delayed_quote_are_not_reported_missing(self):
        r = daily.report("sensex", build(("sensex",)), INTRADAY)
        s = r["indices"][0]
        self.assertEqual((s["intraday_bars"], s["completed_bars"], s["expected_bars"]), ("complete", 1, 1))
        self.assertEqual(s["bars_through"], "2026-11-18T09:20:00+05:30")
        text = daily.markdown(r)
        self.assertNotIn("bars received", text); self.assertNotIn("opening range is omitted", text)

    def test_india_daily_compares_sensex_and_nifty_at_the_same_bar_end(self):
        r = daily.report("india-daily", build(), INTRADAY)
        m = r["same_moment"]
        self.assertEqual(m["at"], "2026-11-18T09:20:00+05:30")
        nifty_prev = r["indices"][0]["levels"]["previous_close"]
        self.assertAlmostEqual(m["nifty_pct"], round((25395.40 / nifty_prev - 1) * 100, 3))
        self.assertAlmostEqual(m["sensex_pct"], round((82540.19 / 82532.19 - 1) * 100, 3))
        text = daily.markdown(r)
        self.assertIn("| Sensex (Yahoo) | 18 Nov 09:20 (~15 min behind) |", text)
        self.assertIn("Sensex's free feed runs ~15 min behind NSE, so compare like for like at the same 5-minute bar, ending 09:20 IST "
                      f"(Yahoo, both indices): Sensex -0.31%, Nifty 50 {m['nifty_pct']:+.2f}%.", text)

    def test_sensex_only_report_fetches_nifty_bars_for_the_comparison(self):
        b = build(("sensex",))
        self.assertIn("comparison_bars", b)
        r = daily.report("sensex", b, INTRADAY)
        self.assertEqual(r["same_moment"]["at"], "2026-11-18T09:20:00+05:30")
        self.assertIn("Nifty 50 is now", daily.markdown(r))

    def test_fresh_nse_levels_need_no_comparison(self):
        r = daily.report("nifty", build(("nifty",)), INTRADAY)
        self.assertIsNone(r["same_moment"])


class ConditionAndProvisionalTests(unittest.TestCase):
    def rows(self, closes, start="2026-11-18T09:15:00+05:30"):
        t = instant(start)
        return [{"at": (t + timedelta(minutes=5 * k)).isoformat(), "close": c} for k, c in enumerate(closes)]

    def test_trigger_time_is_the_start_of_the_current_run_only(self):
        rows = self.rows([100, 95, 101, 96, 94, 93])  # dipped below 97 at 09:20, recovered, then below again from 09:30
        self.assertEqual(daily.trigger_times(rows, 110, 97), (None, "2026-11-18T09:35:00+05:30"))
        self.assertEqual(daily.trigger_times(self.rows([100, 95, 99]), 110, 97), (None, None))  # back inside: nothing met now
        self.assertEqual(daily.trigger_times(self.rows([100, 111, 112]), 110, 97), ("2026-11-18T09:25:00+05:30", None))

    def test_report_says_bearish_condition_met_at_time(self):
        bundle = build(("nifty",))
        bars = bundle["indices"]["nifty"]["candles"]["rows"]
        low = bundle["indices"]["nifty"]["previous"]["low"]
        bars[3].update(open=low - 50, high=low - 40, low=low - 60, close=low - 45)  # the 09:30-09:35 bar closes below the previous low
        bundle["indices"]["nifty"].pop("quote", None)
        r = daily.report("nifty", bundle, INTRADAY)
        n = r["indices"][0]
        self.assertEqual(n["condition"], "BELOW_REFERENCE_RANGE")
        text = daily.markdown(r)
        self.assertIn(f"Bearish condition met at 09:35 IST: every completed 5-minute close since then is below {low:,.2f} (17 Nov low), "
                      "the previous session's low - a sign of weakness.", text)
        self.assertIn("the bearish condition no longer applies", text)
        self.assertNotIn("bullish on a completed 5-minute close above", text)

    def test_provisional_levels_for_the_next_session_mid_session(self):
        r = daily.report("nifty", build(("nifty",)), INTRADAY)
        n = r["indices"][0]; lv = n["levels"]
        expected = (f"Provisional levels for Thu 19 Nov if the session ended now: high {lv['session_high']:,.2f}, low {lv['session_low']:,.2f}, "
                    f"last {n['value']:,.2f}; final after 15:30 IST.")
        self.assertIn(expected, daily.markdown(r)); self.assertIn(expected, daily.markdown(r, brief=True))


class ClarityTests(unittest.TestCase):
    def test_bank_nifty_report_drops_nifty_weekly_expiry_and_sensex_drops_nse_expiries(self):
        bank = daily.markdown(daily.report("banknifty", build(("banknifty",)), INTRADAY))
        self.assertNotIn("Nifty 50 weekly", bank); self.assertIn("monthly expiry moves from Tue 24 Nov", bank)
        sensex = daily.markdown(daily.report("sensex", build(("sensex",)), INTRADAY))
        self.assertNotIn("expiry", sensex); self.assertIn("NSE closed (Prakash Gurpurb", sensex)
        nifty = daily.markdown(daily.report("nifty", build(("nifty",)), INTRADAY))
        self.assertIn("Next weekly expiry: Tue 1 Dec (Nifty 50 weekly).", nifty)

    def test_sensex_vix_comes_from_nse_when_its_snapshot_is_reachable(self):
        r = daily.report("sensex", build(("sensex",)), INTRADAY)
        self.assertEqual(r["vix"]["source"], "nse_live_allindices")
        self.assertIn("India VIX: NSE live index data.", daily.markdown(r))

    def test_one_short_disclaimer_and_compact_combined_report(self):
        text = daily.markdown(daily.report("india-daily", build(), INTRADAY))
        self.assertEqual(text.count("Research only; no orders."), 1)
        self.assertNotIn("Nothing was submitted", text); self.assertNotIn("Spot-index points only", text)
        self.assertEqual(text.count("## Levels"), 1); self.assertNotIn("| Level | Points |", text)  # one levels table for all three
        self.assertIn("| Level | Nifty 50 | Sensex (Yahoo) | Bank Nifty |", text)
        self.assertLessEqual(len(text.splitlines()), 70)


class LiveDriversTests(unittest.TestCase):
    """market_movers.live_drivers on the synthetic cap-weighted market, including a Bank Nifty built from 12 members."""
    @classmethod
    def setUpClass(cls):
        nse_live.clear_cache()
        cls.m = BankMarket()
        snap = nse_live.parse_all_indices(cls.m.snapshot(), MOVERS_NOW)
        cls.d = mm.live_drivers(["NIFTY 50", "NIFTY BANK"], snap, nse_fetch=cls.m.nse, yahoo_fetch=cls.m.yahoo)

    def test_nifty_drags_and_bank_heavyweights(self):
        n, b = self.d["indices"]["NIFTY 50"], self.d["indices"]["NIFTY BANK"]
        self.assertEqual(n["negative"][0]["symbol"], "S00")
        self.assertAlmostEqual(n["estimated_total_points"], n["actual_points"], delta=abs(n["actual_points"]) * 0.05 + 0.5)
        self.assertEqual([x["symbol"] for x in b["heavyweights"][:3]], ["S00", "S01", "S02"])
        self.assertAlmostEqual(b["estimated_total_points"], b["actual_points"], delta=abs(b["actual_points"]) * 0.05 + 0.5)
        self.assertEqual(b["observed"], 12)
        self.assertEqual([x["name"] for x in self.d["sectors_down"]], ["Nifty Metal", "Nifty Bank"])

    def test_sectors_survive_missing_stock_closes(self):
        nse_live.clear_cache()
        snap = nse_live.parse_all_indices(self.m.snapshot(), MOVERS_NOW)
        d = mm.live_drivers(["NIFTY 50"], snap, nse_fetch=lambda url: (_ for _ in ()).throw(nse_live.Missing("x")) if "bhavdata" in url else self.m.nse(url),
                            yahoo_fetch=self.m.yahoo)
        self.assertEqual(d["indices"], {}); self.assertTrue(d["sectors_up"]); self.assertIn("error", d)


class BankMarket(Market):
    BANK = 12

    def __init__(self):
        super().__init__()
        members = self.symbols[:self.BANK]
        self.bank = [sum(sh * self.prices[s][t] for sh, s in zip(self.shares, self.symbols) if s in members) for t in range(len(self.stamps))]

    def chart(self, symbol):
        if symbol != "^NSEBANK": return super().chart(symbol)
        meta = {"symbol": symbol, "currency": "INR", "regularMarketPrice": self.bank[-1], "regularMarketTime": self.stamps[-1] + 299}
        return {"chart": {"result": [{"meta": meta, "timestamp": self.stamps, "indicators": {"quote": [{"close": self.bank}]}}]}}

    def snapshot(self):
        raw = json.loads(super().snapshot())
        for row in raw["data"]:
            if row["index"] == "NIFTY BANK":
                last, prev = self.bank[-1], self.bank[self.prev_t]; ch = round(last - prev, 2)
                row.update(last=round(last, 2), previousClose=round(prev, 2), variation=ch, percentChange=round(ch / prev * 100, 2),
                           open=round(prev, 2), high=round(max(last, prev) + 1, 2), low=round(min(last, prev) - 1, 2))
        return json.dumps(raw).encode()

    def nse(self, url):
        if url == nse_live.NIFTYBANK_LIST:
            return ("Company Name,Industry,Symbol,Series,ISIN Code\n" + "".join(f"{s} Ltd.,Bank,{s},EQ,INE{k:03d}\n" for k, s in enumerate(self.symbols[:self.BANK]))).encode()
        return super().nse(url)


if __name__ == "__main__":
    unittest.main()
