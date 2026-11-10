import copy
from datetime import timedelta
import io
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch
ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT/'scripts'))
from core import Block,instant
from dhan_portfolio import normalize,fetch,main,holding_instrument,ROUTES
from dhan_readonly import DhanReadOnly
from live_state import normalize_order
from trade_import import normalize_trades
from trader import read,write
NOW=instant('2026-11-10T10:00:00+05:30')


class DhanPortfolioTests(unittest.TestCase):
    def setUp(self):
        self.i={'broker':'dhan','exchange':'NSE','symbol':'DEMO','token':'123','kind':'equity','lot_size':1,'tick_size':.05,'master_date':'2026-11-10'}
        self.native={'exchangeSegment':'NSE_EQ','tradingSymbol':'DEMO','securityId':'123','dhanClientId':'1000000009','productType':'CNC'}
        h={'broker':'dhan','account_id':'1000000009','source':'dhan','date':'2026-11-10','as_of':NOW.isoformat(),'complete':True}
        self.opening={**h,'schema':'portfolio_opening_v1','source':'synthetic_reviewed_statement','previous_session':'2026-11-09',
                      'adjustments_reviewed':True,'inventory_events':[],'positions':[{'exchange':'NSE','symbol':'DEMO','token':'123',
                      'product':'CNC','quantity':10,'average_price':100,'previous_close':110,'price_basis':'per_unit_inr',
                      'cost_source':'synthetic','mark_source':'synthetic','mark_session':'2026-11-09'}]}
        self.raw={**h,'schema':'dhan_portfolio_read_v1','started_at':NOW.isoformat(),'orders':[],'trades':[],'positions':[],
                  'holdings':[{'exchange':'ALL','tradingSymbol':'DEMO','securityId':'123','isin':'DEMO00000001',
                               'totalQty':10,'dpQty':10,'t1Qty':0,'availableQty':10,'collateralQty':0}],
                  'quotes':{'source':'dhan','missing':[],'quotes':{'NSE_EQ:123':{'source':'dhan','token':'123','exchange':'NSE',
                            'price':120,'as_of':NOW.isoformat(),'timestamp_basis':'last_trade_time interpreted in IST'}}}}
    def verify(self):self.raw['verification']=copy.deepcopy({k:self.raw[k] for k in ROUTES})
    def run_case(self):
        self.verify();return normalize(self.raw,self.opening,[self.i],NOW)
    def trade(self,side='SELL',q=4,price=115,product='CNC',oid='o1',at='2026-11-10 09:59:00'):
        ident={**self.native,'productType':product,'transactionType':side}
        self.raw['orders'].append({**ident,'orderId':oid,'quantity':q,'filledQty':q,'orderStatus':'TRADED','price':price,
                                  'triggerPrice':0,'orderType':'LIMIT','createTime':at})
        self.raw['trades'].append({**ident,'orderId':oid,'exchangeTradeId':'t'+oid,'tradedQuantity':q,'tradedPrice':price,'exchangeTime':at})
    def position(self,buys=0,sells=0,overnight=0,product='CNC'):
        q=overnight+buys-sells
        self.raw['positions']=[{**self.native,'productType':product,'netQty':q,'positionType':'LONG' if q>0 else 'SHORT' if q<0 else 'CLOSED',
                               'carryForwardBuyQty':max(overnight,0),'carryForwardSellQty':max(-overnight,0),
                               'dayBuyQty':buys,'daySellQty':sells,'buyQty':max(overnight,0)+buys,'sellQty':max(-overnight,0)+sells,
                               'multiplier':1,'crossCurrency':False}]
    def derivative(self):
        self.i.update(exchange='NFO',kind='future',lot_size=5,expiry='2026-11-26',underlying_type='index')
        self.native.update(exchangeSegment='NSE_FNO',productType='MARGIN')
        self.opening['positions'][0].update(exchange='NFO',product='NRML',quantity=-10)
        q=self.raw['quotes']['quotes'].pop('NSE_EQ:123');q['exchange']='NFO';self.raw['quotes']['quotes']['NSE_FNO:123']=q
        self.raw['holdings']=[]
    def test_unchanged_holdings(self):
        r=self.run_case();self.assertEqual(r['report']['totals']['daily_gross_inr'],100);self.assertFalse(r['execution_eligible'])
    def test_sale_applied_once(self):
        self.trade();self.position(sells=4);self.raw['holdings'][0]['availableQty']=6
        r=self.run_case();self.assertEqual(r['snapshot']['positions'][0]['quantity'],6)
        self.assertEqual(r['report']['totals']['daily_gross_inr'],80);self.assertIsNone(r['report']['totals']['daily_net_inr'])
    def test_full_sale_needs_no_quote(self):
        self.trade(q=10);self.position(sells=10);self.raw['quotes']={}
        self.assertEqual(self.run_case()['snapshot']['positions'][0]['quantity'],0)
    def test_new_delivery_buy(self):
        self.opening['positions']=[];self.raw['holdings']=[];self.trade('BUY',5,120);self.position(buys=5)
        self.assertEqual(self.run_case()['snapshot']['positions'][0]['quantity'],5)
    def test_buyback_and_intraday_separation(self):
        self.trade();self.trade('BUY',4,118,oid='o2',at='2026-11-10 09:59:01');self.position(buys=4,sells=4)
        self.assertEqual(self.run_case()['snapshot']['positions'][0]['quantity'],10)
        self.setUp();self.trade(product='INTRADAY');self.position(sells=4,product='INTRADAY')
        self.assertEqual({p['product']:p['quantity'] for p in self.run_case()['snapshot']['positions']},{'CNC':10,'MIS':-4})
    def test_available_quantity_is_not_a_fill(self):
        self.raw['holdings'][0]['availableQty']=0
        self.assertEqual(self.run_case()['snapshot']['positions'][0]['quantity'],10)
    def test_working_reservation_has_no_inventory_effect(self):
        self.trade();self.raw['orders'][0].update(orderStatus='PENDING',filledQty=0);self.raw['trades']=[]
        self.raw['holdings'][0]['availableQty']=6
        r=self.run_case();self.assertEqual(r['report']['working_orders'],1);self.assertEqual(r['snapshot']['positions'][0]['quantity'],10)
    def test_derivative_short_reduction(self):
        self.derivative();self.trade('BUY',5,product='MARGIN');self.position(buys=5,overnight=-10,product='MARGIN')
        r=self.run_case();self.assertEqual(r['fills']['fills'][0]['product'],'NRML')
        self.assertEqual(r['snapshot']['positions'][0]['quantity'],-5);self.assertEqual(r['report']['totals']['daily_gross_inr'],-75)
    def test_derivative_reversal(self):
        self.derivative();self.trade('BUY',15,product='MARGIN');self.position(buys=15,overnight=-10,product='MARGIN')
        self.assertEqual(self.run_case()['snapshot']['positions'][0]['quantity'],5)
    def test_all_exchange_requires_unique_equity_match(self):
        row=self.raw['holdings'][0];other={**self.i,'exchange':'BSE'}
        with self.assertRaises(Block):holding_instrument(row,[self.i,other])
        row['exchange']='BSE';self.assertEqual(holding_instrument(row,[self.i,other])['exchange'],'BSE')
        row['exchange']='MCX'
        with self.assertRaises(Block):holding_instrument(row,[self.i])
    def test_unknown_holding_identity_blocks(self):
        for fields in ({'securityId':'999'},{'tradingSymbol':'OTHER'}):
            old=copy.deepcopy(self.raw);self.raw['holdings'][0].update(fields)
            with self.assertRaises(Block):self.run_case()
            self.raw=old
    def test_unsupported_or_changed_holdings_block(self):
        original=copy.deepcopy(self.raw)
        for fields in ({'t1Qty':1},{'collateralQty':1},{'productType':'MTF'},{'totalQty':6},{'dpQty':6},{'availableQty':11}):
            self.raw=copy.deepcopy(original);self.raw['holdings'][0].update(fields)
            with self.assertRaises(Block):self.run_case()
    def test_duplicate_and_missing_holdings_block(self):
        row=self.raw['holdings'][0]
        for rows in ([],[row,row]):
            self.raw['holdings']=rows
            with self.assertRaises(Block):self.run_case()
    def test_product_kind_mismatch_and_funded_positions_block(self):
        for product in ('MARGIN','MTF','CO','BO','UNKNOWN'):
            self.raw['orders']=[];self.raw['trades']=[];self.trade(product=product)
            with self.assertRaises(Block):self.run_case()
    def test_position_totals_and_direction_block(self):
        self.derivative();self.position(overnight=-10,product='MARGIN');original=copy.deepcopy(self.raw)
        for fields in ({'netQty':-5},{'sellQty':5},{'carryForwardSellQty':5},{'dayBuyQty':5},{'positionType':'LONG'},
                       {'multiplier':5},{'crossCurrency':True}):
            self.raw=copy.deepcopy(original);self.raw['positions'][0].update(fields)
            with self.assertRaises(Block):self.run_case()
    def test_missing_trade_position_and_order_fill_mismatch(self):
        self.trade()
        with self.assertRaises(Block):self.run_case()
        self.position(sells=4);self.raw['trades'][0]['tradedQuantity']=3
        with self.assertRaises(Block):self.run_case()
    def test_foreign_account_rows_block(self):
        self.trade();self.position(sells=4)
        for name in ('orders','trades','positions'):
            self.raw[name][0]['dhanClientId']='OTHER'
            with self.assertRaises(Block):self.run_case()
            self.raw[name][0]['dhanClientId']='1000000009'
    def test_changed_books_block_but_moving_marks_do_not(self):
        self.verify();self.raw['verification']['holdings'][0]['availableQty']=9
        with self.assertRaises(Block):normalize(self.raw,self.opening,[self.i],NOW)
        self.verify();self.raw['verification']['holdings'][0]['productType']='MTF'
        with self.assertRaises(Block):normalize(self.raw,self.opening,[self.i],NOW)
        self.verify();self.raw['verification']['holdings'][0]['avgCostPrice']=105
        self.assertTrue(normalize(self.raw,self.opening,[self.i],NOW)['report']['inventory_reconciled'])
    def test_stale_or_foreign_quotes_block(self):
        original=copy.deepcopy(self.raw)
        for fields in ({'source':'kite'},{'token':'999'},{'as_of':(NOW-timedelta(seconds=61)).isoformat()},
                       {'as_of':(NOW+timedelta(seconds=1)).isoformat()}):
            self.raw=copy.deepcopy(original);self.raw['quotes']['quotes']['NSE_EQ:123'].update(fields)
            with self.assertRaises(Block):self.run_case()
    def test_unsupported_order_state_and_linked_legs_block(self):
        self.trade();self.position(sells=4);original=copy.deepcopy(self.raw)
        for fields in ({'orderStatus':'TRANSIT'},{'legName':'ENTRY_LEG'},{'boProfitValue':1},{'createTime':'2026-11-09 09:59:00'}):
            self.raw=copy.deepcopy(original);self.raw['orders'][0].update(fields)
            with self.assertRaises(Block):self.run_case()
    def test_same_timestamp_sequence_not_inferred(self):
        self.trade(q=2);self.trade(q=2,oid='o2');self.position(sells=4)
        with self.assertRaises(Block):self.run_case()
    def test_execution_and_default_import_stay_intraday_only(self):
        self.trade();self.position(sells=4);r=self.run_case()
        with self.assertRaises(Block):normalize_order('dhan',self.raw['orders'][0],[self.i])
        with self.assertRaises(Block):normalize_trades(self.raw,r['snapshot'],[self.i],NOW)
    def test_read_only_route_does_not_allow_mutation(self):
        with patch.dict('os.environ',{'DHAN_CLIENT_ID':'1000000009','DHAN_ACCESS_TOKEN':'synthetic'}),patch.object(DhanReadOnly,'_read',return_value=(b'[]',{})):
            c=DhanReadOnly();self.assertEqual(c.request('GET','/holdings'),[])
            for method in ('POST','DELETE','PUT'):
                with self.assertRaises(Block):c.request(method,'/holdings')
    def test_fetch_repeats_books_and_checks_account(self):
        calls=[];raw=self.raw
        class Client:
            changed=False;count=0
            def account_id(self):self.count+=1;return 'OTHER' if self.changed and self.count>1 else '1000000009'
            def request(self,method,path):calls.append((method,path));return copy.deepcopy(raw[next(k for k,v in ROUTES.items() if v==path)])
            def quotes(self,keys):
                if keys!=['NSE_EQ:123']:raise AssertionError(keys)
                return copy.deepcopy(raw['quotes'])
        self.assertTrue(fetch(Client(),'1000000009',self.opening,[self.i],clock=lambda:NOW)['report']['inventory_reconciled'])
        self.assertEqual(len(calls),8);client=Client();client.changed=True
        with self.assertRaises(Block):fetch(client,'1000000009',self.opening,[self.i],clock=lambda:NOW)
    def test_window_and_review_failures(self):
        self.raw['started_at']=(NOW-timedelta(seconds=61)).isoformat()
        with self.assertRaises(Block):self.run_case()
        self.raw['started_at']=NOW.isoformat();self.opening['adjustments_reviewed']=False
        with self.assertRaises(Block):self.run_case()
    def test_cli_fixture_and_no_overwrite(self):
        self.verify()
        with tempfile.TemporaryDirectory() as tmp,patch('sys.stdout',new_callable=io.StringIO):
            w=Path(tmp);write(w/'raw.json',self.raw);write(w/'opening.json',self.opening);write(w/'master.json',{'instruments':[self.i]})
            args=['--input',str(w/'raw.json'),'--opening',str(w/'opening.json'),'--master',str(w/'master.json'),
                  '--account-id','1000000009','--now',NOW.isoformat(),'--out',str(w/'out.json')]
            self.assertEqual(main(args),0);before=(w/'out.json').read_bytes();self.assertEqual(main(args),2)
            self.assertEqual((w/'out.json').read_bytes(),before);self.assertFalse(read(w/'out.json')['submitted'])


if __name__=='__main__':unittest.main()
