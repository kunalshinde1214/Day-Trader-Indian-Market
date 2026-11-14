"""Opt-in broker acceptance probe; no login, trading, or credential output."""
import argparse
import json
from pathlib import Path
import sys
from core import Block, BROKERS, instant, fingerprint
from broker import adapter
from broker_data import refresh
from trader import read


def main(argv=None):
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument("--broker",required=True,choices=sorted(BROKERS))
    p.add_argument("--public-master",action="store_true")
    p.add_argument("--authenticated-readonly",action="store_true")
    p.add_argument("--interval-minutes",type=int,choices=(1,5),default=5)
    p.add_argument("--instrument");p.add_argument("--account-id");p.add_argument("--payload");p.add_argument("--out",required=True)
    a=p.parse_args(argv)
    try:
        out=Path(a.out)
        if out.exists():raise Block("OUTPUT: preserve existing acceptance evidence")
        if a.public_master==a.authenticated_readonly:raise Block("PROBE: choose exactly one acceptance mode")
        if a.public_master and a.broker=="kite":raise Block("PROBE: Kite master is authenticated; use explicit account probe")
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
            result["scope"]="Selected instrument/session reads only; not execution or full-broker certification"
        result.update(status="PASS",checks=checks)
        encoded=json.dumps(result,indent=2);out.parent.mkdir(parents=True,exist_ok=True)
        with out.open("x",encoding="utf-8") as stream:stream.write(encoded)
        print(encoded);return 0
    except (Block,ValueError,KeyError,TypeError,OSError):
        print(json.dumps({"status":"BLOCK","broker":a.broker,"reason":"Acceptance input/network/response check failed; no account details printed","submitted":False}));return 2


if __name__=="__main__":sys.exit(main())
