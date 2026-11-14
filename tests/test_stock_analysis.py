"""Offline contracts for the one-stock analysis report; never contacts the network."""
from datetime import timedelta
import json
from pathlib import Path
import sys
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
from core import Block, instant
from stock_analysis import analyse, markdown, normalize, ratios, rsi

NOW = instant("2026-11-13T16:00:00+05:30")


def sessions(count=520):
    days, day = [], NOW.replace(hour=9, minute=15, second=0, microsecond=0)
    while len(days) < count:
        if day.weekday() < 5:
            days.append(day)
        day -= timedelta(days=1)
    return list(reversed(days))


def chart_payload(selected, closes, name="Example Industries Limited", splits=None):
    days = sessions(len(closes))
    payload = {"chart": {"error": None, "result": [{
        "meta": {"symbol": selected, "currency": "INR", "exchangeTimezoneName": "Asia/Kolkata", "dataGranularity": "1d", "longName": name},
        "timestamp": [int(d.timestamp()) for d in days],
        "indicators": {"quote": [{"open": closes, "high": [c * 1.01 for c in closes], "low": [c * 0.99 for c in closes],
                                  "close": closes, "volume": [100000] * len(closes)}]}}]}}
    if splits:
        payload["chart"]["result"][0]["events"] = {"splits": {str(s): {"date": s, "numerator": 2, "denominator": 1}} for s in splits}
    return json.dumps(payload).encode()


def series(name, rows):
    return {"meta": {"symbol": ["EXAMPLE.NS"], "type": [name]}, name: [{"asOfDate": d, "reportedValue": {"raw": v}} for d, v in rows]}


def fundamentals_payload(**override):
    data = {
        "annualTotalRevenue": [("2023-03-31", 800.0), ("2024-03-31", 900.0), ("2025-03-31", 1000.0), ("2026-03-31", 1100.0)],
        "annualNetIncome": [("2025-03-31", 100.0), ("2026-03-31", 110.0)],
        "annualDilutedEPS": [("2025-03-31", 10.0), ("2026-03-31", 11.0)],
        "trailingDilutedEPS": [("2026-09-30", 12.5)],
        "annualStockholdersEquity": [("2025-03-31", 500.0), ("2026-03-31", 600.0)],
        "annualOrdinarySharesNumber": [("2026-03-31", 10.0)],
        "annualTotalDebt": [("2026-03-31", 300.0)],
        "annualOperatingIncome": [("2026-03-31", 165.0)],
        "annualInterestExpense": [("2026-03-31", 15.0)],
        "annualCurrentAssets": [("2026-03-31", 400.0)],
        "annualCurrentLiabilities": [("2026-03-31", 200.0)],
        "annualFreeCashFlow": [("2026-03-31", 50.0)],
        "annualCashDividendsPaid": [("2026-03-31", -20.0)],
        "quarterlyNetIncome": [("2026-06-30", 25.0), ("2026-09-30", 30.0)],
    }
    data.update(override)
    return json.dumps({"timeseries": {"result": [series(k, v) for k, v in data.items() if v is not None]}}).encode()


GOOGLE = b"""<?xml version="1.0"?><rss><channel>
<item><title>Example Industries shares rise after results - Moneycontrol</title><link>https://news.google.com/rss/articles/a</link><pubDate>Fri, 13 Nov 2026 05:00:00 GMT</pubDate></item>
<item><title>Example Industries shares rise after results - Moneycontrol</title><link>https://news.google.com/rss/articles/b</link><pubDate>Fri, 13 Nov 2026 05:00:00 GMT</pubDate></item>
<item><title>Old story - Mint</title><link>https://news.google.com/rss/articles/c</link><pubDate>Mon, 01 Jan 2024 05:00:00 GMT</pubDate></item>
</channel></rss>"""
EMPTY_RSS = b"<?xml version='1.0'?><rss><channel></channel></rss>"


class Feed:
    def __init__(self, closes=None, fundamentals=None, splits=None, fail=()):
        self.closes = closes or [100 * 1.001 ** k for k in range(520)]
        self.fundamentals = fundamentals or fundamentals_payload()
        self.splits = splits
        self.fail = set(fail)
        self.urls = []

    def __call__(self, url):
        self.urls.append(url)
        for key in self.fail:
            if key in url:
                raise Block("PUBLIC: source unavailable")
        if "/v8/finance/chart/%5ENSEI" in url:
            return chart_payload("^NSEI", [100 * 1.0005 ** k for k in range(520)], name="NIFTY 50")
        if "/v8/finance/chart/" in url:
            return chart_payload("EXAMPLE.NS", self.closes, splits=self.splits)
        if "fundamentals-timeseries" in url:
            return self.fundamentals
        if "news.google.com" in url:
            return GOOGLE
        return EMPTY_RSS


def run(feed, **kwargs):
    return analyse("example", fetcher=feed, news_fetcher=feed, clock=lambda: NOW, **kwargs)


class StockAnalysisTests(unittest.TestCase):
    def test_symbol_normalisation(self):
        self.assertEqual(normalize("reliance"), "RELIANCE.NS")
        self.assertEqual(normalize("500325.BO"), "500325.BO")
        self.assertEqual(normalize(" tcs.ns "), "TCS.NS")
        for bad in ("RELIANCE; rm -rf", "", "A" * 40):
            with self.subTest(bad=bad), self.assertRaises(Block):
                normalize(bad)

    def test_complete_report_sections(self):
        r = run(Feed())
        self.assertEqual(r["symbol"], "EXAMPLE.NS")
        self.assertEqual(r["name"], "Example Industries Limited")
        self.assertEqual(r["technical"]["trend"], "uptrend")
        self.assertGreater(r["technical"]["relative"]["1Y"]["difference"], 0)
        self.assertFalse(r["execution_eligible"])
        self.assertFalse(r["submitted"])
        for g in r["outlook"]["ranges"].values():
            self.assertLessEqual(g["low_10"], g["median"])
            self.assertLessEqual(g["median"], g["high_90"])
        text = markdown(r)
        for heading in ("## Snapshot", "## Technical condition", "## Financial ratios", "## News", "## Outlook", "not a prediction", "not investment advice"):
            self.assertIn(heading, text)

    def test_ratios_use_reported_values(self):
        f = run(Feed())["fundamentals"]
        price = 100 * 1.001 ** 519
        self.assertAlmostEqual(f["pe"], price / 12.5)
        self.assertEqual(f["eps"]["basis"], "trailing 12 months")
        self.assertAlmostEqual(f["pb"], price / 60)
        self.assertAlmostEqual(f["roe"], 110 / 550)
        self.assertAlmostEqual(f["debt_to_equity"], 0.5)
        self.assertAlmostEqual(f["current_ratio"], 2.0)
        self.assertAlmostEqual(f["interest_cover"], 11.0)
        self.assertAlmostEqual(f["revenue_growth"], 0.1)
        self.assertAlmostEqual(f["revenue_cagr_3y"], (1100 / 800) ** (1 / 3) - 1)
        self.assertAlmostEqual(f["quarter_profit_change"], 0.2)
        self.assertFalse(f["bank_like"])

    def test_stale_trailing_eps_yields_to_newer_annual_and_split_is_flagged(self):
        payload = fundamentals_payload(trailingDilutedEPS=[("2025-06-30", 90.0)])
        split = int(instant("2026-08-26T09:15:00+05:30").timestamp())
        r = run(Feed(fundamentals=payload, splits=[split]))
        self.assertEqual(r["fundamentals"]["eps"]["value"], 11.0)
        self.assertEqual(r["fundamentals"]["eps"]["basis"], "last financial year")
        self.assertEqual(r["fundamentals"]["split_after_statements"], ["2026-08-26"])
        self.assertIn("Split/bonus", markdown(r))

    def test_bank_like_statements_mark_non_meaningful_ratios(self):
        payload = fundamentals_payload(annualCurrentAssets=None, annualCurrentLiabilities=None, annualOperatingIncome=None)
        r = run(Feed(fundamentals=payload))
        self.assertTrue(r["fundamentals"]["bank_like"])
        self.assertIsNone(r["fundamentals"]["current_ratio"])
        self.assertIn("bank or financial company", markdown(r))

    def test_news_is_deduplicated_and_windowed(self):
        items = run(Feed())["news"]["items"]
        self.assertEqual([i["headline"] for i in items], ["Example Industries shares rise after results"])
        self.assertEqual(items[0]["outlet"], "Moneycontrol")

    def test_partial_failures_stay_visible(self):
        r = run(Feed(fail=("fundamentals-timeseries", "news.google.com", "%5ENSEI")))
        self.assertIsNone(r["fundamentals"])
        self.assertIn("fundamentals", r["errors"])
        self.assertIn("benchmark", r["errors"])
        self.assertTrue(r["news"]["failures"])
        self.assertIsNone(r["technical"]["relative"]["1Y"]["nifty"])
        self.assertIn("## Data gaps", markdown(r))

    def test_missing_price_history_blocks_with_guidance(self):
        with self.assertRaisesRegex(Block, "Check the NSE symbol"):
            run(Feed(fail=("/v8/finance/chart/EXAMPLE",)))

    def test_short_history_blocks(self):
        with self.assertRaises(Block):
            run(Feed(closes=[100.0] * 30))

    def test_rsi_bounds(self):
        self.assertEqual(rsi([float(k) for k in range(30)]), 100.0)
        self.assertLess(rsi([float(30 - k) for k in range(30)]), 1)
        self.assertIsNone(rsi([1.0] * 5))

    def test_ratios_tolerate_missing_fields(self):
        f = ratios({"annualNetIncome": [{"date": "2026-03-31", "value": 5.0}]}, 100.0, NOW)
        self.assertIsNone(f["pe"])
        self.assertIsNone(f["market_cap"])


if __name__ == "__main__":
    unittest.main()
