"""Offline tests for the portfolio answer: live price labels, demerged shares valued and taxed, Income-tax Act 2025 wording,
the typed-price midpoint alternative, returns against the Nifty 50, sale costs and plain summary wording."""
from datetime import date, datetime, timedelta
from decimal import Decimal
import json
from pathlib import Path
import sys
import unittest
import urllib.parse

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT / "tests"))
from core import IST, instant
import holdings_review as hr
from position_size import DELIVERY
from test_buy_price_check import SBIN_BARS, flat
from test_holdings_review import NIFTY500, holdings_doc

NOW = instant("2026-11-18T10:00:00+05:30")   # a Wednesday, session open
TODAY = NOW.date()
QUOTE_AT = int(datetime(2026, 11, 18, 9, 58, tzinfo=IST).timestamp())
RATES = dict(hr.DEFAULT_RATES)


def payload(selected, bars, splits=()):
    stamp = lambda d: int(datetime.combine(d, datetime.min.time(), IST).replace(hour=9, minute=15).timestamp())
    closes = [round((lo + hi) / 2, 2) for _, lo, hi in bars]
    result = {"meta": {"symbol": selected, "currency": "INR", "exchangeTimezoneName": "Asia/Kolkata", "dataGranularity": "1d",
                       "regularMarketTime": QUOTE_AT, "regularMarketPrice": closes[-1]},
              "timestamp": [stamp(d) for d, _, _ in bars],
              "indicators": {"quote": [{"open": closes, "high": [hi for _, _, hi in bars], "low": [lo for _, lo, _ in bars],
                                        "close": closes, "volume": [1000] * len(bars)}]}}
    if splits:
        result["events"] = {"splits": {str(stamp(d)): {"date": stamp(d), "numerator": n, "denominator": m} for d, n, m in splits}}
    return json.dumps({"chart": {"error": None, "result": [result]}}).encode()


def today(price):
    return [(TODAY, price - 1, price + 1)]


CHARTS = {
    # Yahoo bars are in today's units: before the 2024 1:1 bonus RELIANCE shows half its price of the time.
    "RELIANCE": (flat(date(2022, 1, 3), date(2024, 10, 25), 1150.0, 1250.0) + flat(date(2024, 10, 28), date(2026, 11, 17), 1300.0, 1400.0)
                 + today(3000.0), [(date(2024, 10, 28), 2, 1)]),
    "ITC": (flat(date(2022, 1, 3), date(2025, 1, 3), 400.0, 440.0) + flat(date(2025, 1, 6), date(2026, 11, 17), 280.0, 300.0) + today(500.0), []),
    "ITCHOTELS": (flat(date(2025, 1, 29), date(2026, 11, 17), 170.0, 180.0) + today(176.0), []),
    "JIOFIN": (flat(date(2023, 8, 21), date(2026, 11, 17), 220.0, 230.0) + today(225.0), []),
    "SBIN": (SBIN_BARS + today(1110.0), []),
    "INFY": (flat(date(2022, 1, 3), date(2026, 11, 17), 1100.0, 1140.0) + today(1120.0), []),
    "^NSEI": (flat(date(2022, 1, 3), date(2023, 12, 29), 17900.0, 18100.0) + flat(date(2024, 1, 1), date(2026, 11, 17), 24900.0, 25100.0)
              + today(25000.0), []),
}


class Feed:
    def __init__(self, charts=CHARTS, fail=()):
        self.charts, self.fail, self.urls = charts, set(fail), []

    def __call__(self, url):
        self.urls.append(url)
        if "ind_nifty500list.csv" in url:
            return NIFTY500
        name = urllib.parse.unquote(url.split("/chart/")[1].split("?")[0])
        sym = name[:-3] if name.endswith(".NS") else name
        if sym in self.fail or sym not in self.charts:
            from core import Block
            raise Block("PUBLIC: source unavailable")
        bars, splits = self.charts[sym]
        return payload(name, bars, splits)


QUESTION = [{"symbol": "ITC", "quantity": 200, "average_price": 380, "bought": "2023-06"},
            {"symbol": "RELIANCE", "quantity": 30, "average_price": 2400, "bought": "2023-01"},
            {"symbol": "INFY", "quantity": 15, "average_price": 1500},
            {"symbol": "SBIN", "quantity": 40, "average_price": 600, "bought": "2026-10"}]


def run(holdings=QUESTION, feed=None, **kw):
    return hr.review(holdings_doc(holdings), None, fetcher=feed or Feed(), clock=lambda: NOW, sleep=lambda _: None, **kw)


REPORT = run()


def money(x):
    return Decimal(str(x))


class LivePriceLabelTests(unittest.TestCase):
    def test_session_prices_are_labelled_live_with_time_and_last_close(self):
        t = REPORT["totals"]
        self.assertEqual((t["price_kind"], t["price_times"], t["last_close_dates"]), ("live", ["09:58"], ["2026-11-17"]))
        md = hr.markdown(REPORT)
        self.assertIn("prices: live price 09:58 IST (session in progress; Yahoo, may lag a minute or more); last close 2026-11-17", md)
        self.assertIn("| ₹3,000.00 (live 09:58) |", md)
        self.assertIn("Day change (live vs 2026-11-17 close)", md)
        self.assertNotIn("daily close dated", md)
        self.assertIn("since the 2026-11-17 close", hr.summary(REPORT))

    def test_after_the_session_the_close_is_labelled_with_its_date(self):
        evening = NOW.replace(hour=17)
        r = hr.review(holdings_doc(QUESTION), None, fetcher=Feed(), clock=lambda: evening, sleep=lambda _: None)
        self.assertEqual(r["totals"]["price_kind"], "close")
        self.assertIn("prices: closing price 2026-11-18 (Yahoo)", hr.markdown(r))
        self.assertNotIn("live", hr.summary(r).split("_prices")[1])


class DemergedSharesTests(unittest.TestCase):
    def test_unlisted_demerged_shares_are_valued_with_apportioned_cost(self):
        got = {x["symbol"]: x for x in REPORT["demerged_shares"]}
        self.assertEqual(set(got), {"JIOFIN", "ITCHOTELS"})
        self.assertEqual((got["JIOFIN"]["quantity"], got["JIOFIN"]["value"], got["JIOFIN"]["cost"]), (30.0, 6750.0, 3369.6))   # 4.68% of ₹72,000
        self.assertEqual((got["ITCHOTELS"]["quantity"], got["ITCHOTELS"]["value"], got["ITCHOTELS"]["cost"]), (20.0, 3520.0, 10267.6))  # 13.51% of ₹76,000
        w = REPORT["with_demerged_shares"]
        base = REPORT["totals"]["current_value"]
        self.assertEqual((w["extra_value"], w["current_value"]), (10270.0, base + 10270.0))
        self.assertAlmostEqual(w["unrealised_pnl"], base + 10270.0 - REPORT["totals"]["invested_priced"])
        line = hr.demerged_line(REPORT)
        self.assertIn("JIOFIN (30 expected from RELIANCE, worth ₹6,750)", line)
        self.assertIn(f"If you still hold them: +₹10,270 → total {hr.inr(w['current_value'])}", line)
        md = hr.markdown(REPORT)
        self.assertIn("## Demerged shares not in your list", md)
        self.assertIn("| RELIANCE | Jio Financial Services Limited (JIOFIN) | 30 | ₹225.00 (live 09:58) | ₹6,750 | ₹3,370 |", md)

    def test_sell_all_scenarios_match_hand_arithmetic(self):
        h = REPORT["tax"]["hypothetical_sell_all"]
        reliance = [30 * (3000 - 2400), 30 * 3000]   # original lot and nil-cost bonus lot, both long-term
        itc, sbin, infy = 200 * (500 - 380), 40 * (1110 - 600), 15 * (1120 - 1500)
        self.assertEqual(h["estimated_total_tax"], hr.tax([money(sbin)], [money(g) for g in reliance + [itc, infy]], RATES)["total_tax"])
        self.assertEqual(h["estimated_total_tax_low"], hr.tax([money(sbin), money(infy)], [money(g) for g in reliance + [itc]], RATES)["total_tax"])
        # Published cost splits: RELIANCE keeps 95.32%, ITC 86.49% of the original cost.
        rel_split = money(30) * (3000 - money(2400) * money("0.9532"))
        itc_split = money(200) * (500 - money(380) * money("0.8649"))
        split = h["with_demerger_cost_split"]
        self.assertTrue(split["changes_figure"])
        self.assertEqual(split["estimated_total_tax"], hr.tax([money(sbin)], [rel_split, money(90000), itc_split, money(infy)], RATES)["total_tax"])
        jio = money(30) * (225 - money("112.32"))
        hotels = money(20) * (176 - money(380) * money("0.1351") / money("0.1"))
        dm = h["with_demerged_shares"]
        self.assertEqual(sorted(dm["symbols"]), ["ITCHOTELS", "JIOFIN"])
        self.assertEqual(dm["estimated_total_tax"],
                         hr.tax([money(sbin)], [rel_split, money(90000), itc_split, jio, hotels, money(infy)], RATES)["total_tax"])
        brief = hr.summary(REPORT)
        # The split basis (the cost the law uses) leads; the typed-cost figure follows, labelled.
        self.assertIn(f"estimated tax year 2026-27 tax {hr.tax_range(split['estimated_total_tax_low'], split['estimated_total_tax'])} (a range", brief)
        self.assertIn(f"  - Uses the published demerger cost split for ITC and RELIANCE, the cost the law uses (assumes your typed prices are "
                      f"what you paid before the demerger); on your full typed cost, which overstates the ITC and RELIANCE cost: "
                      f"{hr.tax_range(h['estimated_total_tax_low'], h['estimated_total_tax'])}", brief)
        self.assertIn("; selling JIOFIN and ITCHOTELS too: tax ", brief)

    def test_without_unlisted_demerged_shares_nothing_is_added(self):
        r = run(QUESTION + [{"symbol": "JIOFIN", "quantity": 30, "average_price": 0}, {"symbol": "ITCHOTELS", "quantity": 20, "average_price": 0}])
        self.assertEqual(r["demerged_shares"], [])
        self.assertIsNone(r["with_demerged_shares"])
        self.assertNotIn("with_demerged_shares", r["tax"]["hypothetical_sell_all"])


class TaxLawTests(unittest.TestCase):
    def test_tax_year_2026_27_uses_the_income_tax_act_2025(self):
        x = REPORT["tax"]
        self.assertEqual((x["year_label"], x["law"]), ("tax year 2026-27", "Income-tax Act 2025"))
        for needle in ("s.196", "s.198", "formerly s.111A", "formerly s.112A", "₹1,25,000 a tax year", "Finance Act 2026"):
            self.assertIn(needle, x["rates"]["basis"])
        md = hr.markdown(REPORT)
        self.assertIn("## Capital-gains tax estimate — tax year 2026-27", md)
        self.assertIn("LTCG exemption remaining this tax year", md)
        self.assertIn("estimated tax year 2026-27 tax", hr.summary(REPORT))

    def test_earlier_years_keep_the_1961_act(self):
        x = run(fy="2025-26")["tax"]
        self.assertEqual((x["year_label"], x["law"]), ("FY 2025-26", "Income-tax Act 1961"))
        self.assertIn("s.111A", x["rates"]["basis"])
        self.assertNotIn("s.196", x["rates"]["basis"])
        self.assertIsNone(x["hypothetical_sell_all"])
        self.assertEqual(hr.year_label(date(2026, 4, 1)), "tax year 2026-27")
        self.assertEqual(hr.year_label(date(2025, 4, 1)), "FY 2025-26")


class MidpointTests(unittest.TestCase):
    def test_typed_prices_that_drive_the_tax_get_a_midpoint_alternative(self):
        mid = REPORT["tax"]["hypothetical_sell_all"]["at_range_midpoint"]
        prices = {m["symbol"]: m for m in mid["prices"]}
        self.assertEqual(set(prices), {"SBIN", "ITC"})
        self.assertEqual((prices["SBIN"]["typed_price"], prices["SBIN"]["midpoint"]), (600.0, 960.65))   # Oct 2026: 937.50-983.80
        self.assertEqual(prices["ITC"]["midpoint"], 420.0)
        self.assertTrue(prices["ITC"]["range_understated"])   # before the ITC Hotels demerger Yahoo scales prices down
        sbin, infy = 40 * (money(1110) - money("960.65")), money(15 * (1120 - 1500))
        self.assertEqual(mid["estimated_total_tax"],
                         hr.tax([sbin], [money(18000), money(90000), money(200 * (500 - 420)), infy], RATES)["total_tax"])
        brief = hr.summary(REPORT)
        self.assertIn("  - ⚠ ITC and SBIN typed prices are outside what traded then; at ITC ~₹420 in Jun 2023 (Yahoo understates it "
                      "before a demerger) and SBIN ~₹961 in Oct 2026: P&L ", brief)
        self.assertIn("  - ⚠ At ITC ~₹420 in Jun 2023 (Yahoo understates it before a demerger) and SBIN ~₹961 in Oct 2026: tax ", brief)
        self.assertNotIn("at the middle of that range", brief)
        self.assertIn("SBIN at ₹960.65 (middle of the range traded in Oct 2026) instead of ₹600.00", hr.markdown(REPORT))

    def test_a_doubtful_price_that_does_not_move_the_figure_says_so(self):
        # Only ITC's price is doubtful and the long-term total stays inside the exemption either way.
        r = run([{"symbol": "ITC", "quantity": 10, "average_price": 380, "bought": "2023-06"}])
        h = r["tax"]["hypothetical_sell_all"]
        self.assertNotIn("at_range_midpoint", h)
        self.assertTrue(hr.summary(r).split("\n")[3].endswith(" · tax figure unchanged"))   # round 5: the P&L still moves


class ReturnsTests(unittest.TestCase):
    def test_xirr_and_benchmark_on_the_same_dates(self):
        ret = REPORT["returns"]
        by = {x["symbol"]: x for x in ret["holdings"]}
        days = (TODAY - date(2023, 1, 1)).days
        self.assertEqual(by["RELIANCE"]["days"], days)
        self.assertAlmostEqual(by["RELIANCE"]["return"], 180000 / 72000 - 1, places=5)
        self.assertAlmostEqual(by["RELIANCE"]["annualised"], (180000 / 72000) ** (365 / days) - 1, places=4)
        self.assertAlmostEqual(by["RELIANCE"]["benchmark_return"], 25000 / 18000 - 1, places=5)   # first close on/after 1 Jan 2023
        self.assertAlmostEqual(by["RELIANCE"]["benchmark_annualised"], (25000 / 18000) ** (365 / days) - 1, places=4)
        self.assertIsNone(by["SBIN"]["annualised"])   # 48 days: not annualised
        self.assertEqual(by["SBIN"]["days"], 48)
        self.assertEqual(ret["excluded"], ["INFY"])
        p = ret["portfolio"]
        flows = [(date(2023, 1, 1), -72000), (date(2023, 6, 1), -76000), (date(2026, 10, 1), -24000), (TODAY, 180000 + 100000 + 44400)]
        npv = sum(a / (1 + p["annualised"]) ** ((d - date(2023, 1, 1)).days / 365) for d, a in flows)
        self.assertLess(abs(npv), 5)
        self.assertGreater(by["RELIANCE"]["annualised_with_demerged"], by["RELIANCE"]["annualised"])
        brief = hr.summary(REPORT)
        self.assertIn(f"- **Return per year** {hr.pct(p['annualised'])} vs Nifty 50 {hr.pct(p['benchmark_annualised'])} on the same "
                      "dates (XIRR on RELIANCE, ITC and SBIN", brief)
        self.assertIn("INFY has no purchase date", brief)
        self.assertIn("## Returns vs Nifty 50", hr.markdown(REPORT))

    def test_benchmark_failure_is_reported_not_fatal(self):
        r = run(feed=Feed(fail={"^NSEI"}))
        self.assertIn("benchmark", r["errors"])
        self.assertIsNotNone(r["returns"]["portfolio"]["annualised"])
        self.assertIn("Nifty 50 comparison unavailable", hr.summary(r))

    def test_xirr_and_lookup_helpers(self):
        self.assertAlmostEqual(hr.xirr([(date(2024, 1, 1), -100), (date(2024, 12, 31), 110)]), 0.10, places=4)
        self.assertIsNone(hr.xirr([(date(2024, 1, 1), -100)]))
        bars = [{"date": date(2024, 1, 2), "close": 10.0}, {"date": date(2024, 1, 3), "close": 11.0}]
        self.assertEqual(hr.close_on_or_after(bars, date(2024, 1, 1)), (Decimal("10.0"), date(2024, 1, 2)))
        self.assertIsNone(hr.close_on_or_after(bars, date(2024, 1, 4)))
        self.assertIsNone(hr.close_on_or_after(bars, date(2023, 1, 1)))


class SaleCostAndWordingTests(unittest.TestCase):
    def test_sale_costs_use_delivery_stt_and_dp(self):
        c = REPORT["tax"]["hypothetical_sell_all"]["sale_costs"]
        value = 180000 + 100000 + 44400 + 16800
        self.assertAlmostEqual(c["stt"], value * DELIVERY["stt_sell"], places=2)
        self.assertAlmostEqual(c["dp"], 4 * DELIVERY["dp_per_sell"], places=2)
        self.assertGreater(c["total"], c["stt"] + c["dp"])
        h = REPORT["tax"]["hypothetical_sell_all"]
        self.assertAlmostEqual(h["cash_after_tax_and_costs"], value - c["total"] - h["estimated_total_tax"], places=1)
        self.assertIn(f"plus sale costs ≈ {hr.inr(c['total'])} (STT {hr.inr(c['stt'])}, DP {hr.inr(c['dp'])}", hr.summary(REPORT))

    def test_summary_wording(self):
        brief = hr.summary(REPORT)
        self.assertNotIn("top 5", brief)                # four holdings
        self.assertNotIn("Herfindahl", brief)
        self.assertIn(f"like {REPORT['concentration']['effective_holdings']:.1f} equal-sized holdings", brief)
        self.assertIn("(a range because INFY has no purchase date)", brief)
        self.assertNotIn("INFY have", brief)
        md = hr.markdown(REPORT)
        self.assertNotIn("Top 5 holdings", md)
        self.assertIn("| Concentration | highly concentrated — like", md)
        self.assertIn("The range is because the purchase date of INFY is unknown", md)
        self.assertEqual(hr.join_names(["A", "B", "C"]), "A, B and C")


if __name__ == "__main__":
    unittest.main()
