"""Synthetic scenario tests; no accounts, no performance claims."""
import contextlib
import copy
from datetime import timedelta
import io
import json
from pathlib import Path
import sys
import tempfile
import unittest

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/"scripts"))
from core import Block, instant, setup_hit, plan_from_hit, fingerprint, size, ticket, validate_config
from demo import inputs
from patterns import DAILY_SETUPS, daily_reference, rsi
from replay import replay
from trader import main, scan

KEY="NSE:DEMO-EQ"


class PatternTests(unittest.TestCase):
    def fixture(self, setup="gap_and_go"):
        self.c=json.loads((ROOT/"assets/config.example.json").read_text())
        self.c.update(setup=setup,replay_paths=100)
        self.card=json.loads((ROOT/"assets/charges.example.json").read_text())
        self.now,self.i,self.ctx,self.h,self.u,self.candles=inputs(self.c)
        self.data=self.candles["instruments"][KEY]
        self.rows=self.data["bars"]

    def hit(self, now=None):
        return setup_hit(self.rows,now or self.now,self.c,self.i,self.data.get("prior_close"),self.data.get("daily_reference"))

    def plan(self):
        return plan_from_hit(self.hit(),self.i,self.c,self.now)

    def test_gap_up_and_down_geometry(self):
        self.fixture()
        p=self.plan()
        self.assertEqual((p["entry"],p["stop"],p["target"]),(102,100.4,103.6))
        for b in self.rows:
            b.update(open=200-b["open"],high=200-b["low"],low=200-b["high"],close=200-b["close"])
        p=self.plan()
        self.assertEqual((p["side"],p["entry"],p["stop"],p["target"]),("SELL",98,99.6,96.4))

    def test_gap_exact_threshold_and_hold(self):
        self.fixture()
        self.rows[0]["open"]=100.7
        self.assertEqual(self.hit()["state"],"triggered")
        self.rows[0]["open"]=100.699
        self.assertEqual(self.hit()["state"],"waiting")
        for index in range(3):
            self.fixture()
            self.rows[index]["low"]=100
            self.assertEqual(self.hit()["state"],"waiting")
        self.fixture()
        self.rows[-1].update(close=100.8,low=100.6)
        self.assertEqual(self.hit()["state"],"waiting")

    def test_gap_requires_completed_opening_and_fixed_window(self):
        self.fixture()
        self.assertEqual(self.hit(self.now-timedelta(seconds=1))["state"],"waiting")
        self.c["no_entries_before"]="09:35"
        self.assertEqual(self.hit()["state"],"waiting")

    def test_gap_requires_source_calendar_identity_and_availability(self):
        for field,value in (("broker","dhan"),("token","other"),("source",""),("calendar_source",""),
                            ("corporate_actions_clear",False),("price_basis","adjusted"),
                            ("available_at","2026-10-30T09:15:00+05:30"),("available_at",None),
                            ("previous_session","2026-10-30"),("for_session","2026-10-29")):
            with self.subTest(field=field,value=value):
                self.fixture()
                self.data["prior_close"][field]=value
                with self.assertRaises(Block): self.hit()
        self.fixture()
        del self.data["prior_close"]
        result=scan(self.u,self.candles,self.ctx,self.c,self.now)
        self.assertEqual(result["excluded_count"],1)
        self.assertIn("GAP_REFERENCE",result["excluded"][0]["reason"])

    def test_gap_threshold_changes_require_new_evidence(self):
        self.fixture()
        p=self.plan()
        self.ctx["replay"]=replay(p,self.i,self.h,self.c,self.card)
        self.assertFalse(ticket(size(p,self.i,self.c,self.ctx,self.card,self.now),self.i,self.c,self.ctx,self.card,self.now)["submitted"])
        self.c["gap_min_pct"]=0.8
        with self.assertRaisesRegex(Block,"SETUP"): replay(p,self.i,self.h,self.c,self.card)
        with self.assertRaisesRegex(Block,"SETUP"): size(p,self.i,self.c,self.ctx,self.card,self.now)

    def test_invalid_thresholds(self):
        self.fixture()
        for v in (0,-1,True,101):
            with self.assertRaises(Block): validate_config({**self.c,"gap_min_pct":v})

    def test_all_four_setups_use_same_rule_in_historical_replay(self):
        for setup in ["gap_and_go",*sorted(DAILY_SETUPS)]:
            with self.subTest(setup=setup):
                self.fixture(setup)
                for k,b in enumerate(self.h["bars"]):
                    slot=k%70
                    template=self.rows[slot] if slot<len(self.rows) else dict(open=102,high=102.1,low=101.9,close=102,volume=100000)
                    b.update({f:template[f] for f in ("open","high","low","close","volume")})
                p=self.plan()
                result=replay(p,self.i,self.h,self.c,self.card)
                self.assertEqual(result["historical_same_side_firings"],25)
                self.assertEqual(result["historical_outcomes"],{"still_open":25})
                self.assertEqual(result,replay(p,self.i,self.h,self.c,self.card))
                self.h["bars"]+=self.rows
                self.assertEqual(result["target_first_pct"],replay(p,self.i,self.h,self.c,self.card)["target_first_pct"])

    def test_each_historical_session_requires_point_in_time_reference(self):
        for setup in ["gap_and_go",*sorted(DAILY_SETUPS)]:
            self.fixture(setup)
            key="prior_closes" if setup=="gap_and_go" else "daily_references"
            del self.h[key][next(iter(self.h[key]))]
            with self.assertRaises(Block): replay(self.plan(),self.i,self.h,self.c,self.card)

    def test_no_later_bar_changes_first_signal_or_evidence(self):
        for setup in ["gap_and_go",*sorted(DAILY_SETUPS)]:
            self.fixture(setup)
            first=self.hit()
            self.rows.append(dict(at=self.now.isoformat(),open=102,high=130,low=80,close=81,volume=1000000000))
            self.assertEqual(first,self.hit(self.now+timedelta(minutes=5)))

    def test_missing_duplicate_and_zero_volume_bars_block(self):
        for setup in ["gap_and_go",*sorted(DAILY_SETUPS)]:
            for failure in ("missing","duplicate","volume"):
                self.fixture(setup)
                if failure=="missing": self.rows.pop(1)
                elif failure=="duplicate": self.rows.append(copy.deepcopy(self.rows[0]))
                else: self.rows[0]["volume"]=0
                with self.assertRaises(Block): self.hit()

    def test_daily_refs_fail_on_missing_calendar_bars_lookahead_adjustments(self):
        mutations=[lambda r:r["bars"].pop(),lambda r:r.update(previous_session="2026-10-28"),
                   lambda r:r.update(corporate_actions_clear=False),lambda r:r.update(token="bad"),
                   lambda r:r["bars"][-1].update(available_at="2026-10-30T16:00:00+05:30"),
                   lambda r:r["bars"][-1].update(low=999)]
        for mutate in mutations:
            self.fixture("breakout_20d")
            mutate(self.data["daily_reference"])
            with self.assertRaises(Block): self.hit()

    def test_breakout_strict_close_high_and_not_high_wick(self):
        self.fixture("breakout_20d")
        self.rows[-1]["close"]=101
        self.assertEqual(self.hit()["state"],"waiting")
        self.rows[-1]["close"]=101.05
        self.assertEqual(self.hit()["state"],"triggered")

    def test_bull_flag_requires_pole_and_narrow_range(self):
        self.fixture("bull_flag")
        self.assertEqual(self.hit()["state"],"triggered")
        for b in self.data["daily_reference"]["bars"]:
            b.update(open=101,high=101.2,low=100.8,close=101)
        self.assertEqual(self.hit()["state"],"waiting")
        self.fixture("bull_flag")
        self.data["daily_reference"]["bars"][-2].update(open=90,high=90.2,low=89.8,close=90)
        self.assertEqual(self.hit()["state"],"waiting")

    def test_rsi_flat_up_down_and_bounce_gate(self):
        self.assertEqual(rsi([100]*30),50)
        self.assertEqual(rsi(range(1,31)),100)
        self.assertEqual(rsi(range(30,0,-1)),0)
        self.fixture("oversold_bounce")
        self.assertEqual(self.hit()["diagnostics"]["prior_rsi14"],0)
        for b in self.data["daily_reference"]["bars"]:
            b.update(open=101,high=101.2,low=100.8,close=101)
        self.assertEqual(self.hit()["state"],"waiting")

    def test_plans_copy_and_hash_the_reference_and_use_swing_stop(self):
        self.fixture("breakout_20d")
        p=self.plan()
        self.assertEqual((p["entry"],p["stop"],p["target"]),(102,98.9,105.1))
        before=fingerprint(p)
        self.data["daily_reference"]["source"]="new"
        self.assertEqual(fingerprint(p),before)
        self.assertNotEqual(fingerprint(self.plan()),before)

    def test_twenty_new_full_broker_setup_demo_journeys(self):
        for setup in ["gap_and_go",*sorted(DAILY_SETUPS)]:
            for broker in ("kite","upstox","dhan","angel","fyers"):
                with self.subTest(setup=setup,broker=broker), tempfile.TemporaryDirectory() as d:
                    out=io.StringIO()
                    with contextlib.redirect_stdout(out): code=main(["demo","--setup",setup,"--broker",broker,"--workdir",d])
                    self.assertEqual(code,0,out.getvalue())
                    self.assertEqual(json.loads(out.getvalue())["risk"],"PASS")
                    rep=json.loads((Path(d)/"replay.json").read_text())
                    p=json.loads((Path(d)/"plan.json").read_text())
                    self.assertEqual((rep["paths"],rep["plan_hash"]),(10000,fingerprint(p)))
                    self.assertFalse(json.loads((Path(d)/"ticket.json").read_text())["submitted"])


if __name__=="__main__": unittest.main()
