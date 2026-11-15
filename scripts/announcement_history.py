"""Bounded public NSE announcement backfill; never complete event clearance."""
import argparse
from datetime import date, datetime, timedelta
import hashlib
import json
from pathlib import Path
import re
import urllib.parse
import urllib.request

from core import Block, IST, instant, fingerprint
from data import MAX_BYTES, NoRedirect
from filing_workflow import eligible_url, discover_links

BASE = 'https://www.nseindia.in/api/corporate-announcements'
PAGE = 'https://www.nseindia.in/companies-listing/corporate-filings-announcements'


def endpoint(day):
    stamp = day.strftime('%d-%m-%Y')
    return BASE + '?' + urllib.parse.urlencode({'index': 'equities', 'from_date': stamp, 'to_date': stamp})


def fetch(url):
    parsed = urllib.parse.urlsplit(url); query = urllib.parse.parse_qs(parsed.query)
    if parsed.scheme != 'https' or parsed.netloc != 'www.nseindia.in' or parsed.path != '/api/corporate-announcements' or parsed.fragment:
        raise Block('ANNOUNCEMENTS: unsupported endpoint')
    if set(query) != {'index', 'from_date', 'to_date'} or query['index'] != ['equities'] or query['from_date'] != query['to_date'] or len(query['from_date']) != 1:
        raise Block('ANNOUNCEMENTS: bounded daily query required')
    day = datetime.strptime(query['from_date'][0], '%d-%m-%Y').date()
    if endpoint(day) != url:
        raise Block('ANNOUNCEMENTS: canonical endpoint required')
    req = urllib.request.Request(url, headers={'User-Agent': 'IndiaDayTrader personal research', 'Accept': 'application/json'})
    with urllib.request.build_opener(NoRedirect()).open(req, timeout=20) as response:
        body = response.read(MAX_BYTES + 1)
    if len(body) > MAX_BYTES:
        raise Block('ANNOUNCEMENTS: oversized response; coverage unresolved')
    return body


def parse(body, session, now):
    if not isinstance(body, bytes) or not body or len(body) > MAX_BYTES:
        raise Block('ANNOUNCEMENTS: empty/oversized response')
    rows = json.loads(body)
    if not isinstance(rows, list) or len(rows) >= 10000:
        raise Block('ANNOUNCEMENTS: unsupported or possibly truncated response')
    digest = hashlib.sha256(body).hexdigest(); records = {}; rejected = 0
    for row in rows:
        try:
            sid = row['seq_id']; symbol = row['symbol']; name = row['sm_name']; subject = row['desc']
            if not isinstance(sid, str) or not re.fullmatch(r'\d{1,30}', sid) or not isinstance(symbol, str) or not re.fullmatch(r'[A-Z0-9&_.-]{1,40}', symbol):
                raise Block('ANNOUNCEMENTS: missing exact record/security identity')
            if not isinstance(name, str) or not name.strip() or not isinstance(subject, str) or not subject.strip():
                raise Block('ANNOUNCEMENTS: missing announcement heading')
            submitted = datetime.strptime(row['an_dt'], '%d-%b-%Y %H:%M:%S').replace(tzinfo=IST)
            released = datetime.strptime(row.get('exchdisstime') or row['an_dt'], '%d-%b-%Y %H:%M:%S').replace(tzinfo=IST)
            if released < submitted or submitted.date() != session or released > now:
                raise Block('ANNOUNCEMENTS: contradictory/unavailable publication time')
            urls = []
            direct = row.get('attchmntFile')
            if eligible_url(direct): urls.append(direct)
            for url in discover_links(row.get('attchmntText') or '', PAGE):
                if url not in urls: urls.append(url)
            isin = row.get('sm_isin')
            identity = None
            if isinstance(isin, str) and re.fullmatch(r'IN[A-Z0-9]{10}', isin):
                identity = {'exchange': 'NSE', 'symbol': symbol, 'isin': isin, 'source': endpoint(session),
                            'source_record_id': sid, 'raw_response_sha256': digest}
            record = {'id': fingerprint({'source': BASE, 'seq_id': sid}), 'headline': name.strip() + ' — ' + subject.strip(),
                      'url': urls[0] if urls else PAGE, 'attachments': urls, 'published_at': released.isoformat(),
                      'submitted_at': submitted.isoformat(), 'sources': ['nse-announcement-history'], 'categories': ['exchange_announcement'],
                      'classification_basis': 'exchange announcement subject; contents unverified',
                      'timezone_verified': False, 'timezone_basis': 'IST assumed for NSE timestamp without explicit offset',
                      'publication_time_raw': row.get('exchdisstime') or row['an_dt'],
                      'publication_basis': 'exchange dissemination time' if row.get('exchdisstime') else 'announcement time; dissemination unavailable',
                      'issuer_evidence': identity, 'issuer_verified': False,
                      'xbrl_advertised': row.get('hasXbrl') is True,
                      'xbrl_attachment_discovered': any(urllib.parse.urlsplit(u).path.lower().endswith(('.xml', '.xbrl', '.xhtml')) for u in urls)}
            if record['id'] in records and records[record['id']] != record:
                raise Block('ANNOUNCEMENTS: conflicting duplicate source ID')
            records[record['id']] = record
        except (Block, ValueError, KeyError, TypeError):
            # A conflicting identifier invalidates the day, not just one competing row.
            if isinstance(row, dict) and isinstance(row.get('seq_id'), str) and fingerprint({'source': BASE, 'seq_id': row['seq_id']}) in records:
                raise Block('ANNOUNCEMENTS: conflicting/invalid duplicate source ID') from None
            rejected += 1
    return {'records': list(records.values()), 'source': endpoint(session), 'sha256': digest,
            'requested_session': session.isoformat(), 'received': len(rows), 'rejected': rejected,
            'status': 'PARTIAL' if rejected else 'COLLECTED', 'pagination_verified': False}


def collect(start, end, *, fetcher=fetch, clock=instant):
    now = clock(); first = date.fromisoformat(start); last = date.fromisoformat(end)
    if first.isoformat() != start or last.isoformat() != end or not first <= last <= now.date() or (last-first).days >= 7:
        raise Block('ANNOUNCEMENTS: one to seven nonfuture calendar dates required')
    records = {}; coverage = []
    for offset in range((last-first).days + 1):
        session = first + timedelta(days=offset)
        try:
            report = parse(fetcher(endpoint(session)), session, now)
            day_records = report.pop('records')
            for row in day_records:
                if row['id'] in records and records[row['id']] != row:
                    raise Block('ANNOUNCEMENTS: duplicate identity across dates')
            records.update({row['id']:row for row in day_records})
            coverage.append(report)
        except (Block, ValueError, KeyError, TypeError, OSError):
            coverage.append({'source': endpoint(session), 'requested_session': session.isoformat(), 'status': 'FAILED',
                             'reason': 'Unavailable, oversized or unsupported response; no substitute or automatic retry'})
    return {'schema': 'india_news_v1', 'as_of': now.isoformat(), 'status': 'PARTIAL', 'hours': None,
            'requested_from': start, 'requested_through': end,
            'records': sorted(records.values(), key=lambda r: (r['published_at'], r['id']), reverse=True),
            'sources': coverage, 'full_filing_coverage': False, 'execution_eligible': False, 'submitted': False,
            'coverage': 'Bounded NSE equities announcement dates; BSE, silent truncation, revisions and undiscovered XBRL links remain unresolved'}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--from-date', required=True); parser.add_argument('--through-date', required=True)
    parser.add_argument('--out', required=True); parser.add_argument('--format',choices=['json','markdown'],default='json')
    parser.add_argument('--limit',type=int,default=30); args = parser.parse_args(argv)
    try:
        out = Path(args.out)
        if out.exists() or not 1<=args.limit<=200: raise Block('OUTPUT: new file and display limit 1..200 required')
        result = collect(args.from_date, args.through_date)
        if args.format=='markdown':
            from india_news import markdown
            rendered=markdown(result,args.limit)
        else:rendered=json.dumps(result,indent=2,allow_nan=False)
        out.parent.mkdir(parents=True, exist_ok=True)
        with out.open('x', encoding='utf-8') as stream: stream.write(rendered)
        print(json.dumps({'status': 'PARTIAL', 'records': len(result['records']), 'days': len(result['sources']), 'submitted': False}))
        return 0 if any(s['status'] != 'FAILED' for s in result['sources']) else 2
    except (Block, ValueError, KeyError, TypeError, OSError):
        print('BLOCKED: invalid announcement window or output'); return 2


if __name__ == '__main__':
    raise SystemExit(main())
