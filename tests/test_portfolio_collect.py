import copy
from datetime import timedelta
import io
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
from core import Block, instant
from portfolio_collect import normalize, fetch, main, ROUTES
from trade_import import normalize_trades
from live_state import _product
from kite_readonly import KiteReadOnly

NOW = instant("2026-11-06T10:00:00+05:30")


def fixture():
    instrument = {"broker": "kite", "exchange": "NSE", "symbol": "DEMO", "token": "123", "kind": "equity", "lot_size": 1,
                  "tick_size": 0.05, "master_date": "2026-11-06", "expiry": None}
    ident = {"exchange": "NSE", "symbol": "DEMO", "token": "123", "product": "CNC"}
    header = {"broker": "kite", "account_id": "DEMO", "source": "kite", "date": "2026-11-06", "as_of": NOW.isoformat(), "complete": True}
    opening = {**header, "source": "synthetic_reviewed_statement", "schema": "portfolio_opening_v1", "previous_session": "2026-11-05",
               "adjustments_reviewed": True, "inventory_events": [], "positions": [{**ident, "quantity": 10, "average_price": 100,
               "previous_close": 110, "price_basis": "per_unit_inr", "cost_source": "synthetic_cost", "mark_source": "synthetic_close", "mark_session": "2026-11-05"}]}
    holding = {"exchange": "NSE", "tradingsymbol": "DEMO", "instrument_token": 123, "product": "CNC", "isin": "SYNTHETIC",
               "quantity": 10, "used_quantity": 0, "opening_quantity": 10, "t1_quantity": 0, "realised_quantity": 10,
               "short_quantity": 0, "collateral_quantity": 0, "discrepancy": False, "mtf": {"quantity": 0, "used_quantity": 0}}
    raw = {**header, "schema": "kite_portfolio_read_v1", "started_at": NOW.isoformat(), "holdings": [holding], "orders": [], "trades": [],
           "positions": {"net": [], "day": []}, "quotes": {"NSE:DEMO": {"instrument_token": 123, "timestamp": "2026-11-06 10:00:00",
           "last_trade_time": "2026-11-06 09:59:59", "last_price": 120}}}
    return raw, opening, [instrument]


class PortfolioCollectTests(unittest.TestCase):
    def setUp(self): self.raw, self.opening, self.master = fixture()

    def verify(self): self.raw["verification"] = copy.deepcopy({k: self.raw[k] for k in ROUTES})

    def run_case(self):
        self.verify()
        return normalize(self.raw, self.opening, self.master, NOW)

    def trade(self, side="SELL", q=4, price=115, product="CNC", oid="o1", at="2026-11-06 09:59:00"):
        i = self.master[0]
        ident = {"exchange": i["exchange"], "tradingsymbol": i["symbol"], "instrument_token": int(i["token"]), "product": product}
        self.raw["orders"].append({**ident, "order_id": oid, "order_timestamp": at, "quantity": q, "filled_quantity": q,
                                   "transaction_type": side, "status": "COMPLETE", "variety": "regular"})
        self.raw["trades"].append({**ident, "order_id": oid, "trade_id": "t" + oid, "quantity": q, "average_price": price,
                                   "transaction_type": side, "fill_timestamp": at})
        return ident

    def position(self, ident, buys=0, sells=0, overnight=0):
        row = {**ident, "quantity": overnight + buys - sells, "overnight_quantity": overnight,
               "multiplier": 1, "day_buy_quantity": buys, "day_sell_quantity": sells}
        self.raw["positions"] = {"net": [row], "day": [{**row, "quantity": buys - sells, "overnight_quantity": 0}]}

    def sale(self, q=4):
        ident = self.trade(q=q)
        self.position(ident, sells=q)
        self.raw["holdings"][0].update(quantity=10-q, realised_quantity=10-q, used_quantity=q)

    def test_unchanged_holdings_daily_and_acquisition_pnl(self):
        result = self.run_case()
        self.assertEqual(result["report"]["totals"]["daily_gross_inr"], 100)
        self.assertEqual(result["report"]["totals"]["unrealized_gross_inr"], 200)
        self.assertFalse(result["execution_eligible"])
        self.assertNotIn("reconciled", result["report"])

    def test_partial_sale_not_subtracted_twice(self):
        self.sale(); result = self.run_case()
        self.assertEqual(result["snapshot"]["positions"][0]["quantity"], 6)
        self.assertEqual(result["report"]["totals"]["daily_gross_inr"], 80)
        self.assertIsNone(result["report"]["totals"]["daily_net_inr"])
        self.assertEqual(result["report"]["status"], "PARTIAL")
        self.assertEqual(result["fills"]["fills"][0]["product"], "CNC")

    def test_complete_sale_needs_no_quote(self):
        self.sale(10); self.raw["quotes"] = {}
        self.assertEqual(self.run_case()["snapshot"]["positions"][0]["quantity"], 0)

    def test_buy_with_holdings_adds_once(self):
        ident = self.trade("BUY", 5, 120); self.position(ident, buys=5)
        self.assertEqual(self.run_case()["snapshot"]["positions"][0]["quantity"], 15)

    def test_new_delivery_purchase_without_holdings(self):
        self.opening["positions"] = []; self.raw["holdings"] = []
        ident = self.trade("BUY", 5, 120); self.position(ident, buys=5)
        self.assertEqual(self.run_case()["snapshot"]["positions"][0]["quantity"], 5)

    def test_sale_and_buyback_preserves_quantity(self):
        self.sale(); ident = self.trade("BUY", 4, 118, oid="o2", at="2026-11-06 09:59:01")
        self.position(ident, buys=4, sells=4)
        self.assertEqual(self.run_case()["snapshot"]["positions"][0]["quantity"], 10)

    def test_intraday_short_kept_separate_from_delivery(self):
        ident = self.trade("SELL", 4, 115, product="MIS"); self.position(ident, sells=4)
        rows = self.run_case()["snapshot"]["positions"]
        self.assertEqual({r["product"]: r["quantity"] for r in rows}, {"CNC": 10, "MIS": -4})

    def test_nrml_short_reduction(self):
        self.master[0].update(exchange="NFO", kind="future", expiry="2026-11-26", underlying_type="index", lot_size=5)
        self.opening["positions"][0].update(exchange="NFO", product="NRML", quantity=-10)
        self.raw["holdings"] = []
        self.raw["quotes"]["NFO:DEMO"] = self.raw["quotes"].pop("NSE:DEMO")
        ident = self.trade("BUY", 5, 115, product="NRML"); self.position(ident, buys=5, overnight=-10)
        result = self.run_case()
        self.assertEqual(result["snapshot"]["positions"][0]["quantity"], -5)
        self.assertEqual(result["report"]["totals"]["daily_gross_inr"], -75)

    def test_bad_holdings_fail_instead_of_guessing(self):
        baseline = copy.deepcopy(self.raw)
        for fields in ({"t1_quantity": 1}, {"short_quantity": 1}, {"collateral_quantity": 1}, {"discrepancy": True},
                       {"mtf": {"quantity": 1, "used_quantity": 0}}, {"quantity": 8}, {"realised_quantity": 8},
                       {"used_quantity": 1}, {"opening_quantity": 11}, {"product": "MTF"}):
            with self.subTest(fields=fields):
                self.raw = copy.deepcopy(baseline); self.raw["holdings"][0].update(fields)
                with self.assertRaises(Block): self.run_case()

    def test_duplicate_holding_and_cross_exchange_isin_block(self):
        self.raw["holdings"].append(copy.deepcopy(self.raw["holdings"][0]))
        with self.assertRaises(Block): self.run_case()

    def test_missing_opening_holding_blocks(self):
        self.raw["holdings"] = []
        with self.assertRaises(Block): self.run_case()

    def test_unreviewed_adjustment_blocks(self):
        self.opening["adjustments_reviewed"] = False
        with self.assertRaises(Block): self.run_case()

    def test_book_change_between_reads_blocks(self):
        self.verify(); self.raw["verification"]["holdings"][0]["quantity"] = 9
        with self.assertRaises(Block): normalize(self.raw, self.opening, self.master, NOW)

    def test_marks_changing_between_reads_do_not_change_inventory(self):
        self.verify(); self.raw["verification"]["holdings"][0]["last_price"] = 121
        self.assertTrue(normalize(self.raw, self.opening, self.master, NOW)["report"]["inventory_reconciled"])

    def test_order_trade_quantity_mismatch_blocks(self):
        self.sale(); self.raw["trades"][0]["quantity"] = 3
        with self.assertRaises(Block): self.run_case()

    def test_position_trade_activity_mismatch_blocks(self):
        self.sale(); self.raw["positions"]["net"][0]["day_sell_quantity"] = 3
        with self.assertRaises(Block): self.run_case()

    def test_missing_day_position_blocks(self):
        self.sale(); self.raw["positions"]["day"] = []
        with self.assertRaises(Block): self.run_case()

    def test_conversion_or_non_unit_multiplier_blocks(self):
        self.sale(); original = copy.deepcopy(self.raw)
        for fields in ({"overnight_quantity": 2}, {"multiplier": 5}, {"quantity": -3}, {"product": "MIS"}):
            self.raw = copy.deepcopy(original); self.raw["positions"]["net"][0].update(fields)
            with self.subTest(fields=fields), self.assertRaises(Block): self.run_case()

    def test_same_timestamp_fills_not_arbitrarily_ordered(self):
        self.sale(); ident = self.trade("BUY", 4, 118, oid="o2")
        self.position(ident, buys=4, sells=4)
        with self.assertRaises(Block): self.run_case()

    def test_quote_identity_time_and_missing_checks(self):
        baseline = copy.deepcopy(self.raw)
        for fields in ({"instrument_token": 124}, {"last_trade_time": "2026-11-06 09:58:00"},
                       {"timestamp": "2026-11-06 10:00:01"}, {"last_trade_time": "2026-11-05 10:00:00"}, {"last_price": 0}):
            self.raw = copy.deepcopy(baseline); self.raw["quotes"]["NSE:DEMO"].update(fields)
            with self.subTest(fields=fields), self.assertRaises(Block): self.run_case()
        self.raw = copy.deepcopy(baseline); self.raw["quotes"] = {}
        with self.assertRaises(KeyError): self.run_case()

    def test_wrong_account_and_old_collection_block(self):
        baseline = copy.deepcopy(self.raw)
        for fields in ({"account_id": "OTHER"}, {"started_at": "2026-11-06T09:58:00+05:30"}, {"complete": False}):
            self.raw = {**copy.deepcopy(baseline), **fields}
            with self.subTest(fields=fields), self.assertRaises(Block): self.run_case()

    def test_unknown_order_status_old_date_and_variety_block(self):
        self.sale(); baseline = copy.deepcopy(self.raw)
        for fields in ({"status": "UPDATE PENDING"}, {"variety": "co"}, {"order_timestamp": "2026-11-05 10:00:00"}):
            self.raw = copy.deepcopy(baseline); self.raw["orders"][0].update(fields)
            with self.subTest(fields=fields), self.assertRaises(Block): self.run_case()

    def test_default_trade_import_and_execution_still_reject_carry(self):
        self.sale(); result = self.run_case()
        with self.assertRaises(Block): normalize_trades(self.raw, result["snapshot"], self.master, NOW)
        with self.assertRaises(Block): _product("kite", {"product": "CNC"})

    def test_cross_account_orders_and_fills_block(self):
        self.sale(); baseline = copy.deepcopy(self.raw)
        for name in ("orders", "trades"):
            self.raw = copy.deepcopy(baseline); self.raw[name][0]["user_id"] = "OTHER"
            with self.subTest(name=name), self.assertRaises(Block): self.run_case()

    def test_empty_account_is_explicit_and_reconciled(self):
        self.raw["holdings"] = []; self.raw["quotes"] = {}; self.opening["positions"] = []
        self.assertEqual(self.run_case()["report"]["status"], "RECONCILED")

    def test_partial_order_imports_only_executed_units(self):
        self.sale(); self.raw["orders"][0].update(quantity=10, status="OPEN")
        result = self.run_case()
        self.assertEqual(result["snapshot"]["orders"][0]["status"], "PARTIAL")
        self.assertEqual(result["snapshot"]["positions"][0]["quantity"], 6)

    def test_duplicate_trade_deduplicates_without_quantity_inflation(self):
        self.sale(); self.raw["trades"].append(copy.deepcopy(self.raw["trades"][0]))
        self.assertEqual(len(self.run_case()["fills"]["fills"]), 1)

    def test_conflicting_duplicate_trade_blocks(self):
        self.sale(); self.raw["trades"].append({**self.raw["trades"][0], "average_price": 116})
        with self.assertRaises(Block): self.run_case()

    def client(self, accounts=("DEMO", "DEMO")):
        outer = self
        class Client:
            def __init__(self): self.calls = []; self.ids = iter(accounts)
            def account_id(self): return next(self.ids)
            def request(self, method, route):
                self.calls.append((method, route))
                return copy.deepcopy(outer.raw[next(k for k, v in ROUTES.items() if v == route)])
            def quotes(self, symbols):
                self.calls.append(("GET", "/quote")); return {"data": copy.deepcopy(outer.raw["quotes"])}
        return Client()

    def test_fetch_only_reads_and_rechecks_books(self):
        client = self.client(); result = fetch(client, "DEMO", self.opening, self.master, clock=lambda: NOW)
        self.assertTrue(result["report"]["inventory_reconciled"])
        self.assertEqual(len(client.calls), 9)
        self.assertTrue(all(method == "GET" for method, path in client.calls))

    def test_fetch_account_change_blocks(self):
        with self.assertRaises(Block): fetch(self.client(("DEMO", "OTHER")), "DEMO", self.opening, self.master, clock=lambda: NOW)

    def test_fetch_wrong_account_stops_before_books(self):
        client = self.client(("OTHER",))
        with self.assertRaises(Block): fetch(client, "DEMO", self.opening, self.master, clock=lambda: NOW)
        self.assertEqual(client.calls, [])

    def test_fetch_long_collection_blocks(self):
        clock = iter([NOW, NOW + timedelta(seconds=61)])
        with self.assertRaises(Block): fetch(self.client(), "DEMO", self.opening, self.master, clock=lambda: next(clock))

    def test_holdings_mutations_are_not_allowlisted(self):
        for method, route in (("POST", "/portfolio/holdings"), ("PUT", "/portfolio/positions"), ("POST", "/portfolio/holdings/authorise")):
            with self.subTest(method=method, route=route), self.assertRaisesRegex(Block, "READ_ONLY"):
                KiteReadOnly().request(method, route)

    def test_holdings_get_transport_unwraps_response(self):
        calls = []
        def opener(request, timeout):
            calls.append((request.get_method(), request.full_url, request.data))
            return io.BytesIO(json.dumps({"status": "success", "data": self.raw["holdings"]}).encode())
        with patch.dict(os.environ, {"KITE_API_KEY": "synthetic", "KITE_ACCESS_TOKEN": "synthetic"}):
            result = KiteReadOnly(opener=opener).request("GET", "/portfolio/holdings")
        self.assertEqual(result, self.raw["holdings"])
        self.assertEqual(calls, [("GET", "https://api.kite.trade/portfolio/holdings", None)])

    def test_cli_saves_bundle_refuses_overwrite_and_redacts_failure(self):
        self.verify()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            for name, data in (("raw", self.raw), ("opening", self.opening), ("master", {"instruments": self.master})):
                (root / (name + ".json")).write_text(json.dumps(data), encoding="utf-8")
            args = ["--input", str(root / "raw.json"), "--opening", str(root / "opening.json"), "--master", str(root / "master.json"),
                    "--account-id", "DEMO", "--now", NOW.isoformat(), "--out", str(root / "result.json")]
            with patch("sys.stdout", new_callable=io.StringIO) as out:
                self.assertEqual(main(args), 0); self.assertEqual(main(args), 2)
                self.assertNotIn("SYNTHETIC", out.getvalue())
            self.assertEqual(json.loads((root / "result.json").read_text())["schema"], "portfolio_collection_v1")


if __name__ == "__main__": unittest.main()
