"""Daily Indian spot-index briefings. No account access or execution endpoints."""
from __future__ import annotations

import argparse
from datetime import date, datetime, time, timedelta
import html
import json
import math
from pathlib import Path
import sys

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


def completed(rows, start, end, now):
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
    count = max(0, int((min(now, end) - start).total_seconds() // 300))
    expected = [start + timedelta(minutes=5 * k) for k in range(count)]
    if [instant(r["at"]) for r in result] != expected:
        raise Block("BARS: incomplete opening/intervening/latest five-minute candles")
    return result


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
    rows = []
    if phase != "preopen":
        bars = data["candles"]
        provenance(bars, now, "candle snapshot", 120)
        if bars.get("interval") != "5minute": raise Block("BARS: five-minute interval required")
        rows = completed(bars["rows"], start, end, now)
    levels = {"previous_high": prev["high"], "previous_low": prev["low"], "previous_close": prev["close"]}
    current = rows[-1]["close"] if rows else prev["close"]
    label = "last_completed_candle_close" if rows else "previous_session_close"
    value_at = (instant(rows[-1]["at"]) + timedelta(minutes=5)).isoformat() if rows else None
    if phase == "intraday" and data.get("quote") is not None:
        q = data["quote"]; provenance(q, now, "index quote", 60)
        if instant(q["as_of"]) < start: raise Block("QUOTE: not from current session")
        current = price(q["value"]); label = "fresh_index_quote"; value_at = instant(q["as_of"]).isoformat()
    if phase == "close" and rows and data.get("official_close"):
        oc = data["official_close"]
        if oc.get("session") == session["date"]:
            current = price(oc["value"]); label = "official_daily_close"; value_at = end.isoformat()
    if rows:
        levels.update(session_open=rows[0]["open"], session_high=max(r["high"] for r in rows), session_low=min(r["low"] for r in rows))
    window = None
    if len(rows) >= 3:
        levels.update(opening_range_high=max(r["high"] for r in rows[:3]), opening_range_low=min(r["low"] for r in rows[:3]))
        window = instant(rows[0]["at"]).strftime("%H:%M") + "-" + (instant(rows[2]["at"]) + timedelta(minutes=5)).strftime("%H:%M")
    upper_basis = "opening_range_high" if levels.get("opening_range_high", -math.inf) > levels["previous_high"] else "previous_high"
    lower_basis = "opening_range_low" if levels.get("opening_range_low", math.inf) < levels["previous_low"] else "previous_low"
    upper, lower = levels[upper_basis], levels[lower_basis]
    evidence_price = rows[-1]["close"] if rows else None
    condition = "PREOPEN_CONTEXT" if phase == "preopen" else "AWAITING_COMPLETED_BAR" if evidence_price is None else "ABOVE_REFERENCE_RANGE" if evidence_price > upper else "BELOW_REFERENCE_RANGE" if evidence_price < lower else "INSIDE_REFERENCE_RANGE"
    gaps = []
    try:
        br = breadth(data["breadth"], session, now, phase)
        if br["status"] != "COMPLETE": gaps.append("Constituent breadth is incomplete or unavailable")
    except (Block, KeyError, ValueError, TypeError, ArithmeticError):
        br = {"status": "UNAVAILABLE", "reason": "Missing or invalid sourced constituent evidence"}; gaps.append("Constituent breadth unavailable")
    sma, history = None, []
    if "history" in data:
        history = daily_history(data["history"], session, now, prev)
        if len(history) >= 20:
            sma = round(sum(v / 20 for _, v in history[-20:]), 3)
            if not math.isfinite(sma): raise Block("HISTORY: unsupported numeric range")
    if sma is None: gaps.append("20-observation historical moving average unavailable")
    reference = history[-2] if len(history) >= 2 else None
    last_session = {"date": prior["session"], **prev, "previous_date": reference[0] if reference else None,
                    "previous_close": reference[1] if reference else None,
                    "change_points": point_change(prev["close"], reference[1]) if reference else None,
                    "change_pct": percent_change(prev["close"], reference[1]) if reference else None}
    has_current = phase != "preopen" and (bool(rows) or label == "fresh_index_quote")
    return {"index_id": key, "name": INDICES[key][0], "status": "PARTIAL" if gaps else "READY", "source": data["source"],
            "as_of": data["as_of"], "phase": phase, "session_date": session["date"], "value": current, "value_basis": label,
            "value_at": value_at, "completed_bars": len(rows),
            "previous_close_date": prior["session"],
            "change_from_previous_close_points": point_change(current, prev["close"]) if has_current else None,
            "change_from_previous_close_pct": percent_change(current, prev["close"]) if has_current else None,
            "last_session": last_session, "opening_range_window": window,
            "condition": condition, "condition_basis": "latest completed five-minute close against previous-session/opening range",
            "levels": levels, "prior_sma20": sma, "breadth": br, "coverage_gaps": gaps,
            "scenarios": [
                {"name": "bullish", "condition": "Completed five-minute close above", "level": upper, "level_basis": upper_basis, "invalidated_by": "Completed close back at or below the level"},
                {"name": "bearish", "condition": "Completed five-minute close below", "level": lower, "level_basis": lower_basis, "invalidated_by": "Completed close back at or above the level"},
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
        return {"status": "OBSERVED", "symbol": VIX_SYMBOL, "source": data["source"], "as_of": data["as_of"], "date": days[-1].isoformat(),
                "close": close, "high": high, "low": low, "previous_date": days[-2].isoformat() if before else None, "previous_close": previous,
                "change_points": point_change(close, previous) if before else None,
                "change_pct": percent_change(close, previous) if before else None,
                "provisional": days[-1].isoformat() == session_date and phase != "close"}
    except (Block, KeyError, ValueError, TypeError, AttributeError, ArithmeticError):
        return {"status": "UNAVAILABLE", "reason": "Missing, stale or invalid India VIX observation"}


def collect_vix(*, fetcher=None, clock=instant):
    """Public Yahoo daily chart for India VIX, using the same contract checks as the other public charts."""
    from public_market import collect_chart, request
    chart = collect_chart(VIX_SYMBOL, "1d", fetcher=fetcher or request, clock=clock)
    rows = [{"session": instant(b["at"]).date().isoformat(), **{k: b[k] for k in ("open", "high", "low", "close")}} for b in chart["bars"][-3:]]
    return {"source": chart["source"], "symbol": VIX_SYMBOL, "as_of": chart["as_of"], "rows": rows}


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
        try: item = index_report(key, bundle["indices"][key], bundle["session"], now, phase, start, end)
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
    available = [r for r in result["indices"] if r["status"] != "BLOCKED"]
    result["context_errors"] = {str(k): str(v) for k, v in bundle.get("context_errors", {}).items()}
    result["coverage_gaps"].extend(result["context_errors"].values())
    result["status"] = "BLOCKED" if not available else "READY" if all(r["status"] == "READY" for r in result["indices"]) and not result["coverage_gaps"] else "PARTIAL"
    result["coverage"] = {"requested": len(selected), "available": len(available), "blocked": len(selected) - len(available)}
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
        return f"Waiting for the first completed 5-minute bar of {day(r['session_date'])}. Last close on {day(s['date'])}: **{pts(s['close'])}**."
    rng = ""
    if "session_high" in r["levels"]:
        rng = f" {'Range so far' if r['phase'] == 'intraday' else day(r['session_date']) + ' range'}: {pts(r['levels']['session_low'])}-{pts(r['levels']['session_high'])}."
    if r["phase"] == "close":
        basis = "" if r["value_basis"] == "official_daily_close" else " (last 5-minute bar; official close may differ slightly)"
        return f"{day(r['session_date'])} close{basis}: **{pts(r['value'])}**, {change} vs {prev}.{rng}"
    basis = "index quote" if r["value_basis"] == "fresh_index_quote" else "last completed 5-minute bar"
    return f"At {clock_time(r['value_at'])} IST ({basis}): **{pts(r['value'])}**, {change} vs {prev}.{rng}"


def position(r):
    upper, lower = r["scenarios"][2]["upper"], r["scenarios"][2]["lower"]
    ub, lb = level_name(r["scenarios"][2]["upper_basis"], r), level_name(r["scenarios"][2]["lower_basis"], r)
    lead = "Finished" if r["phase"] == "close" else "Now"
    return {"PREOPEN_CONTEXT": f"Reference range for today: {pts(lower)} ({lb}) to {pts(upper)} ({ub}).",
            "AWAITING_COMPLETED_BAR": "Position: waiting for a completed 5-minute bar.",
            "ABOVE_REFERENCE_RANGE": f"{lead} above the reference range: over {pts(upper)} ({ub}).",
            "BELOW_REFERENCE_RANGE": f"{lead} below the reference range: under {pts(lower)} ({lb}).",
            "INSIDE_REFERENCE_RANGE": f"{lead} inside the reference range {pts(lower)} ({lb}) to {pts(upper)} ({ub})."}[r["condition"]]


def vix_line(v):
    if not v: return None
    if v["status"] != "OBSERVED": return "India VIX: unavailable."
    label = f"{day(v['date'])}, provisional" if v["provisional"] else day(v["date"])
    change = f", {change_text(v['change_points'], v['change_pct'])} vs {day(v['previous_date'])}" if v["previous_date"] else ""
    return f"India VIX ({label}): {pts(v['close'])}{change}."


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
        rng = f"{pts(c['low'])}-{pts(c['high'])}" if c["low"] is not None else "n/a"
        chg = "n/a" if c["change_points"] is None else f"{c['change_points']:+,.2f}"
        pct = "n/a" if c["change_pct"] is None else f"{c['change_pct']:+.2f}%"
        lines.append(f"| {display_name(names[c['index_id']], short=True)} | {when} | {pts(c['value'])} | {chg} | {pct} | {rng} |")
    v = result.get("vix")
    if v and v["status"] == "OBSERVED":
        rng = f"{pts(v['low'])}-{pts(v['high'])}" if v["low"] is not None else "n/a"
        chg = "n/a" if v["change_points"] is None else f"{v['change_points']:+,.2f}"
        pct = "n/a" if v["change_pct"] is None else f"{v['change_pct']:+.2f}%"
        lines.append(f"| India VIX | {day(v['date'])}{' (provisional)' if v['provisional'] else ''} | {pts(v['close'])} | {chg} | {pct} | {rng} |")
    return lines


def data_notes(result):
    notes, rows = [], [r for r in result["indices"] if r["status"] != "BLOCKED"]
    if any(is_yahoo(r) for r in rows):
        note = "Index data: Yahoo Finance public charts (unofficial; may be delayed or revised; research only)."
        if any(r["index_id"] == "sensex" for r in rows): note += " Sensex is Yahoo data, not an official BSE feed."
        notes.append(note)
    elif rows:
        notes.append("Index data: " + ", ".join(sorted({text_cell(r["source"]) for r in rows})) + ".")
    v = result.get("vix")
    if v and v["status"] == "OBSERVED":
        notes.append("India VIX: " + ("Yahoo Finance public chart (^INDIAVIX)." if str(v["source"]).lower().startswith("yahoo") else text_cell(v["source"]) + "."))
    elif v: notes.append("India VIX: " + text_cell(v.get("reason", "unavailable")) + ".")
    else: notes.append("India VIX: not collected by this input route.")
    errors = result.get("context_errors", {})
    if "calendar" in errors: notes.append("Session times and the previous-session date come from the data provider, not a verified exchange calendar.")
    for key, value in errors.items():
        if key in INDICES: notes.append(f"{INDICES[key][0]}: {text_cell(value)}.")
        elif key not in {"public_feed", "calendar"}: notes.append(text_cell(value).rstrip(".") + ".")
    if result["phase"] == "close" and any(x.get("value_basis") != "official_daily_close" for x in result.get("indices", [])):
        notes.append("Some closing figures use the last completed 5-minute bar, not the official published close.")
    without = [r["name"] for r in rows if r["breadth"].get("status") == "UNAVAILABLE"]
    if without and result["phase"] != "preopen": notes.append("Advance/decline breadth not available for " + ", ".join(without) + ".")
    no_sma = [r["name"] for r in rows if r["prior_sma20"] is None]
    if no_sma: notes.append("20-day average unavailable for " + ", ".join(no_sma) + " (fewer than 20 prior closes supplied).")
    if not result["events"]:
        notes.append("No reviewed event calendar supplied; that is not a sign of no event risk." if any("unavailable" in g for g in result["coverage_gaps"]) else "No event items in the supplied calendar.")
    elif "Events coverage is partial" in result["coverage_gaps"]: notes.append("Event coverage is partial.")
    notes.append("Spot-index points only: no volume/VWAP, contract, lot size or order. Nothing was submitted.")
    return notes


def blocked_text(r):
    return f"{display_name(r)}: unavailable - {text_cell(r['reason'])}."


def watch_line(r):
    """Conditional levels in one sentence; after the close, the levels the next session starts from."""
    up, down = r["scenarios"][0], r["scenarios"][1]
    if r["phase"] == "close":
        lv = r["levels"]
        return (f"Next session reference: {day(r['session_date'])} high {pts(lv['session_high'])} and low {pts(lv['session_low'])}"
                " (the next pre-open report uses these).")
    return (f"Watch (conditions, not orders): bullish on a completed 5-minute close above {pts(up['level'])} ({level_name(up['level_basis'], r)}), "
            f"bearish below {pts(down['level'])} ({level_name(down['level_basis'], r)}); a close back inside voids it, and between them is range-bound.")


def full_index(r, heading=True, context=None):
    lines = ["## " + display_name(r), ""] if heading else ([display_name(r) + ".", ""] if display_name(r) != r["name"] else [])
    if r["status"] == "BLOCKED": return lines + [blocked_text(r), ""]
    lines += [headline(r)] + ([] if r["phase"] == "preopen" else [position(r)]) + ([context] if context else [])
    if r["phase"] != "preopen":  # before the open the headline and watch line already carry every dated level
        lines += ["", "| Level | Points |", "| --- | ---: |"] + [f"| {level_name(k, r)} | {pts(v)} |" for k, v in r["levels"].items()]
    lines += ["", watch_line(r)]
    if sma_text(r): lines += ["", sma_text(r)]
    br = r["breadth"]
    if "observed" in br:
        lines += ["", f"Breadth: {br['advancing']} advancing, {br['declining']} declining, {br['unchanged']} unchanged ({br['observed']}/{br['expected']} members observed; membership: {text_cell(br['membership_basis'])})."]
        if br["strongest_observed"]:
            lines.append("Strongest: " + ", ".join(text_cell(x["symbol"]) + f" {x['change_pct']:+.2f}%" for x in br["strongest_observed"])
                         + ". Weakest: " + ", ".join(text_cell(x["symbol"]) + f" {x['change_pct']:+.2f}%" for x in br["weakest_observed"]) + ".")
    return lines + [""]


def brief_index(r):
    if r["status"] == "BLOCKED": return ["- " + blocked_text(r)]
    s, up, down = r["last_session"], r["scenarios"][0], r["scenarios"][1]
    lines = []
    if r["phase"] == "preopen":
        lines.append(f"- Last close ({day(s['date'])}): {pts(s['close'])}, {change_text(s['change_points'], s['change_pct'])}; range {pts(s['low'])}-{pts(s['high'])}.")
    else:
        lines.append("- " + headline(r).replace("**", ""))
        lines.append("- " + position(r))
    if r["phase"] == "close": lines.append("- " + watch_line(r))
    else: lines.append(f"- Watch completed 5-minute closes: bullish above {pts(up['level'])} ({level_name(up['level_basis'], r)}), "
                       f"bearish below {pts(down['level'])} ({level_name(down['level_basis'], r)}), range in between.")
    if sma_text(r): lines.append("- " + sma_text(r))
    return lines


def title(result):
    name = TITLES[result["command"].removeprefix("/")]
    when = PHASES[result["phase"]] + (f" ({clock_time(result['as_of'])} IST)" if result["phase"] == "intraday" else "")
    return f"{name} - {long_day(result['date'])}, {when}"


def markdown(result, brief=False):
    demo_mode = result["mode"] == "SYNTHETIC_DEMO"
    combined = len(result["indices"]) > 1
    rows = [r for r in result["indices"] if r["status"] != "BLOCKED"]
    vix = vix_line(result.get("vix"))
    if brief:
        lines = [f"**{title(result)}**" + (" - **SYNTHETIC DEMO - not market data**" if demo_mode else "")]
        if combined:
            lines += ["", *summary_table(result), "", "Next session reference levels:" if result["phase"] == "close" else "Watch completed 5-minute closes (conditions, not orders):"]
            for r in result["indices"]:
                if r["status"] == "BLOCKED": lines.append("- " + blocked_text(r)); continue
                up, down, lv = r["scenarios"][0], r["scenarios"][1], r["levels"]
                where = {"ABOVE_REFERENCE_RANGE": "above its range; ", "BELOW_REFERENCE_RANGE": "below its range; ",
                         "INSIDE_REFERENCE_RANGE": "inside its range; "}.get(r["condition"], "")
                if r["phase"] == "close":
                    lines.append(f"- {display_name(r, short=True)}: finished {where}next session reference {day(r['session_date'])} high {pts(lv['session_high'])}, low {pts(lv['session_low'])}.")
                else:
                    lines.append(f"- {display_name(r, short=True)}: {where}bullish above {pts(up['level'])} ({level_name(up['level_basis'], r)}), "
                                 f"bearish below {pts(down['level'])} ({level_name(down['level_basis'], r)}).")
        else:
            lines += brief_index(result["indices"][0])
            if vix: lines.append("- " + vix)
        source = "Yahoo public data, delayed/unofficial" if any(is_yahoo(r) for r in rows) else "supplied data"
        lines.append(f"- Notes: {source}; status {result['status']}; research only, no order. Run without --brief for levels and data notes.")
        return "\n".join(lines) + "\n"
    lines = ["# " + title(result), ""]
    lines += ["**SYNTHETIC DEMO - not market data**" if demo_mode else f"Report time {clock_time(result['as_of'])} IST. Status: {result['status']} (see data notes). Index points; research only.", ""]
    if combined and result["comparison"]:
        lines += summary_table(result) + [""]
    if combined:
        lines += [blocked_text(r) for r in result["indices"] if r["status"] == "BLOCKED"]
    for r in result["indices"]:
        if combined and r["status"] == "BLOCKED": continue
        lines += full_index(r, heading=combined, context=None if combined else vix)
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
        plain = text.replace("₹", "Rs ").replace("–", "-").replace("—", "-").replace("−", "-")
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
        bundle = collect_daily(selected_indices(a.command))
        try: bundle["vix"] = collect_vix()
        except (Block, KeyError, TypeError, ValueError, OSError, OverflowError):
            bundle["vix_error"] = "Public India VIX chart unavailable or inconsistent"
        now = instant()
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
    except OSError as exc:
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
    if a.collect_close:
        return 0 if any(i["status"] == "OBSERVED" for i in result["indices"].values()) else 2
    return 2 if result["status"] == "BLOCKED" else 0


if __name__ == "__main__": sys.exit(main())
