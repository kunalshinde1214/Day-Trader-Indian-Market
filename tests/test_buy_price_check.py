"""Offline tests for the buy/sell price plausibility check in the holdings review; never contacts the network."""
from datetime import date, datetime, timedelta
from decimal import Decimal
import json
from pathlib import Path
import sys
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT / "tests"))
from core import IST, instant
import holdings_review as hr
from test_holdings_review import NIFTY500, holdings_doc

NOW = instant("2026-11-18T10:00:00+05:30")


def weekdays(start, end):
    day, out = start, []
    while day <= end:
        if day.weekday() < 5:
            out.append(day)
        day += timedelta(days=1)
    return out


def payload(symbol, bars, splits=()):
    """Yahoo daily chart from (date, low, high) bars; `splits` are (date, numerator, denominator)."""
    stamp = lambda d: int(datetime.combine(d, datetime.min.time(), IST).replace(hour=9, minute=15).timestamp())
    closes = [round((lo + hi) / 2, 2) for _, lo, hi in bars]
    result = {"meta": {"symbol": symbol, "currency": "INR", "exchangeTimezoneName": "Asia/Kolkata", "dataGranularity": "1d"},
              "timestamp": [stamp(d) for d, _, _ in bars],
              "indicators": {"quote": [{"open": closes, "high": [hi for _, _, hi in bars], "low": [lo for _, lo, _ in bars],
                                        "close": closes, "volume": [1000] * len(bars)}]}}
    if splits:
        result["events"] = {"splits": {str(stamp(d)): {"date": stamp(d), "numerator": n, "denominator": m} for d, n, m in splits}}
    return json.dumps({"chart": {"error": None, "result": [result]}}).encode()


def flat(start, end, low, high):
    return [(d, low, high) for d in weekdays(start, end)]


class Feed:
    """Serves the given symbol -> (bars, splits); anything else gets a flat 100 history from 2024."""
    def __init__(self, charts):
        self.charts = charts

    def __call__(self, url):
        if "ind_nifty500list.csv" in url:
            return NIFTY500
        sym = url.split("/chart/")[1].split("?")[0].replace(".NS", "")
        bars, splits = self.charts.get(sym, (flat(date(2024, 1, 1), date(2026, 11, 17), 99.0, 101.0), ()))
        return payload(sym + ".NS", bars, splits)


# SBIN in Oct 2026 traded 937.50-983.80; the rest of the history sits near 950.
SBIN_BARS = (flat(date(2024, 1, 1), date(2026, 9, 30), 930.0, 960.0) + flat(date(2026, 10, 1), date(2026, 10, 15), 937.5, 960.0)
             + flat(date(2026, 10, 16), date(2026, 10, 31), 950.0, 983.8) + flat(date(2026, 11, 2), date(2026, 11, 17), 960.0, 975.0))


def run(holdings, lots=None, charts=None, **kw):
    return hr.review(holdings_doc(holdings, lots), None, fetcher=Feed(charts or {"SBIN": (SBIN_BARS, ())}), clock=lambda: NOW,
                     sleep=lambda _: None, **kw)


def row(report, symbol):
    return next(h for h in report["holdings"] if h["symbol"] == symbol)


class BuyPriceTests(unittest.TestCase):
    def test_typo_price_outside_the_month_range_is_flagged_but_never_changed(self):
        r = run([{"symbol": "SBIN", "quantity": 40, "average_price": 600, "bought": "2026-10"}])
        sbin = row(r, "SBIN")
        self.assertEqual(sbin["price_warning"],
                         "₹600 (bought) is outside the ₹937–984 range traded in Oct 2026 — check the price or date.")
        self.assertEqual((sbin["quantity"], sbin["average_price"], sbin["invested"]), (40.0, 600.0, 24000.0))  # user's numbers kept
        check = sbin["price_checks"][0]
        self.assertEqual((check["status"], check["low"], check["high"], check["period_start"], check["period_end"]),
                         ("outside", 937.5, 983.8, "2026-10-01", "2026-10-31"))
        text = hr.markdown(r)
        self.assertIn("| SBIN ⚠ |", text)
        self.assertIn("**⚠ Check before trusting these numbers**", text)
        self.assertIn("- **SBIN**: ₹600 (bought) is outside the ₹937–984 range traded in Oct 2026", text)
        brief = hr.summary(r)
        self.assertIn("⚠ 1 item to check**: buy/sell price SBIN", brief)
        self.assertIn("⚠ SBIN typed price is outside what traded then", brief)

    def test_price_inside_the_range_or_within_tolerance_is_not_flagged(self):
        for price, flagged in ((950, False), (937.5, False), (910, False), (905, True), (1013, False), (1015, True)):
            with self.subTest(price=price):
                sbin = row(run([{"symbol": "SBIN", "quantity": 40, "average_price": price, "bought": "2026-10"}]), "SBIN")
                self.assertEqual(bool(sbin["price_warning"]), flagged)
        clean = run([{"symbol": "SBIN", "quantity": 40, "average_price": 950, "bought": "2026-10"}])
        self.assertIn("Nothing flagged", hr.summary(clean))
        self.assertEqual(row(clean, "SBIN")["price_checks_ok"], 1)

    def test_full_date_uses_that_day_and_a_holiday_uses_the_days_around_it(self):
        sbin = row(run([{"symbol": "SBIN", "quantity": 1, "average_price": 1000, "bought": "2026-10-05"}]), "SBIN")
        self.assertIn("outside the ₹937–960 range traded on 2026-10-05", sbin["price_warning"])  # fits the month, not that day
        sunday = row(run([{"symbol": "SBIN", "quantity": 1, "average_price": 500, "bought": "2026-10-04"}]), "SBIN")
        self.assertIn("the days around 2026-10-04 (no trading that day)", sunday["price_warning"])

    def test_lots_and_sells_are_checked_and_purchases_before_the_history_are_skipped(self):
        lots = [{"symbol": "SBIN", "quantity": 10, "price": 950, "date": "2026-10-20"},
                {"symbol": "SBIN", "quantity": 5, "price": 1500, "date": "2026-11-02", "side": "SELL"},
                {"symbol": "SBIN", "quantity": 10, "price": 50, "date": "2019-01-01"}]
        sbin = row(run([{"symbol": "SBIN", "quantity": 15, "average_price": 950}], lots), "SBIN")
        self.assertEqual(len(sbin["price_checks"]), 1)
        self.assertIn("₹1,500 (sold) is outside", sbin["price_warning"])
        self.assertEqual((sbin["price_checks_ok"], sbin["price_checks_not_checked"]), (1, 1))

    def test_prices_before_a_split_are_compared_in_the_units_of_the_time(self):
        # Yahoo shows the 2023 bars in today's units (~1,200) and a 2-for-1 event on 2024-10-28.
        charts = {"AAA": (flat(date(2023, 1, 1), date(2026, 11, 17), 1150.0, 1250.0), [(date(2024, 10, 28), 2, 1)])}
        ok = row(run([{"symbol": "AAA", "quantity": 30, "average_price": 2400, "bought": "2023-01"}], charts=charts, splits=["AAA"]), "AAA")
        self.assertIsNone(ok["price_warning"])
        self.assertEqual(ok["quantity"], 60.0)
        twice = row(run([{"symbol": "AAA", "quantity": 60, "average_price": 1200, "bought": "2023-01"}], charts=charts, splits=["AAA"]), "AAA")
        self.assertIn("outside the ₹2,300–2,500 range traded in Jan 2023", twice["price_warning"])
        self.assertIn("fits the ₹1,150–1,250 range in today's share units", twice["price_warning"])
        self.assertIn("adjusted them a second time", twice["price_warning"])

    def test_before_a_listed_demerger_only_a_low_price_is_flagged(self):
        charts = {"AAA": (flat(date(2023, 1, 1), date(2026, 11, 17), 90.0, 110.0), ())}
        demerger = [{"parent": "AAA", "symbols": ["AAA"], "parent_name": "AAA Ltd", "resulting_company": "BBB Ltd", "resulting_symbol": "BBB",
                     "record_date": date(2024, 6, 1), "new": 1, "per": 1, "parent_pct": Decimal("80"), "resulting_pct": Decimal("20"),
                     "sources": ["https://example.invalid/x.pdf"], "checked": date(2026, 11, 18)}]
        high = row(run([{"symbol": "AAA", "quantity": 10, "average_price": 130, "bought": "2024-01"}], charts=charts, demergers=demerger), "AAA")
        self.assertIsNone(high["price_warning"])  # Yahoo may have scaled pre-demerger prices down
        low = row(run([{"symbol": "AAA", "quantity": 10, "average_price": 60, "bought": "2024-01"}], charts=charts, demergers=demerger), "AAA")
        self.assertIn("demerger on 2024-06-01", low["price_warning"])
        after = row(run([{"symbol": "AAA", "quantity": 10, "average_price": 130, "bought": "2024-07"}], charts=charts, demergers=demerger), "AAA")
        self.assertIn("outside the ₹90–110 range", after["price_warning"])

    def test_no_prices_means_no_check(self):
        r = run([{"symbol": "SBIN", "quantity": 40, "average_price": 600, "bought": "2026-10"}], prices=False)
        self.assertIsNone(row(r, "SBIN")["price_warning"])


if __name__ == "__main__":
    unittest.main()
