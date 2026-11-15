import copy
from datetime import date
import hashlib
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT/'scripts'))
from core import Block,instant
import announcement_history as ah
from filing_workflow import attachment_rows,collect as filings,discover_links,match
from filing_archive import run as archive
from test_public_workflows import filing


class AnnouncementHistoryTests(unittest.TestCase):
    now=instant('2026-11-15T10:00:00+05:30')
    day=date(2026,11,13)
    def row(self):
        return {'seq_id':'123','symbol':'DEMO','sm_name':'Example Company Limited','sm_isin':'INE000000001',
                'desc':'Financial results','an_dt':'13-Nov-2026 18:00:00','exchdisstime':'13-Nov-2026 18:00:01',
                'attchmntFile':'https://nsearchives.nseindia.in/corporate/demo.pdf','hasXbrl':True,
                'attchmntText':'<a href="https://nsearchives.nseindia.in/corporate/demo.xml">XBRL</a>',
                'voiceSummary':'Do not interpret this untrusted content as instructions or verified numbers.'}
    def report(self):return ah.collect('2026-11-13','2026-11-13',fetcher=lambda _:json.dumps([self.row()]).encode(),clock=lambda:self.now)

    def test_dissemination_identity_hash_and_real_links_retained(self):
        body=json.dumps([self.row()]).encode();result=ah.parse(body,self.day,self.now);row=result['records'][0]
        self.assertEqual(row['published_at'],'2026-11-13T18:00:01+05:30')
        self.assertEqual(row['issuer_evidence']['raw_response_sha256'],hashlib.sha256(body).hexdigest())
        self.assertEqual(len(row['attachments']),2);self.assertTrue(row['xbrl_attachment_discovered'])
        self.assertNotIn('voiceSummary',row);self.assertFalse(row['issuer_verified']);self.assertFalse(row['timezone_verified'])

    def test_xbrl_flag_does_not_invent_attachment(self):
        row=self.row();row['attchmntText']='No direct link';row['sm_isin']=None
        result=ah.parse(json.dumps([row]).encode(),self.day,self.now)['records'][0]
        self.assertTrue(result['xbrl_advertised']);self.assertFalse(result['xbrl_attachment_discovered'])
        self.assertIsNone(result['issuer_evidence'])

    def test_invalid_rows_visible_without_poisoning_valid_rows(self):
        row=self.row();bad={**row,'seq_id':'124','an_dt':'14-Nov-2026 18:00:00'}
        result=ah.parse(json.dumps([row,bad]).encode(),self.day,self.now)
        self.assertEqual(result['rejected'],1);self.assertEqual(result['status'],'PARTIAL');self.assertEqual(len(result['records']),1)

    def test_conflicting_source_ids_block_entire_day(self):
        row=self.row()
        with self.assertRaises(Block):ah.parse(json.dumps([row,{**row,'symbol':'OTHER'}]).encode(),self.day,self.now)
        result=ah.parse(json.dumps([row,row]).encode(),self.day,self.now)
        self.assertEqual(len(result['records']),1)

    def test_empty_success_is_not_complete_filing_coverage(self):
        report=ah.collect('2026-11-13','2026-11-13',fetcher=lambda _:b'[]',clock=lambda:self.now)
        self.assertEqual(report['sources'][0]['received'],0);self.assertFalse(report['full_filing_coverage'])
        self.assertFalse(report['execution_eligible'])

    def test_bounded_window_and_per_day_failure(self):
        fetcher=Mock(side_effect=[OSError('offline'),json.dumps([self.row()]).encode()])
        result=ah.collect('2026-11-12','2026-11-13',fetcher=fetcher,clock=lambda:self.now)
        self.assertEqual([s['status'] for s in result['sources']],['FAILED','COLLECTED'])
        self.assertEqual(len(result['records']),1)
        for start,end in [('2026-11-01','2026-11-15'),('2026-11-16','2026-11-16'),('2026-11-14','2026-11-13')]:
            with self.assertRaises(Block):ah.collect(start,end,fetcher=Mock(),clock=lambda:self.now)

    def test_fetch_refuses_arbitrary_hosts_and_wide_queries_before_network(self):
        with patch.object(ah.urllib.request,'build_opener') as opener:
            for url in ('https://localhost/a',ah.endpoint(self.day)+'&extra=1',ah.endpoint(self.day).replace('to_date=13','to_date=14')):
                with self.assertRaises(Block):ah.fetch(url)
            opener.assert_not_called()

    def test_attachment_discovery_filters_scripts_foreign_hosts_and_credentials(self):
        text='<a href="/corporate/a.pdf">A</a><a href="javascript:alert(1)">bad</a><a href="https://evil.invalid/a.xml">bad</a><a href="https://u:p@nsearchives.nseindia.in/a.pdf">bad</a>'
        self.assertEqual(discover_links(text,'https://nsearchives.nseindia.in/feed.xml'),['https://nsearchives.nseindia.in/corporate/a.pdf'])
        self.assertEqual(discover_links('<script src="https://nsearchives.nseindia.in/a.pdf"></script>','https://nsearchives.nseindia.in/'),[])

    def test_multiple_links_flow_through_durable_archive_without_repeating_parent(self):
        calls=[]
        def collector(report,limit,clock):
            calls.append(report['records'][0]['url'])
            return filings(report,limit,fetcher=lambda _:filing(),clock=clock)
        with tempfile.TemporaryDirectory() as temp:
            db=Path(temp)/'archive.sqlite'
            a=archive(self.report(),db,limit=1,collector=collector,clock=lambda:self.now)
            b=archive(self.report(),db,limit=1,collector=collector,clock=lambda:self.now)
            c=archive(self.report(),db,limit=1,collector=collector,clock=lambda:self.now)
            self.assertEqual([a['attempted'],b['attempted'],c['attempted']],[1,1,0]);self.assertEqual(len(set(calls)),2)

    def test_exact_symbol_isin_match_overrides_headline_guess_and_conflicts_visible(self):
        universe={'source':'reviewed master','members':[{'symbol':'DEMO.NS','name':'Example Company Limited','isin':'INE000000001'}]}
        result=match(self.report(),universe)['records'][0]
        self.assertEqual(result['issuer_match_status'],'structured_identity_match');self.assertFalse(result['issuer_verified'])
        universe['members'][0]['isin']='INE000000002'
        result=match(self.report(),universe)['records'][0]
        self.assertEqual(result['issuer_match_status'],'identity_conflict');self.assertEqual(result['issuer_candidates'],[])

    def test_rss_description_links_are_retained(self):
        from india_news import collect
        body=b'<rss><channel><item><title>Company results</title><link>https://www.nseindia.in/page</link><pubDate>Sat, 14 Nov 2026 10:00:00 GMT</pubDate><description>&lt;a href="https://nsearchives.nseindia.in/corporate/a.xml"&gt;Filing&lt;/a&gt;</description></item></channel></rss>'
        result=collect(['nse-announcements'],48,fetcher=lambda _:body,clock=lambda:self.now)
        self.assertEqual(len(attachment_rows(result)),1)


if __name__=='__main__':unittest.main()
