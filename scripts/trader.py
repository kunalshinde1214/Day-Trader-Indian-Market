"""India paper-analysis CLI. Network reads live in the separate broker CLI."""
from __future__ import annotations
import argparse
import json
import os
import sys
import tempfile
from pathlib import Path

from core import (Block, instant, number, fresh, validate_instrument, validate_config, setup_hit,
                  plan_from_hit, size, risk_batch, ticket, payload, charges, alerts, BROKERS, SETUPS)
from replay import replay

ROOT=Path(__file__).resolve().parents[1]


def read(path):
    def invalid_constant(value):
        raise Block("JSON: non-finite numbers are not accepted")
    return json.loads(Path(path).read_text(encoding="utf-8-sig"),parse_constant=invalid_constant)


def write(path, data):
    path=Path(path)
    path.parent.mkdir(parents=True,exist_ok=True)
    with tempfile.NamedTemporaryFile(mode="w",encoding="utf-8",dir=path.parent,delete=False,suffix=".tmp") as f:
        json.dump(data,f,indent=2,allow_nan=False)
        tmp=f.name
    os.replace(tmp,path)


def runtime_context(w,ctx):
    if (w/"quote.json").exists():
        ctx["quote"]=read(w/"quote.json")
    if (w/"state"/"halt.json").exists():
        ctx["local_halt"]=read(w/"state"/"halt.json").get("halted") is not False
    if (w/"state"/"monitor.json").exists():
        ctx["monitor"]=read(w/"state"/"monitor.json")
    if (w/"config.json").exists() and read(w/"config.json").get("quote_feed")=="kite_stream":
        from streaming import read_quote
        config=read(w/"config.json")
        ctx["quote"]=read_quote(w,read(w/"instrument.json"),instant(),config["snapshot_max_age_seconds"])
    return ctx


def initialize(workdir, broker="kite", synthetic=False, setup="orb"):
    if broker not in BROKERS:
        raise Block("CONFIG: unsupported broker")
    if setup not in SETUPS:
        raise Block("SETUP: unsupported scanner rule")
    workdir=Path(workdir)
    if any((workdir/n).exists() for n in ("config.json","charges.json")):
        raise Block("INIT: config or rate card already exists; choose a new directory")
    workdir.mkdir(parents=True,exist_ok=True)
    config=read(ROOT/"assets/config.example.json")
    config["broker"]=broker
    config["setup"]=setup
    card=read(ROOT/"assets/charges.example.json")
    if synthetic:
        card.update(broker=broker,source="synthetic_demo_rates_not_broker_pricing",status="fictional_test_only")
    elif broker!="kite":
        # Other brokers need their own verified tariff.
        card={"schema_version":2,"broker":broker,"source":"","effective_from":"",
              "review_after":"","status":"INCOMPLETE_REQUIRES_SOURCED_BROKER_RATE_CARD",
              "brokerage_rate":None,"brokerage_cap":None,"option_brokerage":None,
              "gst_rate":None,"sebi_rate":None,"segments":{}}
    write(workdir/"config.json",config);write(workdir/"charges.json",card)
    return {"initialized":str(workdir),"broker":broker,"setup":setup,"paper":True,"account_connected":False,
            "rates_configured":bool(card["source"])}


def scan(universe,candles,ctx,c,now):
    validate_config(c)
    if not universe.get("source") or not candles.get("source"):
        raise Block("SOURCE: universe and candles must be attributed")
    fresh(universe["as_of"],now,7*86400,"universe")
    fresh(ctx["restrictions"]["as_of"],now,c["snapshot_max_age_seconds"],"restrictions")
    if not ctx["restrictions"].get("source"):
        raise Block("SOURCE: restrictions")
    hits=[];excluded=[];processed=0
    requested=universe["instruments"]
    seen=set()
    for i in requested:
        key=i["exchange"]+":"+i["symbol"]
        try:
            if key in seen: raise Block("UNIVERSE: duplicate instrument")
            seen.add(key)
            validate_instrument(i,now)
            if i["broker"] != c["broker"]:
                raise Block("BROKER: universe instrument belongs to a different broker")
            if i["symbol"] in c["blocked"] or key in c["blocked"]:
                raise Block("BLOCKED_SYMBOL")
            flags=ctx["restrictions"]["instruments"][key]
            if any(flags.get(k) is not False for k in ("t2t","asm","gsm","ban","suspended","corporate_action","results_due")):
                raise Block("RESTRICTIONS: flagged or unknown")
            if number(i["average_daily_turnover_inr"]) < number(c["liquidity_floor_turnover_inr"]):
                raise Block("LIQUIDITY: below turnover floor")
            data=candles["instruments"][key]
            fresh(data["as_of"],now,c["snapshot_max_age_seconds"],"candle snapshot")
            # Fetch time alone is not evidence that the latest completed bar arrived.
            result=setup_hit(data["bars"],now,c,i,data.get("prior_close"),data.get("daily_reference"))
            processed+=1
            if result["state"]=="triggered":
                age=(now-instant(result["trigger_at"])).total_seconds()
                result["state"]="expired" if age>c["plan_max_age_seconds"] else "triggered"
                hits.append({"symbol":i["symbol"],"exchange":i["exchange"],**result})
        except (Block,KeyError,ValueError,TypeError) as exc:
            excluded.append({"instrument":key,"reason":str(exc)})
    return {"source":candles["source"],"as_of":now.isoformat(),"scope":universe.get("scope","supplied instruments only"),
            "setup":c.get("setup","orb"),
            "requested":len(requested),"processed":processed,"excluded_count":len(excluded),"hits":hits,"excluded":excluded,
            "label":"Rule matches, not recommendations; turnover uses supplied daily averages and candle close-volume estimates."}


def pipeline(w,now):
    c=read(w/"config.json");card=read(w/"charges.json");ctx=runtime_context(w,read(w/"context.json"))
    instrument=read(w/"instrument.json")
    scan_result=scan(read(w/"universe.json"),read(w/"candles.json"),ctx,c,now)
    write(w/"watchlist.json",scan_result)
    hit=next((h for h in scan_result["hits"] if h["symbol"]==instrument["symbol"] and h["exchange"]==instrument["exchange"]),None)
    if hit is None: raise Block("SCAN: selected instrument has no rule match")
    p=plan_from_hit(hit,instrument,c,now);write(w/"plan.json",p)
    rep=replay(p,instrument,read(w/"history.json"),c,card);write(w/"replay.json",rep)
    ctx["replay"]=rep
    order=size(p,instrument,c,ctx,card,now);write(w/"orders.json",[order]);write(w/"payload.json",payload(order))
    risk=risk_batch([order],{instrument["exchange"]+":"+instrument["symbol"]:instrument},c,ctx,card,now)
    write(w/"state"/"risk.json",risk)
    result=ticket(order,instrument,c,ctx,card,now);write(w/"ticket.json",result)
    return {"paper":True,"submitted":False,"setup":p["setup"],"symbols_processed":scan_result["processed"],"replay_paths":rep["paths"],"risk":risk["results"][0]["status"],"ticket":str(w/"ticket.json")}


def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command",choices=["init","demo","scan","entry","plan","replay","size","risk","ticket","charges","alerts"])
    parser.add_argument("--workdir",default="trader")
    parser.add_argument("--broker",choices=sorted(BROKERS),help="init/demo only; other commands use config.json")
    parser.add_argument("--setup",choices=sorted(SETUPS),help="init/demo only; other commands use config.setup (default orb)")
    parser.add_argument("--now",help="Offset-aware historical clock; analysis only")
    parser.add_argument("--index",type=int,default=0,help="One order index for ticket/charges")
    a=parser.parse_args(argv)
    w=Path(a.workdir)
    try:
        if a.broker and a.command not in {"init","demo"}:
            raise Block("CONFIG: --broker is only for init/demo; broker switches require new evidence")
        if a.setup and a.command not in {"init","demo"}:
            raise Block("CONFIG: --setup is only for init/demo; change config.setup and regenerate evidence")
        if a.command=="init": result=initialize(w,a.broker or "kite",setup=a.setup or "orb")
        elif a.command=="demo":
            if w.exists() and any(w.iterdir()): raise Block("DEMO: choose a new or empty output directory")
            initialize(w,a.broker or "kite",synthetic=True,setup=a.setup or "orb")
            from demo import inputs
            now,i,ctx,h,u,b=inputs(read(w/"config.json"))
            for name,data in (("instrument",i),("context",ctx),("history",h),("universe",u),("candles",b)):
                write(w/(name+".json"),data)
            result={"data":"SYNTHETIC ONLY","test_clock":now.isoformat(),**pipeline(w,now)}
        else:
            now=instant(a.now)
            c=read(w/"config.json");card=read(w/"charges.json")
            validate_config(c)
            if a.command in {"scan","entry","plan"}:
                ctx=read(w/"context.json");i=read(w/"instrument.json")
                if a.command=="scan":
                    result=scan(read(w/"universe.json"),read(w/"candles.json"),ctx,c,now)
                    write(w/"watchlist.json",result)
                else:
                    data=read(w/"candles.json")["instruments"][i["exchange"]+":"+i["symbol"]]
                    fresh(data["as_of"],now,c["snapshot_max_age_seconds"],"candles")
                    hit=setup_hit(data["bars"],now,c,i,data.get("prior_close"),data.get("daily_reference"))
                    if hit["state"]=="triggered" and (now-instant(hit["trigger_at"])).total_seconds()>c["plan_max_age_seconds"]: hit["state"]="expired"
                    result=hit if a.command=="entry" else plan_from_hit(hit,i,c,now)
                    write(w/("entry.json" if a.command=="entry" else "plan.json"),result)
            elif a.command=="replay":
                result=replay(read(w/"plan.json"),read(w/"instrument.json"),read(w/"history.json"),c,card)
                write(w/"replay.json",result)
            elif a.command in {"size","risk","ticket"}:
                ctx=runtime_context(w,read(w/"context.json"));ctx["replay"]=read(w/"replay.json")
                i=read(w/"instrument.json")
                if a.command=="size":
                    result=size(read(w/"plan.json"),i,c,ctx,card,now)
                    write(w/"orders.json",[result]);write(w/"payload.json",payload(result))
                    result={**result,"status":"CANDIDATE_REQUIRES_RISK_CHECK"}
                elif a.command=="risk":
                    # For multiple instruments, supply instruments.json keyed EXCHANGE:SYMBOL.
                    instruments=read(w/"instruments.json") if (w/"instruments.json").exists() else {i["exchange"]+":"+i["symbol"]:i}
                    result=risk_batch(read(w/"orders.json"),instruments,c,ctx,card,now)
                    write(w/"state"/"risk.json",result)
                else:
                    if a.index<0: raise Block("INDEX: must be nonnegative")
                    result=ticket(read(w/"orders.json")[a.index],i,c,ctx,card,now)
                    write(w/"ticket.json",result)
            elif a.command=="charges":
                if a.index<0: raise Block("INDEX: must be nonnegative")
                o=read(w/"orders.json")[a.index];p=o["plan"]
                result=charges(p["entry"],p["target"],o["quantity"],p["side"],read(w/"instrument.json"),card,now.date().isoformat())
            else:
                ctx=read(w/"context.json")
                result=alerts(read(w/"plan.json"),ctx["quote"],now,c,ctx["session"]["halted"])
        print(json.dumps(result,indent=2,allow_nan=False))
        return 2 if result.get("status")=="BLOCK" or any(x["status"]=="BLOCK" for x in result.get("results",[])) else 0
    except (Block,ValueError,KeyError,TypeError,IndexError,OSError) as exc:
        print(json.dumps({"status":"BLOCK","reason":str(exc),"submitted":False}))
        return 2


if __name__=="__main__":
    sys.exit(main())
