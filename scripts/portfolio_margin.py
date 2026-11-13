"""Exact Kite basket margin research. No order submission or cross-account netting."""
import argparse
import json
from pathlib import Path
from core import Block, instant, fingerprint, integer, positive, number, validate_instrument, on_tick


def quote(client, bundle, clock=instant):
    now=clock()
    if bundle.get("broker")!="kite" or not bundle.get("account_id"):
        raise Block("BASKET: supported exact broker/account required")
    legs=bundle.get("legs")
    if not isinstance(legs,list) or not 1<=len(legs)<=20:
        raise Block("BASKET: one to twenty ordered legs required")
    orders=[];seen=set()
    for leg in legs:
        i=leg["instrument"];validate_instrument(i,now)
        if i["broker"]!="kite" or leg.get("account_id")!=bundle["account_id"]:
            raise Block("BASKET: never combine accounts or brokers")
        key=(i["exchange"],i["token"],leg["product"])
        if key in seen:raise Block("BASKET: duplicate instrument/product leg")
        seen.add(key)
        qty=integer(leg["quantity"]);price=positive(leg["price"])
        if qty%integer(i["lot_size"]) or not on_tick(price,i["tick_size"]):raise Block("BASKET: lot/tick mismatch")
        if "freeze_quantity" in i and qty>integer(i["freeze_quantity"]):raise Block("BASKET: freeze limit exceeded")
        if leg["side"] not in ("BUY","SELL") or leg["product"] not in ({"MIS","CNC"} if i["kind"]=="equity" else {"MIS","NRML"}):
            raise Block("BASKET: unsupported side/product")
        orders.append({"exchange":i["exchange"],"tradingsymbol":i["symbol"],"transaction_type":leg["side"],
                       "variety":"regular","product":leg["product"],"order_type":"LIMIT","quantity":qty,"price":float(price),"trigger_price":0})
    scenarios=bundle.get('fill_scenarios',[])
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
    baseline=fingerprint({k:before[k] for k in ("positions","orders","margins")})
    estimates=[];scenario_estimates=[]
    # Every execution prefix is quoted: a cheap fully hedged final basket must not hide interim funding.
    for count in range(1,len(orders)+1):
        result=client.request("POST","/margins/basket",query={"consider_positions":"true"},body=orders[:count])
        rows=result.get("orders")
        if not isinstance(rows,list) or len(rows)!=count or any((r.get("exchange"),r.get("tradingsymbol"))!=(o["exchange"],o["tradingsymbol"]) for r,o in zip(rows,orders)):
            raise Block("BASKET: margin response legs mismatch")
        estimates.append({"prefix_legs":count,"initial_inr":float(number(result["initial"]["total"])),
                          "final_inr":float(number(result["final"]["total"])),"response_hash":fingerprint(result)})
    for quantities,selected in scenario_orders:
        result=client.request('POST','/margins/basket',query={'consider_positions':'true'},body=selected)
        rows=result.get('orders')
        if not isinstance(rows,list) or len(rows)!=len(selected) or any((r.get('exchange'),r.get('tradingsymbol'))!=(o['exchange'],o['tradingsymbol']) for r,o in zip(rows,selected)):
            raise Block('BASKET: scenario response legs mismatch')
        scenario_estimates.append({'quantities':quantities,'initial_inr':float(number(result['initial']['total'])),
                                   'final_inr':float(number(result['final']['total'])),'response_hash':fingerprint(result)})
    after=client.account()
    if after.get("account_id")!=bundle["account_id"] or fingerprint({k:after[k] for k in ("positions","orders","margins")})!=baseline:
        raise Block("BASKET: account books changed during estimates")
    return {"schema":"portfolio_margin_v1","broker":"kite","account_id":bundle["account_id"],"as_of":clock().isoformat(),
            "basket_hash":fingerprint(orders),"input_hash":fingerprint(bundle),"account_state_hash":baseline,
            "prefix_estimates":estimates,"fill_scenario_estimates":scenario_estimates,
            "peak_estimate_inr":max(max(r['initial_inr'],r['final_inr']) for r in estimates+scenario_estimates),
            "final_estimate_inr":estimates[-1]['final_inr'],"submitted":False,"execution_eligible":False,
            "limitations":["Broker estimates, not a local SPAN replica or a funding guarantee.",
                           "Only ordered prefixes and explicitly supplied partial-fill scenarios were estimated; unenumerated quantities, market shocks, collateral changes and future margins remain unknown.",
                           "No cross-account netting; rerun after account, order, instrument or price changes. Does not authorize basket execution."]}


def main(argv=None):
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--input',required=True);p.add_argument('--out',required=True)
    a=p.parse_args(argv)
    try:
        from trader import read
        from kite_readonly import KiteReadOnly
        out=Path(a.out)
        if out.exists():raise Block("OUTPUT: choose a new file")
        report=quote(KiteReadOnly(),read(a.input))
        out.parent.mkdir(parents=True,exist_ok=True)
        with out.open('x',encoding='utf-8') as f:json.dump(report,f,indent=2,allow_nan=False)
        print(json.dumps({'status':'SAVED','submitted':False,'path':str(out)}));return 0
    except (Block,KeyError,TypeError,ValueError,OSError):
        print('BLOCKED: basket input/account/response check failed');return 2


if __name__=='__main__':raise SystemExit(main())
