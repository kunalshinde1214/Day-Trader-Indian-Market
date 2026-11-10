"""Match public issuer candidates and retrieve bounded official filing attachments."""
import argparse
import copy
import hashlib
import json
from pathlib import Path
import re
import sys
import subprocess
from urllib.parse import urlsplit
import xml.etree.ElementTree as ET
from core import Block, instant, integer, fingerprint
from public_market import symbol
from research import attachment, xbrl, document
from trader import read


def normalized(value):
    return ' '.join(re.findall(r'[A-Z0-9]+',str(value).upper()))


def match(report,universe):
    if report.get('schema')!='india_news_v1' or not universe.get('source'):raise Block('Sourced news and universe required')
    members=universe['members'];index={};identities=set()
    if not isinstance(members,list) or not 1<=len(members)<=10000:raise Block('Bounded issuer list required')
    for m in members:
        s=symbol(m['symbol']);name=normalized(m['name']);isin=m['isin']
        if s.startswith('^') or s in identities or not name or not isin:raise Block('Invalid/duplicate issuer')
        identities.add(s)
        for key in {name,normalized(s.rsplit('.',1)[0])}:
            # Short symbols are ambiguous ordinary words. Only full names match them.
            if len(key)<4 and key!=name:continue
            index.setdefault(key,[]).append({'symbol':s,'name':m['name'],'isin':isin})
    result=copy.deepcopy(report)
    for r in result['records']:
        text=' '+normalized(r['headline'])+' ';found={}
        for key,values in index.items():
            if ' '+key+' ' in text:
                for value in values:found[value['symbol']]=value
        candidates=list(found.values())
        r['issuer_candidates']=candidates
        r['issuer_match_status']='unique_candidate' if len(candidates)==1 else 'ambiguous' if candidates else 'unmatched'
        r['issuer_match_basis']='Exact normalized full name or whole symbol in headline; no fuzzy match or ownership inference'
        r['issuer_verified']=False
    result['issuer_universe_hash']=fingerprint(universe)
    return result


def eligible_url(value):
    p=urlsplit(value)
    return p.scheme=='https' and not p.username and not p.password and p.port in {None,443} and p.hostname in {'nsearchives.nseindia.com','archives.nseindia.com','www.bseindia.com'} and p.path.lower().endswith(('.xml','.xbrl','.xhtml','.pdf'))


def collect(report,limit=5,*,fetcher=attachment,clock=instant):
    if report.get('schema')!='india_news_v1':raise Block('News report required')
    limit=integer(limit)
    if limit>20:raise Block('At most twenty attachments per invocation')
    candidates=[];seen=set()
    for row in report['records']:
        url=row['url']
        if eligible_url(url) and url not in seen:candidates.append(row);seen.add(url)
    output=[]
    for row in candidates[:limit]:
        item={'url':row['url'],'news_id':row['id'],'published_at':row['published_at'],
              'timezone_verified':row.get('timezone_verified',False),'issuer_candidates':row.get('issuer_candidates',[]),'issuer_verified':False}
        try:
            body=fetcher(row['url']);item['sha256']=hashlib.sha256(body).hexdigest();item['retrieved_at']=clock().isoformat()
            try:item.update(status='PARSED_FINANCIALS',analysis=xbrl(body,row['url'],clock()))
            except (Block,ValueError,KeyError,TypeError,ET.ParseError):
                item.update(status='TEXT_REVIEW_REQUIRED',extraction=document(body,row['url']))
        except (Block,OSError,ValueError,KeyError,TypeError,ET.ParseError,subprocess.TimeoutExpired):item.update(status='FAILED',reason='Unavailable/unsupported official attachment; no facts inferred')
        output.append(item)
    return {'schema':'filing_collection_v1','as_of':clock().isoformat(),'input_hash':fingerprint(report),'discovered':len(candidates),
            'attempted':len(output),'deferred':max(0,len(candidates)-limit),'filings':output,'submitted':False,
            'coverage':'Direct official attachment links in supplied feed only; unstructured facts and issuer identity require review'}


def main(argv=None):
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('command',choices=['match','collect']);p.add_argument('--input',required=True)
    p.add_argument('--universe');p.add_argument('--limit',type=int,default=5);p.add_argument('--out',required=True);a=p.parse_args(argv)
    try:
        path=Path(a.out)
        if path.exists():raise Block('Choose a new output')
        report=read(a.input)
        result=match(report,read(a.universe)) if a.command=='match' else collect(report,a.limit)
        path.parent.mkdir(parents=True,exist_ok=True)
        with path.open('x',encoding='utf-8') as stream:json.dump(result,stream,indent=2,allow_nan=False)
        print(json.dumps({'status':'SAVED','submitted':False,'path':str(path)}));return 0
    except (Block,ValueError,TypeError,KeyError,OSError):print('BLOCKED: invalid/unavailable filing inputs');return 2


if __name__=='__main__':sys.exit(main())
