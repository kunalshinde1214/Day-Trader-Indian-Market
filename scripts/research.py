"""Public Indian news/disclosure discovery and sourced XBRL extraction."""
import argparse
from datetime import date, timedelta
import hashlib
import json
from pathlib import Path
import re
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET

from core import Block, instant, number, fingerprint, integer
from data import rss, SOURCES, link, disclosures as normalize_disclosures
from public_market import request, symbol, NoRedirect, LIMIT
from trader import read

CATEGORIES={"earnings":("financial result","earnings","quarter","profit"),"deal":("acquisition","merger","agreement"),
            "PIT":("insider","pit disclosure","regulation 7(2)"),"SAST":("sast","takeover","regulation 29"),
            "pledge":("pledge","encumbrance"),"bulk":("bulk deal",),"block":("block deal",),
            "capital":("dividend","bonus","split","buyback"),"regulatory":("sebi","penalty","suspension")}
ATTACHMENT_HOSTS={"nsearchives.nseindia.com","archives.nseindia.com","www.nseindia.com","www.bseindia.com","www.bseindia.com"}
METRICS={"revenue":{"RevenueFromOperations","RevenueFromOperationsNet","Revenue"},
         "net_profit":{"ProfitLossForPeriod","ProfitLoss","ProfitLossForThePeriod"},
         "diluted_eps":{"DilutedEarningsLossPerShare","DilutedEarningsLossPerShareFromContinuingOperations","DilutedEarningsPerShare"},
         "assets":{"Assets","TotalAssets"},"cash":{"CashAndCashEquivalents","CashAndCashEquivalentsAtCarryingValue"}}


def news(symbols, now, *, fetcher=request, hours=24, disclosure_only=False):
    if not isinstance(symbols,list) or not 1<=len(symbols)<=50 or len(set(symbols))!=len(symbols):raise Block("NEWS: 1..50 unique equity symbols")
    hours=integer(hours)
    if hours>720:raise Block("NEWS: maximum 720 hours")
    feeds=[];records=[];failures=[];unparsed=0
    for s in symbols:
        symbol(s)
        if s.startswith("^"):raise Block("NEWS: equity watchlist required")
        if not disclosure_only:
            url="https://feeds.finance.yahoo.com/rss/2.0/headline?"+urllib.parse.urlencode({"s":s,"region":"IN","lang":"en-IN"})
            feeds.append((url,s))
    feeds += [(SOURCES["announcements"],None),(SOURCES["corporate_actions"],None)]
    for url,selected in feeds:
        try:
            feed=rss(fetcher(url),url,now);unparsed+=len(feed["unparsed"])
            for r in feed["records"]:
                if instant(r["published_at"])<now-timedelta(hours=hours):continue
                title=r["headline"];categories=[c for c,words in CATEGORIES.items() if any(w in title.lower() for w in words)]
                matches=[selected] if selected else [s for s in symbols if re.search(r"(?<![A-Z0-9])"+re.escape(s.rsplit(".",1)[0])+r"(?![A-Z0-9])",title.upper())]
                if disclosure_only and not set(categories)&{"PIT","SAST","pledge","bulk","block"}:continue
                row={"headline":title,"url":r["url"],"published_at":r["published_at"],"source":url,"symbols":matches,
                     "symbol_match_basis":"requested ticker feed" if selected else "literal headline symbol match; unmatched items retained",
                     "categories":categories or ["other"],"classification_basis":"headline keywords, not verified filing facts",
                     "transaction_date":None,"quantity":None,"review_required":True}
                row["id"]=fingerprint({k:row[k] for k in ("headline","url","published_at")})
                records.append(row)
        except (Block,ValueError,TypeError,KeyError,OSError,ET.ParseError):failures.append({"source":url,"reason":"Unavailable or invalid public feed"})
    unique={}
    for row in records:
        if row["id"] in unique:
            old=unique[row["id"]]
            old["symbols"]=sorted(set(old["symbols"]+row["symbols"]))
            old["sources"]=sorted(set(old["sources"]+[row["source"]]))
        else:unique[row["id"]]={**row,"sources":[row["source"]]}
    return {"schema":"public_research_v1","as_of":now.isoformat(),"records":sorted(unique.values(),key=lambda r:r["published_at"],reverse=True),
            "failed_sources":failures,"unparsed_items":unparsed,"coverage":"partial public feeds; not complete exchange or company coverage",
            "no_event_clearance":True,"submitted":False,"disclosure_facts_parsed":False}


def unseen(report, state, *, all_items=False):
    path=Path(state);path.parent.mkdir(parents=True,exist_ok=True)
    db=sqlite3.connect(path,timeout=10)
    try:
        db.execute("CREATE TABLE IF NOT EXISTS research_seen (id TEXT PRIMARY KEY, published_at TEXT, body TEXT)")
        db.execute("BEGIN IMMEDIATE")
        found=[]
        for r in report["records"]:
            exists=db.execute("SELECT 1 FROM research_seen WHERE id=?",(r["id"],)).fetchone()
            if not exists or all_items:found.append(r)
            db.execute("INSERT OR IGNORE INTO research_seen VALUES (?,?,?)",(r["id"],r["published_at"],json.dumps(r,allow_nan=False)))
        db.commit()
        return {**report,"records":found,"seen_filtered":len(report["records"])-len(found),"persistent_deduplication":True}
    except BaseException:db.rollback();raise
    finally:db.close()


def attachment(url):
    parsed=urllib.parse.urlsplit(link(url))
    if parsed.hostname not in ATTACHMENT_HOSTS or parsed.port not in {None,443} or not parsed.path:
        raise Block("ATTACHMENT: explicit NSE/BSE HTTPS attachment required; use local input for other issuers")
    req=urllib.request.Request(url,headers={"User-Agent":"IndiaDayTrader/0.9 (public research)"})
    try:
        with urllib.request.build_opener(NoRedirect()).open(req,timeout=20) as response:body=response.read(LIMIT+1)
        if not body or len(body)>LIMIT:raise Block("ATTACHMENT: invalid size")
        return body
    except (OSError,ValueError):raise Block("ATTACHMENT: unavailable; no bypass or retry") from None


def local(tag):return tag.rsplit("}",1)[-1].rsplit(":",1)[-1]


def xbrl(body, source, now):
    link(source)
    if not isinstance(body,bytes) or len(body)>LIMIT or b"<!DOCTYPE" in body.upper() or b"<!ENTITY" in body.upper():raise Block("XBRL: oversized/entity-bearing XML refused")
    root=ET.fromstring(body)
    if local(root.tag) not in {"xbrl","html"}:raise Block("XBRL: expected XBRL instance or inline XBRL XHTML")
    contexts={};units={};facts={};unsupported=0
    for element in root.iter():
        name=local(element.tag)
        if name=="context":
            cid=element.attrib["id"]
            if cid in contexts:raise Block("XBRL: duplicate context")
            fields={local(e.tag):(e.text or "").strip() for e in element.iter() if local(e.tag) in {"identifier","startDate","endDate","instant"}}
            if not fields.get("identifier"):raise Block("XBRL: entity missing")
            dims=sorted((e.attrib.get("dimension",""),(e.text or "").strip()) for e in element.iter() if local(e.tag)=="explicitMember")
            if any(local(e.tag)=="typedMember" for e in element.iter()):fields["unsupported_typed_dimensions"]=True
            if "instant" in fields:
                date.fromisoformat(fields["instant"])
            elif "startDate" in fields and "endDate" in fields:
                if date.fromisoformat(fields["startDate"])>date.fromisoformat(fields["endDate"]):raise Block("XBRL: reversed period")
            else:raise Block("XBRL: missing period")
            if date.fromisoformat(fields.get("endDate",fields.get("instant")))>now.date():raise Block("XBRL: future reporting period")
            contexts[cid]={**fields,"dimensions":dims}
        elif name=="unit":
            uid=element.attrib["id"]
            if uid in units:raise Block("XBRL: duplicate unit")
            numerator=[];denominator=[]
            for e in element.iter():
                if local(e.tag)=="unitDenominator":denominator=[local(m.text or "") for m in e.iter() if local(m.tag)=="measure"]
                if local(e.tag)=="unitNumerator":numerator=[local(m.text or "") for m in e.iter() if local(m.tag)=="measure"]
            if not numerator:numerator=[local(e.text or "") for e in element.iter() if local(e.tag)=="measure"]
            units[uid]="*".join(numerator)+("/"+"*".join(denominator) if denominator else "")
    for e in root.iter():
        tag=local(e.attrib.get("name",e.tag));metric=next((k for k,tags in METRICS.items() if tag in tags),None)
        if not metric:continue
        if any(local(k)=="nil" and v in {"true","1"} for k,v in e.attrib.items()):continue
        try:
            context=contexts[e.attrib["contextRef"]];unit=units[e.attrib["unitRef"]]
            if context.get("unsupported_typed_dimensions") or unit not in ({"INR/shares","INR/share"} if metric=="diluted_eps" else {"INR"}):raise ValueError("unsupported basis")
            if e.attrib.get("format") and local(e.attrib["format"]) not in {"num-dot-decimal","numdotdecimal"}:raise ValueError("unsupported inline format")
            raw_value="".join(e.itertext()).strip()
            if not re.fullmatch(r"[+-]?(?:\d+(?:\.\d*)?|\.\d+)",raw_value):
                if not e.attrib.get("format") or not re.fullmatch(r"[+-]?\d{1,3}(?:,\d{3})+(?:\.\d+)?",raw_value):raise ValueError("numeric format")
            value=number(raw_value.replace(",",""),minimum="-1e30")
            if e.attrib.get("sign") not in {None,"-"}:raise ValueError("sign")
            if e.attrib.get("sign") and raw_value.startswith(("-","+")):raise ValueError("ambiguous sign")
            if e.attrib.get("sign")=="-":value=-value
            scale=int(e.attrib.get("scale","0"))
            if not -12<=scale<=12:raise ValueError("scale")
            value=value*(number(10)**scale)
            # decimals is precision, never a scaling multiplier.
            row={"metric":metric,"value":str(value),"unit":unit,"tag":tag,"context_id":e.attrib["contextRef"],**context}
            key=fingerprint({"metric":metric,"unit":unit,"context":context})
            if key in facts and facts[key]["value"]!=row["value"]:raise Block("XBRL: conflicting facts for same metric/period/basis")
            facts[key]=row
        except (ValueError,KeyError,TypeError) as exc:
            if isinstance(exc,Block) and str(exc).startswith("XBRL: conflicting"):raise
            unsupported+=1
    if not facts:raise Block("XBRL: no supported unambiguous INR financial facts")
    values=list(facts.values());comparisons=[];ttm=[]
    for f in values:
        end=f.get("endDate",f.get("instant"));d=date.fromisoformat(end)
        duration=(d-date.fromisoformat(f["startDate"])).days+1 if "startDate" in f else None
        matches=[]
        for prev in values:
            pd=date.fromisoformat(prev.get("endDate",prev.get("instant")))
            pperiod=(pd-date.fromisoformat(prev["startDate"])).days+1 if "startDate" in prev else None
            if (prev["metric"],prev["unit"],prev["identifier"],prev["dimensions"])!=(f["metric"],f["unit"],f["identifier"],f["dimensions"]):continue
            if pd.year==d.year-1 and (pd.month,pd.day)==(d.month,d.day) and (duration is None and pperiod is None or duration is not None and pperiod is not None and abs(duration-pperiod)<=7):matches.append(prev)
        if len(matches)==1:
            a,b=number(f["value"],minimum="-1e30"),number(matches[0]["value"],minimum="-1e30")
            comparisons.append({"context_id":f["context_id"],"metric":f["metric"],"unit":f["unit"],"current":str(a),"previous":str(b),"change":str(a-b),"change_pct":str((a-b)/b*100) if b>0 else None,"basis":"same-period prior-year; matched entity, dimensions and units"})
    groups={}
    for f in values:
        if f["metric"] not in {"revenue","net_profit"} or "startDate" not in f:continue
        duration=(date.fromisoformat(f["endDate"])-date.fromisoformat(f["startDate"])).days+1
        if not 80<=duration<=100:continue
        key=fingerprint({k:f[k] for k in ("metric","unit","identifier","dimensions")});groups.setdefault(key,[]).append(f)
    for group in groups.values():
        last=sorted(group,key=lambda f:f["endDate"])[-4:]
        if len(last)==4 and all(date.fromisoformat(b["startDate"])==date.fromisoformat(a["endDate"])+timedelta(days=1) for a,b in zip(last,last[1:])):
            ttm.append({**{k:last[-1][k] for k in ("metric","unit","identifier","dimensions")},"startDate":last[0]["startDate"],"endDate":last[-1]["endDate"],"value":str(sum(number(f["value"],minimum="-1e30") for f in last)),"derived":True,"basis":"sum of four supplied contiguous standalone quarter facts; no YTD subtraction"})
    return {"schema":"indian_xbrl_analysis_v1","source":source,"retrieved_at":now.isoformat(),"raw_sha256":hashlib.sha256(body).hexdigest(),
            "facts":values,"year_over_year":comparisons,"trailing_twelve_months":ttm,"unsupported_facts":unsupported,
            "coverage":"Supported supplied XBRL tags only; verify restatements, taxonomy and basis against the filing",
            "published_at":None,"point_in_time_backtest_eligible":False,"submitted":False}


def document(body, source):
    link(source)
    if len(body)>LIMIT:raise Block("DOCUMENT: too large")
    if body.startswith(b"%PDF-"):
        exe=shutil.which("pdftotext")
        if not exe:raise Block("DOCUMENT: install Poppler pdftotext for PDF text extraction; never infer missing text")
        with tempfile.TemporaryDirectory() as tmp:
            src=Path(tmp)/"filing.pdf";out=Path(tmp)/"filing.txt";src.write_bytes(body)
            result=subprocess.run([exe,"-layout",str(src),str(out)],capture_output=True,timeout=30)
            if result.returncode or not out.exists() or out.stat().st_size>LIMIT:raise Block("DOCUMENT: PDF extraction failed")
            text=out.read_text(encoding="utf-8")
    else:
        if b"<!DOCTYPE" in body.upper() or b"<!ENTITY" in body.upper():raise Block("DOCUMENT: external entities refused")
        text=body.decode("utf-8-sig")
    return {"source":source,"text":text,"raw_sha256":hashlib.sha256(body).hexdigest(),"review_required":True,"numeric_facts_verified":False,"submitted":False}


def main(argv=None):
    p=argparse.ArgumentParser(description=__doc__);p.add_argument("command",choices=["news","disclosures","analyst","attachment","disclosure-facts"])
    p.add_argument("--symbols",nargs="+");p.add_argument("--state");p.add_argument("--hours",type=int,default=24);p.add_argument("--all",action="store_true")
    p.add_argument("--input");p.add_argument("--url");p.add_argument("--source");p.add_argument("--now");p.add_argument("--out")
    a=p.parse_args(argv)
    try:
        if a.out and Path(a.out).exists():raise Block("OUTPUT: new output required")
        if a.now and (a.url or a.command in {"news","disclosures"}):raise Block("CLOCK: network uses live clock")
        if a.command in {"news","disclosures"}:
            result=news(a.symbols or [],instant(),hours=a.hours,disclosure_only=a.command=="disclosures")
            if a.state:result=unseen(result,a.state,all_items=a.all)
        elif a.command=="disclosure-facts":result=normalize_disclosures(read(a.input),instant(a.now))
        else:
            if bool(a.input)==bool(a.url):raise Block("INPUT: choose local file or explicit attachment URL")
            body=attachment(a.url) if a.url else Path(a.input).read_bytes();source=a.url or a.source
            result=xbrl(body,source,instant(a.now)) if a.command=="analyst" else document(body,source)
        text=json.dumps(result,indent=2,allow_nan=False)
        if a.out:
            path=Path(a.out);path.parent.mkdir(parents=True,exist_ok=True)
            with path.open("x",encoding="utf-8") as stream:stream.write(text)
        else:print(text)
        return 2 if result.get("failed_sources") and not result.get("records") else 0
    except (Block,ValueError,TypeError,KeyError,OSError,sqlite3.Error,ET.ParseError,subprocess.TimeoutExpired):
        print(json.dumps({"status":"BLOCKED","reason":"Research source/input unavailable or unsupported; review required","submitted":False}));return 2


if __name__=="__main__":sys.exit(main())
