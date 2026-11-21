"""Offline contracts for the one-stock analysis report; never contacts the network."""
from datetime import date, timedelta
import json
import math
from pathlib import Path
import sys
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
from core import Block, instant
from stock_analysis import analyse, check_eps, fundamentals, markdown, normalize, outlook, ratios, rsi, summary

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


class ReportFixTests(unittest.TestCase):
    TECH = {"trend": "mixed / sideways", "rsi14": 45.0, "closes": [100.0] * 300, "close_dates": [f"d{k}" for k in range(300)],
            "resistance": 1469.0, "resistance_60d": 1780.1, "high_52w": 1780.1, "support": 1350.6, "support_60d": 1172.8, "sma200": 1310.42}

    def test_bear_levels_descend_and_bull_levels_ascend(self):
        o = outlook(self.TECH)
        self.assertEqual(o["bear_levels"], [1310.42, 1172.8])  # nearest first (HCL, 18 Nov 2026)
        self.assertLess(o["bear_case"].index("1,310.42"), o["bear_case"].index("1,172.80"))
        self.assertEqual(o["bull_levels"], [1780.1])  # 60-day and 52-week highs merged, not listed twice
        self.assertIn("60-day high and 52-week high", o["bull_case"])
        o = outlook({**self.TECH, "resistance_60d": 1600.0})
        self.assertEqual(o["bull_levels"], [1600.0, 1780.1])

    def test_outlook_states_the_years_actually_used(self):
        o = outlook(self.TECH)
        self.assertIn("over the last 1.2 years (300 sessions from d0)", o["method"])
        self.assertNotIn("~2 years", o["method"])
        r = run(Feed())
        self.assertIn("over the last 2.0 years (505 sessions", r["outlook"]["method"])

    def test_summary_compares_pe_on_the_band_basis(self):
        r = run(Feed())
        band, f = r["fundamentals"]["pe_band"], r["fundamentals"]
        self.assertAlmostEqual(band["current"], f["pe"] * 12.5 / 11.0)  # band uses annual EPS 11, headline uses trailing 12.5
        text = summary(r)
        self.assertIn(f"Valuation: P/E {f['pe']:,.1f} (trailing 12 months); on latest annual EPS {band['current']:,.1f}, more expensive than on", text)
        self.assertIn(f"(usual range {band['p25']:,.1f}–{band['p75']:,.1f})", text)
        report = markdown(r)
        self.assertIn(f"Today's P/E on latest annual EPS is {band['current']:,.1f}:", report)
        self.assertIn("uses trailing 12 months EPS, so it is not compared with this range", report)
        self.assertIn("| P/E (trailing 12 months) |", report)


class WeakQuarterTests(unittest.TestCase):
    def quarters(self, *values):
        dates = ("2025-09-30", "2025-12-31", "2026-03-31", "2026-06-30", "2026-09-30")[-len(values):]
        return [{"date": d, "value": v} for d, v in zip(dates, values)]

    def test_depressed_quarter_is_mentioned_when_last_year_was_normal(self):
        eps, flags = check_eps({"date": "2026-09-30", "value": 40.4, "basis": "trailing 12 months"}, self.quarters(16.0, 16.8, 13.2, 11.4, -4.4 + 3.4), 300)
        self.assertEqual(len(flags), 1)
        self.assertIn("weak quarter (2026-09-30", flags[0])
        self.assertEqual(eps["value"], 40.4)  # only mentioned, never replaced

    def test_seasonal_or_unverifiable_weak_quarter_is_not_flagged(self):
        trailing = {"date": "2026-09-30", "value": 30.0, "basis": "trailing 12 months"}
        self.assertEqual(check_eps(trailing, self.quarters(2.0, 10.0, 9.0, 9.0, 2.0), 300)[1], [])  # weak a year ago too
        self.assertEqual(check_eps(trailing, self.quarters(10.0, 9.0, 9.0, 2.0), 300)[1], [])  # no year-ago quarter
        self.assertEqual(check_eps(trailing, self.quarters(10.0, 10.0, 9.0, 9.0, 8.0), 300)[1], [])  # nothing depressed



def payload(data, currency=None):
    """Fundamentals reply from {name: [(date, value), ...]}; money series tagged with `currency` when given."""
    def one(name, rows):
        return {"meta": {"type": [name]}, name: [{"asOfDate": d, "reportedValue": {"raw": v}, **({"currencyCode": currency} if currency and "Shares" not in name else {})}
                                                for d, v in rows]}
    return json.dumps({"timeseries": {"result": [one(k, v) for k, v in data.items()]}}).encode()


def fx_chart(rate_for):
    days = [NOW.date() - timedelta(days=k) for k in range(3000, -1, -1)]
    stamps = [int(instant(f"{d.isoformat()}T12:00:00+05:30").timestamp()) for d in days]
    return json.dumps({"chart": {"result": [{"meta": {"currency": "INR", "symbol": "USDINR=X", "regularMarketPrice": rate_for(days[-1])},
                                             "timestamp": stamps, "indicators": {"quote": [{"close": [rate_for(d) for d in days]}]}}]}}).encode()


def as_series(data):
    return {k: [{"date": d, "value": v} for d, v in rows] for k, rows in data.items()}


class WilderRsiTests(unittest.TestCase):
    def test_matches_the_wilder_reference_not_a_simple_sum(self):
        from patterns import rsi as wilder
        closes = [100 + 10 * math.sin(k / 3) + k * 0.2 for k in range(300)]
        self.assertAlmostEqual(rsi(closes), wilder(closes), places=6)
        # A recent run of gains: a 14-day sum (Cutler) reads 100 while Wilder's smoothed value stays lower.
        closes = [100.0 - (k % 2) * 3 + k * 0.05 for k in range(200)] + [110.0 + k for k in range(14)]
        gains = sum(max(b - a, 0) for a, b in zip(closes[-15:-1], closes[-14:]))
        losses = sum(max(a - b, 0) for a, b in zip(closes[-15:-1], closes[-14:]))
        self.assertEqual(losses, 0)
        self.assertGreater(gains, 0)
        self.assertLess(rsi(closes), 99.0)

    def test_forming_bar_is_left_out_of_rsi_and_volume(self):
        session = instant("2026-11-13T10:00:00+05:30")
        closes = [100 * 1.001 ** k for k in range(520)]
        closes[-1] = closes[-2] * 1.05
        raw = json.loads(chart_payload("EXAMPLE.NS", closes))
        raw["chart"]["result"][0]["indicators"]["quote"][0]["volume"][-1] = 8000  # a fraction of a normal day
        body = json.dumps(raw).encode()
        base = Feed(closes=closes)

        def feed(url):
            return body if "/v8/finance/chart/EXAMPLE" in url else base(url)
        r = analyse("example", fetcher=feed, news_fetcher=feed, clock=lambda: session, peers=False)
        t = r["technical"]
        self.assertTrue(t["forming_bar"])
        self.assertEqual(t["indicator_date"], "2026-11-12")
        self.assertAlmostEqual(t["volume_latest"] / t["volume_avg20"], 1.0)  # yesterday's full day, not 0.08×
        self.assertAlmostEqual(t["rsi14"], rsi(closes[:-1]))
        self.assertIn("still forming", markdown(r))


class UsdReporterTests(unittest.TestCase):
    """Infosys-style USD statements: each period converts at its own average rate, so growth is rupee growth."""
    DATA = {"annualTotalRevenue": [("2025-03-31", 1000.0), ("2026-03-31", 1000.0)],
            "annualNetIncome": [("2025-03-31", 100.0), ("2026-03-31", 100.0)],
            "annualDilutedEPS": [("2025-03-31", 0.8), ("2026-03-31", 0.8)],
            "annualDilutedAverageShares": [("2025-03-31", 125.0), ("2026-03-31", 125.0)],
            "annualStockholdersEquity": [("2025-03-31", 500.0), ("2026-03-31", 500.0)],
            "annualOrdinarySharesNumber": [("2026-03-31", 125.0)]}

    @staticmethod
    def rate(day):
        return 80.0 if day < date(2025, 4, 1) else 88.0

    def feed(self, fx_ok=True):
        def fetch(url):
            if "USDINR=X" in url:
                if not fx_ok:
                    return json.dumps({"chart": {"result": [{"meta": {"currency": "INR", "regularMarketPrice": 90.0}}]}}).encode()
                return fx_chart(self.rate)
            return payload(self.DATA, currency="USD")
        return fetch

    def test_growth_is_in_rupees_at_period_rates(self):
        series = fundamentals("INFY.NS", fetcher=self.feed(), now=NOW)
        self.assertEqual(series["_currency"]["basis"], "period")
        self.assertAlmostEqual(series["annualTotalRevenue"][0]["value"], 80_000.0)
        self.assertAlmostEqual(series["annualTotalRevenue"][1]["value"], 88_000.0)
        f = ratios(series, 1000.0, NOW)
        self.assertAlmostEqual(f["revenue_growth"], 0.10)  # flat in USD, +10% in rupees
        self.assertAlmostEqual(f["eps_growth"], 0.10)       # EPS rebuilt from rupee profit ÷ diluted shares
        self.assertAlmostEqual(series["annualDilutedEPS"][-1]["value"], 100 * 88 / 125)
        self.assertEqual(series["annualOrdinarySharesNumber"][0]["value"], 125.0)
        self.assertIn("converted from USD at period rates", series["_currency"]["label"])

    def test_fallback_to_todays_rate_says_growth_is_in_usd(self):
        series = fundamentals("INFY.NS", fetcher=self.feed(fx_ok=False), now=NOW)
        self.assertEqual(series["_currency"]["basis"], "today")
        self.assertIn("USD terms", series["_currency"]["note"])
        self.assertAlmostEqual(ratios(series, 1000.0, NOW)["revenue_growth"], 0.0)


class ChangeWordingTests(unittest.TestCase):
    def test_sign_changes_are_described_not_given_a_percentage(self):
        data = {"quarterlyNetIncome": [("2025-09-30", 76170e7), ("2026-06-30", 775e7), ("2026-09-30", -5531e7)],
                "annualDilutedEPS": [("2025-03-31", 78.75), ("2026-03-31", -0.61)]}
        f = ratios(as_series(data), 250.0, NOW)
        self.assertIsNone(f["quarter_profit_change"])
        self.assertEqual(f["quarter_profit_text"], "swung from ₹775 cr profit to ₹5,531 cr loss")
        self.assertEqual(f["quarter_profit_yoy_text"], "swung from ₹76,170 cr profit to ₹5,531 cr loss")
        self.assertIsNone(f["eps_growth"])
        self.assertEqual(f["eps_change_text"], "swung from ₹78.75 a share profit to ₹0.61 a share loss")
        self.assertTrue(any("not meaningful" in x for x in f["eps_flags"]))

    def test_profit_that_does_not_match_eps_is_flagged_and_roe_uses_trailing_figures(self):
        # TMPV FY2026: ₹82,390 cr profit (one-off demerger gain) against continuing EPS of -0.61.
        data = {"annualNetIncome": [("2025-03-31", 27830e7), ("2026-03-31", 82390e7)],
                "annualDilutedEPS": [("2025-03-31", 78.75), ("2026-03-31", -0.61)],
                "annualDilutedAverageShares": [("2026-03-31", 368.4e7)],
                "annualStockholdersEquity": [("2025-03-31", 116144e7), ("2026-03-31", 112068e7)],
                "quarterlyStockholdersEquity": [("2025-09-30", 110746e7), ("2026-09-30", 105126e7)],
                "annualTotalRevenue": [("2025-03-31", 363486e7), ("2026-03-31", 333383e7)],
                "trailingNetIncome": [("2026-09-30", -2460e7)], "trailingTotalRevenue": [("2026-09-30", 365695e7)],
                "quarterlyNetIncome": [("2025-12-31", -3486e7), ("2026-03-31", 5783e7), ("2026-06-30", 775e7), ("2026-09-30", -5531e7)]}
        f = ratios(as_series(data), 257.0, NOW)
        self.assertIsNotNone(f["one_off"])
        self.assertTrue(any("about ₹223.64 a share" in x and "−₹0.61" in x for x in f["eps_flags"]))
        self.assertAlmostEqual(f["roe"], -2460 / ((105126 + 110746) / 2))
        self.assertIn("trailing 12 months", f["roe_basis"])
        self.assertGreater(f["roe_annual"], 0.7)  # the misleading 72%-style figure is kept only for reference
        self.assertAlmostEqual(f["net_margin"], -2460 / 365695)

    def test_book_value_uses_the_newest_balance_sheet(self):
        f = run(Feed(fundamentals=fundamentals_payload(quarterlyStockholdersEquity=[("2026-09-30", 750.0)])))["fundamentals"]
        self.assertAlmostEqual(f["pb"], (100 * 1.001 ** 519) / 75.0)  # not the March 600
        self.assertEqual(f["book_date"], "2026-09-30")

    def test_one_off_year_ago_quarter_is_called_out(self):
        from stock_analysis import flag_unusual_base
        fund = {"quarter_profit_values": (-5531.0, 76170.0), "quarter_profits": [76170.0, -3486.0, 5783.0, 775.0, -5531.0],
                "quarter_year_ago": "2025-09-30"}
        flag_unusual_base(fund)
        self.assertIn("unusually large profit", fund["quarter_note"])
        fund = {"quarter_profit_values": (110.0, 100.0), "quarter_profits": [100.0, 90.0, 95.0, 105.0, 110.0], "quarter_year_ago": "2025-09-30"}
        flag_unusual_base(fund)
        self.assertNotIn("quarter_note", fund)


class DemergerStatementTests(unittest.TestCase):
    PARENT = [{"parent": "AAA", "resulting_symbol": "BBB", "resulting_company": "Bee Ltd", "record_date": "2025-10-14",
               "appointed_date": "2025-07-01", "resulting_first_full_fy_end": "2027-03-31", "cost_split_pct": {"parent": 68.85, "resulting": 31.15}}]
    DATA = {"annualTotalRevenue": [("2025-03-31", 57788e7), ("2026-03-31", 83390e7)],
            "annualDilutedEPS": [("2025-03-31", 8.67), ("2026-03-31", 8.23)],
            "annualNetIncome": [("2025-03-31", 3195e7), ("2026-03-31", 3030e7)],
            "annualStockholdersEquity": [("2025-03-31", 10533e7), ("2026-03-31", 12734e7)],
            "quarterlyStockholdersEquity": [("2026-09-30", 15288e7)],
            "trailingNetIncome": [("2026-09-30", 6317e7)], "trailingTotalRevenue": [("2026-09-30", 93515e7)],
            "quarterlyNetIncome": [("2025-09-30", -867e7), ("2025-12-31", 705e7), ("2026-03-31", 1793e7), ("2026-06-30", 2560e7), ("2026-09-30", 1257e7)]}

    def test_resulting_company_withholds_partial_year_growth_and_uses_trailing_roe(self):
        from stock_analysis import demerger_statements
        series = as_series(self.DATA)
        f = ratios(series, 440.0, NOW)
        self.assertAlmostEqual(f["revenue_growth"], 83390 / 57788 - 1)  # the misleading +44% before the check
        notes = demerger_statements(f, series, "BBB", None, self.PARENT)
        self.assertIsNone(f["revenue_growth"])
        self.assertIsNone(f["eps_growth"])
        self.assertTrue(f["pe_band_withheld"])
        self.assertIn("not meaningful until the FY2027 results", notes[0])
        self.assertIn("trailing 12 months", f["roe_basis"])
        self.assertEqual(f["quarter_profit_yoy_text"], "swung from ₹867 cr loss to ₹1,257 cr profit")  # Jul-Sep 2025 follows the appointed date

    def test_parent_year_that_straddles_the_demerger_is_not_compared(self):
        from stock_analysis import demerger_statements
        series = as_series(self.DATA)
        f = ratios(series, 250.0, NOW)
        notes = demerger_statements(f, series, "AAA", None, self.PARENT)
        self.assertIsNone(f["revenue_growth"])
        self.assertIn("straddles the demerger of Bee Ltd", notes[0])
        small = [{**self.PARENT[0], "cost_split_pct": {"parent": 98.09, "resulting": 1.91}}]
        f = ratios(series, 250.0, NOW)
        self.assertEqual(demerger_statements(f, series, "AAA", None, small), [])  # a 2% demerger leaves the business comparable
        self.assertIsNotNone(f["revenue_growth"])


class BandWordingTests(unittest.TestCase):
    def test_percentile_is_said_plainly(self):
        from stock_analysis import band_words
        self.assertEqual(band_words({"percentile": 0.2, "start": "2024-05-30"}), "cheaper than on 80% of trading days since May 2024")
        self.assertEqual(band_words({"percentile": 0.85, "start": "2021-06-01"}), "more expensive than on 85% of trading days since Jun 2021")


class NseQuarterTests(unittest.TestCase):
    XBRL = ('<?xml version="1.0"?><xbrli:xbrl xmlns:xbrli="http://www.xbrl.org/2003/instance" xmlns:in-capmkt="http://x">'
            '<xbrli:context id="OneD"><xbrli:entity><xbrli:identifier scheme="s">1</xbrli:identifier></xbrli:entity><xbrli:period>'
            '<xbrli:startDate>{s}</xbrli:startDate><xbrli:endDate>{e}</xbrli:endDate></xbrli:period></xbrli:context>'
            '<xbrli:context id="FourD"><xbrli:entity><xbrli:identifier scheme="s">1</xbrli:identifier></xbrli:entity><xbrli:period>'
            '<xbrli:startDate>{y}</xbrli:startDate><xbrli:endDate>{e}</xbrli:endDate></xbrli:period></xbrli:context>'
            '<in-capmkt:RevenueFromOperations contextRef="OneD" unitRef="INR" decimals="-5">{rev}</in-capmkt:RevenueFromOperations>'
            '<in-capmkt:RevenueFromOperations contextRef="FourD" unitRef="INR" decimals="-5">999999</in-capmkt:RevenueFromOperations>'
            '<in-capmkt:ProfitLossForPeriod contextRef="OneD" unitRef="INR" decimals="-5">{total}</in-capmkt:ProfitLossForPeriod>'
            '<in-capmkt:ProfitOrLossAttributableToOwnersOfParent contextRef="OneD" unitRef="INR" decimals="-5">{owners}</in-capmkt:ProfitOrLossAttributableToOwnersOfParent>'
            '<in-capmkt:DilutedEarningsLossPerShare contextRef="OneD" unitRef="INRPerShare" decimals="2">18.88</in-capmkt:DilutedEarningsLossPerShare>'
            '</xbrli:xbrl>')

    def filing(self, start, end, rev, owners):
        return self.XBRL.format(s=start, e=end, y=start[:5] + "04-01", rev=rev, total=owners * 11 // 10, owners=owners).encode()

    def test_year_on_year_quarter_comes_from_exchange_filings(self):
        from stock_analysis import apply_quarter_results, nse_quarter_results
        base = "https://nsearchives.nseindia.in/corporate/xbrl/"
        listing = {"data": [
            {"qe_Date": "30-SEP-2026", "consolidated": "Consolidated", "type": "Integrated Filing- Financials", "xbrl": base + "INTEGRATED_FILING_INDAS_2_x.xml", "broadcast_Date": "30-Oct-2026 21:32:55"},
            {"qe_Date": "30-SEP-2026", "consolidated": "Standalone", "type": "Integrated Filing- Financials", "xbrl": base + "INTEGRATED_FILING_INDAS_3_x.xml", "broadcast_Date": "30-Oct-2026 21:32:05"},
            {"qe_Date": "30-SEP-2025", "consolidated": "Consolidated", "type": "Integrated Filing- Financials", "xbrl": base + "INTEGRATED_FILING_INDAS_1_x.xml", "broadcast_Date": "17-Oct-2025 19:50:03"},
            {"qe_Date": "30-SEP-2026", "consolidated": None, "type": "Integrated Filing- Governance", "xbrl": base + "INTEGRATED_FILING_GOVERNANCE_9.xml"}]}
        files = {"INDAS_2_": self.filing("2026-07-01", "2026-09-30", 3328610000000, 255480000000),
                 "INDAS_1_": self.filing("2025-07-01", "2025-09-30", 2546230000000, 181650000000)}

        def fetch(url):
            if "integrated-filing-results" in url:
                return json.dumps(listing).encode()
            return next(v for k, v in files.items() if k in url)
        nse = nse_quarter_results("RELIANCE", fetch)
        self.assertEqual((nse["latest"]["quarter"], nse["year_ago"]["quarter"]), ("2026-09-30", "2025-09-30"))
        self.assertEqual(nse["latest"]["basis"], "consolidated")
        self.assertEqual(nse["latest"]["profit"], 255480000000)  # attributable to shareholders, not the total
        self.assertEqual(nse["latest"]["revenue"], 3328610000000)  # the quarter, not the half-year
        fund = {"latest_quarter": {"date": "2026-09-30", "value": 1.0}}
        apply_quarter_results(fund, nse)
        self.assertAlmostEqual(fund["quarter_profit_yoy"], 25548 / 18165 - 1)
        self.assertAlmostEqual(fund["quarter_revenue_yoy"], 332861 / 254623 - 1)
        self.assertIn("NSE consolidated results", fund["quarter_basis"])
        self.assertIsNone(nse_quarter_results("RELIANCE", lambda url: b"<html>"))


class DividendRecordTests(unittest.TestCase):
    def chart(self, pays):
        events = {}
        for fy, amount in pays.items():
            stamp = int(instant(f"{fy - 1}-08-01T09:15:00+05:30").timestamp())
            events[str(stamp)] = {"amount": amount, "date": stamp}
        first = int(instant("2016-12-01T09:15:00+05:30").timestamp())
        return json.dumps({"chart": {"result": [{"meta": {"symbol": "EXAMPLE.NS", "currency": "INR"}, "timestamp": [first],
                                                 "events": {"dividends": events}}]}}).encode()

    def test_years_in_a_row_growth_payout_and_cover(self):
        from stock_analysis import dividend_record
        pays = {2017: 4.0, 2018: 5.0, 2019: 5.0, 2020: 6.0, 2021: 8.0, 2022: 9.0, 2023: 10.0, 2024: 12.0, 2025: 14.0, 2026: 16.0}
        raw = self.chart(pays)
        d = dividend_record("EXAMPLE.NS", fetcher=lambda url: raw, now=NOW, fund={"payout_ratio": 0.4, "fcf_cover": 2.5})
        self.assertEqual(d["years_paid_in_a_row"], 9)  # FY2018-FY2026; FY2017 is the partial first year of data
        self.assertTrue(d["streak_capped"])
        self.assertAlmostEqual(d["dps_cagr_5y"], ((16 + 14) / (8 + 6)) ** 0.2 - 1)  # two-year averages at each end
        self.assertEqual(d["cagr_years"], "FY2020-21 to FY2025-26 averages")
        self.assertEqual((d["payout_ratio"], d["fcf_cover"]), (0.4, 2.5))
        del pays[2023]
        raw = self.chart(pays)
        self.assertEqual(dividend_record("EXAMPLE.NS", fetcher=lambda url: raw, now=NOW)["years_paid_in_a_row"], 3)

    def test_payout_and_cover_come_from_the_statements(self):
        r = run(Feed())
        f = r["fundamentals"]
        self.assertAlmostEqual(f["payout_ratio"], 20 / 110)
        self.assertAlmostEqual(f["fcf_cover"], 50 / 20)
        text = markdown(r)
        self.assertIn("Dividend payout (cash paid ÷ net profit, last FY)", text)
        self.assertIn("Financial years with a dividend, in a row", text)



NEWS_MIX = b"""<?xml version="1.0"?><rss><channel>
<item><title>Six Stocks To Buy Today, November 13: Bank of Baroda, Example Industries, And More - NDTV Profit</title><link>https://news.google.com/rss/articles/t1</link><pubDate>Fri, 13 Nov 2026 07:00:00 GMT</pubDate></item>
<item><title>Example Industries Share Price Target at Rs 500: ICICI Direct - TopNews</title><link>https://news.google.com/rss/articles/t2</link><pubDate>Fri, 13 Nov 2026 06:30:00 GMT</pubDate></item>
<item><title>How the Nifty sprint left 10 heavyweight stocks in the dust - The Economic Times</title><link>https://news.google.com/rss/articles/t3</link><pubDate>Fri, 13 Nov 2026 06:00:00 GMT</pubDate></item>
<item><title>Sensex rises 0.8%; Example Industries tops large-cap gains - MarketsMojo</title><link>https://news.google.com/rss/articles/t4</link><pubDate>Fri, 13 Nov 2026 05:30:00 GMT</pubDate></item>
<item><title>Example Industries wins Rs 2,000 crore order - Mint</title><link>https://news.google.com/rss/articles/t5</link><pubDate>Fri, 13 Nov 2026 04:00:00 GMT</pubDate></item>
<item><title>Rs 1,330 Puts Draw 2,493 Contracts on Example Industries Ltd - MarketsMojo</title><link>https://news.google.com/rss/articles/t6</link><pubDate>Fri, 13 Nov 2026 03:00:00 GMT</pubDate></item>
</channel></rss>"""


class NewsFeed(Feed):
    def __call__(self, url):
        if "news.google.com" in url:
            self.urls.append(url)
            return NEWS_MIX
        return super().__call__(url)


class HeadlineTests(unittest.TestCase):
    def test_tip_lists_price_targets_and_round_ups_are_left_out(self):
        r = run(NewsFeed(), peers=False)
        heads = [i["headline"] for i in r["news"]["items"]]
        self.assertEqual(heads, ["Sensex rises 0.8%; Example Industries tops large-cap gains", "Example Industries wins Rs 2,000 crore order"])
        self.assertEqual(r["news"]["dropped"], {"tips": 4, "unrelated": 0})
        self.assertIn("4 headline(s) left out: stock-tip lists, price targets and quote pages", r["news"]["left_out"])

    def test_summary_and_full_report_pick_the_same_headline_about_the_company(self):
        r = run(NewsFeed(), peers=False)
        # The newer market round-up only mentions the company; the story about the company itself is the headline.
        self.assertEqual(r["news"]["headline"]["headline"], "Example Industries wins Rs 2,000 crore order")
        self.assertIn("Latest headline (2026-11-13): Example Industries wins Rs 2,000 crore order · Mint", summary(r))
        self.assertIn("Latest headline about the company (2026-11-13): Example Industries wins Rs 2,000 crore order · Mint", markdown(r))
        self.assertEqual(run(NewsFeed(), peers=False)["news"]["headline"], r["news"]["headline"])  # same pick on a rerun

    def test_headline_must_name_the_company_not_its_sister_company(self):
        import stock_analysis as sa
        aliases = {"TATAMOTORS": {"old": "TATAMOTORS", "successors": [
            {"symbol": "TMPV", "what": "cars", "news_terms": ["passenger vehicle", "JLR"]},
            {"symbol": "TMCV", "what": "trucks", "news_terms": ["commercial vehicle", "trucks"]}]}}
        demergers = [{"parent": "TMPV", "resulting_symbol": "TMCV"}]
        m = sa.headline_matcher("TMCV", "Tata Motors Limited", aliases=aliases, demergers=demergers)
        verdict = lambda h: sa.headline_verdict(h, m)
        self.assertEqual(verdict("Tata Motors (TMPV) share price falls after JLR update"), "unrelated")
        self.assertEqual(verdict("Tata Motors (TMPV) Share Price Target at Rs 275: ICICI Direct"), "tips")
        self.assertEqual(verdict("Tata Motors commercial vehicle sales rise 12%"), "keep")
        # Both successors are still "Tata Motors" in headlines, so the shared name alone does not say which one.
        self.assertEqual(verdict("Tata Motors shares slip 2%"), "unrelated")
        self.assertEqual(verdict("TMCV hits a 52-week high"), "keep")
        self.assertEqual(verdict("Ashok Leyland Q2 profit rises"), "unrelated")
        r = sa.headline_matcher("RELIANCE", "Reliance Industries Limited", aliases={}, demergers=[])
        self.assertEqual(sa.headline_verdict("Reliance Power shares hit upper circuit", r), "unrelated")
        self.assertEqual(sa.headline_verdict("Reliance shares gain 2% on Jio listing plan", r), "keep")
        self.assertEqual(sa.headline_verdict("Reliance to sell 5 crore shares of a unit", r), "keep")  # not a round-up


class RevenueCheckTests(unittest.TestCase):
    def fund(self, **override):
        import stock_analysis as sa
        payload = fundamentals_payload(**override)
        series = sa.fundamentals("EXAMPLE.NS", fetcher=lambda url: payload, now=NOW)
        return sa, series, sa.ratios(series, 100.0, NOW)

    def test_revenue_that_more_than_doubled_is_withheld_without_filings(self):
        sa, series, f = self.fund(annualTotalRevenue=[("2025-03-31", 1347.0), ("2026-03-31", 3185.0)],
                                  annualNetIncome=[("2025-03-31", 1613.0), ("2026-03-31", 1561.0)])
        self.assertGreater(f["revenue_growth"], 1.0)
        note = sa.check_revenue(f, series)
        self.assertIsNone(f["revenue_growth"])
        self.assertIsNone(f["net_margin"])
        self.assertIn("revenue +136% in FY2026 vs FY2025", note)
        self.assertIn(note, f["eps_flags"])
        sa2, series2, ok = self.fund()
        self.assertIsNone(sa2.check_revenue(ok, series2))  # +10%: left alone
        self.assertAlmostEqual(ok["revenue_growth"], 0.1)

    def test_filings_replace_yahoo_revenue_when_the_quarter_disagrees(self):
        sa, series, f = self.fund(quarterlyTotalRevenue=[("2026-09-30", 149.65)])
        nse = {"latest": {"quarter": "2026-09-30", "revenue": 245.46, "profit": 62.66, "basis": "consolidated"}}
        annual = {"2026-03-31": {"quarter": "2026-03-31", "revenue": 819.82, "profit": 190.17, "basis": "consolidated"},
                  "2025-03-31": {"quarter": "2025-03-31", "revenue": 696.84, "profit": 166.38, "basis": "consolidated"}}
        note = sa.check_revenue(f, series, nse, lambda: annual)
        self.assertAlmostEqual(f["revenue_growth"], 819.82 / 696.84 - 1)
        self.assertAlmostEqual(f["net_margin"], 190.17 / 819.82)
        self.assertEqual(f["revenue_basis"], "NSE annual results (March-quarter filings)")
        self.assertIn("quarter revenue", note)
        self.assertIn("revenue growth and net margin use NSE's consolidated annual results", note)
        calls = []
        sa, series, f = self.fund(quarterlyTotalRevenue=[("2026-09-30", 240.0)])
        self.assertIsNone(sa.check_revenue(f, series, nse, lambda: calls.append(1)))  # within 10%: no extra fetch
        self.assertEqual(calls, [])

    def test_annual_figures_come_from_the_march_filing_year_column(self):
        import stock_analysis as sa

        def xbrl(end, quarter_rev, year_rev):
            def ctx(cid, a, b):
                return (f'<xbrli:context id="{cid}"><xbrli:entity><xbrli:identifier scheme="s">1</xbrli:identifier></xbrli:entity>'
                        f'<xbrli:period><xbrli:startDate>{a}</xbrli:startDate><xbrli:endDate>{b}</xbrli:endDate></xbrli:period></xbrli:context>')
            year = str(int(end[:4]) - 1)
            return ('<xbrli:xbrl xmlns:xbrli="http://www.xbrl.org/2003/instance" xmlns:c="x">' + ctx("Q", end[:4] + "-01-01", end)
                    + ctx("Y", year + "-04-01", end)
                    + f'<c:RevenueFromOperations contextRef="Q">{quarter_rev}</c:RevenueFromOperations>'
                    + f'<c:RevenueFromOperations contextRef="Y">{year_rev}</c:RevenueFromOperations>'
                    + '<c:ProfitLossForPeriod contextRef="Y">15609000000</c:ProfitLossForPeriod></xbrli:xbrl>').encode()
        files = {"m26": xbrl("2026-03-31", 10185000000, 35133000000), "m25": xbrl("2025-03-31", 4180000000, 20790000000)}
        nse = {"latest": {"basis": "consolidated"}, "filings": [
            {"quarter": "2026-09-30", "url": "s26", "basis": "consolidated"}, {"quarter": "2026-03-31", "url": "m26", "basis": "consolidated"},
            {"quarter": "2025-03-31", "url": "m25", "basis": "consolidated"}, {"quarter": "2024-03-31", "url": "m24", "basis": "consolidated"}]}
        seen = []
        out = sa.nse_annual_results(nse, lambda url: seen.append(url) or files[url])
        self.assertEqual(sorted(out), ["2025-03-31", "2026-03-31"])
        self.assertEqual(out["2026-03-31"]["revenue"], 35133000000.0)  # the full year, not the March quarter
        self.assertEqual(seen, ["m26", "m25"])  # two years only

    def test_operating_profit_above_ebitda_falls_back_to_ebit(self):
        sa, series, f = self.fund(annualOperatingIncome=[("2026-03-31", 408.0)], annualEBITDA=[("2026-03-31", 273.0)],
                                  annualEBIT=[("2026-03-31", 230.0)])
        self.assertAlmostEqual(f["operating_margin"], 230 / 1100)
        self.assertAlmostEqual(f["ebitda_margin"], 273 / 1100)
        sa, series, f = self.fund(annualEBITDA=[("2026-03-31", 200.0)])
        self.assertAlmostEqual(f["operating_margin"], 165 / 1100)  # consistent figures are kept


if __name__ == "__main__":
    unittest.main()
