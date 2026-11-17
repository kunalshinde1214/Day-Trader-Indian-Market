"""Opt-in broker acceptance probe; no login, trading, or credential output."""
import argparse
import json
from pathlib import Path
import sys
from core import Block, BROKERS, instant, fingerprint
from broker import adapter
from broker_data import refresh
from trader import read


def extended_checks(client, broker, account_id, instrument, *, opening=None, master=None, basket=None, depth=False, clock=instant):
    """Run only explicitly selected read-only probes; preserve scope boundaries."""
    checks = {}
    if (opening is None) != (master is None): raise Block('PROBE: carry opening and master must be supplied together')
    # Validate every supplied account before making any extended request.
    for supplied in (opening, basket):
        if supplied is not None and any(supplied.get(k) != value for k, value in (('broker', broker), ('account_id', account_id))):
            raise Block('PROBE: optional evidence belongs to a different account')
    if instrument.get('broker') != broker: raise Block('PROBE: selected broker mismatch')
    if opening is not None:
        from portfolio_exit import collect, reconcile
        instruments = master['instruments']
        if instrument not in instruments: raise Block('PROBE: selected instrument absent from complete master')
        collection = collect(client, broker, account_id, opening, instruments)
        snapshot, report = reconcile(collection, instruments, clock())
        if snapshot['account_id'] != account_id or snapshot['broker'] != broker: raise Block('PROBE: observed carry identity mismatch')
        checks['carry_inventory'] = {'status': 'PASS', 'collection_hash': fingerprint(collection),
            'inventory_reconciled': report['inventory_reconciled'], 'fees_final': report['fees_final'],
            'scope': 'Observed supported inventory/fills only; no delivery authorization or order tested'}
    if basket is not None:
        from portfolio_margin import quote
        report = quote(client, basket, clock=clock)
        checks['basket_margin'] = {'status': 'PASS', 'report_hash': fingerprint(report),
            'prefix_count': len(report['prefix_estimates']), 'scenario_count': len(report['fill_scenario_estimates']),
            'response_identity_verified': all(r['response_identity_verified'] for r in report['prefix_estimates']),
            'scope': 'Selected request/response contract only; no fill or future funding guarantee'}
    if depth:
        from depth_collect import collect
        report = collect(client, instrument, clock=clock)
        checks['cash_depth'] = {'status': 'PASS', 'report_hash': fingerprint(report),
            'liquidity_model_eligible': report.get('liquidity_model_eligible', False),
            'scope': 'Observed cash depth only; last-trade timestamps do not validate book timestamps'}
    return checks


def main(argv=None):
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument("--broker",required=True,choices=sorted(BROKERS))
    p.add_argument("--public-master",action="store_true")
    p.add_argument("--authenticated-readonly",action="store_true")
    p.add_argument("--interval-minutes",type=int,choices=(1,5),default=5)
    p.add_argument("--instrument");p.add_argument("--account-id");p.add_argument("--payload");p.add_argument("--out",required=True)
    p.add_argument('--portfolio-opening'); p.add_argument('--master'); p.add_argument('--basket')
    p.add_argument('--depth', action='store_true')
    a=p.parse_args(argv)
    try:
        out=Path(a.out)
        if out.exists():raise Block("OUTPUT: preserve existing acceptance evidence")
        if a.public_master==a.authenticated_readonly:raise Block("PROBE: choose exactly one acceptance mode")
        if a.public_master and a.broker=="kite":raise Block("PROBE: Kite master is authenticated; use explicit account probe")
        if a.public_master and any((a.portfolio_opening,a.master,a.basket,a.depth,a.payload)):
            raise Block('PROBE: account probes require explicit authenticated-readonly mode')
        client=adapter(a.broker)
        checks={};result={"broker":a.broker,"as_of":instant().isoformat(),"submitted":False,"mode":"public" if a.public_master else "authenticated_readonly"}
        if a.public_master:
            master=client.instruments()
            checks["supported_master_records"]=len(master["instruments"])
            if not checks["supported_master_records"]:raise Block("MASTER: empty supported universe")
            checks["master_hash"]=fingerprint(master)
        else:
            if not a.instrument or not a.account_id:raise Block("PROBE: selected instrument and expected nonsecret account ID required")
            i=read(a.instrument)
            if i["broker"]!=a.broker:raise Block("PROBE: instrument belongs to another broker")
            account=client.account()
            if account.get("account_id")!=a.account_id:raise Block("PROBE: observed account mismatch")
            checks["account_identity_matches"]=True
            candles,quote=refresh(client,i,instant(),interval=a.interval_minutes)
            checks["interval_minutes"]=a.interval_minutes
            checks.update(completed_session_bars=len(candles["bars"]),quote_timestamp=quote["as_of"])
            if a.payload:
                margin=client.margin(read(a.payload))
                if margin.get("account_id")!=a.account_id:raise Block("PROBE: margin account mismatch")
                checks["exact_payload_margin_hash"]=margin["payload_hash"]
            result["account_id"]=a.account_id
            checks.update(extended_checks(client,a.broker,a.account_id,i,
                opening=read(a.portfolio_opening) if a.portfolio_opening else None,
                master=read(a.master) if a.master else None,basket=read(a.basket) if a.basket else None,depth=a.depth))
            result["scope"]="Selected instrument/session reads only; not execution or full-broker certification"
        result.update(status="PASS",checks=checks,execution_acceptance='NOT_TESTED',production_acceptance='NOT_TESTED',
                      profile_modified=False)
        encoded=json.dumps(result,indent=2);out.parent.mkdir(parents=True,exist_ok=True)
        with out.open("x",encoding="utf-8") as stream:stream.write(encoded)
        print(encoded);return 0
    except (Block,ValueError,KeyError,TypeError,OSError):
        print(json.dumps({"status":"BLOCK","broker":a.broker,"reason":"Acceptance input/network/response check failed; no account details printed","submitted":False}));return 2


if __name__=="__main__":sys.exit(main())
