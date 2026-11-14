"""User-owned Upstox carry execution contracts, using fictional offline fixtures."""
import copy
import io
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import Mock,patch
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'scripts'))
from core import Block
from portfolio_exit import prepare,main
from execution import ExecutionStore,execute
from live_broker import wire_payload
import test_upstox_portfolio as fixtures


class UpstoxExecutionTests(unittest.TestCase):
    def setUp(self):
        self.f=fixtures.UpstoxPortfolioTests();self.f.setUp();self.now=fixtures.NOW
        self.f.i['freeze_quantity']=1000
        self.f.opening['source']='reviewed_statement'
        self.f.opening['positions'][0].update(cost_source='statement',mark_source='previous_close')
        self.c={'broker':'upstox','account_id':'SYNTHETIC','flat_by':'15:05','snapshot_max_age_seconds':30}
        self.p={**self.c,'enabled':True,'approval_mode':'human_terminal_two_stage','mixed_inventory_reducing_exits':True,
                'mixed_inventory_acceptance':{**self.c,'status':'PASS','source':'offline test','as_of':self.now.isoformat()}}

    def evidence(self):
        identity={k:self.f.i[k] for k in ('broker','exchange','symbol','token')}
        common={**identity,'account_id':self.c['account_id'],'date':self.now.date().isoformat(),'as_of':self.now.isoformat()}
        delivery={**common,'source':'offline test','status':'AUTHORIZED','method':'CDSL_EDIS','authorized_quantity':10}
        session={**common,'source':'https://www.nseindia.com/static/market-data/market-timings','continuous_open':'09:15',
                 'continuous_close':'15:30','broker_squareoff':'15:20','is_open_day':True,'halted':False,'auction_included':False}
        return delivery,session

    def review(self,**kwargs):
        delivery,session=self.evidence()
        args={'product':'CNC','quantity':4,'price':120,'delivery':delivery,'session':session}
        args.update(kwargs)
        return prepare(self.f.run_case(),[self.f.i],self.f.i,self.c,self.p,self.now,**args)

    def test_delivery_exit_maps_to_d_and_keeps_canonical_identity(self):
        review,_=self.review()
        self.assertEqual(review['payload']['product'],'D')
        self.assertEqual(review['action']['product'],'CNC')
        self.assertFalse(review['payload']['slice'])
        self.assertEqual(review['payload']['instrument_token'],self.f.i['token'])

    def test_derivative_short_uses_whole_lot_d_buy(self):
        self.f.derivative();self.f.position(overnight=-10)
        review,_=self.review(product='NRML',quantity=5)
        self.assertEqual(review['payload']['product'],'D');self.assertEqual(review['action']['side'],'BUY')
        with self.assertRaises(Block):self.review(product='NRML',quantity=3)
        with self.assertRaises(Block):self.review(product='NRML',quantity=15)

    def test_intraday_and_delivery_same_symbol_are_separate(self):
        self.f.trade(side='BUY',q=5,product='I');self.f.position(buys=5,product='I')
        self.assertEqual(self.review()[0]['payload']['product'],'D')
        self.assertEqual(self.review(product='MIS',quantity=5)[0]['payload']['product'],'I')
        with self.assertRaises(Block):self.review(product='MIS',quantity=6)

    def test_partial_sale_and_no_double_count(self):
        self.f.trade();self.f.position(sells=4);self.f.raw['holdings'][0]['cnc_used_quantity']=4
        self.assertEqual(self.review(quantity=6)[0]['action']['quantity'],6)
        with self.assertRaises(Block):self.review(quantity=7)

    def test_working_order_requires_cancel_then_terminal_refresh(self):
        self.f.trade();self.f.raw['orders'][0].update(status='open',filled_quantity=0);self.f.raw['trades']=[]
        self.f.raw['holdings'][0]['cnc_used_quantity']=4
        with self.assertRaises(Block):self.review()
        review,_=self.review(kind='cancel',order_id='o1',session=None)
        self.assertEqual(review['action']['product'],'CNC');self.assertEqual(review['payload'],{'order_id':'o1'})
        self.f.raw['orders'][0]['status']='cancelled';self.f.raw['holdings'][0]['cnc_used_quantity']=0
        self.assertEqual(self.review()[0]['action']['quantity'],4)

    def test_session_is_required_to_prevent_implicit_amo(self):
        with self.assertRaises(Block):self.review(session=None)
        _,session=self.evidence();session['continuous_close']='09:59'
        with self.assertRaises(Block):self.review(session=session)

    def test_stop_requires_exact_fresh_quote_and_delivery(self):
        quote={**{k:self.f.i[k] for k in ('exchange','symbol','token')},'source':'upstox','price':130,'as_of':self.now.isoformat()}
        review,_=self.review(kind='stop',trigger=125,quote=quote)
        self.assertEqual(review['payload']['order_type'],'SL');self.assertEqual(review['payload']['product'],'D')
        with self.assertRaises(Block):self.review(kind='stop',trigger=125,quote=quote,delivery=None)
        with self.assertRaises(Block):self.review(kind='stop',trigger=125,quote={**quote,'price':125})

    def test_normalized_order_details_bind_recovery_product(self):
        self.f.trade();self.f.position(sells=4);self.f.raw['holdings'][0]['cnc_used_quantity']=4
        collection=self.f.run_case();row=collection['execution_orders'][0]
        action=self.review()[0]['action'];action.update(quantity=4,price=115)
        self.assertTrue(ExecutionStore.matches(action,row))
        self.assertFalse(ExecutionStore.matches(action,{**row,'product':'MIS'}))

    def test_disabled_foreign_or_unaccepted_account_blocks(self):
        self.p['enabled']=False
        with self.assertRaises(Block):self.review()
        self.p['enabled']=True;self.p['mixed_inventory_acceptance']['account_id']='other'
        with self.assertRaises(Block):self.review()

    def test_carry_payload_cannot_be_new_entry(self):
        action=self.review()[0]['action']
        with self.assertRaises(Block):wire_payload({**action,'kind':'entry'})

    def test_cli_repeats_collector_before_fake_submission(self):
        collection=self.f.run_case();delivery,session=self.evidence()
        approval=Mock();approval.approve.side_effect=lambda review:review['review_hash']
        transport=Mock();transport.send.return_value={'order_id':'new-1'}
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory)
            for name,body in {'config':self.c,'instrument':self.f.i,'live-profile':self.p,'opening':self.f.opening,
                              'master':{'instruments':[self.f.i]},'delivery':delivery,'session':session}.items():
                (root/(name+'.json')).write_text(json.dumps(body))
            args=['exit','--workdir',directory,'--opening',str(root/'opening.json'),'--master',str(root/'master.json'),
                  '--product','CNC','--quantity','4','--price','120','--delivery-authorization',str(root/'delivery.json'),'--session',str(root/'session.json')]
            with patch('upstox_portfolio.fetch',return_value=collection) as fetch,patch('broker.adapter'), \
                 patch('portfolio_exit.instant',return_value=self.now),patch('portfolio_exit.LiveBroker',return_value=transport), \
                 patch('portfolio_exit.TerminalApproval',return_value=approval),patch('sys.stdout',new_callable=io.StringIO), \
                 patch('portfolio_exit.execute',side_effect=lambda *args:execute(*args,clock=lambda:self.now)):
                self.assertEqual(main(args),0)
            self.assertEqual(fetch.call_count,2);transport.send.assert_called_once()


if __name__=='__main__':unittest.main()
