"""Offline India paper-analysis core. No network and no order submission.

Adapted pipeline, time-slot bootstrap and sequential risk reservations from
Jake Schincariol's MIT day-trader-skill; see LICENSE and references/requirements.md.
"""
from __future__ import annotations

import copy
import hashlib
import json
from datetime import datetime, timedelta, timezone, time, date
from decimal import Decimal, InvalidOperation, ROUND_FLOOR, ROUND_CEILING, ROUND_HALF_UP

IST = timezone(timedelta(hours=5, minutes=30))
D = Decimal
BROKERS = {"kite", "upstox", "dhan", "angel", "fyers"}
SETUPS = {"orb", "level", "vwap_reclaim", "gap_and_go", "breakout_20d", "bull_flag", "oversold_bounce"}


class Block(ValueError):
    pass


def number(value, name="number", minimum=0):
    try:
        if isinstance(value, bool):
            raise InvalidOperation
        v = D(str(value))
    except (InvalidOperation, TypeError, ValueError):
        raise Block(f"INVALID: {name} must be numeric") from None
    if not v.is_finite() or v < D(str(minimum)):
        raise Block(f"INVALID: {name} must be finite and >= {minimum}")
    return v


def positive(value, name="number"):
    v = number(value, name)
    if v <= 0:
        raise Block(f"INVALID: {name} must be positive")
    return v


def integer(value, name="quantity", minimum=1):
    v = number(value, name, minimum)
    if v != v.to_integral_value():
        raise Block(f"INVALID: {name} must be a whole number")
    return int(v)


def money(value):
    return float(D(str(value)).quantize(D("0.01"), rounding=ROUND_HALF_UP))


def instant(text=None):
    dt = datetime.now(IST) if text is None else datetime.fromisoformat(str(text).replace("Z", "+00:00"))
    if dt.tzinfo is None:
        raise Block("TIMEZONE: timestamps require an explicit offset")
    return dt.astimezone(IST)


def fresh(text, now, seconds, label):
    if not isinstance(text, str) or not text.strip():
        raise Block(f"STALE: {label}; explicit source timestamp required")
    age = (now - instant(text)).total_seconds()
    if age < 0 or age > seconds:
        raise Block(f"STALE: {label}; refresh it (age {age:g}s)")


def fingerprint(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()).hexdigest()


def tick(value, step, up=False):
    step = positive(step, "tick_size")
    return float((positive(value) / step).to_integral_value(rounding=ROUND_CEILING if up else ROUND_FLOOR) * step)


def on_tick(value, step):
    return positive(value) % positive(step, "tick_size") == 0


def validate_instrument(i, now):
    if i.get("broker") not in BROKERS:
        raise Block("INSTRUMENT: supported broker identity required; refresh legacy master")
    if not i.get("symbol") or not i.get("token"):
        raise Block("INSTRUMENT: symbol and token required")
    if i["kind"] not in {"equity", "future", "option"}:
        raise Block("INSTRUMENT: unsupported kind")
    allowed = {"equity": {"NSE", "BSE"}, "future": {"NFO", "BFO"}, "option": {"NFO", "BFO"}}
    if i["exchange"] not in allowed[i["kind"]]:
        raise Block("INSTRUMENT: exchange does not match kind")
    positive(i["tick_size"], "tick_size")
    integer(i["lot_size"], "lot_size")
    if i["master_date"] != now.date().isoformat():
        raise Block("MASTER: same-day instrument master required")
    if i["kind"] != "equity":
        if date.fromisoformat(i["expiry"]) < now.date():
            raise Block("EXPIRY: contract expired")
        if i.get("underlying_type") not in {"stock", "index"}:
            raise Block("INSTRUMENT: verify derivative underlying_type as stock or index")


def validate_config(c):
    if c["paper"] is not True:
        raise Block("LIVE_DISABLED: this release cannot trade live")
    if c["currency"] != "INR" or c["broker"] not in BROKERS:
        raise Block("CONFIG: requires INR and an implemented broker (kite/upstox/dhan/angel/fyers)")
    if not isinstance(c.get("account_id"), str) or not c["account_id"].strip():
        raise Block("CONFIG: account_id required; use the broker profile ID, never a credential")
    if not isinstance(c["allow_short"], bool):
        raise Block("CONFIG: allow_short must be a boolean")
    if not isinstance(c.get("monitor_required",False),bool):
        raise Block("CONFIG: monitor_required must be a boolean")
    if not isinstance(c.get("require_instrument_session",False),bool):
        raise Block("CONFIG: require_instrument_session must be a boolean")
    if c.get("setup", "orb") not in SETUPS:
        raise Block("SETUP: unsupported setup; choose one of " + ", ".join(sorted(SETUPS)))
    if positive(c.get("gap_min_pct", 0.7), "gap_min_pct") > 100:
        raise Block("CONFIG: gap_min_pct exceeds 100")
    for key in ("budget", "max_loss_per_trade_inr", "max_daily_loss_inr", "max_order_inr", "min_order_inr", "min_reward_risk", "plan_max_age_seconds", "snapshot_max_age_seconds"):
        positive(c[key], key)
    for key in ("cash_buffer_pct", "max_position_pct"):
        if number(c[key], key) > 100:
            raise Block(f"CONFIG: {key} exceeds 100")
    if number(c["max_cost_fraction"]) > 1:
        raise Block("CONFIG: max_cost_fraction exceeds 1")
    for key in ("max_trades_per_day", "max_orders_per_day", "replay_paths", "minimum_history_sessions"):
        integer(c[key], key)
    clocks = [time.fromisoformat(c[k]) for k in ("no_entries_before", "no_entries_after", "flat_by")]
    from replay_sampling import policy
    policy(c)
    from entry_policy import interval
    interval(c)
    if not clocks[0] <= clocks[1] < clocks[2]:
        raise Block("CONFIG: inconsistent entry and flat-by times")


def validate_rates(instrument, card, broker=None):
    selected = broker or instrument.get("broker")
    if selected not in BROKERS or instrument.get("broker") != selected or card.get("broker") != selected or not card.get("source"):
        raise Block("RATES: sourced rate card must match instrument and selected broker")


def charges(entry, exit_price, qty, side, instrument, card, day):
    """Unrounded components accumulate before two-decimal presentation."""
    if not card["effective_from"] <= day <= card["review_after"]:
        raise Block("RATES: rate card outside validity/review dates")
    validate_rates(instrument, card)
    qty = integer(qty)
    if side not in {"BUY", "SELL"}:
        raise Block("SIDE: use BUY or SELL")
    entry, exit_price = positive(entry), positive(exit_price)
    buy, sell = (entry * qty, exit_price * qty) if side == "BUY" else (exit_price * qty, entry * qty)
    rates = card["segments"].get(instrument["exchange"] + ":" + instrument["kind"])
    if not isinstance(rates,dict):
        raise Block("RATES: selected segment has no verified rate entry")
    for v in rates.values():
        number(v, "rate")
    turnover = buy + sell
    rate=number(rates.get("brokerage_rate",card["brokerage_rate"]))
    cap=number(rates.get("brokerage_cap",card["brokerage_cap"]))
    floor=number(rates.get("brokerage_min",0))
    if floor>cap: raise Block("RATES: brokerage minimum exceeds cap")
    brokerage = (2*number(rates["brokerage_fixed"]) if "brokerage_fixed" in rates else
                 2*number(card["option_brokerage"]) if instrument["kind"] == "option" else
                 sum(max(floor,min(x*rate,cap)) for x in (buy,sell)))
    result = {"brokerage": brokerage, "stt": sell * number(rates["stt_sell"]),
              "exchange": turnover * number(rates["exchange_rate"]),
              "sebi": turnover * number(card["sebi_rate"]), "stamp": buy * number(rates["stamp_buy"])}
    for key in ("ipft","clearing"):
        if key+"_rate" in rates: result[key]=turnover*number(rates[key+"_rate"])
    result["gst"] = (result["brokerage"] + result["exchange"] + result["sebi"] + result.get("ipft",0) + result.get("clearing",0)) * number(card["gst_rate"])
    result["total"] = sum(result.values())
    return {k: money(v) for k, v in result.items()}


def completed_bars(rows, now, day=None, interval=5):
    """Bar timestamps are starts; reject duplicate or malformed OHLC, exclude forming bars."""
    if type(interval) is not int or interval not in (1, 5):
        raise Block("BARS: interval must be 1 or 5")
    out, seen = [], set()
    for row in rows:
        ts = instant(row["at"])
        if ts in seen:
            raise Block("BARS: duplicate timestamp")
        seen.add(ts)
        o, h, l, c = [positive(row[k], k) for k in ("open", "high", "low", "close")]
        number(row["volume"], "volume")
        if not l <= min(o, c) <= max(o, c) <= h:
            raise Block("BARS: inconsistent OHLC")
        if ts.second or ts.microsecond or ts.minute % interval:
            raise Block("BARS: timestamps must start on the configured minute grid")
        if ts + timedelta(minutes=interval) > now:
            continue
        if day and ts.date().isoformat() != day:
            continue
        if time(9, 15) <= ts.time() < time(15, 40):
            out.append({**row, "at": ts.isoformat()})
    return sorted(out, key=lambda x: x["at"])


def session_bars(rows, now, interval=5):
    """Require the complete regular-session prefix, including the latest closed bar."""
    bars = completed_bars(rows, now, now.date().isoformat(), interval)
    start = now.replace(hour=9, minute=15, second=0, microsecond=0)
    close = start.replace(hour=15, minute=30)
    bars = [b for b in bars if instant(b["at"]) < close]
    expected = max(0, int((min(now, close) - start).total_seconds() // (interval*60)))
    if len(bars) != expected:
        raise Block("BARS: missing opening, intervening or latest completed bar")
    for k, b in enumerate(bars):
        if instant(b["at"]) != start + timedelta(minutes=interval*k):
            raise Block("BARS: missing opening or intervening bar")
    return bars


def orb(rows, now, interval=5):
    bars = session_bars(rows, now, interval)
    opening = 15//interval
    if len(bars) <= opening:
        return {"state": "waiting", "setup": "orb", "reason": "Need opening range plus a completed trigger bar"}
    high = max(float(b["high"]) for b in bars[:opening])
    low = min(float(b["low"]) for b in bars[:opening])
    for k, b in enumerate(bars[opening:], opening):
        if float(b["close"]) > high or float(b["close"]) < low:
            level = high if float(b["close"]) > high else low
            return {"state": "triggered", "setup": "orb", "side": "BUY" if float(b["close"]) > high else "SELL",
                    "rule_version": 1, "reference_price": level, "interval_minutes":interval,
                    "percent_over_level": money((number(b["close"])/number(level)-1)*100),
                    "entry": b["close"], "range_high": high, "range_low": low,
                    "trigger_at": (instant(b["at"]) + timedelta(minutes=interval)).isoformat(),
                    "turnover_estimate_inr": money(sum(number(x["close"])*number(x["volume"]) for x in bars[:k+1])),
                    "label": "matched a rule"}
    return {"state": "waiting", "setup": "orb", "range_high": high, "range_low": low}


def vwap_reclaim(rows, now, entry_start="09:30", entry_end="14:30"):
    """First eligible strict close crossover of session HLC3-volume VWAP; bar approximation."""
    bars = session_bars(rows, now)
    first = max(time(9, 30), time.fromisoformat(entry_start))
    last = time.fromisoformat(entry_end)
    weighted, volume, turnover = D(0), D(0), D(0)
    previous_close = previous_vwap = None
    high, low = None, None
    for b in bars:
        # Zero-volume bars cannot establish a price/volume crossover.
        v = positive(b["volume"], "VWAP bar volume")
        c, h, l = [positive(b[k]) for k in ("close", "high", "low")]
        weighted += (h+l+c)/3*v
        volume += v
        turnover += c*v
        vwap = weighted/volume
        high, low = max(high, h) if high is not None else h, min(low, l) if low is not None else l
        at = instant(b["at"]) + timedelta(minutes=5)
        side = None
        if previous_vwap is not None and first <= at.time() <= last:
            if previous_close < previous_vwap and c > vwap:
                side = "BUY"
            elif previous_close > previous_vwap and c < vwap:
                side = "SELL"
        if side:
            return {"state": "triggered", "setup": "vwap_reclaim", "rule_version": 1, "side": side,
                    "entry": float(c), "vwap": float(vwap), "reference_price": float(vwap),
                    "range_high": float(high), "range_low": float(low), "trigger_at": at.isoformat(),
                    "percent_over_level": money((c/vwap-1)*100), "turnover_estimate_inr": money(turnover),
                    "vwap_basis": "session cumulative HLC3 times volume / volume; five-minute approximation",
                    "label": "matched a rule"}
        previous_close, previous_vwap = c, vwap
    return {"state": "waiting", "setup": "vwap_reclaim", "vwap": float(weighted/volume) if volume else None,
            "reason": "No eligible completed-bar VWAP crossover"}


def validate_gap_reference(ref, instrument, session_date):
    """Validate supplied calendar/close evidence; never infer the previous session."""
    if not isinstance(ref, dict) or not isinstance(instrument, dict):
        raise Block("GAP_REFERENCE: sourced previous-session close and exact instrument required")
    if any(ref.get(k) != instrument.get(k) or ref.get(k) is None for k in ("broker", "symbol", "exchange", "token")):
        raise Block("GAP_REFERENCE: prior close belongs to a different instrument/broker")
    if any(not isinstance(ref.get(k), str) or not ref[k].strip() for k in ("source", "calendar_source")):
        raise Block("GAP_REFERENCE: price and calendar sources required")
    if ref.get("price_basis") != "unadjusted_session_close" or ref.get("corporate_actions_clear") is not True:
        raise Block("GAP_REFERENCE: comparable unadjusted session close and corporate-action clearance required")
    try:
        day = date.fromisoformat(session_date)
        previous = date.fromisoformat(ref["previous_session"])
        if ref.get("for_session") != session_date or previous >= day:
            raise ValueError("session relationship")
        value = ref.get("available_at")
        if not isinstance(value, str) or not value.strip():
            raise ValueError("missing source time")
        available = instant(value)
        if not datetime.combine(previous, time(15, 30), IST) <= available < datetime.combine(day, time(9, 15), IST):
            raise ValueError("prior close not known before open")
    except (KeyError, ValueError, TypeError):
        raise Block("GAP_REFERENCE: dated prior session and pre-open availability required") from None
    return positive(ref.get("close"), "prior-session close")


def gap_and_go(rows, now, config, instrument, prior_close):
    previous = validate_gap_reference(prior_close, instrument, now.date().isoformat())
    bars = session_bars(rows, now)
    if len(bars) < 3:
        return {"state": "waiting", "setup": "gap_and_go", "reason": "Need all three completed opening bars"}
    opening = bars[:3]
    for b in opening:
        positive(b["volume"], "gap opening-bar volume")
    opened = positive(opening[0]["open"])
    gap = (opened-previous)/previous*100
    minimum = positive(config.get("gap_min_pct", 0.7), "gap_min_pct")
    high = max(positive(b["high"]) for b in opening)
    low = min(positive(b["low"]) for b in opening)
    entry = positive(opening[-1]["close"])
    trigger = instant(opening[-1]["at"]) + timedelta(minutes=5)
    waiting = {"state": "waiting", "setup": "gap_and_go", "gap_pct": float(gap)}
    if abs(gap) < minimum:
        return {**waiting, "reason": "Opening gap below configured threshold"}
    if not time.fromisoformat(config["no_entries_before"]) <= trigger.time() <= time.fromisoformat(config["no_entries_after"]):
        return {**waiting, "reason": "The 09:30 trigger is outside the configured entry window"}
    long = gap > 0
    if not (low > previous and entry >= opened if long else high < previous and entry <= opened):
        return {**waiting, "reason": "Opening gap filled/touched or continuation close failed"}
    return {"state": "triggered", "setup": "gap_and_go", "rule_version": 1, "side": "BUY" if long else "SELL",
            "entry": float(entry), "range_high": float(high), "range_low": float(low),
            "opening_price": float(opened), "gap_pct": float(gap), "gap_min_pct": float(minimum),
            "reference_price": float(previous), "percent_over_level": money((entry/previous-1)*100),
            "trigger_at": trigger.isoformat(), "gap_reference": copy.deepcopy(prior_close),
            "turnover_estimate_inr": money(sum(number(b["close"])*number(b["volume"]) for b in opening)),
            "label": "matched a rule"}


def setup_hit(rows, now, config, instrument=None, prior_close=None, daily=None):
    from entry_policy import interval, level_hit
    minutes = interval(config)
    setup = config.get("setup", "orb")
    if setup == "orb":
        return orb(rows, now, minutes)
    if setup == "level":
        return level_hit(rows, now, config, instrument)
    if setup == "vwap_reclaim":
        return vwap_reclaim(rows, now, config["no_entries_before"], config["no_entries_after"])
    if setup == "gap_and_go":
        return gap_and_go(rows, now, config, instrument, prior_close)
    if setup in {"breakout_20d", "bull_flag", "oversold_bounce"}:
        from patterns import daily_setup
        return daily_setup(rows, now, config, instrument, daily)
    raise Block("SETUP: unsupported scanner rule")


def plan_from_hit(hit, instrument, config, now, rows=None):
    validate_config(config)
    validate_instrument(instrument, now)
    if instrument["broker"] != config["broker"]:
        raise Block("BROKER: instrument belongs to a different broker")
    if hit["state"] != "triggered":
        raise Block("ENTRY: no completed trigger")
    from entry_policy import check as check_entry, interval, level_reference
    check_entry(hit, config)
    setup = hit.get("setup")
    version = hit.get("rule_version", 1)
    if setup not in SETUPS or setup != config.get("setup", "orb") or type(version) is not int or version != 1:
        raise Block("SETUP: hit does not match configured supported rule")
    if hit["side"] not in {"BUY", "SELL"}:
        raise Block("SIDE: use BUY or SELL")
    fresh(hit["trigger_at"], now, config["plan_max_age_seconds"], "trigger")
    long = hit["side"] == "BUY"
    step = positive(instrument["tick_size"])
    entry = tick(hit["entry"], step, up=long)
    if setup == "orb":
        reference = (number(hit["range_high"]) + number(hit["range_low"])) / 2
    elif setup == "vwap_reclaim":
        reference = positive(hit["vwap"])
    elif setup == "level":
        level_reference(hit.get("level_entry"), instrument, instant(hit["trigger_at"]))
        reference = positive(hit["stop_reference"])
    elif setup == "gap_and_go":
        validate_gap_reference(hit.get("gap_reference"), instrument, instant(hit["trigger_at"]).date().isoformat())
        if number(hit["gap_min_pct"]) != number(config.get("gap_min_pct", 0.7)):
            raise Block("SETUP: gap threshold changed; regenerate evidence")
        reference = positive(hit["range_low"] if long else hit["range_high"])
    else:
        from patterns import daily_reference
        daily_reference(hit.get("daily_reference"), instrument, instant(hit["trigger_at"]).date().isoformat())
        if not long:
            raise Block("SETUP: daily-context rules are long-only")
        reference = positive(hit["stop_reference"])
    if not (positive(hit["entry"]) > reference if long else positive(hit["entry"]) < reference):
        raise Block("STOP_REFERENCE: structural stop must be on the loss side of entry")
    buffer = integer(config["stop_buffer_ticks"], "stop_buffer_ticks", 0) * step
    stop = tick(reference - buffer if long else reference + buffer, step, up=not long)
    risk = abs(D(str(entry)) - D(str(stop)))
    target = tick(D(str(entry)) + (1 if long else -1) * risk * positive(config["target_r"]), step, up=long)
    plan = {"broker": config["broker"], "account_id": config["account_id"],
            "symbol": instrument["symbol"], "exchange": instrument["exchange"], "token": instrument["token"],
            "side": hit["side"], "entry": entry, "stop": stop, "target": target,
            "setup": setup, "rule_version": 1, "as_of": hit["trigger_at"], "flat_by": config["flat_by"], "product": "MIS"}
    plan["interval_minutes"] = interval(config)
    if setup == "level":
        plan.update(level_entry=copy.deepcopy(hit["level_entry"]), stop_reference=float(reference), stop_basis="observed intraday extreme plus tick buffer")
    if setup == "vwap_reclaim":
        plan.update(stop_basis="trigger-bar session VWAP plus configured tick buffer", stop_reference=float(reference))
    elif setup == "gap_and_go":
        plan.update(stop_basis="opening-range extreme plus configured tick buffer", stop_reference=float(reference),
                    gap_reference=copy.deepcopy(hit["gap_reference"]), gap_min_pct=hit["gap_min_pct"],
                    opening_price=hit["opening_price"], gap_pct=hit["gap_pct"])
    elif setup in {"breakout_20d", "bull_flag", "oversold_bounce"}:
        plan.update(stop_basis="intraday swing low through trigger plus tick buffer", stop_reference=float(reference),
                    daily_reference=copy.deepcopy(hit["daily_reference"]), diagnostics=hit["diagnostics"])
    if "exit_stop" in config or "exit_target" in config:
        from analysis_rules import exits, exit_policy
        rules = exit_policy(config)
        selected = exits(hit, step, rows=rows, reference=reference, buffer_ticks=config["stop_buffer_ticks"], **rules)
        plan.update({k:v for k,v in selected.items() if k not in {"risk_per_unit", "reward_risk"}})
        plan["stop_basis"] = "explicit exit_policy; ATR uses completed bars through entry only"
    plan_math(plan, instrument)
    return plan


def plan_math(p, i):
    from entry_policy import interval, level_reference
    interval(p)
    if p.get("setup") == "level":
        level, available = level_reference(p.get("level_entry"), i, instant(p["as_of"]))
        if p["side"] != p["level_entry"]["side"]:
            raise Block("LEVEL: plan side differs from level evidence")
        if available > instant(p["as_of"])-timedelta(minutes=interval(p)) or not (positive(p["entry"])>level if p["side"]=="BUY" else positive(p["entry"])<level):
            raise Block("LEVEL: level must precede the trigger bar and entry must exceed it in the selected direction")
    version = p.get("rule_version", 1 if p.get("setup") == "orb" else None)
    if p.get("setup") not in SETUPS or type(version) is not int or version != 1:
        raise Block("SETUP: unsupported plan setup/version; regenerate the plan")
    if p.get("broker") not in BROKERS or p.get("broker") != i.get("broker"):
        raise Block("BROKER: plan/instrument mismatch; regenerate legacy plans")
    if p["symbol"] != i["symbol"] or p["exchange"] != i["exchange"] or str(p["token"]) != str(i["token"]):
        raise Block("IDENTITY: plan/instrument mismatch")
    if p["side"] not in {"BUY", "SELL"} or p["product"] != "MIS":
        raise Block("PLAN: only BUY/SELL MIS supported")
    if p["setup"] == "gap_and_go":
        trigger = instant(p["as_of"])
        previous = validate_gap_reference(p.get("gap_reference"), i, trigger.date().isoformat())
        opened = positive(p.get("opening_price"))
        gap = (opened-previous)/previous*100
        if trigger.time() != time(9, 30) or abs(gap) < positive(p.get("gap_min_pct")) or (gap > 0) != (p["side"] == "BUY"):
            raise Block("SETUP: invalid gap direction, threshold or trigger time")
    if p["setup"] in {"breakout_20d", "bull_flag", "oversold_bounce"}:
        from patterns import daily_reference
        daily_reference(p.get("daily_reference"), i, instant(p["as_of"]).date().isoformat())
        if p["side"] != "BUY":
            raise Block("SETUP: daily-context rules are long-only")
    e, s, t = [positive(p[k], k) for k in ("entry", "stop", "target")]
    if not all(on_tick(x, i["tick_size"]) for x in (e, s, t)):
        raise Block("TICK: plan prices must align to instrument tick")
    if not (s < e < t if p["side"] == "BUY" else t < e < s):
        raise Block("STOP_TARGET: prices on wrong side")
    return e, abs(e-s), abs(t-e)


def entry_context(p, i, c, ctx, now):
    validate_config(c)
    from analysis_rules import check_policy
    check_policy(p, c)
    validate_instrument(i, now)
    plan_math(p, i)
    if p["setup"] != c.get("setup", "orb"):
        raise Block("SETUP: plan does not match configuration; regenerate plan and replay")
    if ctx.get("local_halt") is True:
        raise Block("LOCAL_HALT: entries paused by user")
    if c.get("monitor_required", False):
        monitor=ctx.get("monitor", {})
        fresh(monitor.get("as_of"),now,c["snapshot_max_age_seconds"],"monitor heartbeat")
        if (monitor.get("state")!="running" or monitor.get("entry_allowed") is not True or
                monitor.get("broker")!=c["broker"] or monitor.get("account_id")!=c["account_id"] or
                monitor.get("plan_hash")!=fingerprint(p)):
            raise Block("MONITOR: running monitor for this exact account and plan required")
    if p["setup"] == "gap_and_go" and number(p["gap_min_pct"]) != number(c.get("gap_min_pct", 0.7)):
        raise Block("SETUP: gap threshold changed; regenerate plan and replay")
    if p["broker"] != c["broker"] or p.get("account_id") != c["account_id"]:
        raise Block("ACCOUNT: plan belongs to a different broker/account")
    if ctx["account"].get("broker") != c["broker"] or ctx["account"].get("account_id") != c["account_id"]:
        raise Block("ACCOUNT: snapshot belongs to a different broker/account")
    fresh(p["as_of"], now, c["plan_max_age_seconds"], "plan")
    replay_result = ctx.get("replays", {}).get(fingerprint(p)) or ctx["replay"]
    from replay_sampling import policy
    if replay_result.get("sampling") != policy(c) or replay_result.get("seed") != c["replay_seed"]:
        raise Block("REPLAY: sampling or seed changed; regenerate replay")
    if replay_result["plan_hash"] != fingerprint(p) or replay_result["paths"] < c["replay_paths"] or replay_result["history_sessions"] < c["minimum_history_sessions"]:
        raise Block("REPLAY: run the configured replay for this exact plan first")
    quote = ctx.get("quotes", {}).get(i["exchange"]+":"+i["symbol"]) or ctx["quote"]
    fresh(quote["as_of"], now, c["snapshot_max_age_seconds"], "quote")
    if quote["symbol"] != i["symbol"] or quote["exchange"] != i["exchange"] or not quote.get("source"):
        raise Block("QUOTE: sourced quote for the exact instrument required")
    price = positive(quote["price"])
    if not (number(p["stop"]) < price < number(p["target"]) if p["side"] == "BUY" else number(p["target"]) < price < number(p["stop"])):
        raise Block("EXPIRED: price has reached or passed the planned stop/target")
    if instant(p["as_of"]).date() != now.date():
        raise Block("PLAN: plan is not from today")
    for key in ("session", "restrictions", "event", "account"):
        snap = ctx[key]
        if not snap.get("source"):
            raise Block(f"SOURCE: {key} missing provenance")
        fresh(snap["as_of"], now, c["snapshot_max_age_seconds"], key)
    session, restrictions, event, account = [ctx[k] for k in ("session", "restrictions", "event", "account")]
    if c.get("require_instrument_session") is True or "instrument_session" in ctx:
        from session_policy import validate
        validate(ctx.get("instrument_session"), i, c, now)
    if session["date"] != now.date().isoformat() or session["is_open_day"] is not True or session["halted"] is not False:
        raise Block("CLOCK: holiday, closed session, halt or unknown status")
    if now.weekday() >= 5 and session.get("special_session") is not True:
        raise Block("CLOCK: weekend without verified special session")
    clock = now.time()
    if not time.fromisoformat(session["open"]) <= clock < time.fromisoformat(session["close"]):
        raise Block("CLOCK: outside continuous session")
    if not time.fromisoformat(c["no_entries_before"]) <= clock <= time.fromisoformat(c["no_entries_after"]):
        raise Block("ENTRY_WINDOW: new entry outside configured window")
    flat = min(time.fromisoformat(c["flat_by"]), time.fromisoformat(session["broker_squareoff"]), time.fromisoformat(session["close"]))
    if p["flat_by"] != c["flat_by"] or now + timedelta(minutes=float(number(c["squareoff_buffer_minutes"]))) >= datetime.combine(now.date(), flat, IST):
        raise Block("SQUAREOFF: insufficient time before flat-by/broker close")
    key = i["exchange"] + ":" + i["symbol"]
    flags = restrictions["instruments"].get(key)
    if flags is None or any(flags.get(k) is not False for k in ("t2t", "asm", "gsm", "ban", "suspended", "corporate_action", "results_due")):
        raise Block("RESTRICTIONS: missing clearance or flagged instrument")
    if i["symbol"] in c["blocked"] or key in c["blocked"]:
        raise Block("BLOCKED_SYMBOL: user block list")
    multiplier = number(event["risk_multiplier"])
    if event["date"] != now.date().isoformat() or multiplier not in {D("0.5"), D(1)}:
        raise Block("EVENT: blocked or unknown event day")
    if multiplier == D("0.5") and event.get("override_confirmed") is not True:
        raise Block("EVENT: half-risk override must be explicitly recorded by user")
    if p["side"] == "SELL" and not c["allow_short"]:
        raise Block("SHORT: new short positions disabled")
    if p["side"] == "SELL" and i["kind"] == "option":
        raise Block("NAKED_OPTION_SHORT: standalone option short entries are unsupported")
    if i["kind"] == "option" and i["expiry"] == now.date().isoformat() and p["side"] == "BUY" and clock >= time(14, 30):
        raise Block("EXPIRY: late expiring-option purchase blocked")
    if i["kind"] != "equity" and i.get("underlying_type") == "stock" and (date.fromisoformat(i["expiry"]) - now.date()).days <= 3:
        raise Block("SETTLEMENT: stock derivative within three days of expiry")
    if account["date"] != now.date().isoformat() or account["reconciled"] is not True:
        raise Block("ACCOUNT: fresh reconciled ledger required")
    for key in ("cash_available", "margin_available", "daily_risk_used"):
        number(account[key], key)
    integer(account["trades_today"], "trades_today", 0)
    integer(account["orders_today"], "orders_today", 0)
    if account["trades_today"] >= c["max_trades_per_day"] or account["orders_today"] >= c["max_orders_per_day"]:
        raise Block("DAILY_COUNT: daily trade/order limit")
    if [i["exchange"], i["symbol"], p["side"]] in account["seen_entries"]:
        raise Block("DUPLICATE: symbol/side already entered or reserved")
    return multiplier


def cost_metrics(p, i, card, q, day):
    e, risk, reward = plan_math(p, i)
    at_stop = charges(e, p["stop"], q, p["side"], i, card, day)
    at_target = charges(e, p["target"], q, p["side"], i, card, day)
    return {"notional_inr": money(e*q), "loss_at_stop_inr": money(risk*q + D(str(at_stop["total"]))),
            "reward_at_target_before_cost_inr": money(reward*q), "charges_at_stop": at_stop, "charges_at_target": at_target}


def execution_metrics(p, i, c, ctx, card, q, now, model=None):
    result=cost_metrics(p,i,card,q,now.date().isoformat())
    if c.get("liquidity_model") is not None:
        from liquidity import evidence, stress
        model=model if model is not None else evidence(p,i,c,ctx,now)
        stressed=stress(p,model,q)
        fees=charges(p["entry"],stressed["stress_exit_price"],q,p["side"],i,card,now.date().isoformat())
        # Never credit a tax reduction against the adverse price reserve.
        result["loss_at_stop_inr"]=money(number(result["loss_at_stop_inr"])+number(stressed["slippage_reserve_inr"])+max(D(0),number(fees["total"])-number(result["charges_at_stop"]["total"])))
        result["liquidity_stress"]=stressed
        result["stressed_stop_charges"]=fees
    return result


def size(p, i, c, ctx, card, now):
    validate_rates(i, card, c["broker"])
    multiplier = entry_context(p, i, c, ctx, now)
    e, risk, reward = plan_math(p, i)
    if reward/risk < number(c["min_reward_risk"]):
        raise Block("REWARD_RISK: below configured minimum")
    a = ctx["account"]
    cash = min(number(a["cash_available"]), number(c["budget"])) - number(c["budget"])*number(c["cash_buffer_pct"])/100
    held = number(a["position_values"].get(i["exchange"]+":"+i["symbol"], 0))
    limits = {"loss": number(c["max_loss_per_trade_inr"])*multiplier/risk,
              "position": max(D(0), number(c["budget"])*number(c["max_position_pct"])/100-held)/e,
              "order": number(c["max_order_inr"])/e, "cash": max(D(0), cash)/e}
    from liquidity import evidence
    model=evidence(p,i,c,ctx,now)
    if model is not None:limits["liquidity"]=number(model["capacity"])
    lot = integer(i["lot_size"])
    q = int(min(limits.values())//lot)*lot
    per_loss = number(c["max_loss_per_trade_inr"])*multiplier
    daily_left = number(c["max_daily_loss_inr"]) - number(a["daily_risk_used"])
    # Costs are monotonic but fixed brokerage changes the loss constraint; binary search whole lots.
    lo, hi = 0, q//lot
    while lo < hi:
        mid = (lo+hi+1)//2
        m = execution_metrics(p, i, c, ctx, card, mid*lot, now, model=model)
        fits = number(m["loss_at_stop_inr"]) <= min(per_loss, daily_left) and number(m["notional_inr"])+number(m["charges_at_stop"]["total"]) <= cash
        if fits:
            lo = mid
        else:
            hi = mid-1
    q = lo*lot
    if not q:
        raise Block("ZERO_SIZE: no whole share/lot fits risk, daily limit and capital")
    m = execution_metrics(p, i, c, ctx, card, q, now, model=model)
    if number(m["notional_inr"]) < number(c["min_order_inr"]):
        raise Block("MIN_ORDER: below minimum order value")
    if number(m["charges_at_target"]["total"]) > number(c["max_cost_fraction"])*number(m["reward_at_target_before_cost_inr"]):
        raise Block("COST: round-trip charges exceed configured target fraction")
    return {"plan": copy.deepcopy(p), "quantity": q, "lots": q//lot, "metrics": m,
            "limiting_cap_before_costs": min(limits, key=limits.get), "costs_and_daily_risk_applied": True}


def payload(order):
    p = order["plan"]
    if p.get("broker") == "fyers":
        from fyers_readonly import full_symbol, token_exchange
        if token_exchange(p["token"]) != p["exchange"] or p["side"] not in {"BUY", "SELL"}:
            raise Block("IDENTITY: FYERS token/exchange or side mismatch")
        return {"symbol": full_symbol(p["symbol"], p["exchange"]), "qty": integer(order["quantity"]),
                "type": 1, "side": 1 if p["side"] == "BUY" else -1, "productType": "INTRADAY",
                "limitPrice": float(positive(p["entry"])), "stopPrice": 0, "disclosedQty": 0,
                "validity": "DAY", "offlineOrder": False, "stopLoss": 0, "takeProfit": 0, "isSliceOrder": False}
    if p.get("broker") == "angel":
        from angel_readonly import symbol_token, exchange_name
        return {"variety": "NORMAL", "tradingsymbol": p["symbol"], "symboltoken": symbol_token(p["token"]),
                "transactiontype": p["side"], "exchange": exchange_name(p["exchange"]), "ordertype": "LIMIT",
                "producttype": "INTRADAY", "duration": "DAY", "price": float(positive(p["entry"])),
                "quantity": integer(order["quantity"]), "squareoff": 0, "stoploss": 0}
    if p.get("broker") == "dhan":
        from dhan_readonly import security_id, segment_for
        return {"dhanClientId": p["account_id"], "transactionType": p["side"],
                "exchangeSegment": segment_for(p["exchange"]), "productType": "INTRADAY",
                "orderType": "LIMIT", "validity": "DAY", "securityId": security_id(p["token"]),
                "quantity": integer(order["quantity"]), "price": float(positive(p["entry"])),
                "triggerPrice": 0, "disclosedQuantity": 0, "afterMarketOrder": False}
    if p.get("broker") == "upstox":
        from upstox_readonly import instrument_key
        key = instrument_key(p["token"])
        expected = {"NSE": "NSE_EQ", "BSE": "BSE_EQ", "NFO": "NSE_FO", "BFO": "BSE_FO"}
        if key.split("|")[0] != expected.get(p["exchange"]):
            raise Block("IDENTITY: Upstox key does not match exchange")
        return {"instrument_token": key, "transaction_type": p["side"],
                "quantity": integer(order["quantity"]), "product": "I", "order_type": "LIMIT",
                "price": float(positive(p["entry"])), "validity": "DAY",
                "disclosed_quantity": 0, "trigger_price": 0, "is_amo": False, "slice": False}
    if p.get("broker") != "kite":
        raise Block("BROKER: payload requires an implemented broker")
    return {"exchange": p["exchange"], "tradingsymbol": p["symbol"], "transaction_type": p["side"],
            "quantity": integer(order["quantity"]), "product": p["product"], "order_type": "LIMIT",
            "price": float(positive(p["entry"])), "validity": "DAY"}


def check_order(order, i, c, ctx, card, now):
    p, q = order["plan"], integer(order["quantity"])
    if q % integer(i["lot_size"]):
        raise Block("LOT: quantity not a whole lot")
    if "freeze_quantity" in i and q>integer(i["freeze_quantity"]):
        raise Block("FREEZE: quantity exceeds supplied instrument freeze limit; no auto-slicing")
    maximum = size(p, i, c, ctx, card, now)
    if q > maximum["quantity"]:
        raise Block("SIZE: order exceeds recomputed limit")
    m = execution_metrics(p, i, c, ctx, card, q, now)
    if number(m["notional_inr"]) < number(c["min_order_inr"]) or number(m["charges_at_target"]["total"]) > number(c["max_cost_fraction"])*number(m["reward_at_target_before_cost_inr"]):
        raise Block("COST_SIZE: actual quantity fails minimum/cost check")
    # Derivative margins must be an exact quantity/payload quote, never a scaled per-lot guess.
    if i["kind"] != "equity":
        margin = ctx.get("margins", {}).get(fingerprint(payload(order))) or ctx["margin"]
        fresh(margin["as_of"], now, c["snapshot_max_age_seconds"], "margin")
        if margin["source"] != c["broker"] or margin.get("account_id") != c["account_id"] or margin["payload_hash"] != fingerprint(payload(order)):
            raise Block("MARGIN: obtain selected-broker/account margin quote for this exact payload")
        required = positive(margin["required_inr"], "margin required")
        if required+number(m["charges_at_stop"]["total"]) > number(ctx["account"]["margin_available"]):
            raise Block("MARGIN: insufficient available margin")
    else:
        required = number(m["notional_inr"])  # no leverage assumed for cash paper analysis
    return {"status": "PASS", "broker": c["broker"], "account_id": c["account_id"],
            "order_hash": fingerprint(order), "as_of": now.isoformat(),
            "metrics": m, "reserved_margin_inr": money(required), "paper": True}


def risk_batch(orders, instruments, c, ctx, card, now):
    projected = copy.deepcopy(ctx)
    results = []
    for order in orders:
        try:
            p = order["plan"]
            i = instruments[p["exchange"]+":"+p["symbol"]]
            result = check_order(order, i, c, projected, card, now)
            a = projected["account"]
            m = result["metrics"]
            a["daily_risk_used"] = money(number(a["daily_risk_used"])+number(m["loss_at_stop_inr"]))
            a["cash_available"] = money(number(a["cash_available"])-number(m["notional_inr"])-number(m["charges_at_stop"]["total"]))
            a["margin_available"] = money(max(D(0),number(a["margin_available"])-number(result["reserved_margin_inr"])))
            a["trades_today"] += 1
            a["orders_today"] += 1
            a["seen_entries"].append([p["exchange"], p["symbol"], p["side"]])
            key=p["exchange"]+":"+p["symbol"]
            a["position_values"][key]=money(number(a["position_values"].get(key,0))+number(m["notional_inr"]))
        except (Block, KeyError, TypeError, ValueError) as exc:
            result = {"status": "BLOCK", "reason": str(exc), "fix": "Correct or refresh the named input, then rerun; do not alter this order to force a PASS."}
        results.append(result)
    return {"as_of": now.isoformat(), "results": results, "paper": True}


def ticket(order, i, c, ctx, card, now):
    result = check_order(order, i, c, ctx, card, now)
    return {"mode": "PAPER_PREVIEW_ONLY", "submitted": False, "risk": result,
            "review_hash": fingerprint({"order": order, "config": c, "context": ctx, "rates": card, "instrument": i}),
            "plain": f"{order['plan']['side']} {order['quantity']} {i['symbol']} units; LIMIT {order['plan']['entry']} INR, MIS. Stop {order['plan']['stop']}, target {order['plan']['target']}; flat by {order['plan']['flat_by']} IST.",
            "broker": c["broker"], "account_id": c["account_id"], "payload_preview": payload(order),
            **({"kite_payload_preview": payload(order)} if c["broker"] == "kite" else {}),
            "stop_status": "Plan only; no stop or target order is installed or monitored.",
            "risk_note": "Loss at stop includes estimated costs; gaps and slippage can cause larger losses."}


def reducing_exit(position, side, qty):
    """Entry gates must not lock exits, but an exit cannot reverse a position."""
    qty = integer(qty)
    held = D(str(position["quantity"]))
    if not held.is_finite() or held == 0 or qty > abs(held) or side != ("SELL" if held > 0 else "BUY"):
        raise Block("EXIT: must strictly reduce a known position without reversal")
    return {"exchange": position["exchange"], "symbol": position["symbol"], "product": position["product"],
            "side": side, "quantity": qty, "paper": True, "submitted": False}


def alerts(p, quote, now, c, halted=False):
    fresh(quote["as_of"], now, c["snapshot_max_age_seconds"], "quote")
    if quote["symbol"] != p["symbol"] or quote["exchange"] != p["exchange"]:
        raise Block("QUOTE: wrong instrument")
    price = positive(quote["price"])
    out=[]
    if halted:
        out.append("HALT: executions may be unavailable")
    if now.time() >= time.fromisoformat(c["flat_warning"]):
        out.append("FLAT_BY: check broker positions before " + c["flat_by"])
    if (price <= number(p["stop"]) if p["side"] == "BUY" else price >= number(p["stop"])):
        out.append("STOP_LEVEL_REACHED")
    if (price >= number(p["target"]) if p["side"] == "BUY" else price <= number(p["target"])):
        out.append("TARGET_LEVEL_REACHED")
    return {"as_of": now.isoformat(), "alerts": out, "continuous_monitoring": False}
