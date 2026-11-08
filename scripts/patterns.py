"""Daily-context intraday rules; these definitions are not validated trading edges."""
from datetime import datetime, time, timedelta, date
import copy
from core import Block, IST, positive, number, instant, session_bars, money

DAILY_SETUPS = {"breakout_20d", "bull_flag", "oversold_bounce"}


def daily_reference(ref, instrument, session):
    if not isinstance(ref, dict) or not isinstance(instrument, dict):
        raise Block("DAILY_REFERENCE: exact instrument and dated daily history required")
    if any(ref.get(k) != instrument.get(k) or ref.get(k) is None for k in ("broker", "exchange", "symbol", "token")):
        raise Block("DAILY_REFERENCE: instrument mismatch")
    if not ref.get("source") or not ref.get("calendar_source") or ref.get("for_session") != session:
        raise Block("DAILY_REFERENCE: sources and target session required")
    if ref.get("price_basis") != "unadjusted_session_ohlc" or ref.get("corporate_actions_clear") is not True:
        raise Block("DAILY_REFERENCE: comparable prices and corporate-action clearance required")
    bars, dates = ref.get("bars", []), ref.get("sessions", [])
    if len(bars) < 30 or len(dates) != len(bars) or dates != sorted(set(dates)):
        raise Block("DAILY_REFERENCE: at least 30 ordered calendar sessions required")
    if dates != [b.get("date") for b in bars] or ref.get("previous_session") != dates[-1]:
        raise Block("DAILY_REFERENCE: missing daily bar or previous session mismatch")
    opened = datetime.combine(date.fromisoformat(session), time(9, 15), IST)
    for b in bars:
        d = date.fromisoformat(b["date"])
        if not b.get("available_at") or not datetime.combine(d, time(15, 30), IST) <= instant(b["available_at"]) < opened:
            raise Block("DAILY_REFERENCE: daily evidence unavailable before target open")
        o, h, l, c = [positive(b[k], k) for k in ("open", "high", "low", "close")]
        positive(b["volume"], "daily volume")
        if not l <= min(o, c) <= max(o, c) <= h:
            raise Block("DAILY_REFERENCE: inconsistent OHLC")
    return bars


def rsi(closes, period=14):
    """Wilder RSI, seeded with the first period in the supplied series."""
    values = [positive(c) for c in closes]
    if len(values) <= period:
        raise Block("RSI: insufficient closes")
    delta = [b-a for a, b in zip(values, values[1:])]
    gain = sum((max(d, 0) for d in delta[:period]), number(0))/period
    loss = sum((max(-d, 0) for d in delta[:period]), number(0))/period
    for d in delta[period:]:
        gain = (gain*(period-1)+max(d, 0))/period
        loss = (loss*(period-1)+max(-d, 0))/period
    return float(100-100/(1+gain/loss)) if loss else (100.0 if gain else 50.0)


def daily_setup(rows, now, config, instrument, reference):
    setup = config["setup"]
    daily = daily_reference(reference, instrument, now.date().isoformat())
    bars = session_bars(rows, now)
    closes = [positive(b["close"]) for b in daily]
    waiting = {"state": "waiting", "setup": setup}
    diagnostics = {}
    if setup == "breakout_20d":
        level = max(closes[-20:])
    elif setup == "bull_flag":
        run = closes[-6]/closes[-16]-1
        width = (max(closes[-5:])-min(closes[-5:]))/closes[-1]
        diagnostics = {"pole_gain_pct": float(run*100), "flag_close_range_pct": float(width*100)}
        if run <= number("0.10") or width >= number("0.05"):
            return {**waiting, **diagnostics, "reason": "No >10% ten-session pole followed by <5% five-close flag"}
        level = max(positive(b["high"]) for b in daily[-5:])
    else:
        score = rsi(closes)
        diagnostics = {"prior_rsi14": score}
        if score >= 30:
            return {**waiting, **diagnostics, "reason": "Prior-session Wilder RSI14 is not below 30"}
        level = positive(daily[-1]["high"])
    for k, b in enumerate(bars):
        at = instant(b["at"])+timedelta(minutes=5)
        if at.time() < max(time(9, 30), time.fromisoformat(config["no_entries_before"])):
            continue
        if at.time() > time.fromisoformat(config["no_entries_after"]):
            break
        close = positive(b["close"])
        if close > level:
            low = min(positive(x["low"]) for x in bars[:k+1])
            high = max(positive(x["high"]) for x in bars[:k+1])
            for x in bars[:k+1]:
                positive(x["volume"], "signal bar volume")
            return {"state": "triggered", "setup": setup, "rule_version": 1, "side": "BUY",
                    "entry": float(close), "range_low": float(low), "range_high": float(high),
                    "stop_reference": float(low), "reference_price": float(level),
                    "percent_over_level": money((close/level-1)*100), "trigger_at": at.isoformat(),
                    "daily_reference": copy.deepcopy(reference), "diagnostics": diagnostics,
                    "turnover_estimate_inr": money(sum(number(x["close"])*number(x["volume"]) for x in bars[:k+1])),
                    "label": "matched a rule"}
    return {**waiting, **diagnostics, "reference_price": float(level), "reason": "No eligible completed close above daily-context level"}
