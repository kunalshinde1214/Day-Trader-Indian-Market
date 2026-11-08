import copy
from datetime import timedelta
import json
from pathlib import Path
import sys
import tempfile
import unittest

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/"scripts"))
from core import Block, instant, plan_from_hit, setup_hit, size, charges, fingerprint
from demo import inputs
from journal import Journal,reconcile,export_csv
from watch import cycle,panic_preview
from data import rss,members_csv,build_universe,references,event_context,analyst,disclosures
from broker_data import quote_key,normalize_quote,refresh
from market import flows,expiry,derivative_diagnostics
from margin_size import propose
from replay import replay
from trader import write,runtime_context


class OperationsTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.w=Path(self.tmp.name)
        self.j=Journal(self.w/"journal.sqlite")
        self.now=instant("2026-10-30T09:35:00+05:30")
        self.batch={"broker":"kite","account_id":"PAPER-DEMO","source":"fictional_contract_note","as_of":self.now.isoformat(),"fills":[]}
        self.fill={"fill_id":"f1","order_id":"o1","exchange":"NSE","symbol":"DEMO-EQ","token":"999999999","product":"MIS",
                   "at":"2026-10-30T09:34:00+05:30","source":"fictional_execution","side":"BUY","quantity":10,"price":100,
                   "fees":{"brokerage":1},"fees_final":True}
        self.snapshot={"broker":"kite","account_id":"PAPER-DEMO","date":"2026-10-30","source":"fictional_snapshot","as_of":self.now.isoformat(),
                       "complete":True,"opening_positions":[],"cash_available":90000,"margin_available":90000,"positions":[],"orders":[]}
        self.reservations={"broker":"kite","account_id":"PAPER-DEMO","date":"2026-10-30","entries":[]}

    def tearDown(self):self.j.close();self.tmp.cleanup()

    def add_entry(self):
        self.batch["fills"]=[self.fill];self.j.import_fills(self.batch,self.now)
        self.snapshot["orders"]=[{k:self.fill[k] for k in ("order_id","exchange","symbol","token","product","side","quantity")}]
        self.snapshot["orders"][0].update(status="FILLED",filled_quantity=10)
        self.snapshot["positions"]=[{k:self.fill[k] for k in ("exchange","symbol","token","product","quantity")}]
        self.snapshot["positions"][0]["position_value_inr"]=1000
        self.reservations["entries"]=[{"entry_order_id":"o1","exit_order_ids":[],"planned_loss_inr":30}]

    def rec(self):return reconcile(self.j,self.snapshot,self.reservations,self.now)

    def test_idempotent_fills_and_restart(self):
        self.batch["fills"]=[self.fill]
        self.assertEqual(self.j.import_fills(self.batch,self.now)["imported"],1)
        self.assertEqual(self.j.import_fills(self.batch,self.now)["duplicates"],1)
        self.j.close();self.j=Journal(self.w/"journal.sqlite")
        self.assertEqual(len(self.j.fills("kite","PAPER-DEMO","2026-10-30")),1)
        self.assertEqual(self.j.fills("dhan","PAPER-DEMO","2026-10-30"),[])

    def test_conflicting_duplicate_rolls_back_whole_batch(self):
        self.add_entry()
        self.batch["fills"]=[{**self.fill,"fill_id":"f2"},{**self.fill,"price":101}]
        with self.assertRaisesRegex(Block,"conflicting"):self.j.import_fills(self.batch,self.now)
        self.assertEqual(len(self.j.fills("kite","PAPER-DEMO","2026-10-30")),1)

    def test_invalid_fill_future_negative_fraction_and_missing_source(self):
        for field,v in (("quantity",-1),("quantity",1.5),("price",0),("source",""),("at","2026-10-30T09:36:00+05:30")):
            self.batch["fills"]=[{**self.fill,field:v}]
            with self.assertRaises(Block):self.j.import_fills(self.batch,self.now)

    def test_flat_empty_account_and_matching_position(self):
        self.assertTrue(self.rec()["reconciled"])
        self.add_entry()
        r=self.rec()
        self.assertTrue(r["reconciled"],r)
        self.assertEqual((r["daily_risk_used"],r["trades_today"],r["orders_today"]),(30,1,1))

    def test_acknowledgement_does_not_create_fill(self):
        self.add_entry()
        self.snapshot["orders"][0]["filled_quantity"]=9
        self.assertFalse(self.rec()["reconciled"])
        self.snapshot["orders"][0]["filled_quantity"]=10
        self.snapshot["positions"][0]["quantity"]=9
        self.assertFalse(self.rec()["reconciled"])

    def test_unknown_pending_unmapped_orders_block(self):
        self.add_entry()
        self.snapshot["orders"][0]["status"]="UNKNOWN"
        self.assertFalse(self.rec()["reconciled"])
        self.snapshot["orders"][0]["status"]="FILLED"
        self.reservations["entries"]=[]
        self.assertFalse(self.rec()["reconciled"])

    def test_pending_and_partial_orders_reserve_full_risk(self):
        self.snapshot["orders"]=[{k:self.fill[k] for k in ("order_id","exchange","symbol","token","product","side","quantity")}]
        self.snapshot["orders"][0].update(status="OPEN",filled_quantity=0)
        self.reservations["entries"]=[{"entry_order_id":"o1","planned_loss_inr":30}]
        self.assertEqual(self.rec()["daily_risk_used"],30)
        self.fill["quantity"]=4;self.batch["fills"]=[self.fill];self.j.import_fills(self.batch,self.now)
        self.snapshot["orders"][0].update(status="PARTIAL",filled_quantity=4)
        self.snapshot["positions"]=[{k:self.fill[k] for k in ("exchange","symbol","token","product","quantity")}]
        self.snapshot["positions"][0]["position_value_inr"]=400
        self.assertTrue(self.rec()["reconciled"])
        self.assertEqual(self.rec()["daily_risk_used"],30)

    def test_profit_never_refunds_risk_and_worse_actual_loss_increases_it(self):
        self.add_entry()
        exitfill={**self.fill,"fill_id":"f2","order_id":"x1","side":"SELL","price":90}
        self.batch["fills"]=[exitfill];self.j.import_fills(self.batch,self.now)
        self.snapshot["orders"].append({**self.snapshot["orders"][0],"order_id":"x1","side":"SELL"})
        self.snapshot["positions"]=[];self.reservations["entries"][0]["exit_order_ids"]=["x1"]
        r=self.rec();self.assertTrue(r["reconciled"],r);self.assertEqual(r["daily_risk_used"],102)
        # Independent account fixture for a profitable round trip.
        other=Journal(self.w/"profit.sqlite")
        try:
            other.import_fills({**self.batch,"fills":[self.fill,{**exitfill,"price":110}]},self.now)
            self.assertEqual(reconcile(other,self.snapshot,self.reservations,self.now)["daily_risk_used"],30)
        finally:other.close()

    def test_stale_partial_account_carry_and_mismatch_fail(self):
        for patch in ({"complete":False},{"opening_positions":None},{"account_id":"OTHER"},{"as_of":"2026-10-30T09:30:00+05:30"}):
            with self.assertRaises(Block):reconcile(self.j,{**self.snapshot,**patch},self.reservations,self.now)

    def test_provisional_fees_never_claim_contract_note_reconciled(self):
        self.fill["fees_final"]=False;self.add_entry()
        self.assertIn("fees not reconciled to contract note",self.rec()["issues"])

    def test_outbox_ack_is_explicit_and_survives_restart(self):
        key=self.j.enqueue({"message":"stop"})
        self.assertEqual(self.j.enqueue({"message":"stop"}),key)
        self.assertEqual(len(self.j.pending()),1)
        self.assertEqual(self.j.acknowledge(key,self.now),{"acknowledged":1})
        self.assertEqual(self.j.pending(),[])

    def test_csv_export_preserves_fees_and_neutralizes_formula(self):
        self.fill["symbol"]="=formula";self.add_entry()
        path=self.w/"fills.csv";export_csv(self.j,"kite","PAPER-DEMO","2026-10-30",path)
        self.assertIn("'=formula",path.read_text())
        with self.assertRaises(FileExistsError):export_csv(self.j,"kite","PAPER-DEMO","2026-10-30",path)

    def watcher_fixture(self):
        c=json.loads((ROOT/"assets/config.example.json").read_text())
        now,i,ctx,h,u,b=inputs(c)
        p=plan_from_hit(setup_hit(b["instruments"]["NSE:DEMO-EQ"]["bars"],now,c),i,c,now)
        for name,value in (("config",c),("instrument",i),("context",ctx),("candles",b),("plan",p)):write(self.w/(name+".json"),value)
        return c,ctx,i,p,h

    def test_watcher_stale_disconnect_recovery_and_no_duplicate_notices(self):
        self.watcher_fixture()
        result=cycle(self.w,self.j,self.now)
        self.assertEqual(result["state"],"running")
        count=len(self.j.pending());cycle(self.w,self.j,self.now)
        self.assertEqual(len(self.j.pending()),count)
        result=cycle(self.w,self.j,self.now+timedelta(minutes=2))
        self.assertEqual(result["state"],"disconnected");self.assertFalse(result["entry_allowed"])
        self.assertEqual(cycle(self.w,self.j,self.now)["state"],"running")

    def test_watcher_uses_selected_stream_and_blocks_stopped_stream(self):
        c,ctx,i,_,_=self.watcher_fixture(); c["quote_feed"]="kite_stream"; write(self.w/"config.json",c)
        quote={"source":"kite_websocket","broker":"kite",**{k:i[k] for k in ("exchange","symbol","token")},
               "price":102,"as_of":self.now.isoformat(),"exchange_as_of":self.now.isoformat(),"received_at":self.now.isoformat(),"instrument_hash":fingerprint(i)}
        stream={"source":"kite_websocket","as_of":self.now.isoformat(),"state":"connected","quotes":{i["token"]:quote}}
        write(self.w/"state/kite-stream.json",stream)
        self.assertEqual(cycle(self.w,self.j,self.now)["state"],"running")
        stream["state"]="stopped"; write(self.w/"state/kite-stream.json",stream)
        self.assertEqual(cycle(self.w,self.j,self.now)["state"],"disconnected")

    def test_local_halt_blocks_size_but_reducing_exit_preview_works(self):
        c,ctx,i,p,h=self.watcher_fixture();c["replay_paths"]=100
        card=json.loads((ROOT/"assets/charges.example.json").read_text())
        ctx["replay"]=replay(p,i,h,c,card)
        write(self.w/"state"/"halt.json",{"halted":True})
        with self.assertRaisesRegex(Block,"LOCAL_HALT"):size(p,i,c,runtime_context(self.w,ctx),card,self.now)
        self.add_entry();preview=panic_preview(self.snapshot)
        self.assertFalse(preview["submitted"])
        self.assertEqual(preview["reducing_exit_previews"][0]["quantity"],10)

    def test_required_monitor_must_bind_plan_and_account(self):
        c,ctx,i,p,h=self.watcher_fixture();c.update(replay_paths=100,monitor_required=True)
        card=json.loads((ROOT/"assets/charges.example.json").read_text());ctx["replay"]=replay(p,i,h,c,card)
        with self.assertRaises(Block):size(p,i,c,ctx,card,self.now)
        cycle(self.w,self.j,self.now);runtime_context(self.w,ctx)
        self.assertGreater(size(p,i,c,ctx,card,self.now)["quantity"],0)
        ctx["monitor"]["state"]="stopped"
        with self.assertRaisesRegex(Block,"MONITOR"):size(p,i,c,ctx,card,self.now)

    def test_rss_dedup_and_unknown_times_never_infer_clearance(self):
        item='<item><title>A</title><link>https://example.org/a</link><pubDate>Fri, 30 Oct 2026 09:30:00 +0530</pubDate></item>'
        raw=('<rss><channel>'+item+item+'<item><title>unknown</title></item></channel></rss>').encode()
        result=rss(raw,"official_fixture",self.now)
        self.assertEqual((len(result["records"]),len(result["unparsed"])),(1,1));self.assertFalse(result["complete"])
        self.assertIsNone(result["records"][0]["symbol"])
        with self.assertRaises(Block):rss(b'<!DOCTYPE rss><rss/>',"fixture",self.now)

    def test_membership_csv_rejects_changed_schema_and_duplicates(self):
        raw=b'Symbol,Series,ISIN Code\nABC,EQ,IN0001\n'
        self.assertEqual(members_csv(raw,"fixture",self.now)["members"][0]["symbol"],"ABC")
        with self.assertRaises(Block):members_csv(raw+b'ABC,EQ,IN0001\n',"fixture",self.now)
        with self.assertRaises(Block):members_csv(b'<html>blocked</html>',"fixture",self.now)

    def test_reference_builder_uses_explicit_prior_sessions(self):
        c=json.loads((ROOT/"assets/config.example.json").read_text());c["setup"]="breakout_20d"
        now,i,_,_,_,candles=inputs(c);ref=candles["instruments"]["NSE:DEMO-EQ"]["daily_reference"]
        daily={**ref,"bars":ref["bars"]}
        calendar={"source":"fictional_calendar","complete":True,"sessions":ref["sessions"]+[now.date().isoformat()]}
        result=references(daily,calendar,i,now.date().isoformat())
        self.assertEqual(result["prior_close"]["close"],101)
        daily["bars"]=daily["bars"][:-1]
        with self.assertRaises(KeyError):references(daily,calendar,i,now.date().isoformat())

    def test_event_review_requires_coverage_and_records_override(self):
        evidence={"source":"manual_official_review","as_of":self.now.isoformat(),"date":"2026-10-30","coverage_complete":True,
                  "events":[{"date":"2026-10-30","url":"https://example.org/event","reviewed_at":self.now.isoformat(),"impact":"major","resolved":False}]}
        self.assertEqual(event_context(evidence,self.now)["risk_multiplier"],0)
        evidence["override"]={"confirmed":True,"user_instruction":"Use half risk for paper analysis","at":self.now.isoformat()}
        self.assertEqual(event_context(evidence,self.now)["risk_multiplier"],0.5)
        evidence["coverage_complete"]=False
        with self.assertRaises(Block):event_context(evidence,self.now)

    def test_analyst_comparable_reports_and_missing_values(self):
        base={"period":"2026Q2","basis":"consolidated","currency":"INR","unit":"crore","duration_months":3,
              "published_at":self.now.isoformat(),"url":"https://example.org/report","metrics":{"revenue":100,"earnings":-2}}
        data={"previous":base,"current":{**base,"period":"2026Q3","metrics":{"revenue":120,"earnings":3,"debt":10}}}
        r=analyst(data);self.assertEqual(r["metrics"]["revenue"]["change_pct"],20);self.assertIsNone(r["metrics"]["earnings"]["change_pct"])
        self.assertEqual(r["metrics"]["debt"]["status"],"missing_comparison")
        data["current"]["basis"]="standalone"
        with self.assertRaises(Block):analyst(data)

    def test_disclosures_preserve_transaction_and_release_dates(self):
        row={"holder":"example","symbol":"ABC","exchange":"NSE","category":"PIT","transaction_date":"2026-10-28",
             "published_at":self.now.isoformat(),"url":"https://example.org/filing","amendment":False,"quantity":100}
        result=disclosures({"records":[row,row]},self.now);self.assertEqual(len(result["records"]),1)
        row["transaction_date"]="2026-10-31"
        with self.assertRaises(Block):disclosures({"records":[row]},self.now)

    def test_contract_fee_correction_is_audited_and_original_reimport_idempotent(self):
        self.fill["fees_final"]=False;self.add_entry()
        correction={"broker":"kite","account_id":"PAPER-DEMO","fill_id":"f1","source":"fictional_contract_note",
                    "as_of":self.now.isoformat(),"fees":{"brokerage":2,"stt":1}}
        self.assertTrue(self.j.contract_fees(correction,self.now)["updated"])
        self.assertTrue(self.rec()["reconciled"])
        self.assertEqual(self.j.import_fills(self.batch,self.now)["duplicates"],1)
        self.assertEqual(self.j.db.execute("SELECT COUNT(*) FROM fee_audit").fetchone()[0],1)
        self.assertFalse(self.j.contract_fees(correction,self.now)["updated"])

    def test_stop_notice_survives_stale_account_gate(self):
        c,ctx,i,p,h=self.watcher_fixture()
        ctx["quote"]["price"]=p["stop"]
        ctx["account"]["as_of"]="2026-10-30T09:30:00+05:30"
        write(self.w/"context.json",ctx)
        result=cycle(self.w,self.j,self.now)
        self.assertEqual(result["state"],"disconnected")
        self.assertIn("STOP_LEVEL_REACHED",result["active_alerts"])

    def test_segment_brokerage_minimum_flat_and_taxable_extra_costs(self):
        c,ctx,i,p,h=self.watcher_fixture()
        card=json.loads((ROOT/"assets/charges.example.json").read_text())
        card["segments"]["NSE:equity"].update(brokerage_rate=0.001,brokerage_min=5,ipft_rate=0.000000001,clearing_rate=0.0001)
        r=charges(100,100,10,"BUY",i,card,"2026-10-30")
        self.assertEqual(r["brokerage"],10)
        self.assertEqual(r["clearing"],0.2)
        card["segments"]["NSE:equity"]["brokerage_fixed"]=20
        self.assertEqual(charges(100,100,10,"BUY",i,card,"2026-10-30")["brokerage"],40)

    def test_broker_quote_identity_clock_and_kite_normalization(self):
        c,ctx,i,p,h=self.watcher_fixture();key=quote_key(i)
        data={"source":"kite","missing":[],"data":{key:{"instrument_token":i["token"],"last_trade_time":"2026-10-30 09:35:00","last_price":102}}}
        q=normalize_quote(data,i,self.now,60)
        self.assertEqual((q["symbol"],q["as_of"]),(i["symbol"],self.now.isoformat()))
        data["data"][key]["instrument_token"]="wrong"
        with self.assertRaises(Block):normalize_quote(data,i,self.now,60)
        for broker in ("upstox","dhan","angel","fyers"):
            _,i,_,_,_,_=inputs({**c,"broker":broker});key=quote_key(i)
            q={"source":broker,"exchange":i["exchange"],"token":i["token"],"price":102,"as_of":self.now.isoformat()}
            data={"source":broker,"quotes":{key:q},"missing":[]}
            self.assertEqual(normalize_quote(data,i,self.now,60)["symbol"],i["symbol"])
            with self.assertRaises(Block):normalize_quote(data,i,self.now+timedelta(minutes=2),60)

    def test_broker_refresh_rejects_missing_candle_prefix(self):
        c,ctx,i,p,h=self.watcher_fixture()
        bars=json.loads((self.w/"candles.json").read_text())["instruments"]["NSE:DEMO-EQ"]["bars"]
        class Fake:
            def candles(fake,*a,**kw):return {**i,"source":"kite","as_of":self.now.isoformat(),"bars":bars}
            def quotes(fake,*a):return {"source":"kite","missing":[],"data":{quote_key(i):{"instrument_token":i["token"],"last_trade_time":self.now.isoformat(),"last_price":102}}}
        self.assertEqual(len(refresh(Fake(),i,self.now)[0]["bars"]),4)
        bars.pop()
        with self.assertRaises(Block):refresh(Fake(),i,self.now)

    def test_flows_never_relabels_prior_date_and_rejects_future_release(self):
        data={"source":"https://example.org/flows","trading_date":"2026-10-29","published_at":"2026-10-29T19:00:00+05:30","provisional":True,
              "fii":{"buy_inr":20,"sell_inr":10},"dii":{"buy_inr":30,"sell_inr":10}}
        r=flows(data,self.now);self.assertEqual(r["regime"],"risk-on");self.assertEqual(r["trading_date"],"2026-10-29")
        data["published_at"]="2026-10-30T19:00:00+05:30"
        with self.assertRaises(Block):flows(data,self.now)

    def test_expiry_from_contract_not_weekday_and_missing_greeks_explicit(self):
        c,ctx,i,p,h=self.watcher_fixture()
        i.update(kind="option",exchange="NFO",expiry="2026-10-30",underlying_type="index")
        self.assertTrue(expiry({"source":"fixture","instruments":[i]},self.now)["contracts"][0]["expires_today"])
        data={**i,"source":"https://example.org/quote","as_of":self.now.isoformat(),"bid":99,"ask":101,"open_interest":1000}
        r=derivative_diagnostics(data,i,self.now);self.assertEqual(r["spread_pct_mid"],2);self.assertIn("iv",r["unavailable"])
        data["delta"]=0.5
        with self.assertRaises(Block):derivative_diagnostics(data,i,self.now)

    def test_exact_margin_search_does_not_scale_one_lot_or_mutate_plan(self):
        c,ctx,i,p,h=self.watcher_fixture();c.update(replay_paths=100,max_cost_fraction=0.5)
        i.update(kind="option",exchange="NFO",expiry="2026-11-05",underlying_type="index",lot_size=10)
        p.update(exchange="NFO");h.update(exchange="NFO")
        ctx["quote"]["exchange"]="NFO";ctx["restrictions"]["instruments"]["NFO:DEMO-EQ"]=ctx["restrictions"]["instruments"].pop("NSE:DEMO-EQ")
        card=json.loads((ROOT/"assets/charges.example.json").read_text());ctx["replay"]=replay(p,i,h,c,card)
        calls=[];before=fingerprint(p)
        class Fake:
            def account_id(fake):return "PAPER-DEMO"
            def margin(fake,payload):
                calls.append(payload["quantity"])
                return {"source":"kite","account_id":"PAPER-DEMO","as_of":self.now.isoformat(),"payload_hash":fingerprint(payload),"required_inr":1000000 if len(calls)==1 else 1000}
        r=propose(Fake(),p,i,c,ctx,card,clock=lambda:self.now,max_queries=2)
        self.assertEqual(calls[0]-calls[1],10);self.assertEqual(r["candidate"]["quantity"],calls[1]);self.assertEqual(fingerprint(p),before)
        calls.clear()
        with self.assertRaises(Block):propose(Fake(),p,i,c,ctx,card,clock=lambda:self.now,max_queries=1)

    def test_universe_turnover_coverage_excludes_incomplete_instruments(self):
        c,ctx,i,p,h=self.watcher_fixture()
        c["setup"]="breakout_20d";_,_,_,_,_,candles=inputs(c)
        daily=candles["instruments"]["NSE:DEMO-EQ"]["daily_reference"]["bars"][-20:]
        meta={"source":"fictional_source","as_of":self.now.isoformat()}
        members={**meta,"members":[{"exchange":"NSE","symbol":"DEMO-EQ"},{"exchange":"NSE","symbol":"MISSING"}]}
        evidence={"sessions":[b["date"] for b in daily],"previous_session":daily[-1]["date"],"calendar_source":"fixture",
                  "daily":[{"date":b["date"],"available_at":b["available_at"],"turnover_inr":200000000} for b in daily]}
        result=build_universe(members,{**meta,"instruments":[i]},{**meta,"instruments":{"NSE:DEMO-EQ":evidence}},self.now)
        self.assertEqual((result["requested"],result["processed"],len(result["excluded"])),(2,1,1))
        self.assertEqual(result["instruments"][0]["average_daily_turnover_inr"],200000000)


if __name__=="__main__":unittest.main()
