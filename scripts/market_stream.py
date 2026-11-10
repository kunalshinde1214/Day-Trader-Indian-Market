"""Bounded multi-broker read-only market streaming; never submits orders."""
import argparse
from datetime import datetime
import json
import os
from pathlib import Path
import struct
import sys
import time
from urllib.parse import urlencode
from core import Block,IST,instant,integer,positive,fresh,fingerprint,validate_instrument
from service import ProcessLock
from trader import read,write

SUPPORTED={'dhan','angel','upstox','fyers'}
DHAN_SEGMENTS={'NSE':(1,'NSE_EQ'),'NFO':(2,'NSE_FNO'),'BSE':(4,'BSE_EQ'),'BFO':(8,'BSE_FNO')}
ANGEL_SEGMENTS={'NSE':1,'NFO':2,'BSE':3,'BFO':4}


def mapping(broker,instruments,now):
    if broker not in SUPPORTED or not isinstance(instruments,list) or not 1<=len(instruments)<=100:raise Block('STREAM: supported broker and 1..100 instruments required')
    result={}
    for i in instruments:
        validate_instrument(i,now)
        if i['broker']!=broker:raise Block('STREAM: broker mismatch')
        key=i['exchange']+':'+i['token']
        if key in result:raise Block('STREAM: duplicate instrument')
        result[key]=i
    return result


def quote(i,price,trade,exchange,now):
    at=datetime.fromtimestamp(integer(trade)/1000,IST);ex=datetime.fromtimestamp(integer(exchange)/1000,IST)
    if at>ex or ex>now or at.date()!=now.date():raise Block('STREAM: invalid trade/exchange timestamp')
    return {**{k:i[k] for k in ('broker','exchange','symbol','token')},'source':i['broker']+'_websocket',
            'price':float(positive(price)),'as_of':at.isoformat(),'exchange_as_of':ex.isoformat(),
            'received_at':now.isoformat(),'instrument_hash':fingerprint(i)}


def decode(broker,data,instruments,now):
    if broker in {'upstox','fyers'}:
        if not isinstance(data,list) or len(data)>1000:raise Block('STREAM: invalid SDK quote batch')
        result=[]
        for q in data:
            i=instruments.get(q['exchange']+':'+q['token'])
            if i is None or q.get('instrument_hash')!=fingerprint(i) or q.get('broker')!=broker or q.get('symbol')!=i['symbol']:raise Block('STREAM: SDK identity mismatch')
            result.append(quote(i,q['price'],int(instant(q['as_of']).timestamp()*1000),int(instant(q['exchange_as_of']).timestamp()*1000),now))
        return result
    if not isinstance(data,bytes) or not 1<=len(data)<=1048576:raise Block('STREAM: invalid frame')
    if broker=='dhan':
        if len(data)<8:raise Block('STREAM: short header')
        kind,length,segment,token=struct.unpack_from('<BHBI',data)
        if length!=len(data):raise Block('STREAM: packet length mismatch')
        exchange=next((e for e,v in DHAN_SEGMENTS.items() if v[0]==segment),None)
        i=instruments.get(str(exchange)+':'+str(token))
        if i is None:raise Block('STREAM: unrequested instrument')
        if kind==6 and len(data)==16:return []  # Prior close is not a fresh quote.
        if kind!=2 or len(data)!=16:raise Block('STREAM: ticker packet required')
        price,at=struct.unpack_from('<fI',data,8)
        return [{**quote(i,price,at*1000,at*1000,now),'timestamp_basis':'last trade; no separate exchange clock'}]
    if broker=='angel':
        if data==b'pong':return []
        if len(data)!=379 or data[0]!=3:raise Block('STREAM: full snap-quote packet required')
        exchange=next((e for e,v in ANGEL_SEGMENTS.items() if v==data[1]),None)
        token=data[2:27].split(b'\0',1)[0].decode('ascii')
        i=instruments.get(str(exchange)+':'+token)
        if i is None:raise Block('STREAM: unrequested instrument')
        sequence,exchange_at,price=struct.unpack_from('<qqq',data,27)
        at=struct.unpack_from('<q',data,123)[0]
        return [{**quote(i,positive(price)/100,at*1000,exchange_at,now),'sequence':integer(sequence,minimum=0)}]
    raise Block('STREAM: unsupported decoder')


def subscriptions(broker,instruments):
    if broker=='dhan':
        rows=[{'ExchangeSegment':DHAN_SEGMENTS[i['exchange']][1],'SecurityId':i['token']} for i in instruments.values()]
        return [{'RequestCode':15,'InstrumentCount':len(rows),'InstrumentList':rows}]
    groups={}
    for i in instruments.values():groups.setdefault(ANGEL_SEGMENTS[i['exchange']],[]).append(i['token'])
    return [{'correlationID':'indiaskill','action':1,'params':{'mode':3,'tokenList':[{'exchangeType':k,'tokens':v} for k,v in groups.items()]}}]


def connection(broker,instruments):
    if broker in {'upstox','fyers'}:
        from sdk_stream_worker import connect
        return connect(broker,instruments)
    try:import websocket
    except ImportError:raise Block('STREAM: install requirements-streaming.txt') from None
    websocket.enableTrace(False)
    def env(name):
        value=os.environ.get(name)
        if not value or any(c.isspace() for c in value):raise Block('STREAM: local credentials required')
        return value
    headers={}
    if broker=='dhan':url='wss://api-feed.dhan.co?'+urlencode({'version':2,'token':env('DHAN_ACCESS_TOKEN'),'clientId':env('DHAN_CLIENT_ID'),'authType':2})
    elif broker=='angel':
        url='wss://smartapisocket.angelone.in/smart-stream'
        headers={'Authorization':env('ANGEL_ACCESS_TOKEN'),'x-api-key':env('ANGEL_API_KEY'),'x-client-code':env('ANGEL_CLIENT_CODE'),'x-feed-token':env('ANGEL_FEED_TOKEN')}
    else:raise Block('STREAM: unsupported transport')
    ws=None
    try:
        ws=websocket.create_connection(url,header=headers,timeout=10,redirect_limit=0,suppress_origin=True,enable_multithread=True)
        if ws.getstatus()!=101:raise Block('upgrade')
        ws.settimeout(1)
        for message in subscriptions(broker,instruments):ws.send(json.dumps(message))
    except Exception:
        if ws:ws.close(timeout=1)
        raise Block('STREAM: connect/subscribe failed; private details suppressed') from None
    class Connection:
        last_ping=time.monotonic()
        def receive(self):
            try:
                if broker=='angel' and time.monotonic()-self.last_ping>=10:ws.send('ping');self.last_ping=time.monotonic()
                op,data=ws.recv_data(control_frame=True)
                if op in {9,10}:return None
                if op==1 and data in {b'pong','pong'}:return None
                if op!=2:raise Block('STREAM: provider closed/error/control response')
                return data
            except websocket.WebSocketTimeoutException:return None
            except Exception:raise Block('STREAM: receive failed; private details suppressed') from None
        def close(self):ws.close(timeout=1)
    return Connection()


def run(workdir,broker,instruments,*,duration=3600,retries=3,connector=connection,clock=instant,monotonic=time.monotonic,sleep=time.sleep):
    if type(duration)!=int or not 1<=duration<=43200 or type(retries)!=int or not 0<=retries<=5:raise Block('STREAM: invalid bounds')
    selected=mapping(broker,instruments,clock());w=Path(workdir);path=w/'state'/f'{broker}-stream.json';quotes={}
    attempts=received=0;started=monotonic();day=clock().date();reason='duration_limit'
    def persist(state):write(path,{'source':broker+'_websocket','as_of':clock().isoformat(),'state':state,'quotes':quotes,'connection_attempts':attempts,'received_quotes':received,'submitted':False})
    def stop():return (w/'state'/f'{broker}-stream-stop.json').exists()
    with ProcessLock(w/'state'/f'{broker}-stream.lock'):
        try:
            while monotonic()-started<duration:
                if stop():reason='requested_stop';break
                if clock().date()!=day:reason='session_rollover';break
                attempts+=1;quotes.clear();persist('connecting');client=None
                try:
                    client=connector(broker,selected);persist('connected');flushed=None
                    while monotonic()-started<duration and not stop() and clock().date()==day:
                        data=client.receive()
                        if data is not None:
                            for q in decode(broker,data,selected,clock()):
                                key=q['exchange']+':'+q['token'];previous=quotes.get(key)
                                if previous and any(instant(q[field])<instant(previous[field]) for field in ('exchange_as_of','as_of')):continue
                                quotes[key]=q;received+=1
                        if flushed is None or monotonic()-flushed>=0.5:persist('connected');flushed=monotonic()
                    if stop():reason='requested_stop'
                    elif clock().date()!=day:reason='session_rollover'
                    break
                except (Block,ValueError,TypeError,KeyError,OSError,OverflowError):
                    quotes.clear();persist('disconnected')
                    if attempts>retries:reason='retry_limit';break
                    remaining=duration-(monotonic()-started)
                    if remaining>0:sleep(min(2**attempts,30,remaining))
                finally:
                    if client:
                        try:client.close()
                        except Exception:pass
        finally:quotes.clear();persist('stopped')
    return {'reason':reason,'connection_attempts':attempts,'received_quotes':received,'orders_submitted':0}


def read_quote(workdir,instrument,now,max_age=60):
    if instrument['broker']=='kite':
        from streaming import read_quote as kite_quote
        return kite_quote(workdir,instrument,now,max_age)
    mapping(instrument['broker'],[instrument],now)
    snapshot=read(Path(workdir)/'state'/f"{instrument['broker']}-stream.json")
    fresh(snapshot['as_of'],now,5,'stream heartbeat')
    source=instrument['broker']+'_websocket'
    if snapshot.get('state')!='connected' or snapshot.get('source')!=source:raise Block('STREAM: connected matching source required')
    q=snapshot['quotes'][instrument['exchange']+':'+instrument['token']]
    if q.get('instrument_hash')!=fingerprint(instrument) or q.get('source')!=source or any(q.get(k)!=instrument[k] for k in ('broker','exchange','symbol','token')):raise Block('STREAM: quote identity changed')
    for field in ('as_of','exchange_as_of','received_at'):fresh(q[field],now,max_age,'stream quote')
    positive(q['price']);return q


def main(argv=None):
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('command',choices=['run','stop','clear-stop','status'])
    p.add_argument('--broker',choices=sorted(SUPPORTED),required=True);p.add_argument('--workdir',required=True);p.add_argument('--master')
    p.add_argument('--duration',type=int,default=3600);p.add_argument('--retries',type=int,default=3);a=p.parse_args(argv);w=Path(a.workdir)
    try:
        if a.command=='run':result=run(w,a.broker,read(a.master)['instruments'],duration=a.duration,retries=a.retries)
        elif a.command=='stop':write(w/'state'/f'{a.broker}-stream-stop.json',{'as_of':instant().isoformat()});result={'stop_requested':True}
        elif a.command=='clear-stop':
            with ProcessLock(w/'state'/f'{a.broker}-stream.lock'):(w/'state'/f'{a.broker}-stream-stop.json').unlink(missing_ok=True)
            result={'stop_latch_cleared':True}
        else:
            result=read(w/'state'/f'{a.broker}-stream.json');result.pop('quotes',None)
            try:fresh(result['as_of'],instant(),5,'stream heartbeat')
            except Block:result['state']='stale'
            result['process_liveness_verified']=False
        print(json.dumps(result));return 2 if result.get('reason')=='retry_limit' else 0
    except (Block,ValueError,TypeError,KeyError,OSError,OverflowError,KeyboardInterrupt):print('{"status":"BLOCK_OR_STOPPED","submitted":false}');return 2


if __name__=='__main__':sys.exit(main())
