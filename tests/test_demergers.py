"""Offline tests for demerger flags (assets/demergers.json), --apply-demergers and --summary in the holdings review."""
from contextlib import redirect_stdout
from datetime import date, datetime, timedelta
from decimal import Decimal
import io
import json
from pathlib import Path
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT / "tests"))
from core import IST, Block, instant
import holdings_review as hr
from test_holdings_review import NIFTY500, holdings_doc

NOW = instant("2026-11-18T00:20:00+05:30")


def chart(symbol, closes, last=date(2026, 11, 17), split=None):
    days, day = [], datetime.combine(last, datetime.min.time(), IST).replace(hour=9, minute=15)
    while len(days) < len(closes):
        if day.weekday() < 5:
            days.append(day)
        day -= timedelta(days=1)
    days.reverse()
    result = {"meta": {"symbol": symbol, "currency": "INR", "exchangeTimezoneName": "Asia/Kolkata", "dataGranularity": "1d"},
              "timestamp": [int(d.timestamp()) for d in days],
              "indicators": {"quote": [{"open": closes, "high": closes, "low": closes, "close": closes, "volume": [1000] * len(closes)}]}}
    if split:
        stamp = int(datetime.combine(split[0], datetime.min.time(), IST).replace(hour=9, minute=15).timestamp())
        result["events"] = {"splits": {str(stamp): {"date": stamp, "numerator": split[1], "denominator": split[2]}}}
    return json.dumps({"chart": {"error": None, "result": [result]}}).encode()


class Feed:
    """Two closes per symbol (previous, last); `stale` symbols end in October; `fail` symbols raise; HDFCBANK carries a 1:1 bonus/split."""
    def __init__(self, prices=None, stale=(), fail=()):
        self.prices = {"ITC": (405.0, 410.0), "RELIANCE": (1490.0, 1500.0), "TCS": (3000.0, 3100.0), "HDFCBANK": (800.0, 815.0),
                       "ITCHOTELS": (200.0, 210.0), **(prices or {})}
        self.stale, self.fail = set(stale), set(fail)

    def __call__(self, url):
        if "ind_nifty500list.csv" in url:
            return NIFTY500
        sym = url.split("/chart/")[1].split("?")[0].replace(".NS", "")
        if sym in self.fail:
            raise Block("PUBLIC: source unavailable")
        prev, last = self.prices.get(sym, (100.0, 100.0))
        split = (date(2025, 8, 26), 2, 1) if sym == "HDFCBANK" else None
        return chart(sym + ".NS", [prev, prev, last], last=date(2026, 10, 1) if sym in self.stale else date(2026, 11, 17), split=split)


def run(doc, feed=None, **kw):
    return hr.review(doc, None, fetcher=feed or Feed(), clock=lambda: NOW, sleep=lambda _: None, **kw)


def row(report, symbol):
    return next(h for h in report["holdings"] if h["symbol"] == symbol)


def entry(parent, resulting, record, new=1, per=1, split=(50, 50)):
    return {"parent": parent, "symbols": [parent], "parent_name": parent + " Ltd", "resulting_company": resulting + " Ltd",
            "resulting_symbol": resulting, "record_date": record, "new": new, "per": per,
            "parent_pct": None if split is None else Decimal(str(split[0])), "resulting_pct": None if split is None else Decimal(str(split[1])),
            "sources": ["https://example.invalid/x.pdf"], "checked": date(2026, 11, 18)}


ITC_DATED = [{"symbol": "ITC", "quantity": 1000, "average_price": 400, "bought": "2024-06-01"}]


class ReferenceFileTests(unittest.TestCase):
    def test_every_entry_loads_with_a_source_and_a_checked_date(self):
        raw = json.loads((ROOT / "assets" / "demergers.json").read_text(encoding="utf-8"))
        self.assertEqual(raw["schema"], "demergers_v1")
        for e in raw["demergers"]:
            self.assertTrue(e["sources"] and all(s["url"].startswith("https://") for s in e["sources"]), e["parent"])
            date.fromisoformat(e["checked"])
            date.fromisoformat(e["record_date"])
            self.assertGreaterEqual(date.fromisoformat(e["record_date"]), date(2023, 1, 1))
        loaded = hr.load_demergers()
        self.assertEqual(len(loaded), len(raw["demergers"]))
        self.assertEqual(len({(e["parent"], e["record_date"]) for e in loaded}), len(loaded))
        for e in loaded:
            if e["parent_pct"] is not None:
                self.assertEqual(e["parent_pct"] + e["resulting_pct"], 100)

    def test_itc_entry_matches_the_company_letter(self):
        itc = next(e for e in hr.load_demergers() if e["parent"] == "ITC")
        self.assertEqual((itc["resulting_symbol"], itc["record_date"], itc["new"], itc["per"]), ("ITCHOTELS", date(2025, 1, 6), 1, 10))
        self.assertEqual((itc["parent_pct"], itc["resulting_pct"]), (Decimal("86.49"), Decimal("13.51")))

    def test_hul_and_vedanta_entries_match_the_company_letters(self):
        loaded = {e["parent"]: e for e in hr.load_demergers()}
        hul, vedl = loaded["HINDUNILVR"], loaded["VEDL"]
        self.assertEqual((hul["resulting_symbol"], hul["record_date"], hul["new"], hul["per"]), ("KWIL", date(2025, 12, 5), 1, 1))
        self.assertEqual((hul["parent_pct"], hul["resulting_pct"]), (Decimal("98.09"), Decimal("1.91")))
        self.assertEqual((vedl["record_date"], vedl["parent_pct"], vedl["resulting_pct"]), (date(2026, 5, 1), Decimal("52.34"), Decimal("47.66")))
        raw = {e["parent"]: e for e in json.loads((ROOT / "assets" / "demergers.json").read_text(encoding="utf-8"))["demergers"]}
        parts = raw["VEDL"]["resulting_companies"]
        self.assertEqual([c["symbol"] for c in parts], ["VAML", "VEDPOWER", "VOGL", "VISL"])
        self.assertAlmostEqual(sum(c["cost_split_pct"] for c in parts), 47.66)
        self.assertEqual(raw["VEDL"]["ex_date"], "2026-04-30")  # 1 May 2026 was a market holiday
        self.assertEqual(raw["TMPV"]["appointed_date"], "2025-07-01")
        for e in raw.values():  # the optional dates, when present, are ISO dates
            for key in ("ex_date", "appointed_date", "effective_date", "resulting_first_full_fy_end"):
                if e.get(key):
                    date.fromisoformat(e[key])

    def test_malformed_entries_are_refused(self):
        good = {"parent": "AAA", "resulting_company": "B", "resulting_symbol": "BBB", "record_date": "2025-01-01",
                "ratio": {"new_shares": 1, "per_parent_shares": 1}, "cost_split_pct": {"parent": 60, "resulting": 40},
                "sources": [{"url": "https://example.invalid/a.pdf"}], "checked": "2026-11-18"}
        bad = [{**good, "sources": []}, {**good, "checked": None}, {**good, "cost_split_pct": {"parent": 60, "resulting": 30}},
               {**good, "ratio": {"new_shares": 0, "per_parent_shares": 1}}, {**good, "sources": [{"url": "http://insecure"}]}]
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "d.json"
            path.write_text(json.dumps({"schema": "demergers_v1", "demergers": [good]}), encoding="utf-8")
            self.assertEqual(len(hr.load_demergers(path)), 1)
            for b in bad:
                path.write_text(json.dumps({"schema": "demergers_v1", "demergers": [b]}), encoding="utf-8")
                with self.assertRaises(Block):
                    hr.load_demergers(path)

    def test_unreadable_list_is_reported_not_fatal(self):
        original = hr.DEMERGERS_PATH
        hr.load_demergers.__defaults__ = (Path("missing-demergers.json"),)
        try:
            r = run(holdings_doc(ITC_DATED))
        finally:
            hr.load_demergers.__defaults__ = (original,)
        self.assertIn("demergers", r["errors"])
        self.assertIsNone(row(r, "ITC")["demerger_warning"])


class WarningTests(unittest.TestCase):
    def test_holding_bought_before_the_record_date_is_flagged_with_the_numbers(self):
        r = run(holdings_doc(ITC_DATED))
        itc = row(r, "ITC")
        w = itc["demerger_warning"]
        self.assertIn("record date 2025-01-06", w)
        self.assertIn("1 ITCHOTELS share for every 10 ITC shares", w)
        self.assertIn("You held 1,000 ITC shares then, so you should also have received 100 ITCHOTELS shares.", w)
        # The company's own illustration: 1,000 shares at 400 -> 3,45,960 stays with ITC, 54,040 to the 100 ITC Hotels shares.
        for needle in ("86.49% ITC / 13.51% ITCHOTELS", "₹3,45,960 stays with ITC", "₹345.96 a share instead of ₹400.00",
                       "₹54,040 becomes the cost of the ITCHOTELS shares (₹540.40 each)", "not adjusted", "--apply-demergers",
                       "ITCHOTELS is not in this file"):
            self.assertIn(needle, w)
        d = itc["demergers"][0]
        self.assertEqual((d["held_at_record_date"], d["entitled_new_shares"], d["conditional"], d["applied"]), (1000.0, 100.0, False, False))
        self.assertEqual((d["parent_cost_per_share_after"], d["resulting_cost_per_share"]), (345.96, 540.4))
        self.assertEqual((itc["quantity"], itc["average_price"]), (1000.0, 400.0))  # flagged, not changed
        text = hr.markdown(r)
        self.assertIn("| ITC ⚠ |", text)
        self.assertIn("- **ITC**: Demerger:", text)

    def test_undated_holding_gets_a_conditional_warning(self):
        itc = row(run(holdings_doc([{"symbol": "ITC", "quantity": 250, "average_price": 400}])), "ITC")
        self.assertTrue(itc["demerger_warning"].startswith("Demerger: if you bought ITC before 2025-01-06"))
        self.assertIn("you should also hold 25 ITCHOTELS shares for these 250", itc["demerger_warning"])
        self.assertTrue(itc["demergers"][0]["conditional"])

    def test_holding_bought_on_or_after_the_record_date_gets_nothing(self):
        for bought in ("2025-01-06", "2025-02-01"):
            itc = row(run(holdings_doc([{"symbol": "ITC", "quantity": 1000, "average_price": 400, "bought": bought}])), "ITC")
            self.assertIsNone(itc["demerger_warning"], bought)
            self.assertEqual(itc["demergers"], [])
            self.assertNotIn("ITC ⚠", hr.markdown(run(holdings_doc([{"symbol": "ITC", "quantity": 1, "average_price": 400, "bought": bought}]))))

    def test_lots_count_only_shares_held_on_the_record_date(self):
        lots = [{"symbol": "ITC", "quantity": 1000, "price": 400, "date": "2024-06-01"},
                {"symbol": "ITC", "quantity": 400, "price": 420, "date": "2024-12-01", "side": "SELL"},
                {"symbol": "ITC", "quantity": 500, "price": 430, "date": "2025-03-01"}]
        itc = row(run(holdings_doc([{"symbol": "ITC", "quantity": 1100, "average_price": 415}], lots)), "ITC")
        self.assertIn("You held 600 ITC shares then, so you should also have received 60 ITCHOTELS shares", itc["demerger_warning"])
        self.assertIn("of the ₹2,40,000 paid", itc["demerger_warning"])

    def test_fractional_entitlement_and_new_shares_already_in_the_file(self):
        r = run(holdings_doc([{"symbol": "ITC", "quantity": 1005, "average_price": 400, "bought": "2024-06-01"},
                              {"symbol": "ITCHOTELS", "quantity": 100, "average_price": 0}]))
        w = row(r, "ITC")["demerger_warning"]
        self.assertIn("100 ITCHOTELS shares, plus cash for the fractional entitlement", w)
        self.assertIn("ITCHOTELS is also in this file (100 ITCHOTELS shares)", w)

    def test_unknown_cost_split_says_so_and_ratio_four_for_five(self):
        rel = row(run(holdings_doc([{"symbol": "RAYMOND", "quantity": 100, "average_price": 1500, "bought": "2024-01-15"}])), "RAYMOND")
        lifestyle, realty = rel["demergers"]
        self.assertEqual((lifestyle["resulting_symbol"], lifestyle["entitled_new_shares"], lifestyle["cost_split_known"]), ("RAYMONDLSL", 80.0, False))
        self.assertIn("4 RAYMONDLSL shares for every 5 RAYMOND shares", rel["demerger_warning"])
        self.assertIn("not in this skill's reference list", rel["demerger_warning"])
        self.assertEqual((realty["resulting_symbol"], realty["entitled_new_shares"], realty["cost_split_known"]), ("RAYMONDREL", 100.0, True))

    def test_later_share_change_is_called_out_and_apply_works_in_todays_units(self):
        doc = holdings_doc([{"symbol": "HDFCBANK", "quantity": 25, "average_price": 1600, "bought": "2024-03-01"}])
        demerger = [entry("HDFCBANK", "NEWCO", date(2025, 1, 1), split=(80, 20))]
        plain = row(run(doc, demergers=demerger), "HDFCBANK")
        self.assertIn("You held 25 HDFCBANK shares then", plain["demerger_warning"])
        self.assertIn("₹1,280.00 a share instead of ₹1,600.00", plain["demerger_warning"])
        self.assertIn("later share change on 2025-08-26", plain["demerger_warning"])
        applied = row(run(doc, demergers=demerger, apply_demergers=True), "HDFCBANK")  # split 2:1 on 2025-08-26 after the demerger
        self.assertEqual(applied["quantity"], 50.0)
        self.assertAlmostEqual(applied["average_price"], 1600 * 0.8 / 2)

    def test_old_tata_motors_symbol_is_recognised(self):
        w = row(run(holdings_doc([{"symbol": "TATAMOTORS", "quantity": 10, "average_price": 700, "bought": "2025-01"}])), "TATAMOTORS")["demerger_warning"]
        self.assertIn("10 TMCV shares", w)
        self.assertIn("68.85% TATAMOTORS / 31.15% TMCV", w)

    def test_month_only_purchase_in_the_record_month_is_caveated(self):
        w = row(run(holdings_doc([{"symbol": "ITC", "quantity": 10, "average_price": 400, "bought": "2025-01"}])), "ITC")["demerger_warning"]
        self.assertIn("month-only", w)
        self.assertIn("on or after 2025-01-06 this does not apply", w)


class ApplyTests(unittest.TestCase):
    def test_apply_uses_the_published_split_for_dated_holdings(self):
        r = run(holdings_doc(ITC_DATED), apply_demergers=True)
        itc = row(r, "ITC")
        self.assertAlmostEqual(itc["average_price"], 345.96)
        self.assertAlmostEqual(itc["invested"], 345960)
        self.assertAlmostEqual(itc["pnl"], 1000 * 410 - 345960)
        self.assertEqual(itc["quantity"], 1000.0)
        self.assertTrue(itc["demergers"][0]["applied"])
        self.assertIn("already use this split", itc["demerger_warning"])
        self.assertTrue(any("average cost ₹400.00 became ₹345.96" in n for n in r["notes"]))
        self.assertEqual([l["buy_price"] for l in r["tax"]["open_lots"]], [345.96])
        self.assertTrue(r["demerger_reference"]["applied"])
        plain = run(holdings_doc(ITC_DATED))
        self.assertEqual(row(plain, "ITC")["average_price"], 400.0)
        self.assertEqual([l["buy_price"] for l in plain["tax"]["open_lots"]], [400.0])

    def test_apply_leaves_undated_unknown_split_and_mismatched_holdings_alone(self):
        undated = run(holdings_doc([{"symbol": "ITC", "quantity": 1000, "average_price": 400}]), apply_demergers=True)
        self.assertEqual(row(undated, "ITC")["average_price"], 400.0)
        unknown = run(holdings_doc([{"symbol": "AAA", "quantity": 10, "average_price": 100, "bought": "2024-01-01"}]),
                      demergers=[entry("AAA", "BBB", date(2025, 1, 1), split=None)], apply_demergers=True)
        self.assertEqual(row(unknown, "AAA")["average_price"], 100.0)
        self.assertIn("not in this skill's reference list", row(unknown, "AAA")["demerger_warning"])
        lots = [{"symbol": "ITC", "quantity": 1000, "price": 400, "date": "2024-06-01"}]
        mismatch = run(holdings_doc([{"symbol": "ITC", "quantity": 1200, "average_price": 400}], lots), apply_demergers=True)
        self.assertEqual(row(mismatch, "ITC")["average_price"], 400.0)
        self.assertTrue(any("--apply-demergers not applied" in n for n in mismatch["notes"]))

    def test_chained_demergers_compound_and_the_second_uses_the_cost_after_the_first(self):
        chain = [entry("AAA", "BBB", date(2024, 1, 1), split=(50, 50)), entry("AAA", "CCC", date(2025, 1, 1), split=(80, 20))]
        doc = holdings_doc([{"symbol": "AAA", "quantity": 10, "average_price": 100, "bought": "2023-06-01"}])
        flagged = row(run(doc, demergers=chain), "AAA")
        self.assertIn("of the ₹500 paid for these shares, ₹400 stays with AAA", flagged["demerger_warning"])
        applied = row(run(doc, demergers=chain, apply_demergers=True), "AAA")
        self.assertAlmostEqual(applied["average_price"], 100 * 0.5 * 0.8)

    def test_realised_sale_across_the_record_date(self):
        lots = [{"symbol": "ITC", "quantity": 100, "price": 400, "date": "2024-06-01"},
                {"symbol": "ITC", "quantity": 100, "price": 450, "date": "2026-06-01", "side": "SELL"}]
        doc = holdings_doc([{"symbol": "TCS", "quantity": 1, "average_price": 3000}], lots)
        plain = run(doc, fy="2026-27")
        self.assertEqual(plain["tax"]["realised_matches"][0]["buy_price"], 400.0)
        self.assertTrue(any("realised gain uses the full original cost" in n and "86.49%" in n for n in plain["notes"]))
        applied = run(doc, fy="2026-27", apply_demergers=True)
        self.assertEqual(applied["tax"]["realised_matches"][0]["buy_price"], 345.96)
        self.assertFalse(any("realised gain uses the full original cost" in n for n in applied["notes"]))

    def test_fifo_demerger_event_scales_cost_only(self):
        lots = [hr.lot("ITC", "2024-06-01", "BUY", 10, 400), hr.lot("ITC", "2025-01-06", "BUY", 10, 420)]
        _, open_lots, _, _ = hr.fifo(lots, {"ITC": [{"date": date(2025, 1, 6), "kind": "demerger", "factor": Decimal("0.8649")}]})
        self.assertEqual([(l["quantity"], l["price"]) for l in open_lots], [(Decimal(10), Decimal("345.9600")), (Decimal(10), Decimal(420))])


class OrderAndDemergedSharesTests(unittest.TestCase):
    """Notes give the corporate events in date order; the summary says whether the demerged shares are held."""

    def reliance(self, **kw):
        from test_buy_price_check import Feed as BarFeed, flat
        feed = BarFeed({"RELIANCE": (flat(date(2022, 1, 3), date(2026, 11, 17), 1150.0, 1260.0), [(date(2024, 10, 28), 2, 1)])})
        doc = holdings_doc([{"symbol": "RELIANCE", "quantity": 30, "average_price": 2400, "bought": "2023-01"}])
        return run(doc, feed=feed, **kw)

    def test_demerger_then_bonus_note_is_in_date_order_with_correct_costs(self):
        r = self.reliance(apply_demergers=True)
        rel = row(r, "RELIANCE")
        self.assertEqual((rel["quantity"], rel["average_price"]), (60.0, 1143.84))
        note = next(n for n in r["notes"] if n.startswith("RELIANCE: 30 shares"))
        self.assertIn("30 shares at ₹2,400.00 (cost ₹72,000), adjusted in date order: JIOFIN demerger (record date 2023-07-20", note)
        self.assertIn("cost ₹72,000 became ₹68,630 (30 shares, ₹2,287.68 a share); then 1:1 bonus on 2024-10-28: 30 shares became 60 "
                      "(₹1,143.84 a share, the new shares at nil cost; total cost unchanged at ₹68,630)", note)
        self.assertIn("Result: 60 shares at ₹1,143.84 (cost ₹68,630).", note)
        self.assertLess(note.index("JIOFIN demerger"), note.index("1:1 bonus"))
        self.assertFalse(any("same total cost" in n for n in r["notes"]))  # the contradictory one-line note is gone
        self.assertFalse(any("average cost ₹1,200.00 became" in n for n in r["notes"]))
        plain = self.reliance()  # without --apply-demergers only the bonus changed the holding
        self.assertTrue(any("30 shares at ₹2,400.00 became 60 at ₹1,200.00 (same total cost)" in n for n in plain["notes"]))

    def test_summary_says_whether_demerged_shares_are_held(self):
        missing = hr.summary(run(holdings_doc(ITC_DATED)))
        self.assertIn("- **Demerged shares**: ITCHOTELS (100 expected from ITC, worth ₹21,000) is not in your holdings — check your demat account; "
                      "the value and P&L above exclude them. If you still hold them: +₹21,000 → total ₹4,31,000, P&L ₹31,000 (+7.8%)", missing)
        held = hr.summary(run(holdings_doc(ITC_DATED + [{"symbol": "ITCHOTELS", "quantity": 100, "average_price": 0}])))
        self.assertIn("- **Demerged shares**: ITCHOTELS (100 held, from ITC) is in your holdings", held)
        undated = hr.summary(run(holdings_doc([{"symbol": "ITC", "quantity": 250, "average_price": 400}])))
        self.assertIn("ITCHOTELS (25 expected if your ITC was bought before 2025-01-06, worth ₹5,250) is not in your holdings", undated)
        self.assertNotIn("Demerged shares", hr.summary(run(holdings_doc([{"symbol": "TCS", "quantity": 5, "average_price": 3400}]))))
        w = row(run(holdings_doc(ITC_DATED)), "ITC")["demerger_warning"]
        self.assertIn("ITCHOTELS is not in this file; check your demat account for these shares", w)


class SummaryTests(unittest.TestCase):
    HOLDINGS = [{"symbol": "ITC", "quantity": 1000, "average_price": 400, "bought": "2024-06"},
                {"symbol": "RELIANCE", "quantity": 20, "average_price": 2400, "bought": "2023-01"},
                {"symbol": "HDFCBANK", "quantity": 25, "average_price": 1650},
                {"symbol": "TCS", "quantity": 5, "average_price": 3400},
                {"symbol": "OLDCO", "quantity": 10, "average_price": 100},
                {"symbol": "GONE", "quantity": 10, "average_price": 100}]

    def report(self):
        return run(holdings_doc(self.HOLDINGS), feed=Feed(stale={"OLDCO"}, fail={"GONE"}))

    def test_summary_answers_first_in_about_ten_lines(self):
        r = self.report()
        text = hr.summary(r)
        lines = [l for l in text.splitlines() if l.strip()]
        self.assertLessEqual(len(lines), 12)
        t = r["totals"]
        self.assertIn(f"- **Value** {hr.inr(t['current_value'])} on {hr.inr(t['invested_priced'])} invested", text)
        self.assertIn(f"P&L **{hr.inr(t['unrealised_pnl'])} ({hr.pct(t['unrealised_pnl_pct'])})** (5 of 6 holdings priced)", text)
        self.assertIn(f"- **Day change** {hr.inr(t['day_change'])}", text)
        ranked = sorted((h for h in r["holdings"] if h["pnl_pct"] is not None), key=lambda h: h["pnl_pct"])
        self.assertIn(f"**Best** {ranked[-1]['symbol']}", text)
        self.assertIn(f"**worst** {ranked[0]['symbol']}", text)
        self.assertIn(f"- **Largest holding** {r['concentration']['top_holding']}", text)
        self.assertIn(r["concentration"]["reading"], text)
        self.assertIn(f" · **top sector** {r['sectors'][0]['sector']}", text)   # round 6: on the largest-holding line
        self.assertIn("- **If you sold everything today**: estimated tax year 2026-27 tax", text)
        self.assertIn("HDFCBANK, TCS and OLDCO undated", text)
        # The cost splits leave the figure unchanged here (long-term gains stay inside the exemption), so no caveat is printed.
        self.assertFalse(r["tax"]["hypothetical_sell_all"]["with_demerger_cost_split"]["changes_figure"])
        self.assertNotIn("demerger cost splits", text)
        self.assertIn("top 5", text)  # six holdings
        self.assertNotIn("top 5", hr.summary(run(holdings_doc(self.HOLDINGS[:5]))))
        self.assertNotIn("Herfindahl", text)
        self.assertIn("like 1.4 equal-sized holdings", text)
        self.assertIn("⚠ 5 items to check**: bonus/split HDFCBANK; demerger ITC, RELIANCE; stale price OLDCO; missing price GONE", text)
        self.assertNotIn("Holdings |", text)  # not the full report

    def test_summary_without_prices_and_with_nothing_flagged(self):
        r = run(holdings_doc([{"symbol": "TCS", "quantity": 5, "average_price": 3400}]), prices=False)
        text = hr.summary(r)
        self.assertIn("no prices loaded", text)
        self.assertIn("Nothing flagged", text)
        self.assertIn("tax not estimated", text)

    def test_cli_summary_and_apply_flags(self):
        with tempfile.TemporaryDirectory() as tmp:
            src = Path(tmp) / "h.json"
            src.write_text(json.dumps({"schema": "holdings_v1", "holdings": ITC_DATED}), encoding="utf-8")
            out = io.StringIO()
            with redirect_stdout(out):
                self.assertEqual(hr.main(["--input", str(src), "--no-prices", "--summary"]), 0)
            self.assertIn("# Portfolio summary", out.getvalue())
            self.assertIn("demerger ITC", out.getvalue())
            js = Path(tmp) / "s.json"
            self.assertEqual(hr.main(["--input", str(src), "--no-prices", "--summary", "--format", "json", "--out", str(js)]), 0)
            data = json.loads(js.read_text(encoding="utf-8"))
            self.assertEqual(data["flags"], {"demerger": ["ITC"]})
            self.assertFalse(data["execution_eligible"])
            full = Path(tmp) / "f.json"
            self.assertEqual(hr.main(["--input", str(src), "--no-prices", "--apply-demergers", "--format", "json", "--out", str(full)]), 0)
            self.assertAlmostEqual(json.loads(full.read_text(encoding="utf-8"))["holdings"][0]["average_price"], 345.96)


if __name__ == "__main__":
    unittest.main()
