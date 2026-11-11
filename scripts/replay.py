"""Seeded same-time-of-day bootstrap adapted from trader-timemachine (MIT).

Conditioned on the observed entry, not a forecast from yesterday's close.
Retains open/previous-close discontinuities; stops fill at worse gap opens.
"""
from collections import Counter
from datetime import datetime, timedelta, time
import random

from core import Block, IST, instant, completed_bars, plan_math, charges, fingerprint, integer, number, setup_hit, plan_from_hit, validate_config, validate_rates, validate_gap_reference
from patterns import DAILY_SETUPS, daily_reference
from replay_sampling import policy, draw_slots, limitation


def outcome(bar, stop, target, long):
    o, h, l, c = bar
    if (o <= stop if long else o >= stop):
        return "stop", o
    if (o >= target if long else o <= target):
        return "target", target  # no favorable price improvement assumed
    if (l <= stop if long else h >= stop):
        return "stop", stop  # unknown intrabar order: stop first
    if (h >= target if long else l <= target):
        return "target", target
    return None, c


def replay(p, instrument, history, config, card):
    validate_config(config)
    from analysis_rules import check_policy
    check_policy(p, config)
    validate_rates(instrument, card, config["broker"])
    plan_math(p, instrument)
    if p["setup"] != config.get("setup", "orb"):
        raise Block("SETUP: replay requires the plan's configured setup")
    if p["setup"] == "gap_and_go" and number(p["gap_min_pct"]) != number(config.get("gap_min_pct", 0.7)):
        raise Block("SETUP: gap threshold changed; regenerate plan and replay")
    if p["broker"] != config["broker"] or p.get("account_id") != config["account_id"] or history.get("broker") != config["broker"]:
        raise Block("HISTORY: broker/account identity mismatch")
    if history["symbol"] != p["symbol"] or history["exchange"] != p["exchange"] or str(history["token"]) != str(p["token"]):
        raise Block("HISTORY: requires the exact instrument, not an underlying proxy")
    if not history.get("source"):
        raise Block("HISTORY: source required")
    plan_time = instant(p["as_of"])
    cutoff = min(instant(history["as_of"]), plan_time)
    day = plan_time.date().isoformat()
    # All historical observations must precede plan date, even if fetched later.
    rows = completed_bars(history["bars"], cutoff)
    sessions = {}
    for b in rows:
        ts = instant(b["at"])
        if ts.date() >= plan_time.date():
            continue
        sessions.setdefault(ts.date().isoformat(), {})[ts.strftime("%H:%M")] = b
    start = datetime.combine(plan_time.date(), time(9,15), IST)
    end = datetime.combine(plan_time.date(), time.fromisoformat(p["flat_by"]), IST)
    if plan_time.second or plan_time.microsecond or plan_time.minute % 5 or not start < plan_time < end:
        raise Block("REPLAY_CLOCK: completed five-minute entry before flat-by required")
    keys = [(start+timedelta(minutes=5*k)).strftime("%H:%M") for k in range(int((end-start).total_seconds()//300))]
    complete = [(d, [s[k] for k in keys]) for d,s in sorted(sessions.items()) if all(k in s for k in keys)]
    if len(complete) < integer(config["minimum_history_sessions"]):
        raise Block(f"HISTORY: {len(complete)} complete sessions; need {config['minimum_history_sessions']}")
    references = history.get("prior_closes", {})
    if p["setup"] == "gap_and_go":
        if not isinstance(references, dict):
            raise Block("GAP_REFERENCE: history requires a prior_closes map")
        for d, _ in complete:
            validate_gap_reference(references.get(d), instrument, d)
    daily_refs = history.get("daily_references", {})
    if p["setup"] in DAILY_SETUPS:
        if not isinstance(daily_refs, dict):
            raise Block("DAILY_REFERENCE: history requires daily_references map")
        for d, _ in complete:
            daily_reference(daily_refs.get(d), instrument, d)
    # Adapted build_pool: own instrument bars divided by preceding close at the same slot.
    pool=[[] for _ in keys]
    for _, bars in complete:
        for k in range(1,len(keys)):
            prev=float(bars[k-1]["close"])
            pool[k].append(tuple(float(bars[k][field])/prev for field in ("open","high","low","close")))
    first=int((plan_time-start).total_seconds()//300)
    n=integer(config["replay_paths"])
    if n > 100000:
        raise Block("REPLAYS: maximum 100000 paths")
    rng=random.Random(config["replay_seed"])
    sampling=policy(config)
    tail=pool[first:]
    long=p["side"]=="BUY"
    counts=Counter()
    all_costs=0.0
    lot=integer(instrument["lot_size"])
    cached={}
    for _ in range(n):
        price=float(p["entry"])
        state=None
        for ratios in draw_slots(tail,rng,sampling):
            bar=tuple(price*x for x in ratios)
            state,price=outcome(bar,float(p["stop"]),float(p["target"]),long)
            if state:
                break
        counts[state or "still_open"]+=1
        if price not in cached:
            cached[price]=charges(p["entry"],price,lot,p["side"],instrument,card,day)["total"]
        all_costs+=cached[price]
    historical=Counter()
    fired=0
    for d,bars in complete:
        hist_now=datetime.combine(datetime.fromisoformat(d).date(),time.fromisoformat(p["flat_by"]),IST)
        hit=setup_hit(bars,hist_now,config,instrument,references.get(d) if isinstance(references,dict) else None,
                      daily_refs.get(d) if isinstance(daily_refs,dict) else None)
        if hit["state"]!="triggered" or hit["side"]!=p["side"]:
            continue
        trigger=instant(hit["trigger_at"])
        if not time.fromisoformat(config["no_entries_before"]) <= trigger.time() <= time.fromisoformat(config["no_entries_after"]):
            continue
        hp=plan_from_hit(hit,{**instrument,"master_date":d},config,trigger,rows=bars)
        state=None
        for b in bars:
            if instant(b["at"]) < trigger:
                continue
            state,_=outcome(tuple(float(b[f]) for f in ("open","high","low","close")),hp["stop"],hp["target"],long)
            if state:
                break
        historical[state or "still_open"]+=1
        fired+=1
    result = {"plan_hash":fingerprint(p),"history_hash":fingerprint(history),"as_of":p["as_of"],
            "source":history["source"],"paths":n,"seed":config["replay_seed"],"history_sessions":len(complete),
            "dropped_incomplete_sessions":len(sessions)-len(complete),
            "target_first_pct":round(100*counts["target"]/n,2),"stop_first_pct":round(100*counts["stop"]/n,2),
            "still_open_pct":round(100*counts["still_open"]/n,2),
            "historical_setup":p["setup"],"historical_rule_version":p.get("rule_version",1),
            "historical_same_side_firings":fired,"historical_outcomes":dict(historical),
            "historical_target_first_pct":round(100*historical["target"]/fired,2) if fired else None,
            "costs": {"quantity":lot,"average_round_trip_estimate_inr":round(all_costs/n,2),"reprice_after_sizing":True},
            "sampling":sampling,
            "method":f"Same-slot {sampling['mode']} bootstrap conditioned on observed entry; stop first in ambiguous bars; adverse gap-open stop fills.",
            "limitations":["Synthetic frequencies are not calibrated probabilities; repeated draws are not additional historical evidence.",limitation(sampling),"No pre-open gap forecast, slippage model, order queue or liquidity simulation.","History is conditional on this exact instrument and may be insufficient for short-lived option contracts."]}
    if p["setup"] == "orb":
        result["historical_same_side_orb_firings"] = fired
    elif p["setup"] == "vwap_reclaim":
        result["limitations"].append("VWAP is estimated from five-minute HLC3 and bar volume, not individual trades.")
    elif p["setup"] == "gap_and_go":
        result["historical_gap_reference_sessions"] = len(complete)
        result["limitations"].append("Gap matching uses supplied prior-session close/calendar evidence; bootstrap paths remain conditioned on the observed entry, not gap-matched or pre-open forecasts.")
    elif p["setup"] in DAILY_SETUPS:
        result["historical_daily_reference_sessions"] = len(complete)
        result["limitations"].append("Daily-context matching uses supplied point-in-time calendar and corporate-action assertions, not an independently reconstructed survivorship-free universe.")
    return result
