"""Human-operated Kite reducing exits from reconciled mixed inventory. Disabled by default."""
import argparse
import copy
import json
from pathlib import Path
import sqlite3

from core import Block, fingerprint, fresh, instant, integer, positive, on_tick, validate_instrument, reducing_exit
from execution import ExecutionStore, TerminalApproval, execute, profile_check, review_data
from journal import Journal
from live_broker import LiveBroker, identifier, wire_payload
from live_state import TERMINAL, state_hash
from portfolio import FIELDS, save_opening, reconcile_portfolio
from portfolio_collect import fetch
from trader import read


def reconcile(collection, instruments, now):
    if collection.get('schema')!='portfolio_collection_v1':raise Block('MIXED_EXIT: collector bundle required')
    source=collection['snapshot']
    if source.get('broker')!='kite' or source.get('source')!='kite':raise Block('MIXED_EXIT: Kite collector required')
    for part in ('opening','fills'):
        if any(collection[part].get(k)!=source.get(k) for k in ('broker','account_id')):
            raise Block('MIXED_EXIT: opening/fill account mismatch')
    journal=Journal(':memory:')
    try:
        save_opening(journal,collection['opening'],instruments,now)
        journal.import_fills(collection['fills'],now,allow_carry=True)
        report=reconcile_portfolio(journal,source,instruments,now)
    finally:journal.close()
    if report['inventory_reconciled'] is not True:raise Block('MIXED_EXIT: inventory/fills/orders do not reconcile')
    orders=collection.get('execution_orders')
    if not isinstance(orders,list) or len(orders)!=len(source['orders']):raise Block('MIXED_EXIT: complete detailed execution order book required')
    canonical={row['order_id']:row for row in source['orders']}
    seen=set()
    for order in orders:
        oid=identifier(order['order_id'])
        if oid in seen or oid not in canonical:raise Block('MIXED_EXIT: duplicate or mismatched detailed order')
        seen.add(oid)
        if any(order[k]!=canonical[oid][k] for k in (*FIELDS,'side','quantity','filled_quantity','status')):
            raise Block('MIXED_EXIT: detailed/canonical order mismatch')
        if order['status']=='UNKNOWN':raise Block('MIXED_EXIT: ambiguous order blocks action')
        for key in ('price','trigger_price','order_type','variety'):
            if key not in order:raise Block('MIXED_EXIT: recovery fields missing')
    snapshot={k:source[k] for k in ('broker','account_id','source','date','as_of')}
    snapshot.update(orders=sorted(copy.deepcopy(orders),key=lambda r:r['order_id']),
                    positions=sorted([{**{k:r[k] for k in FIELDS},'quantity':r['quantity']} for r in source['positions']],
                                     key=lambda r:tuple(r[k] for k in FIELDS)))
    snapshot['state_hash']=state_hash(snapshot)
    return snapshot, report


def prepare(collection, instruments, instrument, config, profile, now, *, product, kind='exit', quantity=None, price=None, order_id=None, delivery=None, trigger=None, quote=None):
    broker,account=config['broker'],config['account_id']
    if broker!='kite' or product not in ('MIS','CNC','NRML') or kind not in ('exit','stop','cancel'):
        raise Block('MIXED_EXIT: Kite reducing limit/stop/cancel only')
    profile_check(profile,broker,account,now)
    if profile.get('mixed_inventory_reducing_exits') is not True:
        raise Block('MIXED_EXIT: separate explicit live-profile opt-in required')
    acceptance=profile.get('mixed_inventory_acceptance',{})
    if acceptance.get('broker')!=broker or acceptance.get('account_id')!=account or acceptance.get('status')!='PASS' or not acceptance.get('source'):
        raise Block('MIXED_EXIT: account-specific mixed-inventory acceptance required')
    fresh(acceptance.get('as_of'),now,86400,'mixed-inventory acceptance')
    validate_instrument(instrument,now)
    if instrument['broker']!=broker or instrument not in instruments:
        raise Block('MIXED_EXIT: exact current selected master record required')
    if product=='CNC' and instrument['kind']!='equity' or product=='NRML' and instrument['kind']=='equity':
        raise Block('MIXED_EXIT: wrong product for instrument kind')
    opening_sources=[collection['opening'].get('source','')]
    opening_sources.extend(r.get(k,'') for r in collection['opening'].get('positions',[]) for k in ('cost_source','mark_source'))
    if any(word in str(source).lower() for source in opening_sources for word in ('synthetic','fictional','demo')):
        raise Block('MIXED_EXIT: synthetic opening evidence cannot authorize execution')
    snapshot,report=reconcile(collection,instruments,now)
    if snapshot['account_id']!=account:raise Block('MIXED_EXIT: account mismatch')
    fresh(snapshot['as_of'],now,30,'mixed account snapshot')
    key=tuple(instrument[k] for k in FIELDS[:3])+(product,)
    action={'kind':kind,'broker':broker,'account_id':account,'instrument':copy.deepcopy(instrument),'product':product}
    if kind=='cancel':
        found=[r for r in snapshot['orders'] if r['order_id']==identifier(order_id) and tuple(r[k] for k in FIELDS)==key]
        if len(found)!=1 or found[0]['status'] not in ('OPEN','PARTIAL'):raise Block('MIXED_EXIT: exact working product/order required')
        action.update(order_id=order_id,variety=found[0]['variety'],expected_order=copy.deepcopy(found[0]))
    else:
        matches=[r for r in snapshot['positions'] if tuple(r[k] for k in FIELDS)==key and r['quantity']]
        if len(matches)!=1:raise Block('MIXED_EXIT: exact nonzero product position required')
        position=matches[0]
        if any(tuple(r[k] for k in FIELDS)==key and r['status'] not in TERMINAL for r in snapshot['orders']):
            raise Block('MIXED_EXIT: cancel and confirm same-product working orders before replacement')
        q=integer(quantity);limit=positive(price)
        side='SELL' if position['quantity']>0 else 'BUY'
        reducing_exit(position,side,q)
        if q%integer(instrument['lot_size']) or q>integer(instrument['freeze_quantity']) or not on_tick(limit,instrument['tick_size']):
            raise Block('MIXED_EXIT: lot, freeze or tick limit failed; no slicing')
        if product=='CNC':
            if not isinstance(delivery,dict) or any(str(delivery.get(k))!=str(instrument[k]) for k in ('broker','exchange','symbol','token')):
                raise Block('DELIVERY_AUTH: exact instrument authorization evidence required')
            if (delivery.get('account_id')!=account or delivery.get('date')!=now.date().isoformat() or delivery.get('status')!='AUTHORIZED'
                    or delivery.get('method') not in ('DDPI','POA','CDSL_EDIS') or not delivery.get('source') or integer(delivery.get('authorized_quantity'))<q):
                raise Block('DELIVERY_AUTH: current account/quantity authorization required')
            fresh(delivery.get('as_of'),now,30,'delivery authorization evidence')
        action.update(side=side,quantity=q,price=float(limit))
        if kind=='stop':
            if not isinstance(quote,dict) or quote.get('source')!=broker or any(str(quote.get(k))!=str(instrument[k]) for k in ('exchange','symbol','token')):
                raise Block('MIXED_STOP: exact broker quote required')
            fresh(quote.get('as_of'),now,30,'mixed stop quote')
            trigger=positive(trigger)
            if not on_tick(trigger,instrument['tick_size']) or not (trigger<positive(quote['price']) if side=='SELL' else trigger>positive(quote['price'])):
                raise Block('MIXED_STOP: trigger reached or off tick')
            action['trigger_price']=float(trigger)
    economics={'config':config,'opening':collection['opening'],'fills':collection['fills'],
               'positions':[{k:v for k,v in r.items() if k!='mark_as_of'} for r in collection['snapshot']['positions']],
               'delivery':delivery if product=='CNC' and kind in ('exit','stop') else None,
               'stop_quote':quote if kind=='stop' else None}
    review={'action':action,'payload':wire_payload(action),'state_hash':snapshot['state_hash'],
            'baseline_order_ids':[r['order_id'] for r in snapshot['orders']],
            'bundle_hash':fingerprint(review_data(economics)),'profile_hash':fingerprint(profile)}
    return {**review,'review_hash':fingerprint(review),'as_of':now.isoformat(),
            'inventory_reconciled':True,'fees_final':report['fees_final'],
            'warning':'Real reducing order for the selected product only. Stop-limits may not fill. Broker acknowledgement is not a fill; delivery authorization and broker RMS can still reject it.'},snapshot


def main(argv=None):
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('command',choices=('preview','exit','stop','cancel','status','bind'))
    p.add_argument('--kind',choices=('exit','stop'),default='exit');p.add_argument('--trigger')
    p.add_argument('--workdir',required=True);p.add_argument('--opening',required=True);p.add_argument('--master',required=True)
    p.add_argument('--product',choices=('MIS','CNC','NRML'),required=True);p.add_argument('--quantity',type=int);p.add_argument('--price')
    p.add_argument('--order-id');p.add_argument('--intent-id');p.add_argument('--delivery-authorization')
    a=p.parse_args(argv);store=None
    try:
        from kite_readonly import KiteReadOnly
        w=Path(a.workdir);initial=read(w/'config.json')
        if initial['broker']!='kite':raise Block('MIXED_EXIT: currently Kite only')
        client=KiteReadOnly();store=ExecutionStore(w/'state/execution.sqlite');approval=TerminalApproval()
        def current():
            config=read(w/'config.json');instrument=read(w/'instrument.json');instruments=read(a.master)['instruments']
            if (config['broker'],config['account_id'])!=(initial['broker'],initial['account_id']):raise Block('MIXED_EXIT: selected account changed')
            collection=fetch(client,config['account_id'],read(a.opening),instruments,execution_details=True)
            return config,instrument,instruments,collection
        def refresh():
            config,instrument,instruments,collection=current()
            kind='cancel' if a.command=='cancel' else 'stop' if a.command=='stop' else a.kind if a.command=='preview' else 'exit'
            quote=None
            if kind=='stop':
                from broker_data import normalize_quote,quote_key
                quote=normalize_quote(client.quotes([quote_key(instrument)]),instrument,instant(),30)
            return prepare(collection,instruments,instrument,config,read(w/'live-profile.json'),instant(),product=a.product,
                           kind=kind,quantity=a.quantity,price=a.price,order_id=a.order_id,trigger=a.trigger,quote=quote,
                           delivery=read(a.delivery_authorization) if a.delivery_authorization else None)
        if a.command in ('status','bind'):
            config,instrument,instruments,collection=current();snapshot,report=reconcile(collection,instruments,instant())
            result=store.bind(a.intent_id,a.order_id,snapshot,approval) if a.command=='bind' else {'intents':store.observe(snapshot,instant()),'portfolio':report,'submitted':False}
        elif a.command=='preview':result,_=refresh();result['submitted']=False
        else:result=execute(store,LiveBroker('kite',client),approval,refresh)
        print(json.dumps(result,indent=2,allow_nan=False));return 0
    except (Block,ValueError,KeyError,TypeError,OSError,sqlite3.Error,KeyboardInterrupt):
        print(json.dumps({'status':'BLOCK_OR_UNKNOWN','reason':'Mixed-inventory check or action failed. Inspect the shared execution log and broker book; never retry an ambiguous submission.'}));return 2
    finally:
        if store:store.close()


if __name__=='__main__':raise SystemExit(main())
