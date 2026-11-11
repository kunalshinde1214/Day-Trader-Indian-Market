"""Credential-free Indian public market research. Never execution evidence."""
import argparse
import csv
from datetime import datetime, timedelta
import hashlib
import io
import json
from pathlib import Path
import random
import re
import sys
import time
import urllib.parse
import urllib.request

from core import Block, IST, instant, integer, positive, number, fingerprint
from analysis_rules import screen, entry, exits, prefix
from trader import read

INDEX_SYMBOLS = {"nifty": "^NSEI", "banknifty": "^NSEBANK", "sensex": "^BSESN"}
UNIVERSE_URL = "https://nsearchives.nseindia.com/content/equities/EQUITY_L.csv"
LIMIT = 12*1024*1024
WARNING = "Public/unofficial feed; may be delayed, revised or unavailable. Research only; not broker-grade execution evidence."


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs): raise Block("PUBLIC: redirect refused")


def request(url):
    parsed = urllib.parse.urlsplit(url)
    if parsed.scheme != "https" or parsed.hostname not in {"query1.finance.yahoo.com", "feeds.finance.yahoo.com", "nsearchives.nseindia.com"} or parsed.username or parsed.password or parsed.port not in {None,443}:
        raise Block("PUBLIC: unsupported source")
    req = urllib.request.Request(url, headers={"User-Agent":"IndiaDayTrader/0.9 (public research)", "Accept":"application/json,text/csv,application/xml,text/xml"})
    try:
        with urllib.request.build_opener(NoRedirect()).open(req, timeout=20) as response: raw=response.read(LIMIT+1)
        if len(raw)>LIMIT: raise Block("PUBLIC: oversized response")
        return raw
    except (OSError, ValueError): raise Block("PUBLIC: source unavailable; no login, bypass or automatic retry") from None


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
    rows=[];missing=[];seen=set()
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
            "last_bar_at":last,"last_bar_age_seconds":(now-instant(last)).total_seconds(),"bars":rows,"missing_bars":missing,
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


def public_entry(history, now, **options):
    if history.get("schema")!="public_chart_v1" or history.get("execution_eligible") is not False:raise Block("ENTRY: public chart contract required")
    symbol(history["symbol"])
    if history["symbol"].startswith("^"):raise Block("ENTRY: equity observations required")
    interval={"1m":1,"5m":5}.get(history["interval"])
    if interval is None:raise Block("ENTRY: intraday chart required")
    if instant(history["as_of"])>now or (now-instant(history["as_of"])).total_seconds()>120:raise Block("ENTRY: stale collection")
    result=entry(history["bars"],now,interval=interval,**options)
    return {**result,"symbol":history["symbol"],"source":history["source"],"as_of":now.isoformat(),"chart_hash":fingerprint(history),
            "bars":history["bars"],"warning":WARNING,"execution_eligible":False,"submitted":False}


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
    indices={};errors={};session=None
    for key in selected:
        try:
            h=collect_chart(INDEX_SYMBOLS[key],fetcher=fetcher,clock=clock)
            now=clock();period=h["regular_session"]
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
            if now>=start:
                intraday=collect_chart(INDEX_SYMBOLS[key],"5m",fetcher=fetcher,clock=clock)
                item["candles"]={"source":"yahoo_public_5m","as_of":intraday["as_of"],"interval":"5minute","rows":[b for b in intraday["bars"] if start<=instant(b["at"])<end]}
            item["as_of"]=clock().isoformat();indices[key]=item
        except (Block,KeyError,TypeError,ValueError,OSError,OverflowError):errors[key]="Public index/session data unavailable or inconsistent; no synthetic substitute"
    if session is None:raise Block("PUBLIC: no current provider session; use sourced input for closed/special sessions")
    return {"session":session,"indices":indices,"context_errors":{"public_feed":WARNING,"calendar":"Session times and previous date are provider observations, not independently verified exchange-calendar clearance",**errors},"submitted":False}


def main(argv=None):
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument("command",choices=["universe","chart","scan","entry","watch","plan","replay"])
    p.add_argument("--symbol");p.add_argument("--symbols",nargs="+");p.add_argument("--input");p.add_argument("--out");p.add_argument("--now")
    p.add_argument("--interval",choices=["1d","5m","1m"],default="5m");p.add_argument("--setup",choices=["orb","level"],default="orb")
    p.add_argument("--level");p.add_argument("--side",choices=["BUY","SELL"],default="BUY");p.add_argument("--stop",default="or-mid");p.add_argument("--target",default="2R");p.add_argument("--step",default="0.01")
    p.add_argument("--cycles",type=int,default=1);p.add_argument("--every",type=int,default=30);p.add_argument("--paths",type=int,default=10000);p.add_argument("--seed",type=int,default=19)
    p.add_argument("--sampling",choices=("independent","block","session"),default="independent");p.add_argument("--block-bars",type=int)
    a=p.parse_args(argv)
    try:
        if a.out and Path(a.out).exists():raise Block("OUTPUT: choose a new path")
        if a.now and not (a.input and a.command in {"entry","plan","replay"}):raise Block("CLOCK: network reads require actual clock")
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
                result=public_entry(h,instant(a.now),setup=a.setup,level=a.level,side=a.side);outputs.append({k:v for k,v in result.items() if k!="bars"})
                if result["state"]=="triggered":break
            if a.command=="watch":result["checks"]=outputs
        text=json.dumps(result,indent=2,allow_nan=False)
        if a.out:
            path=Path(a.out);path.parent.mkdir(parents=True,exist_ok=True)
            with path.open("x",encoding="utf-8") as stream:stream.write(text)
        else:print(text)
        return 2 if result.get("processed")==0 else 0
    except (Block,KeyError,TypeError,ValueError,OSError,OverflowError):
        print(json.dumps({"status":"BLOCKED","reason":"Public source or input unavailable/invalid; no clearance or substitute data inferred","submitted":False}));return 2


if __name__=="__main__":sys.exit(main())
