"""Read-only broker basket margin research. No submission or cross-account netting."""
import argparse
import json
from pathlib import Path
from itertools import product as combinations
from core import Block, instant, fingerprint, integer, positive, number, validate_instrument, on_tick, fresh
from basket_margins import estimate, identity


def quote(client, bundle, clock=instant):
    now=clock()
    broker = bundle.get('broker')
    if broker not in {'kite', 'upstox', 'angel', 'fyers'} or not bundle.get("account_id"):
        raise Block("BASKET: supported exact broker/account required")
    legs=bundle.get("legs")
    if not isinstance(legs,list) or not 1<=len(legs)<=20:
        raise Block("BASKET: one to twenty ordered legs required")
    orders=[];seen=set();instruments={};tokens=set()
    for leg in legs:
        i=leg["instrument"];validate_instrument(i,now)
        if i["broker"]!=broker or leg.get("account_id")!=bundle["account_id"]:
            raise Block("BASKET: never combine accounts or brokers")
        identity(broker, i)
        if broker == 'upstox' and i['token'] in tokens:
            raise Block('BASKET: Upstox forbids repeated instrument keys, including different products')
        tokens.add(i['token'])
        key=(i["exchange"],i["token"],leg["product"])
        if key in seen:raise Block("BASKET: duplicate instrument/product leg")
        seen.add(key)
        qty=integer(leg["quantity"]);price=positive(leg["price"])
        if qty%integer(i["lot_size"]) or not on_tick(price,i["tick_size"]):raise Block("BASKET: lot/tick mismatch")
        if "freeze_quantity" in i and qty>integer(i["freeze_quantity"]):raise Block("BASKET: freeze limit exceeded")
        if leg["side"] not in ("BUY","SELL") or leg["product"] not in ({"MIS","CNC"} if i["kind"]=="equity" else {"MIS","NRML"}):
            raise Block("BASKET: unsupported side/product")
        label=(i['exchange'],i['symbol'],leg['product'])
        if label in instruments: raise Block('BASKET: conflicting symbol/token identity')
        instruments[label]=i
        orders.append({"exchange":i["exchange"],"tradingsymbol":i["symbol"],"transaction_type":leg["side"],
                       "variety":"regular","product":leg["product"],"order_type":"LIMIT","quantity":qty,"price":float(price),"trigger_price":0})
    scenarios=bundle.get('fill_scenarios',[])
    grid=bundle.get('fill_grid')
    if grid is not None:
        if scenarios or not isinstance(grid,list) or len(grid)!=len(orders):raise Block('BASKET: choose explicit scenarios or one grid axis per leg')
        combinations_count=1;axes=[]
        for axis,order,leg in zip(grid,orders,legs):
            if not isinstance(axis,list) or not 1<=len(axis)<=41:raise Block('BASKET: bounded nonempty grid axis required')
            axis=[integer(q,minimum=0) for q in axis]
            if len(set(axis))!=len(axis) or any(q>order['quantity'] or q%integer(leg['instrument']['lot_size']) for q in axis):
                raise Block('BASKET: invalid grid quantity/duplicate/lot')
            axes.append(axis);combinations_count*=len(axis)
        # Bound before materializing Cartesian products; the empty basket is omitted.
        if combinations_count-int(all(0 in axis for axis in axes))>40:raise Block('BASKET: grid exceeds forty nonempty scenarios')
        scenarios=[list(row) for row in combinations(*axes) if any(row)]
        if not scenarios:raise Block('BASKET: grid contains no exposure')
    if not isinstance(scenarios,list) or len(scenarios)>40:raise Block('BASKET: at most forty explicit fill scenarios')
    scenario_orders=[];scenario_keys=set()
    for quantities in scenarios:
        if not isinstance(quantities,list) or len(quantities)!=len(orders):raise Block('BASKET: scenario needs a quantity for every leg')
        quantities=[integer(q,minimum=0) for q in quantities]
        if not any(quantities) or tuple(quantities) in scenario_keys:raise Block('BASKET: empty/duplicate scenario')
        scenario_keys.add(tuple(quantities));selected=[]
        for q,order,leg in zip(quantities,orders,legs):
            if q>order['quantity'] or q%integer(leg['instrument']['lot_size']):raise Block('BASKET: scenario exceeds proposed units or lot limits')
            if q:selected.append({**order,'quantity':q})
        scenario_orders.append((quantities,selected))
    before=client.account()
    if before.get("account_id")!=bundle["account_id"]:raise Block("BASKET: observed account mismatch")
    if broker != 'kite':
        if before.get('source') != broker or before.get('broker') != broker: raise Block('BASKET: account provider mismatch')
        fresh(before.get('as_of'), clock(), 60, 'basket account')
    baseline=fingerprint({k:before[k] for k in ("positions","orders","margins")})
    estimates=[];scenario_estimates=[]
    # Every execution prefix is quoted: a cheap fully hedged final basket must not hide interim funding.
    for count in range(1,len(orders)+1):
        estimates.append({'prefix_legs':count, **estimate(client, broker, orders[:count], instruments)})
    for quantities,selected in scenario_orders:
        scenario_estimates.append({'quantities':quantities, **estimate(client, broker, selected, instruments)})
    after=client.account()
    if after.get("account_id")!=bundle["account_id"] or fingerprint({k:after[k] for k in ("positions","orders","margins")})!=baseline:
        raise Block("BASKET: account books changed during estimates")
    if broker != 'kite':
        if after.get('source') != broker or after.get('broker') != broker: raise Block('BASKET: account provider mismatch')
        fresh(after.get('as_of'), clock(), 60, 'basket account')
        fresh(now.isoformat(), clock(), 60, 'basket estimate window')
    return {"schema":"portfolio_margin_v1","broker":broker,"account_id":bundle["account_id"],"as_of":clock().isoformat(),
            "basket_hash":fingerprint(orders),"input_hash":fingerprint(bundle),"account_state_hash":baseline,
            "prefix_estimates":estimates,"fill_scenario_estimates":scenario_estimates,
            "peak_estimate_inr":max(r['required_inr'] for r in estimates+scenario_estimates),
            "final_estimate_inr":estimates[-1]['final_inr'],"submitted":False,"execution_eligible":False,
            "final_required_inr":estimates[-1]['required_inr'],
            "peak_account_total_inr":max(r['account_total_inr'] for r in estimates+scenario_estimates) if broker=='fyers' else None,
            "limitations":["Broker estimates, not a local SPAN replica or a funding guarantee.",
                           "Upstox/Angel/FYERS responses do not echo exact leg identities; request hashes bind submitted inputs but cannot independently verify response identity.",
                           "Only ordered prefixes and explicitly supplied partial-fill scenarios were estimated; unenumerated quantities, market shocks, collateral changes and future margins remain unknown.",
                           "No cross-account netting; rerun after account, order, instrument or price changes. Does not authorize basket execution."]}


def main(argv=None):
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--input',required=True);p.add_argument('--out',required=True)
    a=p.parse_args(argv)
    try:
        from trader import read
        from broker import adapter
        out=Path(a.out)
        if out.exists():raise Block("OUTPUT: choose a new file")
        bundle=read(a.input)
        report=quote(adapter(bundle['broker']),bundle)
        out.parent.mkdir(parents=True,exist_ok=True)
        with out.open('x',encoding='utf-8') as f:json.dump(report,f,indent=2,allow_nan=False)
        print(json.dumps({'status':'SAVED','submitted':False,'path':str(out)}));return 0
    except (Block,KeyError,TypeError,ValueError,OSError):
        print('BLOCKED: basket input/account/response check failed');return 2


if __name__=='__main__':raise SystemExit(main())
