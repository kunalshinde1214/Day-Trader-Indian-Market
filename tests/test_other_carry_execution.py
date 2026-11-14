"""Angel/FYERS carry execution journeys with simulated broker books only."""
import copy
import io
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'scripts'))
from core import Block
from execution import ExecutionStore, execute
from live_broker import LiveBroker, wire_payload
from portfolio_exit import prepare, main
import test_broker_portfolio as fixtures


class Cases:
    def setUp(self):
        self.f=(fixtures.AngelPortfolioTests if self.broker=='angel' else fixtures.FyersPortfolioTests)()
        self.f.setUp();self.now=fixtures.NOW
        self.f.i['freeze_quantity']=1000
        self.f.opening['source']='reviewed_statement'
        self.f.opening['positions'][0].update(cost_source='statement',mark_source='previous_close')
        self.c={'broker':self.broker,'account_id':'SYNTHETIC','flat_by':'15:05','snapshot_max_age_seconds':30}
        self.p={**self.c,'enabled':True,'approval_mode':'human_terminal_two_stage','mixed_inventory_reducing_exits':True,
                'mixed_inventory_acceptance':{**self.c,'status':'PASS','source':'offline fixture','as_of':self.now.isoformat()}}

    def evidence(self):
        common={**{k:self.f.i[k] for k in ('broker','exchange','symbol','token')},'account_id':self.c['account_id'],
                'date':self.now.date().isoformat(),'as_of':self.now.isoformat()}
        return ({**common,'source':'offline fixture','status':'AUTHORIZED','method':'CDSL_EDIS','authorized_quantity':10},
                {**common,'source':'https://www.nseindia.com/static/market-data/market-timings',
                 'continuous_open':'09:15','continuous_close':'15:30','broker_squareoff':'15:20',
                 'is_open_day':True,'halted':False,'auction_included':False})

    def review(self,**kwargs):
        delivery,session=self.evidence()
        args={'product':'CNC','quantity':4,'price':115,'delivery':delivery,'session':session};args.update(kwargs)
        return prepare(self.f.run_case(),[self.f.i],self.f.i,self.c,self.p,self.now,**args)

    def test_delivery_and_short_derivative_products(self):
        review,_=self.review();field='producttype' if self.broker=='angel' else 'productType'
        self.assertEqual(review['payload'][field],'DELIVERY' if self.broker=='angel' else 'CNC')
        self.f.derivative();self.f.position(overnight=-10)
        review,_=self.review(product='NRML',quantity=5)
        self.assertEqual(review['payload'][field],'CARRYFORWARD' if self.broker=='angel' else 'MARGIN')
        self.assertEqual(review['action']['side'],'BUY')
        with self.assertRaises(Block):self.review(product='NRML',quantity=3)
        with self.assertRaises(Block):self.review(product='NRML',quantity=15)

    def test_partial_sale_and_intraday_remain_separate(self):
        self.f.trade();self.f.position(sells=4)
        self.assertEqual(self.review(quantity=6)[0]['action']['quantity'],6)
        with self.assertRaises(Block):self.review(quantity=7)
        self.setUp();self.f.trade(side='BUY',q=5,product='INTRADAY');self.f.position(buys=5,product='INTRADAY')
        self.assertEqual(self.review()[0]['action']['product'],'CNC')
        self.assertEqual(self.review(product='MIS',quantity=5)[0]['action']['side'],'SELL')

    def test_stop_limits_and_short_cover_stops(self):
        quote={**{k:self.f.i[k] for k in ('exchange','symbol','token')},'source':self.broker,'price':130,'as_of':self.now.isoformat()}
        review,_=self.review(kind='stop',trigger=125,quote=quote)
        self.assertEqual(review['payload'].get('ordertype',review['payload'].get('type')),'STOPLOSS_LIMIT' if self.broker=='angel' else 4)
        if self.broker=='angel':self.assertEqual(review['payload']['variety'],'STOPLOSS')
        else:self.assertFalse(review['payload']['offlineOrder']);self.assertFalse(review['payload']['isSliceOrder'])
        with self.assertRaises(Block):self.review(kind='stop',trigger=110,quote=quote)
        with self.assertRaises(Block):self.review(kind='stop',trigger=135,quote=quote)
        self.f.derivative();self.f.position(overnight=-10)
        quote.update({k:self.f.i[k] for k in ('exchange','symbol','token')})
        self.assertEqual(self.review(kind='stop',product='NRML',quantity=5,price=140,trigger=135,quote=quote)[0]['action']['side'],'BUY')

    def test_opt_in_delivery_and_session_gates(self):
        with self.assertRaises(Block):self.review(delivery=None)
        with self.assertRaises(Block):self.review(session=None)
        delivery,session=self.evidence();session['halted']=True
        with self.assertRaises(Block):self.review(session=session)
        delivery['authorized_quantity']=3
        with self.assertRaises(Block):self.review(delivery=delivery)
        self.p['mixed_inventory_reducing_exits']=False
        with self.assertRaises(Block):self.review()
        self.p['mixed_inventory_reducing_exits']=True;self.p['mixed_inventory_acceptance']['account_id']='OTHER'
        with self.assertRaises(Block):self.review()

    def test_cancel_retains_product_and_angel_variety(self):
        self.f.trade();self.f.raw['trades']=[]
        self.f.raw['orders'][0].update({'orderstatus':'trigger pending','filledshares':0,'variety':'STOPLOSS','ordertype':'STOPLOSS_LIMIT','triggerprice':125}
                                     if self.broker=='angel' else {'status':6,'filledQty':0,'type':4,'stopPrice':125})
        with self.assertRaises(Block):self.review()
        review,snap=self.review(kind='cancel',order_id='o1',session=None,delivery=None)
        self.assertTrue(ExecutionStore.matches(review['action'],snap['orders'][0]))
        self.assertFalse(ExecutionStore.matches(review['action'],{**snap['orders'][0],'product':'MIS'}))
        if self.broker=='angel':
            self.assertEqual(review['payload'],{'orderid':'o1','variety':'STOPLOSS'})
            self.assertFalse(ExecutionStore.matches(review['action'],{**snap['orders'][0],'variety':'NORMAL'}))

    def test_recovery_matches_product_price_and_variety(self):
        self.f.trade();self.f.position(sells=4)
        review,snap=self.review()
        row=snap['orders'][0]
        self.assertTrue(ExecutionStore.matches(review['action'],row))
        for changes in ({'product':'MIS'},{'quantity':5},{'price':114}):
            self.assertFalse(ExecutionStore.matches(review['action'],{**row,**changes}))
        if self.broker=='angel':self.assertFalse(ExecutionStore.matches(review['action'],{**row,'variety':'STOPLOSS'}))
        with self.assertRaises(Block):wire_payload({**review['action'],'kind':'entry'})

    def test_cli_recollects_and_only_sends_after_approval(self):
        collection=self.f.run_case();delivery,session=self.evidence()
        approval=Mock();approval.approve.side_effect=lambda review:review['review_hash']
        transport=Mock();transport.send.return_value={'order_id':'new-1'}
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory)
            for name,body in {'config':self.c,'instrument':self.f.i,'live-profile':self.p,'opening':self.f.opening,
                              'master':{'instruments':[self.f.i]},'delivery':delivery,'session':session}.items():
                (root/(name+'.json')).write_text(json.dumps(body))
            args=['exit','--workdir',directory,'--opening',str(root/'opening.json'),'--master',str(root/'master.json'),
                  '--product','CNC','--quantity','4','--price','115','--delivery-authorization',str(root/'delivery.json'),'--session',str(root/'session.json')]
            with patch('broker_portfolio.fetch',return_value=collection) as fetch,patch('broker.adapter'), \
                 patch('portfolio_exit.instant',return_value=self.now),patch('portfolio_exit.LiveBroker',return_value=transport), \
                 patch('portfolio_exit.TerminalApproval',return_value=approval),patch('sys.stdout',new_callable=io.StringIO), \
                 patch('portfolio_exit.execute',side_effect=lambda *args:execute(*args,clock=lambda:self.now)):
                self.assertEqual(main(args),0)
            self.assertEqual(fetch.call_count,2);approval.approve.assert_called_once();transport.send.assert_called_once()
            self.assertEqual(fetch.call_args.args[1:3],(self.broker,'SYNTHETIC'))

    def test_cli_changed_evidence_prevents_send(self):
        review,snap=self.review();changed=copy.deepcopy(review);changed['review_hash']='changed'
        approval=Mock();approval.approve.side_effect=lambda r:r['review_hash'];transport=Mock()
        with tempfile.TemporaryDirectory() as directory:
            store=ExecutionStore(Path(directory)/'execution.sqlite')
            try:
                with self.assertRaises(Block):execute(store,transport,approval,Mock(side_effect=[(review,snap),(changed,snap)]),clock=lambda:self.now)
                transport.send.assert_not_called()
            finally:store.close()


class AngelCarryExecutionTests(Cases,unittest.TestCase):broker='angel'
class FyersCarryExecutionTests(Cases,unittest.TestCase):broker='fyers'

if __name__=='__main__':unittest.main()
