"""Broker-bound interval and point-in-time custom-level contracts."""
from datetime import timedelta, time
from core import Block, instant, positive, fingerprint, on_tick


def interval(config):
    value = config.get("interval_minutes", 5)
    if type(value) is not int or value not in (1, 5):
        raise Block("INTERVAL: integer 1 or 5 required")
    if value == 1 and config.get("setup", "orb") not in ("orb", "level"):
        raise Block("INTERVAL: one-minute broker entries support ORB and level only")
    return value


def level_reference(ref, instrument, now):
    if not isinstance(ref, dict) or not isinstance(instrument, dict):
        raise Block("LEVEL: exact instrument and sourced level required")
    if any(str(ref.get(k)) != str(instrument.get(k)) or ref.get(k) is None
           for k in ("broker", "symbol", "exchange", "token")):
        raise Block("LEVEL: broker/instrument mismatch")
    if ref.get("side") not in ("BUY", "SELL") or not isinstance(ref.get("source"), str) or not ref["source"].strip():
        raise Block("LEVEL: side and source required")
    if ref.get("for_session") != now.date().isoformat():
        raise Block("LEVEL: level must belong to the evaluated session")
    available = instant(ref.get("available_at")) if ref.get("available_at") else None
    if available is None or available > now:
        raise Block("LEVEL: point-in-time availability required")
    price = positive(ref.get("price"))
    if not on_tick(price, instrument["tick_size"]):
        raise Block("LEVEL: price must align to the exact instrument tick")
    return price, available


def level_hit(rows, now, config, instrument):
    from core import session_bars, number, money
    minutes = interval(config)
    ref = config.get("level_entry")
    price, available = level_reference(ref, instrument, now)
    bars = session_bars(rows, now, minutes)
    long = ref["side"] == "BUY"
    for k, b in enumerate(bars):
        start = instant(b["at"])
        trigger = start + timedelta(minutes=minutes)
        if start < available or not time.fromisoformat(config["no_entries_before"]) <= trigger.time() <= time.fromisoformat(config["no_entries_after"]):
            continue
        close = positive(b["close"])
        if close > price if long else close < price:
            observed = bars[:k+1]
            return {"state":"triggered", "setup":"level", "rule_version":1, "side":ref["side"],
                    "interval_minutes":minutes, "entry":float(close), "reference_price":float(price),
                    "stop_reference":float(min(positive(x["low"]) for x in observed) if long else max(positive(x["high"]) for x in observed)),
                    "range_high":float(max(positive(x["high"]) for x in observed)),
                    "range_low":float(min(positive(x["low"]) for x in observed)),
                    "trigger_at":trigger.isoformat(), "level_entry":dict(ref),
                    "percent_over_level":money((close/price-1)*100),
                    "turnover_estimate_inr":money(sum(number(x["close"])*number(x["volume"]) for x in observed)),
                    "label":"matched a user-supplied level rule"}
    return {"state":"waiting", "setup":"level", "interval_minutes":minutes, "reason":"No completed eligible close beyond the supplied level"}


def check(plan, config):
    if plan.get("interval_minutes", 5) != interval(config):
        raise Block("ENTRY_POLICY: interval changed; regenerate evidence")
    if plan.get("setup") == "level" and fingerprint(plan.get("level_entry")) != fingerprint(config.get("level_entry")):
        raise Block("ENTRY_POLICY: custom level changed; regenerate evidence")


def check_candles(data, config):
    expected = interval(config)
    actual = data.get("interval", "5minute")
    if actual != ("minute" if expected == 1 else "5minute"):
        raise Block("INTERVAL: candle metadata must match the configured interval")
