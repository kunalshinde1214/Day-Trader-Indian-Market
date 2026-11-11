import copy
from datetime import timedelta
import json
from pathlib import Path
import sys
import unittest

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/"scripts"))
from core import (Block, number, instant, fingerprint, orb, plan_from_hit, charges, size,
    risk_batch, ticket, reducing_exit, payload, alerts, check_order, completed_bars)
from demo import inputs
from replay import replay, outcome
from trader import scan


class CoreTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.config=json.loads((ROOT/"assets/config.example.json").read_text())
        cls.card=json.loads((ROOT/"assets/charges.example.json").read_text())
        cls.base=inputs(cls.config)
        now,i,ctx,h,u,b=cls.base
        hit=orb(b["instruments"]["NSE:DEMO-EQ"]["bars"],now)
        cls.plan=plan_from_hit(hit,i,cls.config,now)
        cls.replayed=replay(cls.plan,i,h,cls.config,cls.card)

    def setUp(self):
        self.c=copy.deepcopy(self.config);self.r=copy.deepcopy(self.card)
        self.now,self.i,self.ctx,self.h,self.u,self.b=copy.deepcopy(self.base)
        self.p=copy.deepcopy(self.plan)
        self.ctx["replay"]=copy.deepcopy(self.replayed)

    def order(self):
        return size(self.p,self.i,self.c,self.ctx,self.r,self.now)

    def test_end_to_end_costs_in_risk_and_ticket(self):
        o=self.order();t=ticket(o,self.i,self.c,self.ctx,self.r,self.now)
        self.assertEqual(t["risk"]["status"],"PASS")
        self.assertFalse(t["submitted"])
        self.assertLessEqual(o["metrics"]["loss_at_stop_inr"],1000)
        gross=abs(self.p["entry"]-self.p["stop"])*o["quantity"]
        self.assertGreater(o["metrics"]["loss_at_stop_inr"],gross)
        self.assertNotIn("algo_id",t["kite_payload_preview"])
        self.assertNotIn("stop",t["kite_payload_preview"])

    def test_option_worked_example(self):
        i={**self.i,"kind":"option","exchange":"NFO"}
        c=charges(100,120,65,"BUY",i,self.r,"2026-10-31")
        self.assertEqual(c["total"],65.11)
        self.assertEqual(c["stt"],11.70)

    def test_short_charges_buy_sell_sides(self):
        a=charges(120,100,65,"SELL",{**self.i,"kind":"option","exchange":"NFO"},self.r,"2026-10-31")
        b=charges(100,120,65,"BUY",{**self.i,"kind":"option","exchange":"NFO"},self.r,"2026-10-31")
        self.assertEqual(a,b)

    def test_rate_card_expires(self):
        with self.assertRaisesRegex(Block,"RATES"):
            charges(100,110,1,"BUY",self.i,self.r,"2026-12-01")

    def test_invalid_numbers(self):
        for v in (float("nan"),float("inf"),True,-1,"bad"):
            with self.subTest(v=v),self.assertRaises(Block): number(v)

    def test_live_disabled(self):
        self.c["paper"]=False
        with self.assertRaisesRegex(Block,"LIVE_DISABLED"): self.order()

    def test_missing_context_fails_closed(self):
        for key in ("session","restrictions","event","account","replay","quote"):
            ctx=copy.deepcopy(self.ctx);del ctx[key]
            # Build valid order first, then remove safety evidence.
            result=risk_batch([self.order()],{"NSE:DEMO-EQ":self.i},self.c,ctx,self.r,self.now)
            self.assertEqual(result["results"][0]["status"],"BLOCK",key)

    def test_stale_and_future_snapshot(self):
        for seconds in (-61,1):
            self.ctx["account"]["as_of"]=(self.now+timedelta(seconds=seconds)).isoformat()
            with self.assertRaisesRegex(Block,"STALE"): self.order()

    def test_clock_requires_offset(self):
        with self.assertRaisesRegex(Block,"TIMEZONE"): instant("2026-10-30T09:35:00")
        self.assertEqual(instant("2026-10-30T04:05:00Z"),self.now)

    def test_master_stale(self):
        self.i["master_date"]="2026-10-29"
        with self.assertRaisesRegex(Block,"MASTER"): self.order()

    def test_stop_wrong_side_and_off_tick(self):
        for stop in (103,100.031):
            self.p["stop"]=stop
            with self.assertRaises(Block): self.order()

    def test_replay_bound_to_plan(self):
        self.p["target"]+=1
        with self.assertRaisesRegex(Block,"REPLAY"): self.order()

    def test_replay_sampling_and_seed_bound_to_risk(self):
        for change in ({"replay_sampling":"session"}, {"replay_sampling":"block","replay_block_bars":3}, {"replay_seed":20}):
            self.c={**self.config,**change}
            with self.assertRaisesRegex(Block,"REPLAY"):self.order()

    def test_dependent_replay_reproducible_and_accepted(self):
        for mode in ("block","session"):
            self.c.update(replay_sampling=mode,replay_paths=20)
            result=replay(self.p,self.i,self.h,self.c,self.r)
            self.assertEqual(result,replay(self.p,self.i,self.h,self.c,self.r))
            self.assertEqual(result["sampling"]["mode"],mode)
            self.ctx["replay"]=result
            self.order()

    def test_legacy_unlabelled_replay_requires_regeneration(self):
        self.ctx["replay"].pop("sampling")
        with self.assertRaisesRegex(Block,"REPLAY"):self.order()

    def test_block_length_change_invalidates_replay(self):
        self.c.update(replay_sampling="block",replay_block_bars=3,replay_paths=20)
        self.ctx["replay"]=replay(self.p,self.i,self.h,self.c,self.r)
        self.c["replay_block_bars"]=6
        with self.assertRaisesRegex(Block,"REPLAY"):self.order()

    def test_quote_beyond_target(self):
        self.ctx["quote"]["price"]=self.p["target"]
        with self.assertRaisesRegex(Block,"EXPIRED"): self.order()

    def test_holiday_and_halt(self):
        for field,value in (("is_open_day",False),("halted",True)):
            ctx=copy.deepcopy(self.ctx);ctx["session"][field]=value
            with self.assertRaisesRegex(Block,"CLOCK"): size(self.p,self.i,self.c,ctx,self.r,self.now)

    def test_event_block_and_half_size(self):
        normal=self.order()["quantity"]
        self.ctx["event"]["risk_multiplier"]=0
        with self.assertRaisesRegex(Block,"EVENT"): self.order()
        self.ctx["event"]["risk_multiplier"]=0.5
        with self.assertRaisesRegex(Block,"EVENT"): self.order()
        self.ctx["event"]["override_confirmed"]=True
        smaller=self.order()
        self.assertLess(smaller["quantity"],normal)
        self.assertLessEqual(smaller["metrics"]["loss_at_stop_inr"],500)

    def test_every_restriction_blocks(self):
        for field in self.ctx["restrictions"]["instruments"]["NSE:DEMO-EQ"]:
            ctx=copy.deepcopy(self.ctx)
            ctx["restrictions"]["instruments"]["NSE:DEMO-EQ"][field]=True
            with self.subTest(field=field),self.assertRaisesRegex(Block,"RESTRICTIONS"):
                size(self.p,self.i,self.c,ctx,self.r,self.now)

    def test_absent_restriction_is_not_false(self):
        del self.ctx["restrictions"]["instruments"]["NSE:DEMO-EQ"]["ban"]
        with self.assertRaisesRegex(Block,"RESTRICTIONS"): self.order()

    def test_duplicate_batch_reservation(self):
        o=self.order()
        r=risk_batch([o,o],{"NSE:DEMO-EQ":self.i},self.c,self.ctx,self.r,self.now)
        self.assertEqual([x["status"] for x in r["results"]],["PASS","BLOCK"])
        self.assertIn("DUPLICATE",r["results"][1]["reason"])
        self.assertEqual(self.ctx["account"]["daily_risk_used"],0)

    def test_daily_budget_cannot_be_reset_by_positive_results(self):
        self.ctx["account"]["daily_risk_used"]=2990
        with self.assertRaises(Block): self.order()

    def test_zero_cash(self):
        self.ctx["account"]["cash_available"]=9000
        with self.assertRaisesRegex(Block,"ZERO_SIZE"): self.order()

    def test_trade_and_order_counts(self):
        for field,value in (("trades_today",3),("orders_today",10)):
            ctx=copy.deepcopy(self.ctx);ctx["account"][field]=value
            with self.assertRaisesRegex(Block,"DAILY_COUNT"): size(self.p,self.i,self.c,ctx,self.r,self.now)

    def test_tampered_quantity_and_metrics(self):
        o=self.order();o["quantity"]+=100;o["metrics"]["loss_at_stop_inr"]=0
        with self.assertRaisesRegex(Block,"SIZE"): check_order(o,self.i,self.c,self.ctx,self.r,self.now)

    def test_fractional_quantity(self):
        o=self.order();o["quantity"]=1.5
        with self.assertRaises(Block): check_order(o,self.i,self.c,self.ctx,self.r,self.now)

    def test_derivative_requires_exact_margin(self):
        self.i.update(kind="option",exchange="NFO",lot_size=65,expiry="2026-11-03",underlying_type="index")
        self.p["exchange"]="NFO"
        self.p["target"]=110
        self.ctx["restrictions"]["instruments"]["NFO:DEMO-EQ"]=self.ctx["restrictions"]["instruments"].pop("NSE:DEMO-EQ")
        self.ctx["quote"]["exchange"]="NFO"
        self.ctx["replay"]["plan_hash"]=fingerprint(self.p)
        o=self.order()
        self.assertEqual(o["quantity"]%65,0)
        with self.assertRaises(KeyError): check_order(o,self.i,self.c,self.ctx,self.r,self.now)
        self.ctx["margin"]={"source":"kite","account_id":self.c["account_id"],"as_of":self.now.isoformat(),"required_inr":20000,"payload_hash":fingerprint(payload(o))}
        self.assertEqual(check_order(o,self.i,self.c,self.ctx,self.r,self.now)["status"],"PASS")
        self.ctx["margin"]["payload_hash"]="wrong"
        with self.assertRaisesRegex(Block,"MARGIN"): check_order(o,self.i,self.c,self.ctx,self.r,self.now)

    def test_reducing_exits_do_not_reverse(self):
        pos={"symbol":"DEMO-EQ","exchange":"NSE","product":"MIS","quantity":10}
        self.assertEqual(reducing_exit(pos,"SELL",10)["quantity"],10)
        for side,qty in (("BUY",1),("SELL",11),("SELL",0)):
            with self.assertRaises(Block): reducing_exit(pos,side,qty)
        self.assertEqual(reducing_exit({**pos,"quantity":-10},"BUY",5)["quantity"],5)

    def test_forming_bar_not_trigger(self):
        rows=self.b["instruments"]["NSE:DEMO-EQ"]["bars"]
        result=orb(rows,self.now-timedelta(seconds=1))
        self.assertEqual(result["state"],"waiting")

    def test_missing_and_duplicate_bars(self):
        rows=self.b["instruments"]["NSE:DEMO-EQ"]["bars"]
        with self.assertRaisesRegex(Block,"duplicate"): orb(rows+[rows[0]],self.now)
        later={**rows[-1],"at":(self.now).isoformat()}
        with self.assertRaisesRegex(Block,"missing"): orb([rows[0],rows[2],rows[3],later],self.now+timedelta(minutes=5))

    def test_corrupt_bar(self):
        rows=self.b["instruments"]["NSE:DEMO-EQ"]["bars"]
        rows[0]["high"]=98
        with self.assertRaisesRegex(Block,"OHLC"): orb(rows,self.now)

    def test_scan_partial_counts(self):
        self.u["instruments"].append({**self.i,"symbol":"MISSING","average_daily_turnover_inr":200000000})
        result=scan(self.u,self.b,self.ctx,self.c,self.now)
        self.assertEqual((result["requested"],result["processed"],result["excluded_count"]),(2,1,1))

    def test_liquidity_crore_conversion(self):
        self.u["instruments"][0]["average_daily_turnover_inr"]=99999999
        self.assertEqual(scan(self.u,self.b,self.ctx,self.c,self.now)["processed"],0)

    def test_replay_seed_and_normalization(self):
        again=replay(self.p,self.i,self.h,self.c,self.r)
        self.assertEqual(again,self.replayed)
        self.assertAlmostEqual(sum(again[k] for k in ("target_first_pct","stop_first_pct","still_open_pct")),100,places=1)
        self.assertEqual(again["paths"],10000)
        self.assertEqual(again["history_sessions"],25)

    def test_replay_no_same_day_lookahead(self):
        self.h["bars"]+=self.b["instruments"]["NSE:DEMO-EQ"]["bars"]
        changed=replay(self.p,self.i,self.h,self.c,self.r)
        self.assertEqual(changed["stop_first_pct"],self.replayed["stop_first_pct"])
        self.assertEqual(changed["history_sessions"],25)

    def test_replay_insufficient_and_wrong_instrument(self):
        self.h["symbol"]="OTHER"
        with self.assertRaisesRegex(Block,"HISTORY"): replay(self.p,self.i,self.h,self.c,self.r)
        self.h["symbol"]="DEMO-EQ";self.h["bars"]=self.h["bars"][:70]
        with self.assertRaisesRegex(Block,"HISTORY"): replay(self.p,self.i,self.h,self.c,self.r)

    def test_ambiguous_bar_and_gap_stop(self):
        self.assertEqual(outcome((100,110,90,101),95,105,True),("stop",95))
        self.assertEqual(outcome((90,100,85,99),95,105,True),("stop",90))
        self.assertEqual(outcome((110,115,99,101),105,95,False),("stop",110))

    def test_alert_is_one_shot_and_checks_quote(self):
        q={**self.ctx["quote"],"price":self.p["stop"]}
        result=alerts(self.p,q,self.now,self.c)
        self.assertIn("STOP_LEVEL_REACHED",result["alerts"])
        self.assertFalse(result["continuous_monitoring"])
        q["as_of"]=(self.now-timedelta(minutes=5)).isoformat()
        with self.assertRaises(Block): alerts(self.p,q,self.now,self.c)


if __name__=="__main__": unittest.main()
