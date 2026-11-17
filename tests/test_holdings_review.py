"""Offline contracts for the personal holdings review; never contacts the network."""
from datetime import date, timedelta
import io
import json
from pathlib import Path
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
from core import Block, instant
import holdings_review as hr
from holdings_review import (OTHER_SECTOR, financial_year, fifo, inr, is_long_term, lot, main, markdown, parse_text,
                             review, tax)

NOW = instant("2026-11-17T16:00:00+05:30")
RATES = dict(hr.DEFAULT_RATES)
NIFTY500 = ("Company Name,Industry,Symbol,Series,ISIN Code\n"
            "Reliance Industries Ltd.,Oil Gas & Consumable Fuels,RELIANCE,EQ,INE002A01018\n"
            "Tata Consultancy Services Ltd.,Information Technology,TCS,EQ,INE467B01029\n"
            "Infosys Ltd.,Information Technology,INFY,EQ,INE009A01021\n").encode()


def chart_payload(selected, closes):
    days, day = [], NOW.replace(hour=9, minute=15, second=0, microsecond=0)
    while len(days) < len(closes):
        if day.weekday() < 5:
            days.append(day)
        day -= timedelta(days=1)
    days.reverse()
    return json.dumps({"chart": {"error": None, "result": [{
        "meta": {"symbol": selected, "currency": "INR", "exchangeTimezoneName": "Asia/Kolkata", "dataGranularity": "1d"},
        "timestamp": [int(d.timestamp()) for d in days],
        "indicators": {"quote": [{"open": closes, "high": closes, "low": closes, "close": closes, "volume": [1000] * len(closes)}]}}]}}).encode()


class Feed:
    def __init__(self, prices=None, fail=()):
        self.prices = prices or {"RELIANCE": (1400.0, 1500.0), "TCS": (3100.0, 3000.0), "INFY": (1500.0, 1500.0), "ZOMATO": (240.0, 250.0)}
        self.fail = set(fail)
        self.urls = []

    def __call__(self, url):
        self.urls.append(url)
        for key in self.fail:
            if key in url:
                raise Block("PUBLIC: source unavailable")
        if "ind_nifty500list.csv" in url:
            return NIFTY500
        for name, (previous, last) in self.prices.items():
            if f"/chart/{name}.NS?" in url:
                return chart_payload(name + ".NS", [previous - 10, previous, last])
        raise Block("PUBLIC: source unavailable")


def holdings_doc(holdings, lots=None):
    doc = {"schema": "holdings_v1", "as_of": "2026-11-17T09:00:00+05:30", "source": "test", "holdings": holdings}
    if lots is not None:
        doc["lots"] = lots
    return parse_text(json.dumps(doc))


def run(source, tradebook=None, feed=None, **kwargs):
    return review(source, tradebook, fetcher=feed or Feed(), clock=lambda: NOW, sleep=lambda _: None, **kwargs)


BASIC = [{"symbol": "RELIANCE", "quantity": 10, "average_price": 1200}, {"symbol": "TCS", "quantity": 5, "average_price": 3400},
         {"symbol": "ZOMATO", "quantity": 100, "average_price": 200}]
TRADEBOOK_HEAD = "symbol,isin,trade_date,exchange,segment,series,trade_type,auction,quantity,price,trade_id,order_id,order_execution_time\n"


def trade(sym, day, side, q, price, segment="EQ", at="10:00:00"):
    return f"{sym},INE000,{day},NSE,{segment},EQ,{side},false,{q},{price},1,2,{day}T{at}\n"


def L(sym, day, side, q, price):
    return lot(sym, day, side, q, price)


class ParsingTests(unittest.TestCase):
    def test_holdings_v1_parses_and_merges_duplicates(self):
        s = holdings_doc(BASIC + [{"symbol": "reliance.ns", "quantity": 10, "average_price": 1300}],
                         [{"symbol": "TCS", "quantity": 5, "price": 3400, "date": "2025-01-01", "side": "BUY"}])
        self.assertEqual(s["kind"], "holdings_v1")
        rel = next(h for h in s["holdings"] if h["symbol"] == "RELIANCE")
        self.assertEqual(rel["quantity"], 20)
        self.assertEqual(rel["average_price"], 1250)
        self.assertEqual(len(s["lots"]), 1)

    def test_zerodha_holdings_csv_with_bom_extra_columns_and_blank_rows(self):
        text = ("﻿Instrument,Qty.,Avg. cost,LTP,Cur. val,P&L,Net chg.,Day chg.\n"
                "RELIANCE,10,\"1,200.00\",1500,15000,3000,25%,1%\n,,,,,,,\nTCS,5,3400,3000,15000,-2000,-11%,0%\n")
        s = parse_text(text)
        self.assertEqual(s["kind"], "zerodha_holdings_csv")
        self.assertEqual([(h["symbol"], float(h["quantity"]), float(h["average_price"])) for h in s["holdings"]],
                         [("RELIANCE", 10.0, 1200.0), ("TCS", 5.0, 3400.0)])

    def test_tradebook_keeps_only_equity_segment(self):
        text = TRADEBOOK_HEAD + trade("INFY", "2025-01-02", "buy", 10, 1500) + "\n" + trade("NIFTY25JANFUT", "2025-01-02", "buy", 50, 23000, segment="FO")
        s = parse_text(text)
        self.assertEqual(s["kind"], "zerodha_tradebook_csv")
        self.assertEqual(len(s["lots"]), 1)
        self.assertIn("outside the equity segment", s["notes"][0])

    def test_unknown_headers_block_with_expected_columns(self):
        with self.assertRaisesRegex(Block, "Instrument, Qty., Avg. cost"):
            parse_text("Name,Shares,Cost\nRELIANCE,10,1200\n")
        with self.assertRaises(Block):
            parse_text('{"schema":"other","holdings":[]}')

    def test_negative_quantity_rejected(self):
        with self.assertRaisesRegex(Block, "negative"):
            holdings_doc([{"symbol": "TCS", "quantity": -5, "average_price": 3400}])
        with self.assertRaisesRegex(Block, "negative"):
            parse_text(TRADEBOOK_HEAD + trade("TCS", "2025-01-02", "buy", -1, 3000))


class ReviewTests(unittest.TestCase):
    def test_pnl_weights_and_day_change(self):
        r = run(holdings_doc(BASIC))
        t = r["totals"]
        self.assertEqual(t["invested"], 12000 + 17000 + 20000)
        self.assertEqual(t["current_value"], 15000 + 15000 + 25000)
        self.assertEqual(t["unrealised_pnl"], 6000)
        self.assertAlmostEqual(t["unrealised_pnl_pct"], 6000 / 49000, places=6)
        self.assertEqual(t["day_change"], 10 * 100 - 5 * 100 + 100 * 10)
        rows = {h["symbol"]: h for h in r["holdings"]}
        self.assertEqual(rows["TCS"]["pnl"], -2000)
        self.assertAlmostEqual(rows["ZOMATO"]["weight"], 25000 / 55000, places=6)
        self.assertEqual(rows["RELIANCE"]["price_date"], "2026-11-17")
        self.assertFalse(r["execution_eligible"])
        self.assertFalse(r["submitted"])

    def test_concentration_numbers(self):
        c = run(holdings_doc(BASIC))["concentration"]
        w = [25000 / 55000, 15000 / 55000, 15000 / 55000]
        self.assertEqual(c["top_holding"], "ZOMATO")
        self.assertAlmostEqual(c["top_holding_weight"], w[0], places=6)
        self.assertAlmostEqual(c["top5_weight"], 1.0, places=6)
        self.assertAlmostEqual(c["herfindahl"], sum(x * x for x in w), places=6)
        self.assertEqual(c["reading"], "highly concentrated")
        self.assertEqual(hr.hhi_reading(0.05), "diversified")
        self.assertEqual(hr.hhi_reading(0.12), "moderately concentrated")

    def test_sector_mapping_including_other(self):
        r = run(holdings_doc(BASIC))
        sectors = {s["sector"]: s for s in r["sectors"]}
        self.assertEqual(sectors["Information Technology"]["holdings"], ["TCS"])
        self.assertEqual(sectors[OTHER_SECTOR]["holdings"], ["ZOMATO"])
        self.assertIn("Oil Gas & Consumable Fuels", sectors)

    def test_price_failure_stays_visible(self):
        r = run(holdings_doc(BASIC), feed=Feed(fail=("/chart/TCS.NS",)))
        tcs = next(h for h in r["holdings"] if h["symbol"] == "TCS")
        self.assertIsNone(tcs["last_price"])
        self.assertIsNone(tcs["pnl"])
        self.assertEqual(tcs["price_status"], "price unavailable")
        self.assertIn("TCS", r["errors"])
        self.assertEqual(r["totals"]["priced_holdings"], 2)
        self.assertEqual(r["totals"]["current_value"], 40000)
        text = markdown(r)
        self.assertIn("price unavailable", text)
        self.assertIn("priced holdings only", text)

    def test_sector_failure_and_no_prices_offline(self):
        r = run(holdings_doc(BASIC), feed=Feed(fail=("nifty500",)))
        self.assertIsNone(r["sectors"])
        self.assertIn("sectors", r["errors"])
        feed = Feed()
        offline = run(holdings_doc(BASIC), feed=feed, prices=False)
        self.assertEqual(feed.urls, [])
        self.assertIsNone(offline["totals"]["current_value"])
        self.assertAlmostEqual(offline["concentration"]["top_holding_weight"], 20000 / 49000, places=6)
        self.assertIn("Portfolio review", markdown(offline))

    def test_tradebook_as_input_derives_holdings(self):
        text = TRADEBOOK_HEAD + trade("INFY", "2025-01-02", "buy", 10, 1400) + trade("INFY", "2025-02-02", "buy", 10, 1600) + trade("INFY", "2025-03-03", "sell", 5, 1700)
        r = run(parse_text(text))
        infy = r["holdings"][0]
        self.assertEqual(infy["quantity"], 15)
        self.assertAlmostEqual(infy["average_price"], (5 * 1400 + 10 * 1600) / 15, places=2)


class TaxTests(unittest.TestCase):
    def test_fifo_partial_sells_across_lots(self):
        matches, open_lots, unmatched, _ = fifo([
            L("INFY", "2025-01-01", "BUY", 10, 100), L("INFY", "2025-02-01", "BUY", 10, 120),
            L("INFY", "2025-03-01", "SELL", 15, 150), L("INFY", "2025-04-01", "SELL", 3, 160)])
        self.assertEqual([(m["buy_date"].isoformat(), int(m["quantity"]), float(m["gain"])) for m in matches],
                         [("2025-01-01", 10, 500.0), ("2025-02-01", 5, 150.0), ("2025-02-01", 3, 120.0)])
        self.assertEqual([(l["date"].isoformat(), int(l["quantity"])) for l in open_lots], [("2025-02-01", 2)])
        self.assertEqual(unmatched, [])

    def test_fifo_orders_by_date_not_file_order_and_flags_unmatched(self):
        matches, open_lots, unmatched, _ = fifo([
            L("TCS", "2025-03-01", "SELL", 4, 50), L("TCS", "2025-02-01", "BUY", 3, 40), L("TCS", "2025-01-01", "BUY", 3, 30)])
        self.assertEqual([m["buy_date"].isoformat() for m in matches], ["2025-01-01", "2025-02-01"])
        self.assertEqual(float(matches[0]["gain"]), 60.0)
        self.assertEqual([int(l["quantity"]) for l in open_lots], [2])
        _, _, unmatched, _ = fifo([L("TCS", "2025-03-01", "SELL", 4, 50)])
        self.assertEqual(int(unmatched[0]["quantity"]), 4)

    def test_twelve_month_boundary(self):
        bought = date(2025, 1, 10)
        self.assertFalse(is_long_term(bought, date(2026, 1, 10)))  # 365 days, exactly 12 months: short-term
        self.assertTrue(is_long_term(bought, date(2026, 1, 11)))   # 366 days, more than 12 months: long-term
        self.assertEqual((date(2026, 1, 10) - bought).days, 365)
        # Across a leap day, 366 days can be exactly 12 months, which is still short-term.
        self.assertFalse(is_long_term(date(2023, 6, 1), date(2024, 6, 1)))
        self.assertTrue(is_long_term(date(2023, 6, 1), date(2024, 6, 2)))
        self.assertTrue(is_long_term(date(2024, 2, 29), date(2025, 3, 1)))
        self.assertFalse(is_long_term(date(2024, 2, 29), date(2025, 2, 28)))
        m, _, _, _ = fifo([L("A", "2025-01-10", "BUY", 1, 100), L("A", "2026-01-10", "SELL", 1, 110)])
        self.assertEqual((m[0]["term"], m[0]["days_held"]), ("short", 365))

    def test_financial_year_boundaries(self):
        self.assertEqual(financial_year(None, date(2026, 3, 31))[0], "2025-26")
        self.assertEqual(financial_year(None, date(2026, 4, 1))[0], "2026-27")
        self.assertEqual(financial_year("2099-00")[1:], (date(2099, 4, 1), date(2100, 3, 31)))
        with self.assertRaises(Block):
            financial_year("2026-28")
        lots = [{"symbol": "INFY", "quantity": 2, "price": 100, "date": "2025-01-01", "side": "BUY"},
                {"symbol": "INFY", "quantity": 1, "price": 150, "date": "2026-03-31", "side": "SELL"},
                {"symbol": "INFY", "quantity": 1, "price": 170, "date": "2026-04-01", "side": "SELL"}]
        s = holdings_doc([{"symbol": "TCS", "quantity": 1, "average_price": 3000}], lots)
        old, new = run(s, fy="2025-26")["tax"], run(s, fy="2026-27")["tax"]
        self.assertEqual([m["sell_date"] for m in old["realised_matches"]], ["2026-03-31"])
        self.assertEqual([m["sell_date"] for m in new["realised_matches"]], ["2026-04-01"])
        self.assertEqual(old["realised"]["long_term_gains"], 50)
        self.assertEqual(new["realised"]["long_term_gains"], 70)

    def test_exemption_applies_only_to_long_term(self):
        x = tax([300000], [200000], RATES)
        self.assertEqual(x["taxable_short_term"], 300000)
        self.assertEqual(x["ltcg_exemption_used"], 125000)
        self.assertEqual(x["taxable_long_term"], 75000)
        self.assertAlmostEqual(x["stcg_tax"], 60000)
        self.assertAlmostEqual(x["ltcg_tax"], 9375)
        self.assertAlmostEqual(x["cess"], 0.04 * 69375)
        self.assertAlmostEqual(x["total_tax"], 69375 * 1.04)
        st_only = tax([300000], [], RATES)
        self.assertEqual(st_only["ltcg_exemption_used"], 0)
        self.assertEqual(st_only["ltcg_exemption_remaining"], 125000)
        self.assertAlmostEqual(st_only["stcg_tax"], 60000)

    def test_loss_set_off_rules(self):
        st_loss = tax([-50000], [200000], RATES)  # short-term loss reduces long-term gain
        self.assertEqual(st_loss["short_term_loss_set_off_against_long_term"], 50000)
        self.assertEqual(st_loss["long_term_after_set_off"], 150000)
        self.assertEqual(st_loss["taxable_long_term"], 25000)
        lt_loss = tax([100000], [-80000], RATES)   # long-term loss cannot reduce short-term gain
        self.assertEqual(lt_loss["taxable_short_term"], 100000)
        self.assertEqual(lt_loss["unused_long_term_loss"], 80000)
        mixed = tax([40000, -100000], [30000], RATES)
        self.assertEqual(mixed["taxable_short_term"], 0)
        self.assertEqual(mixed["long_term_after_set_off"], 0)
        self.assertEqual(mixed["unused_short_term_loss"], 30000)
        self.assertEqual(mixed["total_tax"], 0)

    def test_review_tax_section_with_tradebook(self):
        text = (TRADEBOOK_HEAD + trade("INFY", "2024-11-01", "buy", 10, 1000) + trade("INFY", "2026-05-01", "buy", 10, 1400)
                + trade("INFY", "2026-06-01", "sell", 12, 1600))
        source = holdings_doc([{"symbol": "INFY", "quantity": 8, "average_price": 1400}])
        x = run(source, parse_text(text), fy="2026-27")["tax"]
        self.assertEqual([(m["term"], m["quantity"], m["gain"]) for m in x["realised_matches"]], [("long", 10, 6000), ("short", 2, 400)])
        self.assertEqual(x["realised"]["taxable_long_term"], 0)
        self.assertEqual(x["ltcg_exemption_remaining"], 119000)
        self.assertEqual(x["unrealised"]["short"]["gain"], 8 * (1500 - 1400))
        self.assertEqual(x["open_lots"][0]["long_term_from"], "2027-05-02")
        self.assertIsNotNone(x["hypothetical_sell_all"])
        md = markdown(run(source, parse_text(text), fy="2026-27"))
        for needle in ("default rates — verify current rates", "Cess", "Not modelled", "Grandfathering", "check with a tax professional",
                       "LTCG exemption remaining this FY"):
            self.assertIn(needle.lower(), md.lower())
        for banned in ("you should sell", "we recommend", "harvest now"):
            self.assertNotIn(banned, md.lower())

    def test_indian_grouping(self):
        self.assertEqual(inr(125000), "₹1,25,000")
        self.assertEqual(inr(-12345678.5, 2), "-₹1,23,45,678.50")
        self.assertEqual(inr(999), "₹999")


class CliTests(unittest.TestCase):
    def test_out_refuses_overwrite_and_blocks_cleanly(self):
        with tempfile.TemporaryDirectory() as tmp:
            src = Path(tmp) / "h.json"
            src.write_text(json.dumps({"schema": "holdings_v1", "holdings": BASIC}), encoding="utf-8")
            out = Path(tmp) / "report.md"
            self.assertEqual(main(["--input", str(src), "--no-prices", "--out", str(out)]), 0)
            first = out.read_text(encoding="utf-8")
            err = io.StringIO()
            with redirect_stderr(err):
                self.assertEqual(main(["--input", str(src), "--no-prices", "--out", str(out)]), 2)
            self.assertIn("BLOCKED - output exists", err.getvalue())
            self.assertEqual(out.read_text(encoding="utf-8"), first)
            bad = Path(tmp) / "bad.csv"
            bad.write_text("a,b\n1,2\n", encoding="utf-8")
            err = io.StringIO()
            with redirect_stderr(err):
                self.assertEqual(main(["--input", str(bad), "--no-prices"]), 2)
            self.assertIn("BLOCKED - INPUT: unrecognised format", err.getvalue())
            js = Path(tmp) / "r.json"
            self.assertEqual(main(["--input", str(src), "--no-prices", "--format", "json", "--out", str(js)]), 0)
            data = json.loads(js.read_text(encoding="utf-8"))
            self.assertFalse(data["execution_eligible"])
            self.assertFalse(data["submitted"])


if __name__ == "__main__":
    unittest.main()
