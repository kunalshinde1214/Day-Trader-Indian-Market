import copy
from datetime import timedelta
import io
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch
ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT/'scripts'))
from broker_portfolio import normalize,fetch,main,routes
from broker_data import quote_key
from core import Block,instant
from live_state import normalize_order
from trade_import import normalize_trades
from trader import read,write
NOW=instant('2026-11-10T10:00:00+05:30')


class Cases:
    def setUp(self):
        b=self.broker
        self.i={'broker':b,'exchange':'NSE','symbol':'DEMO','token':'123' if b=='angel' else '10100000000123',
                'kind':'equity','lot_size':1,'tick_size':.05,'master_date':'2026-11-10'}
        self.ident={'exchange':'NSE','symbol':'DEMO','token':self.i['token'],'product':'CNC'}
        self.native={'exchange':'NSE','tradingsymbol':'DEMO','symboltoken':'123','producttype':'DELIVERY'} if b=='angel' else {
            'symbol':'NSE:DEMO','fyToken':self.i['token'],'exchange':10,'segment':10,'productType':'CNC'}
        h={'broker':b,'account_id':'SYNTHETIC','source':b,'date':'2026-11-10','as_of':NOW.isoformat(),'complete':True}
        self.opening={**h,'source':'synthetic_reviewed','schema':'portfolio_opening_v1','previous_session':'2026-11-09',
                      'adjustments_reviewed':True,'inventory_events':[],'positions':[{**self.ident,'quantity':10,'average_price':100,
                      'previous_close':110,'price_basis':'per_unit_inr','cost_source':'synthetic','mark_source':'synthetic','mark_session':'2026-11-09'}]}
        holding={**self.native,'isin':'SYNTHETIC','quantity':10}
        if b=='angel':holding.update(product='DELIVERY',realisedquantity=10,t1quantity=0,authorisedquantity=0,collateralquantity=0)
        else:holding.update(holdingType='HLD',remainingQuantity=10,qty_t1=0,collateralQuantity=0,remainingPledgeQuantity=0)
        self.raw={**h,'schema':b+'_portfolio_read_v1','started_at':NOW.isoformat(),'orders':[],'trades':[],'positions':[],'holdings':[holding],
                  'quotes':{'source':b,'missing':[],'quotes':{quote_key(self.i):{'source':b,'token':self.i['token'],'exchange':'NSE',
                  'symbol':'DEMO','price':120,'as_of':NOW.isoformat()}}}}
    def verify(self):self.raw['verification']=copy.deepcopy({k:self.raw[k] for k in ('orders','trades','positions','holdings')})
    def run_case(self):
        self.verify();return normalize(self.raw,self.opening,[self.i],NOW)
    def trade(self,side='SELL',q=4,price=115,product=None,oid='o1',at='2026-11-10 09:59:00'):
        n=copy.deepcopy(self.native)
        if product:n['producttype' if self.broker=='angel' else 'productType']=product
        if self.broker=='angel':
            self.raw['orders'].append({**n,'orderid':oid,'quantity':q,'filledshares':q,'transactiontype':side,'orderstatus':'complete',
                                      'price':price,'triggerprice':0,'ordertype':'LIMIT','variety':'NORMAL','updatetime':at})
            self.raw['trades'].append({**n,'orderid':oid,'fillid':'t'+oid,'fillsize':q,'fillprice':price,'transactiontype':side,'filltime':at})
        else:
            self.raw['orders'].append({**n,'id':oid,'qty':q,'filledQty':q,'side':1 if side=='BUY' else -1,'status':2,
                                      'limitPrice':price,'stopPrice':0,'type':1,'orderDateTime':at})
            self.raw['trades'].append({**n,'orderNumber':oid,'tradeNumber':'t'+oid,'tradedQty':q,'tradePrice':price,
                                      'side':1 if side=='BUY' else -1,'orderDateTime':at})
    def position(self,buys=0,sells=0,overnight=0,product=None):
        n=copy.deepcopy(self.native);q=overnight+buys-sells
        if product:n['producttype' if self.broker=='angel' else 'productType']=product
        if self.broker=='angel':n.update(netqty=q,buyqty=buys,sellqty=sells,cfbuyqty=max(overnight,0),cfsellqty=max(-overnight,0),
                                        multiplier=1,pricenum=1,priceden=1,gennum=1,genden=1)
        else:n.update(netQty=q,qty=abs(q),side=1 if q>0 else -1 if q<0 else 0,dayBuyQty=buys,daySellQty=sells,
                      cfBuyQty=max(overnight,0),cfSellQty=max(-overnight,0),buyQty=max(overnight,0)+buys,
                      sellQty=max(-overnight,0)+sells,crossCurrency='N',qtyMulti_com=1)
        self.raw['positions']=[n]
    def derivative(self):
        old=quote_key(self.i);self.i.update(exchange='NFO',kind='future',expiry='2026-11-26',underlying_type='index',lot_size=5)
        if self.broker=='angel':self.native.update(exchange='NFO',producttype='CARRYFORWARD')
        else:
            self.i['token']='10110000000123';self.native.update(segment=11,fyToken=self.i['token'],productType='MARGIN')
        self.opening['positions'][0].update(exchange='NFO',token=self.i['token'],product='NRML',quantity=-10)
        q=self.raw['quotes']['quotes'].pop(old);q.update(exchange='NFO',token=self.i['token']);self.raw['quotes']['quotes'][quote_key(self.i)]=q
        self.raw['holdings']=[]
    def test_holdings_and_daily_pnl(self):
        r=self.run_case();self.assertEqual(r['report']['totals']['daily_gross_inr'],100);self.assertFalse(r['execution_eligible'])
    def test_sale_applied_once(self):
        self.trade();self.position(sells=4)
        if self.broker=='fyers':self.raw['holdings'][0]['remainingQuantity']=6
        r=self.run_case();self.assertEqual(r['snapshot']['positions'][0]['quantity'],6)
        self.assertEqual(r['report']['totals']['daily_gross_inr'],80);self.assertIsNone(r['report']['totals']['daily_net_inr'])
    def test_working_order_does_not_change_inventory(self):
        self.trade();self.raw['trades']=[]
        self.raw['orders'][0].update({'orderstatus':'open','filledshares':0} if self.broker=='angel' else {'status':6,'filledQty':0})
        r=self.run_case();self.assertEqual(r['snapshot']['positions'][0]['quantity'],10);self.assertEqual(r['report']['working_orders'],1)
    def test_unknown_order_or_wrong_session_blocks(self):
        self.trade();self.position(sells=4);original=copy.deepcopy(self.raw)
        edits=[{'orderstatus':'unknown'},{'updatetime':'2026-11-09 09:59:00'}] if self.broker=='angel' else [
            {'status':4},{'orderDateTime':'2026-11-09 09:59:00'}]
        for values in edits:
            self.raw=copy.deepcopy(original);self.raw['orders'][0].update(values)
            with self.assertRaises(Block):self.run_case()
    def test_holdings_route_is_read_only(self):
        if self.broker=='angel':
            from angel_readonly import AngelReadOnly as Client
            settings={k:'synthetic' for k in ('ANGEL_ACCESS_TOKEN','ANGEL_API_KEY','ANGEL_LOCAL_IP','ANGEL_PUBLIC_IP','ANGEL_MAC_ADDRESS')}
            body=b'{"status":true,"errorcode":"","data":[]}'
        else:
            from fyers_readonly import FyersReadOnly as Client
            settings={'FYERS_APP_ID':'synthetic','FYERS_ACCESS_TOKEN':'synthetic'};body=b'{"s":"ok","code":200,"holdings":[]}'
        with patch.object(Client,'credentials',return_value=settings),patch.object(Client,'_read',return_value=(body,{})):
            c=Client();result=c.request('GET',routes(self.broker)['holdings'])
            self.assertEqual(result if self.broker=='angel' else result['holdings'],[])
            for method in ('POST','PUT','DELETE'):
                with self.assertRaises(Block):c.request(method,routes(self.broker)['holdings'])
    def test_full_sale_without_mark(self):
        self.trade(q=10);self.position(sells=10);self.raw['quotes']={}
        self.assertEqual(self.run_case()['snapshot']['positions'][0]['quantity'],0)
    def test_new_delivery_buy(self):
        self.opening['positions']=[];self.raw['holdings']=[];self.trade('BUY',5);self.position(buys=5)
        self.assertEqual(self.run_case()['snapshot']['positions'][0]['quantity'],5)
    def test_buyback_and_intraday_separation(self):
        self.trade();self.trade('BUY',4,118,oid='o2',at='2026-11-10 09:59:01');self.position(buys=4,sells=4)
        self.assertEqual(self.run_case()['snapshot']['positions'][0]['quantity'],10)
        self.setUp();self.trade(product='INTRADAY');self.position(sells=4,product='INTRADAY')
        self.assertEqual({r['product']:r['quantity'] for r in self.run_case()['snapshot']['positions']},{'CNC':10,'MIS':-4})
    def test_derivative_reduction_and_provider_quote_key(self):
        self.derivative();self.trade('BUY',5);self.position(buys=5,overnight=-10)
        r=self.run_case();self.assertEqual(r['snapshot']['positions'][0]['quantity'],-5)
        self.assertEqual(r['fills']['fills'][0]['product'],'NRML')
        if self.broker=='fyers':self.assertEqual(quote_key(self.i),'NSE:DEMO')
    def test_derivative_reversal(self):
        self.derivative();self.trade('BUY',15);self.position(buys=15,overnight=-10)
        self.assertEqual(self.run_case()['snapshot']['positions'][0]['quantity'],5)
    def test_unknown_settlement_and_collateral_block(self):
        original=copy.deepcopy(self.raw)
        fields=('t1quantity','collateralquantity') if self.broker=='angel' else ('qty_t1','collateralQuantity','remainingPledgeQuantity')
        for field in fields:
            for value in (1,None):
                self.raw=copy.deepcopy(original);self.raw['holdings'][0][field]=value
                with self.assertRaises((Block,TypeError)):self.run_case()
    def test_reduced_and_duplicate_holdings_block(self):
        self.raw['holdings'][0]['quantity']=6
        with self.assertRaises(Block):self.run_case()
        self.raw['holdings'][0]['quantity']=10;self.raw['holdings']*=2
        with self.assertRaises(Block):self.run_case()
    def test_unsupported_products_block(self):
        for product in ('MTF','BO','UNKNOWN'):
            self.raw['orders']=[];self.raw['trades']=[];self.trade(product=product)
            with self.assertRaises(Block):self.run_case()
    def test_missing_and_mismatched_activity_blocks(self):
        self.trade()
        with self.assertRaises(Block):self.run_case()
        self.position(sells=3)
        with self.assertRaises(Block):self.run_case()
    def test_position_units_and_multiplier_block(self):
        self.derivative();self.position(overnight=-10);original=copy.deepcopy(self.raw)
        edits=[{'netqty':-5},{'cfbuyqty':1},{'multiplier':-1},{'priceden':100}] if self.broker=='angel' else [
            {'netQty':-5},{'cfBuyQty':1},{'qtyMulti_com':5},{'crossCurrency':'Y'},{'side':True}]
        for values in edits:
            self.raw=copy.deepcopy(original);self.raw['positions'][0].update(values)
            with self.assertRaises(Block):self.run_case()
    def test_identity_and_stale_quotes_block(self):
        original=copy.deepcopy(self.raw)
        for values in ({'token':'wrong'},{'source':'kite'},{'as_of':(NOW-timedelta(seconds=61)).isoformat()}):
            self.raw=copy.deepcopy(original);self.raw['quotes']['quotes'][quote_key(self.i)].update(values)
            with self.assertRaises(Block):self.run_case()
    def test_changed_books_and_foreign_accounts_block(self):
        self.verify();self.raw['verification']['holdings'][0]['quantity']=9
        with self.assertRaises(Block):normalize(self.raw,self.opening,[self.i],NOW)
        self.raw['holdings'][0]['clientcode' if self.broker=='angel' else 'fy_id']='OTHER'
        with self.assertRaises(Block):self.run_case()
    def test_same_time_fills_block(self):
        self.trade(q=2);self.trade(q=2,oid='o2');self.position(sells=4)
        with self.assertRaises(Block):self.run_case()
    def test_execution_and_default_import_stay_intraday_only(self):
        self.trade();self.position(sells=4);r=self.run_case()
        with self.assertRaises(Block):normalize_order(self.broker,self.raw['orders'][0],[self.i])
        with self.assertRaises(Block):normalize_trades(self.raw,r['snapshot'],[self.i],NOW)
    def test_fetch_double_reads_and_account_change(self):
        b=self.broker;raw=self.raw;calls=[]
        class Client:
            count=0;changed=False
            def account_id(self):self.count+=1;return 'OTHER' if self.changed and self.count>1 else 'SYNTHETIC'
            def request(self,method,path):
                calls.append((method,path));k=next(k for k,v in routes(b).items() if v==path);value=copy.deepcopy(raw[k])
                return value if b=='angel' else {{'holdings':'holdings','positions':'netPositions','orders':'orderBook','trades':'tradeBook'}[k]:value}
            def quotes(self,keys):return copy.deepcopy(raw['quotes'])
        self.assertTrue(fetch(Client(),b,'SYNTHETIC',self.opening,[self.i],clock=lambda:NOW)['report']['inventory_reconciled'])
        self.assertEqual(len(calls),8);c=Client();c.changed=True
        with self.assertRaises(Block):fetch(c,b,'SYNTHETIC',self.opening,[self.i],clock=lambda:NOW)
    def test_missing_review_or_stale_collection_blocks(self):
        self.opening['adjustments_reviewed']=False
        with self.assertRaises(Block):self.run_case()
        self.opening['adjustments_reviewed']=True;self.raw['started_at']=(NOW-timedelta(seconds=61)).isoformat()
        with self.assertRaises(Block):self.run_case()
    def test_cli_new_output(self):
        self.verify()
        with tempfile.TemporaryDirectory() as tmp,patch('sys.stdout',new_callable=io.StringIO):
            w=Path(tmp);write(w/'raw.json',self.raw);write(w/'opening.json',self.opening);write(w/'master.json',{'instruments':[self.i]})
            args=['--broker',self.broker,'--input',str(w/'raw.json'),'--opening',str(w/'opening.json'),'--master',str(w/'master.json'),
                  '--account-id','SYNTHETIC','--now',NOW.isoformat(),'--out',str(w/'out.json')]
            self.assertEqual(main(args),0);before=(w/'out.json').read_bytes();self.assertEqual(main(args),2)
            self.assertEqual(before,(w/'out.json').read_bytes());self.assertFalse(read(w/'out.json')['submitted'])


class AngelPortfolioTests(Cases,unittest.TestCase):broker='angel'
class FyersPortfolioTests(Cases,unittest.TestCase):broker='fyers'


if __name__=='__main__':unittest.main()
