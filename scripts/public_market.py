"""Credential-free Indian public market research. Never execution evidence."""
import argparse
import csv
from datetime import datetime, time as clock_time, timedelta
import hashlib
import http.client
import io
import json
from pathlib import Path
import random
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

from core import Block, IST, instant, integer, positive, number, fingerprint
from analysis_rules import screen, entry, exits, prefix, data_end, until
from position_size import inr, range_sizes, range_markdown
from trader import read

INDEX_SYMBOLS = {"nifty": "^NSEI", "banknifty": "^NSEBANK", "sensex": "^BSESN"}
UNIVERSE_URL = "https://nsearchives.nseindia.com/content/equities/EQUITY_L.csv"
LIMIT = 12*1024*1024
WARNING = "Public/unofficial feed; may be delayed, revised or unavailable. Research only; not broker-grade execution evidence."


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs): raise Block("PUBLIC: redirect refused")


TIMEOUT = 20
BACKOFF = (1.0, 3.0)   # waits before the 2nd and 3rd attempts
BUDGET = 45.0          # no new attempt starts once this many seconds (plus the wait) have passed


def transient(exc):
    """True for failures worth one more polite try: a timeout, a dropped/reset connection or a body cut short
    (IncompleteRead), HTTP 429 or a 5xx. Other 4xx answers (refused, not found) are final."""
    if isinstance(exc, urllib.error.HTTPError):
        return exc.code == 429 or 500 <= exc.code <= 599
    return isinstance(exc, (TimeoutError, ConnectionError, http.client.IncompleteRead, http.client.RemoteDisconnected,
                            http.client.BadStatusLine, urllib.error.URLError))


def request(url, *, timeout=None, sleep=time.sleep, clock=time.monotonic, backoff=BACKOFF, budget=BUDGET):
    """GET one allowlisted public URL. Transient failures are retried with backoff (~1 s, then ~3 s) while the total
    stays within ``budget`` seconds; everything else, and a final failure, raises Block."""
    parsed = urllib.parse.urlsplit(url)
    if parsed.scheme != "https" or parsed.hostname not in {"query1.finance.yahoo.com", "feeds.finance.yahoo.com", "nsearchives.nseindia.com"} or parsed.username or parsed.password or parsed.port not in {None,443}:
        raise Block("PUBLIC: unsupported source")
    req = urllib.request.Request(url, headers={"User-Agent":"IndiaDayTrader/0.9 (public research)", "Accept":"application/json,text/csv,application/xml,text/xml"})
    started = clock()
    waits = list(backoff)
    while True:
        try:
            with urllib.request.build_opener(NoRedirect()).open(req, timeout=timeout or TIMEOUT) as response: raw=response.read(LIMIT+1)
            if len(raw)>LIMIT: raise Block("PUBLIC: oversized response")
            return raw
        except Block: raise
        except (OSError, ValueError, http.client.HTTPException) as exc:
            # retry only if the wait plus one more full timeout still fits the budget (worst case about 41 s at the defaults)
            if not transient(exc) or not waits or clock() - started + waits[0] + (timeout or TIMEOUT) > budget:
                raise Block("PUBLIC: source unavailable; no login or bypass") from None
            sleep(waits.pop(0))


def symbol(value):
    if not isinstance(value,str) or not (value in INDEX_SYMBOLS.values() or value == "^INDIAVIX" or re.fullmatch(r"[A-Z0-9&_.-]+\.(?:NS|BO)",value)):
        raise Block("PUBLIC: use an exact Indian Yahoo symbol ending .NS/.BO, or a supported index")
    return value


def chart(raw, selected, interval, now):
    symbol(selected)
    if interval not in {"1d","5m","1m"}: raise Block("PUBLIC: unsupported interval")
    data=json.loads(raw, parse_constant=lambda _: (_ for _ in ()).throw(Block("PUBLIC: nonfinite JSON"))) if isinstance(raw,(str,bytes)) else raw
    content=data["chart"]
    if content.get("error") is not None or not isinstance(content.get("result"),list) or len(content["result"]) != 1: raise Block("PUBLIC: chart error/empty result")
    r=content["result"][0]; meta=r["meta"]
    if meta["symbol"] != selected or meta.get("currency") != "INR" or meta.get("exchangeTimezoneName") not in {"Asia/Kolkata","Asia/Calcutta"} or meta.get("dataGranularity") != interval:
        raise Block("PUBLIC: wrong instrument/currency/timezone/interval")
    stamps=r["timestamp"]; quote=r["indicators"]["quote"]
    if not isinstance(stamps,list) or not stamps or len(stamps)>100000 or len(quote)!=1: raise Block("PUBLIC: missing chart observations")
    q=quote[0]
    if any(not isinstance(q.get(k),list) or len(q[k])!=len(stamps) for k in ("open","high","low","close","volume")):
        raise Block("PUBLIC: misaligned arrays")
    rows=[];missing=[];seen=set();dropped=None
    step={"5m":300,"1m":60}.get(interval)
    if step and len(stamps)>1 and integer(stamps[-1])%step and datetime.fromtimestamp(integer(stamps[-1]),IST)<=now:
        # Yahoo appends the still-forming bar stamped with the latest trade time; it is incomplete and off the candle grid.
        dropped=datetime.fromtimestamp(integer(stamps[-1]),IST).isoformat();stamps=stamps[:-1]
    for n,stamp in enumerate(stamps):
        at=datetime.fromtimestamp(integer(stamp),IST)
        if at in seen or at>now: raise Block("PUBLIC: duplicate/future bar")
        seen.add(at)
        if any(q[k][n] is None for k in ("open","high","low","close")) or q["volume"][n] is None and not selected.startswith("^"):
            missing.append(at.isoformat());continue
        o,h,l,c=[positive(q[k][n]) for k in ("open","high","low","close")]
        v=number(q["volume"][n] or 0)
        if not l<=min(o,c)<=max(o,c)<=h: raise Block("PUBLIC: contradictory OHLC")
        rows.append({"at":at.isoformat(),"open":float(o),"high":float(h),"low":float(l),"close":float(c),"volume":float(v)})
    if not rows or rows!=sorted(rows,key=lambda b:instant(b["at"])): raise Block("PUBLIC: empty/unordered chart")
    last=rows[-1]["at"]
    return {"schema":"public_chart_v1","symbol":selected,"interval":interval,"source":"yahoo_public_chart",
            "source_url":"https://finance.yahoo.com/quote/"+urllib.parse.quote(selected,safe="")+"/", "as_of":now.isoformat(),
            "last_bar_at":last,"last_bar_age_seconds":(now-instant(last)).total_seconds(),"bars":rows,"missing_bars":missing,"dropped_in_progress_bar":dropped,
            "regular_session":meta.get("currentTradingPeriod",{}).get("regular"),"corporate_actions":r.get("events",{}),
            "price_basis":"unadjusted_provider_OHLC", "warning":WARNING,"execution_eligible":False,"submitted":False}


def collect_chart(selected, interval="1d", *, fetcher=request, clock=instant):
    symbol(selected)
    if interval not in {"1d","5m","1m"}: raise Block("PUBLIC: unsupported interval")
    query=urllib.parse.urlencode({"range":{"1d":"6mo","5m":"1mo","1m":"5d"}[interval],"interval":interval,"includePrePost":"false","events":"div,splits"})
    raw=fetcher("https://query1.finance.yahoo.com/v8/finance/chart/"+urllib.parse.quote(selected,safe="")+"?"+query)
    result=chart(raw,selected,interval,clock());result["raw_sha256"]=hashlib.sha256(raw).hexdigest();return result


def universe(body, now):
    rows=list(csv.DictReader(io.StringIO(body.decode("utf-8-sig")),skipinitialspace=True))
    if not rows or not {"SYMBOL","NAME OF COMPANY","SERIES","ISIN NUMBER"}<=rows[0].keys(): raise Block("UNIVERSE: unexpected NSE CSV")
    out=[];seen=set()
    for r in rows:
        if r["SERIES"].strip()!="EQ":continue
        s=symbol(r["SYMBOL"].strip()+".NS")
        if s in seen or not r["ISIN NUMBER"].strip():raise Block("UNIVERSE: duplicate/missing identity")
        seen.add(s);out.append({"symbol":s,"exchange":"NSE","name":r["NAME OF COMPANY"].strip(),"isin":r["ISIN NUMBER"].strip()})
    if not out:raise Block("UNIVERSE: no supported equities")
    return {"source":UNIVERSE_URL,"as_of":now.isoformat(),"members":out,"scope":"NSE EQ listing, not index membership or trading eligibility",
            "effective_date_verified":False,"execution_eligible":False,"raw_sha256":hashlib.sha256(body).hexdigest()}


def scan(symbols, *, fetcher=request, clock=instant, sleep=time.sleep):
    if not isinstance(symbols,list) or not 1<=len(symbols)<=500 or len(set(symbols))!=len(symbols):raise Block("SCAN: 1..500 unique symbols required")
    results=[];errors=[]
    for n,s in enumerate(symbols):
        symbol(s)
        if s.startswith("^"): raise Block("SCAN: equity symbols required")
        if n:sleep(0.5)
        try:
            history=collect_chart(s,fetcher=fetcher,clock=clock)
            if history["corporate_actions"].get("splits"): raise Block("SCREEN: split-adjusted research needed")
            if history["missing_bars"]:raise Block("SCREEN: incomplete daily data")
            if (clock().date()-instant(history["last_bar_at"]).date()).days>7:raise Block("SCREEN: old history")
            results.append({"symbol":s,**screen(history["bars"],clock()),"source":history["source"],"as_of":history["as_of"],"corporate_actions":history["corporate_actions"]})
        except (Block,KeyError,TypeError,ValueError,OSError,OverflowError):errors.append({"symbol":s,"reason":"Unavailable, stale, incomplete or split-affected public history"})
    return {"status":"PARTIAL" if errors else "READY","as_of":clock().isoformat(),"requested":len(symbols),"processed":len(results),
            "results":results,"errors":errors,"warning":WARNING,"submitted":False,"execution_eligible":False}


def replay_history(history, now):
    """A live chart cut back to an earlier moment for a replay of a past session (--now on an earlier date): only bars that
    started before `now`, stamped as collected then. The date check is in main()."""
    if history.get("schema")!="public_chart_v1":raise Block("ENTRY: public chart contract required")
    bars=[b for b in history["bars"] if instant(b["at"])<now]
    if not any(instant(b["at"]).date()==now.date() for b in bars):
        raise Block(f"REPLAY: the chart has no {history.get('interval','5m')} bars for {now:%d %b %Y} (Yahoo keeps about a month of 5-minute bars)")
    return {**history,"bars":bars,"as_of":now.isoformat(),"last_bar_at":bars[-1]["at"],"replay_of":now.date().isoformat()}


def public_entry(history, now, **options):
    if history.get("schema")!="public_chart_v1" or history.get("execution_eligible") is not False:raise Block("ENTRY: public chart contract required")
    symbol(history["symbol"])
    if history["symbol"].startswith("^"):raise Block("ENTRY: equity observations required")
    interval={"1m":1,"5m":5}.get(history["interval"])
    if interval is None:raise Block("ENTRY: intraday chart required")
    if instant(history["as_of"])>now or (now-instant(history["as_of"])).total_seconds()>120:raise Block("ENTRY: stale collection")
    result=entry(history["bars"],now,interval=interval,**options)
    view=session_view(history["bars"],now,interval,result,options.get("not_before","09:30"),options.get("not_after","14:30"))
    return {**result,**view,"symbol":history["symbol"],"source":history["source"],"as_of":now.isoformat(),"chart_hash":fingerprint(history),
            "bars":history["bars"],"warning":WARNING,"execution_eligible":False,"submitted":False}


def r2(value):
    return None if value is None else round(float(value),2)


def session_view(rows, now, interval, result, not_before="09:30", not_after="14:30"):
    """What a person needs to act on the check: today's completed bars, the opening range, the trigger level, the next time a
    completed bar can confirm, the range-based stops, and a reason that says exactly what is being waited for."""
    cap=data_end(rows,now,interval);done=prefix(until(rows,cap),cap,interval);n=15//interval;side=result["side"];buy=side=="BUY"
    start=now.replace(hour=9,minute=15,second=0,microsecond=0);close_at=start.replace(hour=15,minute=30)
    out={"bars_completed":len(done),"session_over":now>=close_at,
         "data_ends_at":cap.strftime("%H:%M") if cap<close_at else None,"opening_range":None,"last_close":None,"trigger_level":r2(result.get("reference_price")),"next_check_at":None,"stops":None}
    if done:
        last=done[-1];out["last_close"]={"price":r2(last["close"]),"bar":instant(last["at"]).strftime("%H:%M"),
                                         "completed_at":(instant(last["at"])+timedelta(minutes=interval)).strftime("%H:%M")}
        first=done[:n];high=max(float(b["high"]) for b in first);low=min(float(b["low"]) for b in first)
        out["opening_range"]={"high":r2(high),"low":r2(low),"mid":r2((high+low)/2),"complete":len(done)>=n,"bars":len(first),"of":n,
                              "window":"09:15-09:30 IST"}
        if out["opening_range"]["complete"]:
            out["stops"]={"or-mid":r2((high+low)/2),"or-low" if buy else "or-high":r2(low if buy else high)}
            if result["setup"]=="orb":out["trigger_level"]=r2(high if buy else low)
    boundary=start+timedelta(minutes=interval*(len(done)+1))
    if result["setup"]=="orb":boundary=max(boundary,start+timedelta(minutes=15+interval))
    first_allowed=datetime.combine(now.date(),clock_time.fromisoformat(not_before),now.tzinfo)
    while boundary<first_allowed:boundary+=timedelta(minutes=interval)
    window_closed=boundary.time()>clock_time.fromisoformat(not_after) or now>=close_at
    ends=datetime.combine(now.date(),clock_time.fromisoformat(not_after),now.tzinfo)
    out["entry_window"]={"from":not_before,"to":not_after,"closed":window_closed,
                         "minutes_left":None if window_closed else max(0,int((ends-now).total_seconds()//60))}
    if result["state"]!="triggered" and not window_closed:out["next_check_at"]=boundary.strftime("%H:%M")
    word="above" if buy else "below";level=out["trigger_level"]
    if result["state"]=="triggered":
        out["reason"]=(f"Triggered: the {interval}-minute bar completed at {instant(result['trigger_at']).strftime('%H:%M')} closed at "
                       f"{result['entry']:,.2f}, {word} {result['reference_price']:,.2f}")
    elif result["setup"]=="orb" and not (out["opening_range"] or {}).get("complete"):
        out["reason"]=(f"Opening range still forming: {len(done)} of {n} {interval}-minute bars complete; the range is set when the "
                       f"09:25 bar completes at 09:30 IST")
    elif window_closed:
        out["reason"]=f"No completed {interval}-minute close {word} {level:,.2f} inside the {not_before}-{not_after} IST entry window; nothing to act on today"
    else:
        out["reason"]=(f"Range complete; waiting for a completed {interval}-minute close {word} {level:,.2f} "
                       f"(next check after {out['next_check_at']} IST)")
    return out


def orb_tally(rows, now, *, side="BUY", interval=5, target_r=2, not_before="09:30", not_after="14:30"):
    """Opening-range breakouts on the earlier sessions in this chart: on how many sessions a close broke the range in the entry
    window and, from that close, whether the stop or a 2R target was touched first (stop counted first when one bar touches both)."""
    days=sorted({instant(b["at"]).date() for b in rows if instant(b["at"]).date()<now.date()})
    buy=side=="BUY";sign=1 if buy else -1
    out={"sessions":0,"skipped_incomplete":0,"triggers":0,"target_r":target_r,"interval_minutes":interval,"side":side,"entry_window":[not_before,not_after],
         "stops":{name:{"target_first":0,"stop_first":0,"neither":0,"neither_r_at_close":[]} for name in ("or-mid","or-low" if buy else "or-high")}}
    for day in days:
        own=sorted((b for b in rows if instant(b["at"]).date()==day),key=lambda b:instant(b["at"]))
        # Every bar through the end of the entry window must exist to find the trigger; after it, Yahoo often omits a late bar
        # (commonly 15:20), so the walk to 15:30 uses the bars that exist and counts the sessions with such gaps.
        end=datetime.combine(day,clock_time.fromisoformat(not_after),now.tzinfo)+timedelta(minutes=interval)
        try:
            hit=entry([b for b in own if instant(b["at"])<end],end,setup="orb",side=side,interval=interval,not_before=not_before,not_after=not_after)
        except Block:
            out["skipped_incomplete"]+=1;continue
        bars=[b for b in own if instant(b["at"]).time()<clock_time(15,30)]
        if len(bars)<375//interval:out["sessions_with_late_gaps"]=out.get("sessions_with_late_gaps",0)+1
        out["sessions"]+=1
        if hit["state"]!="triggered":continue
        out["triggers"]+=1
        price=float(hit["entry"]);high,low=float(hit["range_high"]),float(hit["range_low"]);at=instant(hit["trigger_at"])
        later=[b for b in bars if instant(b["at"])>=at]
        for name,stop in (("or-mid",(high+low)/2),("or-low" if buy else "or-high",low if buy else high)):
            risk=(price-stop)*sign;bucket=out["stops"][name]
            if risk<=0:bucket["neither"]+=1;continue
            target=price+sign*target_r*risk;result=None
            for b in later:
                if (float(b["low"])<=stop if buy else float(b["high"])>=stop):result="stop_first";break
                if (float(b["high"])>=target if buy else float(b["low"])<=target):result="target_first";break
            if result:bucket[result]+=1
            else:
                bucket["neither"]+=1;bucket["neither_r_at_close"].append(round((float(bars[-1]["close"])-price)*sign/risk,2))
    out["first_session"]=days[0].isoformat() if days else None;out["last_session"]=days[-1].isoformat() if days else None
    out["note"]="Small-sample history from this chart only; not a forecast or probability. No charges, slippage or gaps beyond the bars."
    return out


def stops_touched(r):
    """'; since the 13:55 trigger the price has reached the range-midpoint stop ₹1,366.65' from the completed bars after the
    trigger bar, or ''."""
    stops=r.get("stops") or {};bars=r.get("bars") or [];buy=r["side"]=="BUY"
    if not stops or not r.get("trigger_at") or not bars:return ""
    at=instant(r["trigger_at"]);interval=timedelta(minutes=r.get("interval_minutes") or 5);now=instant(r["as_of"])
    after=[b for b in bars if instant(b["at"])>=at and instant(b["at"])+interval<=now]   # trigger_at = the trigger bar's completion
    if not after:return ""
    extreme=min(float(b["low"]) for b in after) if buy else max(float(b["high"]) for b in after)
    hit=[(k,v) for k,v in stops.items() if (extreme<=v if buy else extreme>=v)]
    if not hit:return ""
    names=" and ".join(f"{'range-midpoint' if k=='or-mid' else 'range-low' if k=='or-low' else 'range-high'} stop {inr(v,2)}" for k,v in hit)
    return f"; since the {at.strftime('%H:%M')} trigger the price has reached the {names} (completed bars)"


def position_line(r):
    """One plain line on where the latest completed close sits against the opening range and how far the trigger is."""
    rng=r.get("opening_range") or {};last=r.get("last_close") or {}
    if not rng.get("complete") or last.get("price") is None:return None
    price,high,low=last["price"],rng["high"],rng["low"];buy=r["side"]=="BUY"
    where="above the range high" if price>high else "below the range low" if price<low else "inside the range"
    when=(f"at the end of the data (bar {last['bar']}, completed {last['completed_at']} IST)" if r.get("session_over")
          else f"now (last completed close, {last['completed_at']} IST)")
    text=f"{inr(price,2)} {when} — {where} ({inr(low,2)}–{inr(high,2)})"
    trigger=r.get("trigger_level")
    if r["state"]=="triggered":return text+("" if r.get("session_over") else stops_touched(r))+"."
    if trigger is None or r.get("session_over"):return text+"."
    gap=(trigger-price)/price*100 if buy else (price-trigger)/price*100
    side="long trigger (close above" if buy else "short trigger (close below"
    if gap<=0:return text+f"; the {side} {inr(trigger,2)}) is already crossed, waiting for a close inside the entry window."
    return text+f"; the {side} {inr(trigger,2)}) is {gap:.1f}% away."


def window_text(r):
    """' · entry window closes 14:30 IST (43 min left)' or ' · entry window closed for today' (not once triggered)."""
    w=r.get("entry_window")
    if not w or r["state"]=="triggered":return ""
    if w["closed"]:return f" · entry window ({w['from']}–{w['to']} IST) closed for today"
    return f" · entry window closes {w['to']} IST ({w['minutes_left']} min left)"


STOP_NAMES={"or-mid":"range midpoint","or-low":"range low","or-high":"range high"}


def review_outcomes(r):
    """After the session: from a trigger, which of each range stop and its 2R target today's completed bars touched first
    (the stop when one bar touches both), with the size for that stop when --risk was given. [] unless triggered."""
    if r["state"]!="triggered" or not r.get("stops") or not r.get("trigger_at"):return []
    buy=r["side"]=="BUY";sign=1 if buy else -1;price=float(r["entry"]);at=instant(r["trigger_at"])
    step=timedelta(minutes=r.get("interval_minutes") or 5);now=instant(r["as_of"])
    end=datetime.combine(now.date(),clock_time.fromisoformat(r["data_ends_at"]),now.tzinfo) if r.get("data_ends_at") else now
    bars=sorted((b for b in r.get("bars") or [] if at<=instant(b["at"]) and instant(b["at"])+step<=min(now,end)),key=lambda b:instant(b["at"]))
    sizes={x["stop_rule"]:x for x in (r.get("sizing") or {}).get("sizes",[]) if "quantity" in x}
    out=[]
    for key,stop in r["stops"].items():
        risk=(price-stop)*sign
        if risk<=0:continue
        target=price+sign*2*risk;first=None
        for b in bars:
            if (float(b["low"])<=stop if buy else float(b["high"])>=stop):first=("stop",b);break
            if (float(b["high"])>=target if buy else float(b["low"])<=target):first=("target",b);break
        name=STOP_NAMES.get(key,key);size=sizes.get(name)
        item={"stop_rule":name,"stop":stop,"target_2r":round(target,2),"first":first[0] if first else None,
              "at":(instant(first[1]["at"])+step).strftime("%H:%M") if first else None,"quantity":size["quantity"] if size else None}
        if first is None and bars:
            last=float(bars[-1]["close"]);item["r_at_end"]=round((last-price)*sign/risk,2);item["last_close"]=last
            item["pnl_at_end"]=round((last-price)*sign*size["quantity"],2) if size else None
        elif first and size:
            item["pnl"]=-size["loss_at_stop"] if first[0]=="stop" else size["gain_at_target"]
        out.append(item)
    return out


def review_lines(r):
    """Plain lines for the after-close review: the stop outcomes and where the data ends."""
    lines=[]
    for o in review_outcomes(r):
        qty=f" with {o['quantity']:,} shares" if o.get("quantity") else ""
        if o["first"]=="stop":
            tail=f", a loss of ≈ {inr(-o['pnl'])} including charges" if o.get("pnl") is not None else " (−1R)"
            lines.append(f"- {o['stop_rule'].capitalize()} stop {inr(o['stop'],2)}{qty}: the stop was reached first, by the bar completed "
                         f"at {o['at']} IST{tail}.")
        elif o["first"]=="target":
            tail=f", a gain of ≈ {inr(o['pnl'])} after charges" if o.get("pnl") is not None else " (+2R)"
            lines.append(f"- {o['stop_rule'].capitalize()} stop {inr(o['stop'],2)}{qty}: the 2R target {inr(o['target_2r'],2)} was reached "
                         f"first, by the bar completed at {o['at']} IST{tail}.")
        elif "r_at_end" in o:
            money=(f", {'+' if o['pnl_at_end']>=0 else '−'}{inr(abs(o['pnl_at_end']))} before charges"
                   if o.get("pnl_at_end") is not None else "")
            lines.append(f"- {o['stop_rule'].capitalize()} stop {inr(o['stop'],2)}{qty}: neither the stop nor the 2R target "
                         f"{inr(o['target_2r'],2)} was reached; {o['r_at_end']:+.2f}R at the last close {inr(o['last_close'],2)}{money}.")
    if r.get("data_ends_at"):
        lines.append(f"- The provider's {r['interval_minutes']}-minute bars for the session end at {r['data_ends_at']} IST (the later bars "
                     "are missing after the close); the review uses the bars it has.")
    return lines


def nse_tick(price):
    """NSE cash-equity tick size by price band (revised 2025): ₹0.01 below ₹250, ₹0.05 to ₹1,000, ₹0.10 to ₹5,000, ₹0.50 to
    ₹10,000, ₹1 to ₹20,000, ₹5 above. Check the instrument's tick in your broker's list."""
    for top,tick in ((250,0.01),(1000,0.05),(5000,0.10),(10000,0.50),(20000,1.00)):
        if price<top:return tick
    return 5.00


def lakh(value):
    """₹4.36 lakh / ₹1.25 crore for large rupee amounts; plain rupees below a lakh."""
    if abs(value)>=1e7:return f"₹{value/1e7:,.2f} crore"
    if abs(value)>=1e5:return f"₹{value/1e5:,.2f} lakh"
    return inr(value)


def exposure_lines(r, sizes):
    """Exposure of the largest size shown, checked against --capital when given."""
    if not sizes:return []
    big=max(sizes,key=lambda s:s["exposure"]);cap=r.get("capital");rule=big["stop_rule"]
    text=f"- Exposure up to {lakh(big['exposure'])} ({big['quantity']:,} shares, {rule} stop; est. intraday margin {inr(big['capital_needed'])})"
    if cap is None:return [text+" — pass --capital to check it fits your account."]
    if big["capital_needed"]>cap:
        return [text+f" — ⚠ the est. margin is more than your capital {inr(cap)}: take fewer shares."]
    if big["exposure"]>cap:
        return [text+f" — ⚠ {big['exposure']/cap:.1f}× your capital {inr(cap)}: it fits only with MIS intraday leverage (margin assumed 20%; your broker's figure decides)."]
    return [text+f" — within your capital {inr(cap)}."]


def tick_size_markdown(r):
    """Size table led by a close one tick past the trigger (a confirming close cannot fill exactly at it); the size at the
    trigger itself stays as a reference column."""
    t,base=r["sizing_tick"],r["sizing"];buy=r["side"]=="BUY";more="higher" if buy else "lower"
    steps=base["sizes"][0]["later_closes"] if base["sizes"] else []
    tick=r["tick"];word="above" if buy else "below"
    head=" | ".join(f"Close {inr(x['entry'],2)} ({'+' if buy else '-'}{x['move_pct']:g}%)" for x in steps)
    lines=[f"Risk {inr(t['risk_budget'])}, {t['product']} {'intraday' if t['product']=='MIS' else 'delivery'}; charges included. A confirming close "
           f"cannot be exactly at the trigger, so the size in bold is for a close one tick {word} it ({inr(t['entry_assumed'],2)}; tick "
           f"{inr(tick,2)} at this price{'' if r.get('tick_given') else ', from NSE price-band tick sizes — check your broker'}); the size at the "
           "trigger itself is for reference.","",
           f"| Stop | **Close one tick {word}, {inr(t['entry_assumed'],2)}** | At the trigger {inr(base['entry_assumed'],2)} (reference) | {head} |",
           "| --- | ---: | ---: |"+" ---: |"*len(steps)]
    for s,b in zip(t["sizes"],base["sizes"]):
        first=f"**{s['quantity']:,}** ({inr(s['risk_per_share'],2)} a share, loss {inr(s['loss_at_stop'])})" if "quantity" in s else s["blocked"]
        ref=f"{b['quantity']:,}" if "quantity" in b else "n/a"
        later=" | ".join(f"{x['quantity']:,}" if x["quantity"] else "too far" for x in b["later_closes"])
        lines.append(f"| {s['stop_rule']} {inr(s['stop'],2)} | {first} | {ref} | {later} |")
    lines.append("")
    sized=[s for s in t["sizes"] if "quantity" in s]
    for s in sized:
        worst=inr(s["worst_entry"],2) if s["worst_entry"] is not None else "n/a"
        lines.append(f"- Stop at the {s['stop_rule']}: **{s['quantity']:,}** shares (exposure {lakh(s['exposure'])}, est. intraday margin "
                     f"{inr(s['capital_needed'])}) fits the budget up to an entry of {worst}; each ₹1 {more} adds {inr(s['risk_added_per_rupee'])} "
                     f"to the loss at the stop. 2R target {inr(s['target_2r'],2)}.")
    lines+=exposure_lines(r,sized)
    lines+=["",f"⚠ Sizes assume the confirming close is one tick {word} the trigger; a close further {word} raises the risk per share — "
               f"past the worst entry shown, the same share count risks more than the budget, so size again with the actual close.",""]
    return "\n".join(lines)


def entry_markdown(r):
    """Compact answer for a public entry check: state, range, trigger, next check, stops, sizing and the ORB history."""
    rng=r.get("opening_range") or {};at=instant(r["as_of"]).strftime("%Y-%m-%d %H:%M")
    tag=" (replay)" if r.get("replay") else ""
    if r.get("session_over"):
        w=r.get("entry_window") or {"from":"09:30","to":"14:30"}
        state=(f"**ENTRY WINDOW CLOSED** — the session ended at 15:30 IST and the {w['from']}–{w['to']} IST entry window with it; "
               "nothing to act on now. "
               + (f"What would have been: TRIGGERED at {instant(r['trigger_at']).strftime('%H:%M')} — {r['reason'][len('Triggered: '):]}."
                  if r["state"]=="triggered" else f"{r['reason']}."))
        lines=[f"# {r['symbol']} {r['setup'].upper()} {r['side']} review — {at} IST, session closed{tag}","",state,""]
        review=review_lines(r);ends=[l for l in review if l.startswith("- The provider's")];review=[l for l in review if l not in ends]
        if review:lines+=["**How it played out** (completed bars to the close; research, not a forecast):",""]+review+ends+[""]
        elif ends:lines+=[ends[0][2:],""]
    else:
        lines=[f"# {r['symbol']} {r['setup'].upper()} {r['side']} check — {at} IST{tag}","",f"**{r['state'].upper()}** — {r['reason']}{window_text(r)}.",""]
    rows=["| | |","| --- | ---: |"]
    if rng:
        label="Opening range (09:15-09:30)" if rng["complete"] else f"Opening range so far ({rng['bars']} of {rng['of']} bars)"
        rows.append(f"| {label} | high {inr(rng['high'],2)} · low {inr(rng['low'],2)} · mid {inr(rng['mid'],2)} |")
    now_line=position_line(r)
    if now_line:lines[4:4]=[now_line,""]
    rows.append(f"| Bars completed today | {r['bars_completed']} ({r['interval_minutes']}-minute) |")
    if r.get("last_close"):
        rows.append(f"| Last completed close | {inr(r['last_close']['price'],2)} (bar {r['last_close']['bar']}, completed {r['last_close']['completed_at']}) |")
    if r.get("trigger_level") is not None:
        rows.append(f"| Trigger | a completed close {'above' if r['side']=='BUY' else 'below'} {inr(r['trigger_level'],2)} |")
    if r["state"]=="triggered":
        rows.append(f"| Entry (confirming close) | {inr(r['entry'],2)} at {instant(r['trigger_at']).strftime('%H:%M')} |")
    if r.get("next_check_at"):rows.append(f"| Next check | after {r['next_check_at']} IST, when the current bar completes |")
    if r.get("stops"):
        rows.append("| Stop references | "+" · ".join(f"{'range midpoint' if k=='or-mid' else 'range low' if k=='or-low' else 'range high'} {inr(v,2)}" for k,v in r["stops"].items())+" |")
    lines+=rows+[""]
    size_head="## Size (what it would have been)" if r.get("session_over") else "## Size"
    if r.get("sizing_tick"):
        lines+=[size_head,""]+tick_size_markdown(r).splitlines()+[""]
    elif r.get("sizing"):
        body=range_markdown(r["sizing"]).splitlines()
        if r["state"]=="triggered":
            # Sized at the actual confirming close, not at the trigger level: say so.
            more="above" if r["side"]=="BUY" else "below"
            body=[l.replace("| Close at the trigger ","| Confirming close ",1) if l.startswith("| Stop |") else
                  f"⚠ Sized at the confirming close {inr(r['entry'],2)}; a fill {more} it raises the risk per share — past the worst entry "
                  "shown, the same share count risks more than the budget, so size again with the actual fill." if l.startswith("⚠ ") else l
                  for l in body]
        cut=next((i for i,l in enumerate(body) if l.startswith("⚠ ")),len(body))
        if cut and body[cut-1]=="":cut-=1   # the exposure bullet joins the per-stop bullets, before the closing ⚠ note
        lines+=[size_head,""]+body[:cut]+exposure_lines(r,[s for s in r["sizing"]["sizes"] if "quantity" in s])+body[cut:]+[""]
    elif r.get("sizing_note"):
        lines+=[size_head,"",r["sizing_note"],""]
    t=r.get("orb_history")
    if t:
        lines+=["## ORB history for this symbol",""]
        if not t["sessions"]:
            lines.append("No complete earlier sessions in the chart.")
        else:
            w=t.get("entry_window") or ["09:30","14:30"]
            lines.append(f"History counts breaks confirmed by a completed {t['interval_minutes']}-minute close between {w[0]} and {w[1]} IST "
                         "(the same entry window as today's check); later breaks are not counted.")
            lines.append("")
            lines.append(f"{t['sessions']} complete earlier sessions ({t['first_session']} to {t['last_session']}, {t['interval_minutes']}-minute bars"
                         + (f"; {t['skipped_incomplete']} skipped for missing bars before 14:30" if t["skipped_incomplete"] else "")
                         + f"): a close {'above the range high' if t.get('side',r['side'])=='BUY' else 'below the range low'} in the entry window on **{t['triggers']}**. From that close, first touch of:")
            lines+=["","| Stop | 2R target first | Stop first | Neither by 15:30 (avg R at close) |","| --- | ---: | ---: | ---: |"]
            for name,b in t["stops"].items():
                rs=b["neither_r_at_close"];avg=f" ({sum(rs)/len(rs):+.2f}R)" if rs else ""
                lines.append(f"| {'range midpoint' if name=='or-mid' else 'range low' if name=='or-low' else 'range high'} | {b['target_first']} | {b['stop_first']} | {b['neither']}{avg} |")
            if t.get("sessions_with_late_gaps"):
                lines+=["",f"{t['sessions_with_late_gaps']} of {t['sessions']} sessions miss a late bar in Yahoo's data (usually 15:20); the walk to 15:30 skips it."]
            lines+=["",f"_{t['note']}_"]
        lines.append("")
    lines+=[f"_{r['warning']} Paper research only: no order is placed. Full JSON with bars: --format json --verbose, or --out FILE._",""]
    return "\n".join(lines)


def public_plan(hit, *, stop="or-mid", target="2R", step="0.01"):
    if hit.get("state")!="triggered" or hit.get("execution_eligible") is not False:raise Block("PLAN: public triggered entry required")
    symbol(hit["symbol"])
    calculated=exits(hit,positive(step),stop=stop,target=target,rows=hit["bars"])
    return {"schema":"public_plan_v1","symbol":hit["symbol"],"side":hit["side"],"as_of":hit["trigger_at"],
            "source":hit["source"],"setup":hit["setup"],"interval_minutes":hit["interval_minutes"],**calculated,
            "rounding_step":float(positive(step)),"rounding_basis":"Research precision supplied by user; not a verified exchange tick",
            "warning":WARNING,"execution_eligible":False,"submitted":False}


def public_replay(bundle, *, paths=10000, seed=19, sampling="independent", block_bars=None):
    from replay import outcome
    from replay_sampling import policy, draw_slots, limitation
    options={"replay_seed":seed,"replay_sampling":sampling}
    if block_bars is not None:options["replay_block_bars"]=block_bars
    selected=policy(options)
    p,h=bundle["plan"],bundle["history"]
    if p.get("schema")!="public_plan_v1" or h.get("schema")!="public_chart_v1" or p["symbol"]!=h["symbol"] or p.get("execution_eligible") is not False:raise Block("REPLAY: exact public plan/history required")
    interval=integer(p["interval_minutes"])
    if h["interval"]!=str(interval)+"m" or interval not in {1,5}:raise Block("REPLAY: interval mismatch")
    paths=integer(paths)
    if paths>100000:raise Block("REPLAY: maximum 100000")
    if h.get("corporate_actions",{}).get("splits"):raise Block("REPLAY: split-affected history")
    at=instant(p["as_of"]);side=p["side"]
    e,s,t=[positive(p[k]) for k in ("entry","stop","target")]
    if side not in {"BUY","SELL"} or not(s<e<t if side=="BUY" else t<e<s):raise Block("REPLAY: invalid geometry")
    start=at.replace(hour=9,minute=15,second=0,microsecond=0);end=at.replace(hour=15,minute=30,second=0,microsecond=0)
    if not start<at<end or (at-start).total_seconds()%(60*interval):raise Block("REPLAY: entry outside session/grid")
    sessions={}
    for b in h["bars"]:
        d=instant(b["at"]).date()
        if d<at.date():sessions.setdefault(d,[]).append(b)
    complete=[]
    for day,rows in sorted(sessions.items()):
        try:complete.append(prefix(rows,datetime.combine(day,end.timetz()),interval))
        except Block:continue
    if len(complete)<5:raise Block("REPLAY: need at least five complete prior sessions")
    first=int((at-start).total_seconds()//(60*interval));pool=[]
    for k in range(first,len(complete[0])):
        pool.append([tuple(float(positive(bars[k][key])/positive(bars[k-1]["close"])) for key in ("open","high","low","close")) for bars in complete])
    rng=random.Random(seed);counts={"stop":0,"target":0,"still_open":0}
    for _ in range(paths):
        price=float(e);state=None
        for ratios in draw_slots(pool,rng,selected):
            state,price=outcome(tuple(price*x for x in ratios),float(s),float(t),side=="BUY")
            if state:break
        counts[state or "still_open"]+=1
    return {"paths":paths,"seed":seed,"sampling":selected,"history_sessions":len(complete),"dropped_sessions":len(sessions)-len(complete),"outcomes":counts,
            "plan_hash":fingerprint(p),"history_hash":fingerprint(h),"execution_eligible":False,"submitted":False,
            "limitations":["Public unadjusted exact-symbol history; no survivorship correction.",limitation(selected),"Conditioned on entry; not calibrated probabilities; repeated draws are not additional historical evidence.","No fees, liquidity, queue or slippage model; not a broker risk replay."]}


def collect_daily(selected, *, fetcher=request, clock=instant):
    indices={};errors={};session=None;last_seen=None
    for key in selected:
        try:
            h=collect_chart(INDEX_SYMBOLS[key],fetcher=fetcher,clock=clock)
            now=clock();period=h["regular_session"]
            done=[instant(r["at"]).date() for r in h["bars"] if instant(r["at"]).date()<=now.date()]
            if done:last_seen=max(last_seen or done[-1],done[-1])
            start=datetime.fromtimestamp(integer(period["start"]),IST);end=datetime.fromtimestamp(integer(period["end"]),IST)
            if start.date()!=now.date() or end.date()!=now.date() or start>=end:raise Block("CALENDAR: no provider session today")
            if now.weekday()>=5:raise Block("CALENDAR: special sessions need explicit calendar evidence")
            previous=[r for r in h["bars"] if instant(r["at"]).date()<now.date()]
            if not previous or (now.date()-instant(previous[-1]["at"]).date()).days>7:raise Block("DAILY: previous observation too old")
            prev=previous[-1];day=instant(prev["at"]).date().isoformat()
            cal={"source":"yahoo_provider_session_and_observed_history_not_exchange_verified","as_of":now.isoformat(),"date":now.date().isoformat(),
                 "previous_session":day,"is_open_day":True,"open":start.strftime("%H:%M"),"close":end.strftime("%H:%M"),"exchanges":["NSE","BSE"]}
            if session and any(cal[f]!=session[f] for f in ("date","previous_session","open","close")):raise Block("CALENDAR: conflicting index sessions")
            session=cal
            item={"index_id":key,"price_basis":"spot_index_points","source":"yahoo_public_chart","as_of":now.isoformat(),
                  "previous":{**{f:prev[f] for f in ("open","high","low","close")},"source":"yahoo_public_day","as_of":now.isoformat(),"session":day},
                  "history":{"source":"yahoo_public_day","as_of":now.isoformat(),"rows":[{"session":instant(b["at"]).date().isoformat(),"close":b["close"],"available_at":now.isoformat()} for b in previous]}}
            today=[r for r in h["bars"] if instant(r["at"]).date()==now.date()]
            if now>=end and today:
                # Yahoo's daily bar for today carries the official closing value once the session has ended.
                item["official_close"]={"value":today[-1]["close"],"session":now.date().isoformat(),"source":"yahoo_public_day","as_of":now.isoformat()}
            if now>=start:
                intraday=collect_chart(INDEX_SYMBOLS[key],"5m",fetcher=fetcher,clock=clock)
                item["candles"]={"source":"yahoo_public_5m","as_of":intraday["as_of"],"interval":"5minute","rows":[b for b in intraday["bars"] if start<=instant(b["at"])<end]}
            item["as_of"]=clock().isoformat();indices[key]=item
        except (Block,KeyError,TypeError,ValueError,OSError,OverflowError,http.client.HTTPException):errors[key]="Public index/session data unavailable or inconsistent; no synthetic substitute"
    if session is None:
        if last_seen:
            raise Block(f"PUBLIC: no trading session today (weekend, holiday or before the provider's calendar updates). The last session was {last_seen.isoformat()}; "
                        f"for its closing report run daily.py with --collect-close --session {last_seen.isoformat()}")
        raise Block("PUBLIC: no current provider session; use sourced input for closed/special sessions")
    return {"session":session,"indices":indices,"context_errors":{"public_feed":WARNING,"calendar":"Session times and previous date are provider observations, not independently verified exchange-calendar clearance",**errors},"submitted":False}


def main(argv=None):
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument("command",choices=["universe","chart","scan","entry","watch","plan","replay"])
    p.add_argument("--symbol");p.add_argument("--symbols",nargs="+");p.add_argument("--input");p.add_argument("--out");p.add_argument("--now")
    p.add_argument("--interval",choices=["1d","5m","1m"],default="5m");p.add_argument("--setup",choices=["orb","level"],default="orb")
    p.add_argument("--level");p.add_argument("--side",choices=["BUY","SELL"],default="BUY");p.add_argument("--stop",default="or-mid");p.add_argument("--target",default="2R");p.add_argument("--step",default="0.01")
    p.add_argument("--cycles",type=int,default=1);p.add_argument("--every",type=int,default=30);p.add_argument("--paths",type=int,default=10000);p.add_argument("--seed",type=int,default=19)
    p.add_argument("--sampling",choices=("independent","block","session"),default="independent");p.add_argument("--block-bars",type=int)
    p.add_argument("--risk",type=float,help="entry/watch: rupees at risk; sizes the trade from the opening range with Indian intraday charges")
    p.add_argument("--product",choices=["MIS","CNC"],default="MIS",help="entry/watch sizing: MIS intraday (default) or CNC delivery")
    p.add_argument("--capital",type=float,help="entry/watch sizing: your trading capital in rupees; the exposure and margin are checked against it")
    p.add_argument("--tick",type=float,help="entry/watch sizing: the stock's tick size (default: NSE's price-band tick sizes)")
    p.add_argument("--format",choices=["markdown","json"],help="entry/watch: markdown summary (default) or JSON; other commands print JSON")
    p.add_argument("--verbose",action="store_true",help="entry/watch JSON: include every bar")
    a=p.parse_args(argv)
    try:
        if a.out and Path(a.out).exists():raise Block("OUTPUT: choose a new path")
        replay=bool(a.now and not a.input and a.command=="entry")
        if replay and not instant(a.now).date()<instant().date():
            raise Block("CLOCK: --now without --input replays an earlier date only (a live check uses the actual clock)")
        if a.now and not replay and not (a.input and a.command in {"entry","plan","replay"}):raise Block("CLOCK: network reads require actual clock")
        if a.risk is not None and (a.command not in {"entry","watch"} or not a.risk>0):raise Block("RISK: --risk is a positive rupee amount for entry/watch")
        for name in ("capital","tick"):
            value=getattr(a,name)
            if value is not None and (a.risk is None or not value>0):raise Block(f"RISK: --{name} is a positive number used with --risk")
        summary=None
        if a.command=="universe":
            body=request(UNIVERSE_URL);result=universe(body,instant())
        elif a.command=="scan": result=scan(a.symbols or ([r["symbol"] for r in read(a.input)["members"]] if a.input else []))
        elif a.command=="chart":result=collect_chart(a.symbol,a.interval)
        elif a.command=="plan":result=public_plan(read(a.input),stop=a.stop,target=a.target,step=a.step)
        elif a.command=="replay":result=public_replay(read(a.input),paths=a.paths,seed=a.seed,sampling=a.sampling,block_bars=a.block_bars)
        else:
            if a.interval=="1d" or not 1<=a.cycles<=120 or not 15<=a.every<=300 or a.command=="watch" and a.input:raise Block("WATCH: bounded live intraday reads required")
            outputs=[]
            for n in range(a.cycles if a.command=="watch" else 1):
                if n:time.sleep(a.every)
                h=read(a.input) if a.input else collect_chart(a.symbol,a.interval)
                if replay:h=replay_history(h,instant(a.now))
                result=public_entry(h,instant(a.now),setup=a.setup,level=a.level,side=a.side);outputs.append({k:v for k,v in result.items() if k!="bars"})
                result["replay"]=replay
                if result["state"]=="triggered":break
            if a.command=="watch":result["checks"]=outputs
            if a.setup=="orb":
                try:result["orb_history"]=orb_tally(result["bars"],instant(result["as_of"]),side=a.side,interval=result["interval_minutes"])
                except (Block,KeyError,TypeError,ValueError):result["orb_history"]=None
            if a.risk is not None:
                rng=result.get("opening_range") or {}
                if rng.get("complete"):
                    triggered=result["state"]=="triggered"
                    result["sizing"]=range_sizes(range_high=rng["high"],range_low=rng["low"],risk=a.risk,side=a.side,product=a.product,
                                                 entry=result["entry"] if triggered else None)
                    result["capital"]=a.capital
                    if not triggered and a.setup=="orb":
                        # A confirming close is at least one tick past the trigger: that size leads, the at-trigger one is a reference.
                        level=rng["high"] if a.side=="BUY" else rng["low"]
                        tick=a.tick or nse_tick(level);result["tick"]=tick;result["tick_given"]=a.tick is not None
                        result["sizing_tick"]=range_sizes(range_high=rng["high"],range_low=rng["low"],risk=a.risk,side=a.side,product=a.product,
                                                          entry=round(level+tick if a.side=="BUY" else level-tick,2))
                else:result["sizing_note"]="Sizing needs the completed opening range (after 09:30 IST); run again then."
            summary=entry_markdown(result)
            if a.format=="json" and not a.verbose and not a.out:result={k:v for k,v in result.items() if k!="bars"}
            if a.format!="json":
                if a.out:
                    path=Path(a.out);path.parent.mkdir(parents=True,exist_ok=True)
                    with path.open("x",encoding="utf-8") as stream:stream.write(json.dumps(result,indent=2,allow_nan=False))
                    summary+=f"\nFull JSON (with bars, for plan/replay) written to {a.out}\n"
                if hasattr(sys.stdout,"reconfigure"):sys.stdout.reconfigure(encoding="utf-8")
                print(summary);return 0
        text=json.dumps(result,indent=2,allow_nan=False)
        if a.out:
            path=Path(a.out);path.parent.mkdir(parents=True,exist_ok=True)
            with path.open("x",encoding="utf-8") as stream:stream.write(text)
        else:print(text)
        return 2 if result.get("processed")==0 else 0
    except Block as error:
        # The specific reason (e.g. which bars are missing), so the answer can say why instead of an opaque block.
        print(json.dumps({"status":"BLOCKED","reason":f"{error}; no clearance or substitute data inferred","submitted":False},ensure_ascii=False));return 2
    except (KeyError,TypeError,ValueError,OSError,OverflowError):
        print(json.dumps({"status":"BLOCKED","reason":"Public source or input unavailable/invalid; no clearance or substitute data inferred","submitted":False}));return 2


if __name__=="__main__":sys.exit(main())
