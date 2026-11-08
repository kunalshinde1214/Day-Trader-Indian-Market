"""Strict normalization of complete intraday broker account reads.

Unsupported products, carry positions, unmapped instruments and unknown states
block execution. These functions never submit orders or infer fills from an ACK.
"""
from core import Block, fingerprint, fresh, instant, integer, number, validate_instrument
from live_broker import identifier

TERMINAL = {"FILLED", "CANCELLED", "REJECTED"}
STATUS = {"COMPLETE": "FILLED", "TRADED": "FILLED", "CANCELLED": "CANCELLED",
          "REJECTED": "REJECTED", "EXPIRED": "CANCELLED", "OPEN": "OPEN",
          "PENDING": "OPEN", "TRIGGER PENDING": "OPEN", "PART_TRADED": "PARTIAL",
          "PARTIALLY FILLED": "PARTIAL"}


def signed(value):
    result = number(value, minimum="-1e30")
    if result != result.to_integral_value():
        raise Block("POSITION: fractional units")
    return int(result)


def state_hash(snapshot):
    return fingerprint({k: snapshot[k] for k in ("broker", "account_id", "date", "orders", "positions")})


def match_instrument(broker, row, instruments):
    from dhan_readonly import EXCHANGES
    from fyers_readonly import full_symbol
    if broker == "fyers":
        matches = [i for i in instruments if full_symbol(i["symbol"], i["exchange"]) == row["symbol"]]
        if "fyToken" in row:
            matches = [i for i in matches if i["token"] == str(row["fyToken"])]
    else:
        exchange = EXCHANGES.get(row["exchangeSegment"]) if broker == "dhan" else row["exchange"]
        token_key = {"kite": "instrument_token", "upstox": "instrument_token", "dhan": "securityId", "angel": "symboltoken"}[broker]
        matches = [i for i in instruments if i["exchange"] == exchange and i["token"] == str(row[token_key])]
        symbol_key = {"kite": "tradingsymbol", "upstox": "trading_symbol", "dhan": "tradingSymbol", "angel": "tradingsymbol"}[broker]
        if symbol_key in row:
            matches = [i for i in matches if i["symbol"] == row[symbol_key]]
    if len(matches) != 1:
        raise Block("MASTER: missing, ambiguous or mismatched broker instrument")
    return {k: matches[0][k] for k in ("exchange", "symbol", "token")}


def _product(broker, row):
    key = "producttype" if broker == "angel" else "productType" if broker in {"dhan", "fyers"} else "product"
    expected = {"kite": "MIS", "upstox": "I"}.get(broker, "INTRADAY")
    if row[key] != expected:
        raise Block("PRODUCT: execution supports a fully mapped intraday account only")
    return "MIS"


def normalize_order(broker, row, instruments):
    ident = match_instrument(broker, row, instruments)
    product = _product(broker, row)
    if broker in {"kite", "upstox"}:
        oid, qty, filled, side, status, price, trigger, typ = [row[k] for k in (
            "order_id", "quantity", "filled_quantity", "transaction_type", "status", "price", "trigger_price", "order_type")]
        variety = row["variety"]
        if variety != ("regular" if broker == "kite" else "SIMPLE"):
            raise Block("VARIETY: unsupported order variety")
    elif broker == "dhan":
        oid, qty, filled, side, status, price, trigger, typ = [row[k] for k in (
            "orderId", "quantity", "filledQty", "transactionType", "orderStatus", "price", "triggerPrice", "orderType")]
        variety = "regular"
        typ = {"STOP_LOSS": "SL", "STOP_LOSS_MARKET": "SL-M"}.get(typ, typ)
    elif broker == "angel":
        oid, qty, filled, side, status, price, trigger, typ = [row[k] for k in (
            "orderid", "quantity", "filledshares", "transactiontype", "orderstatus", "price", "triggerprice", "ordertype")]
        variety = row["variety"]
        if variety not in {"NORMAL", "STOPLOSS"}:
            raise Block("VARIETY: unsupported Angel variety")
        typ = {"STOPLOSS_LIMIT": "SL", "STOPLOSS_MARKET": "SL-M"}.get(typ, typ)
    else:
        oid, qty, filled, side, status, price, trigger, typ = [row[k] for k in (
            "id", "qty", "filledQty", "side", "status", "limitPrice", "stopPrice", "type")]
        if type(side) is not int or side not in {1, -1} or type(status) is not int or type(typ) is not int:
            raise Block("ORDER: invalid FYERS enum")
        side = "BUY" if side == 1 else "SELL"
        status = {1: "CANCELLED", 2: "COMPLETE", 4: "TRANSIT", 5: "REJECTED", 6: "PENDING", 7: "EXPIRED"}.get(status, "UNKNOWN")
        typ = {1: "LIMIT", 2: "MARKET", 3: "SL-M", 4: "SL"}.get(typ)
        variety = "regular"
    qty, filled = integer(qty), integer(filled, minimum=0)
    if filled > qty or side not in {"BUY", "SELL"} or typ not in {"LIMIT", "MARKET", "SL", "SL-M"}:
        raise Block("ORDER: invalid quantity, side or type")
    raw_status = str(status).upper()
    status = STATUS.get(raw_status, "UNKNOWN")
    if status == "FILLED" and filled != qty or status == "REJECTED" and filled:
        raise Block("ORDER: contradictory terminal fill state")
    if status in {"OPEN", "PARTIAL"}:
        if filled == qty:
            raise Block("ORDER: working status with complete quantity")
        status = "PARTIAL" if filled else "OPEN"
    return {**ident, "order_id": identifier(oid), "product": product, "side": side,
            "quantity": qty, "filled_quantity": filled, "status": status, "raw_status": raw_status,
            "price": float(number(price)), "trigger_price": float(number(trigger)), "order_type": typ, "variety": variety}


def normalize(raw, instruments, now):
    broker, account = raw["broker"], raw["account_id"]
    if raw.get("source") != broker or not isinstance(account, str) or not account:
        raise Block("ACCOUNT: broker-sourced identity required")
    fresh(raw["as_of"], now, 30, "broker account read")
    for i in instruments:
        validate_instrument(i, now)
        if i["broker"] != broker:
            raise Block("MASTER: broker mismatch")
    orders = raw["orders"]
    positions = raw["positions"]["net"] if broker == "kite" else raw["positions"]
    if not isinstance(orders, list) or not isinstance(positions, list):
        raise Block("ACCOUNT: explicit complete arrays required; null is not empty")
    for row in orders + positions:
        for key in ("dhanClientId", "clientcode", "clientId", "fy_id"):
            if key in row and row[key] != account:
                raise Block("ACCOUNT: row identity mismatch")
    result_orders = [normalize_order(broker, r, instruments) for r in orders]
    if len({r["order_id"] for r in result_orders}) != len(result_orders):
        raise Block("ORDER: duplicate ID")
    result_positions = []
    for row in positions:
        ident = match_instrument(broker, row, instruments)
        product = _product(broker, row)
        for key in ("overnight_quantity", "carryForwardBuyQty", "carryForwardSellQty", "cfbuyqty", "cfsellqty"):
            if key in row and number(row[key], minimum="-1e30") != 0:
                raise Block("CARRY: overnight position requires separate reconciliation")
        qkey = "quantity" if broker in {"kite", "upstox"} else "netqty" if broker == "angel" else "netQty"
        result_positions.append({**ident, "product": product, "quantity": signed(row[qkey])})
    if len({(r["exchange"], r["token"]) for r in result_positions}) != len(result_positions):
        raise Block("POSITION: duplicate instrument")
    funds = raw["margins"]
    if broker == "kite":
        available = funds["equity"]["available"]["live_balance"]
    elif broker == "upstox":
        available = funds["equity"]["available_margin"]
    elif broker == "dhan":
        available = funds["availabelBalance"]
    elif broker == "angel":
        available = funds["availablecash"]
    else:
        available_rows = [r for r in funds if r.get("title") == "Available Balance"]
        if len(available_rows) != 1:
            raise Block("FUNDS: unique Available Balance row required")
        available = available_rows[0]["equityAmount"]
    # Negative funds must not prevent a reducing exit; new entries check funds separately.
    result = {"broker": broker, "account_id": account, "source": broker, "as_of": raw["as_of"],
              "date": instant(raw["as_of"]).date().isoformat(), "available_inr": float(number(available, minimum="-1e30")),
              "orders": sorted(result_orders, key=lambda r: r["order_id"]),
              "positions": sorted(result_positions, key=lambda r: (r["exchange"], r["token"]))}
    result["state_hash"] = state_hash(result)
    return result


def consistent(snapshot):
    """No opening exposure: every net unit must be explained by today's order fills."""
    expected = {}
    for row in snapshot["orders"]:
        if row["status"] == "UNKNOWN":
            raise Block("ORDER_UNKNOWN: wait for an authoritative broker status")
        key = (row["exchange"], row["token"])
        expected[key] = expected.get(key, 0) + row["filled_quantity"] * (1 if row["side"] == "BUY" else -1)
    actual = {(r["exchange"], r["token"]): r["quantity"] for r in snapshot["positions"] if r["quantity"]}
    if {k: q for k, q in expected.items() if q} != actual:
        raise Block("ACCOUNT_RACE: orders and positions disagree; refresh before acting")
    return True
