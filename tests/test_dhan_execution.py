"""Dhan reducing carry actions use simulated collector evidence only."""
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
import test_dhan_portfolio as fixtures


class DhanExecutionTests(unittest.TestCase):
    def setUp(self):
        self.f=fixtures.DhanPortfolioTests();self.f.setUp();self.now=fixtures.NOW
        self.f.i['freeze_quantity']=1000;self.f.opening['source']='reviewed_statement'
        self.f.opening['positions'][0].update(cost_source='statement',mark_source='previous_close')
        self.c={'broker':'dhan','account_id':'1000000009','flat_by':'15:05','snapshot_max_age_seconds':30}
        self.p={**self.c,'enabled':True,'approval_mode':'human_terminal_two_stage','mixed_inventory_reducing_exits':True,
                'mixed_inventory_acceptance':{**self.c,'status':'PASS','source':'offline test','as_of':self.now.isoformat()}}

    def evidence(self):
        common={**{k:self.f.i[k] for k in ('broker','exchange','symbol','token')},'account_id':self.c['account_id'],
                'date':self.now.date().isoformat(),'as_of':self.now.isoformat()}
        delivery={**common,'source':'offline test','status':'AUTHORIZED','method':'CDSL_EDIS','authorized_quantity':10}
        session={**common,'source':'https://www.nseindia.com/static/market-data/market-timings','continuous_open':'09:15',
                 'continuous_close':'15:30','broker_squareoff':'15:20','is_open_day':True,'halted':False,'auction_included':False}
        return delivery,session

    def review(self,**kwargs):
        delivery,session=self.evidence()
        args={'product':'CNC','quantity':4,'price':120,'delivery':delivery,'session':session};args.update(kwargs)
        return prepare(self.f.run_case(),[self.f.i],self.f.i,self.c,self.p,self.now,**args)

    def test_delivery_and_overnight_derivative_mapping(self):
        review,_=self.review();self.assertEqual(review['payload']['productType'],'CNC')
        self.assertEqual(review['payload']['dhanClientId'],self.c['account_id'])
        self.f.derivative();self.f.position(overnight=-10,product='MARGIN')
        review,_=self.review(product='NRML',quantity=5)
        self.assertEqual(review['payload']['productType'],'MARGIN');self.assertEqual(review['payload']['transactionType'],'BUY')
        with self.assertRaises(Block):self.review(product='NRML',quantity=3)

    def test_partial_sale_reconciles_remaining_quantity(self):
        self.f.trade();self.f.position(sells=4);self.f.raw['holdings'][0]['availableQty']=6
        self.assertEqual(self.review(quantity=6)[0]['action']['quantity'],6)
        with self.assertRaises(Block):self.review(quantity=7)

    def test_same_symbol_intraday_is_not_delivery(self):
        self.f.trade(side='BUY',q=5,product='INTRADAY');self.f.position(buys=5,product='INTRADAY')
        self.assertEqual(self.review()[0]['payload']['productType'],'CNC')
        self.assertEqual(self.review(product='MIS',quantity=5)[0]['payload']['productType'],'INTRADAY')

    def test_stop_and_authorization_gates(self):
        quote={**{k:self.f.i[k] for k in ('exchange','symbol','token')},'source':'dhan','price':130,'as_of':self.now.isoformat()}
        review,_=self.review(kind='stop',trigger=125,quote=quote)
        self.assertEqual(review['payload']['orderType'],'STOP_LOSS');self.assertEqual(review['payload']['productType'],'CNC')
        with self.assertRaises(Block):self.review(delivery=None)
        with self.assertRaises(Block):self.review(session=None)
        with self.assertRaises(Block):wire_payload({**review['action'],'kind':'entry'})

    def test_cancel_and_recovery_preserve_product(self):
        self.f.trade();self.f.raw['orders'][0].update(orderStatus='PENDING',filledQty=0);self.f.raw['trades']=[]
        self.f.raw['holdings'][0]['availableQty']=6
        with self.assertRaises(Block):self.review()
        review,snap=self.review(kind='cancel',order_id='o1',session=None)
        self.assertTrue(ExecutionStore.matches(review['action'],snap['orders'][0]))
        self.assertFalse(ExecutionStore.matches(review['action'],{**snap['orders'][0],'product':'MIS'}))

    def test_cli_recollects_before_fake_submission(self):
        collection=self.f.run_case();delivery,session=self.evidence()
        approval=Mock();approval.approve.side_effect=lambda review:review['review_hash']
        transport=Mock();transport.send.return_value={'order_id':'new-1'}
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory)
            for name,body in {'config':self.c,'instrument':self.f.i,'live-profile':self.p,'opening':self.f.opening,
                              'master':{'instruments':[self.f.i]},'delivery':delivery,'session':session}.items():(root/(name+'.json')).write_text(json.dumps(body))
            args=['exit','--workdir',directory,'--opening',str(root/'opening.json'),'--master',str(root/'master.json'),
                  '--product','CNC','--quantity','4','--price','120','--delivery-authorization',str(root/'delivery.json'),'--session',str(root/'session.json')]
            with patch('dhan_portfolio.fetch',return_value=collection) as fetch,patch('broker.adapter'), \
                 patch('portfolio_exit.instant',return_value=self.now),patch('portfolio_exit.LiveBroker',return_value=transport), \
                 patch('portfolio_exit.TerminalApproval',return_value=approval),patch('sys.stdout',new_callable=io.StringIO), \
                 patch('portfolio_exit.execute',side_effect=lambda *args:execute(*args,clock=lambda:self.now)):
                self.assertEqual(main(args),0)
            self.assertEqual(fetch.call_count,2);transport.send.assert_called_once()


if __name__=='__main__':unittest.main()
