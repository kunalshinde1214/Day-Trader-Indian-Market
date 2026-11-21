"""Round 8 (held-out market questions on a weekend): sector and market headlines on a closed day (search back to the
close of the session before, earlier wrap-ups dropped, a stated gap when none are collected), the closed-market banner
on every movers route, the closed-day daily block (next-session levels, pivots, point contributors, reasons, cues, GIFT
last trade, flows in words), mood context (valuation against its own year, 1-month move, VIX 52-week range, all-NSE and
Nifty 500 breadth, NSDL always), and the period lead. Offline fakes only; no sleeps."""
import contextlib
from datetime import date, datetime, timedelta
import io
import json
from pathlib import Path
import sys
import unittest
from unittest.mock import patch
import urllib.parse

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT / "tests"))
from core import Block, IST, instant
import daily
import market_movers as mm
import nse_live
from test_market_movers import FLOWS, Market
from test_market_round7 import METAL, PERIOD_CLOSES, PeriodNSE, bhav_rows
from test_nse_live import holiday_json

SAT = datetime(2026, 11, 21, 16, 0, tzinfo=IST)        # weekend; last session Fri 20 Nov
HOLIDAY = datetime(2026, 11, 24, 10, 0, tzinfo=IST)    # Tue 24 Nov, NSE holiday; last session Mon 23 Nov
NO_WAIT = patch.object(nse_live, "RETRY_DELAYS", (0.0, 0.0))


def rss(items):
    return f"<?xml version='1.0'?><rss><channel><title>x</title>{''.join(items)}</channel></rss>".encode()


def item(title, publisher, at):
    t = at.astimezone(IST).strftime("%a, %d %b %Y %H:%M:%S +0530")
    return f"<item><title>{title} - {publisher}</title><link>https://news.google.com/{abs(hash(title))}</link><pubDate>{t}</pubDate></item>"


# ---------------------------------------------------------------- helpers: window, quiet day, banner

class WindowTests(unittest.TestCase):
    def setUp(self): nse_live.clear_cache()

    def test_news_window_reaches_back_to_the_cut_off_and_is_capped(self):
        self.assertEqual(mm.news_window(SAT, None), ("when:1d", 24))
        self.assertEqual(mm.news_window(SAT, mm.session_close("2026-11-19")), ("when:3d", 50))  # Thu 15:30 to Sat 16:00
        self.assertEqual(mm.news_window(SAT, SAT - timedelta(hours=3)), ("when:1d", 24))
        self.assertEqual(mm.news_window(SAT, SAT - timedelta(days=30)), ("when:7d", 168))

    def test_quiet_since_weekend_holiday_and_before_the_open(self):
        nse = lambda url: holiday_json()
        fri, mon = date(2026, 11, 20), date(2026, 11, 23)
        self.assertTrue(mm.quiet_since(fri, SAT, nse_fetch=nse))
        self.assertTrue(mm.quiet_since(fri, datetime(2026, 11, 23, 8, 0, tzinfo=IST), nse_fetch=nse))     # Monday before 09:15
        self.assertFalse(mm.quiet_since(fri, datetime(2026, 11, 23, 9, 20, tzinfo=IST), nse_fetch=nse))   # Monday's session opened
        self.assertTrue(mm.quiet_since(mon, HOLIDAY, nse_fetch=nse))                                       # Tue 24 Nov holiday
        self.assertFalse(mm.quiet_since(SAT.date(), SAT, nse_fetch=nse))
        self.assertEqual(mm.next_session_after(mon, HOLIDAY, nse_fetch=nse), date(2026, 11, 25))
        down = lambda url: (_ for _ in ()).throw(Block("NSE: source refused the request"))
        self.assertTrue(mm.quiet_since(fri, SAT, nse_fetch=down))  # without the list: next weekday

    def test_banner_names_the_day_reason_last_session_and_next_day(self):
        info = mm.closed_banner_info(HOLIDAY, "2026-11-23", nse_fetch=lambda url: holiday_json())
        self.assertEqual(mm.closed_banner(info), "**Market closed today (Tue 24 Nov 2026: Prakash Gurpurb Sri Guru Nanak Dev) — figures are "
                                                 "from the last session, Mon 23 Nov 2026.** Next trading day: Wed 25 Nov 2026.")
        nse_live.clear_cache()
        self.assertIsNone(mm.closed_banner_info(datetime(2026, 11, 23, 8, 0, tzinfo=IST), "2026-11-20", nse_fetch=lambda url: holiday_json()))
        down = lambda url: (_ for _ in ()).throw(Block("x"))
        self.assertEqual(mm.closed_banner_info(SAT, "2026-11-20", nse_fetch=down)["reason"], "weekend")

    def test_earlier_session_wrap_ups_are_recognised(self):
        def h(t, at): return {"headline": t, "published_at": at.isoformat()}
        thu_eve, fri_noon = datetime(2026, 11, 19, 17, 10, tzinfo=IST), datetime(2026, 11, 20, 12, 0, tzinfo=IST)
        self.assertTrue(mm.earlier_session_report(h("Nifty Auto Surges 4.4% as Maruti Suzuki Jumps 7.6%", thu_eve), "2026-11-20"))
        self.assertTrue(mm.earlier_session_report(h("Sensex and Nifty Rally as Auto Sector Leads Gains", thu_eve), "2026-11-20"))
        self.assertFalse(mm.earlier_session_report(h("Nifty Auto Surges 4.4% as Maruti Suzuki Jumps 7.6%", fri_noon), "2026-11-20"))
        self.assertFalse(mm.earlier_session_report(h("Maruti to raise car prices from December", thu_eve), "2026-11-20"))  # news, no move
        self.assertFalse(mm.earlier_session_report(h("Copper jumps 3% on China stimulus", thu_eve), "2026-11-20"))      # overnight driver


# ---------------------------------------------------------------- 1. sector "why" on a weekend

class WeekendNSE:
    """The sector route on Sat 21 Nov: NSE's snapshot still carries Friday's close; bhavcopies for 19 and 20 Nov."""
    def __init__(self):
        self.calls = []

    def __call__(self, url):
        self.calls.append(url)
        if url == nse_live.HOLIDAYS: return holiday_json()
        if url == nse_live.ALL_INDICES:
            def row(name, pct):
                prev = 1000.0; last = round(prev * (1 + pct / 100), 2)
                return {"key": "SECTORAL INDICES", "index": name, "last": last, "variation": round(last - prev, 2), "percentChange": pct,
                        "previousClose": prev, "open": prev, "high": max(last, prev) + 1, "low": min(last, prev) - 1, "previousDay": "19-Nov-2026"}
            rows = [row("NIFTY 50", 1.12), row("NIFTY METAL", 2.0), row("NIFTY IT", -0.5)]
            return json.dumps({"data": rows, "timestamp": "20-Nov-2026 15:30"}).encode()
        if url == nse_live.INDEX_LIST.format("niftymetal"):
            return ("Company Name,Industry,Symbol,Series,ISIN Code\n" + "".join(f"{n},Metals,{s},EQ,INE{k:03d}\n" for k, (s, n, _, _) in enumerate(METAL))).encode()
        if url == nse_live.INDEX_CLOSE.format("20112026"):
            return ("Index Name,Index Date,Open Index Value,High Index Value,Low Index Value,Closing Index Value,Points Change,Change(%)\n"
                    "Nifty 50,20-11-2026,1000,1012,999,1011.2,11.2,1.12\nNifty Metal,20-11-2026,1000,1021,999,1020,20,2\n").encode()
        if url == nse_live.BHAVCOPY.format("20112026"):
            return bhav_rows(date(2026, 11, 20), [(s, c, p) for s, _, p, c in METAL])  # metals rose on Friday
        raise nse_live.Missing("NSE: no file for that date")


def weekend_news(urls):
    thu_eve, fri, sat = datetime(2026, 11, 19, 17, 0, tzinfo=IST), datetime(2026, 11, 20, 11, 0, tzinfo=IST), datetime(2026, 11, 21, 9, 0, tzinfo=IST)
    sector = [item("Metal stocks surge 4% as copper jumps", "Old Wrap", thu_eve),
              item("Why metal stocks are rising today: SAIL, Vedanta lead", "Business Today", fri),
              item("Metal stocks extend gains as China stimulus lifts copper", "Mint", sat)]
    def fetch(url):
        urls.append(url)
        return rss(sector if "metal" in urllib.parse.unquote_plus(url).lower() else [])
    return fetch


class SectorWeekendTests(unittest.TestCase):
    def setUp(self): nse_live.clear_cache()

    def test_weekend_sector_why_searches_back_to_the_session_before_and_drops_its_wrap_ups(self):
        urls = []
        r = mm.sector_report("metal", nse_fetch=WeekendNSE(), clock=lambda: SAT, news_fetch=weekend_news(urls))
        self.assertEqual((r["session"], r["previous_session"]), ("2026-11-20", "2026-11-19"))
        self.assertEqual(r["news_since"], "2026-11-19T15:30:00+05:30")
        self.assertTrue(urls and all("when%3A3d" in u for u in urls), urls)  # Thu 15:30 to Sat 16:00
        heads = [h["headline"] for h in r["headlines"]]
        self.assertIn("Why metal stocks are rising today: SAIL, Vedanta lead", heads)
        self.assertNotIn("Metal stocks surge 4% as copper jumps", heads)  # Thursday evening's wrap-up of Thursday
        text = mm.sector_summary(r)
        self.assertTrue(text.startswith("**Market closed today (Sat 21 Nov 2026: weekend) — figures are from the last session, Fri 20 Nov 2026.**"), text)
        self.assertIn("Nifty Metal closed **up** 2.00%", text)
        self.assertIn("Possible causes (Google News headlines since the 19 Nov close, the session before Fri 20 Nov", text)
        self.assertNotIn("Headlines not collected", text)
        self.assertIn("**Market closed today", mm.sector_markdown(r))

    def test_failed_search_and_past_session_print_the_gap_line_in_the_summary(self):
        def down(url): raise OSError("news down")
        r = mm.sector_report("metal", nse_fetch=WeekendNSE(), clock=lambda: SAT, news_fetch=down)
        self.assertIn("- Headlines not collected (the news search failed or ran out of time this run", mm.sector_summary(r))
        nse_live.clear_cache()
        monday = datetime(2026, 11, 23, 11, 0, tzinfo=IST)  # Monday's session has opened: Friday is a past session
        r = mm.sector_report("metal", session="2026-11-20", nse_fetch=WeekendNSE(), clock=lambda: monday, news_fetch=down)
        self.assertNotIn("headlines", r)
        self.assertIn("- Headlines not collected (Fri 20 Nov 2026 is a past session", mm.sector_summary(r))
        self.assertIsNone(r["market_closed"])
        nse_live.clear_cache()
        r = mm.sector_report("metal", nse_fetch=WeekendNSE(), clock=lambda: SAT, news=False)
        self.assertIn("Headlines not collected (headline search skipped (--no-news))", mm.sector_summary(r))


# ---------------------------------------------------------------- 2. market-wide movers on the night before the next open

class QuietMoversTests(unittest.TestCase):
    """Session 17 Nov reported at 08:00 on Wed 18 Nov (no session has opened since): news since the 16 Nov close."""
    def setUp(self): nse_live.clear_cache()

    def run_collect(self, fetch):
        m = Market()
        wed_early = datetime(2026, 11, 18, 8, 0, tzinfo=IST)
        with NO_WAIT:
            return mm.collect("2026-11-17", nse_fetch=m.nse, flow_fetch=lambda u: FLOWS, yahoo_fetch=m.yahoo, clock=lambda: wed_early,
                              news=True, news_fetch=fetch)

    def test_market_headlines_reach_back_and_drop_wrap_ups_and_opposite_index_stories(self):
        urls, probe = [], self.run_collect(lambda url: rss([]))
        up = probe["direction"]["change"] > 0
        same, other = ("rises", "falls") if up else ("falls", "rises")
        mon_eve, tue = datetime(2026, 11, 16, 17, 0, tzinfo=IST), datetime(2026, 11, 17, 12, 0, tzinfo=IST)
        items = [item(f"Sensex {same} 300 pts as metal stocks move", "Wrap", mon_eve),   # Monday evening: Monday's move
                 item(f"Sensex {same} 250 pts as FIIs return", "Mint", tue),
                 item(f"Sensex {other} 300 pts in early trade on weak global cues", "Wire", tue - timedelta(hours=2))]
        def fetch(url):
            urls.append(url)
            return rss(items) if "Sensex+Nifty" in url else rss([])
        nse_live.clear_cache()
        r = self.run_collect(fetch)
        self.assertEqual(r["previous_session"], "2026-11-16")
        self.assertTrue(any("Sensex+Nifty+when%3A2d" in u for u in urls), urls)
        heads = [h["headline"] for h in r["headlines"]]
        self.assertEqual(heads, [f"Sensex {same} 250 pts as FIIs return"])
        self.assertIn("published after the 16 Nov close", r["headline_filter"])

    def test_all_shares_breadth_nsdl_gap_and_no_headline_gap_are_in_every_summary(self):
        r = self.run_collect(lambda url: rss([]))
        prices = mm.bhavcopy(date(2026, 11, 17), fetcher=Market().nse)["prices"]
        b = r["all_breadth"]
        self.assertEqual(b["advancing"] + b["declining"] + b["unchanged"], len(prices))
        self.assertEqual(b["advancing"], sum(p["close"] > p["previous_close"] for p in prices.values()))
        text = mm.summary(r)
        self.assertIn(f"- All NSE shares (Tue 17 Nov bhavcopy, EQ series): {b['advancing']:,} up", text)
        self.assertIn("- FPI equity month to date (NSDL): not collected this run", text)
        self.assertIn("- Headlines: none since the 16 Nov close names a moving stock or sector or a macro driver - a gap", text)
        r2 = self.run_collect(lambda url: (_ for _ in ()).throw(OSError("down")))
        self.assertIn("- Headlines not collected (the news search failed this run", mm.summary(r2))


# ---------------------------------------------------------------- 3. mood context

def close_file(day, nifty, pe, pb, dy=1.3, vix=None):
    head = "Index Name,Index Date,Open Index Value,High Index Value,Low Index Value,Closing Index Value,Points Change,Change(%),Volume,Turnover (Rs. Cr.),P/E,P/B,Div Yield\n"
    rows = f"Nifty 50,{day:%d-%m-%Y},{nifty},{nifty},{nifty},{nifty},0,0,1,1,{pe},{pb},{dy}\n"
    if vix: rows += f"India VIX,{day:%d-%m-%Y},{vix},{vix},{vix},{vix},0,0,-,-,-,-,-\n"
    return (head + rows).encode()


class ValuationNSE:
    """ind_close_all files with P/E for 20 Nov 2026, 19 Oct 2026 and every month's last weekday back to Nov 2025."""
    def __init__(self):
        self.files = {date(2026, 11, 20): (25800.6, 21.02, 3.17, 1.32), date(2026, 10, 19): (22888.95, 19.33, 2.81, 1.5),
                      date(2026, 10, 30): (23053.1, 19.3, 2.83, 1.42), date(2025, 11, 28): (26202.95, 22.81, 3.56, 1.28)}
        for k in range(1, 13):
            end = mm._month_end(mm._month_back(date(2026, 11, 20), k))
            while end.weekday() >= 5: end -= timedelta(days=1)
            self.files.setdefault(end, (24000.0 + k, 20.5, 3.0, 1.3))

    def __call__(self, url):
        if url == nse_live.HOLIDAYS: return holiday_json()
        if "ind_close_all_" in url:
            d = datetime.strptime(url[-12:-4], "%d%m%Y").date()
            if d in self.files: return close_file(d, *self.files[d])
        raise nse_live.Missing("x")


class MoodTests(unittest.TestCase):
    def setUp(self): nse_live.clear_cache()

    def test_valuation_against_its_own_year_and_the_one_month_change(self):
        v = mm.valuation_context(date(2026, 11, 21), nse_fetch=ValuationNSE(), now=SAT)  # Saturday: Friday's file
        self.assertEqual((v["session"], v["pe"], v["pb"], v["div_yield"]), ("2026-11-20", 21.02, 3.17, 1.32))
        self.assertEqual(v["range"]["pe"], {"low": 19.3, "low_session": "2026-10-30", "high": 22.81, "high_session": "2025-11-28"})
        self.assertEqual(v["month_ago"], {"session": "2026-10-19", "close": 22888.95})
        result = {"valuation": v, "direction": {"value": 25800.6}, "mode": "session", "session": "2026-11-20",
                  "vix": {"value": 17.32}, "vix_year": {"high": 28.91, "low": 8.72, "basis": "NSE 52-week high/low"}}
        text = " ".join(mm.mood_parts(result))
        self.assertIn("Nifty 50 valuation (NSE, Fri 20 Nov close): P/E 21.02 vs its past-year range 19.30 (Oct 2026) to 22.81 (Nov 2025)", text)
        self.assertIn("P/B 3.17 vs its past-year range", text); self.assertIn("dividend yield 1.32%", text)
        self.assertIn("Nifty 50 over one month: +12.72% (22,888.95 at the Mon 19 Oct 2026 close to 25,800.60 at the Fri 20 Nov 2026 close)", text)
        self.assertIn("India VIX 17.32 vs its 52-week range 8.72-28.91 (NSE 52-week high/low): 43% of the way up the range.", text)

    def test_missing_files_and_vix_range_are_stated_gaps(self):
        with self.assertRaises(Block):
            mm.valuation_context(date(2026, 11, 21), nse_fetch=lambda url: (_ for _ in ()).throw(nse_live.Missing("x")), now=SAT)
        text = " ".join(mm.mood_parts({"mood_missing": "NSE's daily index files were unavailable or slow", "vix": {"value": 17.32}, "direction": {}}))
        self.assertIn("Valuation and 1-month change not collected this run", text)
        self.assertIn("India VIX 17.32; its 52-week range was not collected this run - a gap.", text)

    def test_vix_year_range_nse_first_then_yahoo_and_never_the_network_for_a_fake(self):
        snap = {"indices": {"INDIA VIX": {"year_high": 28.91, "year_low": 8.72}}}
        self.assertEqual(mm.vix_year_range(snap, live=False)["basis"], "NSE 52-week high/low")
        closes = [12.0 + (k % 50) / 5 for k in range(240)]
        def yahoo(url):
            self.assertEqual(url, mm.VIX_YEAR_URL)
            return json.dumps({"chart": {"result": [{"meta": {"symbol": "^INDIAVIX"}, "indicators": {"quote": [{"close": closes}]}}]}}).encode()
        got = mm.vix_year_range({"indices": {}}, yahoo_fetch=yahoo, live=False)
        self.assertEqual((got["low"], got["high"]), (min(closes), max(closes)))
        self.assertIn("Yahoo", got["basis"])
        self.assertIsNone(mm.vix_year_range(None, live=False))

    def test_index_close_parser_keeps_valuation_columns(self):
        rows = nse_live.parse_index_closes(close_file(date(2026, 11, 20), 25800.6, 21.02, 3.17, vix=17.32), date(2026, 11, 20))
        self.assertEqual((rows["NIFTY 50"]["pe"], rows["NIFTY 50"]["pb"], rows["NIFTY 50"]["div_yield"]), (21.02, 3.17, 1.3))
        self.assertNotIn("pe", rows["INDIA VIX"])  # NSE prints '-' for VIX


# ---------------------------------------------------------------- 4/5. period: banner and lead

class PeriodRound8Tests(unittest.TestCase):
    def setUp(self): nse_live.clear_cache()

    def no_yahoo(self, url): raise Block("x")

    def test_named_indices_lead_with_their_gap_and_every_other_index_carries_one(self):
        r = mm.period_report("week", nse_fetch=PeriodNSE(), yahoo_fetch=self.no_yahoo, clock=lambda: SAT,
                             indices=mm.parse_period_indices(["midcaps", "and", "smallcaps"]))
        text = mm.period_summary(r)
        self.assertTrue(text.startswith("**Market closed today (Sat 21 Nov 2026: weekend) — figures are from the last session, Wed 18 Nov 2026.**"), text)
        self.assertIn("\n**This week** (Fri 13 Nov close to Wed 18 Nov close): Nifty Midcap 150 -1.28% (-1.61 pp vs Nifty 50); "
                      "Nifty Smallcap 250 -1.52% (-1.85 pp vs Nifty 50). For comparison, Nifty 50 +0.33% (25,144.30 to 25,227.95).", text)
        self.assertIn("- Midcaps and smallcaps: both lagged the Nifty.", text)
        self.assertIn("Nifty 500 -0.44% (-0.77 pp)", text)
        self.assertIn("weakest Nifty Metal -3.36% (-3.69 pp)", text)
        self.assertIn("**Market closed today", mm.period_markdown(r))

    def test_without_names_nifty_leads(self):
        r = mm.period_report("week", nse_fetch=PeriodNSE(), yahoo_fetch=self.no_yahoo, clock=lambda: SAT)
        self.assertIn("\n**This week** (Fri 13 Nov close to Wed 18 Nov close): Nifty 50 +0.33% (25,144.30 to 25,227.95).", mm.period_summary(r))

    def test_index_names_resolve_and_unknown_ones_are_refused(self):
        for words, want in ((["midcap", "150", "smallcap", "250"], ["NIFTY MIDCAP 150", "NIFTY SMALLCAP 250"]),
                            (["next", "50,", "sensex"], ["NIFTY NEXT 50", "SENSEX"]), (["IT", "bank"], ["NIFTY IT", "NIFTY BANK"]),
                            (["Nifty Midcap 100"], ["NIFTY MIDCAP 100"]), (["PSU", "banks"], ["NIFTY PSU BANK"]), (["nifty", "500"], ["NIFTY 500"])):
            self.assertEqual(mm.parse_period_indices(words), want, words)
        with self.assertRaises(Block): mm.parse_period_indices(["bitcoin"])
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.assertEqual(mm.main(["--indices", "midcaps"]), 2)
        self.assertIn("--indices goes with --period", out.getvalue())


# ---------------------------------------------------------------- 2/6. daily.py on a closed day

class ClosedNSE:
    def __call__(self, url):
        if url == nse_live.HOLIDAYS: return holiday_json()
        raise nse_live.Missing("x")


def closing_result(session, selected, include_gift):
    return {"schema": "india_closing_context_v1", "session": session, "as_of": HOLIDAY.isoformat(), "coverage": [], "missing_coverage": [],
            "indices": {"banknifty": {"status": "OBSERVED", "session": session, "close": 64635.2, "points_change": 1572.2, "change_pct": 2.49,
                                      "high": 64680.8, "low": 62598.55, "open": 62656.65}},
            "index_report": {"source": "https://nsearchives.nseindia.com/content/indices/ind_close_all_23112026.csv"},
            "institutional_flows": {"trading_date": session, "values": {"FII/FPI": {"net_crore": 10308.42}, "DII": {"net_crore": -4859.8}},
                                    "source": "https://www.nseindia.in/api/fiidiiTradeReact"},
            "nse_eq_breadth": {"advancing": 1046, "declining": 1682, "unchanged": 32},
            "gift": {"value": 25836.0, "change_points": 90.5, "change_pct": 0.35, "as_of": "2026-11-24T02:44:00+05:30", "freshness": "STALE",
                     "expiry": "2026-11-30", "timezone_basis": "IST assumed; source omits offset", "source": "https://www.nseindia.in/api/marketStatus"}}


def bank_block():
    rows = lambda *xs: [{"symbol": s, "change_pct": c, "points": p, "weight_pct": 10.0} for s, c, p in xs]
    return {"positive": rows(("HDFCBANK", 2.94, 346.7), ("AXISBANK", 4.72, 292.0), ("INDUSINDBK", 8.59, 239.2), ("ICICIBANK", 1.93, 200.0)),
            "negative": rows(("FEDERALBNK", -0.5, -3.1)), "estimated_total_points": 1566.4, "actual_points": 1572.2,
            "fit": {"r_squared": 0.97}, "heavyweights": []}


class ClosedDailyTests(unittest.TestCase):
    def setUp(self): nse_live.clear_cache()

    def run_closed(self, drivers=None, news=None):
        calls = []
        def fake_drivers(wanted, session, **kw):
            calls.append((wanted, session, kw.get("previous_session")))
            return {"at": "2026-11-23T15:30:00+05:30", "indices": {"NIFTY BANK": bank_block()},
                    "sector_levels": {"Nifty PSU Bank": 1.23, "Nifty Private Bank": 3.01}, "sectors_up": [], "sectors_down": []}
        def fake_news(now, requests, **kw):
            calls.append(("news", sorted(requests), kw["session"], kw["previous_session"], kw["phase"], kw["window"]))
            h = {"headline": "Why are banking stocks rising? Axis, IndusInd lead", "publisher": "Goodreturns", "published_at": "2026-11-23T14:13:00+05:30",
                 "symbol": "AXISBANK"}
            cue = {"headline": "Gift Nifty signals flat start", "publisher": "Mint", "published_at": "2026-11-23T17:19:00+05:30"}
            return {"reasons": {"banknifty": [h]}, "cues": {"GIFT Nifty": cue, "US markets": None}, "gaps": [], "phase": "close"}
        result, _ = daily.closed_day(["banknifty"], HOLIDAY, nse_fetch=ClosedNSE(), closing=closing_result,
                                     drivers=drivers or fake_drivers, news=news or fake_news)
        return result, calls

    def test_holiday_brief_gives_the_next_session_block(self):
        result, calls = self.run_closed()
        self.assertEqual(calls[0], (["NIFTY BANK"], "2026-11-23", "2026-11-20"))
        self.assertEqual(calls[1][:5], ("news", ["banknifty"], "2026-11-23", "2026-11-20", "close"))
        self.assertEqual(calls[1][5][0], "when:4d")  # since the Fri 20 Nov close, on Tue 24 Nov
        text = daily.closed_markdown(result, brief=True)
        self.assertTrue(text.startswith("**Market closed today (Tue 24 Nov 2026: Prakash Gurpurb Sri Guru Nanak Dev) — figures are from the last "
                                        "session, Mon 23 Nov 2026.** Next trading day: Wed 25 Nov 2026."), text)
        for phrase in ("- Bank Nifty (23 Nov close, NSE): 64,635.20, +1,572.20 (+2.49%); range 62,598.55-64,680.80.",
                       "- Bank sub-indices: PSU Bank +1.23%, Private Bank +3.01%.",
                       "- Top point contributors (estimated Bank Nifty points): HDFCBANK +346.7 pts (+2.94%), AXISBANK +292.0 pts (+4.72%), "
                       "INDUSINDBK +239.2 pts (+8.59%); against the move: FEDERALBNK -3.1 pts (-0.50%) (estimates add up to +1566.4 vs the actual +1572.2).",
                       "- Levels for Wed 25 Nov (next session): 23 Nov high 64,680.80, low 62,598.55, close 64,635.20.",
                       "- Classic pivots for Wed 25 Nov (from 23 Nov high/low/close; arithmetic, not a forecast): S2 61,889.27, S1 63,262.23, "
                       "P 63,971.52, R1 65,344.48, R2 66,053.77.",
                       "- Possible reasons (headlines, unverified): AXISBANK: \"Why are banking stocks rising? Axis, IndusInd lead\" (Goodreturns, 23 Nov 14:13 IST).",
                       "- Cues for Wed 25 Nov (look-ahead headlines, unverified; GIFT Nifty since the 23 Nov close, the rest since that session's open): "
                       "GIFT Nifty - \"Gift Nifty signals flat start\" (Mint, 23 Nov 17:19 IST); US markets - none yet.",
                       "- All NSE shares (23 Nov): 1,046 up, 1,682 down, 32 unchanged.",
                       "- Institutional flows (23 Nov, provisional NSE data): FII/FPI +₹10,308.42 cr (net buyers); DII −₹4,859.80 cr (net sellers).",
                       "- GIFT Nifty futures (last trade Tue 24 Nov 02:44 IST; expiry Mon 30 Nov): 25,836.00, +90.50 (+0.35%)"):
            self.assertIn(phrase, text)
        for jargon in ("STALE", "older than 3 minutes", "Not index membership", "finality unverified", "₹-"):
            self.assertNotIn(jargon, text)
        full = daily.closed_markdown(result)
        self.assertIn("# Bank Nifty - market closed today, Tue 24 Nov 2026", full)
        self.assertIn("- NSE share counts cover every EQ-series share in the session's bhavcopy, not any one index's members.", full)
        self.assertIn("NSE does not state a release time or finality", full)
        self.assertNotIn("STALE", full)

    def test_missing_drivers_say_rerun_and_injected_fetchers_never_reach_the_network(self):
        def broken(wanted, session, **kw): raise Block("NSE: source refused the request")
        result, _ = self.run_closed(drivers=broken)
        self.assertIn("⚠ stock contributions not collected — rerun", daily.closed_markdown(result, brief=True))
        nse_live.clear_cache()
        with patch.object(mm, "session_drivers", side_effect=AssertionError("network")), \
             patch.object(mm, "news_context", side_effect=AssertionError("network")):
            plain, _ = daily.closed_day(["banknifty"], HOLIDAY, nse_fetch=ClosedNSE(), closing=closing_result)
        self.assertNotIn("drivers", plain); self.assertNotIn("news", plain)
        self.assertIn("Levels for Wed 25 Nov (next session)", daily.closed_markdown(plain, brief=True))

    def test_flow_and_gift_wording(self):
        self.assertEqual(daily.flow_text("DII", -4859.8), "DII −₹4,859.80 cr (net sellers)")
        self.assertEqual(daily.flow_text("FII/FPI", 10308.42), "FII/FPI +₹10,308.42 cr (net buyers)")
        g = closing_result("2026-11-23", ["banknifty"], True)["gift"]
        self.assertIn("last trade Tue 24 Nov 02:44 IST", daily.gift_line(g, closed=True))
        self.assertIn("Tue 24 Nov 02:44 IST, stale", daily.gift_line(g))


class SessionDriversTests(unittest.TestCase):
    def setUp(self): nse_live.clear_cache()

    def test_point_contributions_from_the_sessions_bhavcopy_and_index_file(self):
        m = Market()
        with NO_WAIT:
            d = mm.session_drivers(["NIFTY 50"], "2026-11-17", nse_fetch=m.nse, yahoo_fetch=m.yahoo, previous_session="2026-11-16")
        b = d["indices"]["NIFTY 50"]
        self.assertEqual((b["observed"], b["expected"]), (50, 50))
        close = nse_live.index_closes(date(2026, 11, 17), fetcher=m.nse)["NIFTY 50"]
        self.assertEqual(b["actual_points"], close["change"])
        self.assertAlmostEqual(b["estimated_total_points"], b["actual_points"], delta=abs(b["actual_points"]) * 0.1 + 1)
        self.assertEqual(d["sector_levels"], {"Nifty IT": 1.0, "Nifty Metal": -3.0})
        self.assertEqual(d["at"], "2026-11-17T15:30:00+05:30")
        nse_live.clear_cache()
        def no_bhav(url):
            if "sec_bhavdata_full" in url: raise Block("NSE: source refused the request")
            return m.nse(url)
        with NO_WAIT:
            gap = mm.session_drivers(["NIFTY 50"], "2026-11-17", nse_fetch=no_bhav, yahoo_fetch=m.yahoo)
        self.assertEqual(gap["indices"], {}); self.assertIn("bhavcopy", gap["error"])


if __name__ == "__main__":
    unittest.main()
