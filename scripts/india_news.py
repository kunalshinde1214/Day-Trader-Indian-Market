"""Personal Indian-market RSS briefing; no orders, article scraping or delivery."""
import argparse
from datetime import timedelta
import html
import http.client
import json
from pathlib import Path
import re
import sys
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET

from core import Block, instant, integer, fingerprint
from data import rss, SOURCES
from public_market import NoRedirect, LIMIT
from research import CATEGORIES, unseen
from filing_workflow import discover_links

# Exchange announcement SUBJECT lines sorted by how often they move a stock. Matched as lower-case substrings.
MATERIAL=("financial result","outcome of board meeting","dividend","bonus","split","buyback","buy back","acquisition","amalgamation",
          "merger","demerger","scheme of arrangement","bagging","orders/contracts","agreements","mou","credit rating","fund raising",
          "qualified institutions","preferential","rights issue","change in management","resignation of director","resignation of statutory auditor",
          "insolvency","litigation","action(s) taken","orders passed","takeover","open offer","suspension","capacity addition",
          "commencement of commercial production","delisting","fraud","payment default","defaulted","usfda","warning letter","import alert")
ROUTINE=("declaration of nav","analysts/institutional investor meet","analyst/investor meet","copy of newspaper","newspaper publication",
         "noc/no dues","confirmation of redemption","payment of interest","esop","esos","esps","options to purchase securities",
         "regulation 50","regulation 51","regulation 52","structural digital database","address change","company secretary",
         "statement of deviation","corrigendum","asset/liability management","trading window","loss of share certificate",
         "duplicate share certificate","certificate under reg","reg. 74","notice of shareholders meetings-xbrl","link of recording",
         "schedule of meet","transcript")


MATERIAL_RE=re.compile(r'(?<![a-z])(?:'+'|'.join(re.escape(w) for w in MATERIAL)+r')(?![a-z])')
ROUTINE_RE=re.compile(r'(?<![a-z])(?:'+'|'.join(re.escape(w) for w in ROUTINE)+r')')


def subject_of(description):
    match=re.search(r'SUBJECT:\s*(.+?)\s*$',str(description or '').strip())
    return match.group(1).strip() if match else None


DIVIDEND_RE=re.compile(r'(?<![a-z])(?:interim dividend|final dividend|special dividend|dividend|record date|ex-date|book closure)(?![a-z])')
LARGE_CAP_LIST='https://nsearchives.nseindia.com/content/indices/ind_nifty100list.csv'


def company_key(name):
    """'Oil India Limited - Ex-Date: 20-Nov-2026' / 'Oil India Ltd.' -> 'oil india'."""
    name=re.split(r'\s+-\s+ex-date',str(name or ''),flags=re.I)[0].lower()
    name=re.sub(r'[^a-z0-9& ]+',' ',name)
    return ' '.join(w for w in name.split() if w not in {'limited','ltd','the','co','company','corporation','corp'})


def importance(subject, text, headline='', large_caps=None):
    """material / notable / routine from the exchange SUBJECT plus the announcement text. Items without a subject stay notable.

    Routine dividend/record-date ex-date notices are not market-moving: they are 'notable' for Nifty 100 companies
    (when that list is supplied) and 'routine' for everyone else; without a list they are 'notable'."""
    s=(subject or '').lower();body=f"{s} {text or ''}".lower()
    if MATERIAL_RE.search(body):
        if DIVIDEND_RE.search(body) and not MATERIAL_RE.search(DIVIDEND_RE.sub(' ',body)):
            if large_caps is None:return 'notable'
            return 'notable' if company_key(headline) in large_caps else 'routine'
        return 'material'
    if subject and ROUTINE_RE.search(s):return 'routine'
    return 'notable'


FEEDS={"nse-announcements":SOURCES["announcements"],"nse-actions":SOURCES["corporate_actions"],
       "et-markets":"https://economictimes.indiatimes.com/markets/rssfeeds/1977021501.cms"}
FEEDS.update({'nse-results':SOURCES['financial_results'],'nse-integrated':SOURCES['integrated_financials'],
              'nse-insider':SOURCES['insider_trading'],'nse-sast':SOURCES['sast29'],'nse-pledge':SOURCES['encumbrance']})
# Google News search RSS (India edition). Google offers it for personal feed readers only: headlines and links are shown,
# never article text, and they are labelled as unverified. One exact URL shape; the query is URL-encoded.
GOOGLE_NEWS='https://news.google.com/rss/search?q={}&hl=en-IN&gl=IN&ceid=IN:en'
GOOGLE_NEWS_RE=re.compile(r'https://news\.google\.com/rss/search\?q=[A-Za-z0-9%+._-]{1,160}&hl=en-IN&gl=IN&ceid=IN:en')
FEEDS['google-news-markets']=GOOGLE_NEWS.format('Sensex+Nifty+when%3A1d')


def google_news_url(query):
    """Exact Google News search-RSS URL for ``query`` (for example '"Titan Company" share when:1d')."""
    url=GOOGLE_NEWS.format(urllib.parse.quote_plus(str(query)))
    if not GOOGLE_NEWS_RE.fullmatch(url):raise Block("Unsupported news query")
    return url


def publisher(headline):
    """Google News titles end with ' - Publisher': (title, publisher)."""
    title,sep,name=str(headline).rpartition(' - ')
    return (title.strip(),name.strip()) if sep and title.strip() and 0<len(name.strip())<=60 else (str(headline).strip(),None)


SOCIAL_PUBLISHERS=re.compile(r"^(?:linkedin|facebook|instagram|x|twitter|youtube|reddit|telegram|quora|threads|tiktok|pinterest|medium)(?:\.com)?$",re.I)


def search(query, hours=24, *, fetcher=None, clock=instant):
    """Recent Google News headlines for one query, newest first: [{headline, publisher, url, published_at}]. Posts on
    social networks (LinkedIn, Facebook, X, YouTube ...) are dropped: they are people's posts, not reporting."""
    url=google_news_url(query);now=clock()
    feed=rss((fetcher or fetch)(url),url,now)
    rows=[r for r in feed['records'] if instant(r['published_at'])>=now-timedelta(hours=integer(hours))]
    out=[]
    for r in sorted(rows,key=lambda r:instant(r['published_at']),reverse=True):
        title,name=publisher(r['headline'])
        if name and SOCIAL_PUBLISHERS.match(name.strip()):continue
        out.append({'headline':title,'publisher':name,'url':r['url'],'published_at':r['published_at'],'source':'Google News'})
    return out


def fetch(url, timeout=20):
    if url not in FEEDS.values() and url!=LARGE_CAP_LIST and not GOOGLE_NEWS_RE.fullmatch(url):raise Block("Unsupported news source")
    req=urllib.request.Request(url,headers={"User-Agent":"IndiaDayTrader/0.9 personal RSS reader"})
    try:
        from nse_live import read_within  # a feed that drips bytes is cut off after 3 per-read timeouts
        with urllib.request.build_opener(NoRedirect()).open(req,timeout=timeout) as response:body=read_within(response,LIMIT,timeout*3)
    except http.client.HTTPException:raise Block("News source connection dropped") from None  # e.g. IncompleteRead
    if len(body)>LIMIT:raise Block("Oversized feed")
    return body


def large_cap_names(body):
    """Normalized company names from NSE's Nifty 100 constituent CSV."""
    import csv,io
    rows=list(csv.DictReader(io.StringIO(body.decode('utf-8-sig') if isinstance(body,bytes) else body),skipinitialspace=True))
    names={company_key(r.get('Company Name')) for r in rows if r.get('Company Name')}
    if len(names)<80:raise Block('Unexpected Nifty 100 list')
    return names


def collect(sources, hours=24, *, fetcher=fetch, clock=instant, large_caps=None):
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
                subject=subject_of(row.get('description'));text=re.sub(r'\s*\|?\s*SUBJECT:.*$','',str(row.get('description') or '')).strip()
                haystack=f"{row['headline']} {subject or ''} {text}".lower()
                categories=[c for c,words in CATEGORIES.items() if any(w in haystack for w in words)]
                if key in records:records[key]['sources'].append(name)
                else:records[key]={'id':key,'headline':row['headline'],'url':row['url'],'published_at':row['published_at'],
                                  'sources':[name],'categories':categories or ['market'],'subject':subject,
                                  'importance':importance(subject,text,row['headline'],large_caps),'summary':text[:240] or None,'classification_basis':'headline keywords; unverified',
                                  'timezone_basis':row.get('timezone_basis','Explicit feed offset'),'publication_time_raw':row.get('publication_time_raw'),
                                  'timezone_verified':row.get('timezone_verified',True),
                                  'attachments':discover_links(row.get('description',''),row['url'])}
        except (Block,OSError,ValueError,TypeError,KeyError,ET.ParseError):coverage.append({'source':name,'status':'FAILED','reason':'Unavailable or invalid feed; no bypass/retry'})
    successful=sum(s['status']=='OK' for s in coverage)
    return {'schema':'india_news_v1','as_of':clock().isoformat(),'hours':hours,
            'status':'BLOCKED' if not successful else 'PARTIAL' if successful<len(sources) or any(s.get('unparsed') or s.get('assumed_timezone_items') for s in coverage) else 'COLLECTED',
            'records':sorted(records.values(),key=lambda r:instant(r['published_at']),reverse=True),'sources':coverage,
            'coverage':'Selected RSS items only; not complete market or event clearance','submitted':False}


def safe(text):
    text=html.escape(re.sub(r'[\r\n\t]+',' ',str(text)))
    return re.sub(r'([\\`*_{}\[\]()#!|])',r'\\\1',text)


def markdown(report,limit=30,include_routine=False):
    limit=integer(limit)
    if limit>200:raise Block('Maximum 200 displayed headlines')
    kept=[r for r in report['records'] if include_routine or r.get('importance','notable')!='routine']
    order={'material':0,'notable':1,'routine':2}
    rows=sorted(kept,key=lambda r:order.get(r.get('importance','notable'),1))[:limit]  # stable: newest first within each level
    hidden=len(report['records'])-len(kept)
    window=(report['requested_from']+' through '+report['requested_through']) if report.get('requested_from') else f"Last {report['hours']} hours"
    lines=['# Indian market news','',f"{report['as_of']} | {report['status']} | {window}",'',report['coverage'],
           'Personal reading only. Headlines are source claims, not verified trading signals.',
           *(['Timezone: IST assumed for NSE items; the publisher timestamp has no offset.'] if any(r.get('timezone_verified') is False for r in report['records']) else []),'',
           f"Showing {len(rows)} of {len(report['records'])} items; routine filings hidden: {hidden}; previously seen filtered: {report.get('seen_filtered',0)}.",'']
    section=None
    for row in rows:
        level=row.get('importance','notable')
        if level!=section:
            section=level;lines.extend([x for x in ('' if lines[-1] else None,f"## {dict(material='Material',notable='Notable',routine='Routine').get(level,level.title())}",'') if x is not None])
        url=row['url'].replace(' ','%20').replace('(','%28').replace(')','%29').replace('<','%3C').replace('>','%3E')
        title=row['headline'] or 'Untitled'
        if row.get('subject') and row['subject'].lower() not in title.lower():title=f"{title} — {row['subject']}"
        lines.extend([f"- [{safe(title)}]({url})",f"  {safe(', '.join(row['sources']))} · {row['published_at']} · {safe(', '.join(row['categories']))}"])
        summary=re.sub(r'^.*?has (?:informed|submitted to) the Exchange(?:,)? (?:about|regarding)?\s*','',row.get('summary') or '',flags=re.I).strip(" '.")
        if level=='material' and summary and summary.lower()!=(row.get('subject') or '').lower():lines.append(f"  {safe(summary[:200])}")

    if not rows:lines.append('No recent unseen headlines returned. This does not mean no market news exists.')
    lines.extend(['','Source coverage:'])
    for s in report['sources']:lines.append(f"- {s['source']}: {s['status']}; recent {s.get('recent','unknown')}; unparsed {s.get('unparsed','unknown')}")
    return '\n'.join(lines)+'\n'


def main(argv=None):
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--sources',nargs='+',choices=sorted(FEEDS),default=['nse-announcements','nse-actions'])
    p.add_argument('--hours',type=int,default=24);p.add_argument('--limit',type=int,default=30)
    p.add_argument('--state');p.add_argument('--all',action='store_true');p.add_argument('--include-routine',action='store_true',help='Also list routine filings (NAVs, analyst-meet notices, newspaper copies...)');p.add_argument('--format',choices=['json','markdown'],default='markdown');p.add_argument('--out')
    a=p.parse_args(argv)
    try:
        if not 1<=a.limit<=200 or a.out and Path(a.out).exists():raise Block('Invalid limit or output already exists')
        try:large=large_cap_names(fetch(LARGE_CAP_LIST))
        except (Block,OSError,ValueError,KeyError):large=None  # without the list, dividend notices stay 'notable', never 'material'
        result=collect(a.sources,a.hours,large_caps=large)
        if a.state:result=unseen(result,a.state,all_items=a.all)
        body=json.dumps(result,indent=2,allow_nan=False) if a.format=='json' else markdown(result,a.limit,a.include_routine)
        if a.out:
            path=Path(a.out);path.parent.mkdir(parents=True,exist_ok=True)
            with path.open('x',encoding='utf-8') as stream:stream.write(body)
        else:
            sys.stdout.reconfigure(encoding='utf-8');print(body)
        return 2 if result['status']=='BLOCKED' else 0
    except (Block,OSError,ValueError):print('BLOCKED: news source/input unavailable; no clearance inferred');return 2


if __name__=='__main__':sys.exit(main())
