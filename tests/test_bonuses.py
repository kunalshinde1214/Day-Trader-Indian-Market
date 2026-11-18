"""Offline tests for the curated bonus list (assets/bonuses.json) and automatic bonus-vs-split treatment."""
from contextlib import redirect_stderr
from datetime import date
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
from core import Block, instant
import holdings_review as hr
from test_buy_price_check import Feed, flat
from test_holdings_review import holdings_doc

NOW = instant("2026-11-18T10:00:00+05:30")
BONUSES = hr.load_bonuses()


def action(day, num, den):
    return {"date": day, "ratio": Decimal(str(num)) / Decimal(str(den)), "num": num, "den": den, "label": f"{num:g}-for-{den:g}"}


def reliance_feed(split_day=date(2024, 10, 28), ratio=(2, 1)):
    # Yahoo bars in today's units: about 1,200 in Jan 2023 (2,400 before the bonus).
    return Feed({"RELIANCE": (flat(date(2022, 1, 3), date(2026, 11, 17), 1150.0, 1260.0), [(split_day, *ratio)])})


def run(holdings, feed=None, **kw):
    return hr.review(holdings_doc(holdings), None, fetcher=feed or reliance_feed(), clock=lambda: NOW, sleep=lambda _: None,
                     demergers=[], **kw)


RELIANCE = [{"symbol": "RELIANCE", "quantity": 30, "average_price": 2400, "bought": "2023-01-15"}]


class ReferenceFileTests(unittest.TestCase):
    def test_every_entry_is_sourced_and_checked(self):
        raw = json.loads((ROOT / "assets" / "bonuses.json").read_text(encoding="utf-8"))
        self.assertEqual(raw["schema"], "bonuses_v1")
        self.assertEqual(len(BONUSES), len(raw["bonuses"]))
        self.assertEqual(len({(e["symbol"], e["record_date"]) for e in BONUSES}), len(BONUSES))
        for e in raw["bonuses"]:
            with self.subTest(symbol=e["symbol"]):
                self.assertTrue(e["sources"] and all(s["url"].startswith("https://") and s.get("covers") for s in e["sources"]))
                self.assertGreaterEqual(date.fromisoformat(e["record_date"]), date(2022, 1, 1))
                date.fromisoformat(e["checked"])
                if e.get("allotment_date") is None:
                    self.assertTrue(e.get("allotment_note"), "say why the allotment date is missing")

    def test_known_entries(self):
        by = {e["symbol"]: e for e in BONUSES}
        rel, hdfc, bajaj = by["RELIANCE"], by["HDFCBANK"], by["BAJFINANCE"]
        self.assertEqual((rel["new"], rel["held"], rel["record_date"], rel["allotment_date"]), (1, 1, date(2024, 10, 28), date(2024, 10, 29)))
        self.assertEqual((hdfc["record_date"], hdfc["ex_date"], hdfc["combined"]), (date(2025, 8, 27), date(2025, 8, 26), 2))
        self.assertEqual((bajaj["split_factor"], bajaj["bonus_factor"], bajaj["combined"]), (2, 5, 10))
        self.assertEqual(by["POWERGRID"]["bonus_factor"], Decimal(4) / Decimal(3))

    def test_malformed_entries_are_refused(self):
        good = {"symbol": "AAA", "ratio": {"new_shares": 1, "per_held_shares": 1}, "record_date": "2025-01-10",
                "allotment_date": "2025-01-11", "sources": [{"url": "https://example.invalid/a.pdf"}], "checked": "2026-11-18"}
        bad = [{**good, "sources": []}, {**good, "sources": [{"url": "http://x"}]}, {**good, "ratio": {"new_shares": 0, "per_held_shares": 1}},
               {**good, "allotment_date": "2025-01-01"}, {**good, "checked": None},
               {**good, "split_before_bonus": {"new_shares": 1, "per_old_shares": 1}}]
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "b.json"
            path.write_text(json.dumps({"schema": "bonuses_v1", "bonuses": [good]}), encoding="utf-8")
            self.assertEqual(len(hr.load_bonuses(path)), 1)
            for b in bad:
                with self.subTest(entry=b):
                    path.write_text(json.dumps({"schema": "bonuses_v1", "bonuses": [b]}), encoding="utf-8")
                    with self.assertRaises(Block):
                        hr.load_bonuses(path)


class MatchingTests(unittest.TestCase):
    def test_matching_event_becomes_a_bonus_dated_from_allotment(self):
        events, texts = hr.classify_actions("RELIANCE", [action(date(2024, 10, 28), 2, 1)], BONUSES)
        self.assertEqual([(e["kind"], e["ratio"], e["lot_date"]) for e in events], [("bonus", 2, date(2024, 10, 29))])
        self.assertIn("1:1 bonus (record date 2024-10-28)", texts[0])
        self.assertIn("matched to the listed bonus issue", texts[0])
        self.assertIn("allotment date 2024-10-29", texts[0])
        self.assertIn("--split RELIANCE", texts[0])

    def test_ex_date_before_record_date_matches(self):
        events, _ = hr.classify_actions("HDFCBANK", [action(date(2025, 8, 26), 2, 1)], BONUSES)
        self.assertEqual((events[0]["kind"], events[0]["lot_date"]), ("bonus", date(2025, 8, 28)))

    def test_wrong_ratio_symbol_or_date_stays_a_split(self):
        for symbol, act in (("RELIANCE", action(date(2024, 10, 28), 3, 1)), ("RELIANCE", action(date(2024, 11, 10), 2, 1)),
                            ("TCS", action(date(2024, 10, 28), 2, 1))):
            with self.subTest(symbol=symbol, act=act["date"]):
                events, texts = hr.classify_actions(symbol, [act], BONUSES)
                self.assertEqual(events[0]["kind"], "split")
                self.assertIn(f"use --bonus {symbol} if it was a bonus", texts[0])

    def test_record_date_without_allotment_is_used_with_a_note(self):
        events, texts = hr.classify_actions("POWERGRID", [action(date(2023, 9, 12), 4, 3)], BONUSES)
        self.assertEqual((events[0]["kind"], events[0]["lot_date"]), ("bonus", date(2023, 9, 12)))
        self.assertIn("allotment date not in the list", texts[0])

    def test_forcing_split_or_bonus(self):
        events, texts = hr.classify_actions("RELIANCE", [action(date(2024, 10, 28), 2, 1)], BONUSES, force_split=True)
        self.assertEqual(events[0]["kind"], "split")
        self.assertIn("--split", texts[0])
        events, texts = hr.classify_actions("TCS", [action(date(2024, 10, 28), 2, 1)], BONUSES, force_bonus=True)
        self.assertEqual((events[0]["kind"], events[0]["lot_date"]), ("bonus", date(2024, 10, 28)))
        self.assertIn("dated from that ex-date", texts[0])

    def test_split_plus_bonus_on_one_day(self):
        for yahoo in ((2, 1), (10, 1)):  # Yahoo has recorded only the split, or both together
            with self.subTest(yahoo=yahoo):
                events, texts = hr.classify_actions("BAJFINANCE", [action(date(2025, 6, 16), *yahoo)], BONUSES)
                self.assertEqual([(e["kind"], e["ratio"]) for e in events], [("split", 2), ("bonus", 5)])
                self.assertEqual(events[1]["lot_date"], date(2025, 6, 17))
                self.assertIn("2-for-1 split and then a 4:1 bonus", texts[0])
        lots = [hr.lot("BAJFINANCE", "2024-01-10", "BUY", 10, 7000)]
        events, _ = hr.classify_actions("BAJFINANCE", [action(date(2025, 6, 16), 2, 1)], BONUSES)
        _, open_lots, _, _ = hr.fifo(lots, {"BAJFINANCE": events})
        self.assertEqual([(l["quantity"], l["price"], l["date"]) for l in open_lots],
                         [(20, 3500, date(2024, 1, 10)), (80, 0, date(2025, 6, 17))])


class ReviewTests(unittest.TestCase):
    def test_listed_bonus_is_applied_automatically_and_said_so(self):
        r = run(RELIANCE)
        rel = r["holdings"][0]
        self.assertEqual((rel["quantity"], rel["average_price"], rel["invested"]), (60.0, 1200.0, 72000.0))
        self.assertIsNone(rel["corporate_action_warning"])
        self.assertTrue(any("RELIANCE: adjusted for 1:1 bonus (record date 2024-10-28)" in n and "matched to the listed bonus issue" in n
                            and "30 shares at ₹2,400.00 became 60 at ₹1,200.00 (same total cost)" in n for n in r["notes"]))
        lots = [(l["buy_date"], l["quantity"], l["buy_price"], l["long_term_from"]) for l in r["tax"]["open_lots"]]
        self.assertEqual(lots, [("2023-01-15", 30.0, 2400.0, "2024-01-16"), ("2024-10-29", 30.0, 0.0, "2025-10-30")])
        self.assertEqual(r["bonus_reference"]["entries"], len(BONUSES))
        self.assertIsNone(rel["price_warning"])  # 2,400 sits in Yahoo's Jan 2023 range once scaled back by the bonus

    def test_split_switch_overrides_the_list(self):
        r = run(RELIANCE, splits=["RELIANCE"])
        self.assertEqual([(l["quantity"], l["buy_price"]) for l in r["tax"]["open_lots"]], [(60.0, 1200.0)])
        self.assertEqual(r["bonus_reference"]["forced_split"], ["RELIANCE"])
        self.assertEqual(r["holdings"][0]["current_value"], run(RELIANCE)["holdings"][0]["current_value"])

    def test_undated_holding_warning_names_the_bonus(self):
        w = run([{"symbol": "RELIANCE", "quantity": 30, "average_price": 2400}])["holdings"][0]["corporate_action_warning"]
        self.assertIn("1:1 bonus (record date 2024-10-28)", w)
        self.assertIn("Give the purchase date", w)

    def test_unreadable_list_falls_back_to_splits_and_is_reported(self):
        original = hr.load_bonuses.__defaults__
        hr.load_bonuses.__defaults__ = (Path("missing-bonuses.json"),)
        try:
            r = run(RELIANCE)
        finally:
            hr.load_bonuses.__defaults__ = original
        self.assertIn("bonuses", r["errors"])
        self.assertEqual(len(r["tax"]["open_lots"]), 1)

    def test_cli_split_and_bonus_flags(self):
        with tempfile.TemporaryDirectory() as tmp:
            src = Path(tmp) / "h.json"
            src.write_text(json.dumps({"schema": "holdings_v1", "holdings": RELIANCE}), encoding="utf-8")
            err = io.StringIO()
            with redirect_stderr(err):
                self.assertEqual(hr.main(["--input", str(src), "--no-prices", "--bonus", "RELIANCE", "--split", "reliance"]), 2)
            self.assertIn("both --bonus and --split", err.getvalue())
            out = Path(tmp) / "r.json"
            self.assertEqual(hr.main(["--input", str(src), "--no-prices", "--split", "RELIANCE", "--format", "json", "--out", str(out)]), 0)
            self.assertEqual(json.loads(out.read_text(encoding="utf-8"))["bonus_reference"]["forced_split"], ["RELIANCE"])


if __name__ == "__main__":
    unittest.main()
