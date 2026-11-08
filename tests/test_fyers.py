"""Fictional FYERS fixtures. No real credentials, accounts or network calls."""
import contextlib
import copy
import io
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch
import urllib.error
import urllib.parse

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
from fyers_readonly import FyersReadOnly, NoRedirect, PROFILE, FUNDS, POSITIONS, ORDERS, HISTORY, QUOTES, DEPTH, MARGIN, MASTER_URLS, SEGMENTS
from core import Block, check_order, fingerprint, fresh, instant, orb, payload, plan_from_hit, size, ticket
from demo import inputs
from replay import replay
from trader import main as trader_main
from broker import main as broker_main

TOKEN = "10100000999999"
KEY = "NSE:DEMO-EQ"
ACCOUNT = "DEMO123"
STAMP = "2026-10-30T09:35:00+05:30"
ENV = {"FYERS_APP_ID": "DEMOAPP-100", "FYERS_ACCESS_TOKEN": "PRIVATE_TOKEN", "FYERS_ACCOUNT_ID": ACCOUNT}


class Response(io.BytesIO):
    def __init__(self, raw, headers=None):
        super().__init__(raw)
        self.headers = headers or {}


def response(data):
    return Response(json.dumps({"s": "ok", "code": 200, **data}).encode())


def master(**changes):
    return {"fyToken": TOKEN, "symTicker": KEY, "exchange": 10, "segment": 10, "exInstType": 0, "tradeStatus": 1,
            "minLotSize": 1, "tickSize": 0.05, "expiryDate": "", "strikePrice": -1, "optType": "XX",
            "underSym": "DEMO", "lastUpdate": "2026-10-01", "asmGsmVal": "", **changes}


def quote(key=KEY, **changes):
    return {"n": key, "s": "ok", "v": {"symbol": key, "fyToken": TOKEN, "lp": 999, "tt": 1, **changes}}


def preview(**changes):
    return {"symbol": KEY, "qty": 10, "side": 1, "type": 1, "productType": "INTRADAY", "limitPrice": 102,
            "stopPrice": 0, "disclosedQty": 0, "validity": "DAY", "offlineOrder": False, "stopLoss": 0,
            "takeProfit": 0, "isSliceOrder": False, **changes}


def rows(bars):
    return [[int(instant(b["at"]).timestamp()), *[b[k] for k in ("open", "high", "low", "close", "volume")]] for b in bars]


class FyersTests(unittest.TestCase):
    def setUp(self):
        env = patch.dict(os.environ, ENV, clear=True)
        env.start()
        self.addCleanup(env.stop)

    def test_allowlist_precedes_auth(self):
        with patch.dict(os.environ, {}, clear=True):
            for method, path in (("POST", ORDERS), ("DELETE", POSITIONS), ("PATCH", ORDERS + "/sync"),
                                 ("POST", "/api/v3/validate-authcode"), ("GET", PROFILE + "?x=1"), ("POST", QUOTES)):
                with self.assertRaisesRegex(Block, "READ_ONLY"):
                    FyersReadOnly().request(method, path)

    def test_credentials_reject_missing_whitespace_and_prefixed_tokens(self):
        for changes in ({"FYERS_APP_ID": ""}, {"FYERS_ACCOUNT_ID": ""}, {"FYERS_ACCESS_TOKEN": "a\nb"}, {"FYERS_ACCESS_TOKEN": "APP:token"}):
            with patch.dict(os.environ, changes), self.assertRaisesRegex(Block, "FYERS_AUTH"):
                FyersReadOnly(lambda *a, **k: self.fail("network")).request("GET", PROFILE)

    def test_auth_header_and_observed_profile(self):
        def opener(req, **kwargs):
            self.assertEqual(req.get_header("Authorization"), "DEMOAPP-100:PRIVATE_TOKEN")
            self.assertTrue(req.full_url.endswith(PROFILE))
            self.assertEqual(kwargs["timeout"], 20)
            return response({"data": {"fy_id": ACCOUNT, "name": "NOT_PERSISTED"}})
        self.assertEqual(FyersReadOnly(opener).account_id(), ACCOUNT)

    def test_redirect_refused(self):
        with self.assertRaisesRegex(Block, "REDIRECT"):
            NoRedirect().redirect_request(None, None, 302, "", {}, "https://example.test")

    def test_transport_errors_redacted_without_retry(self):
        for error in (urllib.error.HTTPError("PRIVATE", 429, "PRIVATE", {}, None), urllib.error.URLError("PRIVATE"), TimeoutError("PRIVATE")):
            calls = []
            def opener(*a, **k):
                calls.append(1)
                raise error
            with self.assertRaises(Block) as caught:
                FyersReadOnly(opener).request("GET", PROFILE)
            self.assertNotIn("PRIVATE", str(caught.exception))
            self.assertEqual(len(calls), 1)

    def test_malformed_envelopes_and_duplicate_json_rejected(self):
        for raw in (b'null', b'PRIVATE', b'{"s":"error","message":"PRIVATE"}', b'{"s":"ok","code":-1}',
                    b'{"s":"ok","code":true}', b'{"s":"ok","data":NaN}', b'{"s":"ok","s":"error"}'):
            with self.assertRaises(Block) as caught:
                FyersReadOnly(lambda *a, **k: Response(raw)).request("GET", PROFILE)
            self.assertNotIn("PRIVATE", str(caught.exception))

    def test_size_cap_and_download_deadline(self):
        with patch("fyers_readonly.MAX_BYTES", 10), self.assertRaisesRegex(Block, "too large"):
            FyersReadOnly(lambda *a, **k: Response(b'x' * 11)).request("GET", PROFILE)
        with patch("fyers_readonly.clock.monotonic", side_effect=[0, 61]), self.assertRaisesRegex(Block, "deadline"):
            FyersReadOnly(lambda *a, **k: response({})).request("GET", PROFILE)

    def test_all_four_public_segments_no_auth_with_separate_source_dates(self):
        calls = []
        def opener(req, **kwargs):
            self.assertIsNone(req.get_header("Authorization"))
            segment = next(s for s, url in MASTER_URLS.items() if url == req.full_url)
            calls.append(segment)
            ex, seg, _ = SEGMENTS[segment]
            ticker = segment[:3] + ":DEMO" + ("-EQ" if seg == 10 else "26NOVFUT")
            r = master(fyToken=f"{ex}{seg}0000999999", symTicker=ticker, exchange=ex, segment=seg,
                       exInstType=0 if seg == 10 else 11, expiryDate=int(instant("2026-11-24T15:30:00+05:30").timestamp()))
            return Response(json.dumps({ticker: r}).encode(), {"Last-Modified": "Fri, 30 Oct 2026 02:50:00 GMT"})
        with patch.dict(os.environ, {}, clear=True):
            result = FyersReadOnly(opener).instruments()
        self.assertEqual(set(calls), set(SEGMENTS))
        self.assertEqual(len(result["instruments"]), 4)
        self.assertEqual({i["master_date"] for i in result["instruments"]}, {"2026-10-30"})
        self.assertEqual(result["instruments"][0]["tick_size"], 0.05)
        self.assertEqual(result["instruments"][0]["row_last_update"], "2026-10-01")

    def test_missing_future_source_time_and_partial_download_block(self):
        for headers in ({}, {"Last-Modified": "bad"}, {"Last-Modified": "Sat, 01 Jan 2095 00:00:00 GMT"}):
            with self.assertRaisesRegex(Block, "MASTER"):
                FyersReadOnly(lambda *a, **k: Response(b'{}', headers)).instruments()
        calls = []
        def opener(*a, **k):
            calls.append(1)
            if len(calls) > 1:
                raise TimeoutError()
            return Response(json.dumps({KEY: master()}).encode(), {"Last-Modified": "Fri, 30 Oct 2026 02:50:00 GMT"})
        with self.assertRaisesRegex(Block, "NETWORK"):
            FyersReadOnly(opener).instruments()
        self.assertEqual(len(calls), 2)

    def test_master_derivative_units_and_expiry(self):
        key = "NSE:DEMO26NOV24000CE"
        r = master(fyToken="1011261124999999", symTicker=key, segment=11, exInstType=14, minLotSize=65,
                   expiryDate=int(instant("2026-11-24T15:30:00+05:30").timestamp()), strikePrice=24000, optType="CE")
        i = FyersReadOnly.normalize_instruments({key: r}, "NSE_FO", "2026-10-30")["instruments"][0]
        self.assertEqual((i["exchange"], i["strike"], i["tick_size"], i["expiry"], i["underlying_type"]), ("NFO", 24000, 0.05, "2026-11-24", "index"))

    def test_inactive_and_non_equity_excluded(self):
        data = {KEY: master(tradeStatus=0), "NSE:INDEX": master(exInstType=10)}
        result = FyersReadOnly.normalize_instruments(data, "NSE_CM", "2026-10-30")
        self.assertEqual(result, {"instruments": [], "excluded": 2})

    def test_master_identity_and_numeric_failures(self):
        for changes in ({"exchange": 12}, {"segment": 11}, {"fyToken": "12100000999999"}, {"symTicker": "NSE:OTHER-EQ"},
                        {"tickSize": 0}, {"minLotSize": 1.5}, {"tradeStatus": 2}):
            with self.assertRaises(Block):
                FyersReadOnly.normalize_instruments({KEY: master(**changes)}, "NSE_CM", "2026-10-30")
        with self.assertRaisesRegex(Block, "duplicate"):
            FyersReadOnly.normalize_instruments({KEY: master(), "NSE:OTHER-EQ": master(symTicker="NSE:OTHER-EQ")}, "NSE_CM", "2026-10-30")

    def candles(self, data, **changes):
        args = dict(token=TOKEN, symbol="DEMO-EQ", exchange="NSE", start="2026-10-30T09:15:00+05:30", end=STAMP)
        return FyersReadOnly(lambda *a, **k: response({"candles": data})).candles(**{**args, **changes})

    def test_history_request_completed_bar_bound_and_no_continuous_contract(self):
        def opener(req, **kwargs):
            query = urllib.parse.parse_qs(urllib.parse.urlsplit(req.full_url).query)
            self.assertEqual(query["range_to"], [str(int(instant("2026-10-30T09:30:00+05:30").timestamp()))])
            self.assertEqual(query["cont_flag"], ["0"])
            self.assertEqual(query["resolution"], ["5"])
            return response({"candles": [[int(instant("2026-10-30T09:30:00+05:30").timestamp()), 100, 102, 99, 101, 10]]})
        result = FyersReadOnly(opener).candles(TOKEN, "DEMO-EQ", "NSE", "2026-10-30T03:45:00Z", STAMP)
        self.assertEqual(result["bars"][0]["at"], "2026-10-30T09:30:00+05:30")
        self.assertFalse(result["continuous"])

    def test_bad_history_range_before_network(self):
        for start in (STAMP, "2026-01-01T09:15:00+05:30", "2026-10-30T09:16:00+05:30", "2026-10-30T09:15:00"):
            with self.assertRaises(Block):
                FyersReadOnly(lambda *a, **k: self.fail("network")).candles(TOKEN, "DEMO-EQ", "NSE", start, STAMP)

    def test_daily_session_start_included_and_end_exclusive(self):
        row = [int(instant("2026-10-29T09:15:00+05:30").timestamp()), 100, 102, 99, 101, 10]
        result = self.candles([row], start="2026-10-29", end="2026-10-30", interval="day")
        self.assertEqual(len(result["bars"]), 1)

    def test_candle_malformed_stale_units_and_partial_bar_failures(self):
        row = [int(instant("2026-10-30T09:30:00+05:30").timestamp()), 100, 102, 99, 101, 10]
        for data in (None, [row, row], [[row[0] * 1000, *row[1:]]], [[row[0] + 300, *row[1:]]],
                     [[row[0] + 1, *row[1:]]], [[row[0], 100, 98, 99, 101, 10]], [[row[0], 100, 102, 99, 101, -1]], [row[:5]]):
            with self.assertRaises(Block):
                self.candles(data)
        self.assertTrue(self.candles([])["empty"])

    def quote_client(self, q=None, depth=None):
        def opener(req, **kwargs):
            if urllib.parse.urlsplit(req.full_url).path == QUOTES:
                return response({"d": [quote()] if q is None else q})
            return response({"d": {KEY: {"ltp": 102, "ltt": int(instant(STAMP).timestamp())}} if depth is None else depth})
        return FyersReadOnly(opener)

    def test_quote_price_and_time_come_from_same_depth_snapshot(self):
        result = self.quote_client().quotes([KEY])["quotes"][KEY]
        self.assertEqual((result["price"], result["as_of"], result["token"]), (102, STAMP, TOKEN))
        self.assertEqual(result["quote_tt"], 1)

    def test_partial_quotes_disclose_missing_without_depth_for_failed_symbols(self):
        result = self.quote_client(q=[quote(), {"n": "NSE:MISSING-EQ", "s": "error", "v": {"message": "PRIVATE"}}]).quotes([KEY, "NSE:MISSING-EQ", "NSE:ABSENT-EQ"])
        self.assertEqual(result["missing"], ["NSE:MISSING-EQ", "NSE:ABSENT-EQ"])
        self.assertNotIn("PRIVATE", json.dumps(result))

    def test_old_depth_trade_time_cannot_be_refreshed_by_quote_or_fetch_time(self):
        result = self.quote_client(depth={KEY: {"ltp": 102, "ltt": 315532800}}).quotes([KEY])
        with self.assertRaisesRegex(Block, "STALE"):
            fresh(result["quotes"][KEY]["as_of"], instant(STAMP), 60, "quote")

    def test_quote_missing_depth_time_and_conflicting_identity_block(self):
        for depth in ({}, {"BSE:DEMO-EQ": {"ltp": 102, "ltt": 123}}, {KEY: {"ltp": 102}}, {KEY: {"ltp": 102, "ltt": None}}):
            with self.assertRaises(Block):
                self.quote_client(depth=depth).quotes([KEY])
        for q in ([quote(), quote()], [quote(key="NSE:OTHER-EQ")], [quote(symbol="NSE:OTHER-EQ")], [quote(fyToken="12100000999999")]):
            with self.assertRaises(Block):
                self.quote_client(q=q).quotes([KEY])

    def test_special_symbols_urlencoded_and_request_limits(self):
        def opener(req, **kwargs):
            self.assertIn("M%26M", req.full_url)
            self.assertEqual(urllib.parse.parse_qs(urllib.parse.urlsplit(req.full_url).query)["symbols"], ["NSE:M&M-EQ"])
            return response({"d": []})
        FyersReadOnly(opener).quotes(["NSE:M&M-EQ"])
        for keys in ([], [KEY, KEY], [f"NSE:S{i}-EQ" for i in range(51)], ["MCX:DEMO"], ["NSE:BAD?x=1"]):
            with self.assertRaises(Block):
                FyersReadOnly(lambda *a, **k: self.fail("network")).quotes(keys)

    def account_client(self, identity=ACCOUNT, funds=None, positions=None, orders=None):
        def opener(req, **kwargs):
            data = {PROFILE: {"data": {"fy_id": identity}}, FUNDS: {"fund_limit": [{"title": "Available Balance", "equityAmount": 100000}]},
                    POSITIONS: {"netPositions": []}, ORDERS: {"orderBook": []}}
            for route, change in ((FUNDS, funds), (POSITIONS, positions), (ORDERS, orders)):
                if change is not None:
                    data[route] = change
            return response(data[urllib.parse.urlsplit(req.full_url).path])
        return FyersReadOnly(opener)

    def test_account_observed_identity_and_unreconciled_state(self):
        result = self.account_client().account()
        self.assertEqual(result["account_id"], ACCOUNT)
        self.assertFalse(result["reconciled"])

    def test_account_mismatched_or_missing_arrays_block(self):
        for client in (self.account_client(identity="OTHER"), self.account_client(funds={"fund_limit": []}),
                       self.account_client(positions={"netPositions": None}), self.account_client(orders={}),
                       self.account_client(orders={"orderBook": [{"fy_id": "OTHER"}]})):
            with self.assertRaisesRegex(Block, "ACCOUNT"):
                client.account()

    def test_margin_uses_proposed_order_total_and_full_preview_hash(self):
        def opener(req, **kwargs):
            if req.full_url.endswith(PROFILE):
                return response({"data": {"fy_id": ACCOUNT}})
            self.assertTrue(req.full_url.endswith(MARGIN))
            self.assertEqual(json.loads(req.data), {"data": [{k: preview()[k] for k in ("symbol", "qty", "side", "type", "productType", "limitPrice", "stopPrice", "stopLoss", "takeProfit")}]})
            return response({"data": {"margin_total": 500, "margin_new_order": 9000, "margin_avail": 100000}})
        result = FyersReadOnly(opener).margin(preview())
        self.assertEqual((result["required_inr"], result["payload_hash"], result["account_id"]), (500, fingerprint(preview()), ACCOUNT))

    def test_margin_bad_payload_before_network(self):
        for changes in ({"type": 2}, {"type": True}, {"side": True}, {"productType": "MARGIN"}, {"validity": "IOC"},
                        {"qty": 1.5}, {"offlineOrder": True}, {"isSliceOrder": True}, {"stopLoss": 1}, {"limitPrice": 0}, {"extra": 1}):
            with self.assertRaises(Block):
                FyersReadOnly(lambda *a, **k: self.fail("network")).margin(preview(**changes))

    def test_margin_no_fallback_to_available_balance(self):
        def opener(req, **kwargs):
            return response({"data": {"fy_id": ACCOUNT}} if req.full_url.endswith(PROFILE) else {"data": {"margin_avail": 100000}})
        with self.assertRaises(Block):
            FyersReadOnly(opener).margin(preview())


class FyersWorkflowTests(unittest.TestCase):
    def setUp(self):
        self.config = json.loads((ROOT / "assets/config.example.json").read_text())
        self.config.update(broker="fyers", account_id=ACCOUNT)
        self.card = json.loads((ROOT / "assets/charges.example.json").read_text())
        self.card.update(broker="fyers", source="fictional_test_rates_not_fyers_tariff")
        self.now, self.i, self.ctx, self.history, self.universe, self.current = inputs(self.config)

    def test_adapter_to_replay_size_risk_and_ticket(self):
        i = FyersReadOnly.normalize_instruments({KEY: master()}, "NSE_CM", "2026-10-30")["instruments"][0]
        def opener(req, **kwargs):
            url = urllib.parse.urlsplit(req.full_url)
            if url.path == HISTORY:
                start = int(urllib.parse.parse_qs(url.query)["range_from"][0])
                bars = self.current["instruments"][KEY]["bars"] if start == int(instant("2026-10-30T09:15:00+05:30").timestamp()) else self.history["bars"]
                return response({"candles": rows(bars)})
            return response({"d": [quote()]} if url.path == QUOTES else {"d": {KEY: {"ltp": 102, "ltt": int(self.now.timestamp())}}})
        with patch.dict(os.environ, ENV, clear=True):
            client = FyersReadOnly(opener)
            current = client.candles(TOKEN, i["symbol"], "NSE", "2026-10-30T09:15:00+05:30", STAMP)
            history = client.candles(TOKEN, i["symbol"], "NSE", self.history["bars"][0]["at"], "2026-10-30T00:00:00+05:30")
            self.ctx["quote"] = client.quotes([KEY])["quotes"][KEY]
        p = plan_from_hit(orb(current["bars"], self.now), i, self.config, self.now)
        self.ctx["replay"] = replay(p, i, history, self.config, self.card)
        order = size(p, i, self.config, self.ctx, self.card, self.now)
        result = ticket(order, i, self.config, self.ctx, self.card, self.now)
        self.assertEqual(result["risk"]["status"], "PASS")
        self.assertEqual(result["payload_preview"]["symbol"], KEY)
        self.assertEqual(result["payload_preview"]["type"], 1)
        self.assertEqual(self.ctx["replay"]["paths"], 10000)
        self.assertFalse(result["submitted"])

    def test_derivative_margin_binding_and_payload_identity(self):
        self.i.update(kind="option", exchange="NFO", token="1011261103999999", lot_size=65, expiry="2026-11-03", underlying_type="index")
        p = plan_from_hit(orb(self.current["instruments"][KEY]["bars"], self.now), self.i, self.config, self.now)
        p["target"] = 110
        self.ctx["quote"]["exchange"] = "NFO"
        self.ctx["restrictions"]["instruments"]["NFO:DEMO-EQ"] = self.ctx["restrictions"]["instruments"].pop(KEY)
        self.history.update(exchange="NFO", token=self.i["token"])
        self.ctx["replay"] = replay(p, self.i, self.history, self.config, self.card)
        order = size(p, self.i, self.config, self.ctx, self.card, self.now)
        margin = {"source": "fyers", "account_id": ACCOUNT, "as_of": STAMP, "payload_hash": fingerprint(payload(order)), "required_inr": 20000}
        self.ctx["margin"] = margin
        self.assertEqual(check_order(order, self.i, self.config, self.ctx, self.card, self.now)["status"], "PASS")
        for field, wrong in (("source", "kite"), ("account_id", "OTHER"), ("payload_hash", "wrong")):
            self.ctx["margin"] = {**margin, field: wrong}
            with self.assertRaisesRegex(Block, "MARGIN"):
                check_order(order, self.i, self.config, self.ctx, self.card, self.now)
        order["plan"]["token"] = TOKEN
        with self.assertRaisesRegex(Block, "IDENTITY"):
            payload(order)

    def test_init_incomplete_rates_and_cli_failure_preserves_no_output(self):
        with tempfile.TemporaryDirectory() as d, contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(trader_main(["init", "--broker", "fyers", "--workdir", d]), 0)
            card = json.loads((Path(d) / "charges.json").read_text())
            self.assertIsNone(card["brokerage_rate"])
            self.assertEqual(card["broker"], "fyers")
            out = Path(d) / "account.json"
            with patch.object(FyersReadOnly, "account", side_effect=ValueError("PRIVATE")):
                self.assertEqual(broker_main(["account", "--broker", "fyers", "--out", str(out)]), 2)
            self.assertFalse(out.exists())


if __name__ == "__main__":
    unittest.main()
