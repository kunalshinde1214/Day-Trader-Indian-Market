import copy
from datetime import timedelta
import json
from pathlib import Path
import struct
import sys
import tempfile
import unittest
import ssl
from types import SimpleNamespace
from unittest.mock import patch
ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT/'scripts'))
from core import Block,instant
from market_stream import mapping,decode,subscriptions,run,read_quote,connection
from trader import read,write
from sdk_stream_worker import quotes,secure_websocket
NOW=instant('2026-11-10T09:35:00+05:30')
def instrument(broker):return {'broker':broker,'exchange':'NSE','symbol':'DEMO','token':'123','kind':'equity','lot_size':1,'tick_size':.05,'master_date':NOW.date().isoformat()}
def packet(broker):
    if broker=='dhan':return struct.pack('<BHBIfI',2,16,1,123,123.5,int(NOW.timestamp()))
    b=bytearray(379);b[0]=3;b[1]=1;b[2:5]=b'123';struct.pack_into('<qqq',b,27,1,int(NOW.timestamp()*1000),12350);struct.pack_into('<q',b,123,int(NOW.timestamp()));return bytes(b)
class MarketStreamTests(unittest.TestCase):
    def test_runtime_rejects_cross_broker_feed_and_instrument(self):
        from trader import runtime_context
        with tempfile.TemporaryDirectory() as tmp:
            w=Path(tmp);write(w/'instrument.json',instrument('fyers'))
            for config in ({'quote_feed':'dhan_stream','broker':'dhan'},{'quote_feed':'fyers_stream','broker':'dhan'}):
                write(w/'config.json',{**config,'snapshot_max_age_seconds':60})
                with self.assertRaises(Block):runtime_context(w,{})
    def test_sdk_transport_overrides_insecure_tls(self):
        class App:
            url='wss://example.invalid/feed'
            def run_forever(self,**kwargs):return kwargs
        secure_websocket(SimpleNamespace(WebSocketApp=App))
        options=App().run_forever(sslopt={'cert_reqs':ssl.CERT_NONE,'check_hostname':False})['sslopt']
        self.assertEqual(options['cert_reqs'],ssl.CERT_REQUIRED)
        self.assertTrue(options['check_hostname'])
        self.assertTrue(options['context'].check_hostname)
        App.url='ws://example.invalid/feed'
        with self.assertRaises(Block):App().run_forever()
    def test_raw_transport_subscribes_and_suppresses_private_errors(self):
        class Socket:
            def __init__(self):self.sent=[];self.closed=False
            def getstatus(self):return 101
            def settimeout(self,value):self.timeout=value
            def send(self,value):self.sent.append(value)
            def recv_data(self,**kwargs):raise OSError('secret-token')
            def close(self,**kwargs):self.closed=True
        ws=Socket();calls=[]
        def connect(url,**kwargs):calls.append((url,kwargs));return ws
        module=SimpleNamespace(enableTrace=lambda x:None,create_connection=connect,WebSocketTimeoutException=TimeoutError)
        with patch.dict(sys.modules,{'websocket':module}),patch.dict('os.environ',{'DHAN_ACCESS_TOKEN':'synthetic','DHAN_CLIENT_ID':'test'}):
            c=connection('dhan',mapping('dhan',[instrument('dhan')],NOW))
            self.assertEqual(json.loads(ws.sent[0])['RequestCode'],15)
            self.assertEqual(calls[0][1]['redirect_limit'],0)
            self.assertNotIn('sslopt',calls[0][1])
            with self.assertRaises(Block) as error:c.receive()
            self.assertNotIn('secret-token',str(error.exception))
            c.close();self.assertTrue(ws.closed)
    def test_deployment_routes_start_and_stop_to_selected_broker(self):
        from deployment import render
        for broker in ('kite','upstox','dhan','angel','fyers'):
            spec={'schema':'readonly_deployment_v1','target':'systemd','name':'test-stream','python':'/usr/bin/python',
                  'skill_dir':'/opt/india','workdir':'/home/user/trader','component':'stream','master':'/home/user/master.json',
                  'duration':60,'retries':0,'broker':broker}
            files=render(spec);actions=json.loads(files['operator-actions.json'])['commands']
            script='streaming.py' if broker=='kite' else 'market_stream.py'
            self.assertIn(script,files['test-stream.service']);self.assertIn(script,actions['stop_latch'][1])
            if broker!='kite':self.assertEqual(actions['stop_latch'][3:5],['--broker',broker])
        spec['broker']='unknown'
        with self.assertRaises(Block):render(spec)
    def test_upstox_protobuf_json_preserves_trade_and_exchange_time(self):
        i={**instrument('upstox'),'token':'NSE_EQ|DEMO00000001'};m=mapping('upstox',[i],NOW)
        raw={'type':'live_feed','currentTs':str(int(NOW.timestamp()*1000)),'feeds':{i['token']:{'ltpc':{'ltp':123.5,'ltt':str(int(NOW.timestamp()*1000))}}}}
        q=quotes('upstox',raw,m,NOW)
        self.assertEqual(decode('upstox',q,m,NOW)[0]['price'],123.5)
        self.assertEqual(quotes('upstox',{'type':'market_info'},m,NOW),[])
    def test_fyers_full_symbol_maps_derivative_exchange(self):
        i={**instrument('fyers'),'exchange':'NFO','symbol':'DEMO26NOVFUT','token':'10110000999999','kind':'future','expiry':'2026-11-26','underlying_type':'index'}
        m=mapping('fyers',[i],NOW)
        q=quotes('fyers',{'type':'sf','symbol':'NSE:DEMO26NOVFUT','ltp':123.5,'last_traded_time':int(NOW.timestamp())},m,NOW)
        self.assertEqual(q[0]['exchange'],'NFO');self.assertEqual(decode('fyers',q,m,NOW)[0]['price'],123.5)
    def test_sdk_missing_trade_time_and_unrequested_symbol_rejected(self):
        i=instrument('fyers');m=mapping('fyers',[i],NOW)
        for raw in ({'type':'sf','symbol':'NSE:OTHER','ltp':100,'last_traded_time':int(NOW.timestamp())},{'type':'sf','symbol':'NSE:DEMO','ltp':100}):
            with self.assertRaises((Block,KeyError)):quotes('fyers',raw,m,NOW)
    def test_sdk_quote_tampering_blocked_in_parent(self):
        i=instrument('fyers');m=mapping('fyers',[i],NOW)
        q=quotes('fyers',{'type':'sf','symbol':'NSE:DEMO','ltp':123,'last_traded_time':int(NOW.timestamp())},m,NOW)
        q[0]['instrument_hash']='wrong'
        with self.assertRaises(Block):decode('fyers',q,m,NOW)
    def test_binary_quotes_for_both_brokers(self):
        for broker in ('dhan','angel'):
            i=instrument(broker);q=decode(broker,packet(broker),mapping(broker,[i],NOW),NOW)[0]
            self.assertEqual(q['price'],123.5);self.assertEqual(q['as_of'],NOW.isoformat());self.assertEqual(q['broker'],broker)
    def test_packet_length_and_identity(self):
        for broker in ('dhan','angel'):
            m=mapping(broker,[instrument(broker)],NOW)
            for b in (b'',packet(broker)[:-1],packet(broker)+b'x'):
                with self.assertRaises(Block):decode(broker,b,m,NOW)
            with self.assertRaises(Block):decode(broker,packet(broker),{},NOW)
    def test_future_prices_and_prior_day_rejected(self):
        for broker in ('dhan','angel'):
            m=mapping(broker,[instrument(broker)],NOW)
            for now in (NOW-timedelta(seconds=1),NOW+timedelta(days=1)):
                with self.assertRaises(Block):decode(broker,packet(broker),m,now)
    def test_master_identity_and_duplicates(self):
        for broker in ('dhan','angel'):
            for rows in ([],[instrument(broker)]*2,[instrument('kite')],[{**instrument(broker),'master_date':'2026-11-09'}]):
                with self.assertRaises(Block):mapping(broker,rows,NOW)
    def test_subscriptions_are_market_only(self):
        d=subscriptions('dhan',mapping('dhan',[instrument('dhan')],NOW))[0]
        self.assertEqual(d['RequestCode'],15);self.assertEqual(d['InstrumentList'][0]['ExchangeSegment'],'NSE_EQ')
        a=subscriptions('angel',mapping('angel',[instrument('angel')],NOW))[0]
        self.assertEqual(a['params']['mode'],3);self.assertEqual(a['action'],1)
    def test_prior_close_never_becomes_current_quote(self):
        raw=bytearray(packet('dhan'));raw[0]=6
        self.assertEqual(decode('dhan',bytes(raw),mapping('dhan',[instrument('dhan')],NOW),NOW),[])
    def test_snapshot_requires_identity_and_fresh_trade(self):
        with tempfile.TemporaryDirectory() as tmp:
            i=instrument('dhan');q=decode('dhan',packet('dhan'),mapping('dhan',[i],NOW),NOW)[0]
            data={'source':'dhan_websocket','as_of':NOW.isoformat(),'state':'connected','quotes':{'NSE:123':q}}
            path=Path(tmp)/'state/dhan-stream.json';write(path,data)
            self.assertEqual(read_quote(tmp,i,NOW)['price'],123.5)
            q['as_of']=(NOW-timedelta(seconds=61)).isoformat();write(path,data)
            with self.assertRaises(Block):read_quote(tmp,i,NOW)
    def test_runner_stops_and_clears_quotes(self):
        with tempfile.TemporaryDirectory() as tmp:
            class Client:
                closed=False
                def receive(self):write(Path(tmp)/'state/dhan-stream-stop.json',{});return packet('dhan')
                def close(self):self.closed=True
            c=Client();r=run(tmp,'dhan',[instrument('dhan')],connector=lambda b,i:c,clock=lambda:NOW,duration=5)
            self.assertEqual(r['reason'],'requested_stop');self.assertEqual(r['received_quotes'],1);self.assertTrue(c.closed)
            self.assertEqual(read(Path(tmp)/'state/dhan-stream.json')['quotes'],{})
    def test_retry_limit_and_preexisting_stop(self):
        with tempfile.TemporaryDirectory() as tmp:
            def fail(b,i):raise Block('network')
            r=run(tmp,'dhan',[instrument('dhan')],connector=fail,clock=lambda:NOW,retries=0)
            self.assertEqual(r['reason'],'retry_limit');self.assertEqual(r['connection_attempts'],1)
            write(Path(tmp)/'state/dhan-stream-stop.json',{})
            self.assertEqual(run(tmp,'dhan',[instrument('dhan')],connector=fail,clock=lambda:NOW)['connection_attempts'],0)
