"""Offline contracts for the screener, peer groups, P/E history band, currency conversion and the price chart."""
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
        self.assertIn("## Peers", sa.markdown(r))
        r = sa.analyse("ALPHA", fetcher=Feed(), news_fetcher=Feed(), clock=lambda: NOW, peers=False)
        self.assertIsNone(r["peers"])

    def test_price_chart_svg(self):
        r = sa.analyse("ALPHA", fetcher=Feed(), news_fetcher=Feed(), clock=lambda: NOW, peers=False)
        svg = sa.svg_price_chart(r)
        xml.dom.minidom.parseString(svg.encode())
        for part in ("50-day avg", "200-day avg", "20d high", "20d low"):
            self.assertIn(part, svg)


if __name__ == "__main__":
    unittest.main()
