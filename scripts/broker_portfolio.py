"""Angel One/FYERS read-only carry collectors; accounting only, no execution."""
import argparse
from collections import defaultdict
import json
from pathlib import Path
import sqlite3
import sys
from broker_data import normalize_quote,quote_key
from core import Block,fingerprint,fresh,instant,integer,positive
from journal import Journal
from live_state import match_instrument,normalize_order
from portfolio import FIELDS,array,evidence,item_key,quantity,validate_opening,save_opening,reconcile_portfolio
from trade_import import trade_product,trade_time,normalize_trades
from trader import read

SUPPORTED={'angel','fyers'}
HOLDING_FIELDS={
 'angel':('exchange','tradingsymbol','symboltoken','product','isin','quantity','realisedquantity','t1quantity','authorisedquantity','collateralquantity'),
 'fyers':('symbol','fyToken','holdingType','isin','quantity','remainingQuantity','qty_t1','collateralQuantity','remainingPledgeQuantity','exchange','segment')}
POSITION_FIELDS={
 'angel':('exchange','tradingsymbol','symboltoken','producttype','netqty','buyqty','sellqty','cfbuyqty','cfsellqty','multiplier','pricenum','priceden','gennum','genden'),
 'fyers':('symbol','fyToken','productType','netQty','buyQty','sellQty','cfBuyQty','cfSellQty','dayBuyQty','daySellQty','qty','side','crossCurrency','qtyMulti_com','exchange','segment')}


def stable_book(book,broker):
    out={}
    for name in ('holdings','positions','orders','trades'):
        fields=HOLDING_FIELDS[broker] if name=='holdings' else POSITION_FIELDS[broker] if name=='positions' else None
        rows=[]
        for r in array(book[name]):
            value={k:r[k] for k in fields} if fields else r
            value={**value,**{k:r[k] for k in ('clientcode','fy_id','productType') if k in r}}
            rows.append(fingerprint(value))
        out[name]=sorted(rows)
    return out


def normalize(raw,opening,instruments,now):
    pair,master=validate_opening(opening,instruments,now);b=pair[0];day=now.date().isoformat()
    if b not in SUPPORTED:raise Block('CARRY: unsupported broker')
    evidence(raw,now,day,pair)
    if raw.get('source')!=b or raw.get('schema')!=b+'_portfolio_read_v1':raise Block('CARRY: matching source/schema required')
    start,end=instant(raw['started_at']),instant(raw['as_of']);fresh(raw['started_at'],now,60,'collection start')
    if start.date()!=now.date() or start>end or instant(opening['as_of'])>start:raise Block('CARRY: collection window invalid')
    if stable_book(raw,b)!=stable_book(raw['verification'],b):raise Block('CARRY: books changed; refresh')

    def identity(row):
        for field in ('clientcode','fy_id'):
            if field in row and row[field]!=pair[1]:raise Block('CARRY: foreign account')
        ident=match_instrument(b,row,instruments)
        i=master[tuple(ident[k] for k in FIELDS[:3])]
        if b=='fyers':
            if 'fyToken' in row and str(row['fyToken'])!=i['token']:raise Block('CARRY: token mismatch')
            if 'exchange' in row and integer(row['exchange'])!= (10 if i['exchange'] in {'NSE','NFO'} else 12):raise Block('CARRY: exchange mismatch')
            if 'segment' in row and integer(row['segment'])!=(10 if i['kind']=='equity' else 11):raise Block('CARRY: segment mismatch')
        return ident,i
    def key_for(row):
        ident,i=identity(row)
        return item_key({**ident,'product':trade_product(b,row,True,instrument=i)},master)

    orders=[]
    for r in array(raw['orders']):
        key=key_for(r);stamp=r['updatetime'] if b=='angel' else r['orderDateTime']
        if trade_time(stamp,day,b)>end:raise Block('CARRY: future order')
        product_field='producttype' if b=='angel' else 'productType'
        order=normalize_order(b,{**r,product_field:'INTRADAY'},instruments);order['product']=key[3]
        if order['status']=='UNKNOWN':raise Block('CARRY: ambiguous order')
        orders.append(order)
    header={'broker':b,'account_id':pair[1],'source':b,'date':day,'as_of':raw['as_of'],'complete':True}
    snapshot={**header,'schema':'portfolio_snapshot_v1','inventory_events':[],'orders':orders,'positions':[]}
    batch=normalize_trades(raw,snapshot,instruments,now,allow_carry=True)
    opens={tuple(r[k] for k in FIELDS):r for r in opening['positions']}
    traded=defaultdict(lambda:{'BUY':0,'SELL':0})
    for f in batch['fills']:traded[tuple(f[k] for k in FIELDS)][f['side']]+=f['quantity']
    holdings={};isins=set()
    for r in array(raw['holdings']):
        ident,i=identity(r);key=item_key({**ident,'product':'CNC'},master);isin=r['isin']
        if key in holdings or not isinstance(isin,str) or not isin or isin in isins:raise Block('CARRY: duplicate/invalid holding')
        isins.add(isin);held=integer(r['quantity'],minimum=0)
        if held!=quantity(opens.get(key,{}).get('quantity',0)):raise Block('CARRY: opening holding total mismatch')
        if b=='angel':
            if r['product']!='DELIVERY' or any(integer(r[k],minimum=0) for k in ('t1quantity','collateralquantity')):
                raise Block('CARRY: unsupported or unknown settlement/collateral')
            if integer(r['realisedquantity'],minimum=0)!=held or integer(r['authorisedquantity'],minimum=0)>held:
                raise Block('CARRY: holding basis mismatch')
        else:
            if r.get('productType','CNC')!='CNC' or r['holdingType']!='HLD' or any(integer(r[k],minimum=0) for k in ('qty_t1','collateralQuantity','remainingPledgeQuantity')):
                raise Block('CARRY: unsupported/ambiguous pledged or unsettled holding')
            remaining=integer(r['remainingQuantity'],minimum=0)
            if remaining>held or held-remaining>traded[key]['SELL']:raise Block('CARRY: holdings reduction lacks matching fills')
        holdings[key]=held
    positions={}
    for r in array(raw['positions']):
        key=key_for(r)
        if key in positions:raise Block('CARRY: duplicate position')
        if b=='angel':
            if any(positive(r[k])!=1 for k in ('multiplier','pricenum','priceden','gennum','genden')):raise Block('CARRY: unverified price/unit multiplier')
            buys,sells,cb,cs=(integer(r[k],minimum=0) for k in ('buyqty','sellqty','cfbuyqty','cfsellqty'));q=quantity(r['netqty'])
        else:
            buys,sells,cb,cs=(integer(r[k],minimum=0) for k in ('dayBuyQty','daySellQty','cfBuyQty','cfSellQty'));q=quantity(r['netQty'])
            if r['crossCurrency']!='N' or positive(r['qtyMulti_com'])!=1:raise Block('CARRY: foreign currency/non-unit multiplier')
            if (integer(r['buyQty'],minimum=0),integer(r['sellQty'],minimum=0))!=(cb+buys,cs+sells):raise Block('CARRY: position totals mismatch')
            if integer(r['qty'],minimum=0)!=abs(q) or type(r['side']) is not int or r['side']!=(1 if q>0 else -1 if q<0 else 0):raise Block('CARRY: position direction mismatch')
        expected=quantity(opens.get(key,{}).get('quantity',0)) if key[3]=='NRML' else 0
        if cb and cs or cb-cs!=expected or (buys,sells)!=(traded[key]['BUY'],traded[key]['SELL']) or q!=cb-cs+buys-sells:
            raise Block('CARRY: opening/position/trades mismatch')
        positions[key]=q
    if any(key not in positions for key in traded if any(traded[key].values())):raise Block('CARRY: missing traded position')
    for key in opens:
        if key[3]=='CNC' and key not in holdings or key[3]=='NRML' and key not in positions:raise Block('CARRY: missing opening inventory')
    for key in sorted(holdings.keys()|positions.keys()):
        q=holdings.get(key,0)+positions.get(key,0) if key[3]=='CNC' else positions[key]
        row={**dict(zip(FIELDS,key)),'quantity':q}
        if q:
            quote=normalize_quote(raw['quotes'],master[key[:3]],now,60);at=instant(quote['as_of'])
            if quote.get('source')!=b or at.date()!=now.date() or at>end:raise Block('CARRY: invalid quote clock/source')
            row.update(mark_price=float(positive(quote['price'])),mark_source=b+'_quote',mark_as_of=at.isoformat(),price_basis='per_unit_inr')
        snapshot['positions'].append(row)
    j=Journal(':memory:')
    try:
        save_opening(j,opening,instruments,now);j.import_fills(batch,now,allow_carry=True)
        report=reconcile_portfolio(j,snapshot,instruments,now)
    finally:j.close()
    if not report['inventory_reconciled']:raise Block('CARRY: inventory did not reconcile')
    return {'schema':'portfolio_collection_v1','opening':opening,'snapshot':snapshot,'fills':batch,'report':report,
            'raw_hash':fingerprint(raw),'opening_hash':fingerprint(opening),'execution_eligible':False,'submitted':False,'authenticated_acceptance':False}


def routes(broker):
    if broker=='angel':import angel_readonly as api
    elif broker=='fyers':import fyers_readonly as api
    else:raise Block('CARRY: unsupported broker')
    return {'holdings':api.HOLDINGS,'positions':api.POSITIONS,'orders':api.ORDERS,'trades':api.TRADES}


def fetch(client,broker,account_id,opening,instruments,clock=instant):
    started=clock()
    if validate_opening(opening,instruments,started)[0]!=(broker,account_id) or client.account_id()!=account_id:raise Block('CARRY: account mismatch')
    def book():
        rows={k:client.request('GET',p) for k,p in routes(broker).items()}
        if broker=='fyers':rows={k:rows[k][v] for k,v in {'holdings':'holdings','positions':'netPositions','orders':'orderBook','trades':'tradeBook'}.items()}
        stable_book(rows,broker);return rows
    first=book();selected=[]
    for r in first['holdings']+first['positions']:
        ident=match_instrument(broker,r,instruments);selected.append(next(i for i in instruments if all(i[k]==ident[k] for k in ident)))
    keys=sorted({quote_key(i) for i in selected});quotes={'source':broker,'quotes':{},'missing':[]}
    for offset in range(0,len(keys),50):
        result=client.quotes(keys[offset:offset+50])
        if result.get('source')!=broker:raise Block('CARRY: quote source mismatch')
        quotes['quotes'].update(result['quotes']);quotes['missing'].extend(result['missing'])
    verification=book()
    if client.account_id()!=account_id:raise Block('CARRY: account changed')
    ended=clock()
    return normalize({**first,'verification':verification,'quotes':quotes,'broker':broker,'account_id':account_id,'source':broker,
                      'schema':broker+'_portfolio_read_v1','date':ended.date().isoformat(),'started_at':started.isoformat(),
                      'as_of':ended.isoformat(),'complete':True},opening,instruments,ended)


def main(argv=None):
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--broker',choices=sorted(SUPPORTED),required=True)
    for name in ('account-id','opening','master','out'):p.add_argument('--'+name,required=True)
    mode=p.add_mutually_exclusive_group(required=True);mode.add_argument('--fetch',action='store_true');mode.add_argument('--input');p.add_argument('--now')
    a=p.parse_args(argv)
    try:
        out=Path(a.out);opening=read(a.opening);instruments=read(a.master)['instruments']
        if out.exists() or a.fetch and a.now or (opening.get('broker'),opening.get('account_id'))!=(a.broker,a.account_id):raise Block('CARRY: output/account/clock invalid')
        if a.fetch:
            from broker import adapter
            result=fetch(adapter(a.broker),a.broker,a.account_id,opening,instruments)
        else:result=normalize(read(a.input),opening,instruments,instant(a.now))
        out.parent.mkdir(parents=True,exist_ok=True)
        with out.open('x',encoding='utf-8') as f:json.dump(result,f,indent=2,allow_nan=False)
        print(json.dumps({'status':'saved','report_status':result['report']['status'],'submitted':False}));return 0
    except (Block,ValueError,KeyError,TypeError,OSError,OverflowError,sqlite3.Error):
        print('{"status":"BLOCK","reason":"Carry evidence failed validation; private response suppressed","submitted":false}');return 2


if __name__=='__main__':sys.exit(main())
