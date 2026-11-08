"""Fictional SmartAPI contracts; no real credentials or network calls."""
import copy
import io
import json
import os
from pathlib import Path
import sys
import unittest
from unittest.mock import patch
import urllib.error

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
from angel_readonly import AngelReadOnly, NoRedirect, MASTER_URL, PROFILE, FUNDS, POSITIONS, ORDERS, CANDLES, QUOTES, MARGIN
from core import Block, check_order, fingerprint, fresh, instant, orb, payload, plan_from_hit, size, ticket
from demo import inputs
from replay import replay

STAMP = "2026-10-30T09:35:00+05:30"
TOKEN = "999999999"
ACCOUNT = "DEMO123"
ENV = {"ANGEL_API_KEY": "TEST_KEY", "ANGEL_ACCESS_TOKEN": "PRIVATE_TOKEN", "ANGEL_CLIENT_CODE": ACCOUNT,
       "ANGEL_LOCAL_IP": "192.0.2.1", "ANGEL_PUBLIC_IP": "203.0.113.1", "ANGEL_MAC_ADDRESS": "02:00:00:00:00:01"}


class Response(io.BytesIO):
    def __init__(self, value, headers=None):
        super().__init__(value)
        self.headers = headers or {}


def response(data):
    return Response(json.dumps({"status": True, "errorcode": "", "data": data}).encode())


def master(**changes):
    return {"token": TOKEN, "symbol": "DEMO-EQ", "name": "DEMO", "expiry": "", "strike": "-1",
            "lotsize": "1", "instrumenttype": "", "exch_seg": "NSE", "tick_size": "5", **changes}


def quote(**changes):
    return {"exchange": "NSE", "symbolToken": TOKEN, "tradingSymbol": "DEMO-EQ", "ltp": 102,
            "exchTradeTime": "30-Oct-2026 09:35:00", "exchFeedTime": "30-Oct-2026 09:35:05", **changes}


def preview(**changes):
    return {"variety": "NORMAL", "tradingsymbol": "DEMO-EQ", "symboltoken": TOKEN, "transactiontype": "BUY",
            "exchange": "NSE", "ordertype": "LIMIT", "producttype": "INTRADAY", "duration": "DAY",
            "price": 102, "quantity": 10, "squareoff": 0, "stoploss": 0, **changes}


def candle_rows(bars):
    return [[b[k] for k in ("at", "open", "high", "low", "close", "volume")] for b in bars]


class AngelTests(unittest.TestCase):
    def setUp(self):
        p = patch.dict(os.environ, ENV, clear=True)
        p.start()
        self.addCleanup(p.stop)

    def candles(self, data, **changes):
        args = dict(token=TOKEN, symbol="DEMO-EQ", exchange="NSE", start="2026-10-30T09:15:00+05:30", end=STAMP)
        return AngelReadOnly(lambda *a, **k: response(data)).candles(**{**args, **changes})

    def test_allowlist_rejects_mutations_before_credentials(self):
        with patch.dict(os.environ, {}, clear=True):
            for method, route in (("POST", ORDERS), ("DELETE", ORDERS), ("POST", "/rest/auth/angelbroking/user/v1/loginByPassword"),
                                  ("GET", QUOTES), ("GET", PROFILE + "?extra=1")):
                with self.assertRaisesRegex(Block, "READ_ONLY"):
                    AngelReadOnly().request(method, route)

    def test_missing_or_invalid_settings_never_call_network(self):
        for changes in ({"ANGEL_API_KEY": ""}, {"ANGEL_LOCAL_IP": "bad"}, {"ANGEL_PUBLIC_IP": "bad"},
                        {"ANGEL_MAC_ADDRESS": "bad"}, {"ANGEL_ACCESS_TOKEN": "x\ny"}):
            with patch.dict(os.environ, changes), self.assertRaisesRegex(Block, "ANGEL_AUTH"):
                AngelReadOnly(lambda *a, **k: self.fail("network")).request("GET", PROFILE)

    def test_auth_headers_and_profile_identity(self):
        def opener(req, **kwargs):
            self.assertEqual(req.get_header("Authorization"), "Bearer PRIVATE_TOKEN")
            self.assertEqual(req.get_header("X-privatekey"), "TEST_KEY")
            self.assertEqual(req.get_header("X-clientlocalip"), ENV["ANGEL_LOCAL_IP"])
            self.assertEqual(req.get_header("X-clientpublicip"), ENV["ANGEL_PUBLIC_IP"])
            self.assertEqual(req.get_header("X-macaddress"), ENV["ANGEL_MAC_ADDRESS"])
            self.assertEqual(kwargs["timeout"], 30)
            return response({"clientcode": ACCOUNT})
        self.assertEqual(AngelReadOnly(opener).account_id(), ACCOUNT)

    def test_redirect_rejected(self):
        with self.assertRaisesRegex(Block, "REDIRECT"):
            NoRedirect().redirect_request(None, None, 302, "", {}, "https://example.test")

    def test_transport_failures_redacted_without_retry(self):
        for error in (urllib.error.HTTPError("PRIVATE", 429, "PRIVATE", {}, None), urllib.error.URLError("PRIVATE"), TimeoutError("PRIVATE")):
            calls = []
            def opener(*args, **kwargs):
                calls.append(1)
                raise error
            with self.assertRaises(Block) as caught:
                AngelReadOnly(opener).request("GET", PROFILE)
            self.assertNotIn("PRIVATE", str(caught.exception))
            self.assertEqual(len(calls), 1)

    def test_strict_envelope_and_nonfinite_json(self):
        for raw in (b'null', b'PRIVATE', b'{"status":"true","data":{}}', b'{"status":true}',
                    b'{"status":true,"data":NaN}', b'{"status":true,"data":{},"errorCode":"PRIVATE"}',
                    b'{"status":false,"data":"PRIVATE"}'):
            with self.assertRaises(Block) as caught:
                AngelReadOnly(lambda *a, **k: Response(raw)).request("GET", PROFILE)
            self.assertNotIn("PRIVATE", str(caught.exception))

    def test_response_size_cap(self):
        with patch("angel_readonly.MAX_BYTES", 10), self.assertRaisesRegex(Block, "too large"):
            AngelReadOnly(lambda *a, **k: Response(b'x' * 11)).request("GET", PROFILE)

    def test_slow_download_deadline(self):
        with patch("angel_readonly.monotonic_clock.monotonic", side_effect=[0, 61]), self.assertRaisesRegex(Block, "deadline"):
            AngelReadOnly(lambda *a, **k: response({})).request("GET", PROFILE)

    def test_public_master_no_auth_and_preserves_source_date(self):
        def opener(req, **kwargs):
            self.assertEqual(req.full_url, MASTER_URL)
            self.assertIsNone(req.get_header("Authorization"))
            self.assertIsNone(req.get_header("X-privatekey"))
            return Response(json.dumps([master()]).encode(), {"Last-Modified": "Fri, 30 Oct 2026 02:50:00 GMT"})
        with patch.dict(os.environ, {}, clear=True):
            i = AngelReadOnly(opener).instruments()["instruments"][0]
        self.assertEqual((i["tick_size"], i["master_date"], i["token"]), (0.05, "2026-10-30", TOKEN))

    def test_master_missing_or_future_source_date(self):
        for headers in ({}, {"Last-Modified": "bad"}, {"Last-Modified": "Sat, 01 Jan 2095 00:00:00 GMT"}):
            with self.assertRaisesRegex(Block, "MASTER"):
                AngelReadOnly(lambda *a, **k: Response(b'[]', headers)).instruments()

    def test_derivative_units_expiry_and_index_exclusion(self):
        rows = [master(exch_seg="NFO", instrumenttype="OPTIDX", symbol="DEMO03NOV26CE", expiry="03NOV2026", strike="2400000", lotsize="65"),
                master(exch_seg="NFO", token="12345", instrumenttype="FUTIDX", expiry="24NOV2026", tick_size="20"),
                master(instrumenttype="AMXIDX", tick_size="0")]
        items = AngelReadOnly.normalize_instruments(rows, "2026-10-30", STAMP)["instruments"]
        self.assertEqual(len(items), 2)
        self.assertEqual((items[0]["strike"], items[0]["expiry"], items[0]["lot_size"]), (24000, "2026-11-03", 65))
        self.assertEqual(items[1]["tick_size"], 0.2)
        self.assertIsNone(items[1]["strike"])

    def test_master_malformed_rows_fail_closed(self):
        for rows in ([master(), master()], [master(tick_size="0")], [master(lotsize="1.5")], [master(token="01")],
                     [master(symbol="")], [master(instrumenttype="OPTIDX")], [None]):
            with self.assertRaises(Block):
                AngelReadOnly.normalize_instruments(rows, "2026-10-30", STAMP)

    def test_same_token_different_exchanges_is_distinct(self):
        self.assertEqual(len(AngelReadOnly.normalize_instruments([master(), master(exch_seg="BSE")], "2026-10-30", STAMP)["instruments"]), 2)

    def test_candle_request_converts_utc_to_ist(self):
        def opener(req, **kwargs):
            self.assertTrue(req.full_url.endswith(CANDLES))
            self.assertEqual(json.loads(req.data), {"exchange": "NSE", "symboltoken": TOKEN, "interval": "FIVE_MINUTE",
                                                  "fromdate": "2026-10-30 09:15", "todate": "2026-10-30 09:35"})
            return response([["2026-10-30T04:00:00Z", 100, 102, 99, 101, 50]])
        result = AngelReadOnly(opener).candles(TOKEN, "DEMO-EQ", "NSE", "2026-10-30T03:45:00Z", STAMP)
        self.assertEqual(result["bars"][0]["at"], "2026-10-30T09:30:00+05:30")

    def test_candle_range_invalid_before_network(self):
        for start in (STAMP, "2026-01-01T09:15:00+05:30", "2026-10-30T09:15:01+05:30", "2026-10-30T09:15:00"):
            with self.assertRaises(Block):
                AngelReadOnly(lambda *a, **k: self.fail("network")).candles(TOKEN, "DEMO-EQ", "NSE", start, STAMP)

    def test_candle_end_boundary_excluded(self):
        result = self.candles([["2026-10-30T09:30:00+05:30", 100, 102, 99, 101, 10], [STAMP, 100, 102, 99, 101, 10]])
        self.assertEqual((len(result["bars"]), result["excluded_end_boundary"]), (1, 1))

    def test_candle_bad_rows_and_null_rejected(self):
        row = ["2026-10-30T09:30:00+05:30", 100, 102, 99, 101, 10]
        for data in (None, [row, row], [[None, *row[1:]]], [[row[0], 100, 98, 99, 101, 10]],
                     [[row[0], 100, 102, 99, 101, -1]], [["2026-10-30T09:31:00+05:30", *row[1:]]], [row[:5]]):
            with self.assertRaises(Block):
                self.candles(data)

    def test_empty_candles_explicit(self):
        self.assertTrue(self.candles([])["empty"])

    def test_daily_end_exclusive(self):
        result = self.candles([["2026-10-29T00:00:00+05:30", 100, 102, 99, 101, 1],
                               ["2026-10-30T00:00:00+05:30", 100, 102, 99, 101, 1]],
                              start="2026-10-29", end="2026-10-30", interval="day")
        self.assertEqual((len(result["bars"]), result["excluded_end_boundary"]), (1, 1))

    def test_quote_grouping_partial_coverage_and_trade_time(self):
        def opener(req, **kwargs):
            self.assertEqual(json.loads(req.data), {"mode": "FULL", "exchangeTokens": {"NSE": [TOKEN, "2"], "NFO": ["3"]}})
            return response({"fetched": [quote()], "unfetched": [{"exchange": "NSE", "symbolToken": "2"}]})
        result = AngelReadOnly(opener).quotes(["NSE:" + TOKEN, "NSE:2", "NFO:3"])
        self.assertEqual(result["missing"], ["NSE:2", "NFO:3"])
        self.assertEqual(result["quotes"]["NSE:" + TOKEN]["as_of"], STAMP)

    def test_old_quote_not_refreshed_by_feed_time(self):
        result = AngelReadOnly(lambda *a, **k: response({"fetched": [quote(exchTradeTime="01-Jan-1980 00:00:00")], "unfetched": []})).quotes(["NSE:" + TOKEN])
        with self.assertRaisesRegex(Block, "STALE"):
            fresh(result["quotes"]["NSE:" + TOKEN]["as_of"], instant(STAMP), 60, "quote")

    def test_quote_invalid_coverage_or_time_rejected(self):
        for data in ({"fetched": [quote(), quote()], "unfetched": []}, {"fetched": [quote(symbolToken="2")], "unfetched": []},
                     {"fetched": [quote(exchTradeTime=None)], "unfetched": []}, {"fetched": [], "unfetched": None},
                     {"fetched": [quote()], "unfetched": [{"exchange": "NSE", "symbolToken": TOKEN}]}):
            with self.assertRaises(Block):
                AngelReadOnly(lambda *a, **k: response(data)).quotes(["NSE:" + TOKEN])

    def test_quote_request_limits_before_network(self):
        for keys in ([], ["NSE:1"] * 2, [f"NSE:{i}" for i in range(1, 52)], ["NSE_EQ:1"], ["NSE:01"]):
            with self.assertRaises(Block):
                AngelReadOnly(lambda *a, **k: self.fail("network")).quotes(keys)

    def test_account_observed_identity_remains_unreconciled(self):
        def opener(req, **kwargs):
            return response({"clientcode": ACCOUNT} if req.full_url.endswith(PROFILE) else {} if req.full_url.endswith(FUNDS) else [])
        result = AngelReadOnly(opener).account()
        self.assertEqual(result["account_id"], ACCOUNT)
        self.assertFalse(result["reconciled"])

    def test_wrong_profile_blocks(self):
        with self.assertRaisesRegex(Block, "ACCOUNT"):
            AngelReadOnly(lambda *a, **k: response({"clientcode": "OTHER"})).account()

    def test_null_or_cross_account_rows_block(self):
        for rows in (None, [{"clientcode": "OTHER"}]):
            def opener(req, **kwargs):
                return response({"clientcode": ACCOUNT} if req.full_url.endswith(PROFILE) else {} if req.full_url.endswith(FUNDS) else rows)
            with self.assertRaisesRegex(Block, "ACCOUNT"):
                AngelReadOnly(opener).account()

    def test_margin_exact_payload_and_observed_account(self):
        def opener(req, **kwargs):
            if req.full_url.endswith(PROFILE):
                return response({"clientcode": ACCOUNT})
            self.assertTrue(req.full_url.endswith(MARGIN))
            self.assertEqual(json.loads(req.data), {"positions": [{"exchange": "NSE", "qty": 10, "price": 102,
                "productType": "INTRADAY", "token": TOKEN, "tradeType": "BUY", "orderType": "LIMIT"}]})
            return response({"totalMarginRequired": 500})
        result = AngelReadOnly(opener).margin(preview())
        self.assertEqual((result["payload_hash"], result["account_id"], result["required_inr"]), (fingerprint(preview()), ACCOUNT, 500))

    def test_margin_invalid_payload_never_calls_network(self):
        for changes in ({"ordertype": "MARKET"}, {"duration": "IOC"}, {"producttype": "CNC"}, {"squareoff": 1},
                        {"quantity": 1.5}, {"symboltoken": "bad"}, {"price": 0}, {"extra": True}):
            with self.assertRaises(Block):
                AngelReadOnly(lambda *a, **k: self.fail("network")).margin(preview(**changes))


class AngelWorkflowTests(unittest.TestCase):
    def setUp(self):
        self.config = json.loads((ROOT / "assets/config.example.json").read_text())
        self.config.update(broker="angel", account_id=ACCOUNT)
        self.card = json.loads((ROOT / "assets/charges.example.json").read_text())
        self.card.update(broker="angel", source="fictional_test_rates_not_angel_tariff")
        self.now, self.i, self.ctx, self.history, self.universe, self.current = inputs(self.config)

    def test_adapter_to_replay_size_and_ticket(self):
        i = AngelReadOnly.normalize_instruments([master()], "2026-10-30", STAMP)["instruments"][0]
        def opener(req, **kwargs):
            if req.full_url.endswith(CANDLES):
                bars = self.current["instruments"]["NSE:DEMO-EQ"]["bars"] if json.loads(req.data)["fromdate"].startswith("2026-10-30") else self.history["bars"]
                return response(candle_rows(bars))
            return response({"fetched": [quote()], "unfetched": []})
        with patch.dict(os.environ, ENV, clear=True):
            client = AngelReadOnly(opener)
            current = client.candles(TOKEN, i["symbol"], "NSE", "2026-10-30T09:15:00+05:30", STAMP)
            history = client.candles(TOKEN, i["symbol"], "NSE", self.history["bars"][0]["at"], "2026-10-30T00:00:00+05:30")
            self.ctx["quote"] = client.quotes(["NSE:" + TOKEN])["quotes"]["NSE:" + TOKEN]
        plan = plan_from_hit(orb(current["bars"], self.now), i, self.config, self.now)
        self.ctx["replay"] = replay(plan, i, history, self.config, self.card)
        order = size(plan, i, self.config, self.ctx, self.card, self.now)
        result = ticket(order, i, self.config, self.ctx, self.card, self.now)
        self.assertEqual(result["risk"]["status"], "PASS")
        self.assertEqual(result["payload_preview"]["symboltoken"], TOKEN)
        self.assertEqual(result["payload_preview"]["producttype"], "INTRADAY")
        self.assertEqual(self.ctx["replay"]["paths"], 10000)
        self.assertFalse(result["submitted"])

    def test_derivative_margin_binds_broker_account_and_payload(self):
        self.i.update(kind="option", exchange="NFO", lot_size=65, expiry="2026-11-03", underlying_type="index")
        plan = plan_from_hit(orb(self.current["instruments"]["NSE:DEMO-EQ"]["bars"], self.now), self.i, self.config, self.now)
        plan["target"] = 110
        self.ctx["quote"]["exchange"] = "NFO"
        self.ctx["restrictions"]["instruments"]["NFO:DEMO-EQ"] = self.ctx["restrictions"]["instruments"].pop("NSE:DEMO-EQ")
        self.history["exchange"] = "NFO"
        self.ctx["replay"] = replay(plan, self.i, self.history, self.config, self.card)
        order = size(plan, self.i, self.config, self.ctx, self.card, self.now)
        margin = {"source": "angel", "account_id": ACCOUNT, "as_of": STAMP, "payload_hash": fingerprint(payload(order)), "required_inr": 20000}
        self.ctx["margin"] = margin
        self.assertEqual(check_order(order, self.i, self.config, self.ctx, self.card, self.now)["status"], "PASS")
        for field, wrong in (("source", "kite"), ("account_id", "OTHER"), ("payload_hash", "wrong")):
            self.ctx["margin"] = {**margin, field: wrong}
            with self.assertRaisesRegex(Block, "MARGIN"):
                check_order(order, self.i, self.config, self.ctx, self.card, self.now)


if __name__ == "__main__":
    unittest.main()
