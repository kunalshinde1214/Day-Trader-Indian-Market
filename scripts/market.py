"""Timestamped context, expiry and derivatives diagnostics; no directional forecasts."""
import argparse
from datetime import date
import json
from pathlib import Path
import sys
from core import Block,instant,number,positive,fresh,validate_instrument
from trader import read
from data import link


def flows(data,now):
    link(data["source"])
    release=instant(data["published_at"])
    trading=date.fromisoformat(data["trading_date"])
    if release>now or trading>release.date() or not isinstance(data.get("provisional"),bool):raise Block("FLOWS: invalid release/trading date or provisional status")
    values={}
    for name in ("fii","dii"):
        buy,sell=[number(data[name][k]) for k in ("buy_inr","sell_inr")]
        values[name+"_net_inr"]=float(buy-sell)
    a,b=values.values()
    regime="risk-on" if a>0 and b>0 else "risk-off" if a<0 and b<0 else "mixed"
    return {**values,"trading_date":data["trading_date"],"published_at":data["published_at"],"source":data["source"],
            "provisional":data["provisional"],"regime":regime,"rule":"both net positive / both negative / otherwise mixed; descriptive heuristic only"}


def expiry(master,now):
    result=[]
    for i in master["instruments"]:
        if i["kind"]=="equity":continue
        validate_instrument(i,now)
        days=(date.fromisoformat(i["expiry"])-now.date()).days
        result.append({"symbol":i["symbol"],"exchange":i["exchange"],"token":i["token"],"expiry":i["expiry"],
                       "days_remaining":days,"expires_today":days==0,"underlying_type":i["underlying_type"],
                       "stock_settlement_gate":i["underlying_type"]=="stock" and days<=3})
    return {"source":master["source"],"as_of":now.isoformat(),"contracts":result,"margin_assumption":None,
            "note":"Actual master expiries only; no weekday inference, theta estimate or pinning forecast"}


def derivative_diagnostics(data,instrument,now,max_age=60):
    validate_instrument(instrument,now)
    if instrument["kind"]=="equity":raise Block("DERIVATIVE: selected contract required")
    if any(data.get(k)!=instrument.get(k) for k in ("broker","exchange","symbol","token")):raise Block("DERIVATIVE: exact contract required")
    fresh(data.get("as_of"),now,max_age,"derivative snapshot");link(data["source"])
    bid,ask=positive(data["bid"]),positive(data["ask"])
    if bid>ask:raise Block("DERIVATIVE: crossed market")
    result={"symbol":instrument["symbol"],"expiry":instrument["expiry"],"source":data["source"],"as_of":data["as_of"],
            "spread_inr":float(ask-bid),"spread_pct_mid":float((ask-bid)/((ask+bid)/2)*100),
            "open_interest":float(number(data["open_interest"])),"unavailable":[]}
    for k in ("iv","delta","gamma","theta","vega"):
        if k not in data:result["unavailable"].append(k);continue
        if not data.get("greeks_source") or not data.get("greeks_as_of"):raise Block("GREEKS: model/source time required")
        fresh(data["greeks_as_of"],now,max_age,"greeks")
        result[k]=float(number(data[k],k,minimum="-1e30"))
    result["label"]="observed/model-supplied diagnostics; no pinning or return prediction"
    return result


def main(argv=None):
    p=argparse.ArgumentParser(description=__doc__);p.add_argument("command",choices=["flows","expiry","derivatives"])
    p.add_argument("--input",required=True);p.add_argument("--instrument");p.add_argument("--out",required=True);p.add_argument("--now")
    a=p.parse_args(argv)
    try:
        now=instant(a.now);data=read(a.input);out=Path(a.out)
        if out.exists():raise Block("OUTPUT: choose new evidence file")
        result=flows(data,now) if a.command=="flows" else expiry(data,now) if a.command=="expiry" else derivative_diagnostics(data,read(a.instrument),now)
        encoded=json.dumps(result,indent=2,allow_nan=False);out.parent.mkdir(parents=True,exist_ok=True)
        with out.open("x",encoding="utf-8") as stream:stream.write(encoded)
        print(json.dumps({"status":"saved","path":str(out)}));return 0
    except (Block,ValueError,KeyError,TypeError,OSError):
        print(json.dumps({"status":"BLOCK","reason":"Market evidence invalid/incomplete; no forecast inferred"}));return 2


if __name__=="__main__":sys.exit(main())
