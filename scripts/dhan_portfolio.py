"""Read-only Dhan carry accounting from reviewed opening evidence; no trading."""
import argparse
from collections import defaultdict
import json
from pathlib import Path
import sqlite3
import sys

from broker_data import normalize_quote, quote_key
from core import Block, fingerprint, fresh, instant, integer, number, positive
from journal import Journal
from live_state import match_instrument, normalize_order
from portfolio import FIELDS, array, evidence, item_key, quantity, validate_opening, save_opening, reconcile_portfolio
from trade_import import normalize_trades, trade_product, trade_time
from trader import read

ROUTES={'orders':'/orders','trades':'/trades','holdings':'/holdings','positions':'/positions'}
HOLDING_FIELDS=('exchange','tradingSymbol','securityId','isin','totalQty','dpQty','t1Qty','availableQty','collateralQty')
POSITION_FIELDS=('dhanClientId','tradingSymbol','securityId','exchangeSegment','productType','positionType',
                 'netQty','buyQty','sellQty','carryForwardBuyQty','carryForwardSellQty','dayBuyQty','daySellQty','multiplier','crossCurrency')


def stable_book(book):
    def rows(name,fields=None):
        values=[]
        for r in array(book[name]):
            value={k:r[k] for k in fields} if fields else r
            if name=='holdings':value={**value,**{k:r[k] for k in ('productType','dhanClientId') if k in r}}
            values.append(fingerprint(value))
        return sorted(values)
    return {'orders':rows('orders'),'trades':rows('trades'),'holdings':rows('holdings',HOLDING_FIELDS),
            'positions':rows('positions',POSITION_FIELDS)}


def holding_instrument(row,instruments):
    exchange=row['exchange']
    if exchange not in {'ALL','NSE','BSE'}:raise Block('DHAN_PORTFOLIO: unsupported holding exchange')
    matches=[i for i in instruments if i['broker']=='dhan' and i['kind']=='equity' and i['token']==str(row['securityId'])
             and i['symbol']==row['tradingSymbol'] and (exchange=='ALL' or i['exchange']==exchange)]
    if len(matches)!=1:raise Block('DHAN_PORTFOLIO: missing/ambiguous holding identity')
    return matches[0]


def normalize(raw,opening,instruments,now):
    day=now.date().isoformat();pair,master=validate_opening(opening,instruments,now)
    evidence(raw,now,day,pair)
    if pair[0]!='dhan' or raw.get('schema')!='dhan_portfolio_read_v1' or raw.get('source')!='dhan':
        raise Block('DHAN_PORTFOLIO: complete Dhan source required')
    start,end=instant(raw['started_at']),instant(raw['as_of'])
    fresh(raw['started_at'],now,60,'collection start')
    if start.date()!=now.date() or start>end or instant(opening['as_of'])>start:
        raise Block('DHAN_PORTFOLIO: invalid collection window')
    if stable_book(raw)!=stable_book(raw['verification']):raise Block('DHAN_PORTFOLIO: account changed; refresh')

    def key_for(row):
        if row['dhanClientId']!=pair[1]:raise Block('DHAN_PORTFOLIO: foreign account row')
        ident=match_instrument('dhan',row,instruments);i=master[tuple(ident[k] for k in FIELDS[:3])]
        return item_key({**ident,'product':trade_product('dhan',row,True,instrument=i)},master)

    orders=[]
    for row in array(raw['orders']):
        key=key_for(row)
        if trade_time(row['createTime'],day,'dhan')>end:raise Block('DHAN_PORTFOLIO: future order')
        if row.get('legName') not in {None,''} or any(number(row.get(k,0)) for k in ('boProfitValue','boStopLossValue')):
            raise Block('DHAN_PORTFOLIO: linked/bracket order unsupported')
        # Actual accounting product is validated above; live execution remains MIS only.
        order=normalize_order('dhan',{**row,'productType':'INTRADAY'},instruments);order['product']=key[3]
        if order['status']=='UNKNOWN':raise Block('DHAN_PORTFOLIO: ambiguous order')
        orders.append(order)
    header={'broker':'dhan','account_id':pair[1],'source':'dhan','date':day,'as_of':raw['as_of'],'complete':True}
    snapshot={**header,'schema':'portfolio_snapshot_v1','inventory_events':[],'orders':orders,'positions':[]}
    for row in array(raw['trades']):key_for(row)
    batch=normalize_trades(raw,snapshot,instruments,now,allow_carry=True)
    opens={tuple(r[k] for k in FIELDS):r for r in opening['positions']}
    traded=defaultdict(lambda:{'BUY':0,'SELL':0})
    for fill in batch['fills']:traded[tuple(fill[k] for k in FIELDS)][fill['side']]+=fill['quantity']
    holdings={};isins=set()
    for row in array(raw['holdings']):
        i=holding_instrument(row,instruments);key=item_key({**i,'product':'CNC'},master);isin=row['isin']
        if key in holdings or not isinstance(isin,str) or not isin or isin in isins:
            raise Block('DHAN_PORTFOLIO: duplicate/invalid holding')
        isins.add(isin)
        if 'dhanClientId' in row and row['dhanClientId']!=pair[1]:raise Block('DHAN_PORTFOLIO: foreign holding')
        if row.get('productType','CNC')!='CNC' or integer(row['t1Qty'],minimum=0) or integer(row['collateralQty'],minimum=0):
            raise Block('DHAN_PORTFOLIO: unsupported funded/unsettled/pledged holding')
        total,dp,available=(integer(row[k],minimum=0) for k in ('totalQty','dpQty','availableQty'))
        if total!=dp or total!=quantity(opens.get(key,{}).get('quantity',0)) or available>total:
            raise Block('DHAN_PORTFOLIO: total holding/opening mismatch; no overlap inference')
        # Availability is an authorization/reservation quantity, not a fill.
        holdings[key]=total
    positions={}
    for row in array(raw['positions']):
        key=key_for(row)
        if key in positions or positive(row['multiplier'])!=1 or row['crossCurrency'] is not False:
            raise Block('DHAN_PORTFOLIO: duplicate/non-unit/foreign-currency position')
        buys,sells=(integer(row[k],minimum=0) for k in ('dayBuyQty','daySellQty'))
        carried_buy,carried_sell=(integer(row[k],minimum=0) for k in ('carryForwardBuyQty','carryForwardSellQty'))
        carried=carried_buy-carried_sell;q=quantity(row['netQty'])
        expected=quantity(opens.get(key,{}).get('quantity',0)) if key[3]=='NRML' else 0
        if carried_buy and carried_sell or carried!=expected or (buys,sells)!=(traded[key]['BUY'],traded[key]['SELL']):
            raise Block('DHAN_PORTFOLIO: carry/day activity mismatch')
        if (integer(row['buyQty'],minimum=0),integer(row['sellQty'],minimum=0))!=(carried_buy+buys,carried_sell+sells) or q!=carried+buys-sells:
            raise Block('DHAN_PORTFOLIO: position totals disagree')
        if row['positionType']!=('LONG' if q>0 else 'SHORT' if q<0 else 'CLOSED'):
            raise Block('DHAN_PORTFOLIO: contradictory position direction')
        positions[key]=q
    if any(key not in positions for key in traded if any(traded[key].values())):
        raise Block('DHAN_PORTFOLIO: missing traded position')
    for key in opens:
        if key[3]=='CNC' and key not in holdings or key[3]=='NRML' and key not in positions:
            raise Block('DHAN_PORTFOLIO: opening inventory absent')
    for key in sorted(holdings.keys()|positions.keys()):
        q=holdings.get(key,0)+positions.get(key,0) if key[3]=='CNC' else positions[key]
        row={**dict(zip(FIELDS,key)),'quantity':q}
        if q:
            quote=normalize_quote(raw['quotes'],master[key[:3]],now,60);at=instant(quote['as_of'])
            if quote.get('source')!='dhan' or at.date()!=now.date() or at>end:raise Block('DHAN_PORTFOLIO: invalid quote clock/source')
            row.update(mark_price=float(positive(quote['price'])),mark_source='dhan_quote',mark_as_of=at.isoformat(),price_basis='per_unit_inr')
        snapshot['positions'].append(row)
    journal=Journal(':memory:')
    try:
        save_opening(journal,opening,instruments,now);journal.import_fills(batch,now,allow_carry=True)
        report=reconcile_portfolio(journal,snapshot,instruments,now)
    finally:journal.close()
    if not report['inventory_reconciled']:raise Block('DHAN_PORTFOLIO: inventory did not reconcile')
    return {'schema':'portfolio_collection_v1','opening':opening,'snapshot':snapshot,'fills':batch,'report':report,
            'execution_orders':[dict(row) for row in orders],
            'raw_hash':fingerprint(raw),'opening_hash':fingerprint(opening),'execution_eligible':False,
            'submitted':False,'authenticated_acceptance':False}


def fetch(client,account_id,opening,instruments,clock=instant):
    started=clock()
    if validate_opening(opening,instruments,started)[0]!=('dhan',account_id) or client.account_id()!=account_id:
        raise Block('DHAN_PORTFOLIO: connected/opening account mismatch')
    book={k:client.request('GET',route) for k,route in ROUTES.items()};stable_book(book)
    selected=[holding_instrument(r,instruments) for r in book['holdings']]
    for row in book['positions']:
        ident=match_instrument('dhan',row,instruments)
        selected.append(next(i for i in instruments if all(i[k]==ident[k] for k in ident)))
    keys=sorted({quote_key(i) for i in selected});quotes={'source':'dhan','quotes':{},'missing':[]}
    for offset in range(0,len(keys),1000):
        result=client.quotes(keys[offset:offset+1000])
        if result.get('source')!='dhan':raise Block('DHAN_PORTFOLIO: wrong quote source')
        quotes['quotes'].update(result['quotes']);quotes['missing'].extend(result['missing'])
    verification={k:client.request('GET',route) for k,route in ROUTES.items()}
    if client.account_id()!=account_id:raise Block('DHAN_PORTFOLIO: account changed')
    ended=clock()
    raw={**book,'verification':verification,'quotes':quotes,'schema':'dhan_portfolio_read_v1','broker':'dhan',
         'account_id':account_id,'source':'dhan','date':ended.date().isoformat(),'started_at':started.isoformat(),
         'as_of':ended.isoformat(),'complete':True}
    return normalize(raw,opening,instruments,ended)


def main(argv=None):
    p=argparse.ArgumentParser(description=__doc__)
    for name in ('account-id','opening','master','out'):p.add_argument('--'+name,required=True)
    mode=p.add_mutually_exclusive_group(required=True);mode.add_argument('--fetch',action='store_true');mode.add_argument('--input')
    p.add_argument('--now');a=p.parse_args(argv)
    try:
        out=Path(a.out)
        if out.exists() or a.fetch and a.now:raise Block('DHAN_PORTFOLIO: new output/live clock required')
        opening,instruments=read(a.opening),read(a.master)['instruments']
        if opening.get('account_id')!=a.account_id:raise Block('DHAN_PORTFOLIO: selected account mismatch')
        if a.fetch:
            from dhan_readonly import DhanReadOnly
            result=fetch(DhanReadOnly(),a.account_id,opening,instruments)
        else:result=normalize(read(a.input),opening,instruments,instant(a.now))
        out.parent.mkdir(parents=True,exist_ok=True)
        with out.open('x',encoding='utf-8') as stream:json.dump(result,stream,indent=2,allow_nan=False)
        print(json.dumps({'status':'saved','report_status':result['report']['status'],'submitted':False}));return 0
    except (Block,ValueError,KeyError,TypeError,OSError,OverflowError,sqlite3.Error):
        print('{"status":"BLOCK","reason":"Dhan portfolio evidence failed validation; private response suppressed","submitted":false}');return 2


if __name__=='__main__':sys.exit(main())
