"""Daily Indian spot-index briefings. No account access or execution endpoints."""
from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeout
from datetime import date, datetime, time, timedelta
import html
import json
import math
from decimal import Decimal, ROUND_HALF_UP
import http.client
from pathlib import Path
import socket
import sys
from time import monotonic

from core import Block, IST, fresh, instant, number, positive
from trader import read

INDICES = {"nifty": ("Nifty 50", "NSE"), "sensex": ("Sensex", "BSE"), "banknifty": ("Bank Nifty", "NSE")}
COMMANDS = ("india-daily", *INDICES)
TITLES = {"india-daily": "India markets", **{k: v[0] for k, v in INDICES.items()}}
PHASES = {"preopen": "pre-open", "intraday": "market hours", "close": "after close"}
VIX_SYMBOL = "^INDIAVIX"
VIX_MAX_AGE = 900


def provenance(data, now, label, max_age=None):
    if not isinstance(data.get("source"), str) or not data["source"].strip():
        raise Block(label + ": source required")
    stamp = instant(data["as_of"])
    if stamp > now:
        raise Block(label + ": future source timestamp")
    if max_age is not None:
        fresh(data["as_of"], now, max_age, label)


def session_bounds(session, now, requested="auto"):
    provenance(session, now, "calendar", 7 * 86400)
    if session["date"] != now.date().isoformat() or session.get("is_open_day") is not True:
        raise Block("SESSION: verified open session for the report date required; no holiday guess")
    if now.weekday() >= 5 and session.get("special_session") is not True:
        raise Block("SESSION: explicit special-session evidence required on weekends")
    if date.fromisoformat(session["previous_session"]) >= now.date():
        raise Block("SESSION: previous trading session must precede report date")
    start = datetime.combine(now.date(), time.fromisoformat(session["open"]), IST)
    end = datetime.combine(now.date(), time.fromisoformat(session["close"]), IST)
    if start >= end or (end - start).total_seconds() % 300:
        raise Block("SESSION: invalid five-minute session boundaries")
    phase = "preopen" if now < start else "intraday" if now < end else "close"
    if requested not in {"auto", phase}:
        raise Block("PHASE: requested report does not match its clock; use --now for dated analysis")
    return start, end, phase


def price(value):
    result = float(positive(value))
    if not math.isfinite(result) or result <= 0:
        raise Block("PRICE: outside supported numeric range")
    return result


def percent_change(value, previous):
    result = float((positive(value) / positive(previous) - 1) * 100)
    if not math.isfinite(result):
        raise Block("CHANGE: outside supported numeric range")
    return round(result, 3)


def point_change(value, previous):
    result = float(value) - float(previous)
    if not math.isfinite(result):
        raise Block("CHANGE: outside supported numeric range")
    return round(result, 2)


def ohlc(row):
    values = {k: price(row[k]) for k in ("open", "high", "low", "close")}
    if not values["low"] <= min(values["open"], values["close"]) <= max(values["open"], values["close"]) <= values["high"]:
        raise Block("OHLC: contradictory range")
    return values


def expected_bars(start, end, now):
    count = max(0, int((min(now, end) - start).total_seconds() // 300))
    return [start + timedelta(minutes=5 * k) for k in range(count)]


def completed(rows, start, end, now, strict=True):
    """Completed five-minute bars of the session. Corrupt data (duplicates, disorder, foreign or future bars) always
    raises. A gap raises only when ``strict``; otherwise the bars that exist are returned and the caller labels the gap."""
    if not isinstance(rows, list):
        raise Block("BARS: explicit array required")
    seen, result = set(), []
    last = None
    for row in rows:
        at = instant(row["at"])
        values = ohlc(row)
        if at in seen or last is not None and at <= last:
            raise Block("BARS: duplicate or unordered timestamps")
        seen.add(at); last = at
        if not start <= at < end or (at - start).total_seconds() % 300 or at > now:
            raise Block("BARS: foreign session, misaligned or future bar")
        if at + timedelta(minutes=5) <= min(now, end):
            result.append({"at": at.isoformat(), **values})
    if strict and [instant(r["at"]) for r in result] != expected_bars(start, end, now):
        raise Block("BARS: incomplete opening/intervening/latest five-minute candles")
    return result


def quote_age_limit(q):
    """Seconds a live quote stays usable: 60 by default; a delayed public source may declare up to 30 minutes (its age is shown)."""
    limit = q.get("max_age_seconds", 60)
    return 60 if isinstance(limit, bool) or not isinstance(limit, (int, float)) or not 0 < limit <= 1800 else limit


def live_quote(q, now, start):
    """A validated current-session quote, or None with a plain reason (stale/foreign quotes are ignored, never trusted)."""
    try:
        provenance(q, now, "index quote", quote_age_limit(q))
        if instant(q["as_of"]) < start: return None, "Latest quote is not from today's session; not used"
        value = price(q["value"])
        out = {"value": value, "as_of": instant(q["as_of"]).isoformat(), "provider": str(q.get("provider") or q["source"]),
               "source": q["source"], "page": q.get("page")}
        for key in ("open", "high", "low"):
            out[key] = price(q[key]) if q.get(key) not in (None, "", 0) else None
        if out["high"] is not None and out["low"] is not None and not out["low"] <= value <= out["high"]:
            out.update(open=None, high=None, low=None)
        return out, None
    except Block as exc:
        return None, "Latest quote is stale; not used" if str(exc).startswith("STALE") else "Latest quote failed checks; not used"
    except (KeyError, TypeError, ValueError, ArithmeticError):
        return None, "Latest quote failed checks; not used"


def breadth(data, session, now, phase):
    if phase == "preopen":
        return {"status": "UNAVAILABLE", "reason": "Current-session breadth is not available before open"}
    provenance(data, now, "breadth", 60 if phase == "intraday" else 86400)
    if data["membership_date"] != session["date"] or not data.get("membership_source"):
        raise Block("BREADTH: same-day sourced membership required")
    members = data["members"]
    if not isinstance(members, list) or not members or any(not isinstance(s, str) or not s for s in members) or len(set(members)) != len(members):
        raise Block("BREADTH: unique member symbols required")
    observations, missing, seen = [], [], set()
    for row in data["observations"]:
        symbol = row["symbol"]
        if symbol not in members or symbol in seen:
            raise Block("BREADTH: duplicate or non-member observation")
        seen.add(symbol)
        stamp = instant(row["as_of"])
        if stamp.date().isoformat() != session["date"] or stamp > now:
            raise Block("BREADTH: observation from another session or future")
        if phase == "intraday": fresh(row["as_of"], now, 60, "member quote")
        elif row.get("final") is not True: raise Block("BREADTH: closing report needs final member closes")
        if row["previous_session"] != session["previous_session"]:
            raise Block("BREADTH: previous-close date mismatch")
        change = percent_change(price(row["value"]), price(row["previous_close"]))
        observations.append({"symbol": symbol, "change_pct": change})
    missing = sorted(set(members) - seen)
    ordered = sorted(observations, key=lambda r: (-r["change_pct"], r["symbol"]))
    return {"status": "PARTIAL" if missing or data.get("membership_verified") is False else "COMPLETE", "source": data["source"], "expected": len(members),
            "membership_basis": data.get("membership_basis", "supplied sourced current-session membership"),
            "observed": len(observations), "missing": missing, "advancing": sum(r["change_pct"] > 0 for r in observations),
            "declining": sum(r["change_pct"] < 0 for r in observations), "unchanged": sum(r["change_pct"] == 0 for r in observations),
            "strongest_observed": ordered[:3], "weakest_observed": list(reversed(ordered[-3:])),
            "interpretation": "Unweighted constituent returns; not index-point contributions"}


def daily_history(data, session, now, prev):
    """Validated prior-session closes ending at the previous session, oldest first."""
    provenance(data, now, "daily history")
    dates = [date.fromisoformat(r["session"]) for r in data["rows"]]
    if dates != sorted(set(dates)) or not dates or dates[-1].isoformat() != session["previous_session"] or dates[-1] >= now.date():
        raise Block("HISTORY: ordered prior sessions ending at previous session required")
    rows = []
    for r, day in zip(data["rows"], dates):
        if instant(r["available_at"]) > now or instant(r["available_at"]).date() < day:
            raise Block("HISTORY: invalid availability timestamp")
        rows.append((day.isoformat(), price(r["close"])))
    if rows[-1][1] != prev["close"]: raise Block("HISTORY: previous close disagrees with daily reference")
    return rows


def intraday_bars(data, start, end, now, horizon=None):
    """(rows, state, note): complete, incomplete (gaps) or unavailable bars. Never blocks the index report.

    ``horizon``: the time of a delayed feed's latest quote (Yahoo's Sensex runs ~15 minutes behind). Bars ending after it
    have not been delivered yet, or are still forming in that feed, so they are neither used nor reported as missing."""
    try:
        bars = data["candles"]
        provenance(bars, now, "candle snapshot", 120)
        if bars.get("interval") != "5minute": raise Block("BARS: five-minute interval required")
        rows = completed(bars["rows"], start, end, now, strict=False)
    except (Block, KeyError, TypeError, ValueError, ArithmeticError):
        return [], "unavailable", "Intraday 5-minute bars unavailable from the public feed"
    if horizon is not None:
        rows = [r for r in rows if instant(r["at"]) + timedelta(minutes=5) <= horizon]
    expected = expected_bars(start, end, horizon or now)
    if [instant(r["at"]) for r in rows] == expected:
        return rows, "complete", None
    return rows, "incomplete", f"Intraday 5-minute bars incomplete ({len(rows)} of {len(expected)} received)"


SPIKE_LIMIT = 0.003   # a wick reaching more than 0.3% beyond both the bar's open and close...
SPIKE_SHARE = 0.25    # ...is a bad tick when the reference index's bar at the same time has a wick under a quarter as long
DAY_TOLERANCE = 0.0005  # a bar beyond the published day high/low by less than 0.05% is clipped silently (rounding)
MAX_BAD_TICKS = 3       # more bars than this beyond the published range means the two feeds disagree; none is clipped to it
REFERENCE_INDEX = {"sensex": "nifty", "banknifty": "nifty", "nifty": "sensex"}
BAR_ERRORS = (Block, KeyError, TypeError, ValueError, ArithmeticError, IndexError, AttributeError)


def _wicks(row):
    """(upper wick, lower wick) as fractions of the bar's body edges, plus the body's top and bottom."""
    o, h, l, c = (float(row[k]) for k in ("open", "high", "low", "close"))
    top, bottom = max(o, c), min(o, c)
    if not (math.isfinite(h) and math.isfinite(l) and top > 0 and bottom > 0): raise ValueError("bar")
    return h / top - 1, 1 - l / bottom, top, bottom


def despike(rows, reference=None, day_high=None, day_low=None):
    """(rows, fixes): five-minute bars with isolated bad-tick wicks cut back. A wick counts as a bad tick when it reaches
    more than SPIKE_LIMIT beyond both the bar's open and close while the reference index's bar at the same time is not
    similarly extended (its wick under SPIKE_SHARE of this one); it is cut back to the bar's body. When the provider's own
    day high/low is known (after the close), no bar may reach beyond it. Without a reference bar, a wick inside the day
    range is kept: a real fast move must never be erased. Malformed rows pass through untouched (``completed`` judges them)."""
    ref = {}
    for r in reference or []:
        try: ref[instant(r["at"])] = _wicks(r)
        except BAR_ERRORS: continue

    def beyond(row):
        try: return (day_high and float(row["high"]) > day_high * (1 + DAY_TOLERANCE)) or (day_low and float(row["low"]) < day_low * (1 - DAY_TOLERANCE))
        except BAR_ERRORS: return False
    if sum(bool(beyond(r)) for r in rows) > MAX_BAD_TICKS:
        day_high = day_low = None  # many bars outside the published range: the feeds disagree, not one bad tick; clip nothing
    out, fixes = [], []
    for row in rows:
        try:
            up, down, top, bottom = _wicks(row)
            at, high, low = instant(row["at"]), float(row["high"]), float(row["low"])
        except BAR_ERRORS:
            out.append(row); continue
        new_high, new_low, kinds = high, low, []
        other = ref.get(at)
        if up > SPIKE_LIMIT and other is not None and other[0] < up * SPIKE_SHARE: new_high = top; kinds.append("spike")
        if down > SPIKE_LIMIT and other is not None and other[1] < down * SPIKE_SHARE: new_low = bottom; kinds.append("spike")
        if day_high and new_high > day_high:
            if new_high > day_high * (1 + DAY_TOLERANCE): kinds.append("day_range")
            new_high = max(top, day_high)
        if day_low and new_low < day_low:
            if new_low < day_low * (1 - DAY_TOLERANCE): kinds.append("day_range")
            new_low = min(bottom, day_low)
        if (new_high, new_low) != (high, low):
            if kinds:
                fixes.append({"at": instant(row["at"]).isoformat(), "kind": "spike" if "spike" in kinds else "day_range",
                              "high": high, "low": low, "high_now": new_high, "low_now": new_low,
                              "wick_pct": round(max(up if new_high != high else 0, down if new_low != low else 0) * 100, 2)})
            row = {**row, "high": new_high, "low": new_low}
        out.append(row)
    return out, fixes


def reference_bars(key, bundle):
    """(index key, rows) of the reference index's five-minute bars for spike checks, or (None, None)."""
    other = REFERENCE_INDEX.get(key)
    rows = (((bundle.get("indices") or {}).get(other) or {}).get("candles") or {}).get("rows")
    if isinstance(rows, list) and rows: return other, rows
    ref = bundle.get("reference_bars") or {}
    if ref.get("index") == other and isinstance(ref.get("rows"), list) and ref["rows"]: return other, ref["rows"]
    cmp = bundle.get("comparison_bars") or {}
    if key == "sensex" and isinstance(cmp.get("rows"), list) and cmp["rows"]: return "nifty", cmp["rows"]
    return None, None


def clean_bars(key, data, bundle, phase, session_date):
    """(data, fixes): the index evidence with its five-minute bars despiked (a copy; the bundle is not changed). The
    cleaned bars feed the day range (when no published high/low exists), the opening range and the pivots alike."""
    try:
        rows = data["candles"]["rows"]
        if not isinstance(rows, list) or not rows: return data, []
        ref_key, ref_rows = reference_bars(key, bundle)
        high = low = None
        oc = data.get("official_close") or {}
        if phase == "close" and oc.get("session") == session_date and oc.get("high") not in (None, "", 0) and oc.get("low") not in (None, "", 0):
            high, low = float(oc["high"]), float(oc["low"])
            if not 0 < low <= high: high = low = None
        cleaned, fixes = despike(rows, ref_rows, high, low)
    except BAR_ERRORS:
        return data, []
    if not fixes and cleaned == rows: return data, []
    for f in fixes: f["reference"] = INDICES[ref_key][0] if ref_key and f["kind"] == "spike" else None
    return {**data, "candles": {**data["candles"], "rows": cleaned}}, fixes


def trigger_times(rows, upper, lower):
    """(bullish_met_at, bearish_met_at): end time of the first bar in the CURRENT run of completed closes beyond a
    trigger, or None. A close back inside ends the run, so a stale crossing is never reported as live."""
    def run(beyond):
        first = None
        for r in reversed(rows):
            if not beyond(r["close"]): break
            first = r
        return (instant(first["at"]) + timedelta(minutes=5)).isoformat() if first else None
    return run(lambda c: c > upper), run(lambda c: c < lower)


def record_flags(data, previous_session, prev, levels, current, phase, session_date):
    """52-week and record context, or None. 52-week high/low: NSE allIndices (yearHigh/yearLow) for Nifty 50/Bank Nifty,
    Yahoo's fiftyTwoWeekHigh for Sensex. Record closes/highs: Yahoo daily history (about 10 years, unofficial)."""
    yr, hist = data.get("year_range") or {}, data.get("record_history") or {}
    out = {}
    try:
        if yr.get("high"):
            high = price(yr["high"]); out.update(year_high=high, year_high_source=str(yr.get("source") or "NSE"))
            out["previous_high_is_year_high"] = abs(prev["high"] - high) <= max(0.05, high * 1e-6)
            today = levels.get("session_high")
            if phase != "preopen" and today is not None:
                out["session_high_is_year_high"] = today >= high - max(0.05, high * 1e-6)
                out["session_high_above_year_high"] = today > high + max(0.05, high * 1e-6)
            if current is not None:
                out["pct_below_year_high"] = round((1 - current / high) * 100, 2)
        if hist.get("max_close_before_previous") and hist.get("first"):
            out["history_from"] = hist["first"]; out["history_basis"] = str(hist.get("basis") or "Yahoo daily history (unofficial)")
            out["previous_record_close"] = prev["close"] > price(hist["max_close_before_previous"][1])
            before_today = max(price(hist["max_close_before_previous"][1]), prev["close"])
            if phase == "close" and current is not None:
                out["record_close_today"] = current > before_today
            if hist.get("max_high_before_today") and phase != "preopen" and levels.get("session_high") is not None:
                out["record_high_today"] = levels["session_high"] > max(price(hist["max_high_before_today"][1]), prev["high"])
    except (Block, KeyError, TypeError, ValueError, IndexError):
        return None
    return out or None


def index_report(key, data, session, now, phase, start, end):
    if data.get("index_id") != key or data.get("price_basis") != "spot_index_points":
        raise Block("INDEX: exact index identity and spot-index price basis required")
    if INDICES[key][1] not in session.get("exchanges", []):
        raise Block("CALENDAR: selected exchange not covered")
    provenance(data, now, "index snapshot", 120)
    prior = data["previous"]
    provenance(prior, now, "previous session")
    if prior["session"] != session["previous_session"] or instant(prior["as_of"]).date() < date.fromisoformat(prior["session"]):
        raise Block("PREVIOUS: exact previous trading session required")
    prev = ohlc(prior)
    gaps, rows, bar_state = [], [], "not_due"
    quote, why, horizon = None, None, None
    if phase == "intraday" and data.get("quote") is not None:
        quote, why = live_quote(data["quote"], now, start)
        if quote and quote["provider"] != "NSE" and instant(quote["as_of"]) <= now - timedelta(minutes=3):
            horizon = instant(quote["as_of"])  # delayed public feed: judge its bars up to its own latest time
    if phase != "preopen":
        rows, bar_state, note = intraday_bars(data, start, end, now, horizon)
        if note: gaps.append(note)
    expected = expected_bars(start, end, horizon or now)
    levels = {"previous_high": prev["high"], "previous_low": prev["low"], "previous_close": prev["close"]}
    current = rows[-1]["close"] if rows else prev["close"]
    label = "last_completed_candle_close" if rows else "previous_session_close"
    value_at = (instant(rows[-1]["at"]) + timedelta(minutes=5)).isoformat() if rows else None
    close_basis, day_range = None, None
    if phase == "intraday" and data.get("quote") is not None:
        if quote:
            current = quote["value"]; label = "fresh_index_quote"; value_at = quote["as_of"]
            if quote["high"] is not None: day_range = quote
        else: gaps.append(why)
    if phase == "close" and data.get("official_close"):
        oc = data["official_close"]
        if oc.get("session") == session["date"]:
            current = price(oc["value"]); label = "official_daily_close"; value_at = end.isoformat()
            close_basis = str(oc.get("basis") or oc.get("source") or "")
            # The provider's own day high/low (NSE's daily report; Yahoo's daily candle for Sensex) beats the extremes of
            # its 5-minute bars, which can carry a bad tick. A missing open falls back to the first bar's open.
            if all(oc.get(k) not in (None, "", 0) for k in ("high", "low")):
                rng = {"open": price(oc["open"]) if oc.get("open") not in (None, "", 0) else None, "high": price(oc["high"]), "low": price(oc["low"])}
                if rng["low"] <= current <= rng["high"] and (rng["open"] is None or rng["low"] <= rng["open"] <= rng["high"]): day_range = rng
    if phase == "close" and label == "last_completed_candle_close" and bar_state == "incomplete":
        label = "last_available_bar_close"
    if day_range is not None:
        levels.update(session_open=day_range["open"] or (rows[0]["open"] if rows else None), session_high=day_range["high"], session_low=day_range["low"])
        if levels["session_open"] is None: del levels["session_open"]
    elif rows:
        levels.update(session_open=rows[0]["open"], session_high=max(r["high"] for r in rows), session_low=min(r["low"] for r in rows))
    window = None
    if len(rows) >= 3 and len(expected) >= 3 and [instant(r["at"]) for r in rows[:3]] == expected[:3]:
        levels.update(opening_range_high=max(r["high"] for r in rows[:3]), opening_range_low=min(r["low"] for r in rows[:3]))
        window = instant(rows[0]["at"]).strftime("%H:%M") + "-" + (instant(rows[2]["at"]) + timedelta(minutes=5)).strftime("%H:%M")
    upper_basis = "opening_range_high" if levels.get("opening_range_high", -math.inf) > levels["previous_high"] else "previous_high"
    lower_basis = "opening_range_low" if levels.get("opening_range_low", math.inf) < levels["previous_low"] else "previous_low"
    upper, lower = levels[upper_basis], levels[lower_basis]
    # Position uses the latest completed 5-minute close; with gaps in the bars it uses the latest quote (labelled).
    evidence_price, condition_basis = None, "latest completed five-minute close against previous-session/opening range"
    if rows and bar_state == "complete":
        evidence_price = rows[-1]["close"]
    elif phase == "close" and label == "official_daily_close":
        evidence_price, condition_basis = current, "session close against previous-session/opening range"
    elif quote:
        evidence_price, condition_basis = quote["value"], "latest quote against previous-session/opening range (5-minute bars incomplete)"
    elif rows:
        evidence_price = rows[-1]["close"]; condition_basis = "latest available five-minute close (bars incomplete)"
    condition = "PREOPEN_CONTEXT" if phase == "preopen" else "AWAITING_COMPLETED_BAR" if evidence_price is None else "ABOVE_REFERENCE_RANGE" if evidence_price > upper else "BELOW_REFERENCE_RANGE" if evidence_price < lower else "INSIDE_REFERENCE_RANGE"
    bull_at, bear_at = trigger_times(rows, upper, lower) if bar_state == "complete" else (None, None)
    try:
        br = breadth(data["breadth"], session, now, phase)
        if br["status"] != "COMPLETE": gaps.append("Constituent breadth is incomplete or unavailable")
    except (Block, KeyError, ValueError, TypeError, ArithmeticError):
        br = {"status": "UNAVAILABLE", "reason": "Missing or invalid sourced constituent evidence"}; gaps.append("Constituent breadth unavailable")
    sma, history = None, []
    if "history" in data:
        try:
            history = daily_history(data["history"], session, now, prev)
        except (Block, KeyError, TypeError, ValueError):
            history = []; gaps.append("Daily history failed its cross-check; 20-day average and last-session change omitted")
        if len(history) >= 20:
            sma = float((sum(Decimal(str(v)) for _, v in history[-20:]) / 20).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))  # exact half-up, as published averages are
            if not math.isfinite(sma): raise Block("HISTORY: unsupported numeric range")
    if sma is None: gaps.append("20-observation historical moving average unavailable")
    reference = history[-2] if len(history) >= 2 else None
    last_session = {"date": prior["session"], **prev, "previous_date": reference[0] if reference else None,
                    "previous_close": reference[1] if reference else None,
                    "change_points": point_change(prev["close"], reference[1]) if reference else None,
                    "change_pct": percent_change(prev["close"], reference[1]) if reference else None}
    has_current = phase != "preopen" and (bool(rows) or label in ("fresh_index_quote", "official_daily_close"))
    records = record_flags(data, prior["session"], prev, levels, current if has_current else None, phase, session["date"])
    return {"index_id": key, "name": INDICES[key][0], "status": "PARTIAL" if gaps else "READY", "source": data["source"],
            "as_of": data["as_of"], "phase": phase, "session_date": session["date"], "value": current, "value_basis": label,
            "value_at": value_at, "completed_bars": len(rows), "expected_bars": len(expected) if phase != "preopen" else 0, "bars_through": horizon.isoformat() if horizon else None,
            "intraday_bars": bar_state, "quote_provider": quote["provider"] if quote else None, "quote_page": quote["page"] if quote else None,
            "close_basis": close_basis, "range_basis": ("quote" if quote and day_range is quote else "close_source" if day_range else "five_minute_bars" if rows else None),
            "history_basis": data.get("history", {}).get("basis") if isinstance(data.get("history"), dict) else None,
            "previous_close_date": prior["session"],
            "change_from_previous_close_points": point_change(current, prev["close"]) if has_current else None,
            "change_from_previous_close_pct": percent_change(current, prev["close"]) if has_current else None,
            "last_session": last_session, "opening_range_window": window,
            "condition": condition, "condition_basis": condition_basis,
            "levels": levels, "prior_sma20": sma, "breadth": br, "coverage_gaps": gaps, "records": records,
            "scenarios": [
                {"name": "bullish", "condition": "Completed five-minute close above", "level": upper, "level_basis": upper_basis, "invalidated_by": "Completed close back at or below the level", "met_at": bull_at},
                {"name": "bearish", "condition": "Completed five-minute close below", "level": lower, "level_basis": lower_basis, "invalidated_by": "Completed close back at or above the level", "met_at": bear_at},
                {"name": "range", "condition": "Completed closes remain between", "lower": lower, "upper": upper, "lower_basis": lower_basis, "upper_basis": upper_basis}],
            "trade_ready": False, "note": "Spot-index points only. No spot-index volume/VWAP, probability, executable contract, lot size or order is inferred."}


def vix_report(data, now, phase, session_date):
    """Latest India VIX observation and its change from the preceding observation; never blocks the report."""
    try:
        provenance(data, now, "India VIX", VIX_MAX_AGE)
        if data.get("symbol") != VIX_SYMBOL: raise Block("VIX: exact India VIX symbol required")
        rows = data["rows"]
        if not isinstance(rows, list) or not rows: raise Block("VIX: observations required")
        days = [date.fromisoformat(r["session"]) for r in rows]
        if days != sorted(set(days)) or days[-1] > now.date(): raise Block("VIX: ordered nonfuture sessions required")
        last, before = rows[-1], (rows[-2] if len(rows) >= 2 else None)
        close = price(last["close"])
        high, low = (price(last["high"]), price(last["low"])) if "high" in last and "low" in last else (None, None)
        if high is not None and not low <= close <= high: raise Block("VIX: contradictory range")
        previous = price(before["close"]) if before else None
        # Yahoo's ^INDIAVIX daily candle sometimes freezes at the open (18 Nov 2026: 15.52 all day vs NSE's 15.57 close).
        frozen = (str(data["source"]).lower().startswith("yahoo") and days[-1] == now.date() and "open" in last and high is not None
                  and price(last["open"]) == close and high != low)
        return {"status": "OBSERVED", "symbol": VIX_SYMBOL, "source": data["source"], "as_of": data["as_of"], "date": days[-1].isoformat(),
                "possibly_stale": frozen, "basis": data.get("basis"),
                "close": close, "high": high, "low": low, "previous_date": days[-2].isoformat() if before else None, "previous_close": previous,
                "change_points": point_change(close, previous) if before else None,
                "change_pct": published_pct(last, close, previous) if before else None,
                "provisional": days[-1].isoformat() == session_date and phase != "close",
                "at": instant(data["at"]).isoformat() if data.get("at") else None}
    except (Block, KeyError, ValueError, TypeError, AttributeError, ArithmeticError):
        return {"status": "UNAVAILABLE", "reason": "Missing, stale or invalid India VIX observation"}


def published_pct(row, close, previous):
    """The source's own percent change (NSE computes it from unrounded levels, so a 2-decimal VIX can differ by a few
    hundredths from one computed here) when it agrees with the rounded levels; otherwise the computed one."""
    mine = percent_change(close, previous)
    theirs = row.get("change_pct")
    if isinstance(theirs, (int, float)) and not isinstance(theirs, bool) and math.isfinite(theirs) and abs(theirs - mine) <= 0.05 + 1.0 / previous:
        return round(float(theirs), 2)
    return mine


def collect_vix(*, fetcher=None, clock=instant):
    """Public Yahoo daily chart for India VIX, using the same contract checks as the other public charts."""
    from public_market import collect_chart, request
    chart = collect_chart(VIX_SYMBOL, "1d", fetcher=fetcher or request, clock=clock)
    rows = [{"session": instant(b["at"]).date().isoformat(), **{k: b[k] for k in ("open", "high", "low", "close")}} for b in chart["bars"][-3:]]
    out = {"source": chart["source"], "symbol": VIX_SYMBOL, "as_of": chart["as_of"], "rows": rows}
    # Yahoo's daily candle for today can sit frozen at the open (18 Nov 2026: 15.52 all day, the 17 Nov close, so the
    # change read +0.00 against NSE's 15.57, +0.29%). Its quote (meta.regularMarketPrice, with its own trade time) is
    # current: use it for today's value and keep the previous candle's close as the base.
    try:
        import nse_live
        q = nse_live.yahoo_quote("INDIA VIX", fetcher=fetcher, clock=clock)
        day = q["as_of"][:10]
        if q.get("previous_day") and rows and day >= rows[-1]["session"]:
            kept = [r for r in rows if r["session"] < day]
            if kept and kept[-1]["session"] == q["previous_day"]:
                today = {"session": day, "close": q["last"]}
                if q.get("high") and q.get("low") and q["low"] <= q["last"] <= q["high"]:
                    today.update(high=q["high"], low=q["low"])
                if q.get("open"): today["open"] = q["open"]
                out["rows"] = kept + [today]
                out["basis"] = "Yahoo quote (regularMarketPrice) vs the previous daily candle's close"
    except (Block, KeyError, IndexError, TypeError, ValueError, OSError, ArithmeticError):
        pass  # the candle values stand (flagged when they look frozen)
    return out


def same_moment(bundle, result, start, end, now):
    """Sensex and Nifty 50 at the same five-minute bar end when the Sensex quote is a delayed Yahoo quote, so a ~15-minute
    lag is not read as Sensex outperforming. Nifty bars: the bundle's Nifty 50 candles, or ``comparison_bars`` collected
    for a Sensex-only report. Returns None when no common completed bar exists."""
    try:
        sx = next(r for r in result["indices"] if r["index_id"] == "sensex" and r["status"] != "BLOCKED")
        if sx["value_basis"] != "fresh_index_quote" or sx.get("quote_provider") == "NSE" or not sx.get("bars_through"):
            return None
        horizon = instant(sx["bars_through"])
        sx_rows = completed(bundle["indices"]["sensex"]["candles"]["rows"], start, end, now, strict=False)
        nifty = next((r for r in result["indices"] if r["index_id"] == "nifty" and r["status"] != "BLOCKED"), None)
        ref = bundle.get("comparison_bars") or {}
        n_rows = completed((bundle["indices"].get("nifty") or {}).get("candles", {}).get("rows") or ref.get("rows") or [], start, end, now, strict=False)
        n_prev = nifty["levels"]["previous_close"] if nifty else price(ref["previous_close"])
        ends = {instant(r["at"]) + timedelta(minutes=5): r for r in n_rows}
        common = [r for r in sx_rows if instant(r["at"]) + timedelta(minutes=5) <= horizon and instant(r["at"]) + timedelta(minutes=5) in ends]
        if not common: return None
        s_bar = common[-1]; at = instant(s_bar["at"]) + timedelta(minutes=5); n_bar = ends[at]
        s_prev = sx["levels"]["previous_close"]
        out = {"at": at.isoformat(), "sensex": s_bar["close"], "sensex_points": point_change(s_bar["close"], s_prev), "sensex_pct": percent_change(s_bar["close"], s_prev),
               "nifty": n_bar["close"], "nifty_points": point_change(n_bar["close"], n_prev), "nifty_pct": percent_change(n_bar["close"], n_prev),
               "basis": "Yahoo 5-minute bars (both indices, same bar end)", "sensex_quote_at": sx["value_at"]}
        if nifty and nifty["value_at"]:
            out.update(nifty_now=nifty["value"], nifty_now_pct=nifty["change_from_previous_close_pct"], nifty_now_at=nifty["value_at"])
        elif ref.get("now"):
            out.update(nifty_now=ref["now"]["value"], nifty_now_pct=ref["now"]["change_pct"], nifty_now_at=ref["now"]["as_of"])
        if out.get("nifty_now") and out.get("nifty_now_at") and (not nifty or nifty.get("quote_provider") == "NSE"):
            # Start from the freshest Sensex figure: the quote itself when a Nifty 50 bar ends within 2.5 minutes of it.
            base = (s_bar["close"], n_bar, at, "bar")
            q_at = instant(sx["value_at"]) if sx.get("value_at") and sx.get("value") else None
            near = [t for t in ends if q_at and t >= at and abs((t - q_at).total_seconds()) <= 150]
            if near:
                t = min(near, key=lambda t: abs((t - q_at).total_seconds()))
                base = (sx["value"], ends[t], q_at, "quote")
            if instant(out["nifty_now_at"]) > base[2]:
                out.update(sensex_estimate(sx_rows, ends, {"close": base[0]}, base[1], s_prev, out["nifty_now"], out["nifty_now_at"]),
                           sensex_estimate_from=base[2].isoformat(), sensex_estimate_base=base[3])
        return out
    except (StopIteration, Block, KeyError, TypeError, ValueError, ArithmeticError):
        return None


def sensex_ratio(sx_rows, nifty_ends):
    """Sensex/Nifty 50 ratio of five-minute moves fitted on today's common completed bars (least squares through zero).
    1.0 when fewer than six common moves; clamped to 0.8-1.2 so one odd bar cannot swing the estimate."""
    s = {instant(r["at"]) + timedelta(minutes=5): r["close"] for r in sx_rows}
    n = {t: r["close"] for t, r in nifty_ends.items()}
    pairs = []
    for t in sorted(set(s) & set(n)):
        u = t - timedelta(minutes=5)
        if u in s and u in n: pairs.append((n[t] / n[u] - 1, s[t] / s[u] - 1))
    den = sum(x * x for x, _ in pairs)
    if len(pairs) < 6 or den <= 0: return 1.0, 0
    return min(1.2, max(0.8, sum(x * y for x, y in pairs) / den)), len(pairs)


def sensex_estimate(sx_rows, nifty_ends, s_bar, n_bar, s_prev, nifty_now, nifty_now_at):
    """'Sensex now' estimated from live NSE Nifty 50: Sensex at the shared bar moved by Nifty's change since that bar,
    scaled by the fitted Sensex/Nifty ratio. An estimate, never a Sensex quote."""
    ratio, moves = sensex_ratio(sx_rows, nifty_ends)
    value = s_bar["close"] * (1 + ratio * (nifty_now / n_bar["close"] - 1))
    return {"sensex_estimate": round(value, 2), "sensex_estimate_pct": percent_change(value, s_prev),
            "sensex_estimate_points": point_change(value, s_prev), "sensex_estimate_at": nifty_now_at,
            "sensex_ratio": round(ratio, 3), "sensex_ratio_moves": moves}


def summary_row(r, phase):
    """Comparable figures for one index: the last session before the open, the current session afterwards."""
    if phase == "preopen":
        s = r["last_session"]
        return {"index_id": r["index_id"], "name": r["name"], "basis": "last_session_close", "date": s["date"], "at": None,
                "value": s["close"], "change_points": s["change_points"], "change_pct": s["change_pct"],
                "compared_with": s["previous_date"], "high": s["high"], "low": s["low"], "source": r["source"]}
    lv = r["levels"]
    return {"index_id": r["index_id"], "name": r["name"], "basis": r["value_basis"], "date": r["session_date"], "at": r["value_at"],
            "value": r["value"], "change_points": r["change_from_previous_close_points"], "change_pct": r["change_from_previous_close_pct"],
            "compared_with": r["previous_close_date"], "high": lv.get("session_high"), "low": lv.get("session_low"), "source": r["source"]}


def report(command, bundle, now, phase="auto"):
    command = command.removeprefix("/")
    if command not in COMMANDS: raise Block("COMMAND: unsupported daily briefing")
    selected = list(INDICES) if command == "india-daily" else [command]
    def has_fixture(value):
        if isinstance(value, dict):
            if any(s in str(value.get("source", "")).lower() for s in ("synthetic", "fictional")): return True
            return any(has_fixture(v) for v in value.values())
        return isinstance(value, list) and any(has_fixture(v) for v in value)
    result = {"command": "/" + command, "as_of": now.isoformat(), "date": now.date().isoformat(),
              "mode": "SYNTHETIC_DEMO" if bundle.get("synthetic") is True or has_fixture(bundle) else "SOURCED_ANALYSIS",
              "submitted": False, "indices": [], "events": [], "coverage_gaps": []}
    start, end, phase = session_bounds(bundle["session"], now, phase)
    result["phase"] = phase
    result["session"] = {"date": bundle["session"]["date"], "previous_session": bundle["session"]["previous_session"],
                         "open": bundle["session"]["open"], "close": bundle["session"]["close"]}
    for key in selected:
        try:
            data, fixes = clean_bars(key, bundle["indices"][key], bundle, phase, bundle["session"]["date"])
            item = index_report(key, data, bundle["session"], now, phase, start, end)
            if fixes: item["bar_fixes"] = fixes
        except Block as exc:
            item = {"index_id": key, "name": INDICES[key][0], "status": "BLOCKED", "reason": str(exc), "trade_ready": False}
        except (KeyError, ValueError, TypeError, ArithmeticError):
            item = {"index_id": key, "name": INDICES[key][0], "status": "BLOCKED", "reason": "Missing, stale, inconsistent or wrong-session index evidence", "trade_ready": False}
        result["indices"].append(item)
    try:
        events = bundle["events"]; provenance(events, now, "events", 3600)
        if events["session"] != bundle["session"]["date"] or events["coverage"] not in {"partial", "reviewed_complete"}:
            raise Block("EVENTS: dated reviewed coverage required")
        for e in events["items"]:
            if not e.get("source") or not e.get("headline") or instant(e["published_at"]) > now:
                raise Block("EVENTS: invalid release metadata")
            result["events"].append({k: e[k] for k in ("headline", "source", "published_at")})
        if events["coverage"] != "reviewed_complete": result["coverage_gaps"].append("Events coverage is partial")
        result["event_source"] = events["source"]
    except (Block, KeyError, ValueError, TypeError):
        result["events"] = []; result["coverage_gaps"].append("Event calendar unavailable; no event clearance asserted")
    if "vix" in bundle:
        result["vix"] = vix_report(bundle["vix"], now, phase, bundle["session"]["date"])
    elif "vix_error" in bundle:
        result["vix"] = {"status": "UNAVAILABLE", "reason": str(bundle["vix_error"])}
    else:
        result["vix"] = None
    for name in ("calendar", "previous_context", "market_breadth", "broader_market", "sources_used", "nse_live_at", "news"):
        if bundle.get(name) is not None: result[name] = bundle[name]
    next_session = (result.get("calendar") or {}).get("next_trading_day")
    for item in result["indices"]:
        if item["status"] != "BLOCKED": item["next_session_date"] = next_session
    available = [r for r in result["indices"] if r["status"] != "BLOCKED"]
    result["context_errors"] = {str(k): str(v) for k, v in bundle.get("context_errors", {}).items()}
    result["coverage_gaps"].extend(result["context_errors"].values())
    result["status"] = "BLOCKED" if not available else "READY" if all(r["status"] == "READY" for r in result["indices"]) and not result["coverage_gaps"] else "PARTIAL"
    result["coverage"] = {"requested": len(selected), "available": len(available), "blocked": len(selected) - len(available)}
    result["same_moment"] = same_moment(bundle, result, start, end, now)
    if bundle.get("drivers"): result["drivers"] = bundle["drivers"]
    result["comparison"] = [summary_row(r, phase) for r in available]
    return result


# ---------------------------------------------------------------- rendering

def text_cell(value):
    return html.escape(str(value)).replace("\n", " ").replace("\r", " ").replace("|", "\\|").replace("[", "\\[").replace("]", "\\]")


def day(value):
    d = date.fromisoformat(str(value)[:10]); return f"{d.day} {d:%b}"


def long_day(value):
    d = date.fromisoformat(str(value)[:10]); return f"{d:%a} {d.day} {d:%b} {d.year}"


def clock_time(value):
    return instant(value).strftime("%H:%M")


def same_minute(a, b):
    """True when two timestamps fall in the same IST minute (one 'as of' time is then enough)."""
    try: return a is not None and b is not None and clock_time(a) == clock_time(b) and str(a)[:10] == str(b)[:10]
    except (Block, TypeError, ValueError): return False


def stamp(at, ref=None):
    """' (HH:MM IST)' for a context line, or '' when it is the headline's own time."""
    return "" if same_minute(at, ref) else f" ({clock_time(at)} IST)"


def headline_time(r):
    """The headline block's single 'as of' time: the NSE live level's time, else None (times stay on each line)."""
    if (r.get("status") != "BLOCKED" and r.get("phase") == "intraday" and r.get("value_basis") == "fresh_index_quote"
            and r.get("quote_provider") == "NSE"):
        return r["value_at"]
    return None


def pts(value):
    return "n/a" if value is None else f"{value:,.2f}"


def change_text(points, pct):
    return "change n/a" if points is None or pct is None else f"{points:+,.2f} ({pct:+.2f}%)"


def is_yahoo(r):
    return str(r.get("source", "")).lower().startswith("yahoo")


def display_name(r, short=False):
    if r["index_id"] == "sensex" and is_yahoo(r):
        return "Sensex (Yahoo)" if short else "Sensex (Yahoo data; no official BSE feed)"
    return r["name"]


def level_name(basis, r):
    prev, today = day(r["previous_close_date"]), day(r["session_date"])
    window = (r.get("opening_range_window") or "") + " opening-range"
    return {"previous_high": prev + " high", "previous_low": prev + " low", "previous_close": prev + " close",
            "session_open": today + " open", "session_high": today + " high so far" if r["phase"] == "intraday" else today + " high",
            "session_low": today + " low so far" if r["phase"] == "intraday" else today + " low",
            "opening_range_high": window.strip() + " high", "opening_range_low": window.strip() + " low"}.get(basis, basis.replace("_", " "))


def quote_age_minutes(r):
    try: return int((instant(r["as_of"]) - instant(r["value_at"])).total_seconds() // 60)
    except (KeyError, TypeError, ValueError, Block): return 0


def provider_label(r):
    """Plain source of the headline value, with its age when it is more than a few minutes old."""
    if r["value_basis"] == "fresh_index_quote":
        age = quote_age_minutes(r)
        if r.get("quote_provider") == "NSE": return "NSE live index level" + (f", {age} min old" if age >= 3 else "")
        return "Yahoo quote" + (f", about {age} min delayed" if age >= 3 else ", may be delayed")
    if r["value_basis"] == "last_available_bar_close":
        return "last available 5-minute bar; bars incomplete"
    return "last completed 5-minute bar"


def close_tag(r):
    basis = str(r.get("close_basis") or "").lower()
    if "nse" in basis: return " (NSE closing value)"
    if r["index_id"] == "sensex" or "yahoo" in basis: return " (Yahoo daily close; unofficial, BSE publishes the official value)"
    return ""


def headline(r):
    """One dated, plain-language sentence for the index's latest figure."""
    s = r["last_session"]
    if r["phase"] == "preopen":
        line = f"Last close on {day(s['date'])}: **{pts(s['close'])}**, {change_text(s['change_points'], s['change_pct'])}"
        line += f" vs {day(s['previous_date'])}." if s["previous_date"] else " (earlier close not supplied)."
        return line + f" {day(s['date'])} range: {pts(s['low'])}-{pts(s['high'])}."
    change = change_text(r["change_from_previous_close_points"], r["change_from_previous_close_pct"])
    prev = f"{day(r['previous_close_date'])} close {pts(r['levels']['previous_close'])}"
    if r["value_at"] is None:
        return (f"No intraday level for {day(r['session_date'])} yet (public feeds have not delivered one). "
                f"Last close on {day(s['date'])}: **{pts(s['close'])}**, {change_text(s['change_points'], s['change_pct'])}"
                + (f" vs {day(s['previous_date'])}." if s["previous_date"] else "."))
    rng = ""
    if "session_high" in r["levels"]:
        rng = f" {'Range so far' if r['phase'] == 'intraday' else day(r['session_date']) + ' range'}: {pts(r['levels']['session_low'])}-{pts(r['levels']['session_high'])}."
    if r["phase"] == "close":
        if r["value_basis"] == "official_daily_close":
            return f"{day(r['session_date'])} close: **{pts(r['value'])}**{close_tag(r)}, {change} vs {prev}.{rng}"
        basis = (f" (last available 5-minute bar at {clock_time(r['value_at'])}; bars incomplete, not the official close)"
                 if r["value_basis"] == "last_available_bar_close" else " (last 5-minute bar; official close may differ slightly)")
        return f"{day(r['session_date'])} close{basis}: **{pts(r['value'])}**, {change} vs {prev}.{rng}"
    return f"At {clock_time(r['value_at'])} IST ({provider_label(r)}): **{pts(r['value'])}**, {change} vs {prev}.{rng}"


PLAIN_LEVEL = {"previous_low": "the previous session's low", "previous_high": "the previous session's high",
               "opening_range_low": "the low of the first 15 minutes", "opening_range_high": "the high of the first 15 minutes"}
GLOSSARY = ("Bullish/bearish here only mean a completed 5-minute close above the upper level or below the lower level - "
            "a description of price so far, not a forecast.")
GLOSSARY_OR = "How to read this: the opening range is the high and low of the first 15 minutes (09:15-09:30 IST). "


def position(r):
    upper, lower = r["scenarios"][2]["upper"], r["scenarios"][2]["lower"]
    ub, lb = level_name(r["scenarios"][2]["upper_basis"], r), level_name(r["scenarios"][2]["lower_basis"], r)
    lead = "Finished" if r["phase"] == "close" else "Now"
    text = {"PREOPEN_CONTEXT": f"Reference range for today: {pts(lower)} ({lb}) to {pts(upper)} ({ub}).",
            "AWAITING_COMPLETED_BAR": "Position: waiting for a completed 5-minute bar or a fresh quote.",
            "ABOVE_REFERENCE_RANGE": f"{lead} above the reference range: over {pts(upper)} ({ub}).",
            "BELOW_REFERENCE_RANGE": f"{lead} below the reference range: under {pts(lower)} ({lb}).",
            "INSIDE_REFERENCE_RANGE": f"{lead} inside the reference range {pts(lower)} ({lb}) to {pts(upper)} ({ub})."}[r["condition"]]
    met = met_side(r)
    if met and r["phase"] == "intraday":
        side, level, basis, at = met
        key = r["scenarios"][0 if side == "bullish" else 1]["level_basis"]
        text = (f"{side.capitalize()} condition met at {clock_time(at)} IST: every completed 5-minute close since then is "
                f"{'above' if side == 'bullish' else 'below'} {pts(level)} ({basis}), {PLAIN_LEVEL.get(key, 'the reference level')} - "
                f"a sign of {'strength' if side == 'bullish' else 'weakness'}.")
        back = r["value"] > level if side == "bearish" else r["value"] < level
        if r["value_basis"] == "fresh_index_quote" and back:
            text = text[:-1] + (f"; the latest value {pts(r['value'])} (the headline level) is back {'above' if side == 'bearish' else 'below'} it, "
                                "not yet a completed close.")
        return text
    if "latest quote" in r.get("condition_basis", "") and r["condition"] not in ("PREOPEN_CONTEXT", "AWAITING_COMPLETED_BAR"):
        text = text[:-1] + " (judged on the latest quote because 5-minute bars are incomplete)."
    elif r["phase"] == "intraday" and r["condition"] == "INSIDE_REFERENCE_RANGE" and r["value_basis"] == "fresh_index_quote":
        side = "below" if r["value"] < lower else "above" if r["value"] > upper else None
        if side:
            text = (f"Last completed 5-minute close is inside the reference range {pts(lower)} ({lb}) to {pts(upper)} ({ub}); the latest "
                    f"value {pts(r['value'])} (the headline level) is {side} {pts(lower if side == 'below' else upper)}, "
                    "not yet confirmed by a completed close.")
    return text


def met_side(r):
    """(side, level, level name, met_at) when price is beyond a watch trigger on completed bars, else None."""
    up, down = r["scenarios"][0], r["scenarios"][1]
    if r["condition"] == "ABOVE_REFERENCE_RANGE" and up.get("met_at"):
        return "bullish", up["level"], level_name(up["level_basis"], r), up["met_at"]
    if r["condition"] == "BELOW_REFERENCE_RANGE" and down.get("met_at"):
        return "bearish", down["level"], level_name(down["level_basis"], r), down["met_at"]
    return None


def watch_core(r, short=False, after_position=False):
    """The intraday/pre-open watch clause: the generic conditions, or what is already met and what voids it."""
    up, down = r["scenarios"][0], r["scenarios"][1]
    ub, lb = level_name(up["level_basis"], r), level_name(down["level_basis"], r)
    met = met_side(r) if r["phase"] == "intraday" else None
    if met:
        side, level, basis, at = met
        other = (f"the bullish signal needs a completed close above {pts(up['level'])} ({ub})" if side == "bearish"
                 else f"the bearish signal needs a completed close below {pts(down['level'])} ({lb})")
        if after_position:  # the position line already said when it was met
            return (f"after a completed 5-minute close back {'above' if side == 'bearish' else 'below'} {pts(level)}, the "
                    f"{side} condition no longer applies; {other}")
        return (f"{side} condition met at {clock_time(at)} IST; after a completed 5-minute close back {'above' if side == 'bearish' else 'below'} "
                f"{pts(level)}, it no longer applies; {other}")
    if r["phase"] == "intraday" and r["condition"] in ("ABOVE_REFERENCE_RANGE", "BELOW_REFERENCE_RANGE"):
        bear = r["condition"] == "BELOW_REFERENCE_RANGE"
        level, basis = (down["level"], lb) if bear else (up["level"], ub)
        return (f"the latest price is {'below the bearish' if bear else 'above the bullish'} trigger {pts(level)} ({basis}) but no completed "
                f"5-minute close confirms it yet; {'bullish above ' + pts(up['level']) + ' (' + ub + ')' if bear else 'bearish below ' + pts(down['level']) + ' (' + lb + ')'}")
    if short: return f"bullish above {pts(up['level'])} ({ub}), bearish below {pts(down['level'])} ({lb})"
    return (f"bullish on a completed 5-minute close above {pts(up['level'])} ({ub}), bearish below {pts(down['level'])} ({lb}); "
            "after a close back between them the signal no longer applies, and the index is range-bound")


def vix_line(v, ref=None):
    if not v: return None
    if v["status"] != "OBSERVED": return "India VIX: unavailable."
    label = f"{day(v['date'])}, provisional" if v["provisional"] else day(v["date"])
    if v.get("at") and same_minute(v["at"], ref): label = "same time, provisional" if v["provisional"] else "same time"
    elif v.get("at"): label = f"{day(v['date'])} {clock_time(v['at'])} IST" + (", provisional" if v["provisional"] else "")
    change = f", {change_text(v['change_points'], v['change_pct'])} vs {day(v['previous_date'])}" if v["previous_date"] else ""
    if v.get("source") == "nse_ind_close_all": label += ", NSE official close"
    stale = " (Yahoo, unofficial: its value equals today's open, so it may be stale)" if v.get("possibly_stale") else ""
    return f"India VIX ({label}): {pts(v['close'])}{change}{stale}."


def sma_text(r):
    sma = r["prior_sma20"]
    if sma is None: return None
    s = r["last_session"]; gap = (s["close"] / sma - 1) * 100
    return f"20-day average of closes to {day(s['date'])}: {pts(sma)} ({day(s['date'])} close {abs(gap):.1f}% {'above' if gap >= 0 else 'below'} it)."


def summary_table(result):
    intraday = result["phase"] == "intraday"
    lines = [f"| Index | Date | {'Level' if intraday else 'Close'} | Change | Change % | {'Range so far' if intraday else 'Day range'} |",
             "| --- | --- | ---: | ---: | ---: | --- |"]
    names = {r["index_id"]: r for r in result["indices"]}
    for c in result["comparison"]:
        when = day(c["date"]) + (f" {clock_time(c['at'])}" if intraday and c["at"] else "")
        r = names[c["index_id"]]
        if intraday and r.get("value_basis") == "fresh_index_quote" and r.get("quote_provider") != "NSE" and quote_age_minutes(r) >= 3:
            when += f" (~{quote_age_minutes(r)} min behind)"
        rng = f"{pts(c['low'])}-{pts(c['high'])}" if c["low"] is not None else "n/a"
        chg = "n/a" if c["change_points"] is None else f"{c['change_points']:+,.2f}"
        pct = "n/a" if c["change_pct"] is None else f"{c['change_pct']:+.2f}%"
        lines.append(f"| {display_name(names[c['index_id']], short=True)} | {when} | {pts(c['value'])} | {chg} | {pct} | {rng} |")
    v = result.get("vix")
    if v and v["status"] == "OBSERVED":
        rng = f"{pts(v['low'])}-{pts(v['high'])}" if v["low"] is not None else "n/a"
        chg = "n/a" if v["change_points"] is None else f"{v['change_points']:+,.2f}"
        pct = "n/a" if v["change_pct"] is None else f"{v['change_pct']:+.2f}%"
        when = day(v["date"]) + (f" {clock_time(v['at'])}" if intraday and v.get("at") else "")
        lines.append(f"| India VIX | {when}{' (provisional)' if v['provisional'] else ''} | {pts(v['close'])} | {chg} | {pct} | {rng} |")
    return lines


def source_phrase(result):
    """Short plain-language source description for the --brief notes line."""
    used = result.get("sources_used") or []
    rows = [r for r in result["indices"] if r["status"] != "BLOCKED"]
    if used:
        parts = []
        for u in used:
            if u.startswith("NSE live"): parts.append(u.replace("NSE live index data (www.nseindia.com, ", "NSE live data (nseindia.com, "))
            elif u.startswith("NSE daily"): parts.append("NSE daily closes")
            elif u.startswith("Yahoo"):
                late = [f"{r['name']} ~{quote_age_minutes(r)} min delayed" for r in rows
                        if r["value_basis"] == "fresh_index_quote" and r.get("quote_provider") != "NSE" and quote_age_minutes(r) >= 3]
                parts.append("Yahoo public data for " + u.split(" for ", 1)[-1] + ("; " + ", ".join(late) if late else ""))
        return " + ".join(parts)
    if any(is_yahoo(r) for r in rows): return "Yahoo public data, delayed/unofficial"
    if result["mode"] == "SYNTHETIC_DEMO": return "synthetic demo data"
    return "data from the supplied input file"


def data_notes(result):
    notes, rows = [], [r for r in result["indices"] if r["status"] != "BLOCKED"]
    if result.get("sources_used"):
        notes.append("Sources: " + "; ".join(result["sources_used"]) + ".")
        late = [r for r in rows if r["value_basis"] == "fresh_index_quote" and r.get("quote_provider") != "NSE" and quote_age_minutes(r) >= 3]
        for r in late:
            notes.append(f"{r['name']} via Yahoo was about {quote_age_minutes(r)} minutes behind at report time: free BSE data is delayed and BSE "
                         "refuses scripts, so no fresher free source is used; its 5-minute bars are judged up to the quote's time.")
        if any(r["index_id"] == "sensex" and is_yahoo(r) for r in rows) and not any(r["index_id"] == "sensex" for r in late):
            notes.append("Sensex is Yahoo data, not an official BSE feed (BSE does not serve scripts).")
    elif any(is_yahoo(r) for r in rows):
        note = "Index data: Yahoo Finance public charts (unofficial; may be delayed or revised; research only)."
        if any(r["index_id"] == "sensex" for r in rows): note += " Sensex is Yahoo data, not an official BSE feed."
        notes.append(note)
    elif rows:
        notes.append("Index data: " + ", ".join(sorted({text_cell(r["source"]) for r in rows})) + ".")
    v = result.get("vix")
    if v and v["status"] == "OBSERVED":
        src = str(v["source"]).lower()
        notes.append("India VIX: " + ("Yahoo Finance public chart (^INDIAVIX); NSE's live and daily index data were unavailable this run." if src.startswith("yahoo")
                                      else "NSE daily index report (official close)." if src == "nse_ind_close_all"
                                      else "NSE live index data." if "nse" in src else text_cell(v["source"]) + "."))
    elif v: notes.append("India VIX: " + text_cell(v.get("reason", "unavailable")) + ".")
    else: notes.append("India VIX: not collected by this input route.")
    errors = result.get("context_errors", {})
    if "calendar" in errors and not (result.get("calendar") or {}).get("today_is_trading_day"): notes.append("Session times and the previous-session date come from the data provider, not a verified exchange calendar.")
    for key, value in errors.items():
        if key in INDICES: notes.append(f"{INDICES[key][0]}: {text_cell(value)}.")
        elif key not in {"public_feed", "calendar"}: notes.append(text_cell(value).rstrip(".") + ".")
    for r in rows:
        if r.get("intraday_bars") in ("incomplete", "unavailable"):
            what = ("received " + str(r["completed_bars"]) + " of " + str(r["expected_bars"])) if r["intraday_bars"] == "incomplete" else "not received"
            shown = {"fresh_index_quote": "the level shown is the latest quote", "official_daily_close": "the close shown is the daily close"}.get(r["value_basis"], "the level shown is the latest bar received")
            notes.append(f"{r['name']}: intraday 5-minute bars {what}; {shown}, and the opening range is omitted when its bars are missing.")
    for r in rows:
        if r.get("bar_fixes"): notes.append(bar_fix_note(r))
    if result["phase"] == "close" and any(x.get("value_basis") in ("last_completed_candle_close", "last_available_bar_close") for x in rows):
        notes.append("Some closing figures use the last 5-minute bar, not the official published close.")
    without = [r["name"] for r in rows if r["breadth"].get("status") == "UNAVAILABLE" and r["index_id"] not in (result.get("market_breadth") or {})]
    if without and result["phase"] != "preopen": notes.append("Advance/decline breadth not available for " + ", ".join(without) + ".")
    no_sma = [r["name"] for r in rows if r["prior_sma20"] is None]
    if no_sma: notes.append("20-day average unavailable for " + ", ".join(no_sma) + " (fewer than 20 prior closes supplied).")
    bases = {}
    for r in rows:
        if r.get("history_basis") and r["prior_sma20"] is not None: bases.setdefault(r["history_basis"], []).append(r["name"])
    for basis, names in bases.items():
        notes.append(f"{' and '.join(names)} 20-day average{'s' if len(names) > 1 else ''}: {text_cell(basis)}.")
    if result["events"] and "Events coverage is partial" in result["coverage_gaps"]: notes.append("Event coverage is partial.")
    d = result.get("drivers")
    if d:
        checks = [f"{index_label(n)} est. {b.get('check_estimated_points', b['estimated_total_points']):+.1f} vs actual {b['actual_points']:+.1f} pts at "
                  f"{clock_time(b['actual_at'])} IST" + (" (same 5-minute bar)" if "check_estimated_points" in b else "")
                  for n, b in (d.get("indices") or {}).items() if b.get("actual_at") and reliable(b)]
        anchors = {n: (b.get("fit") or {}).get("anchor") for n, b in (d.get("indices") or {}).items()}
        groups = {}  # (factsheet, date) -> indices anchored to it (Sensex uses the Nifty 50 factsheet)
        for n, a in anchors.items():
            if a: groups.setdefault(({"NIFTY BANK": "Nifty Bank"}.get(a.get("index"), "Nifty 50"), a["as_of"]), []).append(index_label(n))
        anchored = sorted(groups)
        basis = ("weights estimated" + (": " + "; ".join(f"{' and '.join(names)} weights anchored to the NSE Indices {sheet} factsheet of {long_day(when)[4:]}"
                                                          for (sheet, when), names in groups.items())
                 + " (top-10 stock and sector weights, rolled forward by price), the rest of each sector split by a month of 5-minute data"
                 if anchored else " from a month of 5-minute data"))
        unanchored = [index_label(n) for n, a in anchors.items() if not a]
        if anchored and unanchored: basis += f"; {' and '.join(unanchored)} weights fitted from a month of 5-minute data (no official weights file)"
        nse_at = result.get("nse_live_at")
        off = [b for b in (d.get("indices") or {}).values()
               if checks and nse_at and b.get("actual_at") and clock_time(b["actual_at"]) != clock_time(nse_at) and reliable(b)]
        if off:
            # One snapshot time or a labelled difference: the check is at Yahoo's moment, not the NSE headline time.
            kinds = ((["the stock-price moment"] if any("check_estimated_points" not in b for b in off) else [])
                     + (["Sensex's shared 5-minute bar"] if any("check_estimated_points" in b for b in off) else []))
            checks[-1] += f"; check times are Yahoo's ({' and '.join(kinds)}), the index levels above are NSE at {clock_time(nse_at)} IST"
        notes.append("Drivers: NSE sector indices; stock point contributions (estimated) use Yahoo prices vs NSE previous closes with "
                     + basis + (f" (check: {'; '.join(checks)})" if checks else "") + ".")
        for n in ("NIFTY 50", "NIFTY BANK"):
            line = ((d.get("indices") or {}).get(n) or {}).get("anchor_line")
            if line: notes.append(line)
        if "SENSEX" in (d.get("indices") or {}):
            how = ("weights are the factsheet-anchored Nifty 50 weights of those 30 stocks, rescaled to 100%" if anchors.get("SENSEX")
                   else "their weights are fitted the same way")
            notes.append("Sensex points: BSE does not serve Sensex weights or its member list to scripts, so the 30 Sensex members are "
                         "identified from Sensex's own 5-minute moves against Nifty 50 stocks (a member outside Nifty 50 would be missed) and "
                         f"{how}; they are Sensex points, not Nifty 50 points.")
    m = result.get("same_moment") or {}
    if m.get("sensex_estimate") is not None and sensex_now_line(result) and result.get("phase") == "intraday":
        notes.append("Like-for-like check: " + same_moment_line(result))
    if m.get("sensex_estimate") is not None:
        notes.append(f"Estimated Sensex now: {estimate_base(m)} (Yahoo) moved by the live NSE Nifty 50 "
                     f"change since then, scaled by the Sensex/Nifty ratio {m['sensex_ratio']:.2f} "
                     + (f"fitted on {m['sensex_ratio_moves']} of today's 5-minute moves" if m.get("sensex_ratio_moves") else "(1:1; too few of today's 5-minute moves to fit)")
                     + "; an estimate, not a BSE figure.")
    if result.get("news") and result.get("phase") in ("intraday", "close"):
        ahead = (" After the close the cues look ahead: GIFT Nifty headlines since today's 15:30 close, US markets, gold, crude "
                 "and rupee headlines since today's open." if result["phase"] == "close" else "")
        notes.append(f"Possible reasons and cues: Google News RSS (and ET Markets RSS) headlines published since {news_since(result)}, "
                     "kept only when they name today's moving stocks or sectors or a macro driver; old news, corporate housekeeping "
                     "(ESOP grants, AGM/EGM, board-meeting notices) and multi-day stories against today's move are dropped, and "
                     "headlines that give a cause come first. They are the publishers' claims, not verified causes." + ahead)
    if any(record_line(r) for r in rows):
        notes.append("Records: 52-week highs from NSE (Sensex: Yahoo); record closes from Yahoo daily history (unofficial).")
    notes.append("Research only; no orders.")
    return notes


def bar_fix_note(r):
    """Which 5-minute bars were cut back as bad ticks, by how much, and what the day range uses instead."""
    parts = []
    for f in r["bar_fixes"][:3]:
        side = []
        if f["high_now"] != f["high"]: side.append(f"high {pts(f['high'])}")
        if f["low_now"] != f["low"]: side.append(f"low {pts(f['low'])}")
        if f["kind"] == "spike":
            ref = f" while {f['reference']}'s bar did not move with it" if f.get("reference") else ""
            parts.append(f"the {clock_time(f['at'])} bar's {' and '.join(side)} ({f['wick_pct']:.2f}% beyond its open and close{ref}) "
                         "was cut back to the bar's open/close")
        else:
            parts.append(f"the {clock_time(f['at'])} bar's {' and '.join(side)} went beyond the day's published high/low and was cut back to it")
    more = f" ({len(r['bar_fixes']) - 3} more bars likewise)" if len(r["bar_fixes"]) > 3 else ""
    basis = {"close_source": "the day's published high/low", "quote": "the live quote's high/low"}.get(r.get("range_basis"), "the cleaned bars")
    return (f"{r['name']}: bad tick in the 5-minute bars - {'; '.join(parts)}{more}. The opening range and pivots ignore it; "
            f"the day range uses {basis}.")


def index_label(name):
    return {"NIFTY 50": "Nifty 50", "NIFTY BANK": "Bank Nifty", "SENSEX": "Sensex"}.get(name, name)


def blocked_text(r):
    return f"{display_name(r)}: today's figures could not be confirmed from the public feeds this run; re-run in a few minutes."


def next_label(r):
    nxt = r.get("next_session_date")
    return long_day(nxt).rsplit(" ", 1)[0] if nxt else "the next session"


def levels_for(r):
    return f"Levels for {next_label(r)} (next session)" if r.get("next_session_date") else "Levels for the next session"


def pivot_line(r):
    """Classic floor pivots from one session's high/low/close: plain arithmetic many traders watch, not a forecast."""
    if r["phase"] == "close":
        lv = r["levels"]
        if "session_high" not in lv: return None
        h, l, c, when, src = lv["session_high"], lv["session_low"], r["value"], next_label(r), day(r["session_date"])
    elif r["phase"] == "preopen":
        s = r["last_session"]; h, l, c, when, src = s["high"], s["low"], s["close"], "today", day(s["date"])
    else:
        return None
    p = (h + l + c) / 3
    pts_ = {"S2": p - (h - l), "S1": 2 * p - h, "P": p, "R1": 2 * p - l, "R2": p + (h - l)}
    return (f"Classic pivots for {when} (from {src} high/low/close; arithmetic, not a forecast): "
            + ", ".join(f"{k} {pts(v)}" for k, v in pts_.items()) + ".")


def finalise_line(r):
    return f"Today's high, low and close become the reference levels for {next_label(r)} once the session ends at 15:30 IST."


def watch_line(r, finalise=True):
    """Conditional levels in one sentence; after the close, the levels the next session starts from."""
    up, down = r["scenarios"][0], r["scenarios"][1]
    if r["phase"] == "close":
        lv, today = r["levels"], day(r["session_date"])
        if "session_high" in lv:
            return (f"{levels_for(r)}: {today} high {pts(lv['session_high'])}, low {pts(lv['session_low'])}, "
                    f"close {pts(r['value'])}.")
        return f"{levels_for(r)}: {today} close {pts(r['value'])}; the day's high/low were not available."
    text = f"Watch (conditions, not orders): {watch_core(r, after_position=True)}."
    if r["phase"] == "intraday" and finalise:
        text += " " + (provisional_line(r) or finalise_line(r))
    return text


def provisional_line(r):
    """Mid-session 'tomorrow's levels': today's high/low so far and the last value, clearly provisional."""
    lv = r["levels"]
    if r["phase"] != "intraday" or "session_high" not in lv or r.get("value_at") is None: return None
    return (f"Provisional levels for {next_label(r)} if the session ended now: high {pts(lv['session_high'])}, low {pts(lv['session_low'])}, "
            f"last {pts(r['value'])}; final after 15:30 IST.")


def _sector_text(rows):
    return ", ".join(f"{x['name'].removeprefix('Nifty ')} {x['change_pct']:+.2f}%" for x in rows)


def _points_text(rows):
    return ", ".join(f"{x['symbol']} {x['points']:+.1f} pts ({x['change_pct']:+.2f}%)" for x in rows)


def reliable(block):
    """Use estimated point contributions only when the weight fit is good and they explain the actual move."""
    try:
        est, act = block.get("check_estimated_points", block["estimated_total_points"]), block["actual_points"]
        return block["fit"]["r_squared"] >= 0.9 and abs(est - act) <= max(10.0, abs(act) * 0.25)
    except (KeyError, TypeError):
        return False


def driver_line(result, key, head=None, ref=None):
    """One line on what is moving the index: NSE sectors, then estimated point drags/lifts (Nifty 50 points for Nifty,
    Sensex points for Sensex - never one relabelled as the other) or the heavyweights' moves (Bank Nifty). None when
    drivers were not collected. ``ref``: the headline time; the line drops its own time when it is the same minute."""
    d = result.get("drivers")
    if not d: return None
    when = stamp(d["at"], ref)
    falling = next(((r["change_from_previous_close_points"] or 0) < 0 for r in result["indices"] if r["status"] != "BLOCKED" and r["index_id"] == key), False)
    if key == "banknifty":
        b = (d.get("indices") or {}).get("NIFTY BANK")
        lv = d.get("sector_levels") or {}
        banks = [f"{n.removeprefix('Nifty ')} {lv[n]:+.2f}%" for n in ("Nifty PSU Bank", "Nifty Private Bank") if n in lv]
        if not b and not banks: return None
        parts = []
        if b and reliable(b):
            parts.append("heavyweights " + ", ".join(f"{x['symbol']} {x['change_pct']:+.2f}% ({x['points']:+.0f} pts)" for x in b["heavyweights"])
                         + " (estimated Bank Nifty points)")
        elif b:
            parts.append("heavyweights " + ", ".join(f"{x['symbol']} {x['change_pct']:+.2f}%" for x in b["heavyweights"]))
        if banks: parts.append("bank sub-indices " + ", ".join(banks))
        return f"{head or 'What' + chr(39) + 's moving it'}{when}: " + "; ".join(parts) + "."
    down = ("weakest sectors " + _sector_text(d["sectors_down"][:2])) if d.get("sectors_down") else None
    up = ("strongest sectors " + _sector_text(d["sectors_up"][:2])) if d.get("sectors_up") else None
    parts = [x for x in ((down, up) if falling else (up, down)) if x]  # the side that explains the move first
    name, unit = ("SENSEX", "Sensex") if key == "sensex" else ("NIFTY 50", "Nifty 50")
    n = (d.get("indices") or {}).get(name)
    if n and reliable(n):
        drags, lifts = n["negative"][:3], n["positive"][:3]
        stock = [("biggest drags " + _points_text(drags)) if drags else None, ("lifts " + _points_text(lifts)) if lifts else None]
        stock = [x for x in (stock if falling else stock[::-1]) if x]
        if stock: parts.append("; ".join(stock) + f" (estimated {unit} points)")
    elif n and key == "sensex":  # the point estimate failed its check: members' % moves only, no points
        moved = sorted({x["symbol"]: x for x in n["negative"] + n["positive"]}.values(), key=lambda x: -abs(x["change_pct"]))[:4]
        if moved:
            parts.append("largest Sensex-member moves " + ", ".join(f"{x['symbol']} {x['change_pct']:+.2f}%" for x in moved)
                         + " (% moves only; the Sensex point estimate did not match Sensex's own move closely enough)")
    if not parts: return None
    head = head or ("What's moving it" if key in ("nifty", "sensex") else "What's moving the market")
    return f"{head}{when}: " + "; ".join(parts) + "."


DRIVER_NAMES = {"nifty": "NIFTY 50", "sensex": "SENSEX", "banknifty": "NIFTY BANK", "market": "NIFTY 50"}


def driver_gap(result, key="market"):
    """'⚠ stock contributions not collected — rerun' when this run collected sector drivers but not the stocks driving
    the index (most often NSE refusing the previous-day bhavcopy). None when they are present or not part of this route."""
    if result.get("phase") not in ("intraday", "close"): return None
    d, errors = result.get("drivers"), result.get("context_errors") or {}
    if d is None and "drivers" not in errors: return None
    if d and (d.get("indices") or {}).get(DRIVER_NAMES.get(key, "NIFTY 50")): return None
    why = (d or {}).get("error") or ("stock-level data missing" if d else "drivers unavailable")
    return f"⚠ stock contributions not collected — rerun ({why}; the sector moves above are complete)."


def _news_item(h):
    who = h.get("publisher") or h.get("source") or "news"
    text = " ".join(str(h["headline"]).split())
    return (f"{h['symbol']}: " if h.get("symbol") else "") + f'"{text}" ({who}, {day(h["published_at"])} {clock_time(h["published_at"])} IST)'


def news_since(result):
    prev = (result.get("session") or {}).get("previous_session")
    return f"the {day(prev)} close" if prev else "the last close"


def reasons_line(result, key=None):
    """'Possible reasons (headlines, unverified)': up to three dated headlines on today's movers or the market, or the gap."""
    n = result.get("news")
    if not n or result.get("phase") not in ("intraday", "close"): return None
    rkey = "market" if len(result["indices"]) > 1 else key
    items, gaps = (n.get("reasons") or {}).get(rkey), n.get("gaps") or []
    if items:
        return "Possible reasons (headlines, unverified): " + "; ".join(_news_item(h) for h in items[:3]) + "."
    if items is None or f"reasons:{rkey}" in gaps or "news" in gaps:
        return "Possible reasons (headlines): the news search failed or ran out of time this run - a gap; the drivers above are from prices only."
    return f"Possible reasons (headlines): no dated headline since {news_since(result)} names today's movers - a gap, not an all-clear."


def cues_line(result):
    """'Cues': the newest dated headline on GIFT Nifty, US markets, gold, crude and the rupee since the last close; after
    the close, look-ahead cues for the next session."""
    n = result.get("news")
    if not n or result.get("phase") not in ("intraday", "close"): return None
    if result["phase"] == "close": return lookahead_cues_line(result, n)
    cues = n.get("cues")
    if not cues:
        return "Cues (GIFT Nifty, US markets, gold, crude, rupee): the news search failed or ran out of time this run - a gap."
    found = [f"{label} - {_news_item(h)}" for label, h in cues.items() if h]
    missing = [label for label, h in cues.items() if not h]
    if not found:
        return f"Cues: no dated headline since {news_since(result)} on {', '.join(missing)} - a gap."
    return (f"Cues (headlines since {news_since(result)}, unverified): " + "; ".join(found)
            + (f"; none found for {', '.join(missing)}" if missing else "") + ".")


def next_session_text(result):
    nxt = (result.get("calendar") or {}).get("next_trading_day")
    return f"{long_day(nxt)[:3]} {day(nxt)}" if nxt else "the next session"


def lookahead_cues_line(result, n):
    """After the close: what to watch for the next session - GIFT Nifty since today's close, US markets, gold, crude and
    the rupee since today's open - each the newest dated headline, or 'none yet'."""
    head = f"Cues for {next_session_text(result)} (look-ahead headlines, unverified; GIFT Nifty since today's 15:30 close, the rest since today's open)"
    cues = n.get("cues")
    if not cues:
        return f"{head}: the news search failed or ran out of time this run - a gap; check GIFT Nifty, US markets, crude, gold and the rupee before the open."
    parts = [f"{label} - {_news_item(h)}" if h else f"{label} - none yet" for label, h in cues.items()]
    return f"{head}: " + "; ".join(parts) + "."


def record_line(r):
    """52-week/record context in one sentence, only when the index is at or near its 52-week high or set a record."""
    rec = r.get("records")
    if not rec or r["status"] == "BLOCKED": return None
    lv, prev_day, today = r["levels"], day(r["previous_close_date"]), day(r["session_date"])
    src, start = rec.get("year_high_source", "NSE"), rec.get("history_from")
    since = long_day(start)[4:] if start else ""  # the first date of the history actually used, not a rounded year
    hist = f"highest close in Yahoo daily data from {since}, the start of the data used" if since else "highest close on record in the data used"
    parts = []
    if r["phase"] == "close" and rec.get("record_close_today"):
        parts.append(f"{today} close {pts(r['value'])} is a record close ({hist})")
    if rec.get("record_high_today"):
        parts.append(f"{today} high {pts(lv['session_high'])} is a record high (above every high in Yahoo daily data from {since})")
    elif rec.get("session_high_above_year_high"):
        was = f", the {prev_day} high" if rec.get("previous_high_is_year_high") else ""
        parts.append(f"{today} high {pts(lv['session_high'])} is a new 52-week high (above {pts(rec['year_high'])}{was}; {src})")
    elif rec.get("session_high_is_year_high"):
        parts.append(f"{today} high {pts(lv['session_high'])} is at the 52-week high ({src})")
    below = rec.get("pct_below_year_high")
    near = below is not None and 0 < below <= 2.0 and not rec.get("session_high_is_year_high")
    lead = "it closed" if r["phase"] == "close" else "now" if r["phase"] == "intraday" else "it last closed"
    distance = f"{lead} {below:.2f}% below" if near else None
    if r["phase"] != "close" and rec.get("previous_high_is_year_high") and not rec.get("session_high_is_year_high"):
        parts.append(f"{prev_day} high {pts(lv['previous_high'])} is the 52-week high ({src})" + (f", {distance} it" if distance else ""))
        distance = None
    if r["phase"] != "close" and rec.get("previous_record_close"):
        parts.append(f"{prev_day} close {pts(lv['previous_close'])} was a record close ({hist})")
    if distance:
        parts.append(f"{distance} the 52-week high {pts(rec['year_high'])} ({src})")
    if not parts: return None
    return "Records: " + "; ".join(parts) + "."


def same_moment_line(result):
    """When Sensex comes from a delayed feed, compare Sensex and Nifty 50 at one bar end instead of across times."""
    m = result.get("same_moment")
    if not m: return None
    sx = next((r for r in result["indices"] if r["index_id"] == "sensex" and r["status"] != "BLOCKED"), None)
    age = quote_age_minutes(sx) if sx else 0
    quote_at = m.get("sensex_quote_at")
    try: gap = abs((instant(quote_at) - instant(m["at"])).total_seconds()) if quote_at else None
    except (Block, TypeError, ValueError): gap = None
    # The quote and the latest common bar end are the same moment: one Sensex figure (the quote), not a second one.
    same = gap is not None and gap <= 120 and sx and sx.get("change_from_previous_close_pct") is not None
    sensex_pct = sx["change_from_previous_close_pct"] if same else m["sensex_pct"]
    text = (f"Sensex's free feed runs ~{age} min behind NSE, so compare like for like at the same 5-minute bar, ending "
            f"{clock_time(m['at'])} IST (Yahoo, both indices): Sensex {sensex_pct:+.2f}%, Nifty 50 {m['nifty_pct']:+.2f}%.")
    if m.get("nifty_now_at") and m.get("nifty_now_pct") is not None:
        text += f" Nifty 50 is now {m['nifty_now_pct']:+.2f}% ({clock_time(m['nifty_now_at'])} IST, NSE)."
    return text


def sensex_estimate_line(result):
    """'Estimated Sensex now' from live NSE Nifty 50, always labelled as an estimate (whole points: it is not a quote)."""
    m = result.get("same_moment")
    if not m or m.get("sensex_estimate") is None: return None
    return (f"Estimated Sensex now ≈ {round(m['sensex_estimate']):,} ({m['sensex_estimate_pct']:+.2f}%) at {clock_time(m['sensex_estimate_at'])} IST - "
            f"an estimate from live Nifty 50, not a Sensex quote ({estimate_base(m)}, moved by Nifty 50's NSE change "
            f"since then x {m['sensex_ratio']:.2f}, {ratio_basis(m)}).")


def sensex_now_line(result, r=None, bold=False):
    """ONE Sensex figure for the reader during market hours: the estimate from live Nifty 50, then the last (delayed)
    Yahoo quote it starts from, each with its time. None when there is no estimate (the quote headline is used)."""
    m = result.get("same_moment") or {}
    if m.get("sensex_estimate") is None: return None
    r = r or next((x for x in result["indices"] if x["index_id"] == "sensex" and x["status"] != "BLOCKED"), None)
    if not r or r.get("value_at") is None: return None
    value = f"{round(m['sensex_estimate']):,}"
    text = (f"Sensex ≈ {'**' + value + '**' if bold else value} ({m['sensex_estimate_pct']:+.2f}%) at {clock_time(m['sensex_estimate_at'])} IST "
            f"(estimate from live Nifty 50); last Yahoo quote {pts(r['value'])} ({r['change_from_previous_close_pct']:+.2f}%) at "
            f"{clock_time(r['value_at'])} IST, vs {day(r['previous_close_date'])} close {pts(r['levels']['previous_close'])}.")
    if "session_high" in r["levels"]:
        text += f" Range so far (Yahoo): {pts(r['levels']['session_low'])}-{pts(r['levels']['session_high'])}."
    return text


def sensex_lead(result, r, bold=True):
    """The Sensex lead lines: the single estimated figure when available, else the quote headline and the like-for-like line."""
    one = sensex_now_line(result, r, bold=bold) if r["phase"] == "intraday" else None
    if one: return [one]
    return [headline(r)] + [x for x in (same_moment_line(result), sensex_estimate_line(result)) if x]


def estimate_base(m):
    at = clock_time(m.get("sensex_estimate_from") or m["at"])
    return f"the {at} Sensex quote" if m.get("sensex_estimate_base") == "quote" else f"Sensex at the {at} bar"


def ratio_basis(m):
    return ("Sensex/Nifty ratio fitted on today's 5-minute bars" if m.get("sensex_ratio_moves")
            else "1:1, too few of today's 5-minute bars to fit a ratio")


def breadth_line(result, r, ref=None):
    b = (result.get("market_breadth") or {}).get(r["index_id"])
    if not b: return None
    when = f"{day(b['as_of'])} {clock_time(b['as_of'])} IST" if result["phase"] == "intraday" else day(b["as_of"])
    if result["phase"] == "intraday" and same_minute(b["as_of"], ref): when = "same time"
    return f"{r['name']} members ({when}, NSE): {b['advances']} up, {b['declines']} down, {b['unchanged']} unchanged."


def broader_market(snap):
    """Midcap 150, Smallcap 250 and Nifty 500 (level, change, advances/declines) from an NSE allIndices snapshot, or None."""
    broad = {}
    for name in BROADER:
        row = snap["indices"].get(name)
        if row: broad[name] = {k: row.get(k) for k in ("last", "change", "change_pct", "advances", "declines", "unchanged")}
    return {"as_of": snap["as_of"], "source": snap["page"], "indices": broad} if broad else None


def broader_line(result):
    """Midcaps, smallcaps and Nifty 500 breadth from the same NSE allIndices snapshot as the headline levels."""
    b = result.get("broader_market")
    if not b: return None
    rows, parts = b["indices"], []
    for name, label in (("NIFTY MIDCAP 150", "Nifty Midcap 150"), ("NIFTY SMALLCAP 250", "Nifty Smallcap 250")):
        if rows.get(name) and rows[name].get("change_pct") is not None: parts.append(f"{label} {rows[name]['change_pct']:+.2f}%")
    n500 = rows.get("NIFTY 500") or {}
    tail = ""
    if n500.get("advances") is not None and n500.get("declines") is not None:
        tail = (f"{'; ' if parts else ''}Nifty 500 stocks {n500['advances']} up, {n500['declines']} down"
                + (f", {n500['unchanged']} unchanged" if n500.get("unchanged") is not None else ""))
    if not parts and not tail: return None
    when = clock_time(b["as_of"]) + " IST" if result["phase"] == "intraday" else day(b["as_of"])
    return f"Broader market (NSE, {when}): " + ", ".join(parts) + tail + "."


def full_index(r, context=None, result=None):
    """One index on its own: headline, position, drivers, VIX, levels table, watch, provisional levels, average, breadth."""
    result = result or {}
    lines = [display_name(r) + ".", ""] if display_name(r) != r["name"] else []
    if r["status"] == "BLOCKED": return lines + [blocked_text(r), ""]
    ref = headline_time(r)
    lead = sensex_lead(result, r) if r["index_id"] == "sensex" else [headline(r)]
    if r["phase"] != "preopen": lead.append(position(r))
    if context is None: context = vix_line(result.get("vix"), ref)
    for extra in (driver_line(result, r["index_id"], ref=ref), driver_gap(result, r["index_id"]), reasons_line(result, r["index_id"]), cues_line(result),
                  context or None, record_line(r)):
        if extra: lead.append(extra)
    lines += lead
    if r["phase"] != "preopen":  # before the open the headline and watch line already carry every dated level
        lines += ["", "| Level | Points |", "| --- | ---: |"] + [f"| {level_name(k, r)} | {pts(v)} |" for k, v in r["levels"].items()]
        if r["phase"] == "intraday":
            lines += ["", (GLOSSARY_OR if "opening_range_high" in r["levels"] else "How to read this: ") + GLOSSARY]
    lines += ["", watch_line(r)]
    if pivot_line(r): lines += ["", pivot_line(r)]
    if sma_text(r): lines += ["", sma_text(r)]
    b = breadth_line(result, r, ref)
    if b: lines += ["", b]
    lines += supplied_breadth(r)
    return lines + [""]


def supplied_breadth(r):
    """Constituent breadth from a supplied/broker membership file (not the public route)."""
    br = r["breadth"]
    if "observed" not in br: return []
    out = ["", f"Breadth: {br['advancing']} advancing, {br['declining']} declining, {br['unchanged']} unchanged ({br['observed']}/{br['expected']} members observed; membership: {text_cell(br['membership_basis'])})."]
    if br["strongest_observed"]:
        out.append("Strongest: " + ", ".join(text_cell(x["symbol"]) + f" {x['change_pct']:+.2f}%" for x in br["strongest_observed"])
                   + ". Weakest: " + ", ".join(text_cell(x["symbol"]) + f" {x['change_pct']:+.2f}%" for x in br["weakest_observed"]) + ".")
    return out


def levels_table(rows):
    """One levels table for several indices (rows: levels, columns: indices) instead of one near-identical table each."""
    keys = []
    for r in rows:
        keys += [k for k in r["levels"] if k not in keys]
    out = ["| Level | " + " | ".join(display_name(r, short=True) for r in rows) + " |", "| --- |" + " ---: |" * len(rows)]
    for k in keys:
        name = level_name(k, next(r for r in rows if k in r["levels"]))
        out.append(f"| {name} | " + " | ".join(pts(r["levels"].get(k)) for r in rows) + " |")
    return out


def combined_index_line(r, result, brief=False):
    """One bullet per index in a combined report: position or met condition, watch levels, average and breadth."""
    name = display_name(r, short=True)
    if r["status"] == "BLOCKED": return f"{name}: today's figures could not be confirmed from the public feeds this run."
    lv = r["levels"]
    if r["phase"] == "close":
        where = {"ABOVE_REFERENCE_RANGE": "above its range", "BELOW_REFERENCE_RANGE": "below its range", "INSIDE_REFERENCE_RANGE": "inside its range"}.get(r["condition"], "")
        hl = f"high {pts(lv['session_high'])}, low {pts(lv['session_low'])}, " if "session_high" in lv else ""
        text = f"{name}: finished {where}; {day(r['session_date'])} {hl}close {pts(r['value'])}."
        if not brief and pivot_line(r): text += " " + pivot_line(r)
    elif r["phase"] == "preopen":
        text = f"{name}: watch {watch_core(r, short=brief)}."
        if not brief and pivot_line(r): text += " " + pivot_line(r)
    else:
        met = met_side(r)
        if met:
            text = f"{name}: {watch_core(r)}."
        else:
            where = {"ABOVE_REFERENCE_RANGE": "above its range", "BELOW_REFERENCE_RANGE": "below its range",
                     "INSIDE_REFERENCE_RANGE": "inside its range"}.get(r["condition"], "waiting for a completed bar")
            text = f"{name}: {where}; {watch_core(r, short=brief)}."
    if not brief:
        if sma_text(r): text += " " + sma_text(r)
        b = (result.get("market_breadth") or {}).get(r["index_id"])
        if b: text += f" Members: {b['advances']} up, {b['declines']} down."
    return text


def combined_markdown(result, brief=False):
    """india-daily: comparison table, like-for-like Sensex line, drivers, one levels table and one line per index."""
    rows = [r for r in result["indices"] if r["status"] != "BLOCKED"]
    lines = []
    if result["comparison"]:
        lines += summary_table(result) + [""]
    one = sensex_now_line(result) if result["phase"] == "intraday" else None
    head = ([one] if one else [same_moment_line(result), sensex_estimate_line(result)]) + [broader_line(result), driver_line(result, "nifty", head="What's moving the market"),
            driver_line(result, "banknifty", head="Bank Nifty") if any(r["index_id"] == "banknifty" for r in rows) else None,
            driver_gap(result, "market"), reasons_line(result), cues_line(result)]
    head += [f"{display_name(r, short=True)} - {record_line(r)}" for r in rows if record_line(r)]
    head = [x for x in head if x]
    if brief:
        lines += ["- " + x for x in head] + ([""] if head else [])
        lines.append(levels_for(rows[0]) + ":" if result["phase"] == "close" and rows else "Watch completed 5-minute closes (conditions, not orders):")
        lines += ["- " + combined_index_line(r, result, brief=True) for r in result["indices"]]
        return lines
    if head: lines += head + [""]
    if result["phase"] == "intraday" and rows:
        lines += [finalise_line(rows[0]) + " The 'so far' rows below are the provisional values if the session ended now.", ""]
    lines += [blocked_text(r) for r in result["indices"] if r["status"] == "BLOCKED"]
    if any(r["status"] == "BLOCKED" for r in result["indices"]): lines.append("")
    if rows:
        lines += ["## Levels", ""] + levels_table(rows) + [""]
        lines += ["## Position and watch (conditions, not orders)", ""] + ["- " + combined_index_line(r, result) for r in rows] + [""]
        for r in rows:
            extra = supplied_breadth(r)
            if extra: lines += [f"{r['name']}:"] + extra[1:] + [""]
    return lines


def brief_index(r, result=None):
    if r["status"] == "BLOCKED": return ["- " + blocked_text(r)]
    result = result or {}
    s = r["last_session"]
    lines = []
    if r["phase"] == "preopen":
        lines.append(f"- Last close ({day(s['date'])}): {pts(s['close'])}, {change_text(s['change_points'], s['change_pct'])}; range {pts(s['low'])}-{pts(s['high'])}.")
    else:
        lines += ["- " + x.replace("**", "") for x in (sensex_lead(result, r, bold=False) if r["index_id"] == "sensex" else [headline(r)])]
        lines.append("- " + position(r))
    for extra in (driver_line(result, r["index_id"], ref=headline_time(r)), driver_gap(result, r["index_id"]), reasons_line(result, r["index_id"]),
                  cues_line(result), record_line(r)):
        if extra: lines.append("- " + extra)
    if r["phase"] == "close":
        lines.append("- " + watch_line(r))
        if pivot_line(r): lines.append("- " + pivot_line(r))
    else:
        lines.append("- Watch completed 5-minute closes: " + watch_core(r, after_position=True) + ".")
        if r["phase"] == "intraday": lines.append("- " + (provisional_line(r) or finalise_line(r)))
    if sma_text(r): lines.append("- " + sma_text(r))
    return lines


def title(result):
    name = TITLES[result["command"].removeprefix("/")]
    rows = [r for r in result["indices"] if r.get("status") != "BLOCKED"]
    shared = headline_time(rows[0]) if len(rows) == 1 else None  # single NSE-live index: the title carries its data time
    nse_at = result.get("nse_live_at")  # otherwise the NSE snapshot time the body's 'now' lines use, not the run time
    when = PHASES[result["phase"]] + ((f", data as of {clock_time(shared)} IST" if shared else
                                       f", NSE data as of {clock_time(nse_at)} IST" if nse_at else f" ({clock_time(result['as_of'])} IST)")
                                      if result["phase"] == "intraday" else "")
    return f"{name} - {long_day(result['date'])}, {when}"


def previous_lines(result, brief=False):
    """The previous session's closing context: index change, FII/DII and NSE breadth, each dated."""
    ctx = result.get("previous_context")
    if not ctx: return []
    out = []
    session = ctx.get("session")
    parts = []
    for key, row in (ctx.get("indices") or {}).items():
        if row.get("status") == "OBSERVED":
            parts.append(f"{LABELS_SHORT.get(key, key)} {pts(row['close'])} ({row['change_pct']:+.2f}%)")
    sx = ctx.get("sensex")
    if sx: parts.append(f"Sensex {pts(sx['close'])} ({sx['change_pct']:+.2f}%, Yahoo)")
    if parts and session and session != result["date"]: out.append(f"Previous session ({day(session)}) close: " + ", ".join(parts) + ".")
    f = ctx.get("institutional_flows")
    if f: out.append(flows_line(f))
    b = ctx.get("nse_eq_breadth")
    if b and not brief: out.append(f"NSE shares ({day(session)}, all EQ): {b['advancing']:,} up, {b['declining']:,} down, {b['unchanged']:,} unchanged.")
    g = ctx.get("gift")
    if g and result["phase"] == "preopen":
        stale = "" if g.get("freshness") != "STALE" else ", stale"
        out.append(f"GIFT Nifty futures ({day(g['as_of'])} {g['as_of'][11:16]} IST{stale}): {pts(g['value'])}, {g['change_points']:+,.2f} ({g['change_pct']:+.2f}%); not the spot index.")
    return out


LABELS_SHORT = {"nifty": "Nifty 50", "banknifty": "Bank Nifty", "sensex": "Sensex"}


def calendar_lines(result):
    cal = result.get("calendar")
    if not cal: return []
    from nse_live import calendar_lines as lines
    command = result["command"]
    if command == "/sensex":  # NSE expiries are not Sensex (BSE) expiries; keep only exchange holidays
        return lines({k: v for k, v in cal.items() if k != "expiries"})
    return lines(cal, weekly=command != "/banknifty")  # Bank Nifty has monthly expiries only


def markdown(result, brief=False):
    demo_mode = result["mode"] == "SYNTHETIC_DEMO"
    combined = len(result["indices"]) > 1
    vix = vix_line(result.get("vix"), headline_time(result["indices"][0]) if not combined else None)
    prev, cal = previous_lines(result, brief), calendar_lines(result)
    if brief:
        lines = [f"**{title(result)}**" + (" - **SYNTHETIC DEMO - not market data**" if demo_mode else "")]
        if combined:
            lines += [""] + combined_markdown(result, brief=True)
        else:
            lines += brief_index(result["indices"][0], result)
            if vix: lines.append("- " + vix)
        lines += ["- " + x for x in prev[:2]]
        lines += ["- " + x for x in cal[:2]]
        lines.append(f"- Notes: {source_phrase(result)}; research only, no order. Run without --brief for all levels and data notes.")
        return "\n".join(lines) + "\n"
    lines = ["# " + title(result), ""]
    if demo_mode: lines += ["**SYNTHETIC DEMO - not market data**", ""]
    elif result["phase"] != "intraday": lines += [f"Report time {clock_time(result['as_of'])} IST.", ""]
    if combined:
        lines += combined_markdown(result)
    else:
        lines += full_index(result["indices"][0], context=None if vix else False, result=result)
    if prev:
        lines += ["## Previous session", ""] + ["- " + x for x in prev] + [""]
    if cal:
        lines += ["## Calendar (NSE)", ""] + ["- " + x for x in cal] + [""]
    if result["events"]:
        lines += ["## Events", ""]
        lines += ["- " + text_cell(e["headline"]) + " - " + text_cell(e["source"]) + " (" + text_cell(e["published_at"]) + ")" for e in result["events"]]
        lines += [""]
    lines += ["## Data notes", ""] + ["- " + n for n in data_notes(result)]
    return "\n".join(lines) + "\n"


def demo(phase="intraday"):
    phase = "intraday" if phase == "auto" else phase
    now = instant({"preopen": "2026-11-06T09:00:00+05:30", "intraday": "2026-11-06T09:35:00+05:30", "close": "2026-11-06T16:00:00+05:30"}[phase])
    source = "synthetic_fixture_not_market_data"
    session = {"source": source, "as_of": now.isoformat(), "date": "2026-11-06", "previous_session": "2026-11-05", "is_open_day": True, "open": "09:15", "close": "15:30", "exchanges": ["NSE", "BSE"]}
    bundle = {"synthetic": True, "session": session, "indices": {}, "events": {"source": source, "as_of": now.isoformat(), "session": session["date"], "coverage": "partial", "items": []},
              "vix": {"source": source, "symbol": VIX_SYMBOL, "as_of": now.isoformat(), "rows": [
                  {"session": "2026-11-04", "open": 14.2, "high": 14.9, "low": 13.8, "close": 14.5},
                  {"session": "2026-11-05", "open": 14.5, "high": 15.1, "low": 13.9, "close": 14.1}]}}
    for key, base in (("nifty", 25000), ("sensex", 80000), ("banknifty", 55000)):
        count = 0 if phase == "preopen" else 4 if phase == "intraday" else 75
        start = now.replace(hour=9, minute=15, second=0)
        rows = [{"at": (start + timedelta(minutes=5*k)).isoformat(), "open": base+k*5, "high": base+k*5+12, "low": base+k*5-8, "close": base+k*5+6} for k in range(count)]
        bundle["indices"][key] = {"index_id": key, "source": source, "as_of": now.isoformat(), "price_basis": "spot_index_points",
            "previous": {"source": source, "as_of": "2026-11-05T16:00:00+05:30", "session": "2026-11-05", "open": base-50, "high": base+10, "low": base-100, "close": base-20},
            "history": {"source": source, "as_of": now.isoformat(), "rows": [
                {"session": "2026-11-04", "close": base-60, "available_at": "2026-11-04T16:00:00+05:30"},
                {"session": "2026-11-05", "close": base-20, "available_at": "2026-11-05T16:00:00+05:30"}]},
            "candles": {"source": source, "as_of": now.isoformat(), "interval": "5minute", "rows": rows}}
    return bundle, now


# ---------------------------------------------------------------- public collection (NSE first, Yahoo for bars/Sensex)

NSE_NAMES = {"nifty": "NIFTY 50", "banknifty": "NIFTY BANK"}
BROADER = ("NIFTY MIDCAP 150", "NIFTY SMALLCAP 250", "NIFTY 500")
# Optional context must degrade, never crash: a body cut short (http.client.IncompleteRead), a reset or a timeout
# becomes a data note.
NETWORK_ERRORS = (OSError, http.client.HTTPException, ConnectionError, socket.timeout, TimeoutError)
CONTEXT_ERRORS = (Block, KeyError, TypeError, ValueError, ArithmeticError, IndexError) + NETWORK_ERRORS
CONTEXT_BUDGET = 45.0  # seconds from the start of the parallel context jobs until whatever has not arrived is a stated gap


def _phase(session, now):
    start = datetime.combine(now.date(), time.fromisoformat(session["open"]), IST)
    end = datetime.combine(now.date(), time.fromisoformat(session["close"]), IST)
    return ("preopen" if now < start else "intraday" if now < end else "close"), start, end


def nse_session(summary, now):
    """Today's regular NSE session when Yahoo has no provider session but NSE's holiday list shows a trading day."""
    return {"source": "nse_holiday_master_regular_session", "as_of": now.isoformat(), "date": now.date().isoformat(),
            "previous_session": summary["previous_trading_day"], "is_open_day": True, "open": "09:15", "close": "15:30", "exchanges": ["NSE", "BSE"]}


YAHOO_INDEX = {"nifty": "^NSEI", "banknifty": "^NSEBANK", "sensex": "^BSESN"}


def record_history(symbol, previous_session, today, *, fetcher=None):
    """Highest close before the previous session and highest high before today from Yahoo's ~10-year daily chart
    (unofficial), plus Yahoo's 52-week high. Only these summaries are kept, never the 2,500 rows."""
    from public_market import request
    import urllib.parse
    url = "https://query1.finance.yahoo.com/v8/finance/chart/" + urllib.parse.quote(symbol, safe="") + "?range=10y&interval=1d"
    data = json.loads((fetcher or request)(url))["chart"]["result"][0]
    if data["meta"].get("symbol") != symbol: raise Block("RECORDS: wrong instrument")
    q = data["indicators"]["quote"][0]
    rows = [(datetime.fromtimestamp(int(t), IST).date().isoformat(), float(c), float(h))
            for t, c, h in zip(data["timestamp"], q["close"], q["high"]) if c is not None and h is not None and c > 0 and h >= c]
    if len(rows) < 500 or len({r[0] for r in rows}) != len(rows): raise Block("RECORDS: history too short or duplicated")
    before_prev = [r for r in rows if r[0] < previous_session]
    before_today = [r for r in rows if r[0] < today]
    best_close = max(before_prev, key=lambda r: r[1]); best_high = max(before_today, key=lambda r: r[2])
    high52 = data["meta"].get("fiftyTwoWeekHigh")
    return {"basis": "Yahoo daily history (unofficial)", "first": rows[0][0], "sessions": len(rows),
            "max_close_before_previous": [best_close[0], best_close[1]], "max_high_before_today": [best_high[0], best_high[2]],
            "fifty_two_week_high": float(high52) if isinstance(high52, (int, float)) and high52 > 0 else None}


def enrich_public(bundle, selected, now, *, nse_fetch=None, yahoo_fetch=None, closing=None, drivers=None, records=None, news=None):
    """Add NSE evidence to a Yahoo public bundle: official previous closes and 20-day history, live levels, India VIX,
    constituent breadth, the holiday calendar and the previous session's closing context. Every step is optional:
    a failure becomes a plain data note, never a block."""
    import nse_live
    live_run = nse_fetch is None  # injected fetchers (tests, replays) never trigger the live news search by default
    nse_fetch = nse_fetch or nse_live.fetch
    errors = bundle.setdefault("context_errors", {})
    session, used = bundle["session"], []
    phase, start, end = _phase(session, now)
    today = now.date().isoformat()
    cal = None
    try:
        cal = nse_live.holiday_calendar(fetcher=nse_fetch, clock=lambda: now)
        bundle["calendar"] = nse_live.calendar_summary(cal, now.date())
    except CONTEXT_ERRORS:
        errors["nse_calendar"] = "NSE holiday list unavailable this run; holidays and expiry shifts were not checked"
    nse_keys = [k for k in selected if k in NSE_NAMES]
    if nse_keys:
        try:
            hist = nse_live.index_history([NSE_NAMES[k] for k in nse_keys], now.date() - timedelta(days=1), 21, cal=cal, fetcher=nse_fetch)
            last = hist["sessions"][-1]
            special = [d for d in hist["special_sessions_included"]]
            failed = hist.get("dates_failed") or []
            if any(d > last for d in failed):  # the newest file could not be read: NSE cannot give the previous session
                raise Block("NSE: latest daily index file unreadable")
            if last != session["previous_session"]:
                errors["previous_session"] = (f"Previous session taken from NSE's daily files ({day(last)}); "
                                              f"Yahoo's history ended {day(session['previous_session'])}")
                session["previous_session"] = last
            basis = "NSE official daily closes (ind_close_all files)" + (f", including the {', '.join(day(d) for d in special)} special session" if special else "")
            if failed:
                basis += (f"; the NSE file for {', '.join(day(d) for d in failed)} could not be read this run, so the window starts "
                          f"{len(failed)} session{'s' if len(failed) > 1 else ''} earlier")
            for key in nse_keys:
                rows = hist["series"].get(NSE_NAMES[key]) or []
                if not rows or rows[-1]["session"] != last or None in (rows[-1]["open"], rows[-1]["high"], rows[-1]["low"]):
                    continue
                item = bundle["indices"].setdefault(key, {"index_id": key, "price_basis": "spot_index_points", "source": "nse_index_close_files"})
                item["as_of"] = now.isoformat()
                p = rows[-1]
                item["previous"] = {"source": "nse_ind_close_all", "as_of": now.isoformat(), "session": last, **{k: p[k] for k in ("open", "high", "low", "close")}}
                item["history"] = {"source": "nse_ind_close_all", "as_of": now.isoformat(), "basis": basis,
                                   "rows": [{"session": r["session"], "close": r["close"], "available_at": now.isoformat()} for r in rows]}
                errors.pop(key, None)
            used.append("NSE daily index files for previous closes and 20-day averages")
        except CONTEXT_ERRORS:
            errors["nse_history"] = "NSE daily index files unavailable; previous closes and 20-day averages use Yahoo daily history"
    for key in selected:  # any 20-day average still on Yahoo's daily history (Sensex always; Nifty/Bank Nifty when NSE failed)
        item = bundle["indices"].get(key)
        if not item or not isinstance(item.get("history"), dict) or item["history"].get("source") == "nse_ind_close_all":
            continue
        rows = item["history"].get("rows") or []
        have = {r["session"] for r in rows[-20:]}
        lost = [d for d in (cal or {}).get("special_sessions", []) if rows[-20]["session"] <= d <= rows[-1]["session"] and d not in have] if len(rows) >= 20 else []
        item["history"]["basis"] = ("Yahoo daily closes (unofficial)" + ("" if key == "sensex" else ", NSE daily files unavailable this run") + (
            f"; Yahoo omits the {', '.join(day(d) for d in lost)} special session, so the window starts one session earlier" if lost else ""))
    snap = None
    if phase != "preopen":
        try:
            snap = nse_live.live_indices(fetcher=nse_fetch, clock=lambda: now)
            if instant(snap["as_of"]).date() != now.date(): snap = None
        except CONTEXT_ERRORS:
            snap = None
        if snap is None and nse_keys:
            errors["nse_live"] = "NSE live index data unavailable this run; levels use Yahoo"
    for key in selected:
        item = bundle["indices"].get(key)
        if item is None: continue
        name = NSE_NAMES.get(key, "SENSEX")
        try:
            if phase == "intraday":
                q = nse_live.quote(name, snapshot=snap if snap else False, fetcher=nse_fetch, yahoo_fetcher=yahoo_fetch, clock=lambda: now)
                item["quote"] = {"source": q["source"], "provider": q["provider"], "page": q["source"], "as_of": q["as_of"], "value": q["last"],
                                 "open": q.get("open"), "high": q.get("high"), "low": q.get("low"), "max_age_seconds": 300 if q["provider"] == "NSE" else 1500}
            elif phase == "close":
                close = None
                if key in NSE_NAMES:
                    try:
                        row = nse_live.index_closes(now.date(), fetcher=nse_fetch)[name]
                        close = {"value": row["close"], "open": row["open"], "high": row["high"], "low": row["low"], "basis": "NSE daily index report (official close)"}
                    except CONTEXT_ERRORS:
                        row = (snap or {}).get("indices", {}).get(name)
                        if row and instant(snap["as_of"]) >= end:
                            close = {"value": row["last"], "open": row["open"], "high": row["high"], "low": row["low"], "basis": "NSE index level after 15:30 (closing value)"}
                else:
                    q = nse_live.yahoo_quote("SENSEX", fetcher=yahoo_fetch, clock=lambda: now)
                    if instant(q["as_of"]).date() == now.date():
                        value = (item.get("official_close") or {}).get("value", q["last"])
                        close = {"value": value, "open": q.get("open"), "high": q["high"], "low": q["low"], "basis": "Yahoo daily close (unofficial)"}
                        if not (close["high"] and close["low"] and close["low"] <= value <= close["high"]):
                            close.update(open=None, high=None, low=None)
                if close:
                    item["official_close"] = {**close, "session": today, "source": close["basis"], "as_of": now.isoformat()}
        except CONTEXT_ERRORS:
            pass
    if snap:
        bundle["nse_live_at"] = snap["as_of"]
        used.insert(0, f"NSE live index data ({nse_live.LIVE_PAGE.split('//')[1].split('/')[0]}, {clock_time(snap['as_of'])} IST)")
        breadth = {}
        for key, name in NSE_NAMES.items():
            row = snap["indices"].get(name)
            if key in selected and row and None not in (row.get("advances"), row.get("declines"), row.get("unchanged")):
                breadth[key] = {k: row[k] for k in ("advances", "declines", "unchanged")} | {"as_of": snap["as_of"], "source": snap["page"]}
        if breadth: bundle["market_breadth"] = breadth
        broad = broader_market(snap)
        if broad and len(selected) > 1: bundle["broader_market"] = broad
        for key, name in NSE_NAMES.items():  # 52-week range as NSE publishes it
            row, item = snap["indices"].get(name), bundle["indices"].get(key)
            if key in selected and row and item and row.get("year_high"):
                item["year_range"] = {"high": row["year_high"], "low": row.get("year_low"), "source": "NSE", "as_of": snap["as_of"]}
        v = snap["indices"].get("INDIA VIX")
        if v:
            rows = [{"session": v["previous_day"] or session["previous_session"], "close": v["previous_close"]},
                    {"session": today, "close": v["last"], "change_pct": v.get("change_pct"),
                     **({"high": v["high"], "low": v["low"]} if v["high"] and v["low"] else {})}]
            if rows[0]["session"] < today:
                bundle["vix"] = {"source": "nse_live_allindices", "symbol": VIX_SYMBOL, "as_of": now.isoformat(), "at": snap["as_of"], "rows": rows}
                bundle.pop("vix_error", None)
        elif "INDIA VIX" in (snap.get("rejected_rows") or []):
            errors["vix"] = "NSE's live India VIX row failed its consistency checks this run"
    if phase == "close":  # after the close India VIX's official close comes from NSE's daily index report (it can differ from 15:30)
        try:
            v = nse_live.index_closes(now.date(), fetcher=nse_fetch).get("INDIA VIX")
            if v:
                prev_day = (((snap or {}).get("indices") or {}).get("INDIA VIX") or {}).get("previous_day") or session["previous_session"]
                bundle["vix"] = {"source": "nse_ind_close_all", "basis": "official close", "symbol": VIX_SYMBOL, "as_of": now.isoformat(),
                                 "rows": [{"session": prev_day, "close": round(v["close"] - v["change"], 4)},
                                          {"session": today, "close": v["close"], "change_pct": v["change_pct"],
                                           **({"high": v["high"], "low": v["low"]} if v.get("high") and v.get("low") else {})}]}
                bundle.pop("vix_error", None); errors.pop("vix", None)
        except CONTEXT_ERRORS:
            pass  # not yet published: the snapshot (or Yahoo) value stands
    # Independent context runs alongside the drivers instead of after them (post-close runs spent ~5 s on these in turn):
    # record closes/highs, the reference bars for spike checks, the closing context and, on the live route, the cue search.
    import market_movers
    background = ThreadPoolExecutor(max_workers=6)
    deadline = monotonic() + CONTEXT_BUDGET  # a stalled download (a server dripping bytes) becomes a gap, not a hang
    left = lambda: max(0.5, deadline - monotonic())
    keys = [k for k in selected if bundle["indices"].get(k) is not None]
    record_jobs = {key: background.submit((records or record_history), YAHOO_INDEX[key], session["previous_session"], today, fetcher=yahoo_fetch)
                   for key in keys}
    sx = bundle["indices"].get("sensex")
    ref_key = REFERENCE_INDEX.get(selected[0]) if len(selected) == 1 else None
    ref_job = None
    if phase != "preopen" and ref_key and selected[0] in bundle["indices"] and ref_key not in bundle["indices"]:
        ref_job = background.submit(reference_chart, ref_key, start, now, yahoo_fetch)
    ctx_day = today if phase == "close" else session["previous_session"]
    import closing_context
    ctx_job = background.submit((closing or closing_context.collect), ctx_day, selected=nse_keys or ["nifty"], include_gift=phase == "preopen")
    want_news = snap is not None and phase in ("intraday", "close") and news is not False and (news or live_run)
    cue_job = None
    if want_news and news is None:
        cue_since, cue_by = market_movers.cue_window(phase, session["previous_session"], today)
        cue_job = background.submit(market_movers.market_cues, now, since=cue_since, since_by=cue_by, news_fetch=market_movers.news_getter())
    try:
        # A Sensex-only report still needs Nifty 50 bars to compare a delayed Sensex quote at the same moment; every
        # single-index report uses its reference index's bars to tell a bad tick from a real move.
        if ref_job is not None:
            try:
                rows = ref_job.result(timeout=left())
                bundle["reference_bars"] = {"index": ref_key, "symbol": YAHOO_INDEX[ref_key], "rows": rows}
                if selected == ["sensex"] and phase == "intraday":
                    row = (snap or {}).get("indices", {}).get("NIFTY 50")
                    bundle["comparison_bars"] = {"rows": rows, "previous_close": row["previous_close"] if row else None,
                                                 "now": {"value": row["last"], "change_pct": row["change_pct"], "as_of": snap["as_of"]} if row else None}
            except CONTEXT_ERRORS + (StopIteration, FutureTimeout):
                pass
        # What is moving the market: NSE sectors plus estimated point drivers (Nifty 50; Bank Nifty heavyweights).
        if snap and phase != "preopen":
            # Sensex-only: Sensex points from weights fitted to Sensex's own moves, never Nifty 50 points relabelled.
            sx_prev = ((bundle["indices"].get("sensex") or {}).get("previous") or {}).get("close")
            wanted = ((["NIFTY 50"] if "nifty" in selected else []) + (["SENSEX"] if selected == ["sensex"] and sx_prev else [])
                      + (["NIFTY BANK"] if "banknifty" in selected else []))
            extra = {"sensex_previous": float(sx_prev)} if "SENSEX" in wanted else {}
            try:
                if drivers is None: extra["previous_session"] = session["previous_session"]  # NSE's evening snapshot may lack it
                bundle["drivers"] = (drivers or market_movers.live_drivers)(wanted, snap, nse_fetch=nse_fetch, yahoo_fetch=yahoo_fetch, **extra)
                if not bundle["drivers"].get("indices"):
                    why = bundle["drivers"].get("error") or "constituent prices or weights missing"
                    errors["drivers"] = f"Stock-level drivers unavailable this run ({why}); sectors shown"
            except CONTEXT_ERRORS + (StopIteration,):
                errors["drivers"] = "Sector and stock drivers unavailable this run"
        # Same-day "why": dated headlines on today's movers and the cues (Google News RSS), within a time budget. After
        # the close both stay: the reasons explain the day, the cues look ahead to the next session.
        if want_news:
            try:
                requests = news_requests(selected, bundle.get("drivers") or {}, snap)
                kw = {"cues": cue_job} if cue_job is not None else {}
                bundle["news"] = (news or market_movers.news_context)(now, requests, previous_session=session["previous_session"], session=today,
                                                                      phase=phase, **kw)
            except CONTEXT_ERRORS + (StopIteration,):
                bundle["news"] = {"reasons": {}, "cues": None, "gaps": ["news"], "phase": phase}
        # Record closes/highs (Yahoo ~10-year daily history); Sensex's 52-week high from Yahoo (no NSE equivalent).
        for key, job in record_jobs.items():
            try:
                hist = job.result(timeout=left())
                item = bundle["indices"][key]
                item["record_history"] = hist
                if key == "sensex" and hist.get("fifty_two_week_high"):
                    item["year_range"] = {"high": hist["fifty_two_week_high"], "low": None, "source": "Yahoo"}
            except CONTEXT_ERRORS + (StopIteration, FutureTimeout):
                pass
        if any(k in bundle["indices"] for k in selected):
            yahoo_parts = (["5-minute bars"] if phase != "preopen" else []) + (["Sensex (unofficial)"] if "sensex" in selected else [])
            if not snap and phase != "preopen" and nse_keys: yahoo_parts.append("index levels")
            if yahoo_parts: used.append("Yahoo public charts for " + " and ".join(yahoo_parts))
        bundle["sources_used"] = used
        try:
            ctx = ctx_job.result(timeout=left())
            sx = bundle["indices"].get("sensex")
            if "sensex" in selected and sx and ctx_day == session["previous_session"]:
                hist = sx.get("history", {}).get("rows", [])
                if len(hist) >= 2 and hist[-1]["session"] == ctx_day:
                    ctx["sensex"] = {"close": float(hist[-1]["close"]), "change_pct": percent_change(hist[-1]["close"], hist[-2]["close"])}
            bundle["previous_context"] = ctx
        except CONTEXT_ERRORS + (FutureTimeout,):
            errors["previous_context"] = "Previous-session NSE context (FII/DII, breadth) unavailable this run"
    finally:
        background.shutdown(wait=False, cancel_futures=True)
    return bundle


def reference_chart(key, start, now, yahoo_fetch=None):
    """Today's completed five-minute bars of another index (Yahoo), for same-moment comparisons and spike checks."""
    from public_market import collect_chart, request
    chart = collect_chart(YAHOO_INDEX[key], "5m", fetcher=yahoo_fetch or request, clock=lambda: max(now, instant()))
    return [{"at": b["at"], **{k: b[k] for k in ("open", "high", "low", "close")}} for b in chart["bars"]
            if start <= instant(b["at"]) and instant(b["at"]) + timedelta(minutes=5) <= now]

BANK_SECTORS = ("Nifty Bank", "Nifty PSU Bank", "Nifty Private Bank")


def news_requests(selected, drivers, snap):
    """What to search headlines for: per report key, today's index change, the stocks driving it (on the side of the
    move) and the sectors (bank sub-indices for Bank Nifty). india-daily asks once, for the market (Nifty 50)."""
    rows, blocks = snap["indices"], drivers.get("indices") or {}
    levels = sorted(({"name": n, "change_pct": v} for n, v in (drivers.get("sector_levels") or {}).items()), key=lambda r: -r["change_pct"])
    plan = {"market": ("NIFTY 50", "NIFTY 50")} if len(selected) > 1 else {
        "nifty": ("NIFTY 50", "NIFTY 50"), "banknifty": ("NIFTY BANK", "NIFTY BANK"), "sensex": ("SENSEX", "NIFTY 50")}
    out = {}
    for key, (block_name, level_name) in plan.items():
        if len(selected) == 1 and key not in selected: continue
        b = blocks.get(block_name) or {}
        change = (b.get("actual_points") if block_name == "SENSEX" else None) or (rows.get(level_name) or {}).get("change") or 0.0
        movers = (b.get("negative") if change < 0 else b.get("positive")) or []
        sectors = [x for x in levels if x["name"] in BANK_SECTORS] if key == "banknifty" else levels
        out[key] = {"change": change, "movers": movers[:3], "sectors": sectors}
    return out


def closed_day(selected, now, *, nse_fetch=None, closing=None, yahoo_fetch=None, drivers=None, news=None):
    """Market-closed answer (weekend or holiday): today's status, the last session's published NSE closing report and,
    as after that session's close, the block for the next session - its levels and classic pivots, the top point
    contributors (estimated), the possible reasons (headlines since the session before) and the look-ahead cues.
    ``drivers``/``news``: injected sources (tests, replays); without them the live drivers and news search run only for
    the real NSE fetch (an injected NSE fetcher never triggers network work elsewhere)."""
    import closing_context, nse_live
    import market_movers
    live_run = nse_fetch is None
    cal = nse_live.holiday_calendar(fetcher=nse_fetch or nse_live.fetch, clock=lambda: now)
    summary = nse_live.calendar_summary(cal, now.date())
    if summary.get("today_is_trading_day") is not False: return None, summary
    last = nse_live.previous_trading_day(cal, now.date())
    special = [d for d in cal["special_sessions"] if last.isoformat() < d < now.date().isoformat()]
    session = special[-1] if special else last.isoformat()
    try:
        previous = nse_live.previous_trading_day(cal, date.fromisoformat(session)).isoformat()
    except CONTEXT_ERRORS:
        previous = None
    nse = [k for k in selected if k in NSE_NAMES]
    pool = ThreadPoolExecutor(max_workers=3)
    deadline = monotonic() + CONTEXT_BUDGET
    left = lambda: max(0.5, deadline - monotonic())
    try:
        # The look-ahead cue search starts at once: it does not depend on the session's numbers.
        want_news = news is not False and (news is not None or live_run)
        cue_job = None
        if want_news and news is None:
            cue_since, cue_by = market_movers.cue_window("close", previous, session)
            cue_job = pool.submit(market_movers.market_cues, now, since=cue_since, since_by=cue_by, news_fetch=market_movers.news_getter())
        result = (closing or closing_context.collect)(session, selected=nse, include_gift=True) if nse else closing_context.sensex_notice(session)
        reason = summary.get("today_holiday") or ("weekend" if now.weekday() >= 5 else "not a listed trading day")
        result["closed_today"] = {"date": now.date().isoformat(), "reason": reason, "next_trading_day": summary.get("next_trading_day"),
                                  "last_session": session, "previous_session": previous, "calendar": summary, "source": nse_live.HOLIDAY_PAGE}
        sx_prev = None
        if "sensex" in selected:
            try:
                q = nse_live.yahoo_quote("SENSEX", fetcher=yahoo_fetch, clock=lambda: now)
                if q["as_of"][:10] == session:
                    result["sensex_last"] = {"close": q["last"], "change": q["change"], "change_pct": q["change_pct"], "date": q["as_of"][:10],
                                             "high": q.get("high"), "low": q.get("low")}
                    sx_prev = q["previous_close"]
            except CONTEXT_ERRORS:
                pass
        # What moved the index in that session: sectors and the top point contributors (estimated weights).
        wanted = ((["NIFTY 50"] if "nifty" in selected else []) + (["SENSEX"] if selected == ["sensex"] and sx_prev else [])
                  + (["NIFTY BANK"] if "banknifty" in selected else []))
        if wanted and (drivers is not None or live_run):
            try:
                job = pool.submit(drivers or market_movers.session_drivers, wanted, session, nse_fetch=nse_fetch, yahoo_fetch=yahoo_fetch,
                                  sensex_previous=sx_prev, previous_session=previous)
                result["drivers"] = job.result(timeout=left())
            except CONTEXT_ERRORS + (FutureTimeout, StopIteration):
                result["drivers_error"] = "Sector and stock drivers unavailable this run"
        if want_news:
            try:
                requests = closed_news_requests(selected, result)
                kw = {"cues": cue_job} if cue_job is not None else {}
                since = market_movers.session_close(previous)
                result["news"] = (news or market_movers.news_context)(now, requests, previous_session=previous, session=session, phase="close",
                                                                      window=market_movers.news_window(now, since), **kw)
            except CONTEXT_ERRORS + (StopIteration,):
                result["news"] = {"reasons": {}, "cues": None, "gaps": ["news"], "phase": "close"}
    finally:
        pool.shutdown(wait=False, cancel_futures=True)
    return result, summary


def closed_changes(result):
    """{key: (points change, % change, close, high, low)} of each index in a closed-day result (NSE rows; Sensex from Yahoo)."""
    out = {}
    for key, row in (result.get("indices") or {}).items():
        if row.get("status") == "OBSERVED":
            out[key] = (row["points_change"], row["change_pct"], row["close"], row.get("high"), row.get("low"))
    s = result.get("sensex_last")
    if s and "sensex" not in out: out["sensex"] = (s["change"], s["change_pct"], s["close"], s.get("high"), s.get("low"))
    return out


def closed_news_requests(selected, result):
    """news_requests for the closed-day answer: the session's index changes and drivers instead of a live snapshot."""
    changes = closed_changes(result)
    snap = {"indices": {NSE_NAMES.get(k, "SENSEX"): {"change": v[0]} for k, v in changes.items()}}
    if "nifty" not in changes and "NIFTY 50" not in snap["indices"]:
        snap["indices"]["NIFTY 50"] = {"change": next((v[0] for v in changes.values()), 0.0)}
    return news_requests(selected, result.get("drivers") or {}, snap)


def flow_text(category, crore):
    """'FII/FPI +₹10,308.42 cr (net buyers)' / 'DII −₹4,859.80 cr (net sellers)': a real minus sign, the side in words."""
    side = "net buyers" if crore > 0 else "net sellers" if crore < 0 else "flat"
    return f"{category} {'+' if crore >= 0 else '−'}₹{abs(crore):,.2f} cr ({side})"


def flows_line(f):
    """Institutional flows of one NSE day, buyers/sellers in words; None without the data."""
    if not f: return None
    vals = "; ".join(flow_text(k, v["net_crore"]) for k, v in sorted(f["values"].items(), reverse=True))
    return f"Institutional flows ({day(f['trading_date'])}, provisional NSE data): {vals}."


def gift_line(g, closed=False):
    """GIFT Nifty futures with the time of its last trade (the market-closed answer) or its age (a trading day)."""
    if not g: return None
    when = f"{long_day(g['as_of'])[:-5]} {g['as_of'][11:16]} IST"
    tag = f"last trade {when}" if closed else (f"{when}, stale" if g.get("freshness") == "STALE" else when)
    expiry = f"; expiry {long_day(g['expiry'])[:-5]}" if g.get("expiry") else ""
    return (f"GIFT Nifty futures ({tag}{expiry}): {pts(g['value'])}, {g['change_points']:+,.2f} ({g['change_pct']:+.2f}%) on its own previous "
            "close; a futures price, not the spot index.")


def _pivots(h, l, c):
    h, l, c = round(h, 2), round(l, 2), round(c, 2)  # the printed levels, so one source's float noise cannot move a pivot
    p = (h + l + c) / 3
    return {"S2": p - (h - l), "S1": 2 * p - h, "P": p, "R1": 2 * p - l, "R2": p + (h - l)}


def closed_index_lines(key, row, result, named=False):
    """The after-close block of one index for the next session: last close, levels, classic pivots (``named``: each
    line starts with the index name, for a report on several indices)."""
    c = result["closed_today"]
    nxt = long_day(c["next_trading_day"])[:-5] if c.get("next_trading_day") else "the next session"
    when = day(c["last_session"])
    change, pct, close, high, low = row
    name = LABELS_SHORT.get(key, key)
    lines = [f"{name} ({when} close{', Yahoo, unofficial' if key == 'sensex' else ', NSE'}): {pts(close)}, {change_text(change, pct)}"
             + (f"; range {pts(low)}-{pts(high)}." if high and low else ".")]
    who = f"{name} - " if named else ""
    if high and low:
        lines.append(f"{who}Levels for {nxt} (next session): {when} high {pts(high)}, low {pts(low)}, close {pts(close)}.")
        lines.append(f"{who}Classic pivots for {nxt} (from {when} high/low/close; arithmetic, not a forecast): "
                     + ", ".join(f"{k} {pts(v)}" for k, v in _pivots(high, low, close).items()) + ".")
    else:
        lines.append(f"{who}Levels for {nxt} (next session): {when} close {pts(close)}; the day's high/low were not available.")
    return lines


def closed_cues_line(result):
    """Look-ahead cues for the next session on a closed day: the newest dated headline on GIFT Nifty (since the last
    session's close), US markets, gold, crude and the rupee (since its open), or 'none yet'."""
    n, c = result.get("news"), result["closed_today"]
    if not n: return None
    nxt = long_day(c["next_trading_day"])[:-5] if c.get("next_trading_day") else "the next session"
    head = (f"Cues for {nxt} (look-ahead headlines, unverified; GIFT Nifty since the {day(c['last_session'])} close, the rest since "
            f"that session's open)")
    cues = n.get("cues")
    if not cues:
        return f"{head}: the news search failed or ran out of time this run - a gap; check GIFT Nifty, US markets, crude, gold and the rupee before the open."
    return f"{head}: " + "; ".join(f"{label} - {_news_item(h)}" if h else f"{label} - none yet" for label, h in cues.items()) + "."


CONTRIB_UNITS = {"nifty": ("NIFTY 50", "Nifty 50"), "banknifty": ("NIFTY BANK", "Bank Nifty"), "sensex": ("SENSEX", "Sensex")}


def contributors_line(key, result, change):
    """'Top point contributors (estimated Bank Nifty points): HDFCBANK +80.0 pts (+2.94%), ...; against: ...' - the three
    stocks that added (or took) the most points on the side of the move. None without a reliable estimate."""
    d = result.get("drivers") or {}
    name, unit = CONTRIB_UNITS.get(key, ("NIFTY 50", "Nifty 50"))
    b = (d.get("indices") or {}).get(name)
    if not b or not reliable(b): return None
    main, other = (b["negative"], b["positive"]) if change < 0 else (b["positive"], b["negative"])
    if not main: return None
    text = f"Top point contributors (estimated {unit} points): {_points_text(main[:3])}"
    if other: text += f"; against the move: {_points_text(other[:2])}"
    total = b.get("check_estimated_points", b["estimated_total_points"])
    return text + f" (estimates add up to {total:+.1f} vs the actual {b['actual_points']:+.1f})."


def closed_sector_line(key, result):
    d = result.get("drivers") or {}
    if key == "banknifty":
        lv = d.get("sector_levels") or {}
        banks = [f"{n.removeprefix('Nifty ')} {lv[n]:+.2f}%" for n in ("Nifty PSU Bank", "Nifty Private Bank") if n in lv]
        return ("Bank sub-indices: " + ", ".join(banks) + ".") if banks else None
    up, down = d.get("sectors_up"), d.get("sectors_down")
    parts = ([("strongest " + _sector_text(up[:2]))] if up else []) + ([("weakest " + _sector_text(down[:2]))] if down else [])
    return ("Sectors: " + "; ".join(parts) + ".") if parts else None


def closed_shim(result):
    """The fields the daily reasons/cues renderers read, taken from a closed-day result."""
    c = result["closed_today"]
    keys = list(closed_changes(result))
    return {"news": result.get("news"), "phase": "close", "calendar": {"next_trading_day": c.get("next_trading_day")},
            "session": {"previous_session": c.get("previous_session")}, "indices": [{"index_id": k} for k in keys] or [{}]}


def closed_banner(result):
    import market_movers
    c = result["closed_today"]
    return market_movers.closed_banner({**c, "last_session": c.get("last_session") or result.get("session")})


def closed_markdown(result, brief=False):
    import closing_context
    c = result["closed_today"]
    cal_lines = []
    from nse_live import calendar_lines as nse_calendar
    only = list((result.get("indices") or {}).keys())
    cal = c["calendar"] if only != ["sensex"] else {k: v for k, v in c["calendar"].items() if k != "expiries"}  # NSE expiries are not BSE's
    cal_lines = nse_calendar(cal, weekly=only != ["banknifty"])
    head = [closed_banner(result), ""]
    if "alternative_command" in result and not result.get("sensex_last"):
        body = closing_context.markdown(result, brief=brief)
        tail = ["", "Calendar (NSE): " + " ".join(cal_lines)] if cal_lines else []
        return "\n".join(head) + body + "\n".join(tail) + ("\n" if tail else "")
    changes = closed_changes(result)
    shim = closed_shim(result)
    lines = []
    single = len(changes) == 1
    for key, row in changes.items():
        block = closed_index_lines(key, row, result, named=not single)
        lines += block[:1] + [x for x in (closed_sector_line(key, result) if single or key == "banknifty" else None,
                                          contributors_line(key, result, row[0])) if x] + block[1:]
    if not single:
        sec = closed_sector_line("nifty", result)
        if sec: lines.append(sec)
    for x in (reasons_line(shim, "market" if not single else next(iter(changes), None)), closed_cues_line(result)):
        if x: lines.append(x)
    if result.get("drivers_error") or ((result.get("drivers") or {}).get("error") and not (result.get("drivers") or {}).get("indices")):
        why = (result.get("drivers") or {}).get("error") or "drivers unavailable"
        lines.append(f"⚠ stock contributions not collected — rerun ({why}; the closing levels above are complete).")
    b = result.get("nse_eq_breadth")
    if b: lines.append(f"All NSE shares ({day(result['session'])}): {b['advancing']:,} up, {b['declining']:,} down, {b['unchanged']:,} unchanged.")
    f = result.get("institutional_flows")
    if f: lines.append(flows_line(f))
    g = gift_line(result.get("gift"), closed=True)
    if g: lines.append(g)
    unavailable = [LABELS_SHORT.get(k, k) for k, r in (result.get("indices") or {}).items() if r.get("status") != "OBSERVED" and k not in changes]
    if unavailable: lines.append("Not available this run: " + ", ".join(unavailable) + " (NSE's closing file did not answer); rerun.")
    title = ", ".join(LABELS_SHORT.get(k, k) for k in changes) or "Market"
    if brief:
        out = head + [f"**{title} - last session {long_day(result['session'])}** (research only)"] + ["- " + x for x in lines]
        out += ["- Calendar (NSE): " + " ".join(cal_lines)] if cal_lines else []
        out.append("- Notes: NSE's published closing files; research only, no order. Run without --brief for sources.")
        return "\n".join(out) + "\n"
    out = [f"# {title} - market closed today, {long_day(c['date'])}", ""] + head + [f"Last session {long_day(result['session'])}; report time {result['as_of'][11:16]} IST.", ""]
    out += ["- " + x for x in lines] + [""]
    if cal_lines: out += ["## Calendar (NSE)", ""] + ["- " + x for x in cal_lines] + [""]
    out += ["## Data notes", ""]
    sources = [f"[{name}]({closing_context.public_url(result[key]['source'])})" for key, name in (
        ("index_report", "NSE index closes"), ("cash_report", "NSE bhavcopy"), ("institutional_flows", "NSE FII/DII"), ("gift", "NSE market status")) if key in result]
    if sources: out.append("- Sources: " + ", ".join(sources) + ".")
    if result.get("sensex_last"): out.append("- Sensex: Yahoo's daily close (unofficial; BSE publishes the official value).")
    d = result.get("drivers") or {}
    if d.get("indices"):
        out.append("- Point contributions: weights estimated (anchored to the NSE Indices factsheet where available, fitted on about a month of "
                   "Yahoo 5-minute returns); prices are the session's official bhavcopy closes. Estimates, not NSE's published weights.")
    if b: out.append("- NSE share counts cover every EQ-series share in the session's bhavcopy, not any one index's members.")
    if f: out.append(f"- FII/DII: NSE's provisional figures for {day(f['trading_date'])}; NSE does not state a release time or finality.")
    if result.get("gift"): out.append(f"- GIFT Nifty time: {result['gift']['timezone_basis']}.")
    if result.get("news"):
        out.append(f"- Headlines: Google News RSS since the {day(c['previous_session'])} close (the session before {day(result['session'])}), "
                   "filtered as in the daily reports; publishers' claims, not verified causes.")
    failed = [r["component"].replace("_", " ") for r in result.get("coverage", []) if r["status"] != "COLLECTED"]
    if failed: out.append("- Not collected this run: " + ", ".join(failed) + ".")
    out.append("- Research only; no orders.")
    return "\n".join(out) + "\n"


# ---------------------------------------------------------------- command line

def emit(text, stream=None):
    """Print UTF-8 text even on a legacy Windows console (cp1252 cannot encode the rupee sign)."""
    stream = sys.stdout if stream is None else stream
    if hasattr(stream, "reconfigure") and str(getattr(stream, "encoding", "") or "").lower().replace("-", "").replace("_", "") != "utf8":
        try: stream.reconfigure(encoding="utf-8")
        except (OSError, ValueError): pass
    try:
        stream.write(text + "\n")
    except UnicodeEncodeError:
        encoding = getattr(stream, "encoding", None) or "ascii"
        plain = text.replace("₹", "Rs ").replace("–", "-").replace("—", "-").replace("−", "-").replace("≈", "~")
        stream.write((plain + "\n").encode(encoding, "replace").decode(encoding))


def blocked(reason):
    emit(json.dumps({"status": "BLOCKED", "reason": reason, "submitted": False})); return 2


def selected_indices(command):
    command = command.removeprefix("/")
    return list(INDICES) if command == "india-daily" else [command]


def build(a):
    """Collect or read evidence and return (result, renderer). Raises Block/OSError/input errors only."""
    if sum((bool(a.input), a.demo, a.collect_kite, a.collect_public, a.collect_close)) != 1: raise Block("INPUT: choose exactly one input mode")
    if a.out and Path(a.out).exists(): raise Block("OUTPUT: choose a new file")
    if a.demo and a.now: raise Block("DEMO: use its labelled fixed clock")
    if a.with_context and not a.collect_kite or a.membership and not (a.collect_close or a.collect_kite and a.with_context): raise Block("INPUT: invalid context/membership mode")
    if a.session and not a.collect_close: raise Block("INPUT: explicit session belongs to closing-report mode")
    if a.collect_close:
        if not a.session or a.now or a.calendar or a.events or a.phase not in ("auto", "close"):
            raise Block("CLOSING: explicit session and actual collection clock required")
        import closing_context
        selected = selected_indices(a.command)
        if selected == ["sensex"]:
            result = closing_context.sensex_notice(a.session)
        else:
            result = closing_context.collect(a.session, selected=selected, membership=read(a.membership) if a.membership else None)
        return result, lambda r: closing_context.markdown(r, brief=a.brief)
    if a.collect_public:
        if a.now or a.calendar or a.events: raise Block("PUBLIC: use actual provider session; explicit inputs belong to --input mode")
        from public_market import collect_daily
        selected = selected_indices(a.command)
        try:
            bundle = collect_daily(selected)
        except Block:
            # No Yahoo session today: answer from NSE instead of stopping (closed market, or Yahoo down on a trading day).
            now = instant()
            try:
                closed, summary = closed_day(selected, now)
            except CONTEXT_ERRORS:
                raise Block("PUBLIC: no trading session found today and NSE's holiday list could not be read; "
                            "for the last session's close run daily.py with --collect-close --session YYYY-MM-DD") from None
            if closed is not None:
                return closed, lambda r: closed_markdown(r, brief=a.brief)
            bundle = {"session": nse_session(summary, now), "indices": {}, "submitted": False,
                      "context_errors": {"public_feed": "Yahoo public charts unavailable this run; NSE data only",
                                         **{k: "No public source for this index this run" for k in selected}}}
        import nse_live
        try: bundle["vix"] = nse_live.bounded_call(collect_vix, 2 * nse_live.CALL_BUDGET)
        except (Block, KeyError, TypeError, ValueError, OverflowError) + NETWORK_ERRORS:
            bundle["vix_error"] = "Public India VIX chart unavailable or inconsistent"
        now = instant()
        enrich_public(bundle, selected, now)
    elif a.collect_kite:
        if a.now or not a.calendar: raise Block("COLLECT: a dated calendar and live clock are required")
        from daily_collect import collect
        from kite_readonly import KiteReadOnly
        calendar = read(a.calendar)
        session_bounds(calendar, instant(), a.phase)
        bundle = collect(KiteReadOnly(), calendar, selected_indices(a.command), events=read(a.events) if a.events else None,
                         with_context=a.with_context, membership=read(a.membership) if a.membership else None)
        now = instant()
    else:
        if a.calendar or a.events: raise Block("INPUT: calendar/events flags belong to collection mode")
        bundle, now = demo(a.phase) if a.demo else (read(a.input), instant(a.now))
    return report(a.command, bundle, now, a.phase), lambda r: markdown(r, a.brief)


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("command", choices=[*COMMANDS, *("/" + c for c in COMMANDS)])
    p.add_argument("--input"); p.add_argument("--demo", action="store_true"); p.add_argument("--now")
    p.add_argument("--collect-kite", action="store_true", help="Opt-in read-only collection using local Kite credentials")
    p.add_argument("--collect-public", action="store_true", help="Credential-free public Yahoo index and India VIX observations; research only")
    p.add_argument("--collect-close", action="store_true", help="Dated public NSE closing reports with separately dated flow/GIFT context (Nifty 50, Bank Nifty)")
    p.add_argument("--session", help="Exact YYYY-MM-DD report date for --collect-close")
    p.add_argument("--calendar"); p.add_argument("--events")
    p.add_argument("--with-context", action="store_true"); p.add_argument("--membership")
    p.add_argument("--phase", choices=["auto", "preopen", "intraday", "close"], default="auto")
    p.add_argument("--format", choices=["markdown", "json"], default="markdown")
    p.add_argument("--brief", action="store_true", help="Short answer-first version"); p.add_argument("--out")
    a = p.parse_args(argv)
    # Only evidence collection/validation is converted into a BLOCKED status. Rendering and printing run outside
    # this handler so encoding or programming errors surface as errors instead of looking like missing data.
    try:
        result, render = build(a)
    except Block as exc:
        return blocked(str(exc))
    except (OSError, http.client.HTTPException) as exc:
        return blocked(f"SOURCE: input file or network source unavailable ({type(exc).__name__})")
    except UnicodeError:
        raise
    except (ValueError, KeyError, TypeError, OverflowError) as exc:
        return blocked(f"INPUT: missing or invalid field in the supplied data ({type(exc).__name__}: {str(exc)[:120]})")
    body = json.dumps(result, indent=2, allow_nan=False) if a.format == "json" else render(result)
    if a.out:
        path = Path(a.out)
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            with path.open("x", encoding="utf-8") as stream: stream.write(body)
        except OSError as exc:
            return blocked(f"OUTPUT: could not create {path.name} ({type(exc).__name__})")
    else:
        emit(body)
    if a.collect_close or "closed_today" in result:
        return 0 if any(i["status"] == "OBSERVED" for i in result["indices"].values()) or result.get("sensex_last") else 2
    return 2 if result["status"] == "BLOCKED" else 0


def exit_now(code):
    """Leave without joining abandoned worker threads: Python joins every ThreadPoolExecutor thread at exit, so one
    download that a slow server keeps dripping (each read inside its socket timeout) held a finished report open for
    over five minutes. The report is already written and flushed; nothing else is pending."""
    import os
    try:
        sys.stdout.flush(); sys.stderr.flush()
    finally:
        os._exit(int(code or 0))


if __name__ == "__main__": exit_now(main())
