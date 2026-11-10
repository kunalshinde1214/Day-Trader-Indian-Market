import copy
from datetime import timedelta
import io
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch
ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT/'scripts'))
from core import Block,instant
from live_state import normalize_order
from trade_import import normalize_trades
from trader import read,write
from upstox_portfolio import normalize,fetch,main,ROUTES
from upstox_readonly import UpstoxReadOnly

NOW=instant('2026-11-10T10:00:00+05:30')


class UpstoxPortfolioTests(unittest.TestCase):
    def setUp(self):
        self.i={'broker':'upstox','exchange':'NSE','symbol':'DEMO','token':'NSE_EQ|DEMO00000001','kind':'equity',
                'lot_size':1,'tick_size':.05,'master_date':'2026-11-10'}
        self.ident={'exchange':'NSE','symbol':'DEMO','token':self.i['token'],'product':'CNC'}
        self.native={'exchange':'NSE','trading_symbol':'DEMO','instrument_token':self.i['token'],'product':'D'}
        header={'broker':'upstox','account_id':'SYNTHETIC','source':'upstox','date':'2026-11-10','as_of':NOW.isoformat(),'complete':True}
        self.opening={**header,'source':'synthetic_reviewed_statement','schema':'portfolio_opening_v1','previous_session':'2026-11-09',
                      'adjustments_reviewed':True,'inventory_events':[],'positions':[{**self.ident,'quantity':10,'average_price':100,
                      'previous_close':110,'price_basis':'per_unit_inr','cost_source':'synthetic','mark_source':'synthetic','mark_session':'2026-11-09'}]}
        self.raw={**header,'schema':'upstox_portfolio_read_v1','started_at':NOW.isoformat(),'orders':[],'trades':[],'positions':[],
                  'mtf_positions':[],'holdings':[{**self.native,'isin':'DEMO00000001','quantity':10,'cnc_used_quantity':0,
                  't1_quantity':0,'collateral_quantity':0,'collateral_update_quantity':0}],
                  'quotes':{'source':'upstox','missing':[],'quotes':{self.i['token']:{'source':'upstox','token':self.i['token'],
                  'exchange':'NSE','price':120,'as_of':NOW.isoformat(),'last_trade_time':str(int(NOW.timestamp()*1000)-1000)}}}}

    def verify(self):self.raw['verification']=copy.deepcopy({k:self.raw[k] for k in ROUTES})
    def run_case(self):
        self.verify();return normalize(self.raw,self.opening,[self.i],NOW)
    def trade(self,side='SELL',q=4,price=115,oid='o1',at='2026-11-10 09:59:00',product='D'):
        self.raw['orders'].append({**self.native,'product':product,'order_id':oid,'order_timestamp':at,'quantity':q,'filled_quantity':q,
                                  'transaction_type':side,'status':'complete','price':price,'trigger_price':0,'order_type':'LIMIT','variety':'SIMPLE'})
        self.raw['trades'].append({**self.native,'product':product,'order_id':oid,'trade_id':'t'+oid,'exchange_timestamp':at,
                                  'quantity':q,'average_price':price,'transaction_type':side})
    def position(self,buys=0,sells=0,overnight=0,product='D'):
        self.raw['positions']=[{**self.native,'product':product,'quantity':overnight+buys-sells,'overnight_quantity':overnight,
                               'overnight_buy_quantity':max(overnight,0),'overnight_sell_quantity':max(-overnight,0),
                               'day_buy_quantity':buys,'day_sell_quantity':sells,'multiplier':1}]
    def derivative(self):
        self.i.update(exchange='NFO',token='NSE_FO|123',kind='future',lot_size=5,expiry='2026-11-26',underlying_type='index')
        self.native.update(exchange='NFO',instrument_token=self.i['token'])
        self.opening['positions'][0].update(exchange='NFO',token=self.i['token'],product='NRML',quantity=-10)
        q=self.raw['quotes']['quotes'].pop('NSE_EQ|DEMO00000001');q.update(exchange='NFO',token=self.i['token'])
        self.raw['quotes']['quotes'][self.i['token']]=q;self.raw['holdings']=[]

    def test_unchanged_settled_holdings(self):
        result=self.run_case();self.assertEqual(result['report']['totals']['daily_gross_inr'],100)
        self.assertFalse(result['execution_eligible']);self.assertFalse(result['submitted'])
    def test_partial_sale_applied_once(self):
        self.trade();self.position(sells=4);self.raw['holdings'][0]['cnc_used_quantity']=4
        r=self.run_case();self.assertEqual(r['snapshot']['positions'][0]['quantity'],6)
        self.assertEqual(r['report']['totals']['daily_gross_inr'],80);self.assertIsNone(r['report']['totals']['daily_net_inr'])
    def test_fully_sold_holding_needs_no_mark(self):
        self.trade(q=10);self.position(sells=10);self.raw['quotes']={}
        self.assertEqual(self.run_case()['snapshot']['positions'][0]['quantity'],0)
    def test_open_order_reservation_never_becomes_sale(self):
        self.trade();self.raw['orders'][0].update(status='open',filled_quantity=0);self.raw['trades']=[]
        self.raw['holdings'][0]['cnc_used_quantity']=4
        r=self.run_case();self.assertEqual(r['snapshot']['positions'][0]['quantity'],10)
        self.assertEqual(r['report']['working_orders'],1)
    def test_new_delivery_buy(self):
        self.opening['positions']=[];self.raw['holdings']=[];self.trade('BUY',5,120);self.position(buys=5)
        self.assertEqual(self.run_case()['snapshot']['positions'][0]['quantity'],5)
    def test_sale_buyback_preserves_inventory(self):
        self.trade();self.trade('BUY',4,118,'o2','2026-11-10 09:59:01');self.position(buys=4,sells=4)
        self.assertEqual(self.run_case()['snapshot']['positions'][0]['quantity'],10)
    def test_intraday_short_stays_separate(self):
        self.trade(product='I');self.position(sells=4,product='I')
        r=self.run_case();self.assertEqual({p['product']:p['quantity'] for p in r['snapshot']['positions']},{'CNC':10,'MIS':-4})
    def test_derivative_delivery_product_maps_to_nrml(self):
        self.derivative();self.trade('BUY',5);self.position(buys=5,overnight=-10)
        r=self.run_case();self.assertEqual(r['snapshot']['positions'][0]['quantity'],-5)
        self.assertEqual(r['fills']['fills'][0]['product'],'NRML');self.assertEqual(r['report']['totals']['daily_gross_inr'],-75)
    def test_derivative_reversal(self):
        self.derivative();self.trade('BUY',15);self.position(buys=15,overnight=-10)
        self.assertEqual(self.run_case()['snapshot']['positions'][0]['quantity'],5)
    def test_unknown_and_mtf_products_block(self):
        for product in ('MTF','CO','BO','UNKNOWN'):
            with self.subTest(product=product):
                self.raw['holdings'][0]['product']=product
                with self.assertRaises(Block):self.run_case()
    def test_pledged_and_unsettled_fields_block(self):
        original=copy.deepcopy(self.raw)
        for field in ('t1_quantity','collateral_quantity','collateral_update_quantity'):
            self.raw=copy.deepcopy(original);self.raw['holdings'][0][field]=1
            with self.assertRaises(Block):self.run_case()
    def test_separate_mtf_endpoint_cannot_be_omitted_or_nonempty(self):
        self.raw['mtf_positions']=[{'quantity':1}]
        with self.assertRaises(Block):self.run_case()
        del self.raw['mtf_positions']
        with self.assertRaises(KeyError):self.run_case()
    def test_reduced_holdings_quantity_is_not_guessed(self):
        self.trade();self.position(sells=4);self.raw['holdings'][0].update(quantity=6,cnc_used_quantity=4)
        with self.assertRaises(Block):self.run_case()
    def test_used_quantity_requires_matching_sale_or_reservation(self):
        self.raw['holdings'][0]['cnc_used_quantity']=4
        with self.assertRaises(Block):self.run_case()
    def test_missing_and_duplicate_holdings_block(self):
        holding=self.raw['holdings'][0]
        for rows in ([],[holding,holding]):
            self.raw['holdings']=rows
            with self.assertRaises(Block):self.run_case()
    def test_missing_position_and_fill_mismatch_block(self):
        self.trade()
        with self.assertRaises(Block):self.run_case()
        self.position(sells=4);self.raw['trades'][0]['quantity']=3
        with self.assertRaises(Block):self.run_case()
    def test_position_quantity_and_overnight_mismatches_block(self):
        self.derivative();self.position(overnight=-10);base=copy.deepcopy(self.raw)
        for fields in ({'quantity':-5},{'overnight_quantity':-5},{'overnight_sell_quantity':5},{'multiplier':5}):
            self.raw=copy.deepcopy(base);self.raw['positions'][0].update(fields)
            with self.assertRaises(Block):self.run_case()
    def test_changed_inventory_between_reads_blocks(self):
        self.verify();self.raw['verification']['holdings'][0]['quantity']=9
        with self.assertRaises(Block):normalize(self.raw,self.opening,[self.i],NOW)
    def test_moving_marks_do_not_invalidate_stable_inventory(self):
        self.verify();self.raw['verification']['holdings'][0]['last_price']=121
        self.assertTrue(normalize(self.raw,self.opening,[self.i],NOW)['report']['inventory_reconciled'])
    def test_stale_last_trade_rejected_even_with_fresh_quote_clock(self):
        self.raw['quotes']['quotes'][self.i['token']]['last_trade_time']=str(int((NOW-timedelta(seconds=61)).timestamp()*1000))
        with self.assertRaises(Block):self.run_case()
    def test_quote_identity_and_future_clocks_block(self):
        original=copy.deepcopy(self.raw)
        for fields in ({'token':'wrong'},{'source':'kite'},{'last_trade_time':str(int(NOW.timestamp()*1000)+1)},
                       {'as_of':(NOW+timedelta(seconds=1)).isoformat()}):
            self.raw=copy.deepcopy(original);self.raw['quotes']['quotes'][self.i['token']].update(fields)
            with self.assertRaises(Block):self.run_case()
    def test_foreign_account_orders_and_fills_block(self):
        self.trade();self.position(sells=4)
        for name in ('orders','trades'):
            self.raw[name][0]['user_id']='OTHER'
            with self.assertRaises(Block):self.run_case()
            del self.raw[name][0]['user_id']
    def test_unknown_orders_and_wrong_session_block(self):
        self.trade();self.position(sells=4);original=copy.deepcopy(self.raw)
        for fields in ({'status':'unknown'},{'order_timestamp':'2026-11-09 09:59:00'},{'variety':'CO'}):
            self.raw=copy.deepcopy(original);self.raw['orders'][0].update(fields)
            with self.assertRaises(Block):self.run_case()
    def test_ambiguous_same_timestamp_fills_block(self):
        self.trade(q=2);self.trade(q=2,oid='o2');self.position(sells=4)
        with self.assertRaises(Block):self.run_case()
    def test_live_and_default_trade_import_remain_intraday_only(self):
        self.trade();self.position(sells=4);r=self.run_case()
        with self.assertRaises(Block):normalize_order('upstox',self.raw['orders'][0],[self.i])
        with self.assertRaises(Block):normalize_trades(self.raw,r['snapshot'],[self.i],NOW)
    def test_fetch_double_reads_and_identity_checks(self):
        calls=[];raw=self.raw
        class Client:
            def account_id(self):calls.append('profile');return 'SYNTHETIC'
            def request(self,method,path):
                calls.append((method,path));return copy.deepcopy(raw[next(k for k,v in ROUTES.items() if v==path)])
            def quotes(self,tokens):return copy.deepcopy(raw['quotes'])
        result=fetch(Client(),'SYNTHETIC',self.opening,[self.i],clock=lambda:NOW)
        self.assertTrue(result['report']['inventory_reconciled']);self.assertEqual(calls.count('profile'),2)
        self.assertEqual(sum(isinstance(c,tuple) for c in calls),10)
    def test_fetch_account_change_blocks(self):
        raw=self.raw
        class Client:
            count=0
            def account_id(self):self.count+=1;return 'SYNTHETIC' if self.count==1 else 'OTHER'
            def request(self,method,path):return copy.deepcopy(raw[next(k for k,v in ROUTES.items() if v==path)])
            def quotes(self,tokens):return copy.deepcopy(raw['quotes'])
        with self.assertRaises(Block):fetch(Client(),'SYNTHETIC',self.opening,[self.i],clock=lambda:NOW)
    def test_collection_window_and_opening_review_required(self):
        original=copy.deepcopy(self.raw)
        for at in (NOW-timedelta(seconds=61),NOW+timedelta(seconds=1)):
            self.raw=copy.deepcopy(original);self.raw['started_at']=at.isoformat()
            with self.assertRaises(Block):self.run_case()
        self.raw=original;self.opening['adjustments_reviewed']=False
        with self.assertRaises(Block):self.run_case()
    def test_read_only_holdings_routes_never_allow_mutation(self):
        with patch.dict('os.environ',{'UPSTOX_ACCESS_TOKEN':'synthetic'}),patch.object(UpstoxReadOnly,'_read',return_value=(b'{"status":"success","data":[]}',{})):
            client=UpstoxReadOnly()
            for route in (ROUTES['holdings'],ROUTES['mtf_positions']):
                self.assertEqual(client.request('GET',route),[])
                with self.assertRaises(Block):client.request('POST',route)
    def test_cli_saved_fixture_and_no_overwrite(self):
        self.verify()
        with tempfile.TemporaryDirectory() as tmp,patch('sys.stdout',new_callable=io.StringIO):
            w=Path(tmp);write(w/'raw.json',self.raw);write(w/'opening.json',self.opening);write(w/'master.json',{'instruments':[self.i]})
            args=['--input',str(w/'raw.json'),'--opening',str(w/'opening.json'),'--master',str(w/'master.json'),
                  '--account-id','SYNTHETIC','--now',NOW.isoformat(),'--out',str(w/'out.json')]
            self.assertEqual(main(args),0);before=(w/'out.json').read_bytes();self.assertEqual(main(args),2)
            self.assertEqual((w/'out.json').read_bytes(),before);self.assertFalse(read(w/'out.json')['submitted'])


if __name__=='__main__':unittest.main()
