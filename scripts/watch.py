"""Bounded durable file-feed watcher. Local outbox only; no broker execution."""
import argparse
import json
from pathlib import Path
import sys
import time

from core import Block, instant, fresh, fingerprint, alerts, setup_hit, reducing_exit, integer
from journal import Journal
from trader import read,write


def cycle(workdir,journal,now,client=None):
    w=Path(workdir)
    messages=[];health={"source":"local_file_watcher","as_of":now.isoformat(),"state":"running","entry_allowed":False}
    try:
        c=read(w/"config.json");ctx=read(w/"context.json");i=read(w/"instrument.json")
        if client is not None:
            from broker_data import refresh
            data,quote=refresh(client,i,now,c["snapshot_max_age_seconds"])
            candles=read(w/"candles.json");key=i["exchange"]+":"+i["symbol"]
            old=candles["instruments"][key]
            candles["instruments"][key]={**old,**data};candles.update(source=i["broker"],as_of=data["as_of"])
            write(w/"candles.json",candles)
            ctx["quote"]=quote
            # Do not overwrite account/calendar evidence or fabricate fresh timestamps.
            write(w/"quote.json",quote)
        if c.get("quote_feed")=="kite_stream":
            from streaming import read_quote
            ctx["quote"]=read_quote(w,i,now,c["snapshot_max_age_seconds"])
        health.update(broker=c["broker"],account_id=c["account_id"],instrument_hash=fingerprint(i))
        age=c["snapshot_max_age_seconds"]
        # Price alerts do not depend on permission to enter or a reconciled ledger.
        if (w/"plan.json").exists():
            plan=read(w/"plan.json")
            health["plan_hash"]=fingerprint(plan)
            messages+=alerts(plan,ctx["quote"],now,c,ctx["session"].get("halted") is not False)["alerts"]
        for key in ("session","restrictions","event","account","quote"):
            fresh(ctx[key]["as_of"],now,age,key)
            if not ctx[key].get("source"): raise Block("SOURCE: "+key)
        if ctx["account"].get("broker")!=c["broker"] or ctx["account"].get("account_id")!=c["account_id"] or ctx["account"].get("reconciled") is not True:
            raise Block("ACCOUNT: watcher needs matching reconciled account")
        if ctx["quote"].get("exchange")!=i["exchange"] or ctx["quote"].get("symbol")!=i["symbol"]:
            raise Block("QUOTE: watcher identity mismatch")
        key=i["exchange"]+":"+i["symbol"]
        flags=ctx["restrictions"]["instruments"].get(key,{})
        if any(flags.get(f) is not False for f in ("t2t","asm","gsm","ban","suspended","corporate_action","results_due")):
            messages.append("RESTRICTIONS: flagged or unknown; pause entries")
        if ctx["session"].get("halted") is not False: messages.append("HALT: pause entries and inspect broker")
        if ctx["event"].get("risk_multiplier") not in (0.5,1): messages.append("EVENT: pause entries")
        if (w/"state"/"halt.json").exists() and read(w/"state"/"halt.json").get("halted") is not False:
            messages.append("LOCAL_HALT: user stop remains latched")
        data=read(w/"candles.json")["instruments"][key]
        fresh(data["as_of"],now,age,"candles")
        hit=setup_hit(data["bars"],now,c,i,data.get("prior_close"),data.get("daily_reference"))
        if hit["state"]=="triggered":
            hit["state"]="expired" if (now-instant(hit["trigger_at"])).total_seconds()>c["plan_max_age_seconds"] else "triggered"
        write(w/"entry.json",hit)
        health["entry_allowed"]=not messages
        if hit["state"]=="triggered": messages.append("ENTRY_RULE_MATCH: "+hit["trigger_at"])
    except (Block,ValueError,KeyError,TypeError,OSError):
        health.update(state="disconnected",entry_allowed=False)
        messages.append("DATA_UNAVAILABLE_OR_STALE: pause entries; inspect feed and broker")
    name=str(w.resolve())
    previous=journal.db.execute("SELECT body FROM heartbeat WHERE name=?",(name,)).fetchone()
    old=json.loads(previous[0]).get("active_alerts",[]) if previous else []
    # Event episodes persist across restarts; a cleared and recurring alert is new.
    for message in messages:
        if message not in old:
            journal.enqueue({"workdir":name,"at":now.isoformat(),"message":message,"delivery":"local_outbox_only"})
    health["active_alerts"]=messages
    journal.beat(name,health);write(w/"state"/"monitor.json",health)
    return health


def panic_preview(snapshot):
    if snapshot.get("complete") is not True: raise Block("PANIC: complete canonical broker snapshot required")
    reductions=[]
    for p in snapshot["positions"]:
        from journal import signed
        q=signed(p["quantity"])
        if q: reductions.append(reducing_exit(p,"SELL" if q>0 else "BUY",abs(q)))
    return {"paper":True,"submitted":False,"cancel_previews":[o["order_id"] for o in snapshot["orders"] if o["status"] in {"OPEN","PARTIAL","UNKNOWN"}],
            "reducing_exit_previews":reductions,"warning":"No orders cancelled or positions flattened; use broker controls for real positions"}


def main(argv=None):
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument("command",choices=["run","status","halt","resume","panic-preview"])
    p.add_argument("--workdir",required=True);p.add_argument("--cycles",type=int,default=1)
    p.add_argument("--interval",type=float,default=5);p.add_argument("--input")
    p.add_argument("--broker-poll",action="store_true",help="Explicit read-only quote/candle requests; other context must still be refreshed")
    a=p.parse_args(argv);w=Path(a.workdir);j=None
    try:
        if a.command in {"halt","resume"}:
            write(w/"state"/"halt.json",{"halted":a.command=="halt","at":instant().isoformat(),"source":"explicit_local_command"})
            result={"local_halt":a.command=="halt","broker_orders_changed":False}
        elif a.command=="panic-preview": result=panic_preview(read(a.input))
        elif a.command=="status":
            result=read(w/"state"/"monitor.json")
            c=read(w/"config.json")
            try: fresh(result["as_of"],instant(),c["snapshot_max_age_seconds"],"watcher")
            except Block: result.update(state="stale",entry_allowed=False)
        else:
            if not 1<=integer(a.cycles)<=8640 or not 1<=a.interval<=60: raise Block("WATCH: cycles 1..8640; interval 1..60 seconds")
            j=Journal(w/"state"/"journal.sqlite")
            client=None
            if a.broker_poll:
                from broker import adapter
                client=adapter(read(w/"config.json")["broker"])
                if a.interval<5:raise Block("WATCH: broker polling interval must be at least five seconds")
            from service import ProcessLock
            with ProcessLock(w/"state"/"monitor.lock"):
                try:
                    for index in range(a.cycles):
                        if (w/"state"/"service-stop.json").exists(): break
                        result=cycle(w,j,instant(),client)
                        print(json.dumps(result),flush=True)
                        if index+1<a.cycles: time.sleep(a.interval)
                finally:
                    result={"source":"local_file_watcher","as_of":instant().isoformat(),"state":"stopped","entry_allowed":False}
                    write(w/"state"/"monitor.json",result)
            result["pending_notices"]=len(j.pending())
        print(json.dumps(result,indent=2));return 0
    except (Block,ValueError,KeyError,TypeError,OSError):
        print(json.dumps({"status":"BLOCK","reason":"Watcher inputs unavailable or invalid","submitted":False}));return 2
    finally:
        if j:j.close()


if __name__=="__main__": sys.exit(main())
