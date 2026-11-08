"""Fictional Dhan v2 contract fixtures; no credentials or authenticated network calls."""
import contextlib
import copy
import csv
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
from broker import main as broker_main
from core import Block, check_order, fingerprint, fresh, instant, orb, payload, plan_from_hit, size, ticket
from dhan_readonly import DhanReadOnly, NoRedirect, MASTER_URL
from demo import inputs
from replay import replay
from trader import main as trader_main

ACCOUNT = "1000000001"
TOKEN = "999999999"
STAMP = "2026-10-30T09:35:00+05:30"


class Response(io.BytesIO):
    def __init__(self, value, headers=None):
        super().__init__(value)
        self.headers = headers or {}


def response(value):
    return Response(json.dumps(value).encode())


def master_row(**changes):
    return {"EXCH_ID": "NSE", "SEGMENT": "E", "SECURITY_ID": TOKEN, "INSTRUMENT": "EQUITY",
            "SYMBOL_NAME": "Fictional Demonstration Equity", "UNDERLYING_SYMBOL": "DEMO-EQ",
            "LOT_SIZE": "1.0", "TICK_SIZE": "5.0000", "SM_EXPIRY_DATE": "", "STRIKE_PRICE": "",
            "OPTION_TYPE": "", "DISPLAY_NAME": "Fictional Demo", "SERIES": "EQ", "ASM_GSM_FLAG": "N", **changes}


def csv_bytes(rows):
    stream = io.StringIO()
    writer = csv.DictWriter(stream, fieldnames=list(rows[0]))
    writer.writeheader()
    writer.writerows(rows)
    return stream.getvalue().encode()


def bars_response(bars):
    return {"timestamp": [int(instant(b["at"]).timestamp()) for b in bars],
            **{key: [b[key] for b in bars] for key in ("open", "high", "low", "close", "volume")}}


def preview(**changes):
    return {"dhanClientId": ACCOUNT, "transactionType": "BUY", "exchangeSegment": "NSE_FNO", "productType": "INTRADAY",
            "orderType": "LIMIT", "validity": "DAY", "securityId": "12345", "quantity": 65, "price": 100,
            "triggerPrice": 0, "disclosedQuantity": 0, "afterMarketOrder": False, **changes}


class DhanTests(unittest.TestCase):
    def setUp(self):
        env = patch.dict(os.environ, {"DHAN_CLIENT_ID": ACCOUNT, "DHAN_ACCESS_TOKEN": "PRIVATE_TOKEN"}, clear=True)
        env.start()
        self.addCleanup(env.stop)

    def test_mutation_auth_and_external_routes_rejected_before_auth(self):
        calls = []
        client = DhanReadOnly(lambda *a, **kw: calls.append(a))
        with patch.dict(os.environ, {}, clear=True):
            for method, path in (("POST", "/orders"), ("PUT", "/orders/1"), ("DELETE", "/orders/1"),
                                 ("POST", "/orders/slicing"), ("POST", "/RenewToken"),
                                 ("GET", "https://evil.test"), ("GET", "/fundlimit?x=1"), ("POST", "/killSwitch")):
                with self.subTest(path=path), self.assertRaisesRegex(Block, "READ_ONLY"):
                    client.request(method, path)
        self.assertEqual(calls, [])

    def test_missing_credentials_block_network(self):
        with patch.dict(os.environ, {}, clear=True), self.assertRaisesRegex(Block, "DHAN_AUTH"):
            DhanReadOnly().account()

    def test_redirect_refused(self):
        with self.assertRaisesRegex(Block, "REDIRECT"):
            NoRedirect().redirect_request(None, None, 302, "", {}, "https://evil.test")

    def test_http_network_and_timeout_redaction_no_retry(self):
        for error in (urllib.error.HTTPError("PRIVATE_TOKEN", 429, "PRIVATE_TOKEN", {}, None),
                      urllib.error.URLError("PRIVATE_TOKEN"), TimeoutError("PRIVATE_TOKEN")):
            calls = []
            def opener(req, **kwargs):
                calls.append(req)
                raise error
            with self.subTest(error=type(error).__name__), self.assertRaises(Block) as caught:
                DhanReadOnly(opener).account_id()
            self.assertNotIn("PRIVATE", str(caught.exception))
            self.assertEqual(len(calls), 1)

    def test_error_bodies_and_nonfinite_json_redacted(self):
        for raw in (b'{"errorCode":"DH-901","errorMessage":"PRIVATE_TOKEN"}',
                    b'{"status":"failure","data":"PRIVATE_TOKEN"}', b'PRIVATE_TOKEN',
                    b'{"totalMargin":NaN}', b'null'):
            with self.subTest(raw=raw), self.assertRaises(Block) as caught:
                DhanReadOnly(lambda *a, **k: Response(raw)).request("GET", "/fundlimit")
            self.assertNotIn("PRIVATE", str(caught.exception))

    def test_public_master_source_date_units_and_no_auth(self):
        def opener(req, **kwargs):
            self.assertEqual(req.full_url, MASTER_URL)
            self.assertIsNone(req.get_header("Access-token"))
            self.assertIsNone(req.get_header("Client-id"))
            return Response(csv_bytes([master_row()]), {"Last-Modified": "Fri, 30 Oct 2026 02:50:00 GMT"})
        with patch.dict(os.environ, {}, clear=True):
            result = DhanReadOnly(opener).instruments()
        i = result["instruments"][0]
        self.assertEqual(i["master_date"], "2026-10-30")
        self.assertEqual((i["tick_size"], i["lot_size"], i["broker_instrument"]), (0.05, 1, "EQUITY"))
        self.assertEqual(i["symbol"], "DEMO-EQ@999999999")
        self.assertEqual(i["raw_surveillance_flag"], "N")
        self.assertNotIn("restrictions", result)

    def test_master_missing_timestamp_columns_or_bad_csv_blocks(self):
        cases = [(csv_bytes([master_row()]), {}), (b'a,b\n1,2\n', {"Last-Modified": "Fri, 30 Oct 2026 02:50:00 GMT"}),
                 (b'\xff', {"Last-Modified": "Fri, 30 Oct 2026 02:50:00 GMT"})]
        for raw, headers in cases:
            with self.assertRaisesRegex(Block, "MASTER"):
                DhanReadOnly(lambda *a, **k: Response(raw, headers)).instruments()

    def test_future_master_date_blocks(self):
        with self.assertRaisesRegex(Block, "MASTER"):
            DhanReadOnly(lambda *a, **k: Response(csv_bytes([master_row()]), {"Last-Modified": "Sat, 01 Jan 2095 00:00:00 GMT"})).instruments()

    def test_derivative_identity_expiry_strike_and_tick_conversion(self):
        rows = [master_row(SEGMENT="D", INSTRUMENT="OPTIDX", SECURITY_ID="12345", UNDERLYING_SYMBOL="NIFTY",
                           LOT_SIZE="65.0", SM_EXPIRY_DATE="2026-11-03", STRIKE_PRICE="24000.00000", OPTION_TYPE="CE"),
                master_row(SEGMENT="D", INSTRUMENT="FUTIDX", SECURITY_ID="12346", UNDERLYING_SYMBOL="NIFTY",
                           LOT_SIZE="65.0", SM_EXPIRY_DATE="2026-11-24", STRIKE_PRICE="-0.01000", OPTION_TYPE="XX", TICK_SIZE="20.0000")]
        items = DhanReadOnly.normalize_instruments(rows, "2026-10-30", STAMP)["instruments"]
        self.assertEqual(items[0]["symbol"], "NIFTY-2026-11-03-24000-CE@12345")
        self.assertEqual((items[0]["exchange"], items[0]["underlying_type"], items[0]["strike"]), ("NFO", "index", 24000))
        self.assertIsNone(items[1]["strike"])
        self.assertEqual(items[1]["tick_size"], 0.2)

    def test_same_security_id_on_different_exchanges_is_distinct(self):
        items = DhanReadOnly.normalize_instruments([master_row(), master_row(EXCH_ID="BSE")], "2026-10-30", STAMP)["instruments"]
        self.assertEqual(len(items), 2)
        self.assertNotEqual(items[0]["exchange"], items[1]["exchange"])

    def test_master_duplicate_bad_segment_and_invalid_units(self):
        for rows in ([master_row(), master_row()], [master_row(SEGMENT="D")], [master_row(TICK_SIZE="0")],
                     [master_row(LOT_SIZE="1.5")], [master_row(UNDERLYING_SYMBOL="")]):
            with self.assertRaises(Block):
                DhanReadOnly.normalize_instruments(rows, "2026-10-30", STAMP)

    def test_candle_request_and_epoch_seconds_in_ist(self):
        bar = {"at": "2026-10-30T04:00:00Z", "open": 100, "high": 102, "low": 99, "close": 101, "volume": 50}
        def opener(req, **kwargs):
            self.assertEqual(req.full_url, "https://api.dhan.co/v2/charts/intraday")
            self.assertEqual(req.get_header("Client-id"), ACCOUNT)
            data = json.loads(req.data)
            self.assertEqual(data["interval"], "5")
            self.assertEqual(data["instrument"], "EQUITY")
            self.assertEqual(data["fromDate"], "2026-10-30 09:15:00")
            return response(bars_response([bar]))
        result = DhanReadOnly(opener).candles(TOKEN, "DEMO", "NSE", "2026-10-30T03:45:00Z", STAMP, instrument_type="EQUITY")
        self.assertEqual(result["bars"][0]["at"], "2026-10-30T09:30:00+05:30")
        self.assertEqual(result["bars"][0]["close"], 101)

    def test_candle_requires_master_instrument_type(self):
        for kind in (None, "INDEX", "OPTIDX"):
            with self.assertRaisesRegex(Block, "DHAN_HISTORY"):
                DhanReadOnly().candles(TOKEN, "DEMO", "NSE", "2026-10-30T09:15:00+05:30", STAMP, instrument_type=kind)

    def test_bad_range_rejected_before_network(self):
        for start, end in ((STAMP, STAMP), ("2026-01-01T09:15:00+05:30", STAMP), ("2026-10-30T09:15:00", STAMP)):
            with self.assertRaises(Block):
                DhanReadOnly().candles(TOKEN, "DEMO", "NSE", start, end, instrument_type="EQUITY")

    def test_daily_end_exclusive_and_boundary_bar_disclosed(self):
        def opener(req, **kwargs):
            self.assertEqual(req.full_url, "https://api.dhan.co/v2/charts/historical")
            body = json.loads(req.data)
            self.assertEqual(body["toDate"], "2026-10-30")
            self.assertNotIn("expiryCode", body)
            bars = [{"at": t, "open": 100, "high": 102, "low": 99, "close": 101, "volume": 10}
                    for t in ("2026-10-29T00:00:00+05:30", "2026-10-30T00:00:00+05:30")]
            return response(bars_response(bars))
        result = DhanReadOnly(opener).candles(TOKEN, "DEMO", "NSE", "2026-10-29", "2026-10-30", "day", "EQUITY")
        self.assertEqual(len(result["bars"]), 1)
        self.assertEqual(result["excluded_end_boundary"], 1)

    def test_parallel_array_lengths_and_bad_rows_fail_closed(self):
        good = {"timestamp": [int(instant("2026-10-30T09:30:00+05:30").timestamp())],
                "open": [100], "high": [102], "low": [99], "close": [101], "volume": [10]}
        cases = [{**good, "volume": []}, {**good, "open_interest": []}, {**good, "timestamp": [None]},
                 {**good, "timestamp": [good["timestamp"][0] * 1000]}, {**good, "high": [98]},
                 {key: values * 2 for key, values in good.items()}, {**good, "timestamp": [good["timestamp"][0] + 1]}]
        for data in cases:
            with self.assertRaises(Block):
                DhanReadOnly(lambda *a, **k: response(data)).candles(TOKEN, "DEMO", "NSE", "2026-10-30T09:15:00+05:30", STAMP, instrument_type="EQUITY")

    def test_empty_arrays_explicit(self):
        data = {k: [] for k in ("timestamp", "open", "high", "low", "close", "volume")}
        result = DhanReadOnly(lambda *a, **k: response(data)).candles(TOKEN, "DEMO", "NSE", "2026-10-30T09:15:00+05:30", STAMP, instrument_type="EQUITY")
        self.assertTrue(result["empty"])

    def test_quote_grouping_and_missing_coverage(self):
        def opener(req, **kwargs):
            self.assertEqual(req.get_method(), "POST")
            self.assertEqual(req.full_url, "https://api.dhan.co/v2/marketfeed/quote")
            self.assertEqual(json.loads(req.data), {"NSE_EQ": [100, 101], "NSE_FNO": [100]})
            return response({"status": "success", "data": {"NSE_EQ": {"100": {"last_price": 102, "last_trade_time": "30/10/2026 09:35:00"}}}})
        result = DhanReadOnly(opener).quotes(["NSE_EQ:100", "NSE_EQ:101", "NSE_FNO:100"])
        self.assertEqual(result["missing"], ["NSE_EQ:101", "NSE_FNO:100"])
        self.assertEqual(result["quotes"]["NSE_EQ:100"]["as_of"], STAMP)

    def test_old_trade_time_not_made_current(self):
        data = {"status": "success", "data": {"NSE_EQ": {"100": {"last_price": 102, "last_trade_time": "01/01/1980 00:00:00"}}}}
        result = DhanReadOnly(lambda *a, **k: response(data)).quotes(["NSE_EQ:100"])
        with self.assertRaisesRegex(Block, "STALE"):
            fresh(result["quotes"]["NSE_EQ:100"]["as_of"], instant(STAMP), 60, "quote")

    def test_missing_or_malformed_trade_time_blocks(self):
        for ts in (None, "", "PRIVATE_TOKEN", "2026-10-30T09:35:00+05:30"):
            data = {"status": "success", "data": {"NSE_EQ": {"100": {"last_price": 102, "last_trade_time": ts}}}}
            with self.assertRaisesRegex(Block, "TIMESTAMP") as caught:
                DhanReadOnly(lambda *a, **k: response(data)).quotes(["NSE_EQ:100"])
            self.assertNotIn("PRIVATE", str(caught.exception))

    def test_unknown_quote_identity_rejected(self):
        data = {"status": "success", "data": {"BSE_EQ": {"100": {"last_price": 102, "last_trade_time": "30/10/2026 09:35:00"}}}}
        with self.assertRaisesRegex(Block, "QUOTES"):
            DhanReadOnly(lambda *a, **k: response(data)).quotes(["NSE_EQ:100"])

    def test_account_observed_identity_and_unreconciled_status(self):
        def opener(req, **kwargs):
            if req.full_url.endswith("/fundlimit"):
                return response({"dhanClientId": ACCOUNT, "availabelBalance": 100000})
            return response([])
        result = DhanReadOnly(opener).account()
        self.assertEqual(result["account_id"], ACCOUNT)
        self.assertFalse(result["reconciled"])

    def test_cross_account_funds_and_position_rows_block(self):
        with self.assertRaisesRegex(Block, "ACCOUNT"):
            DhanReadOnly(lambda *a, **k: response({"dhanClientId": "OTHER", "availabelBalance": 100000})).account()
        def opener(req, **kwargs):
            return response({"dhanClientId": ACCOUNT, "availabelBalance": 100000} if req.full_url.endswith("/fundlimit") else [{"dhanClientId": "OTHER"}])
        with self.assertRaisesRegex(Block, "ACCOUNT"):
            DhanReadOnly(opener).account()

    def test_margin_observed_account_and_exact_request_hash(self):
        p = preview()
        def opener(req, **kwargs):
            if req.full_url.endswith("/fundlimit"):
                return response({"dhanClientId": ACCOUNT, "availabelBalance": 100000})
            self.assertEqual(req.full_url, "https://api.dhan.co/v2/margincalculator")
            body = json.loads(req.data)
            self.assertEqual(body["dhanClientId"], ACCOUNT)
            self.assertEqual(body["securityId"], "12345")
            self.assertEqual(body["productType"], "INTRADAY")
            self.assertEqual(body["quantity"], 65)
            self.assertNotIn("orderType", body)
            return response({"totalMargin": 6500, "brokerage": 20})
        result = DhanReadOnly(opener).margin(p)
        self.assertEqual(result["required_inr"], 6500)
        self.assertEqual(result["payload_hash"], fingerprint(p))
        self.assertEqual(result["account_id"], ACCOUNT)

    def test_invalid_margin_payload_never_calls_network(self):
        for changes in ({"dhanClientId": "OTHER"}, {"orderType": "MARKET"}, {"afterMarketOrder": True},
                        {"quantity": 1.5}, {"securityId": "NSE_EQ|100"}, {"productType": "CNC"}, {"price": 0}):
            with self.subTest(changes=changes), self.assertRaises(Block):
                DhanReadOnly(lambda *a, **k: self.fail("unexpected network call")).margin(preview(**changes))


class DhanWorkflowTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.config = json.loads((ROOT / "assets/config.example.json").read_text())
        cls.config.update(broker="dhan", account_id=ACCOUNT)
        cls.card = json.loads((ROOT / "assets/charges.example.json").read_text())
        cls.card.update(broker="dhan", source="synthetic_test_rates_not_dhan_tariff")
        cls.base = inputs(cls.config)

    def setUp(self):
        self.c, self.card = copy.deepcopy(self.config), copy.deepcopy(self.card)
        self.now, self.i, self.ctx, self.history, self.universe, self.candles = copy.deepcopy(self.base)

    def run_cli(self, fn, args):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = fn(args)
        return code, json.loads(out.getvalue())

    def test_adapter_to_replay_size_risk_and_ticket(self):
        normalized = DhanReadOnly.normalize_instruments([master_row()], "2026-10-30", STAMP)["instruments"][0]
        rows = self.candles["instruments"]["NSE:DEMO-EQ"]["bars"]
        def opener(req, **kwargs):
            if req.full_url.endswith("/charts/intraday"):
                data = json.loads(req.data)
                return response(bars_response(rows if data["fromDate"].startswith("2026-10-30") else self.history["bars"]))
            return response({"status": "success", "data": {"NSE_EQ": {TOKEN: {"last_price": 102, "last_trade_time": "30/10/2026 09:35:00"}}}})
        with patch.dict(os.environ, {"DHAN_CLIENT_ID": ACCOUNT, "DHAN_ACCESS_TOKEN": "test"}):
            client = DhanReadOnly(opener)
            current = client.candles(TOKEN, normalized["symbol"], "NSE", "2026-10-30T09:15:00+05:30", STAMP, instrument_type="EQUITY")
            historical = client.candles(TOKEN, normalized["symbol"], "NSE", self.history["bars"][0]["at"], "2026-10-30T00:00:00+05:30", instrument_type="EQUITY")
            quote = client.quotes(["NSE_EQ:" + TOKEN])["quotes"]["NSE_EQ:" + TOKEN]
        self.ctx["quote"] = {**quote, "symbol": normalized["symbol"]}
        flags = self.ctx["restrictions"]["instruments"].pop("NSE:DEMO-EQ")
        self.ctx["restrictions"]["instruments"]["NSE:" + normalized["symbol"]] = flags
        p = plan_from_hit(orb(current["bars"], self.now), normalized, self.c, self.now)
        self.ctx["replay"] = replay(p, normalized, historical, self.c, self.card)
        order = size(p, normalized, self.c, self.ctx, self.card, self.now)
        result = ticket(order, normalized, self.c, self.ctx, self.card, self.now)
        self.assertEqual(result["risk"]["status"], "PASS")
        self.assertEqual(result["payload_preview"]["securityId"], TOKEN)
        self.assertEqual(result["payload_preview"]["productType"], "INTRADAY")
        self.assertEqual(self.ctx["replay"]["paths"], 10000)
        self.assertFalse(result["submitted"])

    def test_derivative_margin_binds_broker_account_and_payload(self):
        self.i.update(kind="option", exchange="NFO", lot_size=65, expiry="2026-11-03", underlying_type="index")
        p = plan_from_hit(orb(self.candles["instruments"]["NSE:DEMO-EQ"]["bars"], self.now), self.i, self.c, self.now)
        p["target"] = 110
        self.ctx["quote"]["exchange"] = "NFO"
        self.ctx["restrictions"]["instruments"]["NFO:DEMO-EQ"] = self.ctx["restrictions"]["instruments"].pop("NSE:DEMO-EQ")
        self.history["exchange"] = "NFO"
        self.ctx["replay"] = replay(p, self.i, self.history, self.c, self.card)
        order = size(p, self.i, self.c, self.ctx, self.card, self.now)
        encoded = payload(order)
        self.assertEqual(encoded["exchangeSegment"], "NSE_FNO")
        margin = {"source": "dhan", "account_id": ACCOUNT, "as_of": STAMP,
                  "payload_hash": fingerprint(encoded), "required_inr": 20000}
        self.ctx["margin"] = margin
        self.assertEqual(check_order(order, self.i, self.c, self.ctx, self.card, self.now)["status"], "PASS")
        for field, wrong in (("source", "kite"), ("account_id", "OTHER"), ("payload_hash", "wrong")):
            self.ctx["margin"] = {**margin, field: wrong}
            with self.subTest(field=field), self.assertRaisesRegex(Block, "MARGIN"):
                check_order(order, self.i, self.c, self.ctx, self.card, self.now)

    def test_init_has_no_inherited_fee_assumptions(self):
        with tempfile.TemporaryDirectory() as d:
            code, result = self.run_cli(trader_main, ["init", "--broker", "dhan", "--workdir", d])
            self.assertEqual(code, 0)
            self.assertFalse(result["rates_configured"])
            card = json.loads((Path(d) / "charges.json").read_text())
            self.assertEqual(card["broker"], "dhan")
            self.assertIsNone(card["brokerage_rate"])

    def test_cli_passes_master_instrument_type(self):
        with tempfile.TemporaryDirectory() as d:
            i = Path(d) / "instrument.json"
            i.write_text(json.dumps({**self.i, "broker_instrument": "EQUITY"}))
            out = Path(d) / "history.json"
            with patch.object(DhanReadOnly, "candles", return_value={"bars": []}) as fetch:
                code, _ = self.run_cli(broker_main, ["candles", "--broker", "dhan", "--instrument", str(i),
                                                    "--from", "2026-10-30T09:15:00+05:30", "--to", STAMP, "--out", str(out)])
            self.assertEqual(code, 0)
            self.assertEqual(fetch.call_args.kwargs["instrument_type"], "EQUITY")

    def test_cli_failure_suppresses_raw_response_and_writes_nothing(self):
        with tempfile.TemporaryDirectory() as d:
            out = Path(d) / "account.json"
            with patch.object(DhanReadOnly, "account", side_effect=ValueError("PRIVATE_TOKEN")):
                code, result = self.run_cli(broker_main, ["account", "--broker", "dhan", "--out", str(out)])
            self.assertEqual(code, 2)
            self.assertNotIn("PRIVATE", json.dumps(result))
            self.assertFalse(out.exists())


if __name__ == "__main__":
    unittest.main()
