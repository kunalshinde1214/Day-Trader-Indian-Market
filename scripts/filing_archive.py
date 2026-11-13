"""Durable personal filing backlog: bounded downloads, deduplication, explicit retries."""
import argparse
import json
from pathlib import Path
import sqlite3
from core import Block, fingerprint, instant, integer
from filing_workflow import eligible_url, collect
from service import ProcessLock


def run(report, database, *, limit=5, retry_failed=False, collector=collect, clock=instant):
    if report.get('schema')!='india_news_v1' or not isinstance(report.get('records'),list):
        raise Block('ARCHIVE: normalized news report required')
    limit=integer(limit)
    if limit>20 or type(retry_failed) is not bool:raise Block('ARCHIVE: limit must be 1..20')
    path=Path(database);path.parent.mkdir(parents=True,exist_ok=True)
    with ProcessLock(path.with_suffix(path.suffix+'.lock')):
        db=sqlite3.connect(path)
        try:
            db.execute('CREATE TABLE IF NOT EXISTS filing_backlog (id TEXT PRIMARY KEY, row_json TEXT NOT NULL, status TEXT NOT NULL, result_json TEXT, discovered_at TEXT NOT NULL, updated_at TEXT NOT NULL, attempts INTEGER NOT NULL DEFAULT 0)')
            added=0
            with db:
                for row in report['records']:
                    if not eligible_url(row['url']):continue
                    instant(row['published_at'])
                    key=fingerprint({'url':row['url'],'published_at':row['published_at']})
                    body=json.dumps(row,allow_nan=False)
                    added+=db.execute("INSERT OR IGNORE INTO filing_backlog VALUES (?,?,'PENDING',NULL,?,?,0)",
                                      (key,body,clock().isoformat(),clock().isoformat())).rowcount
                if retry_failed:db.execute("UPDATE filing_backlog SET status='PENDING' WHERE status='FAILED'")
            selected=db.execute("SELECT id,row_json FROM filing_backlog WHERE status='PENDING' ORDER BY discovered_at,id LIMIT ?",(limit,)).fetchall()
            completed=[]
            for key,body in selected:
                row=json.loads(body)
                result=collector({'schema':'india_news_v1','records':[row]},1,clock=clock)
                if result.get('attempted')!=1 or len(result.get('filings',[]))!=1:raise Block('ARCHIVE: collector did not account for selected filing')
                item=result['filings'][0]
                if item.get('url')!=row['url'] or item.get('news_id')!=row['id'] or item.get('status') not in ('FAILED','TEXT_REVIEW_REQUIRED','PARSED_FINANCIALS'):
                    raise Block('ARCHIVE: result identity/status mismatch')
                with db:
                    db.execute('UPDATE filing_backlog SET status=?, result_json=?, updated_at=?, attempts=attempts+1 WHERE id=?',
                               (item['status'],json.dumps(item,allow_nan=False),clock().isoformat(),key))
                completed.append(item)
            counts=dict(db.execute('SELECT status,count(*) FROM filing_backlog GROUP BY status').fetchall())
            return {'schema':'filing_archive_batch_v1','as_of':clock().isoformat(),'discovered_new':added,'attempted':len(completed),
                    'deferred':counts.get('PENDING',0),'filings':completed,'backlog_counts':counts,'submitted':False,
                    'full_filing_coverage':False,'coverage':'Personal archive of direct official links observed in supplied news; historical backfill and unobserved revisions are not complete. Parsed facts and issuer candidates still require review.'}
        finally:db.close()


def main(argv=None):
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--input',required=True);p.add_argument('--database',required=True)
    p.add_argument('--limit',type=int,default=5);p.add_argument('--retry-failed',action='store_true');p.add_argument('--out',required=True)
    a=p.parse_args(argv)
    try:
        from trader import read
        out=Path(a.out)
        if out.exists():raise Block('OUTPUT: choose a new file')
        result=run(read(a.input),a.database,limit=a.limit,retry_failed=a.retry_failed)
        out.parent.mkdir(parents=True,exist_ok=True)
        with out.open('x',encoding='utf-8') as f:json.dump(result,f,indent=2,allow_nan=False)
        print(json.dumps({'attempted':result['attempted'],'deferred':result['deferred'],'submitted':False}));return 0
    except (Block,KeyError,TypeError,ValueError,OSError,sqlite3.Error):
        print('BLOCKED: filing backlog input/storage/collection failure; inspect local evidence');return 2


if __name__=='__main__':raise SystemExit(main())
