import copy
import contextlib
from datetime import date
import io
import json
from pathlib import Path
import sys
import unittest
from unittest.mock import Mock,patch

ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT/'scripts'))
from core import Block,instant,fingerprint
import closing_context as cc


class ClosingContextTests(unittest.TestCase):
    now=instant('2026-11-15T10:00:00+05:30');day=date(2026,11,13)
    index=b'Index Name,Index Date,Open Index Value,High Index Value,Low Index Value,Closing Index Value,Points Change,Change(%)\nNifty 50,13-11-2026,100,120,90,110,10,10\nNifty Bank,13-11-2026,200,240,180,220,20,10\n'
    cash=b'SYMBOL, SERIES, DATE1, PREV_CLOSE, CLOSE_PRICE\nUP, EQ, 13-Nov-2026, 100, 110\nDOWN, EQ, 13-Nov-2026, 100, 90\nSAME, EQ, 13-Nov-2026, 100, 100\nTFT, BE, 13-Nov-2026, 10, 11\nBAD, EQ, 13-Nov-2026, 0, 10\n'
    def flow(self):return [{'category':'DII','date':'13-Nov-2026','buyValue':'20','sellValue':'30','netValue':'-10'},
                           {'category':'FII/FPI','date':'13-Nov-2026','buyValue':'40','sellValue':'20','netValue':'20'}]
    def gift(self):return {'giftnifty':{'SYMBOL':'NIFTY','INSTRUMENTTYPE':'FUTIDX','OPTIONTYPE':'-','STRIKEPRICE':0,
                                       'TIMESTMP':'14-Nov-2026 02:44','EXPIRYDATE':'23-Nov-2026','LASTPRICE':25000,'DAYCHANGE':10,'PERCHANGE':0.04}}
    def fetch(self,url):
        if url==cc.FLOWS:return json.dumps(self.flow()).encode()
        if url==cc.STATUS:return json.dumps(self.gift()).encode()
        return self.index if url.startswith(cc.INDEX_BASE) else self.cash
    def collect(self,**kw):return cc.collect('2026-11-13',fetcher=self.fetch,clock=lambda:self.now,**kw)

    def test_official_index_names_dates_and_change_arithmetic(self):
        result=cc.indices(self.index,self.day,self.now)
        self.assertEqual(result['indices']['nifty']['close'],110)
        self.assertEqual(result['indices']['nifty']['previous_close_implied_by_source_change'],100)
        for raw in (self.index.replace(b'13-11-2026',b'12-11-2026'),self.index.replace(b'110,10,10',b'110,10,20'),
                    self.index.replace(b'100,120,90',b'100,95,90')):
            with self.assertRaises(Block):cc.indices(raw,self.day,self.now)

    def test_cash_breadth_excludes_other_series_and_accounts_missing(self):
        report=cc.cash_prices(self.cash,self.day,self.now);result=cc.breadth(report['prices'])
        self.assertEqual([result[k] for k in ('observed','advancing','declining','unchanged')],[3,1,1,1])
        self.assertEqual(report['excluded_non_eq_rows'],1);self.assertEqual(report['unavailable_eq_symbols'],['BAD'])
        selected=cc.breadth(report['prices'],['UP','ABSENT']);self.assertEqual(selected['missing'],['ABSENT'])

    def test_cash_wrong_dates_duplicates_and_ragged_rows_block(self):
        for raw in (self.cash.replace(b'13-Nov',b'12-Nov'),self.cash+b'UP, EQ, 13-Nov-2026, 100, 110\n',self.cash+b'BROKEN\n'):
            with self.assertRaises(Block):cc.cash_prices(raw,self.day,self.now)

    def test_flows_convert_crores_without_inventing_release_time(self):
        report=cc.flows(json.dumps(self.flow()).encode(),self.now)
        self.assertEqual(report['values']['FII/FPI']['net_inr'],'200000000')
        self.assertIsNone(report['published_at']);self.assertFalse(report['finality_verified'])
        self.assertFalse(report['point_in_time_backtest_eligible'])

    def test_flow_duplicate_categories_bad_totals_and_dates_block(self):
        for changes in ({'category':'DII'},{'netValue':'19'},{'date':'12-Nov-2026'},{'date':'16-Nov-2026'}):
            rows=self.flow();rows[1].update(changes)
            with self.assertRaises(Block):cc.flows(json.dumps(rows).encode(),self.now)

    def test_gift_old_observation_is_explicitly_stale_not_spot(self):
        report=cc.gift(json.dumps(self.gift()).encode(),self.now)
        self.assertEqual(report['freshness'],'STALE');self.assertFalse(report['timezone_verified'])
        self.assertIn('not domestic spot',report['price_basis'])
        self.assertFalse(report['execution_eligible'])

    def test_gift_future_or_wrong_contract_blocks(self):
        for changes in ({'TIMESTMP':'16-Nov-2026 02:44'},{'SYMBOL':'OTHER'},{'OPTIONTYPE':'CE'}):
            data=self.gift();data['giftnifty'].update(changes)
            with self.assertRaises(Block):cc.gift(json.dumps(data).encode(),self.now)

    def test_sensex_is_unavailable_not_mapped_to_nifty(self):
        report=self.collect()
        self.assertEqual(report['indices']['sensex']['status'],'UNAVAILABLE')
        self.assertEqual(report['indices']['banknifty']['status'],'OBSERVED')
        self.assertFalse(report['full_market_coverage']);self.assertEqual(report['status'],'PARTIAL')

    def test_independent_source_failures_retained(self):
        def fetch(url):
            if url==cc.FLOWS:raise OSError('offline')
            return self.fetch(url)
        report=cc.collect('2026-11-13',fetcher=fetch,clock=lambda:self.now)
        self.assertNotIn('institutional_flows',report)
        self.assertEqual(report['indices']['nifty']['status'],'OBSERVED')
        self.assertTrue(any(r['component']=='institutional_flows' and r['status']=='FAILED' for r in report['coverage']))

    def test_membership_is_date_hash_bound_and_keeps_partial_verification(self):
        item={'schema':'reviewed_index_membership_v1','index_id':'nifty','membership_date':'2026-11-13',
              'as_of':'2026-11-13T10:00:00+05:30','membership_verified':False,'members':['UP','ABSENT'],
              'membership_basis':'operator-reviewed fixture'}
        item['membership_hash']=fingerprint(item)
        report=self.collect(membership={'nifty':item})
        self.assertEqual(report['indices']['nifty']['constituent_breadth']['missing'],['ABSENT'])
        self.assertFalse(report['indices']['nifty']['constituent_breadth']['membership_verified'])
        item['members']=['UP']
        report=self.collect(membership={'nifty':item})
        self.assertIsNone(report['indices']['nifty']['constituent_breadth'])

    def test_markdown_shows_dates_sources_and_staleness(self):
        text=cc.markdown(self.collect())
        self.assertIn('2026-11-13',text);self.assertIn('STALE',text);self.assertIn('release time and finality unverified',text)
        self.assertIn(cc.FLOWS,text);self.assertNotIn('unavailable%',text)

    def test_endpoint_allowlist_and_future_requests_make_no_calls(self):
        with patch.object(cc.urllib.request,'build_opener') as opener:
            for url in ('https://localhost/data',cc.FLOWS+'?extra=1',cc.CASH_BASE+'../data.csv'):
                with self.assertRaises(Block):cc.fetch(url)
            opener.assert_not_called()
        fetch=Mock()
        with self.assertRaises(Block):cc.collect('2026-11-16',fetcher=fetch,clock=lambda:self.now)
        fetch.assert_not_called()

    def test_daily_command_routes_to_closing_report_without_synthetic_candles(self):
        from daily import main
        with patch.object(cc,'collect',return_value=self.collect(selected=['nifty'])) as collector,contextlib.redirect_stdout(io.StringIO()) as output:
            self.assertEqual(main(['nifty','--collect-close','--session','2026-11-13','--format','json']),0)
            report=json.loads(output.getvalue());self.assertEqual(report['schema'],'india_closing_context_v1')
            self.assertNotIn('candles',report);self.assertEqual(collector.call_args.kwargs['selected'],['nifty'])
        with patch.object(cc,'collect') as collector,contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(main(['nifty','--collect-close','--session','2026-11-13','--now',self.now.isoformat()]),2)
            collector.assert_not_called()


if __name__=='__main__':unittest.main()
