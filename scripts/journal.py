"""Transactional local fill journal. Imports evidence; never places broker orders."""
from collections import defaultdict
import argparse
import csv
import json
from pathlib import Path
import sqlite3
import sys

from core import Block, BROKERS, number, positive, integer, instant, fresh, fingerprint, money


def identity(data):
    if data.get("broker") not in BROKERS or not isinstance(data.get("account_id"),str) or not data["account_id"]:
        raise Block("JOURNAL: broker/account identity required")
    return data["broker"],data["account_id"]


def signed(value):
    if isinstance(value,bool): raise Block("JOURNAL: invalid signed quantity")
    result=number(str(value).lstrip("-"))
    if result != result.to_integral_value(): raise Block("JOURNAL: whole quantity required")
    return -int(result) if str(value).startswith("-") else int(result)


class Journal:
    def __init__(self,path):
        Path(path).parent.mkdir(parents=True,exist_ok=True)
        self.db=sqlite3.connect(path,timeout=10)
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA synchronous=FULL")
        self.db.execute("CREATE TABLE IF NOT EXISTS events (broker TEXT, account TEXT, id TEXT, hash TEXT, body TEXT, PRIMARY KEY(broker,account,id))")
        self.db.execute("CREATE TABLE IF NOT EXISTS notices (id TEXT PRIMARY KEY, body TEXT, delivered_at TEXT)")
        self.db.execute("CREATE TABLE IF NOT EXISTS heartbeat (name TEXT PRIMARY KEY, body TEXT)")
        self.db.execute("CREATE TABLE IF NOT EXISTS fee_audit (id INTEGER PRIMARY KEY, broker TEXT, account TEXT, fill_id TEXT, before_body TEXT, after_body TEXT, recorded_at TEXT)")

    def close(self): self.db.close()

    def import_fills(self,batch,now,allow_carry=False):
        broker,account=identity(batch)
        if not batch.get("source") or not batch.get("as_of") or instant(batch["as_of"])>now:
            raise Block("JOURNAL: source and nonfuture snapshot time required")
        rows=batch["fills"]
        if not isinstance(rows,list): raise Block("JOURNAL: fills array required")
        added=0
        with self.db:
            for f in rows:
                for k in ("fill_id","order_id","exchange","symbol","token","product","at","source"):
                    if not isinstance(f.get(k),str) or not f[k].strip(): raise Block("JOURNAL: missing fill identity/provenance")
                products={"MIS","CNC","NRML"} if allow_carry else {"MIS"}
                if f["side"] not in {"BUY","SELL"} or f["product"] not in products: raise Block("JOURNAL: unsupported side/product; carry imports require explicit portfolio mode")
                if instant(f["at"])>instant(batch["as_of"]): raise Block("JOURNAL: fill after snapshot")
                integer(f["quantity"]); positive(f["price"])
                if not isinstance(f.get("fees"),dict): raise Block("JOURNAL: explicit fee components required; empty means unverified")
                for value in f["fees"].values(): number(value,"fee")
                if not isinstance(f.get("fees_final"),bool): raise Block("JOURNAL: fees_final boolean required")
                if f["fees_final"] and not f["fees"]: raise Block("JOURNAL: final fees require explicit components, including explicit zero")
                h=fingerprint(f)
                old=self.db.execute("SELECT hash FROM events WHERE broker=? AND account=? AND id=?",(broker,account,f["fill_id"])).fetchone()
                if old:
                    if old[0]!=h:
                        original=self.db.execute("SELECT before_body FROM fee_audit WHERE broker=? AND account=? AND fill_id=? ORDER BY id LIMIT 1",(broker,account,f["fill_id"])).fetchone()
                        if not original or fingerprint(json.loads(original[0]))!=h:
                            raise Block("JOURNAL: conflicting fill ID; reconcile correction explicitly")
                    continue
                self.db.execute("INSERT INTO events VALUES (?,?,?,?,?)",(broker,account,f["fill_id"],h,json.dumps(f,allow_nan=False)))
                added+=1
        return {"imported":added,"duplicates":len(rows)-added,"submitted":False}

    def fills(self,broker,account,day):
        rows=[json.loads(row[0]) for row in self.db.execute("SELECT body FROM events WHERE broker=? AND account=?",(broker,account))]
        return sorted((r for r in rows if instant(r["at"]).date().isoformat()==day),key=lambda r:(instant(r["at"]),r["fill_id"]))

    def contract_fees(self,correction,now):
        broker,account=identity(correction)
        if not correction.get("source") or not correction.get("as_of") or instant(correction["as_of"])>now:
            raise Block("FEES: contract-note source and release time required")
        fees=correction["fees"]
        if not isinstance(fees,dict) or not fees: raise Block("FEES: explicit contract-note cost components required")
        for value in fees.values():number(value,"contract-note fee")
        with self.db:
            row=self.db.execute("SELECT body FROM events WHERE broker=? AND account=? AND id=?",(broker,account,correction["fill_id"])).fetchone()
            if not row:raise Block("FEES: unknown fill")
            original=json.loads(row[0])
            if instant(correction["as_of"])<instant(original["at"]):raise Block("FEES: contract note predates fill")
            revised={**original,"fees":fees,"fees_final":True,"fee_source":correction["source"],"fee_as_of":correction["as_of"]}
            if original==revised:return {"updated":False}
            body=json.dumps(revised,allow_nan=False)
            self.db.execute("INSERT INTO fee_audit(broker,account,fill_id,before_body,after_body,recorded_at) VALUES (?,?,?,?,?,?)",
                            (broker,account,correction["fill_id"],row[0],body,now.isoformat()))
            self.db.execute("UPDATE events SET body=?,hash=? WHERE broker=? AND account=? AND id=?",(body,fingerprint(revised),broker,account,correction["fill_id"]))
        return {"updated":True,"fill_identity_and_quantity_changed":False}

    def enqueue(self,notice):
        key=fingerprint(notice)
        with self.db:
            self.db.execute("INSERT OR IGNORE INTO notices VALUES (?,?,NULL)",(key,json.dumps(notice,allow_nan=False)))
        return key

    def pending(self):
        return [{"id":k,**json.loads(v)} for k,v in self.db.execute("SELECT id,body FROM notices WHERE delivered_at IS NULL ORDER BY rowid")]

    def acknowledge(self,key,now):
        with self.db:
            cursor=self.db.execute("UPDATE notices SET delivered_at=? WHERE id=? AND delivered_at IS NULL",(now.isoformat(),key))
        return {"acknowledged":cursor.rowcount}

    def beat(self,name,state):
        with self.db:
            self.db.execute("INSERT OR REPLACE INTO heartbeat VALUES (?,?)",(name,json.dumps(state,allow_nan=False)))


def reconcile(journal,snapshot,reservations,now,max_age=60,fee_policy=None,instruments=None,rate_card=None):
    """Canonical snapshot must include *all* intraday orders/positions for the day.

    Raw broker account snapshots intentionally do not satisfy this contract.
    Unknown/missing orders, fees, quantities or mappings leave reconciled false.
    """
    broker,account=identity(snapshot)
    fresh(snapshot.get("as_of"),now,max_age,"account reconciliation")
    day=now.date().isoformat()
    if not snapshot.get("source") or snapshot.get("date")!=day or snapshot.get("complete") is not True:
        raise Block("RECONCILE: sourced complete same-day account snapshot required")
    if snapshot.get("opening_positions") != []:
        raise Block("RECONCILE: non-flat/unknown start-of-day requires explicit carry reconciliation")
    if identity(reservations)!=(broker,account) or reservations.get("date")!=day:
        raise Block("RECONCILE: reservation identity/date mismatch")
    orders=snapshot["orders"]
    if not isinstance(orders,list) or not isinstance(snapshot["positions"],list): raise Block("RECONCILE: explicit arrays required")
    fills=journal.fills(broker,account,day)
    if any(f["product"]!="MIS" for f in fills):
        raise Block("RECONCILE: carry fills require portfolio accounting; intraday risk ledger remains blocked")
    fee_meta={"provisional_fill_count":sum(f.get("fees_final") is not True for f in fills),"fee_basis":"final contract-note fees required"}
    if fee_policy is not None:
        from fee_policy import reserve_fees
        fills,fee_meta=reserve_fees(fills,fee_policy,instruments or [],rate_card,broker,account,now)
    by_order=defaultdict(list)
    net=defaultdict(int)
    for f in fills:
        by_order[f["order_id"]].append(f)
        net[(f["exchange"],f["symbol"],f["token"])]+=integer(f["quantity"])*(1 if f["side"]=="BUY" else -1)
    errors=[]
    known=set()
    active=set()
    order_map={}
    for o in orders:
        oid=o["order_id"]
        if not isinstance(oid,str) or not oid or oid in known: raise Block("RECONCILE: duplicate/missing order ID")
        known.add(oid);order_map[oid]=o
        if o.get("product")!="MIS" or o.get("side") not in {"BUY","SELL"}: raise Block("RECONCILE: unsupported order")
        qty=integer(o["quantity"]);filled=integer(o["filled_quantity"],minimum=0)
        if filled>qty: raise Block("RECONCILE: fills exceed ordered quantity")
        if o["status"] not in {"OPEN","PARTIAL","FILLED","CANCELLED","REJECTED","UNKNOWN"}: raise Block("RECONCILE: unknown status vocabulary")
        if o["status"]=="UNKNOWN": errors.append("ambiguous order "+oid)
        if o["status"] in {"OPEN","PARTIAL","UNKNOWN"}: active.add(oid)
        if (o["status"]=="FILLED" and filled!=qty) or (o["status"]=="REJECTED" and filled): errors.append("inconsistent terminal status "+oid)
        rows=by_order[oid]
        if sum(integer(f["quantity"]) for f in rows)!=filled: errors.append("fill quantity mismatch "+oid)
        if any(any(f.get(k)!=o.get(k) for k in ("exchange","symbol","token","side","product")) for f in rows): errors.append("fill identity mismatch "+oid)
    if set(by_order)-known: errors.append("journal orders absent from broker snapshot")
    observed={}
    values={}
    for p in snapshot["positions"]:
        key=(p["exchange"],p["symbol"],p["token"])
        if key in observed or p.get("product")!="MIS": raise Block("RECONCILE: duplicate/unsupported position")
        observed[key]=signed(p["quantity"])
        value=number(p["position_value_inr"])
        if observed[key] and value<=0: raise Block("RECONCILE: position valuation required")
        values[p["exchange"]+":"+p["symbol"]]=float(value)
    if {k:v for k,v in net.items() if v}!={k:v for k,v in observed.items() if v}: errors.append("position/fill mismatch")
    # Groups bind entry plus reducing exits. Count full planned risk even after gains.
    claimed=set();seen=[];risk=number(0);trades=0
    for group in reservations["entries"]:
        ids=[group["entry_order_id"],*group.get("exit_order_ids",[])]
        if len(set(ids))!=len(ids) or claimed.intersection(ids): raise Block("RECONCILE: order assigned twice")
        claimed.update(ids)
        planned=positive(group["planned_loss_inr"],"planned loss including stop charges")
        if ids[0] not in order_map: errors.append("reserved entry absent from snapshot");continue
        entry=order_map[ids[0]]
        entryfills=by_order[ids[0]]
        exits=[f for oid in ids[1:] for f in by_order[oid]]
        exit_orders=[order_map[oid] for oid in ids[1:] if oid in order_map]
        if len(exit_orders)!=len(ids)-1: errors.append("exit absent from snapshot")
        if any(any(x.get(k)!=entry.get(k) for k in ("exchange","symbol","token","product")) or x.get("side")==entry["side"] for x in exit_orders):
            errors.append("exit identity/direction mismatch")
        eq=sum(integer(f["quantity"]) for f in entryfills)
        xq=sum(integer(f["quantity"]) for f in exits)
        if xq>eq: errors.append("exit reverses entry")
        if sum(integer(x["quantity"])-integer(x["filled_quantity"],minimum=0) for x in exit_orders if x["order_id"] in active)>eq-xq:
            errors.append("pending exits can reverse position")
        if eq or ids[0] in active:
            seen.append([entry["exchange"],entry["symbol"],entry["side"]]);trades+=1
            # Average entry cost allocation, used only for conservative loss reservation.
            average=sum(number(f["price"])*f["quantity"] for f in entryfills)/eq if eq else number(0)
            gross=sum((number(f["price"])-average)*f["quantity"]*(1 if entry["side"]=="BUY" else -1) for f in exits)
            fees=sum((sum((number(v) for v in f["fees"].values()),number(0)) for f in entryfills+exits),number(0))
            risk+=max(planned,fees-gross)
    if known-claimed: errors.append("unmapped broker orders")
    if fee_policy is None and any(f.get("fees_final") is not True for f in fills): errors.append("fees not reconciled to contract note")
    return {"source":"local_journal+"+snapshot["source"],"broker":broker,"account_id":account,"date":day,
            "as_of":snapshot["as_of"],"reconciled":not errors,"issues":errors,
            "cash_available":float(number(snapshot["cash_available"])),"margin_available":float(number(snapshot["margin_available"])),
            "daily_risk_used":money(risk),"trades_today":trades,"orders_today":len(orders),"seen_entries":seen,
            "position_values":values,"snapshot_hash":fingerprint(snapshot),"reservations_hash":fingerprint(reservations),**fee_meta}


def export_csv(journal,broker,account,day,path):
    fields=["fill_id","order_id","at","exchange","symbol","token","side","quantity","price","fees_final","fees","source"]
    with Path(path).open("x",newline="",encoding="utf-8") as stream:
        writer=csv.DictWriter(stream,fieldnames=fields);writer.writeheader()
        for row in journal.fills(broker,account,day):
            values={k:row.get(k) for k in fields};values["fees"]=json.dumps(values["fees"],sort_keys=True)
            # Neutralize spreadsheet formula injection in imported textual columns.
            values={k:("'"+v if isinstance(v,str) and v.startswith(("=","+","-","@","\t","\r")) else v) for k,v in values.items()}
            writer.writerow(values)


def main(argv=None):
    from trader import read,write
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument("command",choices=["import","fees","reconcile","export","pending","ack"])
    p.add_argument("--db",required=True);p.add_argument("--input");p.add_argument("--reservations");p.add_argument("--out")
    p.add_argument("--broker");p.add_argument("--account");p.add_argument("--date");p.add_argument("--id");p.add_argument("--now")
    p.add_argument("--portfolio",action="store_true",help="Explicitly allow canonical CNC/NRML fills for accounting only")
    a=p.parse_args(argv);j=None
    try:
        now=instant(a.now);j=Journal(a.db)
        if a.portfolio and a.command!="import": raise Block("INPUT: --portfolio belongs to fill import only")
        if a.command=="import": result=j.import_fills(read(a.input),now,allow_carry=a.portfolio)
        elif a.command=="fees": result=j.contract_fees(read(a.input),now)
        elif a.command=="reconcile":
            result=reconcile(j,read(a.input),read(a.reservations),now)
            if not a.out: raise Block("OUTPUT: --out required")
            write(a.out,result)
        elif a.command=="export":
            if not all((a.broker,a.account,a.date,a.out)): raise Block("INPUT: broker/account/date/out required")
            export_csv(j,a.broker,a.account,a.date,a.out);result={"exported":a.out,"tax_computation":False}
        elif a.command=="pending": result={"notices":j.pending()}
        else: result=j.acknowledge(a.id,now)
        print(json.dumps(result,indent=2));return 2 if result.get("reconciled") is False else 0
    except (Block,ValueError,KeyError,TypeError,OSError,sqlite3.Error):
        print(json.dumps({"status":"BLOCK","reason":"Journal input/storage validation failed; private content suppressed"}));return 2
    finally:
        if j: j.close()


if __name__=="__main__": sys.exit(main())
