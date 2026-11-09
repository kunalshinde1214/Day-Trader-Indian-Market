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
    if phase == "intraday" and data.get("quote") is not None:
        q = data["quote"]; provenance(q, now, "index quote", 60)
        if instant(q["as_of"]) < start: raise Block("QUOTE: not from current session")
        current = price(q["value"]); label = "fresh_index_quote"
    if rows:
        levels.update(session_open=rows[0]["open"], session_high=max(r["high"] for r in rows), session_low=min(r["low"] for r in rows))
    if len(rows) >= 3:
        levels.update(opening_range_high=max(r["high"] for r in rows[:3]), opening_range_low=min(r["low"] for r in rows[:3]))
    upper = max(levels["previous_high"], levels.get("opening_range_high", levels["previous_high"]))
    lower = min(levels["previous_low"], levels.get("opening_range_low", levels["previous_low"]))
    evidence_price = rows[-1]["close"] if rows else None
    condition = "PREOPEN_CONTEXT" if phase == "preopen" else "AWAITING_COMPLETED_BAR" if evidence_price is None else "ABOVE_REFERENCE_RANGE" if evidence_price > upper else "BELOW_REFERENCE_RANGE" if evidence_price < lower else "INSIDE_REFERENCE_RANGE"
    gaps = []
    try:
        br = breadth(data["breadth"], session, now, phase)
        if br["status"] != "COMPLETE": gaps.append("Constituent breadth is incomplete or unavailable")
    except (Block, KeyError, ValueError, TypeError, ArithmeticError):
        br = {"status": "UNAVAILABLE", "reason": "Missing or invalid sourced constituent evidence"}; gaps.append("Constituent breadth unavailable")
    sma = None
    if "history" in data:
        h = data["history"]; provenance(h, now, "daily history")
        dates = [date.fromisoformat(r["session"]) for r in h["rows"]]
        if dates != sorted(set(dates)) or not dates or dates[-1].isoformat() != session["previous_session"] or dates[-1] >= now.date():
            raise Block("HISTORY: ordered prior sessions ending at previous session required")
        values = []
        for r in h["rows"]:
            if instant(r["available_at"]) > now or instant(r["available_at"]).date() < date.fromisoformat(r["session"]):
                raise Block("HISTORY: invalid availability timestamp")
            values.append(price(r["close"]))
        if values[-1] != prev["close"]: raise Block("HISTORY: previous close disagrees with daily reference")
        if len(values) >= 20:
            sma = round(sum(v / 20 for v in values[-20:]), 3)
            if not math.isfinite(sma): raise Block("HISTORY: unsupported numeric range")
    if sma is None: gaps.append("20-observation historical moving average unavailable")
    return {"index_id": key, "name": INDICES[key][0], "status": "PARTIAL" if gaps else "READY", "source": data["source"],
            "as_of": data["as_of"], "phase": phase, "value": current, "value_basis": label, "completed_bars": len(rows),
            "change_from_previous_close_pct": percent_change(current, prev["close"]) if phase != "preopen" and (rows or label == "fresh_index_quote") else None,
            "condition": condition, "condition_basis": "latest completed five-minute close against previous-session/opening range",
            "levels": levels, "prior_sma20": sma, "breadth": br, "coverage_gaps": gaps,
            "scenarios": [
                {"name": "bullish", "condition": "Completed five-minute close above", "level": upper, "invalidated_by": "Completed close back at or below the level"},
                {"name": "bearish", "condition": "Completed five-minute close below", "level": lower, "invalidated_by": "Completed close back at or above the level"},
                {"name": "range", "condition": "Completed closes remain between", "lower": lower, "upper": upper}],
            "trade_ready": False, "note": "Spot-index points only. No spot-index volume/VWAP, probability, executable contract, lot size or order is inferred."}


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
    available = [r for r in result["indices"] if r["status"] != "BLOCKED"]
    result["coverage_gaps"].extend(str(v) for v in bundle.get("context_errors", {}).values())
    result["status"] = "BLOCKED" if not available else "READY" if all(r["status"] == "READY" for r in result["indices"]) and not result["coverage_gaps"] else "PARTIAL"
    result["coverage"] = {"requested": len(selected), "available": len(available), "blocked": len(selected) - len(available)}
    result["comparison"] = [{"name": r["name"], "change_pct": r["change_from_previous_close_pct"]} for r in available if r["change_from_previous_close_pct"] is not None]
    return result


def text_cell(value):
    return html.escape(str(value)).replace("\n", " ").replace("\r", " ").replace("|", "\\|").replace("[", "\\[").replace("]", "\\]")


def markdown(result, brief=False):
    lines = [f"# {result['command']} - {result['date']}", "", f"{result['phase']} | {result['status']} | {result['as_of']}", "",
             "**SYNTHETIC DEMO - not market data**" if result["mode"] == "SYNTHETIC_DEMO" else "Spot-index analysis; all levels are index points. No order submitted.", ""]
    if len(result["comparison"]) > 1:
        lines += ["| Index | Change from previous close |", "| --- | ---: |"]
        lines += [f"| {r['name']} | {r['change_pct']:+.3f}% |" for r in result["comparison"]]
        lines += [""]
    for r in result["indices"]:
        lines += ["## " + r["name"], ""]
        if r["status"] == "BLOCKED": lines += ["BLOCKED - " + text_cell(r["reason"]), ""]; continue
        lines += [f"**{r['value']:,.2f}** ({r['value_basis']}); {r['condition']}. Coverage: {r['status']}.",
                  f"Source: {text_cell(r['source'])}; snapshot {r['as_of']}; completed bars: {r['completed_bars']}.", "",
                  "| Reference | Index points |", "| --- | ---: |"]
        lines += [f"| {k.replace('_', ' ').title()} | {v:,.2f} |" for k, v in r["levels"].items()]
        lines += ["", "Conditional scenarios (not orders):"]
        for s in r["scenarios"]:
            bounds = f"{s['level']:,.2f}" if "level" in s else f"{s['lower']:,.2f} to {s['upper']:,.2f}"
            lines.append(f"- {s['name'].title()}: {s['condition'].lower()} {bounds}." + (" Invalidated by: " + s["invalidated_by"].lower() + "." if "invalidated_by" in s else ""))
        br = r["breadth"]
        if "observed" in br: lines += ["", f"Breadth: {br['advancing']} advancing, {br['declining']} declining, {br['unchanged']} unchanged; {br['observed']}/{br['expected']} observed.", "Membership: " + text_cell(br["membership_basis"]) + "."]
        if not brief:
            lines += ["", "Prior 20-observation daily SMA: " + (str(r["prior_sma20"]) if r["prior_sma20"] is not None else "unavailable"), r["note"]]
            if "strongest_observed" in br:
                lines += ["Strongest observed: " + ", ".join(text_cell(x["symbol"]) + f" {x['change_pct']:+.3f}%" for x in br["strongest_observed"])]
        lines += ["", *["Coverage: " + g + "." for g in r["coverage_gaps"]], ""]
    lines += ["## Events and coverage", ""]
    lines += ["- " + text_cell(e["headline"]) + " — " + text_cell(e["source"]) + " (" + e["published_at"] + ")" for e in result["events"]]
    if not result["events"]: lines += ["No event items supplied; consult the coverage status."]
    lines += ["- " + g for g in result["coverage_gaps"]]
    return "\n".join(lines) + "\n"


def demo(phase="intraday"):
    phase = "intraday" if phase == "auto" else phase
    now = instant({"preopen": "2026-11-06T09:00:00+05:30", "intraday": "2026-11-06T09:35:00+05:30", "close": "2026-11-06T16:00:00+05:30"}[phase])
    source = "synthetic_fixture_not_market_data"
    session = {"source": source, "as_of": now.isoformat(), "date": "2026-11-06", "previous_session": "2026-11-05", "is_open_day": True, "open": "09:15", "close": "15:30", "exchanges": ["NSE", "BSE"]}
    bundle = {"synthetic": True, "session": session, "indices": {}, "events": {"source": source, "as_of": now.isoformat(), "session": session["date"], "coverage": "partial", "items": []}}
    for key, base in (("nifty", 25000), ("sensex", 80000), ("banknifty", 55000)):
        count = 0 if phase == "preopen" else 4 if phase == "intraday" else 75
        start = now.replace(hour=9, minute=15, second=0)
        rows = [{"at": (start + timedelta(minutes=5*k)).isoformat(), "open": base+k*5, "high": base+k*5+12, "low": base+k*5-8, "close": base+k*5+6} for k in range(count)]
        bundle["indices"][key] = {"index_id": key, "source": source, "as_of": now.isoformat(), "price_basis": "spot_index_points",
            "previous": {"source": source, "as_of": "2026-11-05T16:00:00+05:30", "session": "2026-11-05", "open": base-50, "high": base+10, "low": base-100, "close": base-20},
            "candles": {"source": source, "as_of": now.isoformat(), "interval": "5minute", "rows": rows}}
    return bundle, now


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("command", choices=[*COMMANDS, *("/" + c for c in COMMANDS)])
    p.add_argument("--input"); p.add_argument("--demo", action="store_true"); p.add_argument("--now")
    p.add_argument("--collect-kite", action="store_true", help="Opt-in read-only collection using local Kite credentials")
    p.add_argument("--collect-public", action="store_true", help="Credential-free public Yahoo index observations; research only")
    p.add_argument("--calendar"); p.add_argument("--events")
    p.add_argument("--with-context", action="store_true"); p.add_argument("--membership")
    p.add_argument("--phase", choices=["auto", "preopen", "intraday", "close"], default="auto")
    p.add_argument("--format", choices=["markdown", "json"], default="markdown"); p.add_argument("--brief", action="store_true"); p.add_argument("--out")
    a = p.parse_args(argv)
    try:
        if sum((bool(a.input), a.demo, a.collect_kite, a.collect_public)) != 1: raise Block("INPUT: choose exactly one input mode")
        if a.demo and a.now: raise Block("DEMO: use its labelled fixed clock")
        if (a.with_context or a.membership) and not a.collect_kite or a.membership and not a.with_context: raise Block("INPUT: context flags require Kite collection")
        if a.collect_public:
            if a.now or a.calendar or a.events: raise Block("PUBLIC: use actual provider session; explicit inputs belong to --input mode")
            from public_market import collect_daily
            selected = list(INDICES) if a.command.removeprefix("/") == "india-daily" else [a.command.removeprefix("/")]
            bundle = collect_daily(selected); now = instant()
        elif a.collect_kite:
            if a.now or not a.calendar: raise Block("COLLECT: a dated calendar and live clock are required")
            from daily_collect import collect
            from kite_readonly import KiteReadOnly
            calendar = read(a.calendar)
            session_bounds(calendar, instant(), a.phase)
            selected = list(INDICES) if a.command.removeprefix("/") == "india-daily" else [a.command.removeprefix("/")]
            bundle = collect(KiteReadOnly(), calendar, selected, events=read(a.events) if a.events else None,
                             with_context=a.with_context, membership=read(a.membership) if a.membership else None)
            now = instant()
        else:
            if a.calendar or a.events: raise Block("INPUT: calendar/events flags belong to collection mode")
            bundle, now = demo(a.phase) if a.demo else (read(a.input), instant(a.now))
        result = report(a.command, bundle, now, a.phase)
        body = json.dumps(result, indent=2, allow_nan=False) if a.format == "json" else markdown(result, a.brief)
        if a.out:
            path = Path(a.out); path.parent.mkdir(parents=True, exist_ok=True)
            with path.open("x", encoding="utf-8") as stream: stream.write(body)
        else: print(body)
        return 2 if result["status"] == "BLOCKED" else 0
    except (Block, ValueError, KeyError, TypeError, OSError, OverflowError):
        print(json.dumps({"status": "BLOCKED", "reason": "Daily report input, calendar or output validation failed", "submitted": False})); return 2


if __name__ == "__main__": sys.exit(main())
