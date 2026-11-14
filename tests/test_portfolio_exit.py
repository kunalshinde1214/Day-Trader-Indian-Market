"""Synthetic mixed-inventory execution contracts; no account or real transport."""
import copy
import contextlib
import io
import json
from datetime import timedelta
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import Mock,patch

ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT/'scripts'))
from core import Block
from execution import ExecutionStore,execute
from live_broker import Ambiguous,wire_payload
from live_state import normalize_order
from portfolio_collect import normalize,ROUTES
from portfolio_exit import prepare,reconcile,main
from test_portfolio_collect import fixture,NOW


class MixedExitTests(unittest.TestCase):
    def setUp(self):
        self.raw,self.opening,self.master=fixture()
        self.i=self.master[0];self.i['freeze_quantity']=1000
        self.opening['source']='reviewed_statement'
        self.opening['positions'][0].update(cost_source='statement',mark_source='previous_close')
        self.c={'broker':'kite','account_id':'DEMO'}
        self.profile={**self.c,'enabled':True,'approval_mode':'human_terminal_two_stage','mixed_inventory_reducing_exits':True,
                      'mixed_inventory_acceptance':{**self.c,'status':'PASS','source':'offline_test_only','as_of':NOW.isoformat()}}
        self.delivery={**{k:self.i[k] for k in ('broker','exchange','symbol','token')},'account_id':'DEMO','date':NOW.date().isoformat(),
                       'as_of':NOW.isoformat(),'source':'offline_delivery_fixture','status':'AUTHORIZED','method':'CDSL_EDIS','authorized_quantity':10}

    def collection(self):
        self.raw['verification']=copy.deepcopy({k:self.raw[k] for k in ROUTES})
        return normalize(self.raw,self.opening,self.master,NOW,execution_details=True)

    def test_carry_stop_uses_delivery_product_and_trigger(self):
        quote={**{k:self.i[k] for k in ('exchange','symbol','token')},'source':'kite','as_of':NOW.isoformat(),'price':130}
        review,_=self.review(kind='stop',trigger=125,quote=quote)
        self.assertEqual(review['payload']['product'],'CNC')
        self.assertEqual(review['payload']['order_type'],'SL')
        self.assertEqual(review['payload']['trigger_price'],125)
        self.assertEqual(review['action']['quantity'],4)

    def test_carry_stop_rejects_stale_wrong_and_reached_quotes(self):
        quote={**{k:self.i[k] for k in ('exchange','symbol','token')},'source':'kite','as_of':NOW.isoformat(),'price':130}
        for changed in ({'token':'wrong'},{'as_of':(NOW-timedelta(seconds=31)).isoformat()},{'price':125}):
            with self.subTest(changed=changed),self.assertRaises(Block):self.review(kind='stop',trigger=125,quote={**quote,**changed})

    def test_carry_stop_does_not_bypass_delivery_or_working_orders(self):
        quote={**{k:self.i[k] for k in ('exchange','symbol','token')},'source':'kite','as_of':NOW.isoformat(),'price':130}
        self.delivery['authorized_quantity']=1
        with self.assertRaises(Block):self.review(kind='stop',trigger=125,quote=quote)
        self.delivery['authorized_quantity']=10
        collection=self.collection();self.working(collection)
        with self.assertRaises(Block):self.review(collection,kind='stop',trigger=125,quote=quote)

    def review(self,collection=None,product='CNC',quantity=4,**kwargs):
        return prepare(collection or self.collection(),self.master,self.i,self.c,self.profile,NOW,
                       product=product,quantity=quantity,price=120,delivery=self.delivery,**kwargs)

    def working(self,collection,product='CNC'):
        row={**{k:self.i[k] for k in ('exchange','symbol','token')},'product':product,'order_id':'OPEN1','side':'SELL',
             'quantity':2,'filled_quantity':0,'status':'OPEN'}
        collection['snapshot']['orders'].append(row)
        collection['execution_orders'].append({**row,'price':120,'trigger_price':0,'order_type':'LIMIT','variety':'regular'})

    def test_cnc_exit_keeps_product_and_strict_quantity(self):
        review,snap=self.review()
        self.assertEqual(review['payload']['product'],'CNC');self.assertEqual(review['payload']['transaction_type'],'SELL')
        self.assertEqual(snap['positions'][0]['quantity'],10)
        with self.assertRaisesRegex(Block,'EXIT'):self.review(quantity=11)

    def test_same_symbol_mis_never_counts_as_delivery(self):
        ident={'exchange':'NSE','tradingsymbol':'DEMO','instrument_token':123,'product':'MIS'}
        self.raw['orders']=[{**ident,'order_id':'MIS1','order_timestamp':'2026-11-06 09:59:00','quantity':5,'filled_quantity':5,
                            'transaction_type':'BUY','status':'COMPLETE','variety':'regular','price':120,'trigger_price':0,'order_type':'LIMIT'}]
        self.raw['trades']=[{**ident,'order_id':'MIS1','trade_id':'T1','quantity':5,'average_price':120,'transaction_type':'BUY','fill_timestamp':'2026-11-06 09:59:00'}]
        position={**ident,'quantity':5,'overnight_quantity':0,'multiplier':1,'day_buy_quantity':5,'day_sell_quantity':0}
        self.raw['positions']={'net':[position],'day':[position]}
        collection=self.collection()
        cnc,_=self.review(collection,quantity=10)
        mis,_=self.review(collection,product='MIS',quantity=5)
        self.assertEqual(cnc['payload']['product'],'CNC');self.assertEqual(mis['payload']['product'],'MIS')
        with self.assertRaises(Block):self.review(collection,product='MIS',quantity=6)
        with self.assertRaises(Block):self.review(collection,quantity=11)

    def test_overnight_derivatives_reduce_long_and_short_in_whole_lots(self):
        self.i.update(exchange='NFO',kind='future',underlying_type='index',expiry='2026-11-26',lot_size=5)
        self.raw['holdings']=[]
        self.raw['quotes']={'NFO:DEMO':self.raw['quotes']['NSE:DEMO']}
        for held in (10,-10):
            self.opening['positions'][0].update(exchange='NFO',product='NRML',quantity=held)
            row={'exchange':'NFO','tradingsymbol':'DEMO','instrument_token':123,'product':'NRML','quantity':held,
                 'overnight_quantity':held,'multiplier':1,'day_buy_quantity':0,'day_sell_quantity':0}
            self.raw['positions']={'net':[row],'day':[{**row,'quantity':0,'overnight_quantity':0}]}
            review,_=self.review(product='NRML',quantity=5)
            self.assertEqual(review['payload']['product'],'NRML')
            self.assertEqual(review['action']['side'],'SELL' if held>0 else 'BUY')
            with self.assertRaises(Block):self.review(product='NRML',quantity=3)

    def test_delivery_authorization_not_needed_for_cancel_or_intraday_exit(self):
        collection=self.collection();self.working(collection)
        self.delivery=None
        review,_=self.review(collection,kind='cancel',order_id='OPEN1')
        self.assertEqual(review['action']['kind'],'cancel')

    def test_carry_execution_cannot_be_reused_as_intraday_risk_reservation(self):
        review,snapshot=self.review()
        with tempfile.TemporaryDirectory() as directory:
            store=ExecutionStore(Path(directory)/'execution.sqlite')
            try:
                intent=store.start(review,0,NOW);store.outcome(intent,'FILLED',NOW,'SELL1')
                with self.assertRaisesRegex(Block,'carry exits'):store.reservations(snapshot)
            finally:store.close()

    def test_working_order_requires_confirmed_cancellation(self):
        collection=self.collection();self.working(collection)
        with self.assertRaisesRegex(Block,'working orders'):self.review(collection)
        review,_=self.review(collection,kind='cancel',order_id='OPEN1')
        self.assertEqual(review['action']['product'],'CNC')
        with self.assertRaises(Block):self.review(collection,product='MIS',kind='cancel',order_id='OPEN1')

    def test_other_product_working_order_does_not_reserve_cnc_units(self):
        collection=self.collection();self.working(collection,'MIS')
        self.assertEqual(self.review(collection,quantity=10)[0]['action']['quantity'],10)

    def test_delivery_authorization_must_match_account_date_units_and_freshness(self):
        original=copy.deepcopy(self.delivery)
        for change in ({'account_id':'other'},{'date':'2026-11-05'},{'authorized_quantity':3},
                       {'as_of':(NOW-timedelta(seconds=31)).isoformat()},{'status':'UNKNOWN'},{'token':'foreign'}):
            self.delivery={**original,**change}
            with self.subTest(change=change),self.assertRaises(Block):self.review()

    def test_optin_acceptance_and_exact_book_required(self):
        self.profile['mixed_inventory_reducing_exits']=False
        with self.assertRaises(Block):self.review()
        self.profile['mixed_inventory_reducing_exits']=True
        self.profile['mixed_inventory_acceptance']['status']='UNTESTED'
        with self.assertRaises(Block):self.review()
        self.profile['mixed_inventory_acceptance']['status']='PASS'
        collection=self.collection();self.working(collection)
        collection['execution_orders'][0]['product']='MIS'
        with self.assertRaisesRegex(Block,'mismatch'):self.review(collection)

    def test_report_flag_cannot_override_unreconciled_units(self):
        collection=self.collection();collection['snapshot']['positions'][0]['quantity']=999
        collection['report']['inventory_reconciled']=True
        with self.assertRaises(Block):self.review(collection)
        collection=self.collection();collection['fills']['account_id']='other'
        with self.assertRaisesRegex(Block,'account mismatch'):self.review(collection)

    def test_recovery_matches_product_not_only_symbol_and_price(self):
        review,_=self.review();action=review['action']
        row={**{k:self.i[k] for k in ('exchange','symbol','token')},'product':'MIS','side':'SELL',
             'quantity':4,'price':120,'trigger_price':0,'order_type':'LIMIT'}
        self.assertFalse(ExecutionStore.matches(action,row))
        row['product']='CNC';self.assertTrue(ExecutionStore.matches(action,row))

    def test_carry_transport_cannot_open_a_position_or_use_other_brokers(self):
        action=self.review()[0]['action']
        with self.assertRaises(Block):wire_payload({**action,'kind':'entry'})
        with self.assertRaises(Block):wire_payload({**action,'broker':'upstox','instrument':{**self.i,'broker':'upstox'}})

    def test_default_intraday_normalizer_still_rejects_carry(self):
        row={'exchange':'NSE','instrument_token':123,'tradingsymbol':'DEMO','product':'CNC'}
        with self.assertRaisesRegex(Block,'PRODUCT'):normalize_order('kite',row,self.master)

    def test_shared_execution_log_records_before_send_and_blocks_ambiguous_retry(self):
        collection=self.collection()
        approval=Mock();approval.approve.side_effect=lambda r:r['review_hash']
        transport=Mock();transport.send.side_effect=TimeoutError('simulated')
        with tempfile.TemporaryDirectory() as directory:
            store=ExecutionStore(Path(directory)/'execution.sqlite')
            try:
                refresh=lambda:self.review(collection)
                with self.assertRaises(Ambiguous):execute(store,transport,approval,refresh,clock=lambda:NOW)
                self.assertEqual(store.records('kite','DEMO')[0]['state'],'UNKNOWN')
                with self.assertRaises(Block):execute(store,transport,approval,refresh,clock=lambda:NOW)
                self.assertEqual(transport.send.call_count,1)
            finally:store.close()

    def test_refresh_change_requires_new_human_review(self):
        collection=self.collection();approval=Mock();transport=Mock()
        def approve(review):
            self.delivery['source']='changed authorization';return review['review_hash']
        approval.approve.side_effect=approve
        with tempfile.TemporaryDirectory() as directory:
            store=ExecutionStore(Path(directory)/'execution.sqlite')
            try:
                with self.assertRaisesRegex(Block,'REVIEW_CHANGED'):
                    execute(store,transport,approval,lambda:self.review(collection),clock=lambda:NOW)
                transport.send.assert_not_called();self.assertEqual(store.records('kite','DEMO'),[])
            finally:store.close()

    def test_cli_refreshes_full_inventory_twice_before_fake_submission(self):
        collection=self.collection();approval=Mock();approval.approve.side_effect=lambda r:r['review_hash']
        transport=Mock();transport.send.return_value={'order_id':'CNC_EXIT_1'}
        def clocked(*args):return execute(*args,clock=lambda:NOW)
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory)
            files={'config':self.c,'instrument':self.i,'live-profile':self.profile,'opening':self.opening,
                   'master':{'instruments':self.master},'delivery':self.delivery}
            for name,body in files.items():(root/(name+'.json')).write_text(json.dumps(body),encoding='utf-8')
            args=['exit','--workdir',str(root),'--opening',str(root/'opening.json'),'--master',str(root/'master.json'),
                  '--product','CNC','--quantity','4','--price','120','--delivery-authorization',str(root/'delivery.json')]
            output=io.StringIO()
            with patch('portfolio_exit.fetch',return_value=collection) as fetcher,patch('portfolio_exit.instant',return_value=NOW), \
                 patch('portfolio_exit.TerminalApproval',return_value=approval),patch('portfolio_exit.LiveBroker',return_value=transport), \
                 patch('portfolio_exit.execute',side_effect=clocked),contextlib.redirect_stdout(output):
                self.assertEqual(main(args),0)
            self.assertEqual(fetcher.call_count,2);self.assertEqual(transport.send.call_count,1)
            self.assertTrue(all(c.kwargs['execution_details'] is True for c in fetcher.call_args_list))
            self.assertEqual(json.loads(output.getvalue())['state'],'ACKNOWLEDGED')
