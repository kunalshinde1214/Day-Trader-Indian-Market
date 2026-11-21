"""Round-5 market fixes: same-day 'why' (causal headline ranking, housekeeping and contradicting multi-day stories dropped,
a 'Possible reasons' and a 'Cues' line in the daily briefings), one Sensex headline figure, deterministic factsheet
weights, the Nifty Bank factsheet anchor, and small consistency fixes (check times, record-close data start, cached
past days of Yahoo 5-minute bars). Offline fakes only."""
from datetime import date, datetime, timedelta
import json
from pathlib import Path
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT / "tests"))
from core import IST, instant
import daily
import market_movers as mm
import nse_live
from test_daily_enrich import FakeClosing, FakeNSE as EnrichNSE, yahoo_quote_fetcher
from test_daily_reports import INTRADAY, public_bundle
from test_daily_round2 import build, fake_drivers, fake_records
from test_market_movers import Market
from test_market_round4 import industries, inputs, synthetic_anchor, true_weights

NOW = instant("2026-11-18T13:45:00+05:30")


def gmt(ist_hhmm, day=18):
    """'13:42' IST on 18 Nov -> an RSS pubDate in GMT."""
    t = datetime(2026, 11, day, int(ist_hhmm[:2]), int(ist_hhmm[3:]), tzinfo=IST) - timedelta(hours=5, minutes=30)
    return t.strftime("%a, %d %b %Y %H:%M:%S GMT")


def rss(items):
    return ("<rss><channel>" + "".join(f"<item><title>{t}</title><link>https://news.google.com/rss/articles/{k}</link><pubDate>{d}</pubDate></item>"
                                       for k, (t, d) in enumerate(items)) + "</channel></rss>").encode()


TITAN_FEED = [("Nifty Consumer Durables index slides 3%; Titan, Kalyan Jewellers crack 5% - Business Standard", gmt("13:42")),
              ("Titan Company Share Price - Upstox", gmt("13:28")),
              ("Titan, Kalyan Jewellers shares fall up to 4% as gold, silver prices crash - Moneycontrol.com", gmt("11:59")),
              ("Titan shares slide after gold prices slump to 14-month lows - Mint", gmt("10:40"))]
HDFC_FEED = [("HDFC Bank slips; stock options granted at Tuesday's close price - BusinessLine", gmt("12:52")),
             ("HDFC Bank shares fall 1% amid FII selling in private lenders - ET", gmt("11:10"))]
MARKET_FEED = [("Nifty Rallies 13% As FIIs Pour Rs 88,300 Cr Into Stock Market | How Much Upside Is Left? - Business Today", gmt("13:02")),
               ("Stock Market Today: Sensex Falls 620 Points, Nifty 50 Below 25,200; Titan Tanks 5% - NDTV Profit", gmt("12:46")),
               ("Why Stock Market Is Falling Today? Sensex Drops Over 700 Points, Nifty 50 Below 25,200. Key Reasons Explained - NDTV Profit", gmt("13:39")),
               ("Sensex rises 350 pts from day's low, Nifty reclaims 25,250: Key reasons behind markets paring losses - Moneycontrol.com", gmt("14:32", 18)),
               ("Sensex, Nifty open lower - ANI", gmt("09:20"))]
CUE_FEEDS = {"GIFT": [("GIFT Nifty falls 133 pts, signals weak start for Sensex, Nifty - Moneycontrol.com", gmt("08:09"))],
             "futures": [("U.S. stock futures rise as Nvidia earnings beat lifts AI sentiment - Seeking Alpha", gmt("12:42"))],
             "Wall": [("Walmart Earnings Preview: Wall Street's Highest Price Target Points to $155 - TradingKey", gmt("13:36"))],
             "gold": [("Gold Rate Today November 18: Check latest Gold prices in Delhi, Mumbai - BusinessLine", gmt("13:31")),
                      ("Gold, silver prices in Indian markets crash to nearly a year's low - BusinessLine", gmt("13:30"))],
             "crude": [("Oil prices fall on US inventory rise, supply glut concerns - Anadolu", gmt("13:08"))],
             "rupee": [("Sri Lanka rupee at 332.70/80 to US dollar spot - EconomyNext", gmt("10:34")),
                       ("Rupee gains 24 paise to 93.27 against US dollar in early trade - The Hindu", gmt("09:50"))]}


def news_fetch(url, calls=None):
    if calls is not None: calls.append(url)
    if "economictimes" in url: return rss([])
    q = url.split("q=")[1].split("&")[0]
    if "titan" in q.lower(): return rss(TITAN_FEED)
    if "hdfc" in q.lower(): return rss(HDFC_FEED)
    if "Sensex+Nifty" in q: return rss(MARKET_FEED)
    for key, items in CUE_FEEDS.items():
        if key.lower() in q.lower(): return rss(items)
    return rss([])


# ---------------------------------------------------------------- fix 1a: causal ranking and filters

class HeadlineRankingTests(unittest.TestCase):
    def test_causal_headline_ranks_before_a_newer_one_that_only_reports_the_move(self):
        got = mm.stock_headlines([{"symbol": "TITAN", "name": "Titan Company Ltd.", "change_pct": -4.3}], NOW, news_fetch=news_fetch,
                                 since=mm.session_close("2026-11-17"), session="2026-11-18")
        heads = [h["headline"] for h in got]
        self.assertEqual(heads[0], "Titan, Kalyan Jewellers shares fall up to 4% as gold, silver prices crash")
        self.assertEqual(len(heads), 2)  # a second headline only because it also gives a cause, and is a different story
        self.assertIn("gold prices slump", heads[1]); self.assertNotIn("crack 5%", " ".join(heads))

    def test_corporate_housekeeping_is_not_a_reason(self):
        got = mm.stock_headlines([{"symbol": "HDFCBANK", "name": "HDFC Bank Ltd.", "change_pct": -0.9}], NOW, news_fetch=news_fetch,
                                 since=mm.session_close("2026-11-17"), session="2026-11-18")
        self.assertEqual([h["headline"] for h in got], ["HDFC Bank shares fall 1% amid FII selling in private lenders"])
        only_admin = lambda url: rss([HDFC_FEED[0]])
        self.assertEqual(mm.stock_headlines([{"symbol": "HDFCBANK", "name": "HDFC Bank Ltd.", "change_pct": -0.9}], NOW, news_fetch=only_admin), [])
        for h in ("Board meeting intimation for fund raising", "Infosys allots shares under ESOP scheme", "Trading window closure",
                  "Notice of AGM and e-voting", "EGM on December 5"):
            self.assertTrue(mm.ADMIN_RE.search(h), h)
        self.assertFalse(mm.ADMIN_RE.search("Titan shares fall as gold prices crash"))

    def test_multi_day_story_against_todays_move_is_dropped(self):
        rally = "Nifty Rallies 13% As FIIs Pour Rs 88,300 Cr Into Stock Market | How Much Upside Is Left?"
        self.assertTrue(mm.contradicts(rally, -150.0, index_story=True))
        self.assertFalse(mm.contradicts(rally, +150.0, index_story=True))  # same direction: not contradicting
        self.assertFalse(mm.contradicts("Stock markets fall in early trade after two-day rally as FIIs sell", -150.0, index_story=True))
        self.assertFalse(mm.contradicts("Sensex rises 350 pts from day's low: key reasons behind markets paring losses", -150.0, index_story=True))
        self.assertTrue(mm.contradicts("Bank stocks gain 11% this month on credit growth", -1.0))
        self.assertEqual(mm.headline_direction("Titan to Kalyan: Jewellery stocks plunge 4% as gold prices slide"), "down")

    def test_market_headlines_drop_the_rally_story_and_keep_the_explanation(self):
        terms = mm.mover_terms({"contributors": {"negative": [{"symbol": "TITAN", "name": "Titan Company Ltd."}]}, "sectors": []})
        picks, failed = mm.market_headlines(NOW, news_fetch=news_fetch, terms=terms, since=mm.session_close("2026-11-17"),
                                            session="2026-11-18", change=-220.0)
        heads = [p["headline"] for p in picks]
        self.assertNotIn("Nifty Rallies 13%", " ".join(heads))
        self.assertEqual(heads[0], "Why Stock Market Is Falling Today? Sensex Drops Over 700 Points, Nifty 50 Below 25,200. Key Reasons Explained")
        self.assertGreater(mm.causal_score(heads[0]), mm.causal_score("Sensex, Nifty open lower"))
        unfiltered, _ = mm.market_headlines(NOW, news_fetch=news_fetch, since=mm.session_close("2026-11-17"), session="2026-11-18")
        self.assertIn("Nifty Rallies 13%", " ".join(p["headline"] for p in unfiltered))  # only today's direction drops it


# ---------------------------------------------------------------- fix 1b: reasons and cues in the daily briefings

class ReasonsAndCuesTests(unittest.TestCase):
    MOVERS = [{"symbol": "TITAN", "name": "Titan Company Ltd.", "change_pct": -4.3, "points": -20.9},
              {"symbol": "HDFCBANK", "name": "HDFC Bank Ltd.", "change_pct": -0.9, "points": -24.6}]

    def test_possible_reasons_lead_with_the_top_movers_cause(self):
        items, failed = mm.possible_reasons(NOW, change=-220.0, movers=self.MOVERS, sectors=[], session="2026-11-18",
                                            previous_session="2026-11-17", news_fetch=news_fetch)
        self.assertEqual(len(items), 3)
        self.assertEqual((items[0]["symbol"], items[0]["headline"]), ("TITAN", "Titan, Kalyan Jewellers shares fall up to 4% as gold, silver prices crash"))
        self.assertNotIn("stock options", " ".join(h["headline"] for h in items)); self.assertNotIn("Rallies 13%", " ".join(h["headline"] for h in items))

    def test_cues_pick_one_dated_headline_each_and_skip_noise(self):
        cues = mm.market_cues(NOW, since=mm.session_close("2026-11-17"), news_fetch=news_fetch)
        self.assertEqual(list(cues), ["GIFT Nifty", "US markets", "Gold", "Crude", "Rupee"])
        self.assertIn("GIFT Nifty falls 133 pts", cues["GIFT Nifty"]["headline"])
        self.assertIn("U.S. stock futures rise", cues["US markets"]["headline"])  # not the Walmart preview
        self.assertIn("crash to nearly a year's low", cues["Gold"]["headline"])  # not the city price list
        self.assertIn("Rupee gains 24 paise", cues["Rupee"]["headline"])  # not the Sri Lankan rupee

    def test_news_context_is_bounded_and_states_the_gap(self):
        gate = threading.Event()
        def slow(url):
            gate.wait(5); return rss([])
        try:
            out = mm.news_context(NOW, {"nifty": {"change": -220.0, "movers": self.MOVERS, "sectors": []}}, previous_session="2026-11-17",
                                  session="2026-11-18", news_fetch=slow, budget=0.3)
        finally:
            gate.set()
        self.assertEqual(sorted(out["gaps"]), ["cues", "reasons:nifty"]); self.assertEqual(out["reasons"], {}); self.assertIsNone(out["cues"])

    def bundle(self, selected, news):
        nse_live.clear_cache()
        bundle = public_bundle(INTRADAY, selected)
        def drivers(indices, snap, **kw):
            out = fake_drivers()(indices, snap, **kw)
            out["sector_levels"] = {**out["sector_levels"], "Nifty Metal": -1.74, "Nifty Media": 0.54}
            return out
        daily.enrich_public(bundle, list(selected), INTRADAY, nse_fetch=EnrichNSE(), yahoo_fetch=yahoo_quote_fetcher(INTRADAY),
                            closing=FakeClosing(), drivers=drivers, records=fake_records(), news=news)
        return bundle

    def test_nifty_report_prints_reasons_and_cues_after_the_drivers(self):
        calls = []
        def news(now, requests, **kw):
            calls.append((requests, kw))
            return mm.news_context(NOW, requests, news_fetch=news_fetch, **kw)  # the fake headlines are from later in the day
        text = daily.markdown(daily.report("nifty", self.bundle(("nifty",), news), INTRADAY))
        requests, kw = calls[0]
        self.assertEqual(list(requests), ["nifty"]); self.assertEqual(kw["previous_session"], "2026-11-17")
        self.assertEqual([m["symbol"] for m in requests["nifty"]["movers"]], ["TITAN", "BAJFINANCE", "INFY"])  # the side of the move
        head = text.split("| Level | Points |")[0]
        self.assertLess(head.index("What's moving it"), head.index("Possible reasons (headlines, unverified): "))
        self.assertIn('TITAN: "Titan, Kalyan Jewellers shares fall up to 4% as gold, silver prices crash" (Moneycontrol.com, 18 Nov 11:59 IST)', head)
        self.assertIn("Cues (headlines since the 17 Nov close, unverified): GIFT Nifty - \"GIFT Nifty falls 133 pts", head)
        self.assertIn("- Possible reasons and cues: Google News RSS", text)  # data note
        self.assertIn("- Possible reasons (headlines, unverified): ", daily.markdown(daily.report("nifty", self.bundle(("nifty",), news), INTRADAY), brief=True))

    def test_bank_nifty_and_india_daily_ask_for_their_own_reasons(self):
        seen = []
        def news(now, requests, **kw):
            seen.append(requests); return {"reasons": {k: [] for k in requests}, "cues": {"Gold": None}, "gaps": []}
        bank = daily.markdown(daily.report("banknifty", self.bundle(("banknifty",), news), INTRADAY))
        self.assertEqual([x["name"] for x in seen[0]["banknifty"]["sectors"]], ["Nifty PSU Bank", "Nifty Private Bank"])  # bank sub-indices only
        self.assertIn("Possible reasons (headlines): no dated headline since the 17 Nov close names today's movers - a gap, not an all-clear.", bank)
        self.assertIn("Cues: no dated headline since the 17 Nov close on Gold - a gap.", bank)
        combined = daily.markdown(daily.report("india-daily", self.bundle(("nifty", "sensex", "banknifty"), news), INTRADAY))
        self.assertEqual(list(seen[1]), ["market"]); self.assertIn("Possible reasons (headlines)", combined)

    def test_failed_or_slow_search_is_a_stated_gap(self):
        gap = lambda now, requests, **kw: {"reasons": {}, "cues": None, "gaps": ["cues", "reasons:nifty"]}
        text = daily.markdown(daily.report("nifty", self.bundle(("nifty",), gap), INTRADAY))
        self.assertIn("Possible reasons (headlines): the news search failed or ran out of time this run - a gap", text)
        self.assertIn("Cues (GIFT Nifty, US markets, gold, crude, rupee): the news search failed or ran out of time this run - a gap.", text)

    def test_injected_fetchers_never_search_the_live_news(self):
        b = build(("nifty",))
        self.assertNotIn("news", b)
        self.assertNotIn("Possible reasons", daily.markdown(daily.report("nifty", b, INTRADAY)))


class SkillDocTests(unittest.TestCase):
    def test_web_search_guidance_puts_the_full_date_in_the_query(self):
        for rel in ("skills/market/SKILL.md", "references/web-research.md"):
            text = (ROOT / rel).read_text(encoding="utf-8")
            self.assertIn("stock market today 18 November 2026", text, rel)
            self.assertIn("earlier years", text, rel); self.assertIn("Possible reasons", text, rel)
        for name in ("market", "sensex", "nifty", "india-daily"):
            desc = (ROOT / "skills" / name / "SKILL.md").read_text(encoding="utf-8").split("---")[1]
            self.assertNotIn(": ", desc.split("description:", 1)[1], name)


# ---------------------------------------------------------------- fix 2: one Sensex headline figure

class SensexFigureTests(unittest.TestCase):
    def test_sensex_report_leads_with_one_figure_and_moves_the_comparison_to_the_notes(self):
        text = daily.markdown(daily.report("sensex", build(("sensex",)), INTRADAY))
        head, notes = text.split("## Data notes")
        lead = [l for l in head.splitlines() if l.startswith("Sensex ≈ ")]
        self.assertEqual(len(lead), 1)
        self.assertRegex(lead[0], r"^Sensex ≈ \*\*[\d,]+\*\* \([+-]\d\.\d\d%\) at 09:35 IST \(estimate from live Nifty 50\); "
                                  r"last Yahoo quote [\d,]+\.\d\d \([+-]\d\.\d\d%\) at 09:20 IST, vs 17 Nov close 82,532.19\.")
        self.assertNotIn("At 09:20 IST (Yahoo quote", head); self.assertNotIn("compare like for like", head)
        self.assertNotIn("Estimated Sensex now ≈", head)
        self.assertIn("- Like-for-like check: Sensex's free feed runs", notes)

    def test_india_daily_has_one_sensex_line(self):
        text = daily.markdown(daily.report("india-daily", build(), INTRADAY))
        head = text.split("## Levels")[0]
        self.assertEqual(sum(l.startswith("Sensex ≈ ") for l in head.splitlines()), 1)
        self.assertNotIn("compare like for like", head)
        self.assertIn("Like-for-like check:", text.split("## Data notes")[1])
        self.assertIn("- Sensex ≈ ", daily.markdown(daily.report("india-daily", build(), INTRADAY), brief=True))


# ---------------------------------------------------------------- fix 3: deterministic factsheet weights

class DeterministicWeightTests(unittest.TestCase):
    def test_rolled_forward_top_weights_do_not_depend_on_the_fit_window(self):
        m = Market(); charts, prev, ip = inputs(m); anchor = synthetic_anchor(m)
        ind = industries(m)
        short = {k: {**v, "timestamp": v["timestamp"][40:], "indicators": {"quote": [{"close": v["indicators"]["quote"][0]["close"][40:]}]}}
                 for k, v in charts.items()}
        a = mm.anchored_weights(charts, "^NSEI", prev, ip, anchor, ind)
        b = mm.anchored_weights(short, "^NSEI", prev, ip, anchor, ind)
        for s in ("S00", "S01", "S02"):
            g = (prev[s + ".NS"] / anchor["closes"][s]) / (ip / anchor["index_close"])
            self.assertEqual(a["weights"][s + ".NS"], b["weights"][s + ".NS"])  # identical, run after run
            self.assertAlmostEqual(a["weights"][s + ".NS"], anchor["top"][s] / 100 * g, places=12)  # factsheet x price ratio, unscaled
        self.assertAlmostEqual(sum(a["weights"].values()), 1.0, places=9); self.assertAlmostEqual(sum(b["weights"].values()), 1.0, places=9)
        self.assertNotEqual(a["weights"]["S30.NS"], b["weights"]["S30.NS"])  # the fitted remainder still follows its fit


# ---------------------------------------------------------------- fix 4: Nifty Bank factsheet anchor

class BankAnchorTests(unittest.TestCase):
    def test_shipped_file_carries_the_30_oct_nifty_bank_factsheet(self):
        raw = json.loads((ROOT / "assets" / "index_weights.json").read_text(encoding="utf-8"))["NIFTY BANK"]
        self.assertEqual(raw["as_of"], "2026-10-30"); self.assertIn("Nifty Bank factsheet", raw["source"])
        top = raw["top_constituents_pct"]
        self.assertEqual((top["HDFCBANK"], top["ICICIBANK"], top["KOTAKBANK"], top["AXISBANK"], top["SBIN"]), (18.18, 14.99, 11.28, 9.97, 9.94))
        self.assertEqual(len(top), 10); self.assertEqual(raw["sectors_pct"], {"Financial Services": 100.0})
        self.assertEqual(len(raw["members"]), 14); self.assertEqual(set(raw["reference_closes"]), set(raw["members"]))
        self.assertEqual(raw["index_close"], 54686.95)
        a = mm.load_anchor(mm.BANK, today=date(2026, 11, 18))
        self.assertEqual(a["index"], mm.BANK); self.assertEqual(a["top"]["HDFCBANK"], 18.18)

    def test_bank_nifty_contributions_are_anchored_and_labelled(self):
        m = Market(); charts, prev, ip = inputs(m)
        charts = {("^NSEBANK" if k == "^NSEI" else k): v for k, v in charts.items()}
        anchor = {**synthetic_anchor(m), "index": mm.BANK}
        changes = {s: {"symbol": s, "name": s, "change_pct": round((m.prices[s][-1] / m.prices[s][m.prev_t] - 1) * 100, 2)} for s in m.symbols}
        with patch.object(mm, "load_anchor", lambda index=mm.INDEX, today=None, path=None: anchor if index == mm.BANK else None):
            c = mm.contributor_block(charts, "^NSEBANK", changes, prev, ip, live=False, actual=-10.0, industries=industries(m),
                                     today=date(2026, 11, 18), previous_day="2026-11-17")
        self.assertEqual(c["fit"]["anchor"]["index"], mm.BANK); self.assertIn("Nifty Bank factsheet of 13 Nov 2026", c["method"])
        self.assertTrue(c["anchor_line"].startswith("Nifty Bank factsheet weights (13 Nov 2026) -> estimated at the 17 Nov close"))
        result = {"drivers": {"indices": {"NIFTY BANK": {**c, "actual_at": "2026-11-18T09:35:00+05:30"}}}, "indices": [], "events": [],
                  "coverage_gaps": [], "phase": "intraday", "vix": None}
        notes = "\n".join(daily.data_notes(result))
        self.assertIn("Bank Nifty weights anchored to the NSE Indices Nifty Bank factsheet of 13 Nov 2026", notes)
        self.assertIn("Nifty Bank factsheet weights (13 Nov 2026)", notes)


# ---------------------------------------------------------------- fix 5: times, record wording, cached past bars

class ConsistencyTests(unittest.TestCase):
    def test_check_time_differing_from_the_nse_time_is_labelled(self):
        result = {"drivers": fake_drivers()(["NIFTY 50"], {"as_of": "2026-11-18T13:40:00+05:30", "page": "x"}), "indices": [], "events": [],
                  "coverage_gaps": [], "phase": "intraday", "vix": None, "nse_live_at": "2026-11-18T13:40:00+05:30"}
        notes = "\n".join(daily.data_notes(result))
        self.assertIn("check times are Yahoo's (the stock-price moment), the index levels above are NSE at 13:40 IST", notes)
        same = {**result, "nse_live_at": result["drivers"]["indices"]["NIFTY 50"]["actual_at"]}
        self.assertNotIn("check times are", "\n".join(daily.data_notes(same)))

    def test_past_days_of_bars_are_cached_and_today_is_fetched_fresh(self):
        m = Market(); calls = []
        today = date(2026, 11, 18)
        def fetch(url):
            calls.append(url)
            sym = url.split("/chart/")[1].split("?")[0]
            full = m.chart(sym)
            if "range=1d" in url:  # only today's bars, with a newer last price
                r = full["chart"]["result"][0]; k = 225
                return json.dumps({"chart": {"result": [{"meta": {**r["meta"], "regularMarketPrice": 123.0}, "timestamp": r["timestamp"][k:],
                                                         "indicators": {"quote": [{"close": r["indicators"]["quote"][0]["close"][k:]}]}}]}}).encode()
            return json.dumps(full).encode()
        with tempfile.TemporaryDirectory() as d:
            first = mm.yahoo_charts(["S00.NS"], fetcher=fetch, cache=d, today=today)["S00.NS"]
            saved = json.loads((Path(d) / "yahoo_5m" / "S00.NS.json").read_text(encoding="utf-8"))
            self.assertEqual(saved["through"], "2026-11-17"); self.assertEqual(len(saved["t"]), 225)  # never today's forming bars
            second = mm.yahoo_charts(["S00.NS"], fetcher=fetch, cache=d, today=today)["S00.NS"]
            self.assertEqual(["range=1mo" in u for u in calls], [True, False])
            self.assertEqual(mm.series(second), mm.series(first)); self.assertEqual(second["meta"]["regularMarketPrice"], 123.0)
            calls.clear()
            mm.yahoo_charts(["S00.NS"], fetcher=fetch, cache=d, today=today + timedelta(days=1))  # a new day: full month again
            self.assertIn("range=1mo", calls[0])
        self.assertEqual(mm.CHART_BUDGET, 20.0)


if __name__ == "__main__":
    unittest.main()
