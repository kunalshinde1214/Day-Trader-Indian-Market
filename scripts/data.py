"""Public feed collection and explicit normalized imports. Never infers missing clearance."""
import argparse
import csv
from datetime import datetime, time
from email.utils import parsedate_to_datetime
import hashlib
import io
import json
from pathlib import Path
import sys
import urllib.request
from urllib.parse import urlsplit
import xml.etree.ElementTree as ET

from core import Block, IST, instant, fresh, fingerprint, number, positive, validate_instrument
from patterns import daily_reference
from trader import read,write

SOURCES={
    "nifty50":"https://www.niftyindices.com/IndexConstituent/ind_nifty50list.csv",
    "niftybank":"https://www.niftyindices.com/IndexConstituent/ind_niftybanklist.csv",
    "nifty500":"https://nsearchives.nseindia.com/content/indices/ind_nifty500list.csv",
    "announcements":"https://nsearchives.nseindia.com/content/RSS/Online_announcements.xml",
    "corporate_actions":"https://nsearchives.nseindia.com/content/RSS/Corporate_action.xml",
}
MEMBERSHIP_NAMES = {"nifty50": "Nifty50", "niftybank": "BankNifty", "nifty500": "Nifty500"}
MAX_BYTES=8*1024*1024


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self,*args,**kwargs): raise Block("FEED: redirects refused; verify official source")


def fetch(name):
    if name not in SOURCES: raise Block("FEED: unsupported named source")
    req=urllib.request.Request(SOURCES[name],headers={"User-Agent":"IndiaDayTrader/0.3 (read-only)","Accept":"text/csv,application/xml,text/xml"})
    try:
        with urllib.request.build_opener(NoRedirect()).open(req,timeout=20) as response:
            body=response.read(MAX_BYTES+1)
        if len(body)>MAX_BYTES: raise Block("FEED: response too large")
        return body
    except (OSError,ValueError):
        raise Block("FEED: request failed; no freshness or coverage asserted") from None


def link(value):
    parsed=urlsplit(value)
    if parsed.scheme!="https" or not parsed.hostname or parsed.username or parsed.password:
        raise Block("SOURCE: public HTTPS source link required")
    return value


def rss(body,source,retrieved):
    if len(body)>MAX_BYTES or b"<!DOCTYPE" in body.upper() or b"<!ENTITY" in body.upper():
        raise Block("FEED: oversized XML or document entities refused")
    root=ET.fromstring(body)
    records=[];unknown=[];seen=set()
    for item in root.findall("./channel/item"):
        title=(item.findtext("title") or "").strip()
        url=(item.findtext("link") or "").strip()
        rawdate=(item.findtext("pubDate") or "").strip()
        try:
            published=parsedate_to_datetime(rawdate)
            if published.tzinfo is None or published>retrieved: raise ValueError("time")
            if not title: raise ValueError("title")
            link(url)
        except (ValueError,TypeError,Block):
            unknown.append({"headline":title,"reason":"missing/invalid public link or release time"});continue
        record={"headline":title,"url":url,"published_at":published.astimezone(IST).isoformat(),
                "description":item.findtext("description") or "","source":source,
                "symbol":None,"category":"exchange_announcement"}
        key=fingerprint(record)
        if key not in seen: records.append({**record,"id":key,"retrieved_at":retrieved.isoformat()});seen.add(key)
    if root.tag!="rss": raise Block("FEED: expected RSS channel")
    return {"source":source,"as_of":retrieved.isoformat(),"records":records,"unparsed":unknown,
            "coverage":"feed_items_only_not_complete_market_or_symbol_coverage","complete":False}


def members_csv(body,source,now):
    rows=list(csv.DictReader(io.StringIO(body.decode("utf-8-sig"))))
    if not rows or not {"Symbol","Series","ISIN Code"}.issubset(rows[0]): raise Block("UNIVERSE: unexpected constituent CSV")
    members=[];seen=set()
    for row in rows:
        symbol=row["Symbol"].strip();isin=row["ISIN Code"].strip()
        if not symbol or not isin or symbol in seen or row["Series"].strip()!="EQ": raise Block("UNIVERSE: incomplete/duplicate/non-EQ member")
        members.append({"exchange":"NSE","symbol":symbol,"isin":isin});seen.add(symbol)
    scope = next((label + " source membership" for name, label in MEMBERSHIP_NAMES.items() if SOURCES[name] == source), "supplied source membership")
    return {"source":source,"as_of":now.isoformat(),"members":members,"scope":scope,
            "historical_membership":False,"membership_effective_date":None}


def build_universe(membership,master,turnover,now):
    for data in (membership,master,turnover):
        if not data.get("source"): raise Block("UNIVERSE: every input requires source")
        fresh(data.get("as_of"),now,7*86400,"universe input")
    index={}
    for i in master["instruments"]:
        key=i["exchange"]+":"+i["symbol"]
        if key in index: raise Block("UNIVERSE: ambiguous instrument master")
        index[key]=i
    selected=[];excluded=[];seen=set()
    for member in membership["members"]:
        key=member["exchange"]+":"+member["symbol"]
        if key in seen: continue
        seen.add(key)
        try:
            i=index[key];validate_instrument(i,now)
            evidence=turnover["instruments"][key]
            days=evidence["sessions"]
            rows=evidence["daily"]
            if len(days)!=20 or days!=sorted(set(days)) or days!=[r["date"] for r in rows] or not evidence.get("calendar_source"):
                raise Block("TURNOVER: exactly 20 explicit ordered sessions and calendar source required")
            if days[-1]!=evidence.get("previous_session"): raise Block("TURNOVER: prior-session coverage mismatch")
            for row in rows:
                day=datetime.fromisoformat(row["date"]).date()
                if not datetime.combine(day,time(15,30),IST)<=instant(row["available_at"])<now or day>=now.date():
                    raise Block("TURNOVER: daily value not available before scan")
            average=sum((number(r["turnover_inr"]) for r in rows),number(0))/20
            selected.append({**i,"average_daily_turnover_inr":float(average),"turnover_evidence_hash":fingerprint(evidence)})
        except (Block,KeyError,TypeError,ValueError):
            excluded.append({"instrument":key,"reason":"missing/invalid exact master or 20-session turnover evidence"})
    return {"source":"normalized_membership+master+turnover","as_of":now.isoformat(),"scope":membership.get("scope","explicit membership"),
            "instruments":selected,"requested":len(seen),"processed":len(selected),"excluded":excluded,
            "input_hashes":[fingerprint(x) for x in (membership,master,turnover)]}


def references(daily,calendar,instrument,session):
    """Create target-session references only from point-in-time daily observations."""
    if not daily.get("source") or not calendar.get("source") or calendar.get("complete") is not True:
        raise Block("REFERENCE: sourced daily history and complete calendar required")
    if any(daily.get(k)!=instrument.get(k) for k in ("broker","exchange","symbol","token")):
        raise Block("REFERENCE: exact instrument required")
    dates=calendar["sessions"]
    if dates!=sorted(set(dates)) or session not in dates: raise Block("REFERENCE: ordered calendar including target required")
    previous=dates[:dates.index(session)]
    if len(previous)<30: raise Block("REFERENCE: 30 prior sessions required")
    previous=previous[-30:]
    rows={}
    for b in daily["bars"]:
        if b["date"] in rows: raise Block("REFERENCE: duplicate daily observation")
        rows[b["date"]]=b
    ref={k:instrument[k] for k in ("broker","exchange","symbol","token")}
    ref.update(for_session=session,previous_session=previous[-1],sessions=previous,source=daily["source"],calendar_source=calendar["source"],
               price_basis=daily.get("price_basis"),corporate_actions_clear=daily.get("corporate_actions_clear"),bars=[rows[d] for d in previous])
    daily_reference(ref,instrument,session)
    gap={k:v for k,v in ref.items() if k not in {"bars","sessions"}}
    gap.update(price_basis="unadjusted_session_close",close=ref["bars"][-1]["close"],available_at=ref["bars"][-1]["available_at"])
    return {"daily_reference":ref,"prior_close":gap}


def event_context(evidence,now):
    fresh(evidence.get("as_of"),now,60,"event review")
    day=now.date().isoformat()
    if evidence.get("date")!=day or not evidence.get("source") or evidence.get("coverage_complete") is not True:
        raise Block("EVENT: complete dated review required; empty feed is not clearance")
    current=[]
    for e in evidence["events"]:
        link(e["url"])
        if e.get("impact") not in {"major","minor","unknown"} or not isinstance(e.get("resolved"),bool):
            raise Block("EVENT: explicit impact and resolution status required")
        datetime.fromisoformat(e["date"])
        if not e.get("reviewed_at") or instant(e["reviewed_at"])>now: raise Block("EVENT: invalid review time")
        if e["date"]==day: current.append(e)
    unresolved=any(e.get("resolved") is not True and e.get("impact") in {"major","unknown"} for e in current)
    multiplier=0 if unresolved else 1
    override=evidence.get("override")
    if unresolved and override:
        if override.get("confirmed") is not True or not override.get("user_instruction") or not override.get("at") or instant(override["at"])>now:
            raise Block("EVENT: explicit dated user instruction required")
        multiplier=0.5
    return {"source":evidence["source"],"as_of":evidence["as_of"],"date":day,"risk_multiplier":multiplier,
            "events":current,"override_confirmed":bool(override) if unresolved else False,"evidence_hash":fingerprint(evidence)}


def analyst(data):
    a,b=data["current"],data["previous"]
    for row in (a,b):
        link(row["url"])
        for k in ("period","basis","currency","unit","duration_months","published_at"):
            if not row.get(k): raise Block("ANALYST: reporting metadata required")
        instant(row["published_at"])
    if any(a[k]!=b[k] for k in ("basis","currency","unit","duration_months")):
        raise Block("ANALYST: incomparable reporting basis, units or duration")
    differences={}
    for key in set(a["metrics"])|set(b["metrics"]):
        if key not in a["metrics"] or key not in b["metrics"]:
            differences[key]={"status":"missing_comparison"};continue
        # Metrics may be negative (earnings/debt changes); never coerce to positives.
        values=[number(row["metrics"][key],"reported metric",minimum="-1e30") for row in (a,b)]
        x,y=values
        differences[key]={"current":float(x),"previous":float(y),"change":float(x-y),"change_pct":float((x-y)/y*100) if y>0 else None}
    return {"current_period":a["period"],"previous_period":b["period"],"basis":a["basis"],"unit":a["unit"],"currency":a["currency"],
            "sources":[a["url"],b["url"]],"metrics":differences,"label":"reported facts; no security rating"}


def disclosures(data,now):
    records=[];seen=set()
    for row in data["records"]:
        for k in ("holder","symbol","exchange","category","transaction_date","published_at","url","amendment"):
            if k not in row: raise Block("DISCLOSURE: missing filed fact")
        if row["category"] not in {"PIT","SAST","pledge","bulk","block"}: raise Block("DISCLOSURE: unsupported category")
        datetime.fromisoformat(row["transaction_date"])
        if not isinstance(row["amendment"],bool):raise Block("DISCLOSURE: explicit amendment status required")
        link(row["url"])
        if instant(row["published_at"])>now or row["transaction_date"]>instant(row["published_at"]).date().isoformat(): raise Block("DISCLOSURE: transaction/release lookahead")
        number(row["quantity"])
        key=fingerprint(row)
        if key not in seen: records.append({**row,"id":key});seen.add(key)
    return {"as_of":now.isoformat(),"records":records,"coverage":data.get("coverage","supplied filings only"),"label":"public disclosures; motives unknown"}


def main(argv=None):
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument("command",choices=["collect","universe","references","events","analyst","whales"])
    p.add_argument("--source",choices=sorted(SOURCES));p.add_argument("--input");p.add_argument("--master");p.add_argument("--turnover")
    p.add_argument("--calendar");p.add_argument("--instrument");p.add_argument("--session");p.add_argument("--now");p.add_argument("--out",required=True)
    a=p.parse_args(argv)
    try:
        out=Path(a.out)
        if out.exists(): raise Block("OUTPUT: choose a new evidence filename")
        now=instant(a.now)
        if a.command=="collect":
            if a.now: raise Block("CLOCK: network collection uses actual retrieval time")
            body=fetch(a.source)
            now=instant()
            result=members_csv(body,SOURCES[a.source],now) if a.source in MEMBERSHIP_NAMES else rss(body,SOURCES[a.source],now)
            result["raw_sha256"]=hashlib.sha256(body).hexdigest()
        elif a.command=="universe": result=build_universe(read(a.input),read(a.master),read(a.turnover),now)
        elif a.command=="references": result=references(read(a.input),read(a.calendar),read(a.instrument),a.session)
        elif a.command=="events": result=event_context(read(a.input),now)
        elif a.command=="analyst": result=analyst(read(a.input))
        else: result=disclosures(read(a.input),now)
        # Exclusive creation preserves earlier evidence even if concurrent collectors race.
        encoded=json.dumps(result,indent=2,allow_nan=False);out.parent.mkdir(parents=True,exist_ok=True)
        with out.open("x",encoding="utf-8") as stream:stream.write(encoded)
        print(json.dumps({"status":"saved","path":str(out),"submitted":False}));return 0
    except (Block,ValueError,KeyError,TypeError,OSError,ET.ParseError):
        print(json.dumps({"status":"BLOCK","reason":"Source/input unavailable or invalid; no clearance inferred"}));return 2


if __name__=="__main__":sys.exit(main())
