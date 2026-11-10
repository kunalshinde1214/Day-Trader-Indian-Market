"""Optional official SDK streams in a terminable child process; no trading API calls."""
import logging
import multiprocessing
import os
import ssl
from pathlib import Path
import tempfile
import threading
import time
from core import Block,instant,integer
from market_stream import quote
from fyers_readonly import full_symbol


def secure_websocket(module):
    """Override SDK TLS options only inside this dedicated child process."""
    original=module.WebSocketApp.run_forever
    def verified(self,*args,**kwargs):
        # SDKs call this with keyword arguments. Reject positional TLS overrides.
        if args:raise Block('SDK: positional transport options unsupported')
        if not str(self.url).startswith('wss://'):raise Block('SDK: TLS required')
        kwargs['sslopt']={'context':ssl.create_default_context(),
                          'cert_reqs':ssl.CERT_REQUIRED,'check_hostname':True}
        return original(self,**kwargs)
    module.WebSocketApp.run_forever=verified


def quotes(broker,message,instruments,now):
    if not isinstance(message,dict):raise Block('SDK: object required')
    if broker=='upstox':
        if message.get('type')=='market_info':return []
        if message.get('type') not in {'initial_feed','live_feed'}:raise Block('SDK: unknown feed message')
        selected={i['token']:i for i in instruments.values()};result=[]
        for key,value in message['feeds'].items():
            if key not in selected:raise Block('SDK: unrequested key')
            ltpc=value['ltpc']
            result.append(quote(selected[key],ltpc['ltp'],integer(ltpc['ltt']),integer(message['currentTs']),now))
        return result
    if broker=='fyers':
        if message.get('type') in {'cn','ful','sub','unsub','lit'}:
            if message.get('s')!='ok':raise Block('SDK: control failure')
            return []
        if message.get('type')!='sf':raise Block('SDK: full tradable symbol update required')
        selected={full_symbol(i['symbol'],i['exchange']):i for i in instruments.values()}
        i=selected.get(message['symbol'])
        if i is None:raise Block('SDK: unrequested symbol')
        at=integer(message['last_traded_time'])*1000
        return [quote(i,message['ltp'],at,at,now)]
    raise Block('SDK: unsupported broker')


def worker(broker,instruments,pipe,directory):
    # SDK stdout/stderr and logs must never expose a token to the parent or chat.
    os.chdir(directory)
    with open(os.devnull,'w') as sink:
        os.dup2(sink.fileno(),1);os.dup2(sink.fileno(),2)
    logging.disable(logging.CRITICAL)
    lock=threading.Lock();failed=threading.Event()
    def emit(value):
        with lock:pipe.send(value)
    def error(*args,**kwargs):
        if not failed.is_set():failed.set();emit({'error':True})
    def message(value):
        try:
            result=quotes(broker,value,instruments,instant())
            if result:emit({'quotes':result})
        except Exception:error()
    def env(name):
        value=os.environ.get(name)
        if not value or any(c.isspace() for c in value):raise Block('credentials')
        return value
    try:
        import websocket
        websocket.enableTrace(False)
        secure_websocket(websocket)
        if broker=='upstox':
            import upstox_client
            config=upstox_client.Configuration();config.access_token=env('UPSTOX_ACCESS_TOKEN')
            config.debug=False
            client=upstox_client.MarketDataStreamerV3(upstox_client.ApiClient(config),[i['token'] for i in instruments.values()],'ltpc')
            client.auto_reconnect(False)
            client.on('open',lambda:emit({'ready':True}));client.on('message',message)
            client.on('error',error);client.on('close',error)
        elif broker=='fyers':
            from fyers_apiv3.FyersWebsocket import data_ws
            def opened():
                try:client.subscribe(symbols=[full_symbol(i['symbol'],i['exchange']) for i in instruments.values()],data_type='SymbolUpdate');emit({'ready':True})
                except Exception:error()
            client=data_ws.FyersDataSocket(access_token=env('FYERS_APP_ID')+':'+env('FYERS_ACCESS_TOKEN'),log_path=directory,
                litemode=False,write_to_file=False,reconnect=False,on_connect=opened,on_message=message,on_error=error,on_close=error)
        else:raise Block('broker')
        client.connect()
        while not failed.wait(1):pass
    except Exception:error()
    finally:pipe.close()


def connect(broker,instruments):
    ctx=multiprocessing.get_context('spawn');parent,child=ctx.Pipe(duplex=False)
    temp=tempfile.TemporaryDirectory(prefix='india-stream-sdk-')
    process=ctx.Process(target=worker,args=(broker,instruments,child,temp.name),daemon=True)
    class Connection:
        def receive(self):
            if not parent.poll(1):
                if not process.is_alive():raise Block('SDK: worker stopped')
                return None
            try:msg=parent.recv()
            except (EOFError,OSError):raise Block('SDK: worker stopped') from None
            if msg.get('error'):raise Block('SDK: unavailable; private details suppressed')
            if msg.get('ready'):return None
            return msg['quotes']
        def close(self):
            if process.pid is not None:
                if process.is_alive():process.terminate()
                process.join(timeout=3)
                if process.is_alive():process.kill();process.join(timeout=3)
            parent.close();child.close();temp.cleanup()
    connection=Connection()
    try:
        process.start();child.close()
        if not parent.poll(15) or parent.recv().get('ready') is not True:raise Block('SDK: connection unavailable; install optional SDK and configure credentials locally')
        return connection
    except Exception:
        connection.close()
        raise Block('SDK: connection failed; private details suppressed') from None
