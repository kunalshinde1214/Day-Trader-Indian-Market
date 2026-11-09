"""Offline contracts for public feeds and research; never contacts a broker."""
import contextlib
import copy
from datetime import timedelta
import io
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/"scripts"))
from core import Block, instant, plan_from_hit, setup_hit
from analysis_rules import entry, exits, prefix, screen, check_policy
from public_market import chart, collect_chart, universe, public_entry, public_plan, public_replay, collect_daily, main, request
from research import news, unseen, xbrl, attachment, document
from demo import inputs

NOW=instant("2026-11-09T09:35:00+05:30")


def bars(interval=5,count=4,day=NOW):
    start=day.replace(hour=9,minute=15,second=0,microsecond=0)
    return [{"at":(start+timedelta(minutes=interval*k)).isoformat(),"open":100,"high":103 if k==count-1 else 101,"low":99,"close":102 if k==count-1 else 100,"volume":1000} for k in range(count)]


def payload(rows=None,selected="RELIANCE.NS",interval="5m"):
    rows=rows or bars()
    return {"chart":{"error":None,"result":[{"meta":{"symbol":selected,"currency":"INR","exchangeTimezoneName":"Asia/Kolkata","dataGranularity":interval},"timestamp":[int(instant(b["at"]).timestamp()) for b in rows],"indicators":{"quote":[{k:[b[k] for b in rows] for k in ("open","high","low","close","volume")}]}}]}}


class PublicTests(unittest.TestCase):
    def history(self,interval=5):return chart(payload(bars(interval,20 if interval==1 else 4),interval=str(interval)+"m"),"RELIANCE.NS",str(interval)+"m",NOW)

    def test_chart_identity_and_timestamp(self):
        h=self.history();self.assertEqual(len(h['bars']),4);self.assertFalse(h['execution_eligible'])
        self.assertEqual(h['last_bar_age_seconds'],300)

    def test_wrong_chart_identity_currency_timezone_interval(self):
        for key,value in [('symbol','OTHER.NS'),('currency','USD'),('exchangeTimezoneName','UTC'),('dataGranularity','1m')]:
            p=payload();p['chart']['result'][0]['meta'][key]=value
            with self.subTest(key=key),self.assertRaises(Block):chart(p,'RELIANCE.NS','5m',NOW)

    def test_missing_observation_remains_visible(self):
        p=payload();p['chart']['result'][0]['indicators']['quote'][0]['close'][1]=None
        h=chart(p,'RELIANCE.NS','5m',NOW);self.assertEqual(len(h['missing_bars']),1)
        with self.assertRaises(Block):public_entry(h,NOW)

    def test_index_null_volume_is_not_equity_volume(self):
        p=payload(selected='^NSEI');p['chart']['result'][0]['indicators']['quote'][0]['volume']=[None]*4
        self.assertEqual(len(chart(p,'^NSEI','5m',NOW)['bars']),4)
        with self.assertRaises(Block):public_entry(chart(p,'^NSEI','5m',NOW),NOW)

    def test_duplicate_and_future_timestamps(self):
        for stamp in (int(NOW.timestamp())+1,int(instant(bars()[0]['at']).timestamp())):
            p=payload();p['chart']['result'][0]['timestamp'][-1]=stamp
            with self.assertRaises(Block):chart(p,'RELIANCE.NS','5m',NOW)

    def test_invalid_ohlc_and_array_alignment(self):
        for mutation in ('range','array'):
            p=payload();q=p['chart']['result'][0]['indicators']['quote'][0]
            if mutation=='range':q['low'][0]=200
            else:q['open'].pop()
            with self.assertRaises(Block):chart(p,'RELIANCE.NS','5m',NOW)

    def test_collection_clock_is_after_download(self):
        order=[]
        h=collect_chart('RELIANCE.NS','5m',fetcher=lambda url:(order.append('fetch') or json.dumps(payload()).encode()),clock=lambda:(order.append('clock') or NOW))
        self.assertEqual(order,['fetch','clock']);self.assertEqual(len(h['raw_sha256']),64)

    def test_network_allowlist_rejects_before_http(self):
        for url in ('http://query1.finance.yahoo.com/a','https://evil.example/a','https://u:p@query1.finance.yahoo.com/a','https://query1.finance.yahoo.com:444/a'):
            with self.assertRaises(Block):request(url)

    def test_nse_universe_filters_series(self):
        raw=b'SYMBOL,NAME OF COMPANY,SERIES,ISIN NUMBER\nABC,ABC Ltd,EQ,INE123\nOTHER,Other,BE,INE456\n'
        u=universe(raw,NOW);self.assertEqual([r['symbol'] for r in u['members']],['ABC.NS']);self.assertFalse(u['effective_date_verified'])
        with self.assertRaises(Block):universe(raw+b'ABC,ABC Ltd,EQ,INE123\n',NOW)

    def test_public_orb_and_one_minute_level(self):
        h=public_entry(self.history(),NOW);self.assertEqual(h['state'],'triggered')
        h=public_entry(self.history(1),NOW,setup='level',level=101)
        self.assertEqual(h['interval_minutes'],1);self.assertEqual(h['trigger_at'],NOW.isoformat())

    def test_stale_collection_blocked(self):
        h=self.history();h['as_of']=(NOW-timedelta(seconds=121)).isoformat()
        with self.assertRaises(Block):public_entry(h,NOW)

    def test_forming_bar_not_entry(self):
        self.assertEqual(entry(bars(),NOW-timedelta(seconds=1))['state'],'waiting')

    def test_early_level_never_uses_later_range(self):
        rows=bars(1,20);rows[0].update(high=103,close=102);rows[14]['high']=500
        h=entry(rows,NOW,setup='level',level=101,interval=1,not_before='09:16')
        self.assertEqual(h['range_high'],103);self.assertEqual(h['trigger_at'],'2026-11-09T09:16:00+05:30')

    def test_missing_prefix_and_duplicate_blocked(self):
        for rows in (bars()[1:],bars()+bars()[:1]):
            with self.assertRaises(Block):prefix(rows,NOW)

    def test_public_plan_stop_and_target_modes(self):
        h=public_entry(self.history(),NOW)
        for stop in ('or-mid','or-low','price:99','pct:1','atr:1'):
            p=public_plan(h,stop=stop,target='price:110');self.assertLess(p['stop'],p['entry']);self.assertEqual(p['target'],110);self.assertFalse(p['execution_eligible'])

    def test_sell_geometry_and_invalid_rules(self):
        h={'side':'SELL','entry':98,'range_high':101,'range_low':99}
        p=exits(h,.05,stop='or-low',target='2R');self.assertGreater(p['stop'],p['entry']);self.assertLess(p['target'],p['entry'])
        for stop,target in [('price:97','2R'),('pct:100','2R'),('pct:1','price:101'),('nonsense','2R'),('pct:1','0R')]:
            with self.assertRaises(Block):exits(h,.05,stop=stop,target=target)

    def test_atr_ignores_later_bars(self):
        rows=bars(1,20);rows[14].update(close=102,high=103)
        hit=entry(rows,NOW,setup='level',level=101,interval=1);one=exits(hit,.01,stop='atr:1',rows=rows)
        rows[-1]['high']=10000
        self.assertEqual(one,exits(hit,.01,stop='atr:1',rows=rows))

    def test_multitag_and_exclusion_of_today(self):
        rows=[]
        for k in range(40):
            price=100-k if k<39 else 100
            rows.append({'at':(NOW-timedelta(days=40-k)).isoformat(),'open':price,'high':price+1,'low':price-1,'close':price,'volume':100})
        r=screen(rows,NOW);self.assertIn('reclaim_ma20',r['setups']);self.assertIn('oversold_bounce',r['setups'])
        self.assertEqual(r,screen(rows+bars(),NOW))

    def test_exit_policy_change_requires_replan(self):
        check_policy({}, {'target_r':2})
        with self.assertRaises(Block):check_policy({}, {'target_r':2,'exit_stop':'pct:1'})
        with self.assertRaises(Block):check_policy({'exit_policy':{'stop':'pct:1','target':'2R'}},{'target_r':2,'exit_stop':'pct:2'})

    def test_broker_plan_exit_policy_is_recorded(self):
        c=json.loads((ROOT/'assets/config.example.json').read_text());c.update(exit_stop='pct:1',exit_target='3R')
        now,i,ctx,h,u,candles=inputs(c);rows=candles['instruments']['NSE:DEMO-EQ']['bars']
        hit=setup_hit(rows,now,c,i);p=plan_from_hit(hit,i,c,now,rows=rows)
        self.assertEqual(p['exit_policy'],{'stop':'pct:1','target':'3R'});check_policy(p,c)

    def test_public_replay_prior_sessions_and_seed(self):
        h=self.history();p=public_plan(public_entry(h,NOW));old=[]
        for k in range(1,6):old+=bars(5,75,NOW-timedelta(days=k))
        h['bars']=old+bars();bundle={'plan':p,'history':h}
        r=public_replay(bundle,paths=10);self.assertEqual(sum(r['outcomes'].values()),10);self.assertEqual(r,public_replay(bundle,paths=10))
        h['bars']=old[1:]
        with self.assertRaises(Block):public_replay(bundle,paths=10)

    def test_network_clock_override_rejected_even_with_input(self):
        for command in ('scan','chart','universe','watch'):
            with contextlib.redirect_stdout(io.StringIO()),patch('public_market.request',side_effect=AssertionError('network')):
                self.assertEqual(main([command,'--input','unused','--now',NOW.isoformat()]),2)

    def daily_fetcher(self,now=NOW,broken=None):
        from urllib.parse import urlsplit,unquote,parse_qs
        def fetch(url):
            selected=unquote(urlsplit(url).path.rsplit('/',1)[-1]);interval=parse_qs(urlsplit(url).query)['interval'][0]
            if selected==broken:raise Block('source unavailable')
            rows=bars() if interval=='5m' else [{**bars()[0],'at':(now-timedelta(days=k)).replace(hour=9,minute=15).isoformat()} for k in range(40,0,-1)]
            p=payload(rows,selected,interval);p['chart']['result'][0]['meta']['currentTradingPeriod']={'regular':{'start':int(now.replace(hour=9,minute=15).timestamp()),'end':int(now.replace(hour=15,minute=30).timestamp())}}
            return json.dumps(p).encode()
        return fetch

    def test_all_public_daily_commands_have_visible_coverage_gaps(self):
        from daily import report
        b=collect_daily(['nifty','sensex','banknifty'],fetcher=self.daily_fetcher(),clock=lambda:NOW)
        for command in ('india-daily','nifty','sensex','banknifty'):
            r=report(command,b,NOW);self.assertEqual(r['status'],'PARTIAL');self.assertEqual(r['coverage']['blocked'],0);self.assertFalse(r['submitted'])
            self.assertTrue(any('calendar' in g.lower() for g in r['coverage_gaps']))

    def test_public_daily_partial_source_failure_preserved(self):
        from daily import report
        b=collect_daily(['nifty','sensex'],fetcher=self.daily_fetcher(broken='^BSESN'),clock=lambda:NOW)
        r=report('india-daily',b,NOW);self.assertEqual(r['status'],'PARTIAL');self.assertEqual(r['coverage']['available'],1)

    def test_public_daily_preopen_and_weekend(self):
        from daily import report
        early=NOW.replace(hour=8)
        b=collect_daily(['nifty'],fetcher=self.daily_fetcher(early),clock=lambda:early)
        self.assertEqual(report('nifty',b,early)['phase'],'preopen')
        weekend=NOW-timedelta(days=1)
        with self.assertRaises(Block):collect_daily(['nifty'],fetcher=self.daily_fetcher(weekend),clock=lambda:weekend)

    def test_public_entry_plan_cli_round_trip_and_no_overwrite(self):
        with tempfile.TemporaryDirectory() as temp:
            h,e,p=[Path(temp)/name for name in ('history.json','entry.json','plan.json')];h.write_text(json.dumps(self.history()))
            with contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(main(['entry','--input',str(h),'--now',NOW.isoformat(),'--out',str(e)]),0)
                self.assertEqual(main(['plan','--input',str(e),'--stop','pct:1','--target','3R','--out',str(p)]),0)
                before=p.read_bytes();self.assertEqual(main(['plan','--input',str(e),'--out',str(p)]),2);self.assertEqual(p.read_bytes(),before)

    def test_broker_replay_recomputes_atr_from_each_trigger(self):
        from replay import replay
        c=json.loads((ROOT/'assets/config.example.json').read_text());c.update(exit_stop='atr:0.5',exit_target='2R',replay_paths=10)
        now,i,ctx,h,u,candles=inputs(c);rows=candles['instruments']['NSE:DEMO-EQ']['bars']
        p=plan_from_hit(setup_hit(rows,now,c,i),i,c,now,rows=rows)
        card=json.loads((ROOT/'assets/charges.example.json').read_text())
        r=replay(p,i,h,c,card);self.assertEqual(r['paths'],10);self.assertGreater(r['historical_same_side_firings'],0)
        c['exit_stop']='pct:1'
        with self.assertRaises(Block):replay(p,i,h,c,card)


def filing(facts=None):
    facts=facts or [('a','2025-04-01','2025-06-30','100'),('b','2026-04-01','2026-06-30','120')]
    contexts=''.join(f'<context id="{i}"><entity><identifier>TEST-CIN</identifier></entity><period><startDate>{s}</startDate><endDate>{e}</endDate></period></context>' for i,s,e,v in facts)
    values=''.join(f'<Revenue contextRef="{i}" unitRef="inr" decimals="-6">{v}</Revenue>' for i,s,e,v in facts)
    return f'<xbrl>{contexts}<unit id="inr"><measure>INR</measure></unit>{values}</xbrl>'.encode()


class ResearchTests(unittest.TestCase):
    def test_xbrl_yoy_and_decimals_not_scale(self):
        r=xbrl(filing(),'https://www.nseindia.com/test',NOW)
        self.assertEqual(r['year_over_year'][0]['change_pct'],'20.0');self.assertEqual(r['facts'][0]['value'],'100');self.assertFalse(r['point_in_time_backtest_eligible'])

    def test_xbrl_scale_and_negative_sign(self):
        body=filing().replace(b'decimals="-6"',b'decimals="-6" scale="6" sign="-"')
        r=xbrl(body,'https://www.nseindia.com/test',NOW);self.assertEqual(r['facts'][0]['value'],'-100000000');self.assertIsNone(r['year_over_year'][0]['change_pct'])

    def test_xbrl_ttm_only_four_contiguous_quarters(self):
        facts=[('a','2025-07-01','2025-09-30','100'),('b','2025-10-01','2025-12-31','110'),('c','2026-01-01','2026-03-31','120'),('d','2026-04-01','2026-06-30','130')]
        r=xbrl(filing(facts),'https://www.nseindia.com/test',NOW);self.assertEqual(r['trailing_twelve_months'][0]['value'],'460')
        r=xbrl(filing(facts[1:]),'https://www.nseindia.com/test',NOW);self.assertEqual(r['trailing_twelve_months'],[])

    def test_xbrl_dimensions_not_compared(self):
        body=filing().replace(b'<context id="b">',b'<context id="b"><scenario><explicitMember dimension="Basis">Consolidated</explicitMember></scenario>')
        self.assertEqual(xbrl(body,'https://www.nseindia.com/test',NOW)['year_over_year'],[])

    def test_xbrl_duplicate_conflicting_fact_blocks(self):
        body=filing().replace(b'</xbrl>',b'<Revenue contextRef="a" unitRef="inr">101</Revenue></xbrl>')
        with self.assertRaises(Block):xbrl(body,'https://www.nseindia.com/test',NOW)

    def test_xbrl_unsupported_unit_not_inferred(self):
        with self.assertRaises(Block):xbrl(filing().replace(b'<measure>INR',b'<measure>USD'),'https://www.nseindia.com/test',NOW)

    def test_xbrl_malformed_numbers_not_silently_repaired(self):
        for raw in (b'1,2',b'NaN',b'Infinity'):
            with self.assertRaises(Block):xbrl(filing([('a','2026-04-01','2026-06-30','100')]).replace(b'>100<',b'>'+raw+b'<'),'https://www.nseindia.com/test',NOW)

    def test_xbrl_future_period_and_external_entities_rejected(self):
        for body in (filing().replace(b'2026-06-30',b'2027-06-30'),b'<!DOCTYPE xbrl><xbrl/>'):
            with self.assertRaises(Block):xbrl(body,'https://www.nseindia.com/test',NOW)

    def test_attachment_host_restrictions(self):
        for url in ('http://www.nseindia.com/file','https://evil.example/file','https://www.nseindia.com:444/file'):
            with self.assertRaises(Block):attachment(url)

    def test_text_extraction_never_claims_verified_facts(self):
        r=document(b'Pledge disclosure quantity 123','https://www.nseindia.com/filing');self.assertFalse(r['numeric_facts_verified']);self.assertTrue(r['review_required'])

    def test_news_dedup_unions_symbols_and_keeps_discovery_uncertain(self):
        feed=b'<rss><channel><item><title>RELIANCE earnings profit</title><link>https://example.com/news</link><pubDate>Mon, 09 Nov 2026 03:30:00 GMT</pubDate></item></channel></rss>'
        r=news(['RELIANCE.NS','TCS.NS'],NOW,fetcher=lambda u:feed)
        self.assertEqual(len(r['records']),1);self.assertEqual(r['records'][0]['symbols'],['RELIANCE.NS','TCS.NS']);self.assertIsNone(r['records'][0]['quantity']);self.assertTrue(r['no_event_clearance'])

    def test_failed_news_is_not_empty_event_clearance(self):
        def fail(u):raise Block('Unavailable')
        r=news(['TCS.NS'],NOW,fetcher=fail);self.assertEqual(len(r['failed_sources']),3);self.assertTrue(r['no_event_clearance'])

    def test_news_persistent_dedup(self):
        r={'records':[{'id':'a','published_at':NOW.isoformat(),'headline':'test'}]}
        with tempfile.TemporaryDirectory() as temp:
            path=Path(temp)/'seen.db';self.assertEqual(len(unseen(r,path)['records']),1);self.assertEqual(unseen(r,path)['seen_filtered'],1);self.assertEqual(len(unseen(r,path,all_items=True)['records']),1)


if __name__=='__main__':unittest.main()
