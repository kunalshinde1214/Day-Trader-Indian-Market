import json
from pathlib import Path
import sqlite3
import sys
import tempfile
import unittest

ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT/'scripts'))
from core import Block,instant
from filing_archive import run


class ArchiveTests(unittest.TestCase):
    now=instant('2026-11-11T10:00:00+05:30')
    def report(self):
        return {'schema':'india_news_v1','records':[{'id':str(k),'url':f'https://nsearchives.nseindia.com/corporate/fixture{k}.xml',
                                                  'published_at':self.now.isoformat()} for k in range(3)]}

    def collector(self,report,limit,clock):
        row=report['records'][0]
        return {'attempted':1,'filings':[{'url':row['url'],'news_id':row['id'],'status':'PARSED_FINANCIALS','sha256':'synthetic'}]}

    def test_batches_continue_and_duplicate_discovery_does_not_repeat(self):
        with tempfile.TemporaryDirectory() as directory:
            db=Path(directory)/'archive.sqlite'
            first=run(self.report(),db,limit=1,collector=self.collector,clock=lambda:self.now)
            second=run(self.report(),db,limit=1,collector=self.collector,clock=lambda:self.now)
            third=run(self.report(),db,limit=20,collector=self.collector,clock=lambda:self.now)
            fourth=run(self.report(),db,collector=self.collector,clock=lambda:self.now)
            self.assertEqual([r['deferred'] for r in (first,second,third,fourth)],[2,1,0,0])
            self.assertEqual(second['discovered_new'],0);self.assertEqual(fourth['attempted'],0)
            self.assertNotEqual(first['filings'][0]['news_id'],second['filings'][0]['news_id'])

    def test_failed_items_require_explicit_retry(self):
        def failed(report,limit,clock):
            result=self.collector(report,limit,clock);result['filings'][0]['status']='FAILED';return result
        with tempfile.TemporaryDirectory() as directory:
            db=Path(directory)/'archive.sqlite';report=self.report();report['records']=report['records'][:1]
            first=run(report,db,collector=failed,clock=lambda:self.now)
            second=run(report,db,collector=self.collector,clock=lambda:self.now)
            third=run(report,db,retry_failed=True,collector=self.collector,clock=lambda:self.now)
            self.assertEqual(first['backlog_counts']['FAILED'],1);self.assertEqual(second['attempted'],0)
            self.assertEqual(third['backlog_counts']['PARSED_FINANCIALS'],1)

    def test_foreign_result_cannot_complete_a_queue_item(self):
        def wrong(report,limit,clock):
            r=self.collector(report,limit,clock);r['filings'][0]['url']='https://example.com/other.xml';return r
        with tempfile.TemporaryDirectory() as directory:
            db=Path(directory)/'archive.sqlite'
            with self.assertRaises(Block):run(self.report(),db,collector=wrong,clock=lambda:self.now)
            self.assertEqual(run(self.report(),db,limit=1,collector=self.collector,clock=lambda:self.now)['deferred'],2)
