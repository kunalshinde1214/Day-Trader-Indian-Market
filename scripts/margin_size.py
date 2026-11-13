"""Bounded exact-quantity derivative margin proposals; all calls are read-only."""
import argparse
import json
from pathlib import Path
import sys
from core import Block, instant, integer, number, fingerprint, size, payload, check_order, execution_metrics
from trader import read,write,runtime_context


def propose(client,plan,instrument,config,context,card,clock=instant,max_queries=10):
    if instrument["kind"]=="equity":raise Block("MARGIN_SIZE: derivative instrument required")
    if not 1<=integer(max_queries)<=100:raise Block("MARGIN_SIZE: query budget must be 1..100")
    if client.account_id()!=config["account_id"]:raise Block("MARGIN_SIZE: observed account mismatch")
    candidate=size(plan,instrument,config,context,card,clock())
    lot=integer(instrument["lot_size"]);upper=candidate["quantity"]
    attempts=[]
    # Descending exact lots: do not assume broker margins scale linearly or monotonically.
    for q in range(upper,max(0,upper-max_queries*lot),-lot):
        now=clock()
        candidate={**candidate,"quantity":q,"lots":q//lot,"metrics":execution_metrics(plan,instrument,config,context,card,q,now)}
        p=payload(candidate);margin=client.margin(p)
        if margin.get("payload_hash")!=fingerprint(p) or margin.get("account_id")!=config["account_id"] or margin.get("source")!=config["broker"]:
            raise Block("MARGIN_SIZE: broker evidence mismatch")
        from core import fresh
        fresh(margin.get("as_of"),now,config["snapshot_max_age_seconds"],"margin proposal")
        required=number(margin["required_inr"])+number(candidate["metrics"]["charges_at_stop"]["total"])
        attempts.append({"quantity":q,"payload_hash":fingerprint(p),"required_with_stop_charges_inr":float(required)})
        if required>number(context["account"]["margin_available"]):continue
        checked={**context,"margin":margin,"margins":{fingerprint(p):margin}}
        risk=check_order(candidate,instrument,config,checked,card,clock())
        return {"status":"CANDIDATE_REQUIRES_FRESH_TICKET","candidate":candidate,"margin":margin,"risk":risk,"attempts":attempts,"submitted":False}
    raise Block("MARGIN_SIZE: no candidate fits within bounded search; no order was changed or submitted")


def main(argv=None):
    p=argparse.ArgumentParser(description=__doc__);p.add_argument("--workdir",required=True);p.add_argument("--out",required=True)
    p.add_argument("--max-queries",type=int,default=10);a=p.parse_args(argv)
    try:
        from broker import adapter
        w=Path(a.workdir);out=Path(a.out)
        if out.exists():raise Block("OUTPUT: choose a new proposal filename")
        c=read(w/"config.json");ctx=runtime_context(w,read(w/"context.json"));ctx["replay"]=read(w/"replay.json")
        result=propose(adapter(c["broker"]),read(w/"plan.json"),read(w/"instrument.json"),c,ctx,read(w/"charges.json"),max_queries=a.max_queries)
        encoded=json.dumps(result,indent=2,allow_nan=False);out.parent.mkdir(parents=True,exist_ok=True)
        with out.open("x",encoding="utf-8") as stream:stream.write(encoded)
        print(json.dumps({"status":result["status"],"out":str(out),"queries":len(result["attempts"]),"submitted":False}));return 0
    except (Block,ValueError,KeyError,TypeError,OSError):
        print(json.dumps({"status":"BLOCK","reason":"Margin proposal input/account/API check failed; no order changed","submitted":False}));return 2


if __name__=="__main__":sys.exit(main())
