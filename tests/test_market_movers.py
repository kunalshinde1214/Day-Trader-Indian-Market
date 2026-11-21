"""market_movers: direction first (premise correction), weight estimate, point contributors, sectors, breadth,
flows, summary and session mode. Synthetic cap-weighted market; offline fakes only."""
import contextlib
from datetime import date, datetime, timedelta
import io
import json
from pathlib import Path
import random
import sys
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
from core import Block, IST, instant
import market_movers as mm
import nse_live

DAYS = (date(2026, 11, 13), date(2026, 11, 16), date(2026, 11, 17), date(2026, 11, 18))
TODAY_BARS = 30
NOW = datetime(2026, 11, 18, 9, 15, tzinfo=IST) + timedelta(minutes=5 * TODAY_BARS)


class Market:
    """50 stocks, an index = sum(shares x price) (true cap weighting), 5-minute bars for three sessions."""
    def __init__(self, seed=7, today_drift=-0.0003):
        rng = random.Random(seed)
        self.symbols = [f"S{k:02d}" for k in range(50)]
        raw = [1 / (k + 3) for k in range(50)]; total = sum(raw)
        self.true_weights0 = [r / total for r in raw]
        start = [100.0 + 7 * k for k in range(50)]
        self.shares = [w * 10000 / p for w, p in zip(self.true_weights0, start)]
        self.stamps, self.prices = [], {s: [] for s in self.symbols}
        current = list(start)
        for d in DAYS:
            n = TODAY_BARS if d == DAYS[-1] else 75
            for k in range(n):
                self.stamps.append(int((datetime(d.year, d.month, d.day, 9, 15, tzinfo=IST) + timedelta(minutes=5 * k)).timestamp()))
                factor = rng.gauss(today_drift if d == DAYS[-1] else 0, 0.0012)
                for i, s in enumerate(self.symbols):
                    current[i] *= 1 + factor + rng.gauss(0, 0.002) + (-0.001 if d == DAYS[-1] and i == 0 else 0)
                    self.prices[s].append(round(current[i], 4))
        self.index = [sum(sh * self.prices[s][t] for sh, s in zip(self.shares, self.symbols)) for t in range(len(self.stamps))]
        self.prev_t = 224  # last bar of 17 Nov

    def chart(self, symbol):
        if symbol == "^NSEI": closes = self.index
        else: closes = self.prices[symbol.removesuffix(".NS")]
        meta = {"symbol": symbol, "currency": "INR", "regularMarketPrice": closes[-1], "regularMarketTime": self.stamps[-1] + 299}
        return {"chart": {"result": [{"meta": meta, "timestamp": self.stamps, "indicators": {"quote": [{"close": closes}]}}]}}

    def yahoo(self, url):
        if "range=1d" in url: raise Block("YAHOO: not in fixture")
        symbol = url.split("/chart/")[1].split("?")[0].replace("%5E", "^")
        return json.dumps(self.chart(symbol)).encode()

    def bhav(self, d, t_close, t_prev):
        rows = ["SYMBOL, SERIES, DATE1, PREV_CLOSE, CLOSE_PRICE"] + [
            f"{s}, EQ, {d:%d-%b-%Y}, {self.prices[s][t_prev]}, {self.prices[s][t_close]}" for s in self.symbols]
        return ("\n".join(rows) + "\n").encode()

    def snapshot(self):
        def row(name, last, prev, key):
            ch = round(last - prev, 2)
            return {"key": key, "index": name, "last": round(last, 2), "variation": ch, "percentChange": round(ch / prev * 100, 2), "previousClose": round(prev, 2),
                    "open": round(prev, 2), "high": round(max(last, prev) + 1, 2), "low": round(min(last, prev) - 1, 2), "previousDay": "17-Nov-2026",
                    "advances": "10", "declines": "40", "unchanged": "0"}
        rows = [row("NIFTY 50", self.index[-1], self.index[self.prev_t], "INDICES ELIGIBLE IN DERIVATIVES"),
                row("INDIA VIX", 16.2, 15.5, "BROAD MARKET INDICES"), row("NIFTY IT", 101, 100, "SECTORAL INDICES"),
                row("NIFTY METAL", 97, 100, "SECTORAL INDICES"), row("NIFTY BANK", 99.5, 100, "INDICES ELIGIBLE IN DERIVATIVES")]
        return json.dumps({"data": rows, "timestamp": f"{NOW:%d-%b-%Y %H:%M}"}).encode()

    def nse(self, url):
        if url == nse_live.ALL_INDICES: return self.snapshot()
        if url == nse_live.NIFTY50_LIST:
            return ("Company Name,Industry,Symbol,Series,ISIN Code\n" + "".join(f"{s} Ltd.,Test,{s},EQ,INE{k:03d}\n" for k, s in enumerate(self.symbols))).encode()
        if url == nse_live.BHAVCOPY.format("17112026"): return self.bhav(DAYS[2], self.prev_t, 149)
        if url == nse_live.INDEX_CLOSE.format("17112026"):
            close, prev = self.index[self.prev_t], self.index[149]
            return (f"Index Name,Index Date,Open Index Value,High Index Value,Low Index Value,Closing Index Value,Points Change,Change(%)\n"
                    f"Nifty 50,17-11-2026,{prev:.2f},{max(close, prev) + 5:.2f},{min(close, prev) - 5:.2f},{close:.2f},{close - prev:.2f},{(close / prev - 1) * 100:.2f}\n"
                    f"Nifty IT,17-11-2026,100,102,99,101,1,1\nNifty Metal,17-11-2026,100,101,96,97,-3,-3\n").encode()
        raise nse_live.Missing("NSE: no file for that date")


FLOWS = json.dumps([{"category": "DII", "date": "17-Nov-2026", "buyValue": "100", "sellValue": "50", "netValue": "50"},
                    {"category": "FII/FPI", "date": "17-Nov-2026", "buyValue": "100", "sellValue": "4300", "netValue": "-4200"}]).encode()


class MoversTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        nse_live.clear_cache()
        cls.m = Market()
        cls.live = mm.collect(nse_fetch=cls.m.nse, flow_fetch=lambda url: FLOWS, yahoo_fetch=cls.m.yahoo, clock=lambda: NOW, news=False)

    def test_nnls_recovers_nonnegative_weights(self):
        rng = random.Random(3); true = [0.5, 0.3, 0.2, 0.0]
        X = [[rng.gauss(0, 1) for _ in true] for _ in range(400)]
        y = [sum(w * x for w, x in zip(true, row)) for row in X]
        A = [[sum(r[i] * r[j] for r in X) for j in range(4)] for i in range(4)]; b = [sum(r[i] * t for r, t in zip(X, y)) for i in range(4)]
        for got, want in zip(mm.nnls(A, b), true): self.assertAlmostEqual(got, want, places=4)

    def test_weight_estimate_matches_true_cap_weights_at_previous_close(self):
        m = self.m; t = m.prev_t
        true = {s: sh * m.prices[s][t] / m.index[t] for sh, s in zip(m.shares, m.symbols)}
        weights = self.live["contributors"]["weights_pct"]
        for s in m.symbols[:10]: self.assertAlmostEqual(weights[s], true[s] * 100, delta=0.6, msg=s)
        self.assertGreater(self.live["contributors"]["fit"]["r_squared"], 0.99)

    def test_direction_first_and_premise_correction(self):
        d = self.live["direction"]
        self.assertEqual(d["word"], "down"); self.assertLess(d["change"], 0)
        first = mm.summary(self.live, premise="up").splitlines()[0]
        self.assertTrue(first.startswith("Correction: the market is actually **down**, not up."), first)
        self.assertNotIn("Correction", mm.summary(self.live, premise="down"))

    def test_contributors_explain_the_actual_move_and_name_the_real_drag(self):
        c = self.live["contributors"]
        self.assertAlmostEqual(c["estimated_total_points"], c["actual_points"], delta=abs(c["actual_points"]) * 0.05 + 0.5)
        self.assertEqual(c["negative"][0]["symbol"], "S00")  # heaviest stock with an extra fall
        self.assertIn("Biggest drags (est. index points): S00", mm.summary(self.live))

    def test_breadth_sectors_vix_flows_and_gainers_losers(self):
        r = self.live
        self.assertEqual(r["nifty_breadth"]["declines"], 40)
        self.assertEqual([s["name"] for s in r["sectors"]], ["Nifty IT", "Nifty Bank", "Nifty Metal"])
        self.assertEqual(r["vix"]["value"], 16.2)
        self.assertEqual((r["flows"]["date"], r["flows"]["fii_net_crore"]), ("2026-11-17", -4200.0))
        self.assertTrue(all(x["change_pct"] < 0 for x in r["losers"]))
        self.assertEqual(r["constituents"]["observed"], 50)
        text = mm.summary(r)
        self.assertLessEqual(len(text.strip().splitlines()), 12)  # + the NSDL month-to-date line and the mood-context line
        self.assertIn("FII/FPI net -₹4,200.00 cr", text); self.assertIn("No buy/sell advice", text)

    def test_markdown_lists_method_and_sources(self):
        text = mm.markdown(self.live)
        for phrase in ("## Index-point contributors (Nifty 50, estimated)", "non-negative least squares", "https://www.nseindia.com/market-data/live-market-indices", "## Sectors"):
            self.assertIn(phrase, text)
        self.assertNotIn("nseindia.in", text)

    def test_session_mode_uses_official_files(self):
        nse_live.clear_cache()
        r = mm.collect("2026-11-17", nse_fetch=self.m.nse, flow_fetch=lambda url: FLOWS, yahoo_fetch=self.m.yahoo, clock=lambda: NOW, news=False)
        self.assertEqual(r["mode"], "session"); self.assertEqual(r["session"], "2026-11-17")
        self.assertIn("ind_close_all_17112026", r["source"])
        self.assertEqual(r["constituents"]["price_basis"], "NSE bhavcopy close vs previous close")
        self.assertIn("closed **", mm.summary(r, premise=None).splitlines()[0])

    def test_unknown_session_is_a_plain_unavailable_message(self):
        with patch.object(mm, "collect", side_effect=nse_live.Missing("NSE: no file for that date")), contextlib.redirect_stdout(io.StringIO()) as out:
            self.assertEqual(mm.main(["--session", "2026-11-24", "--no-news"]), 2)
        self.assertIn("unavailable (holiday, not yet published", out.getvalue())


if __name__ == "__main__":
    unittest.main()


class HeadlineTests(unittest.TestCase):
    def test_only_market_wide_headlines_are_listed_with_their_date(self):
        def item(title, hhmm): return (f"<item><title>{title}</title><link>https://economictimes.indiatimes.com/x</link>"
                                       f"<pubDate>Tue, 17 Nov 2026 {hhmm}:00 GMT</pubDate></item>")
        rss = ("<rss><channel>" + item("Dow plunges 850 points as AI selloff deepens", "13:07") + item("Tata Chemicals among 4 stocks that show RSI Trending Up", "13:10")
               + item("Britannia Share Price Live Updates", "13:12") + item("Sensex drops 300 pts; metal stocks struggle", "13:20") + "</channel></rss>").encode()
        nse_live.clear_cache(); m = Market()
        r = mm.collect(nse_fetch=m.nse, flow_fetch=lambda url: FLOWS, yahoo_fetch=m.yahoo, clock=lambda: NOW, news=True, news_fetch=lambda url: rss)
        self.assertEqual([h["headline"] for h in r["headlines"]], ["Sensex drops 300 pts; metal stocks struggle", "Dow plunges 850 points as AI selloff deepens"])
        text = mm.markdown(r)
        self.assertIn("(ET Markets, 17 Nov 18:50 IST)", text); self.assertIn("unverified possible causes", text)


def _item(title, when, link="https://economictimes.indiatimes.com/x"):
    return f"<item><title>{title}</title><link>{link}</link><pubDate>{when}</pubDate></item>"


ET_RSS = ("<rss><channel>" + _item("Sensex drops 300 pts; metal stocks struggle", "Wed, 18 Nov 2026 04:20:00 GMT")
          + _item("Dow plunges 850 points as AI selloff deepens", "Tue, 17 Nov 2026 13:07:00 GMT") + "</channel></rss>").encode()
GOOGLE_MARKET = ("<rss><channel>" + _item("Stock markets fall in early trade after two-day rally as FIIs sell - The Hindu", "Wed, 18 Nov 2026 05:02:00 GMT", "https://news.google.com/rss/articles/a")
                 + _item("Stock market outlook today: Sensex, Nifty prediction - Mint", "Wed, 18 Nov 2026 04:23:00 GMT", "https://news.google.com/rss/articles/b")
                 + _item("Sensex gains 11% till November 17; will momentum continue - India TV", "Wed, 18 Nov 2026 05:33:00 GMT", "https://news.google.com/rss/articles/e")
                 + _item("Vedanta Shares Today at Nifty 50 and BSE Sensex - Blog", "Wed, 18 Nov 2026 05:30:00 GMT", "https://news.google.com/rss/articles/f")
                 + _item("Stock markets fall in early trade after 2-day rally as FIIs sell - Deccan Herald", "Wed, 18 Nov 2026 04:58:00 GMT", "https://news.google.com/rss/articles/h")
                 + "</channel></rss>").encode()
GOOGLE_S00 = ("<rss><channel>" + _item("S00 shares slide after gold price correction - Upstox", "Wed, 18 Nov 2026 05:04:00 GMT", "https://news.google.com/rss/articles/c")
              + _item("Unrelated company wins order - Example", "Wed, 18 Nov 2026 05:05:00 GMT", "https://news.google.com/rss/articles/d")
              + _item("If EPS Growth Is Important To You, S00 Presents An Opportunity - Simply Wall St", "Wed, 18 Nov 2026 05:06:00 GMT", "https://news.google.com/rss/articles/g")
              + "</channel></rss>").encode()


def news_fetch(et_up=True):
    calls = []
    def fetch(url):
        calls.append(url)
        if "economictimes" in url:
            if not et_up: raise OSError("down")
            return ET_RSS
        if "news.google.com" in url and "Sensex+Nifty" in url: return GOOGLE_MARKET
        if "news.google.com" in url and "%22S00%22" in url: return GOOGLE_S00
        if "news.google.com" in url: return b"<rss><channel></channel></rss>"
        raise AssertionError(url)
    fetch.calls = calls
    return fetch


class Round2SummaryTests(unittest.TestCase):
    """--summary keeps the headlines it collects (dated, labelled unverified), adds per-stock news for the top drags,
    labels the times behind the lead and the contribution check, and gives the last session's direction on a wrong premise."""
    @classmethod
    def setUpClass(cls):
        nse_live.clear_cache(); cls.m = Market(); cls.fetch = news_fetch()
        cls.r = mm.collect(nse_fetch=cls.m.nse, flow_fetch=lambda url: FLOWS, yahoo_fetch=cls.m.yahoo, clock=lambda: NOW, news=True, news_fetch=cls.fetch)

    def test_summary_prints_top_three_dated_headlines_with_publishers(self):
        text = mm.summary(self.r)
        self.assertIn("- Headlines (ET Markets RSS, Google News RSS; unverified possible causes, not confirmed reasons):", text)
        self.assertIn('  - "Sensex drops 300 pts; metal stocks struggle" (ET Markets, 18 Nov 09:50 IST)', text)
        self.assertIn('  - "Stock markets fall in early trade after two-day rally as FIIs sell" (The Hindu, 18 Nov 10:32 IST)', text)
        self.assertNotIn("prediction", text)  # pre-market outlook pieces are not causes
        self.assertNotIn("till November 17", text); self.assertNotIn("Shares Today", text)  # retrospective / listing pages
        self.assertNotIn("EPS Growth", text)  # evergreen stock screens are not news about the move
        self.assertEqual(text.count("Stock markets fall in early trade"), 1)  # syndicated near-duplicates collapse
        self.assertLess(text.index("Stock markets fall"), text.index("Sensex drops"))  # newest Indian-market headline first

    def test_google_news_is_the_fallback_when_et_markets_is_down(self):
        nse_live.clear_cache()
        r = mm.collect(nse_fetch=self.m.nse, flow_fetch=lambda url: FLOWS, yahoo_fetch=self.m.yahoo, clock=lambda: NOW, news=True, news_fetch=news_fetch(et_up=False))
        self.assertEqual([h["publisher"] for h in r["headlines"]], ["The Hindu"])
        self.assertNotIn("headlines", [c["component"] for c in r["coverage"]])

    def test_per_stock_headline_for_the_top_drags(self):
        self.assertEqual(self.r["contributors"]["negative"][0]["symbol"], "S00")
        self.assertEqual([(h["symbol"], h["publisher"]) for h in self.r["stock_headlines"]], [("S00", "Upstox")])
        self.assertIn('  - S00: "S00 shares slide after gold price correction" (Upstox, 18 Nov 10:34 IST)', mm.summary(self.r))
        queried = [u for u in self.fetch.calls if "news.google.com" in u and "share" in u]
        self.assertEqual(len(queried), 2)  # the top two drags only
        self.assertTrue(all(u.startswith("https://news.google.com/rss/search?q=%22") for u in queried))

    def test_wrong_premise_also_gives_the_previous_session(self):
        text = mm.summary(self.r, premise="up")
        lines = text.splitlines()
        self.assertTrue(lines[0].startswith("Correction: the market is actually **down**, not up."))
        prev = self.r["previous_direction"]
        self.assertEqual(lines[1], f"- If you meant the last session: Nifty 50 closed **{prev['word']}** {abs(prev['change']):,.2f} points ({prev['change_pct']:+.2f}%) "
                                   f"at {prev['close']:,.2f} on Tue 17 Nov 2026.")
        self.assertNotIn("If you meant", mm.summary(self.r, premise="down"))

    def test_lead_and_check_label_their_times_and_sources(self):
        text = mm.summary(self.r)
        c = self.r["contributors"]
        self.assertIn(f"at {NOW:%H:%M} IST (NSE live)", text.splitlines()[0])
        self.assertIn(f"vs Nifty's {c['actual_points']:+.1f} on Yahoo at {c['actual_at'][11:16]} IST, the time of the stock prices "
                      f"(the {abs(self.r['direction']['change']):,.2f}-point fall above is NSE at {NOW:%H:%M} IST)", text)
        self.assertNotIn("at the same moment (weights", text)
