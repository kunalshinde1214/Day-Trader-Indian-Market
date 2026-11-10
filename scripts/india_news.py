"""Personal Indian-market RSS briefing; no orders, article scraping or delivery."""
import argparse
from datetime import timedelta
import html
import json
from pathlib import Path
import re
import sys
import urllib.request
import xml.etree.ElementTree as ET

from core import Block, instant, integer, fingerprint
from data import rss, SOURCES
from public_market import NoRedirect, LIMIT
from research import CATEGORIES, unseen

FEEDS={"nse-announcements":SOURCES["announcements"],"nse-actions":SOURCES["corporate_actions"],
       "et-markets":"https://economictimes.indiatimes.com/markets/rssfeeds/1977021501.cms"}
FEEDS.update({'nse-results':SOURCES['financial_results'],'nse-integrated':SOURCES['integrated_financials'],
              'nse-insider':SOURCES['insider_trading'],'nse-sast':SOURCES['sast29'],'nse-pledge':SOURCES['encumbrance']})


def fetch(url):
    if url not in FEEDS.values():raise Block("Unsupported news source")
    req=urllib.request.Request(url,headers={"User-Agent":"IndiaDayTrader/0.9 personal RSS reader"})
    with urllib.request.build_opener(NoRedirect()).open(req,timeout=20) as response:body=response.read(LIMIT+1)
    if len(body)>LIMIT:raise Block("Oversized feed")
    return body


def collect(sources, hours=24, *, fetcher=fetch, clock=instant):
    hours=integer(hours)
    if hours>720 or not sources or len(set(sources))!=len(sources) or any(s not in FEEDS for s in sources):raise Block("Invalid sources/window")
    records={};coverage=[]
    for name in sources:
        try:
            body=fetcher(FEEDS[name]);now=clock();feed=rss(body,FEEDS[name],now,nse_local_time=True)
            recent=[r for r in feed['records'] if instant(r['published_at'])>=now-timedelta(hours=hours)]
            coverage.append({'source':name,'status':'OK','received':len(feed['records']),'recent':len(recent),'unparsed':len(feed['unparsed']),'assumed_timezone_items':sum(r.get('timezone_verified') is False for r in feed['records']),'retrieved_at':now.isoformat()})
            for row in recent:
                key=fingerprint({k:row[k] for k in ('headline','url','published_at')})
                categories=[c for c,words in CATEGORIES.items() if any(w in row['headline'].lower() for w in words)]
                if key in records:records[key]['sources'].append(name)
                else:records[key]={'id':key,'headline':row['headline'],'url':row['url'],'published_at':row['published_at'],
                                  'sources':[name],'categories':categories or ['market'],'classification_basis':'headline keywords; unverified',
                                  'timezone_basis':row.get('timezone_basis','Explicit feed offset'),'publication_time_raw':row.get('publication_time_raw'),
                                  'timezone_verified':row.get('timezone_verified',True)}
        except (Block,OSError,ValueError,TypeError,KeyError,ET.ParseError):coverage.append({'source':name,'status':'FAILED','reason':'Unavailable or invalid feed; no bypass/retry'})
    successful=sum(s['status']=='OK' for s in coverage)
    return {'schema':'india_news_v1','as_of':clock().isoformat(),'hours':hours,
            'status':'BLOCKED' if not successful else 'PARTIAL' if successful<len(sources) or any(s.get('unparsed') or s.get('assumed_timezone_items') for s in coverage) else 'COLLECTED',
            'records':sorted(records.values(),key=lambda r:instant(r['published_at']),reverse=True),'sources':coverage,
            'coverage':'Selected RSS items only; not complete market or event clearance','submitted':False}


def safe(text):
    text=html.escape(re.sub(r'[\r\n\t]+',' ',str(text)))
    return re.sub(r'([\\`*_{}\[\]()#!|])',r'\\\1',text)


def markdown(report,limit=30):
    limit=integer(limit)
    if limit>200:raise Block('Maximum 200 displayed headlines')
    rows=report['records'][:limit]
    lines=['# Indian market news','',f"{report['as_of']} | {report['status']} | Last {report['hours']} hours",'',report['coverage'],
           'Personal reading only. Headlines are source claims, not verified trading signals.','',
           f"Showing {len(rows)} of {len(report['records'])} items; previously seen filtered: {report.get('seen_filtered',0)}.",'']
    for row in rows:
        url=row['url'].replace(' ','%20').replace('(','%28').replace(')','%29').replace('<','%3C').replace('>','%3E')
        lines.extend([f"- [{safe(row['headline'])}]({url})",f"  {safe(', '.join(row['sources']))} · {row['published_at']} · {safe(', '.join(row['categories']))}"])
        if row.get('timezone_verified') is False:lines.append('  Timezone: IST assumed; publisher timestamp has no offset.')
    if not rows:lines.append('No recent unseen headlines returned. This does not mean no market news exists.')
    lines.extend(['','Source coverage:'])
    for s in report['sources']:lines.append(f"- {s['source']}: {s['status']}; recent {s.get('recent','unknown')}; unparsed {s.get('unparsed','unknown')}")
    return '\n'.join(lines)+'\n'


def main(argv=None):
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--sources',nargs='+',choices=sorted(FEEDS),default=['nse-announcements','nse-actions'])
    p.add_argument('--hours',type=int,default=24);p.add_argument('--limit',type=int,default=30)
    p.add_argument('--state');p.add_argument('--all',action='store_true');p.add_argument('--format',choices=['json','markdown'],default='markdown');p.add_argument('--out')
    a=p.parse_args(argv)
    try:
        if not 1<=a.limit<=200 or a.out and Path(a.out).exists():raise Block('Invalid limit or output already exists')
        result=collect(a.sources,a.hours)
        if a.state:result=unseen(result,a.state,all_items=a.all)
        body=json.dumps(result,indent=2,allow_nan=False) if a.format=='json' else markdown(result,a.limit)
        if a.out:
            path=Path(a.out);path.parent.mkdir(parents=True,exist_ok=True)
            with path.open('x',encoding='utf-8') as stream:stream.write(body)
        else:print(body)
        return 2 if result['status']=='BLOCKED' else 0
    except (Block,OSError,ValueError):print('BLOCKED: news source/input unavailable; no clearance inferred');return 2


if __name__=='__main__':sys.exit(main())
