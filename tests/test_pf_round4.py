"""Offline contracts for the round-4 portfolio/F&O fixes (18 Nov 2026 review, market open). Never contacts the network.

1. holdings --summary: the long-term loss left over (and the tax) use the published demerger cost split the law applies;
   the full-typed-cost figure is shown second, labelled.
2. grid --summary: breakevens and P&L at -1 / +1 SD in the compact table.
3. chain --summary: during market hours the to-expiry expected move and 68% range are centred on the live level with the
   snapshot IV, labelled; the close-based ones are labelled "from the … close".
4. payoff --summary: "Already in this trade from the close?" is valued on the same intraday snapshot as "If placed now".
5. Margin on hedged index option positions: NSE gives no exposure offset (stated, sourced); broker calculators were lower.
6. ORB history wording names the side of the range.
C. Near-the-money OI walls beside the global maxima; max pain's distance from spot.
D. The participant-OI expiry caveat sits on the participant line; a chain-level expiry warning only for the selected symbol.
"""
from datetime import date, datetime
import json
import math
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from core import IST
import holdings_review as hr
from test_buy_price_check import SBIN_BARS, flat
from test_portfolio_valuation import CHARTS, Feed, QUESTION, REPORT, run as hr_run, today


# ---------------- 1. demerger cost split in the loss left over ----------------

# The reviewer's day: RELIANCE ≈₹1,358 after its 1:1 bonus, ITC ≈₹300 — the typed costs leave a long-term loss that the
# published cost splits (RELIANCE keeps 95.32%, ITC 86.49% of the cost) turn into a net long-term gain.
LOW_CHARTS = dict(CHARTS, RELIANCE=(CHARTS["RELIANCE"][0][:-1] + today(1358.0), CHARTS["RELIANCE"][1]),
                  ITC=(CHARTS["ITC"][0][:-1] + today(300.0), CHARTS["ITC"][1]))


class DemergerLossTests(unittest.TestCase):
    def test_loss_left_over_uses_the_cost_split_first(self):
        r = hr_run(feed=Feed(LOW_CHARTS))
        h = r["tax"]["hypothetical_sell_all"]
        # Typed cost: RELIANCE 30 × (1,358 − 2,400) + bonus lot 30 × 1,358, ITC 200 × (300 − 380); INFY −5,700 when long-term.
        typed = 30 * (1358 - 2400) + 30 * 1358 + 200 * (300 - 380)
        self.assertEqual((h["unused_long_term_loss_low"], h["unused_long_term_loss"]), (-typed, -typed + 5700))
        split = h["with_demerger_cost_split"]
        self.assertEqual((split["unused_long_term_loss_low"], split["unused_long_term_loss"]), (0, 0))
        self.assertTrue(split["changes_loss"])
        self.assertFalse(split["changes_figure"])     # long-term gains stay inside the exemption either way
        brief = hr.summary(r)
        line = next(l for l in brief.splitlines() if "Net long-term loss left over" in l)
        self.assertEqual(line, f"  - Net long-term loss left over: ₹0 on the demerger cost split the law applies to ITC and RELIANCE "
                               f"({hr.inr(-typed)} to {hr.inr(-typed + 5700)} on the full typed cost, which the law does not allow)")
        self.assertNotIn("carried forward", line)     # nothing left to carry forward on the legal basis
        md = hr.markdown(r)
        self.assertIn("does not change this figure. Net long-term loss left over after set-off: ₹0 with the split", md)
        self.assertIn("s.49(2C)/(2D)", md)

    def test_split_basis_leads_when_it_changes_the_tax_and_the_midpoint_follows_it(self):
        h = REPORT["tax"]["hypothetical_sell_all"]
        split, mid = h["with_demerger_cost_split"], h["at_range_midpoint"]
        brief = hr.summary(REPORT)
        lines = brief.splitlines()
        head = next(i for i, l in enumerate(lines) if l.startswith("- **If you sold everything today**"))
        self.assertIn(f"tax {hr.tax_range(split['estimated_total_tax_low'], split['estimated_total_tax'])} (a range", lines[head])
        self.assertTrue(lines[head + 1].startswith("  - Uses the published demerger cost split for ITC and RELIANCE"))
        self.assertTrue(lines[head + 1].endswith(hr.tax_range(h["estimated_total_tax_low"], h["estimated_total_tax"])))
        legal_mid = mid["with_demerger_cost_split"]
        self.assertNotEqual(legal_mid["estimated_total_tax"], mid["estimated_total_tax"])
        self.assertIn(f"in Oct 2026: tax {hr.tax_range(legal_mid['estimated_total_tax_low'], legal_mid['estimated_total_tax'])}", brief)   # round 6 wording
        # The demerged-shares scenario is compared with the split figure (both use the published split).
        self.assertIn("selling JIOFIN and ITCHOTELS too: tax ", brief)

    def test_no_demerger_parent_keeps_the_old_wording(self):
        loss = hr_run([{"symbol": "INFY", "quantity": 15, "average_price": 1500, "bought": "2023-01"}])
        self.assertIsNone(loss["tax"]["hypothetical_sell_all"].get("with_demerger_cost_split"))
        self.assertIn("  - Net long-term loss left over: ₹5,700 — it cannot reduce short-term gains", hr.summary(loss))


# ---------------- F&O fixtures ----------------

import contextlib
import io
from datetime import timedelta

import fno
import fno_margin
import public_market
from test_fno import RATE, SPOT, TRADE, bhavcopy
from test_fno_round2 import LiveFeeds, card_file, run
from test_fno_margin import SpanFeeds
from test_pf_round3 import BANKNIFTY_17NOV, NIFTY_17NOV, CalendarFeeds, real_17nov
from test_orb_quick import RANGE, TODAY as ORB_TODAY, day_bars, history, session

EVENING = datetime(2026, 11, 17, 20, 0, tzinfo=IST)


# ---------------- 2. grid --summary: breakevens and ±1 SD ----------------

class GridSummaryRound4Tests(unittest.TestCase):
    def check(self, clock, feeds):
        with tempfile.TemporaryDirectory() as d:
            argv = ["grid", "NIFTY", "--structure", "condor", "--width", "250", "--charges", card_file(d)]
            code, raw, err = run(argv + ["--format", "json"], feeds(), clock=clock)
            self.assertEqual(code, 0, err)
            rows = json.loads(raw)["grid"]["rows"]
            code, text, err = run(argv + ["--summary"], feeds(), clock=clock)
            self.assertEqual(code, 0, err)
        table = [l for l in text.splitlines() if l.startswith("| ") and not l.startswith("| ---")]
        self.assertEqual(table[0], "| # | Short / long strikes | Credit ₹ | Max loss ₹ | Breakevens | Profit chance | P&L at −1 / +1 SD ₹ | Margin ₹ |")
        self.assertEqual(len(table), 1 + len(rows))
        for line, x in zip(table[1:], rows):
            cells = [c.strip() for c in line.strip("|").split("|")]
            self.assertEqual(len(cells), 8)
            self.assertEqual(len(x["breakevens_after_costs"]), 2)        # a condor: lower and upper
            self.assertEqual(cells[4], " / ".join(fno.fmt(b, 0) for b in x["breakevens_after_costs"]))
            self.assertEqual(cells[6], f"{fno.signed(x['sd_pnl']['-1'][1], 0)} / {fno.signed(x['sd_pnl']['1'][1], 0)}")
        sd = rows[0]["sd_pnl"]
        self.assertIn(f"−1 / +1 SD: NIFTY at {fno.fmt(sd['-1'][0], 0)} / {fno.fmt(sd['1'][0], 0)} at expiry", text)
        self.assertIn("`--row N --chart FILE.svg`", text)
        return text, rows

    def test_close_and_placed_now(self):
        text, _ = self.check(EVENING, SpanFeeds)
        self.assertIn("At the Tue 17 Nov close", text)
        text, rows = self.check(datetime(2026, 11, 18, 10, 0, tzinfo=IST), lambda: SpanFeeds(snapshot="202611180945"))
        self.assertIn("If placed now", text)
        self.assertTrue(all(x.get("eod") for x in rows))     # the table shows the re-priced figures


# ---------------- 3. chain --summary: expected move from the live level ----------------

class ExpectedMoveTests(unittest.TestCase):
    def test_market_hours_line_is_centred_on_the_live_level(self):
        code, text, err = run(["chain", "NIFTY", "--summary", "--no-vix"], SpanFeeds(snapshot="202611180945"))
        self.assertEqual(code, 0, err)
        line = next(l for l in text.splitlines() if l.startswith("- Expected move to expiry"))
        code, raw, _ = run(["chain", "NIFTY", "--summary", "--no-vix", "--format", "json"], SpanFeeds(snapshot="202611180945"))
        data = json.loads(raw)
        n = data["now_estimate"]
        rng = fno.one_sd_range(n["forward"], n["atm_iv"], n["days_to_expiry"] / 365)
        self.assertAlmostEqual(n["range_1sd"][0], rng[0], places=6)
        self.assertEqual(line, f"- Expected move to expiry ±{fno.fmt(n['straddle'])} ({fno.pct(n['straddle'] / n['level'])}) by the now ATM "
                               f"straddle (≈ {fno.straddle_sd(n['straddle'], rng):.1f} SD); 68% implied range (1 SD) {fno.fmt(rng[0], 0)}–{fno.fmt(rng[1], 0)} around the forward {fno.fmt(n['forward'], 0)} — from live 24,900.00 at 09:55 IST, "
                               "snapshot IV")
        near = data["oi_walls_near"]                              # near walls use the same live-centred range
        self.assertEqual((near["window"], near["centre"]), (n["range_1sd"], n["level"]))
        self.assertIn("OI walls near spot (largest OI within the 1-SD range to expiry from live 24,900.00 at 09:55 IST, ", text)
        close_rng = data["range_1sd"]
        self.assertNotIn(fno.fmt(close_rng[0], 0), line)          # the close-centred range is not shown as current
        self.assertLess(rng[1], close_rng[1])                     # live 24,900 is below the 25,000 close
        code, full, _ = run(["chain", "NIFTY", "--no-vix"], SpanFeeds(snapshot="202611180945"))
        self.assertIn("68% implied range at expiry (±1 SD, lognormal; implied by option prices, not a forecast), from the Tue 17 Nov close |", full)
        self.assertIn("| 68% implied range at expiry from live 24,900.00 at 09:55 IST (snapshot IV) |", full)

    def test_after_the_close_the_line_names_its_close(self):
        code, text, _ = run(["chain", "NIFTY", "--summary", "--no-vix"], SpanFeeds(), clock=EVENING)
        line = next(l for l in text.splitlines() if l.startswith("- Expected move to expiry"))
        self.assertTrue(line.endswith("— from the Tue 17 Nov close 25,000.00"), line)
        self.assertIn("by ATM straddle", line)


# ---------------- 4. payoff: 'already in this trade' on the snapshot prices ----------------

class AlreadyInTradeTests(unittest.TestCase):
    def test_mark_uses_the_placed_now_prices(self):
        with tempfile.TemporaryDirectory() as d:
            argv = ["payoff", "NIFTY", "--leg", "SELL:CE:25250:1", "--leg", "BUY:CE:25500:1", "--charges", card_file(d)]
            code, raw, err = run(argv + ["--format", "json"], SpanFeeds(snapshot="202611180945"))
            self.assertEqual(code, 0, err)
            p = json.loads(raw)["payoff"]
            code, text, _ = run(argv + ["--summary", "--held"], SpanFeeds(snapshot="202611180945"))
        units = [l["units"] for l in p["legs"]]
        expected = sum(u * (l["now"] - l["close"]) for u, l in zip(units, p["placed_now"]["legs"]))
        self.assertEqual(p["live_mark"]["source"], "nse_intraday_snapshot")
        self.assertAlmostEqual(p["live_mark"]["pnl"], expected, places=6)
        line = next(l for l in text.splitlines() if l.startswith("- Already in this trade"))
        self.assertEqual(line, f"- Already in this trade from the Tue 17 Nov close? Model P&L now {fno.rupees(expected, 0, True)} before costs "
                               "(at the 'If placed now' prices above, same intraday snapshot).")

    def test_without_a_snapshot_the_close_iv_model_is_labelled(self):
        with tempfile.TemporaryDirectory() as d:
            code, text, _ = run(["payoff", "NIFTY", "--leg", "SELL:CE:25250:1", "--summary", "--held", "--charges", card_file(d)], LiveFeeds())
        self.assertIn("before costs (IVs held at the close, not the 'If placed now' prices).", text)


# ---------------- 5. hedged margin ----------------

CONDOR = [{"type": "CE", "strike": 63200, "units": -30}, {"type": "PE", "strike": 61400, "units": -30},
          {"type": "CE", "strike": 63700, "units": 30}, {"type": "PE", "strike": 60900, "units": 30}]


class HedgedMarginTests(unittest.TestCase):
    def test_condor_states_no_offset_and_the_observed_broker_range(self):
        m = fno_margin.estimate(real_17nov("BANKNIFTY", 62273.25, 5791.41, BANKNIFTY_17NOV), CONDOR, expiry="2026-11-23",
                                trade_day="2026-11-18", index=True)
        self.assertTrue(m["hedged"])
        self.assertAlmostEqual(m["exposure"], 0.02 * 62273.25 * 60, places=2)        # no offset: NSE's rule unchanged
        # Zerodha's total, 18 Nov 2026: ₹81,080 — inside the range (the low end uses the largest gap seen, 19 Nov 2026).
        self.assertLessEqual(m["broker_low"], 14235.50 + 66844.68)
        self.assertGreaterEqual(m["total"], 14235.50 + 66844.68)
        text = fno_margin.hedged_text(m)
        self.assertIn("NSE gives no exposure offset for hedged options (NSE Clearing's equity-derivatives margins page)", text)
        self.assertIn(f"expect roughly ₹{fno_margin.inr(m['broker_low'])}–₹{fno_margin.inr(m['total'])}", text)
        self.assertEqual(fno_margin.margin_text(m), f"indicative ₹{fno_margin.inr(m['total'])} (SPAN ₹14,236 + exposure ₹74,728; "
                                                    "brokers have charged hedged positions less, see the range; your broker's basket margin is final)")

    def test_naked_and_long_only_positions_carry_no_hedge_note(self):
        span = lambda: real_17nov("NIFTY", 25387.40, 2361.03, NIFTY_17NOV)
        naked = fno_margin.estimate(span(), [{"type": "CE", "strike": 25800, "units": -65}], expiry="2026-11-23", trade_day="2026-11-18",
                                    index=True)
        self.assertFalse(naked["hedged"])
        self.assertIsNone(fno_margin.hedged_text(naked))
        long_only = fno_margin.estimate(span(), [{"type": "CE", "strike": 25800, "units": 65}], expiry="2026-11-23", trade_day="2026-11-18",
                                        index=True)
        self.assertFalse(long_only["hedged"])

    def test_payoff_and_grid_summaries_say_it(self):
        with tempfile.TemporaryDirectory() as d:
            code, text, _ = run(["payoff", "NIFTY", "--leg", "SELL:CE:25250:1", "--leg", "BUY:CE:25500:1", "--summary",
                                 "--charges", card_file(d)], SpanFeeds(), clock=EVENING)
            self.assertIn("Hedged position: NSE gives no exposure offset for hedged options", text)
            code, text, _ = run(["grid", "NIFTY", "--structure", "condor", "--width", "250", "--summary", "--charges", card_file(d)],
                                SpanFeeds(), clock=EVENING)
            self.assertIn("Hedged spreads: NSE gives hedged options no exposure offset", text)
            code, text, _ = run(["payoff", "NIFTY", "--leg", "SELL:CE:25250:1", "--summary", "--charges", card_file(d)],
                                SpanFeeds(), clock=EVENING)
            self.assertNotIn("Hedged", text)


# ---------------- 6. ORB history wording ----------------

class OrbWordingTests(unittest.TestCase):
    def render(self, side):
        prior = session(ORB_TODAY - timedelta(days=1), [(1364, 1375, 1363, 1374)])
        rows = prior + day_bars(ORB_TODAY, RANGE + [(1364, 1371, 1363, 1370)])
        with tempfile.TemporaryDirectory() as tmp:
            src = Path(tmp) / "h.json"
            src.write_text(json.dumps(history(rows, ORB_TODAY)), encoding="utf-8")
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                self.assertEqual(public_market.main(["entry", "--input", str(src), "--now", ORB_TODAY.isoformat(), "--side", side]), 0)
        return out.getvalue()

    def test_history_names_the_side_of_the_range(self):
        buy, sell = self.render("BUY"), self.render("SELL")
        self.assertIn("a close above the range high in the entry window on **1**", buy)
        self.assertIn("a close below the range low in the entry window on **0**", sell)
        for text in (buy, sell):
            self.assertNotIn("broke the opening range", text)


# ---------------- C. near-the-money OI walls and max pain distance ----------------

class NearWallTests(unittest.TestCase):
    def report(self):
        # 10% IV: the 1-SD range to expiry (≈24,690–25,330) excludes the biggest walls at 25,500 and 24,500.
        return fno.analyse_chain(fno.parse_bhavcopy(bhavcopy(vol=0.10), "NIFTY"), "NIFTY", rate=RATE)

    def test_near_walls_sit_inside_the_one_sd_range(self):
        r = self.report()
        near = r["oi_walls_near"]
        lo, hi = r["range_1sd"]
        self.assertEqual(near["window"], [lo, hi])
        self.assertEqual((r["resistance_oi"][0], r["support_oi"][0]), (25500, 24500))   # biggest overall, outside the range
        self.assertTrue(lo > 24500 and hi < 25500)
        self.assertEqual(near["calls"], [25250, 25000])      # call OI 80 and 60 inside the range
        self.assertEqual(near["puts"], [24750, 25000])       # put OI 90 and 70
        self.assertEqual(near["basis"], "1-SD range to expiry")
        s = fno.chain_summary(r)
        self.assertEqual(fno.near_walls_text(s["oi_walls_near"]),
                         f"OI walls near spot (largest OI within the 1-SD range to expiry, {fno.fmt(lo, 0)}–{fno.fmt(hi, 0)}): calls 25,250, "
                         "25,000 · puts 24,750, 25,000 — the nearer levels for the next session")
        lines = fno.chain_summary_markdown(s).splitlines()
        near_at = next(i for i, l in enumerate(lines) if l.startswith("- OI walls near spot"))
        overall = next(l for l in lines if l.startswith("- Call OI walls above spot: 25,500"))
        self.assertTrue(overall.endswith("(biggest overall)"))
        self.assertEqual(lines[near_at - 2], "**Next session**")   # round 5: the near walls sit in the next-session block
        self.assertIn("**OI walls near spot", fno.chain_markdown(r))

    def test_max_pain_distance_from_spot(self):
        self.assertEqual(fno.pain_distance({"max_pain_vs_spot": 60000 / 62273.25 - 1, "data_date": TRADE}),
                         " — 3.7% below spot (the Tue 17 Nov close)")
        self.assertEqual(fno.pain_distance({"max_pain_vs_spot": 0.0, "_live": True}), " — at spot")
        self.assertEqual(fno.pain_distance({"max_pain_vs_spot": None}), "")
        r = self.report()
        self.assertAlmostEqual(r["max_pain_vs_spot"], r["max_pain"] / SPOT - 1)
        line = next(l for l in fno.chain_summary_markdown(fno.chain_summary(r)).splitlines() if l.startswith("- Max pain"))
        self.assertIn("spot (the Tue 17 Nov close)", line)


# ---------------- D. participant expiry caveat on its own line ----------------

class ParticipantCaveatTests(unittest.TestCase):
    def test_other_index_expiry_rides_on_the_participant_line(self):
        code, raw, err = run(["chain", "NIFTY", "--format", "json", "--no-vix"], CalendarFeeds())
        self.assertEqual(code, 0, err)
        report = json.loads(raw)
        # As a BANKNIFTY chain sees it: the participant files' window holds NIFTY's expiry, the symbol's own window none.
        r = fno.analyse_chain(fno.parse_bhavcopy(bhavcopy(), "NIFTY"), "NIFTY", rate=RATE)
        r["participants"] = report["participants"]
        r["day_over_day"] = dict(report["day_over_day"], expiry_caveat=None)
        self.assertIn("NIFTY options expired", r["participants"]["expiry_caveat"])
        s = fno.chain_summary(r)
        self.assertEqual(s["warnings"], [])
        with mock.patch.object(fno, "FII_LONG_EXTREME", 0.5):  # summary shows the participant line only when notable
            md = fno.chain_summary_markdown(s)
        self.assertNotIn("- ⚠", md)
        who = next(l for l in md.splitlines() if l.startswith("- Who holds index F&O"))
        self.assertTrue(who.endswith(f"(⚠ {r['participants']['expiry_caveat']})"))

    def test_own_expiry_still_warns_at_chain_level(self):
        with mock.patch.object(fno, "FII_LONG_EXTREME", 0.5):  # summary shows the participant line only when notable
            code, text, _ = run(["chain", "NIFTY", "--summary", "--no-vix"], CalendarFeeds())
        warn = next(l for l in text.splitlines() if l.startswith("- ⚠"))
        self.assertIn("NIFTY options expired on Tue 17 Nov", warn)
        who = next(l for l in text.splitlines() if l.startswith("- Who holds index F&O"))
        self.assertTrue(who.endswith("(⚠ the same expiry caveat as above applies)"))       # not repeated in full


if __name__ == "__main__":
    unittest.main()
