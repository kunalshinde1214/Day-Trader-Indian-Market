import sys
from pathlib import Path
import unittest
ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT/'scripts'))
from filing_workflow import match,collect,eligible_url
from core import instant,Block
from test_public_workflows import filing
NOW=instant('2026-11-10T10:00:00+05:30')
def report(title='Example Company Limited'):
    return {'schema':'india_news_v1','records':[{'id':'one','headline':title,'url':'https://nsearchives.nseindia.com/corporate/test.xml','published_at':NOW.isoformat()}]}
def universe():return {'source':'https://example.com/master','members':[{'symbol':'EXAMPLE.NS','name':'Example Company Limited','isin':'INE000000001'}]}
class FilingTests(unittest.TestCase):
    def test_exact_name_match_keeps_identity_unverified(self):
        r=match(report(),universe())['records'][0]
        self.assertEqual(r['issuer_match_status'],'unique_candidate');self.assertFalse(r['issuer_verified'])
    def test_whole_symbol_not_substring(self):
        self.assertEqual(match(report('ANEXAMPLE'),universe())['records'][0]['issuer_match_status'],'unmatched')
    def test_ambiguity_retained(self):
        u=universe();u['members'].append({'symbol':'OTHER.NS','name':'Example Company Limited','isin':'INE000000002'})
        self.assertEqual(match(report(),u)['records'][0]['issuer_match_status'],'ambiguous')
    def test_duplicate_symbol_rejected(self):
        u=universe();u['members']*=2
        with self.assertRaises(Block):match(report(),u)
    def test_exact_symbol_match(self):
        self.assertEqual(match(report('EXAMPLE results'),universe())['records'][0]['issuer_match_status'],'unique_candidate')
    def test_direct_link_allowlist(self):
        for url in ['http://nsearchives.nseindia.com/a.xml','https://evil.com/a.xml','https://nsearchives.nseindia.com/page','https://u:p@nsearchives.nseindia.com/a.pdf']:
            self.assertFalse(eligible_url(url))
    def test_financial_attachment_pipeline(self):
        r=collect(report(),fetcher=lambda u:filing(),clock=lambda:NOW)
        self.assertEqual(r['filings'][0]['status'],'PARSED_FINANCIALS');self.assertEqual(r['filings'][0]['analysis']['year_over_year'][0]['change_pct'],'20.0')
    def test_unsupported_xml_is_review_only(self):
        r=collect(report(),fetcher=lambda u:b'<announcement>Board meeting</announcement>',clock=lambda:NOW)
        self.assertEqual(r['filings'][0]['status'],'TEXT_REVIEW_REQUIRED');self.assertFalse(r['filings'][0]['extraction']['numeric_facts_verified'])
    def test_failed_download_stays_failure(self):
        def fail(u):raise OSError('test')
        self.assertEqual(collect(report(),fetcher=fail,clock=lambda:NOW)['filings'][0]['status'],'FAILED')
    def test_bounded_download_and_duplicates(self):
        r=report();r['records']*=2;r['records'].append({**r['records'][0],'url':'https://nsearchives.nseindia.com/corporate/two.xml'})
        out=collect(r,1,fetcher=lambda u:filing(),clock=lambda:NOW)
        self.assertEqual((out['discovered'],out['attempted'],out['deferred']),(2,1,1))
    def test_entity_xml_rejected(self):
        out=collect(report(),fetcher=lambda u:b'<!DOCTYPE x><x/>',clock=lambda:NOW)
        self.assertEqual(out['filings'][0]['status'],'FAILED')
