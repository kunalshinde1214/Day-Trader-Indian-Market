"""Public research calculations and explicit exit policies; no broker access."""
from datetime import timedelta, time
import re

from core import Block, instant, positive, number, integer, tick
from patterns import rsi


def prefix(rows, now, interval=5):
    if interval not in {1, 5}: raise Block("INTERVAL: use 1 or 5 minutes")
    start = now.replace(hour=9, minute=15, second=0, microsecond=0)
    end = start.replace(hour=15, minute=30)
    found = {}
    for row in rows:
        at = instant(row["at"])
        if at.date() != now.date(): continue
        if not start <= at < end: continue
        if at > now or (at-start).total_seconds() % (60*interval): raise Block("BARS: future or misaligned bar")
        if at in found: raise Block("BARS: duplicate bar")
        o, h, l, c = [positive(row[k]) for k in ("open", "high", "low", "close")]
        number(row["volume"])
        if not l <= min(o, c) <= max(o, c) <= h: raise Block("BARS: contradictory OHLC")
        found[at] = row
    count = max(0, int((min(now, end)-start).total_seconds() // (interval*60)))
    expected = [start+timedelta(minutes=interval*k) for k in range(count)]
    if any(at not in found for at in expected): raise Block("BARS: incomplete session prefix")
    return [found[at] for at in expected]


def entry(rows, now, *, setup="orb", level=None, side="BUY", interval=5, not_before="09:30", not_after="14:30"):
    if side not in {"BUY", "SELL"} or setup not in {"orb", "level"}: raise Block("ENTRY: invalid side/setup")
    before, after = time.fromisoformat(not_before), time.fromisoformat(not_after)
    if not time(9,15) <= before <= after < time(15,30): raise Block("ENTRY: invalid window")
    bars = prefix(rows, now, interval)
    n = 15//interval
    waiting = {"state": "waiting", "setup": setup, "interval_minutes": interval, "side": side}
    if setup == "orb":
        if len(bars) <= n: return {**waiting, "reason": "Need opening range and completed confirmation"}
        level = max(positive(b["high"]) for b in bars[:n]) if side == "BUY" else min(positive(b["low"]) for b in bars[:n])
    else: level = positive(level)
    for k, b in enumerate(bars):
        at = instant(b["at"])+timedelta(minutes=interval)
        if not before <= at.time() <= after or setup == "orb" and k < n: continue
        close = positive(b["close"])
        if close > level if side == "BUY" else close < level:
            observed = bars[:k+1]
            return {**waiting, "state": "triggered", "rule_version": 1, "entry": float(close), "reference_price": float(level),
                    "range_high": float(max(positive(x["high"]) for x in observed[:n])),
                    "range_low": float(min(positive(x["low"]) for x in observed[:n])),
                    "stop_reference": float(min(positive(x["low"]) for x in observed) if side == "BUY" else max(positive(x["high"]) for x in observed)),
                    "trigger_at": at.isoformat(), "turnover_estimate_inr": float(sum(positive(x["close"])*number(x["volume"]) for x in observed))}
    return {**waiting, "reference_price": float(level), "reason": "No eligible completed close across the level"}


def screen(rows, now):
    # Multi-pattern daily research always excludes today's potentially revised bar.
    past = sorted((b for b in rows if instant(b["at"]).date() < now.date()), key=lambda b: instant(b["at"]))
    dates = [instant(b["at"]).date() for b in past]
    if len(past) < 31 or len(set(dates)) != len(dates): raise Block("SCREEN: 31 distinct prior daily observations required")
    for b in past:
        o,h,l,c = [positive(b[k]) for k in ("open","high","low","close")]
        if not l <= min(o,c) <= max(o,c) <= h: raise Block("SCREEN: invalid OHLC")
        number(b["volume"])
    closes = [positive(b["close"]) for b in past]
    latest, previous = closes[-1], closes[-2]
    ma, previous_ma = sum(closes[-20:])/20, sum(closes[-21:-1])/20
    score, prior_score = rsi(closes), rsi(closes[:-1])
    tags = []
    if latest > max(closes[-21:-1]): tags.append("breakout_20d")
    if closes[-6]/closes[-16]-1 > number("0.10") and (max(closes[-5:])-min(closes[-5:]))/latest < number("0.05"): tags.append("bull_flag")
    if previous < previous_ma and latest > ma: tags.append("reclaim_ma20")
    if score < 30: tags.append("oversold")
    if prior_score < 30 and latest > previous: tags.append("oversold_bounce")
    return {"setups": tags, "session": dates[-1].isoformat(), "close": float(latest), "sma20": float(ma), "rsi14": score,
            "prior_rsi14": prior_score, "observations": len(past), "historical_membership_verified": False,
            "basis": "Unadjusted supplied prior daily observations; not an eligibility-cleared entry"}


def exit_policy(config):
    return {"stop": config.get("exit_stop", "setup"), "target": config.get("exit_target", str(config["target_r"]) + "R")}


def check_policy(plan, config):
    expected = exit_policy(config)
    recorded = plan.get("exit_policy")
    if recorded is not None and recorded != expected or recorded is None and ("exit_stop" in config or "exit_target" in config):
        raise Block("EXIT_POLICY: changed exit rules; regenerate plan and replay")


def exits(hit, step, *, stop="or-mid", target="2R", rows=None, reference=None, buffer_ticks=0):
    long = hit["side"] == "BUY"
    if hit["side"] not in {"BUY", "SELL"}: raise Block("SIDE: BUY or SELL required")
    entry_price = positive(tick(hit["entry"], step, up=long))
    diagnostics = {}
    if stop == "setup": ref = positive(reference)
    elif stop == "or-mid": ref = (positive(hit["range_high"])+positive(hit["range_low"]))/2
    elif stop == "or-low": ref = positive(hit["range_low"] if long else hit["range_high"])
    elif isinstance(stop, str) and ":" in stop:
        kind, value = stop.split(":", 1); value = positive(value)
        if kind == "price": ref = value
        elif kind == "pct":
            if value >= 100: raise Block("STOP: percentage must be below 100")
            ref = entry_price*(1-value/100 if long else 1+value/100)
        elif kind == "atr":
            interval = hit.get("interval_minutes", 5)
            cutoff = instant(hit["trigger_at"])
            bars = prefix([b for b in (rows or []) if instant(b["at"]) < cutoff], cutoff, interval)
            if not bars: raise Block("ATR: completed bars through entry required")
            ranges = []
            for k,b in enumerate(bars):
                high,low = positive(b["high"]),positive(b["low"])
                prior = positive(bars[k-1]["close"]) if k else positive(b["open"])
                ranges.append(max(high-low, abs(high-prior), abs(low-prior)))
            atr = sum(ranges[-14:])/len(ranges[-14:])
            diagnostics = {"atr": float(atr), "atr_bars": min(14,len(bars)), "atr_partial_period": len(bars)<14}
            ref = entry_price-value*atr if long else entry_price+value*atr
        else: raise Block("STOP: unsupported stop rule")
    else: raise Block("STOP: unsupported stop rule")
    if not (ref < entry_price if long else ref > entry_price): raise Block("STOP: stop must be on loss side")
    buffer = integer(buffer_ticks, minimum=0)*positive(step) if stop in {"setup","or-mid","or-low"} else number(0)
    stop_price = positive(tick(ref-buffer if long else ref+buffer, step, up=not long))
    risk = abs(entry_price-stop_price)
    if risk/entry_price < number("0.0005"): raise Block("STOP: below 0.05 percent minimum distance")
    if isinstance(target,str) and target.startswith("price:"): target_value=positive(target[6:])
    elif isinstance(target,str) and re.fullmatch(r"(?:\d+(?:\.\d*)?|\.\d+)[Rr]", target):
        target_value=entry_price+(1 if long else -1)*positive(target[:-1])*risk
    else: raise Block("TARGET: use positive R multiple or price:NUMBER")
    target_price = positive(tick(target_value, step, up=long))
    if not (target_price > entry_price if long else target_price < entry_price): raise Block("TARGET: target must be on profit side")
    return {"entry": float(entry_price), "stop": float(stop_price), "target": float(target_price), "risk_per_unit": float(risk),
            "reward_risk": float(abs(target_price-entry_price)/risk), "exit_policy": {"stop":stop,"target":target}, **diagnostics}
