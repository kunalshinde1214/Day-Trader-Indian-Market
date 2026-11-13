import copy
from datetime import timedelta
import json
from pathlib import Path
import sys
import unittest
from unittest.mock import Mock

ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT/'scripts'))
from core import Block, instant, size, setup_hit, plan_from_hit, check_order
from demo import inputs
from replay import replay
from liquidity import evidence, stress
from portfolio_margin import quote
from india_collect import collect, calendar_observation, status_observation, NSE_HOLIDAYS


class LiquidityTests(unittest.TestCase):
    def setUp(self):
        self.c=json.loads((ROOT/'assets/config.example.json').read_text());self.c['replay_paths']=10
        self.card=json.loads((ROOT/'assets/charges.example.json').read_text())
        self.now,self.i,self.ctx,h,u,b=inputs(self.c)
        self.p=plan_from_hit(setup_hit(b['instruments']['NSE:DEMO-EQ']['bars'],self.now,self.c),self.i,self.c,self.now)
        self.ctx['replay']=replay(self.p,self.i,h,self.c,self.card)
        self.ctx['liquidity']={**{k:self.i[k] for k in ('broker','symbol','exchange','token')},'source':'kite',
            'as_of':self.now.isoformat(),'quantity_unit':'units','traded_units':1000,
            'volume_window_start':(self.now-timedelta(minutes=1)).isoformat(),'volume_window_end':self.now.isoformat(),
            'asks':[{'price':102,'quantity':1000},{'price':102.05,'quantity':500}],
            'bids':[{'price':101.95,'quantity':20},{'price':101.9,'quantity':1000}]}
        self.policy={'mode':'depth_stress_v1','depth_fraction':0.5,'max_participation_fraction':0.1,'exit_shock_bps':100,'max_age_seconds':30}

    def test_depth_participation_cap_and_stress_recomputed(self):
        baseline=size(self.p,self.i,self.c,self.ctx,self.card,self.now)
        self.c['liquidity_model']=self.policy
        order=size(self.p,self.i,self.c,self.ctx,self.card,self.now)
        self.assertLessEqual(order['quantity'],100)
        self.assertLessEqual(order['quantity'],baseline['quantity'])
        self.assertGreater(order['metrics']['liquidity_stress']['slippage_reserve_inr'],0)
        self.assertEqual(check_order(order,self.i,self.c,self.ctx,self.card,self.now)['status'],'PASS')
        self.ctx['liquidity']['asks'][0]['quantity']=2
        with self.assertRaises(Block):check_order(order,self.i,self.c,self.ctx,self.card,self.now)

    def test_stale_crossed_foreign_and_missing_depth_block(self):
        self.c['liquidity_model']=self.policy
        for changes in ({'as_of':(self.now-timedelta(seconds=31)).isoformat()},{'token':'foreign'},{'quantity_unit':'lots'},
                        {'asks':[{'price':101.9,'quantity':100}]},{'bids':[]}):
            with self.subTest(changes=changes),self.assertRaises(Block):
                evidence(self.p,self.i,self.c,{**self.ctx,'liquidity':{**self.ctx['liquidity'],**changes}},self.now)

    def test_limit_without_executable_depth_blocks_size(self):
        self.c['liquidity_model']=self.policy
        self.ctx['liquidity']['asks']=[{'price':102.05,'quantity':10000}]
        with self.assertRaisesRegex(Block,'ZERO_SIZE'):size(self.p,self.i,self.c,self.ctx,self.card,self.now)

    def test_sell_shock_worsens_exit_and_finite_depth_bounds(self):
        self.c['liquidity_model']=self.policy
        p={**self.p,'side':'SELL','entry':101.9,'stop':104,'target':98}
        m=evidence(p,self.i,self.c,self.ctx,self.now)
        self.assertGreater(stress(p,m,20)['stress_exit_price'],104)
        with self.assertRaises(Block):stress(p,m,101)


class BasketTests(unittest.TestCase):
    def setUp(self):
        c=json.loads((ROOT/'assets/config.example.json').read_text())
        self.now,self.i,*_=inputs(c)
        self.bundle={'broker':'kite','account_id':c['account_id'],'legs':[
            {'instrument':self.i,'account_id':c['account_id'],'product':'CNC','side':'BUY','quantity':10,'price':100},
            {'instrument':{**self.i,'symbol':'SECOND','token':'999998'},'account_id':c['account_id'],'product':'CNC','side':'BUY','quantity':10,'price':100}]}
        self.account={'account_id':c['account_id'],'positions':[],'orders':[],'margins':{'cash':10000}}
        self.client=Mock();self.client.account.return_value=self.account
        def request(method,path,query,body):
            return {'initial':{'total':3000 if len(body)==1 else 2500},'final':{'total':2000 if len(body)==1 else 1000},
                    'orders':[{'exchange':x['exchange'],'tradingsymbol':x['tradingsymbol']} for x in body]}
        self.client.request.side_effect=request

    def test_all_prefixes_peak_retained_and_readonly(self):
        result=quote(self.client,self.bundle,clock=lambda:self.now)
        self.assertEqual(result['peak_estimate_inr'],3000);self.assertEqual(result['final_estimate_inr'],1000)
        self.assertEqual(self.client.request.call_count,2);self.assertFalse(result['execution_eligible'])
        self.assertTrue(all(c.args[1]=='/margins/basket' for c in self.client.request.call_args_list))

    def test_changed_account_and_wrong_response_block(self):
        self.client.account.side_effect=[self.account,{**self.account,'orders':[{'changed':True}]}]
        with self.assertRaises(Block):quote(self.client,self.bundle,clock=lambda:self.now)
        self.client.account.side_effect=None;self.client.request.side_effect=None
        self.client.request.return_value={'orders':[]}
        with self.assertRaises(Block):quote(self.client,self.bundle,clock=lambda:self.now)

    def test_cross_account_or_wrong_lot_rejected_before_reads(self):
        self.bundle['legs'][1]['account_id']='other'
        with self.assertRaises(Block):quote(self.client,self.bundle,clock=lambda:self.now)
        self.client.account.assert_not_called()


class CollectionTests(unittest.TestCase):
    now=instant('2026-11-11T10:00:00+05:30')
    def holidays(self):
        return json.dumps({key:[{'tradingDate':'08-Nov-2026','description':'Diwali Laxmi Pujan*'},
                                {'tradingDate':'10-Nov-2026','description':'Diwali Balipratipada'}] for key in ('CM','FO')}).encode()

    def test_holiday_list_does_not_prove_open_session(self):
        r=calendar_observation(self.holidays(),self.now.date(),self.now)
        self.assertEqual(r['segments']['CM']['status'],'NOT_LISTED_AS_HOLIDAY');self.assertFalse(r['execution_eligible'])
        d=self.now.date()-timedelta(days=3)
        self.assertEqual(calendar_observation(self.holidays(),d,self.now)['segments']['CM']['status'],'SPECIAL_SESSION_UNRESOLVED')
        with self.assertRaises(Block):calendar_observation(self.holidays(),self.now.date().replace(year=2027),self.now)

    def test_status_source_timestamp_must_be_current(self):
        raw=json.dumps({'marketState':[{'market':'Capital Market','marketStatus':'Open','tradeDate':'11-Nov-2026 09:59'}]}).encode()
        self.assertEqual(status_observation(raw,self.now)['market_status'],'Open')
        with self.assertRaises(Block):status_observation(raw,self.now+timedelta(minutes=4))

    def test_partial_failure_keeps_other_components_and_no_clearance(self):
        def fetch(url):
            if url==NSE_HOLIDAYS:return self.holidays()
            raise OSError('fixture failure')
        news=Mock(return_value={'schema':'india_news_v1','records':[],'sources':[{'source':'nse-announcements','status':'OK'}]})
        filings=Mock(return_value={'filings':[],'attempted':0,'deferred':0})
        result=collect(['RELIANCE.NS'],sources=['nse-announcements'],fetcher=fetch,
                       charts=Mock(return_value={'source':'synthetic chart'}),news=news,filings=filings,clock=lambda:self.now)
        self.assertIn('calendar',result);self.assertIn('RELIANCE.NS',result['charts'])
        self.assertFalse(result['full_market_coverage']);self.assertEqual(result['status'],'PARTIAL')
        self.assertTrue(any(c['status']=='FAILED' for c in result['coverage']))


class SessionTests(unittest.TestCase):
    def test_instrument_cas_and_broker_cutoffs_are_distinct(self):
        from session_policy import validate
        now=instant('2026-11-11T10:00:00+05:30')
        i={'broker':'kite','exchange':'NSE','symbol':'DEMO','token':'123'}
        c={'account_id':'demo','flat_by':'15:05','snapshot_max_age_seconds':60}
        e={**i,'account_id':'demo','date':'2026-11-11','as_of':now.isoformat(),
           'source':'https://www.nseindia.com/market-data/closing-auction-session',
           'is_open_day':True,'halted':False,'continuous_open':'09:15','continuous_close':'15:15',
           'broker_squareoff':'15:10','auction_included':False}
        self.assertTrue(validate(e,i,c,now))
        for changes in ({'continuous_close':'15:00'},{'auction_included':True},{'halted':None},{'token':'other'},
                        {'broker_squareoff':'15:05'},{'source':'https://example.com/forged'}):
            with self.subTest(changes=changes),self.assertRaises(Block):validate({**e,**changes},i,c,now)
