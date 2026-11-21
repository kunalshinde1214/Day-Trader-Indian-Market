"""Round-6 market fixes: a bad Yahoo 5-minute tick no longer sets the Sensex day high or pivots (the provider's own day
high/low first, isolated spikes cut back against the reference index), 'Possible reasons' and look-ahead 'Cues' after
the close, and market_movers' closed wording, broader-market line and second sector headline. Offline fakes only."""
from datetime import datetime, timedelta
import json
from pathlib import Path
import sys
import unittest
from urllib.parse import parse_qs, unquote, urlsplit

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT / "tests"))
from core import IST, instant
import daily
import market_movers as mm
import nse_live
from public_market import collect_daily
from test_daily_close import AFTER, after_close_fetcher
from test_daily_enrich import FakeClosing, FakeNSE
from test_daily_round2 import fake_drivers, fake_records
from test_market_movers import DAYS, FLOWS, Market
from test_market_round5 import gmt, rss
from test_nse_live import all_indices_json

START = datetime(2026, 11, 18, 9, 15, tzinfo=IST)
SPIKE_AT = 73  # 15:20 IST
# 18 Nov 2026 as Yahoo served it: the 15:20 ^BSESN bar's high 82,817.57 while Nifty 50 sat flat; Yahoo's own day high 82,578.87.
SPIKE_BAR = (82017.390625, 82817.5703125, 81944.359375, 82244.6015625)
DAY = {"open": 82376.5625, "high": 82578.87, "low": 81780.47, "close": 82031.81}


def sensex_bars():
    rows = []
    for k in range(75):
        if k == SPIKE_AT: rows.append(SPIKE_BAR); continue
        mid = 82300.0 - 6 * k  # a quiet drift from ~82,300 to ~81,860
        rows.append((mid, mid + 20, mid - 20, mid - 6))
    rows[0] = (82376.56, 82571.81, 82212.63, 82450.0)  # opening bars inside Yahoo's day range
    return rows


def nifty_bars():
    return [(25220.45, 25221.0, 25219.9, 25220.45) if k >= 70 else (25300.0 - k, 25306.0 - k, 25294.0 - k, 25299.0 - k) for k in range(75)]


def bar_rows(values):
    return [{"at": (START + timedelta(minutes=5 * k)).isoformat(), "open": o, "high": h, "low": l, "close": c} for k, (o, h, l, c) in enumerate(values)]


def spike_fetcher(meta=True):
    """Yahoo as on 18 Nov after the close: Sensex 5-minute bars with the 15:20 spike, flat Nifty 50 bars, and (``meta``)
    the range=1d daily candle whose meta carries Yahoo's own day high/low."""
    base = after_close_fetcher()

    def fetch(url):
        symbol = unquote(urlsplit(url).path.rsplit("/", 1)[-1]); qs = parse_qs(urlsplit(url).query)
        interval, span = qs["interval"][0], qs.get("range", [""])[0]
        if span in ("1d", "5d") and interval == "1d":
            if not meta or symbol != "^BSESN": raise OSError("not in fixture")
            at = int(datetime(2026, 11, 18, 15, 32, 36, tzinfo=IST).timestamp())
            m = {"symbol": "^BSESN", "currency": "INR", "regularMarketPrice": DAY["close"], "chartPreviousClose": 82532.19,
                 "regularMarketTime": at, "regularMarketDayHigh": DAY["high"], "regularMarketDayLow": DAY["low"]}
            q = {k: [DAY[k]] for k in ("open", "high", "low", "close")}
            return json.dumps({"chart": {"result": [{"meta": m, "timestamp": [int(START.timestamp())], "indicators": {"quote": [q]}}]}}).encode()
        data = json.loads(base(url))
        if interval == "1d" and symbol == "^BSESN":  # today's daily candle (collect_daily's official close)
            q = data["chart"]["result"][0]["indicators"]["quote"][0]
            for name in ("open", "high", "low", "close"): q[name][-1] = DAY[name]
        if interval == "5m":
            values = sensex_bars() if symbol == "^BSESN" else nifty_bars()
            q = data["chart"]["result"][0]["indicators"]["quote"][0]
            for k, name in enumerate(("open", "high", "low", "close")): q[name] = [v[k] for v in values]
        return json.dumps(data).encode()
    return fetch


def enriched(selected, yahoo, news=False):
    nse_live.clear_cache()
    bundle = collect_daily(list(selected), fetcher=yahoo, clock=lambda: AFTER)
    daily.enrich_public(bundle, list(selected), AFTER, nse_fetch=FakeNSE(today_file=True), yahoo_fetch=yahoo, closing=FakeClosing(),
                        drivers=fake_drivers(), records=fake_records(), news=news)
    return bundle


def pivots(h, l, c):
    p = (h + l + c) / 3
    return {"S2": p - (h - l), "S1": 2 * p - h, "P": p, "R1": 2 * p - l, "R2": p + (h - l)}


# ---------------------------------------------------------------- fix 1: bad Yahoo ticks

class DespikeTests(unittest.TestCase):
    def test_the_1520_sensex_spike_is_cut_back_when_nifty_did_not_move(self):
        rows, fixes = daily.despike(bar_rows(sensex_bars()), bar_rows(nifty_bars()))
        self.assertEqual(rows[SPIKE_AT]["high"], SPIKE_BAR[3])  # back to the bar's close (its body top)
        self.assertEqual(rows[SPIKE_AT]["low"], SPIKE_BAR[2])   # the low wick (0.09%) is ordinary
        self.assertEqual(len(fixes), 1)
        self.assertEqual((fixes[0]["kind"], fixes[0]["high"], fixes[0]["at"][11:16]), ("spike", SPIKE_BAR[1], "15:20"))
        self.assertLess(max(r["high"] for r in rows), DAY["high"])

    def test_a_real_move_shared_by_the_reference_index_is_kept(self):
        nifty = nifty_bars(); o, h, l, c = nifty[SPIKE_AT]
        nifty[SPIKE_AT] = (o, h * 1.006, l, c)  # Nifty 50 spiked with it: news, not a bad tick
        rows, fixes = daily.despike(bar_rows(sensex_bars()), bar_rows(nifty))
        self.assertEqual((rows[SPIKE_AT]["high"], fixes), (SPIKE_BAR[1], []))

    def test_without_a_reference_bar_a_wick_inside_the_day_range_is_kept(self):
        rows, fixes = daily.despike(bar_rows(sensex_bars()))
        self.assertEqual((rows[SPIKE_AT]["high"], fixes), (SPIKE_BAR[1], []))
        rows, fixes = daily.despike(bar_rows(sensex_bars()), day_high=DAY["high"], day_low=DAY["low"])  # after the close
        self.assertEqual(rows[SPIKE_AT]["high"], DAY["high"]); self.assertEqual(fixes[0]["kind"], "day_range")

    def test_small_wicks_and_malformed_rows_pass_through(self):
        rows = bar_rows(nifty_bars()) + [{"at": "bad", "open": 1}]
        out, fixes = daily.despike(rows, bar_rows(sensex_bars()))
        self.assertEqual((out, fixes), (rows, []))

    def test_low_side_spike_is_cut_back_too(self):
        values = sensex_bars(); values[40] = (82060.0, 82070.0, 81500.0, 82050.0)
        rows, fixes = daily.despike(bar_rows(values), bar_rows(nifty_bars()))
        self.assertEqual(rows[40]["low"], 82050.0); self.assertEqual(len(fixes), 2)


class SensexCloseRangeTests(unittest.TestCase):
    def test_sensex_after_close_uses_yahoos_day_high_low_and_correct_pivots(self):
        """The reviewer's case: R1 was 82,639 from the spike; Yahoo's day candle gives ~82,480."""
        b = enriched(("sensex",), spike_fetcher())
        self.assertEqual(b["reference_bars"]["index"], "nifty")  # Nifty 50 bars fetched for the spike check
        r = daily.report("sensex", b, AFTER)
        s = r["indices"][0]
        self.assertEqual((s["levels"]["session_high"], s["levels"]["session_low"], s["levels"]["session_open"]), (DAY["high"], DAY["low"], DAY["open"]))
        self.assertEqual(s["range_basis"], "close_source")
        text = daily.markdown(r)
        self.assertIn("18 Nov range: 81,780.47-82,578.87.", text); self.assertNotIn("82,817.57 |", text)
        p = pivots(DAY["high"], DAY["low"], DAY["close"])
        self.assertIn(f"R1 {p['R1']:,.2f}", text); self.assertEqual(f"{p['R1']:,.2f}", "82,480.30")
        self.assertIn("Sensex: bad tick in the 5-minute bars - the 15:20 bar's high 82,817.57", text)
        self.assertIn("while Nifty 50's bar did not move with it", text)

    def test_without_the_day_candle_the_cleaned_bars_set_the_range(self):
        b = enriched(("sensex",), spike_fetcher(meta=False))
        s = daily.report("sensex", b, AFTER)["indices"][0]
        self.assertEqual(s["range_basis"], "five_minute_bars")
        self.assertEqual(s["levels"]["session_high"], 82571.81)  # the opening bar, not the 15:20 spike
        self.assertEqual(s["bar_fixes"][0]["high"], SPIKE_BAR[1])

    def test_india_daily_cleans_sensex_against_its_own_nifty_bars(self):
        b = enriched(("nifty", "sensex", "banknifty"), spike_fetcher())
        self.assertNotIn("reference_bars", b)  # the bundle already has every index's bars
        r = daily.report("india-daily", b, AFTER)
        sx = next(x for x in r["indices"] if x["index_id"] == "sensex")
        self.assertEqual(sx["levels"]["session_high"], DAY["high"])
        self.assertTrue(sx["bar_fixes"])
        self.assertFalse(any(x.get("bar_fixes") for x in r["indices"] if x["index_id"] != "sensex"))

    def test_spike_in_the_opening_bars_does_not_set_the_opening_range(self):
        values = sensex_bars(); values[1] = (82400.0, 83200.0, 82390.0, 82410.0)  # 09:20 bad tick, Nifty quiet
        bundle = {"indices": {"sensex": {"candles": {"rows": bar_rows(values)}}, "nifty": {"candles": {"rows": bar_rows(nifty_bars())}}}}
        data, fixes = daily.clean_bars("sensex", bundle["indices"]["sensex"], bundle, "intraday", "2026-11-18")
        self.assertEqual(max(r["high"] for r in data["candles"]["rows"][:3]), 82571.81)
        self.assertEqual(bundle["indices"]["sensex"]["candles"]["rows"][1]["high"], 83200.0)  # the bundle itself is untouched
        self.assertEqual(fixes[0]["reference"], "Nifty 50")

    def test_yahoo_quote_reads_todays_open_from_the_daily_candle(self):
        q = nse_live.yahoo_quote("SENSEX", fetcher=spike_fetcher(), clock=lambda: AFTER)
        self.assertEqual((q["open"], q["high"], q["low"]), (DAY["open"], DAY["high"], DAY["low"]))
        stale = nse_live.yahoo_quote("SENSEX", fetcher=spike_fetcher(), clock=lambda: AFTER + timedelta(days=1))
        self.assertEqual(stale["open"], DAY["open"])  # same day as the quote itself
        def old_candle(url):
            data = json.loads(spike_fetcher()(url)); data["chart"]["result"][0]["timestamp"] = [int((START - timedelta(days=1)).timestamp())]
            return json.dumps(data).encode()
        self.assertIsNone(nse_live.yahoo_quote("SENSEX", fetcher=old_candle, clock=lambda: AFTER)["open"])


class FinalChartCacheTests(unittest.TestCase):
    def test_after_the_session_todays_charts_are_kept_and_not_downloaded_again(self):
        import tempfile
        m = Market(); calls = []
        today = DAYS[-1]
        def fetch(url):
            calls.append(url)
            data = m.chart(url.split("/chart/")[1].split("?")[0])
            data["chart"]["result"][0]["meta"]["regularMarketTime"] = int(datetime(2026, 11, 18, 15, 31, tzinfo=IST).timestamp())
            return json.dumps(data).encode()
        evening = datetime(2026, 11, 18, 16, 5, tzinfo=IST)
        with tempfile.TemporaryDirectory() as d:
            first = mm.yahoo_charts(["S00.NS"], fetcher=fetch, cache=d, today=today, now=evening)["S00.NS"]
            self.assertTrue((Path(d) / "yahoo_5m" / "S00.NS.final.json").exists())
            second = mm.yahoo_charts(["S00.NS"], fetcher=fetch, cache=d, today=today, now=evening + timedelta(minutes=20))["S00.NS"]
            self.assertEqual(len(calls), 1)  # the second post-close run downloads nothing
            self.assertEqual(mm.series(second), mm.series(first)); self.assertEqual(second["meta"]["regularMarketPrice"], first["meta"]["regularMarketPrice"])
            calls.clear()
            mm.yahoo_charts(["S00.NS"], fetcher=fetch, cache=d, today=today, now=datetime(2026, 11, 18, 15, 50, tzinfo=IST))
            self.assertEqual(len(calls), 1)  # before 16:00 the kept chart is not trusted
            calls.clear()
            mm.yahoo_charts(["S00.NS"], fetcher=fetch, cache=d, today=today + timedelta(days=1), now=evening + timedelta(days=1))
            self.assertIn("range=1mo", calls[0])  # another day: downloaded again

    def test_a_chart_whose_last_trade_is_not_from_today_is_not_kept(self):
        import tempfile
        m = Market()
        def fetch(url):
            data = m.chart(url.split("/chart/")[1].split("?")[0])
            data["chart"]["result"][0]["meta"]["regularMarketTime"] = int(datetime(2026, 11, 17, 15, 29, tzinfo=IST).timestamp())
            return json.dumps(data).encode()
        with tempfile.TemporaryDirectory() as d:
            mm.yahoo_charts(["S00.NS"], fetcher=fetch, cache=d, today=DAYS[-1], now=datetime(2026, 11, 18, 16, 5, tzinfo=IST))
            self.assertFalse((Path(d) / "yahoo_5m" / "S00.NS.final.json").exists())


class VixPercentTests(unittest.TestCase):
    def test_india_vix_uses_nses_own_percent_change(self):
        """NSE computes +0.25% from unrounded levels; 15.52 -> 15.56 computed here would read +0.26% (the movers report said +0.25%)."""
        data = {"source": "nse_live_allindices", "symbol": "^INDIAVIX", "as_of": AFTER.isoformat(), "at": "2026-11-18T15:30:00+05:30",
                "rows": [{"session": "2026-11-17", "close": 15.52}, {"session": "2026-11-18", "close": 15.56, "change_pct": 0.25}]}
        self.assertEqual(daily.vix_report(data, AFTER, "close", "2026-11-18")["change_pct"], 0.25)
        data["rows"][1]["change_pct"] = 3.0  # disagrees with the levels: computed instead
        self.assertAlmostEqual(daily.vix_report(data, AFTER, "close", "2026-11-18")["change_pct"], 0.258, places=3)


# ---------------------------------------------------------------- fix 2: reasons and look-ahead cues after the close

CLOSE_CUES = {"GIFT": [("GIFT Nifty signals gap-down start for Sensex, Nifty - Moneycontrol.com", gmt("08:10")),
                       ("GIFT Nifty rises 40 points in evening trade - ET", gmt("17:10"))],
              "futures": [("U.S. stock futures rise as Nvidia earnings beat lifts AI sentiment - Seeking Alpha", gmt("14:42"))],
              "Wall": [], "gold": [("Gold, silver prices crash to nearly a year's low - BusinessLine", gmt("13:30"))],
              "crude": [("Oil prices fall on US inventory rise - Anadolu", gmt("07:58"))],
              "rupee": [("Rupee ends flat after hitting seven-month high - Reuters", gmt("15:37"))]}


def close_news_fetch(url):
    if "economictimes" in url: return rss([])
    q = url.split("q=")[1].split("&")[0].lower()
    if "sensex+nifty" in q: return rss([("Profit booking after nine-month highs drags Sensex down 500 points - CNBC TV18", gmt("15:41"))])
    if "titan" in q: return rss([("Titan shares slide after gold prices slump to 14-month lows - Mint", gmt("10:40"))])
    for key, items in CLOSE_CUES.items():
        if key.lower() in q: return rss(items)
    return rss([])


class CloseNewsTests(unittest.TestCase):
    NOW = instant("2026-11-18T17:30:00+05:30")

    def test_cues_after_the_close_look_ahead(self):
        since, by = mm.cue_window("close", "2026-11-17", "2026-11-18")
        self.assertEqual((since.isoformat(), by["GIFT Nifty"].isoformat()), ("2026-11-18T09:15:00+05:30", "2026-11-18T15:30:00+05:30"))
        cues = mm.market_cues(self.NOW, since=since, since_by=by, news_fetch=close_news_fetch)
        self.assertIn("evening trade", cues["GIFT Nifty"]["headline"])  # not the morning gap-down call
        self.assertIsNone(cues["Crude"])  # 07:58, before today's open
        self.assertIn("Rupee ends flat", cues["Rupee"]["headline"])
        self.assertEqual(mm.cue_window("intraday", "2026-11-17", "2026-11-18")[0].isoformat(), "2026-11-17T15:30:00+05:30")

    def test_one_failed_cue_search_leaves_the_others(self):
        def flaky(url):
            if "crude" in url: raise OSError("down")
            return close_news_fetch(url)
        cues = mm.market_cues(self.NOW, since=mm.session_open("2026-11-18"), news_fetch=flaky)
        self.assertIsNone(cues["Crude"]); self.assertIsNotNone(cues["Gold"])
        with self.assertRaises(OSError): mm.market_cues(self.NOW, news_fetch=lambda url: (_ for _ in ()).throw(OSError("down")))

    def test_news_context_awaits_a_cue_search_already_running(self):
        from concurrent.futures import ThreadPoolExecutor
        with ThreadPoolExecutor(1) as pool:
            early = pool.submit(lambda: {"Gold": None, "Rupee": {"headline": "x", "published_at": "2026-11-18T15:37:00+05:30"}})
            out = mm.news_context(self.NOW, {}, previous_session="2026-11-17", session="2026-11-18", news_fetch=close_news_fetch,
                                  phase="close", cues=early)
        self.assertEqual(list(out["cues"]), ["Gold", "Rupee"]); self.assertEqual(out["phase"], "close")
        self.assertEqual(out["cues_since_by"], {"GIFT Nifty": "2026-11-18T15:30:00+05:30"})

    def run_close(self, selected):
        calls = []
        def news(now, requests, **kw):
            calls.append(kw)
            return mm.news_context(self.NOW, requests, news_fetch=close_news_fetch, **kw)
        b = enriched(selected, spike_fetcher(), news=news)
        return calls, b

    def test_nifty_sensex_banknifty_and_india_daily_keep_reasons_and_cues_after_the_close(self):
        for selected, command in ((("nifty",), "nifty"), (("sensex",), "sensex"), (("banknifty",), "banknifty"),
                                  (("nifty", "sensex", "banknifty"), "india-daily")):
            calls, b = self.run_close(selected)
            self.assertEqual(calls[0]["phase"], "close", command)
            r = daily.report(command, b, AFTER)
            self.assertEqual(r["phase"], "close")
            text = daily.markdown(r)
            self.assertIn("Possible reasons (headlines", text, command)
            self.assertIn("Cues for Thu 19 Nov (look-ahead headlines, unverified; GIFT Nifty since today's 15:30 close, the rest since today's open): "
                          'GIFT Nifty - "GIFT Nifty rises 40 points in evening trade"', text, command)
            self.assertIn("Crude - none yet", text, command)
            self.assertIn("After the close the cues look ahead", text, command)
            self.assertIn("Cues for Thu 19 Nov", daily.markdown(r, brief=True), command)
        text = daily.markdown(daily.report("nifty", self.run_close(("nifty",))[1], AFTER))
        self.assertIn('Possible reasons (headlines, unverified): TITAN: "Titan shares slide after gold prices slump to 14-month lows" (Mint, '
                      '18 Nov 10:40 IST); "Profit booking after nine-month highs drags Sensex down 500 points" (CNBC TV18, 18 Nov 15:41 IST).', text)

    def test_failed_search_after_the_close_is_a_stated_gap(self):
        b = enriched(("nifty",), spike_fetcher(), news=lambda now, requests, **kw: {"reasons": {}, "cues": None, "gaps": ["cues", "reasons:nifty"]})
        text = daily.markdown(daily.report("nifty", b, AFTER))
        self.assertIn("Possible reasons (headlines): the news search failed or ran out of time this run - a gap", text)
        self.assertIn("Cues for Thu 19 Nov (look-ahead headlines, unverified; GIFT Nifty since today's 15:30 close, the rest since today's open): "
                      "the news search failed or ran out of time this run - a gap", text)

    def test_preopen_still_has_no_news_search(self):
        calls = []
        nse_live.clear_cache()
        from test_daily_reports import PREOPEN, public_bundle
        from test_daily_enrich import yahoo_quote_fetcher
        b = public_bundle(PREOPEN, ("nifty",))
        daily.enrich_public(b, ["nifty"], PREOPEN, nse_fetch=FakeNSE(), yahoo_fetch=yahoo_quote_fetcher(PREOPEN), closing=FakeClosing(),
                            drivers=fake_drivers(), records=fake_records(), news=lambda *a, **k: calls.append(1))
        self.assertEqual(calls, []); self.assertNotIn("news", b)


# ---------------------------------------------------------------- fix 3: market_movers after the close

CLOSE_NOW = datetime(2026, 11, 18, 15, 45, tzinfo=IST)


class ClosedMarket(Market):
    def snapshot(self):
        raw = json.loads(super().snapshot())
        raw["timestamp"] = "18-Nov-2026 15:30"
        def row(name, last, prev, adv):
            ch = round(last - prev, 2)
            return {"key": "BROAD MARKET INDICES", "index": name, "last": last, "variation": ch, "percentChange": round(ch / prev * 100, 2),
                    "previousClose": prev, "open": prev, "high": max(last, prev) + 1, "low": min(last, prev) - 1, "previousDay": "17-Nov-2026",
                    "advances": str(adv[0]), "declines": str(adv[1]), "unchanged": str(adv[2])}
        raw["data"] += [row("NIFTY MIDCAP 150", 22389.9, 22648.7, (29, 120, 1)), row("NIFTY SMALLCAP 250", 17667.5, 17754.85, (76, 174, 1)),
                        row("NIFTY 500", 23630.25, 23822.3, (125, 374, 2))]
        return json.dumps(raw).encode()


def movers_news(url):
    if "economictimes" in url: return rss([])
    q = url.split("q=")[1].split("&")[0].lower()
    if "metal+stocks" in q:
        return rss([("Metal stocks: Hindalco, Tata Steel shares - Upstox", gmt("14:00")),
                    ("Why Did Metal Stocks Fall by Up to 7% in Today's Trading Session? - Trade Brains", gmt("15:40")),
                    ("Metal stocks rally 3% as China stimulus lifts prices - ET", gmt("10:00"))])
    return rss([])


class MoversCloseTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        nse_live.clear_cache()
        cls.m = ClosedMarket()
        cls.r = mm.collect(nse_fetch=cls.m.nse, flow_fetch=lambda url: FLOWS, yahoo_fetch=cls.m.yahoo, clock=lambda: CLOSE_NOW, news=True,
                           news_fetch=movers_news)

    def test_after_1530_the_lead_says_closed(self):
        self.assertTrue(self.r["closed"])
        first = mm.summary(self.r, premise="up").splitlines()[0]
        self.assertTrue(first.startswith("Correction: the market actually closed **down**, not up. Nifty 50 closed **down** "), first)
        self.assertIn("today, Wed 18 Nov 2026 (NSE closing level, 15:30 IST).", first)
        self.assertNotIn("NSE live)", first)
        self.assertIn("# Market movers - Wed 18 Nov 2026 close", mm.markdown(self.r))
        self.assertIn("today's are published this evening", mm.summary(self.r))

    def test_broader_market_line(self):
        line = "Broader market (NSE, 18 Nov): Nifty Midcap 150 -1.14%, Nifty Smallcap 250 -0.49%; Nifty 500 stocks 125 up, 374 down, 2 unchanged."
        self.assertIn("- " + line, mm.summary(self.r))
        self.assertIn(line, mm.markdown(self.r))

    def test_the_weakest_sectors_get_one_cause_headline_each(self):
        heads = self.r["sector_headlines"]
        self.assertEqual([(h["sector"], h["headline"]) for h in heads],
                         [("Nifty Metal", "Why Did Metal Stocks Fall by Up to 7% in Today's Trading Session?")])  # not the rally or the list
        self.assertIn('  - Nifty Metal: "Why Did Metal Stocks Fall by Up to 7% in Today\'s Trading Session?" (Trade Brains, 18 Nov 15:40 IST)',
                      mm.summary(self.r))

    def test_sector_already_named_in_a_shown_headline_is_not_searched_again(self):
        calls = []
        shown = [{"headline": "Metal stocks drag Nifty as China demand slows"}]
        result = {"direction": {"change": -10.0}, "sectors": [{"name": "Nifty IT", "change_pct": 0.5}, {"name": "Nifty Metal", "change_pct": -2.4}]}
        out = mm.sector_headlines(result, shown, CLOSE_NOW, news_fetch=lambda url: calls.append(url) or rss([]))
        self.assertEqual((out, calls), ([], []))

    def test_during_the_session_the_wording_stays_live(self):
        from test_market_movers import NOW
        nse_live.clear_cache()
        r = mm.collect(nse_fetch=Market().nse, flow_fetch=lambda url: FLOWS, yahoo_fetch=Market().yahoo, clock=lambda: NOW, news=False)
        self.assertFalse(r["closed"])
        self.assertTrue(mm.summary(r, premise="up").startswith("Correction: the market is actually **down**, not up."))
        self.assertIsNone(mm.broader_text(r))  # the plain fixture has no broader indices


if __name__ == "__main__":
    unittest.main()
