"""Read-only Upstox carry collection against reviewed opening evidence; no orders."""
import argparse
from collections import defaultdict
from datetime import datetime
import json
from pathlib import Path
import sqlite3
import sys

from broker_data import normalize_quote
from core import Block, IST, fingerprint, fresh, instant, integer, positive
from journal import Journal
from live_state import match_instrument, normalize_order
from portfolio import FIELDS, array, evidence, item_key, quantity, validate_opening, save_opening, reconcile_portfolio
from trade_import import normalize_trades, trade_product, trade_time
from trader import read

ROUTES={'orders':'/v2/order/retrieve-all','trades':'/v2/order/trades/get-trades-for-day',
        'holdings':'/v2/portfolio/long-term-holdings','positions':'/v2/portfolio/short-term-positions',
        'mtf_positions':'/v3/portfolio/mtf-positions'}
HOLDING_FIELDS=('exchange','instrument_token','trading_symbol','product','isin','quantity','cnc_used_quantity',
                't1_quantity','collateral_quantity','collateral_update_quantity')
POSITION_FIELDS=('exchange','instrument_token','trading_symbol','product','quantity','overnight_quantity',
                 'overnight_buy_quantity','overnight_sell_quantity','day_buy_quantity','day_sell_quantity','multiplier')


def stable_book(book):
    def rows(name,fields=None):
        return sorted(fingerprint({k:r[k] for k in fields} if fields else r) for r in array(book[name]))
    # MTF is a separate endpoint: never silently omit funded exposure.
    if array(book['mtf_positions']):raise Block('UPSTOX_PORTFOLIO: MTF inventory unsupported')
    return {'orders':rows('orders'),'trades':rows('trades'),'holdings':rows('holdings',HOLDING_FIELDS),
            'positions':rows('positions',POSITION_FIELDS),'mtf_positions':[]}


def normalize(raw,opening,instruments,now):
    day=now.date().isoformat();pair,master=validate_opening(opening,instruments,now)
    evidence(raw,now,day,pair)
    if pair[0]!='upstox' or raw.get('schema')!='upstox_portfolio_read_v1' or raw.get('source')!='upstox':
        raise Block('UPSTOX_PORTFOLIO: complete Upstox source required')
    start,end=instant(raw['started_at']),instant(raw['as_of'])
    fresh(raw['started_at'],now,60,'collection start')
    if start.date()!=now.date() or start>end or instant(opening['as_of'])>start:
        raise Block('UPSTOX_PORTFOLIO: invalid collection window')
    if stable_book(raw)!=stable_book(raw['verification']):raise Block('UPSTOX_PORTFOLIO: account changed; refresh')

    def key_for(row):
        if 'user_id' in row and row['user_id']!=pair[1]:raise Block('UPSTOX_PORTFOLIO: foreign account row')
        ident=match_instrument('upstox',row,instruments)
        i=master[tuple(ident[k] for k in FIELDS[:3])]
        product=trade_product('upstox',row,True,instrument=i)
        return item_key({**ident,'product':product},master)

    orders=[]
    for row in array(raw['orders']):
        key=key_for(row)
        at=trade_time(row['order_timestamp'],day,'upstox')
        if at>end:raise Block('UPSTOX_PORTFOLIO: future order')
        # Reuse order-shape validation only after separately validating the actual
        # accounting product. The execution normalizer itself stays intraday-only.
        order=normalize_order('upstox',{**row,'product':'I'},instruments)
        order['product']=key[3]
        if order['status']=='UNKNOWN':raise Block('UPSTOX_PORTFOLIO: ambiguous order')
        orders.append(order)
    header={'broker':'upstox','account_id':pair[1],'date':day,'source':'upstox','as_of':raw['as_of'],'complete':True}
    snapshot={**header,'schema':'portfolio_snapshot_v1','inventory_events':[],'orders':orders,'positions':[]}
    batch=normalize_trades(raw,snapshot,instruments,now,allow_carry=True)
    opens={tuple(r[k] for k in FIELDS):r for r in opening['positions']}
    traded=defaultdict(lambda:{'BUY':0,'SELL':0})
    for fill in batch['fills']:traded[tuple(fill[k] for k in FIELDS)][fill['side']]+=fill['quantity']
    holdings={};isins=set()
    for row in array(raw['holdings']):
        key=key_for(row);isin=row['isin']
        if key[3]!='CNC' or key in holdings or not isinstance(isin,str) or not isin or isin in isins:
            raise Block('UPSTOX_PORTFOLIO: duplicate or unsupported holding')
        isins.add(isin)
        if any(integer(row[k],minimum=0) for k in ('t1_quantity','collateral_quantity','collateral_update_quantity')):
            raise Block('UPSTOX_PORTFOLIO: unsettled/pledged inventory unsupported')
        held=integer(row['quantity'],minimum=0);used=integer(row['cnc_used_quantity'],minimum=0)
        reserved=sum(o['quantity']-o['filled_quantity'] for o in orders
                     if tuple(o[k] for k in FIELDS)==key and o['side']=='SELL' and o['status'] in {'OPEN','PARTIAL'})
        if held!=quantity(opens.get(key,{}).get('quantity',0)) or used>held or used>traded[key]['SELL']+reserved:
            raise Block('UPSTOX_PORTFOLIO: total holding/opening mismatch; no overlap inference')
        # cnc_used_quantity includes reservations for unfilled orders. It is not
        # an execution and must never be deducted as a second sale.
        holdings[key]=held
    positions={}
    for row in array(raw['positions']):
        key=key_for(row)
        if key in positions or positive(row['multiplier'])!=1:raise Block('UPSTOX_PORTFOLIO: duplicate/non-unit position')
        buys,sells=(integer(row[k],minimum=0) for k in ('day_buy_quantity','day_sell_quantity'))
        overnight=quantity(row['overnight_quantity']);q=quantity(row['quantity'])
        overnight_buy,overnight_sell=(integer(row[k],minimum=0) for k in ('overnight_buy_quantity','overnight_sell_quantity'))
        expected=quantity(opens.get(key,{}).get('quantity',0)) if key[3]=='NRML' else 0
        if (buys,sells)!=(traded[key]['BUY'],traded[key]['SELL']) or overnight!=expected or q!=overnight+buys-sells:
            raise Block('UPSTOX_PORTFOLIO: position/opening/trades disagree')
        if overnight_buy-overnight_sell!=overnight or overnight_buy and overnight_sell:
            raise Block('UPSTOX_PORTFOLIO: ambiguous overnight inventory')
        positions[key]=q
    if any(key not in positions for key in traded if any(traded[key].values())):
        raise Block('UPSTOX_PORTFOLIO: missing traded position')
    for key in opens:
        if key[3]=='CNC' and key not in holdings or key[3]=='NRML' and key not in positions:
            raise Block('UPSTOX_PORTFOLIO: opening inventory absent')
    for key in sorted(holdings.keys()|positions.keys()):
        q=holdings.get(key,0)+positions.get(key,0) if key[3]=='CNC' else positions[key]
        row={**dict(zip(FIELDS,key)),'quantity':q}
        if q:
            quote=normalize_quote(raw['quotes'],master[key[:3]],now,60)
            at=datetime.fromtimestamp(integer(quote['last_trade_time'])/1000,IST)
            fresh(at.isoformat(),now,60,'portfolio last trade')
            if quote.get('source')!='upstox' or at.date()!=now.date() or at>instant(quote['as_of']) or instant(quote['as_of'])>end:
                raise Block('UPSTOX_PORTFOLIO: invalid quote clock/source')
            row.update(mark_price=float(positive(quote['price'])),mark_source='upstox_quote',mark_as_of=at.isoformat(),price_basis='per_unit_inr')
        snapshot['positions'].append(row)
    journal=Journal(':memory:')
    try:
        save_opening(journal,opening,instruments,now)
        journal.import_fills(batch,now,allow_carry=True)
        report=reconcile_portfolio(journal,snapshot,instruments,now)
    finally:journal.close()
    if not report['inventory_reconciled']:raise Block('UPSTOX_PORTFOLIO: inventory did not reconcile')
    return {'schema':'portfolio_collection_v1','opening':opening,'snapshot':snapshot,'fills':batch,'report':report,
            'execution_orders':[dict(row) for row in orders],
            'raw_hash':fingerprint(raw),'opening_hash':fingerprint(opening),'execution_eligible':False,
            'submitted':False,'authenticated_acceptance':False}


def fetch(client,account_id,opening,instruments,clock=instant):
    started=clock()
    if validate_opening(opening,instruments,started)[0]!=('upstox',account_id) or client.account_id()!=account_id:
        raise Block('UPSTOX_PORTFOLIO: connected/opening account mismatch')
    book={k:client.request('GET',route) for k,route in ROUTES.items()};stable_book(book)
    tokens=sorted({match_instrument('upstox',r,instruments)['token'] for r in book['holdings']+book['positions']})
    quotes={'source':'upstox','quotes':{},'missing':[]}
    for offset in range(0,len(tokens),500):
        result=client.quotes(tokens[offset:offset+500])
        if result.get('source')!='upstox':raise Block('UPSTOX_PORTFOLIO: wrong quote source')
        quotes['quotes'].update(result['quotes']);quotes['missing'].extend(result['missing'])
    verification={k:client.request('GET',route) for k,route in ROUTES.items()}
    if client.account_id()!=account_id:raise Block('UPSTOX_PORTFOLIO: account changed')
    ended=clock()
    raw={**book,'verification':verification,'quotes':quotes,'schema':'upstox_portfolio_read_v1','broker':'upstox',
         'account_id':account_id,'source':'upstox','date':ended.date().isoformat(),'started_at':started.isoformat(),
         'as_of':ended.isoformat(),'complete':True}
    return normalize(raw,opening,instruments,ended)


def main(argv=None):
    p=argparse.ArgumentParser(description=__doc__)
    for name in ('account-id','opening','master','out'):p.add_argument('--'+name,required=True)
    mode=p.add_mutually_exclusive_group(required=True);mode.add_argument('--fetch',action='store_true');mode.add_argument('--input')
    p.add_argument('--now');a=p.parse_args(argv)
    try:
        out=Path(a.out)
        if out.exists() or a.fetch and a.now:raise Block('UPSTOX_PORTFOLIO: new output/live clock required')
        opening,instruments=read(a.opening),read(a.master)['instruments']
        if opening.get('account_id')!=a.account_id:raise Block('UPSTOX_PORTFOLIO: selected account mismatch')
        if a.fetch:
            from upstox_readonly import UpstoxReadOnly
            result=fetch(UpstoxReadOnly(),a.account_id,opening,instruments)
        else:result=normalize(read(a.input),opening,instruments,instant(a.now))
        out.parent.mkdir(parents=True,exist_ok=True)
        with out.open('x',encoding='utf-8') as stream:json.dump(result,stream,indent=2,allow_nan=False)
        print(json.dumps({'status':'saved','report_status':result['report']['status'],'submitted':False}));return 0
    except (Block,ValueError,KeyError,TypeError,OSError,OverflowError,sqlite3.Error):
        print('{"status":"BLOCK","reason":"Upstox portfolio evidence failed validation; private response suppressed","submitted":false}');return 2


if __name__=='__main__':sys.exit(main())
