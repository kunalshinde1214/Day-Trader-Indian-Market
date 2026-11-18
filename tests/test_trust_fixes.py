"""Regression tests for the 1.2.1 trust fixes: missing latest close, bonuses and splits in holdings,
EPS cross-checks, tax ranges for undated holdings, and the bank equity warning."""
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
import screener
import stock_analysis as sa
from test_holdings_review import NIFTY500, holdings_doc, parse_text

NOW = instant("2026-11-18T00:20:00+05:30")


def sessions(count, last):
    days, day = [], datetime.combine(last, datetime.min.time(), IST).replace(hour=9, minute=15)
    while len(days) < count:
        if day.weekday() < 5:
            days.append(day)
        day -= timedelta(days=1)
    return list(reversed(days))


def chart(symbol, closes, *, last_close_missing=False, quote=None, split=None):
    days = sessions(len(closes), date(2026, 11, 17))
    close = list(closes)
    if last_close_missing:
        close[-1] = None
    meta = {"symbol": symbol, "currency": "INR", "exchangeTimezoneName": "Asia/Kolkata", "dataGranularity": "1d", "longName": f"{symbol} Ltd"}
    if quote:
        meta.update(quote)
    result = {"meta": meta, "timestamp": [int(d.timestamp()) for d in days],
              "indicators": {"quote": [{"open": closes, "high": [c * 1.01 for c in closes], "low": [c * 0.99 for c in closes],
                                         "close": close, "volume": [1000] * len(closes)}]}}
    if split:
        stamp = int(datetime.combine(split[0], datetime.min.time(), IST).replace(hour=9, minute=15).timestamp())
        result["events"] = {"splits": {str(stamp): {"date": stamp, "numerator": split[1], "denominator": split[2]}}}
    return json.dumps({"chart": {"error": None, "result": [result]}}).encode()


class LatestSessionTests(unittest.TestCase):
    def history(self, raw):
        return sa.daily_history("RELI.NS", fetcher=lambda url: raw, now=NOW)

    def test_missing_latest_close_is_rebuilt_from_the_quote(self):
        quote = {"regularMarketTime": int(datetime(2026, 11, 17, 15, 28, tzinfo=IST).timestamp()), "regularMarketPrice": 120.0,
                 "regularMarketDayHigh": 121.0, "regularMarketDayLow": 115.0, "regularMarketVolume": 5000}
        h = self.history(chart("RELI.NS", [100.0] * 80, last_close_missing=True, quote=quote))
        self.assertEqual(h["bars"][-1]["at"][:10], "2026-11-17")
        self.assertEqual(h["bars"][-1]["close"], 120.0)
        self.assertTrue(h["bars"][-1]["from_quote"])
        self.assertTrue(h["bars"][-1]["low"] <= h["bars"][-1]["open"] <= h["bars"][-1]["high"])
        self.assertIn("had no close", h["price_note"])
        self.assertEqual(h["price_date"], "2026-11-17")

    def test_missing_latest_close_without_quote_is_flagged(self):
        h = self.history(chart("RELI.NS", [100.0] * 80, last_close_missing=True))
        self.assertEqual(h["bars"][-1]["at"][:10], "2026-11-16")
        self.assertIn("2026-11-17", h["price_note"])
        self.assertIn("Check a live quote", h["price_note"])

    def test_complete_history_has_no_note(self):
        quote = {"regularMarketTime": int(datetime(2026, 11, 17, 15, 29, tzinfo=IST).timestamp()), "regularMarketPrice": 100.0}
        h = self.history(chart("RELI.NS", [100.0] * 80, quote=quote))
        self.assertIsNone(h["price_note"])
        self.assertFalse(h["bars"][-1].get("from_quote"))

    def test_report_header_shows_the_note_and_no_trading_claim_at_night(self):
        quote = {"regularMarketTime": int(datetime(2026, 11, 17, 15, 28, tzinfo=IST).timestamp()), "regularMarketPrice": 120.0}
        raw = chart("RELI.NS", [100.0] * 300, last_close_missing=True, quote=quote)
        fetch = lambda url: raw if "/chart/RELI" in url or "/chart/%5ENSEI" in url else b"<rss><channel/></rss>"
        r = sa.analyse("RELI", fetcher=fetch, news_fetcher=fetch, clock=lambda: NOW, peers=False)
        text = sa.markdown(r)
        self.assertIn("⚠ Yahoo's daily bar for 2026-11-17 had no close", text)
        self.assertNotIn("still trading", text)
        self.assertIn("price", r["errors"])


class EpsCheckTests(unittest.TestCase):
    Q = [{"date": d, "value": v} for d, v in (("2025-12-31", 46.12), ("2026-03-31", -1.71), ("2026-06-30", -8.91), ("2026-09-30", 11.05))]

    def test_wrong_trailing_eps_is_replaced_by_quarterly_sum(self):
        eps, flags = sa.check_eps({"date": "2026-09-30", "value": -3381.91, "basis": "trailing 12 months"}, self.Q, 2745.1)
        self.assertAlmostEqual(eps["value"], 46.55)
        self.assertEqual(eps["basis"], "sum of last four quarters")
        self.assertTrue(any("does not match" in f for f in flags))

    def test_one_unusual_quarter_is_flagged_with_a_typical_pe(self):
        q = [{"date": d, "value": v} for d, v in (("2025-12-31", 18.85), ("2026-03-31", 28.5), ("2026-06-30", -57.64), ("2026-09-30", 17.04))]
        eps, flags = sa.check_eps({"date": "2026-09-30", "value": 6.75, "basis": "trailing 12 months"}, q, 389.55)
        self.assertEqual(eps["value"], 6.75)  # accurate, so kept
        self.assertTrue(any("2026-06-30" in f and "would be about" in f for f in flags))

    def test_steady_quarters_raise_nothing(self):
        q = [{"date": d, "value": 15.0} for d in ("2025-12-31", "2026-03-31", "2026-06-30", "2026-09-30")]
        eps, flags = sa.check_eps({"date": "2026-09-30", "value": 60.0, "basis": "trailing 12 months"}, q, 1300)
        self.assertEqual(flags, [])

    def test_gaps_between_quarters_skip_the_check(self):
        q = [{"date": d, "value": 15.0} for d in ("2025-03-31", "2026-03-31", "2026-06-30", "2026-09-30")]
        eps, flags = sa.check_eps({"date": "2026-09-30", "value": -999.0, "basis": "trailing 12 months"}, q, 1300)
        self.assertEqual(eps["value"], -999.0)
        self.assertEqual(flags, [])

    def test_large_annual_eps_jump_is_flagged(self):
        series = {"annualDilutedEPS": [{"date": "2025-03-31", "value": 10.0}, {"date": "2026-03-31", "value": 63.7}],
                  "annualOrdinarySharesNumber": [{"date": "2026-03-31", "value": 1e9}]}
        f = sa.ratios(series, 700.0, NOW)
        self.assertTrue(any("+537%" in x for x in f["eps_flags"]))

    def test_screener_marks_flagged_rows(self):
        report = {"universe": "custom", "as_of": NOW.isoformat(), "filters": ["roe>10"], "requested": 2, "evaluated": 2, "matched": 2,
                  "truncated": False, "missing_data": [], "failed": [], "note": "n",
                  "results": [{"symbol": "AAA.NS", "name": "Aaa", "price_date": "2026-11-17", "eps_flags": ["odd quarter"]},
                              {"symbol": "BANK.NS", "name": "Bank", "price_date": "2026-11-17", "bank_like": True, "eps_flags": []}]}
        text = screener.markdown(report)
        self.assertIn("Aaa (AAA) ⚠", text)
        self.assertIn("- AAA: odd quarter", text)
        self.assertIn("Banks and finance companies (BANK)", text)
        self.assertIn("prices as of 2026-11-17", text)


class Feed:
    def __init__(self, split=None, price=815.0):
        self.split, self.price = split, price

    def __call__(self, url):
        if "ind_nifty500list.csv" in url:
            return NIFTY500
        if "/chart/HDFCBANK.NS?" in url:
            return chart("HDFCBANK.NS", [self.price] * 30, split=self.split)
        if "/chart/" in url:
            sym = url.split("/chart/")[1].split("?")[0]
            return chart(sym, [100.0] * 30)
        raise ValueError("unexpected url")


def run(doc, **kw):
    return hr.review(doc, None, fetcher=Feed(split=kw.pop("split", (date(2025, 8, 26), 2, 1))), clock=lambda: NOW, sleep=lambda _: None, **kw)


class CorporateActionTests(unittest.TestCase):
    def test_split_rescales_open_lots_and_keeps_the_purchase_date(self):
        lots = [hr.lot("HDFCBANK", "2024-03-15", "BUY", 25, 1650)]
        events = {"HDFCBANK": [{"date": date(2025, 8, 26), "ratio": Decimal(2), "kind": "split"}]}
        _, open_lots, _, _ = hr.fifo(lots, events)
        self.assertEqual([(l["quantity"], l["price"], l["date"]) for l in open_lots], [(Decimal(50), Decimal(825), date(2024, 3, 15))])

    def test_bonus_adds_a_nil_cost_lot_dated_on_the_ex_date(self):
        lots = [hr.lot("HDFCBANK", "2024-03-15", "BUY", 25, 1650), hr.lot("HDFCBANK", "2025-09-10", "SELL", 30, 900)]
        events = {"HDFCBANK": [{"date": date(2025, 8, 26), "ratio": Decimal(2), "kind": "bonus"}]}
        matches, open_lots, unmatched, _ = hr.fifo(lots, events)
        self.assertEqual(unmatched, [])
        self.assertEqual([(m["quantity"], m["buy_price"]) for m in matches], [(Decimal(25), Decimal(1650)), (Decimal(5), Decimal(0))])
        self.assertEqual(matches[1]["term"], "short")  # bonus shares held from the ex-date
        self.assertEqual([(l["quantity"], l["price"], l["date"]) for l in open_lots], [(Decimal(20), Decimal(0), date(2025, 8, 26))])

    def test_dated_holding_in_old_units_is_adjusted(self):
        doc = holdings_doc([{"symbol": "HDFCBANK", "quantity": 25, "average_price": 1650, "bought": "2024-03"}])
        r = run(doc)
        h = r["holdings"][0]
        self.assertEqual((h["quantity"], h["average_price"]), (50.0, 825.0))
        self.assertAlmostEqual(h["pnl"], 50 * 815 - 41250)
        self.assertTrue(any("became 50" in n for n in r["notes"]))
        self.assertTrue(any("purchase month only" in n for n in r["notes"]))

    def test_holding_bought_after_the_split_is_left_alone(self):
        doc = holdings_doc([{"symbol": "HDFCBANK", "quantity": 50, "average_price": 800, "bought": "2025-10-01"}])
        h = run(doc)["holdings"][0]
        self.assertEqual((h["quantity"], h["average_price"]), (50.0, 800.0))

    def test_undated_hand_entered_holding_gets_a_visible_warning(self):
        r = run(holdings_doc([{"symbol": "HDFCBANK", "quantity": 25, "average_price": 1650}]))
        self.assertIn("Give the purchase date", r["holdings"][0]["corporate_action_warning"])
        text = hr.markdown(r)
        self.assertIn("| HDFCBANK ⚠ |", text)
        self.assertIn("Check before trusting these numbers", text)

    def test_broker_export_warning_says_it_is_usually_adjusted(self):
        doc = parse_text("Instrument,Qty.,Avg. cost\nHDFCBANK,50,825\n")
        self.assertIn("normally already adjusted", run(doc)["holdings"][0]["corporate_action_warning"])

    def test_bonus_switch_changes_tax_lots_not_value(self):
        doc = holdings_doc([{"symbol": "HDFCBANK", "quantity": 25, "average_price": 1650, "bought": "2024-03-15"}])
        split, bonus = run(doc, splits=["HDFCBANK"]), run(doc, bonus=["HDFCBANK"])
        self.assertEqual(split["holdings"][0]["current_value"], bonus["holdings"][0]["current_value"])
        self.assertEqual(len(split["tax"]["open_lots"]), 1)
        self.assertEqual([l["buy_price"] for l in bonus["tax"]["open_lots"]], [1650.0, 0.0])


class TaxRangeTests(unittest.TestCase):
    def test_undated_holdings_give_a_best_to_worst_range(self):
        doc = holdings_doc([{"symbol": "GAIN", "quantity": 100, "average_price": 50},    # +5,000 at price 100
                            {"symbol": "LOSS", "quantity": 100, "average_price": 150}])  # -5,000
        r = hr.review(doc, None, fetcher=Feed(split=None), clock=lambda: NOW, sleep=lambda _: None)
        h = r["tax"]["hypothetical_sell_all"]
        self.assertEqual(h["estimated_total_tax_low"], 0.0)   # gain long-term within exemption, loss short-term
        self.assertAlmostEqual(h["estimated_total_tax"], 5000 * 0.20 * 1.04)  # gain short-term, loss long-term cannot offset it
        self.assertEqual(sorted(h["undated_symbols"]), ["GAIN", "LOSS"])
        self.assertIn("between ₹0 and ₹1,040", hr.markdown(r))

    def test_dated_holdings_give_one_figure(self):
        doc = holdings_doc([{"symbol": "GAIN", "quantity": 100, "average_price": 50, "bought": "2026-06-01"}])
        h = hr.review(doc, None, fetcher=Feed(split=None), clock=lambda: NOW, sleep=lambda _: None)["tax"]["hypothetical_sell_all"]
        self.assertEqual(h["estimated_total_tax_low"], h["estimated_total_tax"])
        self.assertAlmostEqual(h["estimated_total_tax"], 5000 * 0.20 * 1.04)


if __name__ == "__main__":
    unittest.main()
