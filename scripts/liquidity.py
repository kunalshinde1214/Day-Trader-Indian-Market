"""Conservative visible-depth/participation and exit stress, not a fill forecast."""
from core import Block, instant, fresh, positive, number, integer, on_tick, fingerprint, money


def evidence(plan, instrument, config, context, now):
    policy = config.get("liquidity_model")
    if policy is None:
        return None
    if not isinstance(policy, dict) or policy.get("mode") != "depth_stress_v1":
        raise Block("LIQUIDITY: supported explicit model required")
    haircut = positive(policy.get("depth_fraction"))
    participation = positive(policy.get("max_participation_fraction"))
    shock = number(policy.get("exit_shock_bps"))
    if haircut > 1 or participation > 1 or shock >= 10000:
        raise Block("LIQUIDITY: fractions must be <=1 and shock below 10000 bps")
    maximum_age = min(integer(policy.get("max_age_seconds")), config["snapshot_max_age_seconds"])
    book = context.get("liquidity_books", {}).get(instrument["exchange"]+":"+instrument["symbol"]) or context.get("liquidity")
    if not isinstance(book, dict) or book.get("source") != config["broker"] or book.get("quantity_unit") != "units":
        raise Block("LIQUIDITY: broker-sourced depth in units required")
    if any(str(book.get(k)) != str(instrument[k]) for k in ("broker", "exchange", "symbol", "token")):
        raise Block("LIQUIDITY: instrument mismatch")
    fresh(book.get("as_of"), now, maximum_age, "depth")
    fresh(book.get("volume_window_end"), now, maximum_age, "traded-volume window")
    if not instant(book["volume_window_start"]) < instant(book["volume_window_end"]) <= instant(book["as_of"]):
        raise Block("LIQUIDITY: invalid volume window")
    if (instant(book["volume_window_end"])-instant(book["volume_window_start"])).total_seconds()>300:
        raise Block("LIQUIDITY: participation window must not exceed five minutes")
    volume = integer(book.get("traded_units"), minimum=0)
    ladders = {}
    for side in ("bids", "asks"):
        rows = book.get(side)
        if not isinstance(rows, list) or not 1 <= len(rows) <= 200:
            raise Block("LIQUIDITY: bounded two-sided book required")
        prices = [positive(row["price"]) for row in rows]
        if prices != sorted(set(prices), reverse=side=="bids") or any(not on_tick(p,instrument["tick_size"]) for p in prices):
            raise Block("LIQUIDITY: sorted distinct on-tick prices required")
        ladders[side] = [(p,int(integer(r["quantity"])*haircut)) for p,r in zip(prices,rows)]
    if ladders["bids"][0][0] >= ladders["asks"][0][0]:
        raise Block("LIQUIDITY: crossed or locked depth")
    long = plan["side"] == "BUY"
    entry = ladders["asks" if long else "bids"]
    exit_rows = ladders["bids" if long else "asks"]
    entry_capacity = sum(q for p,q in entry if (p <= positive(plan["entry"]) if long else p >= positive(plan["entry"])))
    capacity = min(entry_capacity, sum(q for _,q in exit_rows), int(volume*participation))
    return {"capacity":capacity, "exit_rows":exit_rows, "exit_shock_bps":shock,
            "book_hash":fingerprint(book), "policy_hash":fingerprint(policy), "long":long}


def stress(plan, model, quantity):
    q = integer(quantity)
    if q > model["capacity"]:
        raise Block("LIQUIDITY: quantity exceeds depth/participation cap")
    remaining=q; notional=number(0)
    for price, units in model["exit_rows"]:
        used=min(remaining,units);notional+=price*used;remaining-=used
        if not remaining:break
    best=model["exit_rows"][0][0]
    impact=abs(notional/q-best)
    stop=positive(plan["stop"])
    adverse=impact+stop*model["exit_shock_bps"]/10000
    exit_price=stop-adverse if model["long"] else stop+adverse
    if exit_price<=0:raise Block("LIQUIDITY: stress exits outside positive price domain")
    return {"stress_exit_price":float(exit_price), "slippage_reserve_inr":money(adverse*q),
            "depth_capacity_units":model["capacity"], "book_hash":model["book_hash"],"policy_hash":model["policy_hash"],
            "basis":"Haircut visible depth and trailing traded-unit cap; current exit-depth impact plus configured adverse stop shock. Future liquidity is unknown; not a guaranteed fill or maximum loss."}
