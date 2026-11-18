"""Offline contracts for the screener, peer groups, P/E history band, currency conversion and the price chart."""
from contextlib import redirect_stderr
import io
import json
from pathlib import Path
import sys
import unittest
import xml.dom.minidom

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT / "tests"))
from core import Block, instant
import screener
import stock_analysis as sa
from test_stock_analysis import NOW, chart_payload, sessions

BANK = "Company Name,Industry,Symbol,Series,ISIN Code\nAlpha Bank,Financial Services,ALPHA,EQ,X1\nBeta Bank,Financial Services,BETA,EQ,X2\nGamma Bank,Financial Services,GAMMA,EQ,X3\n"
ENERGY = "Company Name,Industry,Symbol,Series,ISIN Code\n" + "".join(f"E{i},Power,E{i},EQ,Y{i}\n" for i in range(20)) + "Alpha Bank,Financial Services,ALPHA,EQ,X1\n"
N500 = BANK + "Delta Mills,Textiles,DELTA,EQ,X4\nEpsilon Mills,Textiles,EPSILON,EQ,X5\n"


def fundamentals(eps=10.0, shares=1e9, currency=None, extra=None):
    def series(name, rows):
        return {"meta": {"type": [name]}, name: [{"asOfDate": d, "reportedValue": {"raw": v}, **({"currencyCode": currency} if currency and "Shares" not in name else {})} for d, v in rows]}
    data = {"annualDilutedEPS": [("2023-03-31", eps * 0.8), ("2024-03-31", eps * 0.9), ("2025-03-31", eps), ("2026-03-31", eps)],
            "annualNetIncome": [("2025-03-31", eps * shares), ("2026-03-31", eps * shares)],
            "annualStockholdersEquity": [("2025-03-31", eps * shares * 5), ("2026-03-31", eps * shares * 5)],
            "annualTotalRevenue": [("2025-03-31", eps * shares * 8), ("2026-03-31", eps * shares * 10)],
            "annualOrdinarySharesNumber": [("2026-03-31", shares)]}
    data.update(extra or {})
    return json.dumps({"timeseries": {"result": [series(k, v) for k, v in data.items()]}}).encode()


class Feed:
    """Fake network: index CSVs, charts (price 100 rising), fundamentals per symbol, FX."""
    def __init__(self, eps=None, currency=None, fx=90.0, missing=(), down=()):
        self.eps = eps or {}
        self.currency = currency or {}
        self.fx, self.missing, self.down = fx, set(missing), set(down)

    def __call__(self, url):
        if "ind_niftybanklist" in url:
            return BANK.encode()
        if "ind_niftyenergylist" in url:
            return ENERGY.encode()
        if "ind_nifty500list" in url or "ind_nifty50list" in url:
            return N500.encode()
        if "/indices/" in url:
            raise Block("PUBLIC: source unavailable")
        if "INR=X" in url:
            if self.fx is None:
                raise Block("PUBLIC: source unavailable")
            return json.dumps({"chart": {"result": [{"meta": {"currency": "INR", "regularMarketPrice": self.fx}}]}}).encode()
        symbol = url.split("/")[-1].split("?")[0].replace(".NS", "").replace("%5E", "^")
        if symbol in self.down:
            raise Block("PUBLIC: source unavailable")
        if "/v8/finance/chart/" in url:
            return chart_payload(symbol + ".NS" if not symbol.startswith("^") else symbol, [100 * 1.001 ** k for k in range(1300)], name=f"{symbol} Ltd")
        if "fundamentals-timeseries" in url:
            if symbol in self.missing:
                return json.dumps({"timeseries": {"result": []}}).encode()
            return fundamentals(self.eps.get(symbol, 10.0), currency=self.currency.get(symbol))
        return b"<rss><channel/></rss>"


class ScreenerTests(unittest.TestCase):
    def test_index_members(self):
        members = screener.index_members("bank", fetcher=Feed())
        self.assertEqual([m["symbol"] for m in members], ["ALPHA", "BETA", "GAMMA"])
        with self.assertRaises(Block):
            screener.index_members("nope", fetcher=Feed())

    def test_filter_parsing(self):
        self.assertEqual(screener.parse_filter("pe<25"), ("pe", "<", 25.0))
        self.assertEqual(screener.parse_filter("roe>15"), ("roe", ">", 0.15))
        self.assertEqual(screener.parse_filter("roe >= 15%"), ("roe", ">=", 0.15))
        self.assertEqual(screener.parse_filter("roe>0.15"), ("roe", ">", 0.15))
        for bad in ("pe<<2", "colour=red", "pe"):
            with self.subTest(bad=bad), self.assertRaises(Block):
                screener.parse_filter(bad)

    def test_screen_filters_sorts_and_reports_gaps(self):
        feed = Feed(eps={"ALPHA": 20.0, "BETA": 5.0, "GAMMA": 10.0}, missing={"DELTA"}, down={"EPSILON"})
        r = screener.screen("nifty50", ["pe<30"], sort="pe", descending=False, fetcher=feed, now=NOW)
        self.assertEqual(r["requested"], 5)
        self.assertEqual(r["failed"], ["EPSILON.NS"])
        self.assertEqual(r["missing_data"], ["DELTA.NS"])
        pes = [x["pe"] for x in r["results"]]
        self.assertEqual(pes, sorted(pes))
        self.assertTrue(all(p < 30 for p in pes))
        self.assertNotIn("BETA.NS", [x["symbol"] for x in r["results"]])  # EPS 5 -> P/E well above 30
        self.assertIn("Missing data", screener.markdown(r))
        self.assertFalse(r["execution_eligible"])

    def test_custom_symbols(self):
        r = screener.screen("nifty50", [], symbols=["ALPHA", "gamma.ns"], fetcher=Feed(), now=NOW)
        self.assertEqual(r["universe"], "custom")
        self.assertEqual({x["symbol"] for x in r["results"]}, {"ALPHA.NS", "GAMMA.NS"})

    def test_peers_use_smallest_sector_then_industry(self):
        g = screener.peers_for("ALPHA.NS", fetcher=Feed())
        self.assertIn("bank", g["basis"])  # bank (3) beats energy (21)
        self.assertEqual([m["symbol"] for m in g["peers"]], ["BETA", "GAMMA"])
        g = screener.peers_for("DELTA.NS", fetcher=Feed())
        self.assertIn("Textiles", g["basis"])
        self.assertEqual([m["symbol"] for m in g["peers"]], ["EPSILON"])
        self.assertEqual(screener.peers_for("NOWHERE.NS", fetcher=Feed())["peers"], [])

    def test_pe_band_uses_eps_only_after_publication_lag(self):
        bars = [{"at": d.isoformat(), "close": 100.0} for d in sessions(400)]
        eps = [{"date": "2025-03-31", "value": 10.0}, {"date": "2026-03-31", "value": 20.0}]
        band = screener.pe_band(bars, eps)
        self.assertEqual(band["max"], 10.0)    # price 100 / EPS 10 before 2026-05-30
        self.assertEqual(band["min"], 5.0)     # price 100 / EPS 20 afterwards
        self.assertEqual(band["current"], 5.0)
        self.assertIsNone(screener.pe_band(bars[:50], eps))
        self.assertIsNone(screener.pe_band(bars, [{"date": "2025-03-31", "value": -3.0}]))


class StockDepthTests(unittest.TestCase):
    def test_usd_financials_are_converted_but_shares_are_not(self):
        series = sa.fundamentals("ALPHA.NS", fetcher=Feed(eps={"ALPHA": 0.5}, currency={"ALPHA": "USD"}, fx=90.0), now=NOW)
        self.assertEqual(series["annualDilutedEPS"][-1]["value"], 45.0)
        self.assertEqual(series["annualOrdinarySharesNumber"][-1]["value"], 1e9)
        self.assertEqual(series["_currency"]["reported"], "USD")
        with self.assertRaisesRegex(Block, "rate unavailable"):
            sa.fundamentals("ALPHA.NS", fetcher=Feed(currency={"ALPHA": "USD"}, fx=None), now=NOW)

    def test_converted_reports_flag_currency_and_skip_band(self):
        r = sa.analyse("ALPHA", fetcher=Feed(eps={"ALPHA": 0.5}, currency={"ALPHA": "USD"}), news_fetcher=Feed(), clock=lambda: NOW, peers=False)
        self.assertIsNone(r["fundamentals"]["pe_band"])
        self.assertIn("reported in USD", sa.markdown(r))
        r = sa.analyse("ALPHA", fetcher=Feed(), news_fetcher=Feed(), clock=lambda: NOW, peers=False)
        self.assertIsNotNone(r["fundamentals"]["pe_band"])
        self.assertIn("### Valuation history", sa.markdown(r))

    def test_peer_table_ranks_by_market_cap_with_group_medians(self):
        r = sa.analyse("ALPHA", fetcher=Feed(), news_fetcher=Feed(), clock=lambda: NOW)
        c = r["peers"]
        self.assertEqual(c["evaluated"], 3)
        self.assertEqual({x["symbol"] for x in c["rows"]}, {"BETA.NS", "GAMMA.NS"})
        self.assertIn("pe", c["medians"])
        self.assertIn("## Index peers", sa.markdown(r))
        r = sa.analyse("ALPHA", fetcher=Feed(), news_fetcher=Feed(), clock=lambda: NOW, peers=False)
        self.assertIsNone(r["peers"])

    def test_chosen_peers_replace_index_peers(self):
        r = sa.analyse("ALPHA", fetcher=Feed(), news_fetcher=Feed(), clock=lambda: NOW, peer_symbols=["delta", "EPSILON.NS", "ALPHA"])
        c = r["peers"]
        self.assertEqual(c["kind"], "chosen")
        self.assertEqual({x["symbol"] for x in c["rows"]}, {"DELTA.NS", "EPSILON.NS"})  # own symbol dropped
        text = sa.markdown(r)
        self.assertIn("## Peers", text)
        self.assertNotIn("Index peers", text)

    def test_index_peers_are_labelled_and_size_mismatch_flagged(self):
        own = {"symbol": "ALPHA.NS", "name": "Alpha", "market_cap_cr": 10 ** 9, "pe": 20.0}
        c = sa.peer_comparison("ALPHA.NS", own, fetcher=Feed(), now=NOW)
        self.assertEqual(c["kind"], "index")
        self.assertTrue(c["size_mismatch"])
        r = sa.analyse("ALPHA", fetcher=Feed(), news_fetcher=Feed(), clock=lambda: NOW)
        self.assertIn("## Index peers (same NSE index, not necessarily the same business)", sa.markdown(r))

    def test_summary_is_short_and_answer_first(self):
        r = sa.analyse("ALPHA", fetcher=Feed(), news_fetcher=Feed(), clock=lambda: NOW)
        text = sa.summary(r)
        self.assertLessEqual(len(text.splitlines()), 14)
        self.assertTrue(text.startswith("ALPHA Ltd (ALPHA.NS) — prices as of"))
        for part in ("Valuation: P/E", "Levels:", "not investment advice"):
            self.assertIn(part, text)

    def test_google_news_links_are_not_dumped_into_markdown(self):
        r = sa.analyse("ALPHA", fetcher=Feed(), news_fetcher=Feed(), clock=lambda: NOW, peers=False)
        r["news"]["items"] = [{"published_at": "2026-11-17T10:00:00+05:30", "headline": "Alpha wins order", "outlet": "Mint",
                               "url": "https://news.google.com/rss/articles/" + "x" * 300},
                              {"published_at": "2026-11-17T09:00:00+05:30", "headline": "Alpha Ltd", "outlet": "NSE",
                               "url": "https://nsearchives.nseindia.com/corporate/a.pdf"}]
        text = sa.markdown(r)
        self.assertNotIn("news.google.com", text)
        self.assertIn("Alpha wins order · Mint", text)
        self.assertIn("(https://nsearchives.nseindia.com/corporate/a.pdf)", text)

    def test_price_chart_svg(self):
        r = sa.analyse("ALPHA", fetcher=Feed(), news_fetcher=Feed(), clock=lambda: NOW, peers=False)
        svg = sa.svg_price_chart(r)
        xml.dom.minidom.parseString(svg.encode())
        for part in ("50-day avg", "200-day avg", "20d high", "20d low"):
            self.assertIn(part, svg)


def index_csv(*rows):
    return "\n".join(("Company Name,Industry,Symbol,Series,ISIN Code",) + rows) + "\n"


class ScreenerFixTests(unittest.TestCase):
    def test_placeholders_are_dropped_and_units_are_not_peers(self):
        csv_text = index_csv("Delta Realty,Realty,DREAL,EQ,INE001", "Dummy HEG Ltd.,Capital Goods,DUMMYHEG,EQ,DUM545A01024",
                             "Embassy Office Parks REIT,Realty,EMBASSY,RR,INE041025011", "Omega Realty,Realty,OREAL,EQ,INE002")
        feed = lambda url: csv_text.encode() if "ind_nifty500list" in url else (_ for _ in ()).throw(Block("PUBLIC: source unavailable"))
        self.assertNotIn("DUMMYHEG", [m["symbol"] for m in screener.index_members("nifty500", fetcher=feed)])
        g = screener.peers_for("DREAL.NS", fetcher=feed)
        self.assertEqual([m["symbol"] for m in g["peers"]], ["OREAL"])

    def test_industry_fallback_prefers_companies_of_similar_size(self):
        big = [f"Big{i},Textiles,BIG{i},EQ,INEB{i}" for i in range(6)]
        n500 = index_csv(*big, *(f"Small{i},Textiles,SM{i},EQ,INES{i}" for i in range(6)))
        large = index_csv(*big)
        mid = index_csv("Other,Power,OTHER,EQ,INEO")

        def feed(url):
            for key, text in (("ind_nifty500list", n500), ("ind_nifty100list", large), ("ind_niftymidcap150list", mid)):
                if key in url:
                    return text.encode()
            raise Block("PUBLIC: source unavailable")
        g = screener.peers_for("BIG0.NS", fetcher=feed)
        self.assertEqual([m["symbol"] for m in g["peers"]], ["BIG1", "BIG2", "BIG3", "BIG4", "BIG5"])
        self.assertIn("large-cap companies", g["basis"])
        g = screener.peers_for("SM0.NS", fetcher=feed)  # 5 same-tier peers: enough, no large caps mixed in
        self.assertEqual({m["symbol"] for m in g["peers"]}, {f"SM{i}" for i in range(1, 6)})
        g = screener.peers_for("BIG0.NS", fetcher=feed, min_same_tier=6)  # too few: the adjacent (mid) tier is added
        self.assertIn("adjacent size tier", g["basis"])

    def test_new_universes_and_merged_lists(self):
        self.assertEqual(screener.UNIVERSES["nifty100"], "nifty100")
        self.assertEqual(screener.UNIVERSES["nifty200"], "nifty200")
        lists = {"ind_nifty50list": index_csv("A,X,AAA,EQ,IN1", "B,X,BBB,EQ,IN2"),
                 "ind_niftynext50list": index_csv("B,X,BBB,EQ,IN2", "C,X,CCC,EQ,IN3")}
        feed = lambda url: next(v.encode() for k, v in lists.items() if k in url)
        self.assertEqual([m["symbol"] for m in screener.universe_members(["nifty50", "niftynext50", "nifty50"], fetcher=feed)], ["AAA", "BBB", "CCC"])
        captured = {}
        original = screener.screen
        def stub(universe, *a, **k):
            captured.setdefault("u", []).append(universe)
            raise Block("SCREENER: stopped by the test")
        screener.screen = stub
        try:
            with redirect_stderr(io.StringIO()):
                self.assertEqual(screener.main(["--universe", "nifty50", "niftynext50", "--universe", "it"]), 2)
                screener.main([])
        finally:
            screener.screen = original
        self.assertEqual(captured["u"], [["nifty50", "niftynext50", "it"], ["nifty50"]])

    def test_screen_merges_universes(self):
        r = screener.screen(["nifty50", "bank"], [], fetcher=Feed(), now=NOW)
        self.assertEqual(r["universe"], "nifty50+bank")
        self.assertEqual(r["requested"], 5)  # bank members are already in the nifty50 fixture

    def test_transient_failures_are_retried_not_reported_as_missing(self):
        base, calls, waits = Feed(), {}, []

        def flaky(url):
            if "fundamentals-timeseries" in url and "/ALPHA" in url:
                calls["n"] = calls.get("n", 0) + 1
                if calls["n"] <= 2:
                    raise Block("PUBLIC: source unavailable; no login, bypass or automatic retry")
            return base(url)
        r = screener.screen("nifty50", ["pe<1000"], fetcher=flaky, now=NOW, retries=2, sleep=waits.append, workers=1)
        self.assertIn("ALPHA.NS", [x["symbol"] for x in r["results"]])
        self.assertEqual(r["failed"], [])
        self.assertEqual(waits, [0.5, 1.5])
        calls.clear()
        r = screener.screen("nifty50", ["pe<1000"], fetcher=flaky, now=NOW, retries=0, workers=1)
        self.assertEqual(r["failed"], ["ALPHA.NS"])
        self.assertEqual(r["failed_detail"]["ALPHA.NS"], "financial statements failed to load")
        self.assertNotIn("ALPHA.NS", r["missing_data"])
        text = screener.markdown(r)
        self.assertIn("Failed to load financial statements (needed for a filter): ALPHA", text)

    def test_price_only_filter_keeps_a_stock_whose_statements_failed(self):
        base = Feed()

        def statements_down(url):
            if "fundamentals-timeseries" in url and "/DELTA" in url:
                raise Block("PUBLIC: source unavailable")
            return base(url)
        r = screener.screen("nifty50", ["rsi14<101"], fetcher=statements_down, now=NOW, retries=0)
        self.assertIn("DELTA.NS", [x["symbol"] for x in r["results"]])
        self.assertEqual((r["failed"], r["partial_financials"]), ([], ["DELTA.NS"]))
        self.assertIn("Shown without financial statements (they failed to load): DELTA", screener.markdown(r))

    def test_filtered_and_sorted_fields_are_displayed(self):
        report = {"filters": ["dividend_yield>2.5", "debt_to_equity<0.5"], "sort": "fcf_yield"}
        cols = screener.display_columns(report)
        self.assertEqual(cols[-2:], ("dividend_yield", "fcf_yield"))
        self.assertEqual(cols.count("debt_to_equity"), 1)
        r = screener.screen("nifty50", ["dividend_yield>=0"], sort="dividend_yield", fetcher=Feed(), now=NOW)
        self.assertIn("dividend yield (12m)", screener.markdown(r).split("| --- |")[0])

    def test_one_year_return_matches_stock_analysis(self):
        snap = screener.snapshot("GAMMA", fetcher=Feed(), now=NOW)
        full = sa.analyse("GAMMA", fetcher=Feed(), news_fetcher=Feed(), clock=lambda: NOW, peers=False)
        self.assertAlmostEqual(snap["return_1y"], full["technical"]["returns"]["1Y"])
        self.assertAlmostEqual(snap["return_1y"], 1.001 ** 250 - 1)


class PeBandFixTests(unittest.TestCase):
    bars = [{"at": d.isoformat(), "close": 100.0} for d in sessions(700)]

    def test_negative_latest_eps_gives_no_band_and_old_eps_is_not_reused(self):
        eps = [{"date": "2024-03-31", "value": 10.0}, {"date": "2025-03-31", "value": 20.0}, {"date": "2026-03-31", "value": -0.6}]
        self.assertIsNone(screener.pe_band(self.bars, eps))
        eps = [{"date": "2024-03-31", "value": 10.0}, {"date": "2025-03-31", "value": -2.0}, {"date": "2026-03-31", "value": 20.0}]
        band = screener.pe_band(self.bars, eps)
        self.assertEqual(band["max"], 10.0)
        self.assertEqual(band["current"], 5.0)
        self.assertLess(band["sessions"], 700 - 250)  # the loss year contributes no sessions
        self.assertEqual(band["years"], round(band["sessions"] / 250, 1))

    def test_since_drops_pre_demerger_eps_and_sessions(self):
        eps = [{"date": "2025-03-31", "value": 80.0}, {"date": "2026-03-31", "value": 10.0}]
        band = screener.pe_band(self.bars, eps, since="2025-10-14")
        self.assertEqual((band["min"], band["max"]), (10.0, 10.0))
        self.assertGreaterEqual(band["start"], "2026-05-30")
        self.assertIn("2025-10-14 demerger", band["basis"])


if __name__ == "__main__":
    unittest.main()
