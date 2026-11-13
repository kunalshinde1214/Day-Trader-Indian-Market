"""Bounded automatic Indian public market/calendar/news/filing collection.

Reports observed coverage; never turns absence in a public feed into trading clearance.
"""
import argparse
from datetime import datetime
import hashlib
import json
import sqlite3
from pathlib import Path
import urllib.request
from core import Block, IST, instant, fingerprint, integer
from data import NoRedirect, MAX_BYTES
from india_news import collect as news_collect, FEEDS
from filing_workflow import collect as filing_collect, match
from public_market import collect_chart, symbol

NSE_HOLIDAYS='https://www.nseindia.com/api/holiday-master?type=trading'
NSE_STATUS='https://www.nseindia.com/api/marketStatus'


def official_fetch(url):
    if url not in (NSE_HOLIDAYS,NSE_STATUS):raise Block('CALENDAR: unsupported endpoint')
    request=urllib.request.Request(url,headers={'User-Agent':'IndiaDayTrader personal research','Accept':'application/json'})
    with urllib.request.build_opener(NoRedirect()).open(request,timeout=20) as response:
        body=response.read(MAX_BYTES+1)
    if len(body)>MAX_BYTES:raise Block('CALENDAR: oversized response')
    return body


def calendar_observation(raw,day,now):
    data=json.loads(raw)
    segments={}
    for key in ('CM','FO'):
        rows=data.get(key)
        if not isinstance(rows,list) or not rows:raise Block('CALENDAR: nonempty CM/FO annual lists required')
        parsed={}
        for row in rows:
            date=datetime.strptime(row['tradingDate'],'%d-%b-%Y').date()
            if date.year!=day.year or date in parsed or not row.get('description'):
                raise Block('CALENDAR: wrong year, duplicate or missing holiday description')
            parsed[date]=row['description']
        description=parsed.get(day)
        special=description is not None and ('*' in description or 'muhurat' in description.lower() or 'diwali laxmi' in description.lower())
        segments[key]={'listed_holiday':description is not None,'description':description,'special_session_notice_required':special,
                       'status':'SPECIAL_SESSION_UNRESOLVED' if special else 'LISTED_HOLIDAY' if description else 'NOT_LISTED_AS_HOLIDAY'}
    return {'schema':'india_calendar_observation_v1','source':NSE_HOLIDAYS,'date':day.isoformat(),
            'as_of':now.isoformat(),'sha256':hashlib.sha256(raw).hexdigest(),'segments':segments,
            'execution_eligible':False,'coverage':'NSE annual CM/FO holiday list only; absence is not proof of an open session. Does not cover BSE, ad-hoc closures, halts, settlement dates or special-session timings.'}


def status_observation(raw,now):
    data=json.loads(raw)
    rows=[r for r in data['marketState'] if r.get('market')=='Capital Market']
    if len(rows)!=1:raise Block('STATUS: unique capital-market observation required')
    row=rows[0]
    at=datetime.strptime(row['tradeDate'],'%d-%b-%Y %H:%M').replace(tzinfo=IST)
    if at.date()!=now.date() or not 0<=(now-at).total_seconds()<=180:
        raise Block('STATUS: stale or future market-state observation')
    if row.get('marketStatus') not in ('Open','Closed'):raise Block('STATUS: unrecognized market phase')
    return {'source':NSE_STATUS,'as_of':at.isoformat(),'retrieved_at':now.isoformat(),'market_status':row['marketStatus'],
            'message':row.get('marketStatusMessage'),'sha256':hashlib.sha256(raw).hexdigest(),
            'execution_eligible':False,'coverage':'Aggregate NSE cash-market observation; not instrument-level halt or CAS clearance.'}


def collect(symbols, *, sources=None, hours=24, filing_limit=5, universe=None, archive=None,
            fetcher=official_fetch, charts=collect_chart, news=news_collect, filings=filing_collect, clock=instant):
    if not isinstance(symbols,list) or not 1<=len(symbols)<=20 or len(set(symbols))!=len(symbols):
        raise Block('COLLECT: one to twenty distinct Indian symbols required')
    for selected in symbols:symbol(selected)
    if not 1<=integer(filing_limit)<=20:raise Block('COLLECT: filing limit 1..20')
    if not 1<=integer(hours)<=720:raise Block('COLLECT: news window must be 1..720 hours')
    selected_sources=list(FEEDS) if sources is None else sources
    if not selected_sources or any(s not in FEEDS for s in selected_sources):raise Block('COLLECT: invalid news sources')
    now=clock();coverage=[];result={'schema':'india_collection_v1','as_of':now.isoformat(),'charts':{}}
    for name,url,parser in (('calendar',NSE_HOLIDAYS,lambda body:calendar_observation(body,now.date(),now)),
                            ('market_status',NSE_STATUS,lambda body:status_observation(body,now))):
        try:
            result[name]=parser(fetcher(url));coverage.append({'component':name,'status':'COLLECTED'})
        except (Block,ValueError,KeyError,TypeError,OSError):
            coverage.append({'component':name,'status':'FAILED','reason':'Unavailable, stale or unsupported official response'})
    for selected in symbols:
        try:
            result['charts'][selected]=charts(selected,'5m')
            coverage.append({'component':'chart:'+selected,'status':'COLLECTED'})
        except (Block,ValueError,KeyError,TypeError,OSError):coverage.append({'component':'chart:'+selected,'status':'FAILED'})
    result['news']=news(selected_sources,hours,clock=clock)
    if universe is not None:result['news']=match(result['news'],universe)
    coverage.extend({'component':'news:'+s['source'],'status':s['status']} for s in result['news']['sources'])
    if archive is not None:
        from filing_archive import run
        result['filings']=run(result['news'],archive,limit=filing_limit,collector=filings,clock=clock)
    else:result['filings']=filings(result['news'],filing_limit,clock=clock)
    coverage.append({'component':'filings','status':'PARTIAL' if result['filings']['deferred'] or any(f['status']!='PARSED_FINANCIALS' for f in result['filings']['filings']) else 'COLLECTED',
                     'attempted':result['filings']['attempted'],'deferred':result['filings']['deferred']})
    result.update(status='PARTIAL',coverage=coverage,full_market_coverage=False,execution_eligible=False,submitted=False,
                  missing_coverage=['BSE-specific calendar and comprehensive announcements','Ad-hoc session notices and instrument-specific continuous/CAS windows',
                                    'Complete ASM/GSM/T2T/ban and F&O membership clearance','Historical filing backfill, attachment discovery beyond direct feed links and verified issuer/fact mapping'])
    result['collection_hash']=fingerprint(result)
    return result


def main(argv=None):
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--symbols',nargs='+',required=True)
    p.add_argument('--sources',nargs='+',choices=sorted(FEEDS));p.add_argument('--hours',type=int,default=24)
    p.add_argument('--filing-limit',type=int,default=5);p.add_argument('--universe');p.add_argument('--out',required=True)
    p.add_argument('--archive',help='Personal SQLite filing backlog; later runs continue pending attachments')
    a=p.parse_args(argv)
    try:
        from trader import read
        out=Path(a.out)
        if out.exists():raise Block('OUTPUT: choose a new file')
        result=collect(a.symbols,sources=a.sources,hours=a.hours,filing_limit=a.filing_limit,universe=read(a.universe) if a.universe else None,archive=a.archive)
        out.parent.mkdir(parents=True,exist_ok=True)
        with out.open('x',encoding='utf-8') as f:json.dump(result,f,indent=2,allow_nan=False)
        print(json.dumps({'status':result['status'],'out':str(out),'full_market_coverage':False,'submitted':False}));return 0
    except (Block,KeyError,TypeError,ValueError,OSError,sqlite3.Error):
        print('BLOCKED: invalid or unavailable collection inputs; no trading clearance inferred');return 2


if __name__=='__main__':raise SystemExit(main())
