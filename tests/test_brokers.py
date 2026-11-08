"""Synthetic API-contract fixtures, not captured accounts or proof of live compatibility."""
import contextlib
import copy
import gzip
import io
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch
import urllib.error

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
from core import Block, check_order, fingerprint, instant, orb, payload, plan_from_hit, size, ticket
from demo import inputs
from replay import replay
from trader import main as trader_main
from broker import main as broker_main
from upstox_readonly import UpstoxReadOnly, NoRedirect, MASTER_URL
from kite_readonly import KiteReadOnly

KEY = "NSE_EQ|DEMO00000001"


class Response(io.BytesIO):
    def __init__(self, value, headers=None):
        super().__init__(value)
        self.headers = headers or {}


def response(data):
    return Response(json.dumps({"status": "success", "data": data}).encode())


def master_row(**changes):
    return {"instrument_key": KEY, "segment": "NSE_EQ", "instrument_type": "EQ",
            "trading_symbol": "DEMO-EQ", "lot_size": 1, "tick_size": 5, **changes}


class UpstoxTests(unittest.TestCase):
    def setUp(self):
        self.env = patch.dict(os.environ, {"UPSTOX_ACCESS_TOKEN": "PRIVATE_TEST_TOKEN"}, clear=True)
        self.env.start()
        self.addCleanup(self.env.stop)

    def test_endpoint_allowlist_before_credentials_or_network(self):
        calls = []
        client = UpstoxReadOnly(lambda *a, **k: calls.append(a))
        for method, path in [("POST", "/v3/order/place"), ("DELETE", "/v2/order/cancel"),
                             ("PUT", "/v2/order/modify"), ("GET", "https://evil.test"),
                             ("GET", "/v2/user/profile?redirect=https://evil.test"),
                             ("GET", "/v3/historical-candle/NSE_EQ%7C../minutes/5/2026-10-30/2026-10-01")]:
            with self.subTest(path=path), self.assertRaisesRegex(Block, "READ_ONLY"):
                client.request(method, path)
        self.assertEqual(calls, [])

    def test_missing_credentials(self):
        with patch.dict(os.environ, {}, clear=True), self.assertRaisesRegex(Block, "UPSTOX_AUTH"):
            UpstoxReadOnly().account_id()

    def test_redirect_denied(self):
        with self.assertRaisesRegex(Block, "REDIRECT"):
            NoRedirect().redirect_request(None, None, 302, "", {}, "https://evil.test")

    def test_network_errors_redacted_without_retry(self):
        for error in (urllib.error.HTTPError("PRIVATE_TEST_TOKEN", 401, "PRIVATE_TEST_TOKEN", {}, None),
                      urllib.error.URLError("PRIVATE_TEST_TOKEN"), TimeoutError("PRIVATE_TEST_TOKEN")):
            calls = []
            def opener(req, **kwargs):
                calls.append(req)
                raise error
            with self.assertRaises(Block) as exc:
                UpstoxReadOnly(opener).account_id()
            self.assertNotIn("PRIVATE", str(exc.exception))
            self.assertEqual(len(calls), 1)

    def test_response_errors_redacted(self):
        for raw in (b'not json PRIVATE_TEST_TOKEN', b'[]', b'{"status":"error","message":"PRIVATE_TEST_TOKEN"}',
                    b'{"status":"success","data":NaN}'):
            with self.subTest(raw=raw), self.assertRaises(Block) as exc:
                UpstoxReadOnly(lambda *a, **k: Response(raw)).account_id()
            self.assertNotIn("PRIVATE", str(exc.exception))

    def test_master_public_host_no_authorization_and_source_date_preserved(self):
        def opener(req, **kwargs):
            self.assertEqual(req.full_url, MASTER_URL)
            self.assertIsNone(req.get_header("Authorization"))
            return Response(gzip.compress(json.dumps([master_row()]).encode()),
                            {"Last-Modified": "Fri, 30 Oct 2026 01:00:00 GMT"})
        result = UpstoxReadOnly(opener).instruments()
        self.assertEqual(result["instruments"][0]["tick_size"], 0.05)
        self.assertEqual(result["instruments"][0]["master_date"], "2026-10-30")
        self.assertEqual(result["instruments"][0]["broker"], "upstox")

    def test_master_missing_source_timestamp_blocks(self):
        with self.assertRaisesRegex(Block, "MASTER"):
            UpstoxReadOnly(lambda *a, **k: Response(gzip.compress(b'[]'))).instruments()

    def test_derivative_master_units_and_expiry(self):
        expiry = int(instant("2026-11-03T23:59:59+05:30").timestamp() * 1000)
        rows = [master_row(instrument_key="NSE_FO|12345", segment="NSE_FO", instrument_type="CE",
                           expiry=expiry, underlying_type="INDEX", lot_size=65, strike_price=24000)]
        item = UpstoxReadOnly.normalize_instruments(rows, "2026-10-30", "2026-10-30T01:00:00Z")["instruments"][0]
        self.assertEqual((item["exchange"], item["expiry"], item["strike"], item["lot_size"], item["tick_size"]),
                         ("NFO", "2026-11-03", 24000, 65, 0.05))
        self.assertEqual(item["underlying_type"], "index")

    def test_master_duplicate_or_bad_segment_blocks(self):
        for rows in ([master_row(), master_row()], [master_row(segment="NSE_FO")], [master_row(tick_size=0)]):
            with self.assertRaises(Block):
                UpstoxReadOnly.normalize_instruments(rows, "2026-10-30", "2026-10-30T01:00:00Z")

    def test_candles_request_order_timestamp_and_price_units(self):
        calls = []
        def opener(req, **kwargs):
            calls.append(req)
            return response({"candles": [["2026-10-30T04:05:00Z", 100, 102, 99, 101, 10, 5],
                                         ["2026-10-30T04:00:00Z", 99, 101, 98, 100, 20, 5]]})
        result = UpstoxReadOnly(opener).candles(KEY, "DEMO-EQ", "NSE", "2026-10-30", "2026-10-30")
        self.assertIn("NSE_EQ%7CDEMO00000001/minutes/5/2026-10-30/2026-10-30", calls[0].full_url)
        self.assertEqual(result["bars"][0]["at"], "2026-10-30T09:30:00+05:30")
        self.assertEqual(result["bars"][0]["close"], 100)

    def test_candle_validation_empty_duplicate_invalid_and_foreign_days(self):
        good = ["2026-10-30T09:30:00+05:30", 100, 102, 99, 101, 10]
        for rows in ([good, good], [["2026-10-29T09:30:00+05:30", *good[1:]]],
                     [[good[0], 100, 98, 99, 101, 10]], [[good[0], 100]]):
            with self.assertRaises(Block):
                UpstoxReadOnly(lambda *a, **k: response({"candles": rows})).candles(KEY, "DEMO-EQ", "NSE", "2026-10-30", "2026-10-30")
        result = UpstoxReadOnly(lambda *a, **k: response({"candles": []})).candles(KEY, "DEMO-EQ", "NSE", "2026-10-30", "2026-10-30")
        self.assertTrue(result["empty"])

    def test_intraday_uses_separate_endpoint(self):
        calls = []
        def opener(req, **kwargs):
            calls.append(req.full_url)
            return response({"candles": []})
        UpstoxReadOnly(opener).candles(KEY, "DEMO-EQ", "NSE", intraday=True)
        self.assertTrue(calls[0].endswith("/intraday/NSE_EQ%7CDEMO00000001/minutes/5"))

    def test_quotes_use_instrument_token_not_response_dictionary_key(self):
        data = {"NSE_EQ:DEMO-EQ": {"instrument_token": KEY, "timestamp": "2026-10-30T04:05:00Z", "last_price": 102}}
        result = UpstoxReadOnly(lambda *a, **k: response(data)).quotes([KEY, "NSE_EQ|MISSING"])
        self.assertEqual(result["missing"], ["NSE_EQ|MISSING"])
        self.assertEqual(result["quotes"][KEY]["as_of"], "2026-10-30T09:35:00+05:30")
        self.assertEqual(result["quotes"][KEY]["price"], 102)

    def test_quote_without_timestamp_cannot_be_made_fresh(self):
        with self.assertRaisesRegex(Block, "UPSTOX_QUOTES"):
            UpstoxReadOnly(lambda *a, **k: response({"anything": {"instrument_token": KEY, "last_price": 102}})).quotes([KEY])

    def test_null_source_timestamps_are_not_replaced_with_current_time(self):
        with self.assertRaisesRegex(Block, "TIMESTAMP"):
            UpstoxReadOnly(lambda *a, **k: response({"anything": {"instrument_token": KEY, "last_price": 102, "timestamp": None}})).quotes([KEY])
        with self.assertRaisesRegex(Block, "TIMESTAMP"):
            UpstoxReadOnly(lambda *a, **k: response({"candles": [[None, 100, 102, 99, 101, 10]]})).candles(KEY, "DEMO-EQ", "NSE", "2026-10-30", "2026-10-30", "day")

    def test_account_identity_is_observed_not_user_asserted(self):
        def opener(req, **kwargs):
            if req.full_url.endswith("/profile"):
                return response({"user_id": "ACCOUNT-A", "email": "not-kept@example.invalid"})
            return response({"equity": {"available_margin": 10000}} if req.full_url.endswith("get-funds-and-margin") else [])
        result = UpstoxReadOnly(opener).account()
        self.assertEqual(result["account_id"], "ACCOUNT-A")
        self.assertFalse(result["reconciled"])
        self.assertNotIn("not-kept", json.dumps(result))

    def test_margin_post_mapping_bound_to_exact_payload_and_account(self):
        p = {"instrument_token": "NSE_FO|12345", "transaction_type": "BUY", "quantity": 65, "product": "I",
             "order_type": "LIMIT", "price": 100, "validity": "DAY", "disclosed_quantity": 0,
             "trigger_price": 0, "is_amo": False, "slice": False}
        def opener(req, **kwargs):
            if req.full_url.endswith("/profile"):
                return response({"user_id": "ACCOUNT-A"})
            self.assertEqual(req.full_url, "https://api.upstox.com/v2/charges/margin")
            self.assertEqual(req.get_method(), "POST")
            body = json.loads(req.data)["instruments"][0]
            self.assertEqual(body, {"instrument_key": "NSE_FO|12345", "transaction_type": "BUY", "quantity": 65, "product": "I", "price": 100})
            return response({"margins": [{}], "required_margin": 6500, "final_margin": 6600})
        result = UpstoxReadOnly(opener).margin(p)
        self.assertEqual(result["payload_hash"], fingerprint(p))
        self.assertEqual(result["account_id"], "ACCOUNT-A")
        self.assertEqual(result["required_inr"], 6600)
        with self.assertRaisesRegex(Block, "MARGIN_PAYLOAD"):
            UpstoxReadOnly(opener).margin({**p, "product": "D"})


class MultiBrokerTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.c = json.loads((ROOT / "assets/config.example.json").read_text())
        cls.c["broker"] = "upstox"
        cls.r = json.loads((ROOT / "assets/charges.example.json").read_text())
        cls.r.update(broker="upstox", source="synthetic_test_rates_not_upstox_tariff")
        cls.base = inputs(cls.c)
        now, instrument, context, history, universe, candles = cls.base
        cls.p = plan_from_hit(orb(candles["instruments"]["NSE:DEMO-EQ"]["bars"], now), instrument, cls.c, now)
        cls.rep = replay(cls.p, instrument, history, cls.c, cls.r)

    def setUp(self):
        self.config, self.card = copy.deepcopy(self.c), copy.deepcopy(self.r)
        self.now, self.i, self.ctx, self.h, self.u, self.b = copy.deepcopy(self.base)
        self.plan = copy.deepcopy(self.p)
        self.ctx["replay"] = copy.deepcopy(self.rep)

    def order(self):
        return size(self.plan, self.i, self.config, self.ctx, self.card, self.now)

    def test_upstox_full_paper_path_and_ticket(self):
        order = self.order()
        result = ticket(order, self.i, self.config, self.ctx, self.card, self.now)
        self.assertEqual(result["risk"]["status"], "PASS")
        self.assertFalse(result["submitted"])
        self.assertEqual(result["payload_preview"]["instrument_token"], KEY)
        self.assertEqual(result["payload_preview"]["product"], "I")
        self.assertNotIn("kite_payload_preview", result)
        self.assertEqual(self.rep["paths"], 10000)

    def test_cross_broker_rate_card_blocked(self):
        self.card["broker"] = "kite"
        with self.assertRaisesRegex(Block, "RATES"):
            self.order()

    def test_config_switch_invalidates_existing_plan(self):
        self.config["account_id"] = "OTHER"
        with self.assertRaisesRegex(Block, "ACCOUNT"):
            self.order()

    def test_account_snapshot_from_other_broker_or_account_blocks(self):
        for field, value in (("broker", "kite"), ("account_id", "OTHER")):
            with self.subTest(field=field):
                original = self.ctx["account"][field]
                self.ctx["account"][field] = value
                with self.assertRaisesRegex(Block, "ACCOUNT"):
                    self.order()
                self.ctx["account"][field] = original

    def test_null_account_timestamp_blocks(self):
        self.ctx["account"]["as_of"] = None
        with self.assertRaisesRegex(Block, "STALE"):
            self.order()

    def test_adapter_market_data_drives_paper_plan_and_ticket(self):
        normalized = UpstoxReadOnly.normalize_instruments([master_row()], self.now.date().isoformat(), self.now.isoformat())["instruments"][0]
        raw_bars = [[bar[k] for k in ("at", "open", "high", "low", "close", "volume")]
                    for bar in self.b["instruments"]["NSE:DEMO-EQ"]["bars"]]
        def opener(req, **kwargs):
            if "/historical-candle/" in req.full_url:
                return response({"candles": list(reversed(raw_bars))})
            return response({"NSE_EQ:DEMO-EQ": {"instrument_token": KEY, "last_price": 102, "timestamp": self.now.isoformat()}})
        with patch.dict(os.environ, {"UPSTOX_ACCESS_TOKEN": "synthetic-test"}):
            client = UpstoxReadOnly(opener)
            candles = client.candles(KEY, normalized["symbol"], normalized["exchange"], "2026-10-30", "2026-10-30")
            quote = client.quotes([KEY])["quotes"][KEY]
        plan = plan_from_hit(orb(candles["bars"], self.now), normalized, self.config, self.now)
        self.ctx["quote"] = {**quote, "symbol": normalized["symbol"]}
        self.ctx["replay"] = replay(plan, normalized, self.h, self.config, self.card)
        order = size(plan, normalized, self.config, self.ctx, self.card, self.now)
        result = ticket(order, normalized, self.config, self.ctx, self.card, self.now)
        self.assertEqual(result["risk"]["status"], "PASS")
        self.assertEqual(result["payload_preview"]["instrument_token"], normalized["token"])
        self.assertFalse(result["submitted"])

    def test_derivative_margin_account_broker_and_quantity_must_match(self):
        self.i.update(kind="option", exchange="NFO", token="NSE_FO|12345", lot_size=65, expiry="2026-11-03", underlying_type="index")
        self.plan.update(exchange="NFO", token="NSE_FO|12345", target=110)
        self.ctx["quote"]["exchange"] = "NFO"
        self.ctx["restrictions"]["instruments"]["NFO:DEMO-EQ"] = self.ctx["restrictions"]["instruments"].pop("NSE:DEMO-EQ")
        self.ctx["replay"]["plan_hash"] = fingerprint(self.plan)
        order = self.order()
        margin = {"source": "upstox", "account_id": self.config["account_id"], "payload_hash": fingerprint(payload(order)),
                  "required_inr": 20000, "as_of": self.now.isoformat()}
        self.ctx["margin"] = margin
        self.assertEqual(check_order(order, self.i, self.config, self.ctx, self.card, self.now)["status"], "PASS")
        for field, wrong in (("source", "kite"), ("account_id", "OTHER"), ("payload_hash", fingerprint(payload({**order, "quantity": 65})))):
            self.ctx["margin"] = {**margin, field: wrong}
            with self.subTest(field=field), self.assertRaisesRegex(Block, "MARGIN"):
                check_order(order, self.i, self.config, self.ctx, self.card, self.now)

    def test_history_from_other_broker_blocks(self):
        self.h["broker"] = "kite"
        with self.assertRaisesRegex(Block, "HISTORY"):
            replay(self.plan, self.i, self.h, self.config, self.card)

    def test_review_hash_changes_with_account_state(self):
        order = self.order()
        first = ticket(order, self.i, self.config, self.ctx, self.card, self.now)
        self.ctx["account"]["margin_available"] -= 1
        second = ticket(order, self.i, self.config, self.ctx, self.card, self.now)
        self.assertNotEqual(first["review_hash"], second["review_hash"])

    def test_kite_profile_identity_in_margin(self):
        p = {"exchange": "NFO", "tradingsymbol": "DEMO", "transaction_type": "BUY", "quantity": 65,
             "product": "MIS", "order_type": "LIMIT", "price": 100, "validity": "DAY"}
        def opener(req, **kwargs):
            return response({"user_id": "KITE-A"} if req.full_url.endswith("/profile") else
                            [{"tradingsymbol": "DEMO", "exchange": "NFO", "total": 6500}])
        with patch.dict(os.environ, {"KITE_API_KEY": "test", "KITE_ACCESS_TOKEN": "test"}):
            self.assertEqual(KiteReadOnly(opener).margin(p)["account_id"], "KITE-A")


class BrokerCliTests(unittest.TestCase):
    def run_cli(self, fn, args):
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            code = fn(args)
        return code, json.loads(output.getvalue())

    def test_all_demo_cli_paths(self):
        for broker in ("kite", "upstox", "dhan", "angel", "fyers"):
            with self.subTest(broker=broker), tempfile.TemporaryDirectory() as d:
                code, result = self.run_cli(trader_main, ["demo", "--broker", broker, "--workdir", d])
                self.assertEqual(code, 0)
                self.assertEqual(result["risk"], "PASS")
                t = json.loads((Path(d) / "ticket.json").read_text())
                self.assertEqual(t["broker"], broker)
                self.assertFalse(t["submitted"])
                self.assertEqual(json.loads((Path(d) / "charges.json").read_text())["status"], "fictional_test_only")

    def test_upstox_init_never_inherits_kite_rates(self):
        with tempfile.TemporaryDirectory() as d:
            code, result = self.run_cli(trader_main, ["init", "--broker", "upstox", "--workdir", d])
            self.assertEqual(code, 0)
            self.assertFalse(result["rates_configured"])
            rates = json.loads((Path(d) / "charges.json").read_text())
            self.assertIsNone(rates["brokerage_rate"])
            self.assertEqual(rates["segments"], {})

    def test_capabilities_and_unimplemented_brokers(self):
        for name in ("kite", "upstox", "dhan", "angel", "fyers"):
            code, result = self.run_cli(broker_main, ["capabilities", "--broker", name])
            self.assertEqual(code, 0)
            self.assertFalse(result["live_execution"])
        for name in ("unknown",):
            self.assertEqual(self.run_cli(broker_main, ["capabilities", "--broker", name])[0], 2)

    def test_existing_snapshot_does_not_call_network(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "account.json"
            p.write_text("keep")
            with patch.object(UpstoxReadOnly, "account", side_effect=AssertionError("network called")):
                code, result = self.run_cli(broker_main, ["account", "--broker", "upstox", "--out", str(p)])
            self.assertEqual(code, 2)
            self.assertEqual(p.read_text(), "keep")

    def test_broker_cli_persists_snapshot_without_printing_private_content(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "account.json"
            with patch.object(UpstoxReadOnly, "account", return_value={"account_id": "PRIVATE_ACCOUNT", "reconciled": False}):
                code, result = self.run_cli(broker_main, ["account", "--broker", "upstox", "--out", str(p)])
            self.assertEqual(code, 0)
            self.assertNotIn("PRIVATE_ACCOUNT", json.dumps(result))
            self.assertEqual(json.loads(p.read_text())["account_id"], "PRIVATE_ACCOUNT")


if __name__ == "__main__":
    unittest.main()
