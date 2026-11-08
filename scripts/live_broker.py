"""Single-order execution transport. The read-only adapters stay read-only.

Only execution.py should call send(), after durable recording and human approval.
No redirects, retries, bulk orders, market orders, slicing or session creation.
"""
from __future__ import annotations

import json
import os
import re
import urllib.error
import urllib.parse
import urllib.request

from core import Block, BROKERS, integer, number, positive, payload


class Ambiguous(Block):
    """The request may have reached the broker. Never automatically resubmit."""


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def identifier(value):
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", value):
        raise Block("ORDER_ID: invalid broker order identity")
    return value


def wire_payload(action):
    """Build from canonical values; callers cannot inject arbitrary API parameters."""
    broker = action["broker"]
    if broker not in BROKERS:
        raise Block("BROKER: unsupported execution adapter")
    if action["kind"] == "cancel":
        oid = identifier(action["order_id"])
        return {"orderid": oid, "variety": action["variety"]} if broker == "angel" else {"order_id": oid}
    if action["kind"] not in {"entry", "exit", "stop"} or action["side"] not in {"BUY", "SELL"}:
        raise Block("ACTION: unsupported execution action")
    i = action["instrument"]
    if i["broker"] != broker:
        raise Block("BROKER: instrument mismatch")
    p = {**i, "account_id": action["account_id"], "product": "MIS", "side": action["side"], "entry": action["price"]}
    result = payload({"plan": p, "quantity": integer(action["quantity"])})
    if action["kind"] == "stop":
        trigger, limit = positive(action["trigger_price"]), positive(action["price"])
        if not (limit < trigger if action["side"] == "SELL" else limit > trigger):
            raise Block("STOP: stop-limit needs a strictly worse limit than its trigger")
        if broker in {"kite", "upstox"}:
            result.update(order_type="SL", trigger_price=float(trigger))
        elif broker == "dhan":
            result.update(orderType="STOP_LOSS", triggerPrice=float(trigger))
        elif broker == "angel":
            result.update(variety="STOPLOSS", ordertype="STOPLOSS_LIMIT", triggerprice=float(trigger))
        else:
            result.update(type=4, stopPrice=float(trigger))
    return result


def _env(name):
    value = os.environ.get(name)
    if not value or any(c.isspace() for c in value):
        raise Block("AUTH: configure valid broker credentials locally; never paste them in chat")
    return value


class LiveBroker:
    def __init__(self, broker, readonly):
        if broker not in BROKERS:
            raise Block("BROKER: unsupported execution adapter")
        self.broker, self.readonly = broker, readonly

    def build(self, action):
        """Local validation and credential loading happen before SUBMITTING is recorded."""
        if action["broker"] != self.broker:
            raise Block("BROKER: transport mismatch")
        b, cancel = self.broker, action["kind"] == "cancel"
        body = wire_payload(action)
        headers = {"Accept": "application/json", "Content-Type": "application/json"}
        method = "DELETE" if cancel else "POST"
        if b == "kite":
            headers.update({"X-Kite-Version": "3", "Authorization": "token " + _env("KITE_API_KEY") + ":" + _env("KITE_ACCESS_TOKEN")})
            headers["Content-Type"] = "application/x-www-form-urlencoded"
            url = "https://api.kite.trade/orders/regular"
            if cancel:
                url += "/" + body["order_id"]
            data = None if cancel else urllib.parse.urlencode(body).encode()
        else:
            if b == "upstox":
                headers["Authorization"] = "Bearer " + _env("UPSTOX_ACCESS_TOKEN")
                url = "https://api-hft.upstox.com/v3/order/" + ("cancel?" + urllib.parse.urlencode(body) if cancel else "place")
                if cancel:
                    body = None
            elif b == "dhan":
                client, token = self.readonly.credentials()
                if client != action["account_id"]:
                    raise Block("ACCOUNT: Dhan credential identity mismatch")
                headers.update({"access-token": token, "client-id": client})
                url = "https://api.dhan.co/v2/orders" + ("/" + body["order_id"] if cancel else "")
                if cancel:
                    body = None
            elif b == "angel":
                s = self.readonly.credentials()
                if s["ANGEL_CLIENT_CODE"] != action["account_id"]:
                    raise Block("ACCOUNT: Angel credential identity mismatch")
                headers.update({"Authorization": "Bearer " + s["ANGEL_ACCESS_TOKEN"], "X-PrivateKey": s["ANGEL_API_KEY"],
                    "X-ClientLocalIP": s["ANGEL_LOCAL_IP"], "X-ClientPublicIP": s["ANGEL_PUBLIC_IP"],
                    "X-MACAddress": s["ANGEL_MAC_ADDRESS"], "X-UserType": "USER", "X-SourceID": "WEB"})
                method = "POST"
                url = "https://apiconnect.angelone.in/rest/secure/angelbroking/order/v1/" + ("cancelOrder" if cancel else "placeOrder")
            else:
                s = self.readonly.credentials()
                if s["FYERS_ACCOUNT_ID"] != action["account_id"]:
                    raise Block("ACCOUNT: FYERS credential identity mismatch")
                headers["Authorization"] = s["FYERS_APP_ID"] + ":" + s["FYERS_ACCESS_TOKEN"]
                url = "https://api-t1.fyers.in/api/v3/orders/sync"
                if cancel:
                    body = {"id": body["order_id"]}
            data = None if body is None else json.dumps(body, allow_nan=False, separators=(",", ":")).encode()
        return urllib.request.Request(url, data=data, headers=headers, method=method)

    def send(self, request, action):
        """One network attempt. All post-send failures are ambiguous, even HTTP errors."""
        try:
            with urllib.request.build_opener(NoRedirect()).open(request, timeout=15) as response:
                raw = response.read(1024 * 1024 + 1)
            if len(raw) > 1024 * 1024:
                raise ValueError("response too large")
            result = json.loads(raw, parse_constant=lambda _: (_ for _ in ()).throw(ValueError()))
            return self.acknowledgement(result, action)
        except (OSError, ValueError, TypeError, KeyError, IndexError, urllib.error.URLError):
            raise Ambiguous("EXECUTION_UNKNOWN: reconcile broker state; request must not be retried") from None

    def acknowledgement(self, result, action):
        b = self.broker
        if not isinstance(result, dict):
            raise ValueError("malformed acknowledgement")
        if b in {"kite", "upstox"}:
            if result.get("status") != "success":
                raise ValueError("unsuccessful response")
            data = result["data"]
            if b == "upstox" and action["kind"] != "cancel":
                ids = data["order_ids"]
                if not isinstance(ids, list) or len(ids) != 1:
                    raise ValueError("unexpected split order")
                oid = ids[0]
            else:
                oid = data["order_id"]
        elif b == "dhan":
            if any(k in result for k in ("errorCode", "errorType", "errorMessage")):
                raise ValueError("unsuccessful response")
            oid = result["orderId"]
        elif b == "angel":
            if result.get("status") is not True or result.get("errorcode") not in (None, ""):
                raise ValueError("unsuccessful response")
            oid = result["data"]["orderid"]
        else:
            expected = 1103 if action["kind"] == "cancel" else 1101
            if result.get("s") != "ok" or result.get("code") != expected:
                raise ValueError("unacknowledged FYERS response")
            oid = result["id"]
        oid = identifier(oid)
        if action["kind"] == "cancel" and oid != action["order_id"]:
            raise ValueError("cancel identity mismatch")
        return {"order_id": oid, "state": "ACKNOWLEDGED", "filled": False}
