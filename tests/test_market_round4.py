"""Round-4 market fixes: point-contribution weights anchored to the NSE Indices factsheet (top-10 and sector weights,
rolled forward by price), an 'Estimated Sensex now' line from live Nifty 50, and plain wording (direction words without
a second sign, one like-for-like wording, the title time matching the body). Offline fakes only."""
from datetime import date, datetime, timedelta
import io
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT / "tests"))
from core import IST, instant
import daily
import market_movers as mm
import nse_live
from test_daily_reports import INTRADAY, cp1252_stdout
from test_daily_round2 import build
from test_market_movers import FLOWS, Market, NOW as MOVERS_NOW
from test_market_round3 import SensexMarket

REF_T = 74          # last bar of 13 Nov: the synthetic "factsheet date"
ALPHA = 10          # S00-S09 form sector "Alpha", the rest "Beta"


def true_weights(m, t):
    return {s: sh * m.prices[s][t] / m.index[t] for sh, s in zip(m.shares, m.symbols)}


def synthetic_anchor(m, alpha_shift=0.0, top=("S00", "S01", "S02"), members=None):
    """A factsheet for the synthetic market at REF_T: exact top weights and sector totals (Alpha moved by alpha_shift pct)."""
    w = true_weights(m, REF_T)
    alpha = sum(w[s] for s in m.symbols[:ALPHA]) * 100 + alpha_shift
    return {"as_of": "2026-11-13", "source": "synthetic factsheet", "top": {s: w[s] * 100 for s in top},
            "sectors": {"alpha": alpha, "beta": 100 - alpha}, "closes": {s: m.prices[s][REF_T] for s in m.symbols},
            "index_close": m.index[REF_T], "members": set(members or m.symbols)}


def industries(m):
    return {s: "Alpha" if i < ALPHA else "Beta" for i, s in enumerate(m.symbols)}


def inputs(m):
    charts = {s + ".NS": m.chart(s + ".NS")["chart"]["result"][0] for s in m.symbols} | {"^NSEI": m.chart("^NSEI")["chart"]["result"][0]}
    prev = {s + ".NS": m.prices[s][m.prev_t] for s in m.symbols}
    return charts, prev, m.index[m.prev_t]


# ---------------------------------------------------------------- fix 1: weights anchored to official data

class ShippedWeightsFileTests(unittest.TestCase):
    def test_file_carries_the_30_oct_factsheet(self):
        raw = json.loads((ROOT / "assets" / "index_weights.json").read_text(encoding="utf-8"))["NIFTY 50"]
        self.assertEqual(raw["as_of"], "2026-10-30"); self.assertIn("niftyindices.com/Factsheet/ind_nifty50.pdf", raw["source"])
        top = raw["top_constituents_pct"]
        self.assertEqual((top["INFY"], top["BAJFINANCE"], top["HDFCBANK"]), (3.92, 2.40, 9.95)); self.assertEqual(len(top), 10)
        self.assertEqual(raw["sectors_pct"]["Consumer Durables"], 2.70)
        self.assertAlmostEqual(sum(raw["sectors_pct"].values()), 100.0, places=2)
        self.assertEqual(len(raw["members"]), 50); self.assertEqual(set(raw["reference_closes"]), set(raw["members"]))

    def test_load_anchor_reads_it_and_drops_it_when_stale_early_or_broken(self):
        a = mm.load_anchor(today=date(2026, 11, 18))
        self.assertEqual(a["as_of"], "2026-10-30"); self.assertEqual(a["top"]["INFY"], 3.92)
        self.assertIn(mm._sector_key("Oil Gas & Consumable Fuels"), a["sectors"])  # factsheet "Oil, Gas & ..." matches NSE's list
        self.assertIsNone(mm.load_anchor(today=date(2026, 10, 30) + timedelta(days=mm.PRIOR_MAX_AGE_DAYS + 1)))
        self.assertIsNone(mm.load_anchor(today=date(2026, 10, 29)))  # a session before the factsheet date
        with tempfile.TemporaryDirectory() as d:
            bad = Path(d) / "w.json"
            raw = json.loads(mm.WEIGHTS_FILE.read_text(encoding="utf-8")); raw["NIFTY 50"]["sectors_pct"]["Power"] = 30.0
            bad.write_text(json.dumps(raw), encoding="utf-8")
            self.assertIsNone(mm.load_anchor(path=bad))  # sectors no longer add up to 100
            bad.write_text("{", encoding="utf-8"); self.assertIsNone(mm.load_anchor(path=bad))


class AnchoredWeightTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.m = Market()
        cls.charts, cls.prev, cls.ip = inputs(cls.m)
        cls.truth = true_weights(cls.m, cls.m.prev_t)

    def fit(self, anchor, ind=None):
        return mm.anchored_weights(self.charts, "^NSEI", self.prev, self.ip, anchor, ind or industries(self.m))

    def test_top_weights_are_the_factsheet_rolled_forward_by_price(self):
        f = self.fit(synthetic_anchor(self.m))
        for s in ("S00", "S01", "S02"):  # exact factsheet weight x price drift = the true weight at the previous close
            self.assertAlmostEqual(f["weights"][s + ".NS"], self.truth[s], delta=0.0005, msg=s)
        self.assertEqual(f["anchor"]["fixed"], ["S00", "S01", "S02"]); self.assertEqual(f["anchor"]["sectors_anchored"], 2)
        self.assertAlmostEqual(sum(f["weights"].values()), 1.0, places=9); self.assertGreater(f["r_squared"], 0.99)

    def test_sector_total_follows_the_factsheet_even_when_the_fit_disagrees(self):
        exact = self.fit(synthetic_anchor(self.m))
        shifted = self.fit(synthetic_anchor(self.m, alpha_shift=-6.0))  # factsheet says Alpha is 6 points smaller
        alpha = lambda f: sum(f["weights"][s + ".NS"] for s in self.m.symbols[:ALPHA])
        self.assertAlmostEqual(alpha(exact), sum(self.truth[s] for s in self.m.symbols[:ALPHA]), delta=0.002)
        self.assertAlmostEqual(alpha(exact) - alpha(shifted), 0.06, delta=0.01)  # the sector budget, not the fit, decides
        for s in ("S00", "S01", "S02"):  # fixed anchors do not move
            self.assertAlmostEqual(shifted["weights"][s + ".NS"], exact["weights"][s + ".NS"], delta=0.003)

    def test_changed_membership_keeps_top_anchors_but_drops_sector_budgets(self):
        f = self.fit(synthetic_anchor(self.m, alpha_shift=-6.0, members=self.m.symbols[:-1] + ["NEWCO"]))
        self.assertFalse(f["anchor"]["membership_matches"]); self.assertEqual(f["anchor"]["sectors_anchored"], 0)
        self.assertIn("no sector totals", mm.method_text(f))

    def test_a_split_or_bonus_drops_that_anchor(self):
        a = synthetic_anchor(self.m); a["closes"]["S01"] *= 2  # unadjusted price halved since the factsheet
        self.assertEqual(self.fit(a)["anchor"]["fixed"], ["S00", "S02"])

    def test_without_matching_symbols_the_pure_fit_runs(self):
        f = mm.index_weights(self.charts, "^NSEI", self.prev, self.ip, industries=industries(self.m), today=date(2026, 11, 18))
        self.assertNotIn("anchor", f)  # the shipped file names real stocks, not S00..S49

    def test_labels_name_the_factsheet_and_its_date(self):
        f = self.fit(synthetic_anchor(self.m))
        text = mm.method_text(f, "2026-11-17")
        self.assertIn("Weights estimated, anchored to official data", text); self.assertIn("13 Nov 2026", text); self.assertIn("17 Nov close", text)
        line = mm.anchor_line(f, "2026-11-17")
        self.assertTrue(line.startswith("Factsheet weights (13 Nov 2026) -> estimated at the 17 Nov close")); self.assertIn("S00 ", line)
        c = {"fit": {k: v for k, v in f.items() if k != "weights"}, "estimated_total_points": -10.0, "actual_points": -10.5}
        self.assertIn("weights are estimated, anchored to the NSE Indices factsheet of 13 Nov 2026", mm.check_line({}, c))


class AnchoredReportTests(unittest.TestCase):
    """market_movers.collect and live_drivers with a factsheet file for the synthetic market."""
    @classmethod
    def setUpClass(cls):
        nse_live.clear_cache()
        cls.m = SensexMarket()
        anchor = synthetic_anchor(cls.m)
        cls.patch = patch.object(mm, "load_anchor", lambda index=mm.INDEX, today=None, path=None: anchor)
        cls.patch.start()
        cls.live = mm.collect(nse_fetch=cls.m.nse, flow_fetch=lambda url: FLOWS, yahoo_fetch=cls.m.yahoo, clock=lambda: MOVERS_NOW, news=False)
        snap = nse_live.parse_all_indices(cls.m.snapshot(), MOVERS_NOW)
        cls.d = mm.live_drivers(["SENSEX"], snap, nse_fetch=cls.m.nse, yahoo_fetch=cls.m.yahoo, sensex_previous=cls.m.sensex[cls.m.prev_t])

    @classmethod
    def tearDownClass(cls):
        cls.patch.stop()

    def test_movers_report_prints_the_anchor_and_estimated_points(self):
        c = self.live["contributors"]
        self.assertEqual(c["fit"]["anchor"]["fixed"], ["S00", "S01", "S02"])
        text = mm.markdown(self.live)
        self.assertIn("- Method: Weights estimated, anchored to official data", text)
        self.assertIn("- Factsheet weights (13 Nov 2026) -> estimated at the 17 Nov close", text)
        self.assertIn("## Index-point contributors (Nifty 50, estimated)", text)
        self.assertIn("anchored to the NSE Indices factsheet of 13 Nov 2026", mm.summary(self.live))
        self.assertAlmostEqual(c["estimated_total_points"], c["actual_points"], delta=abs(c["actual_points"]) * 0.05 + 0.5)

    def test_sensex_weights_are_the_anchored_nifty_weights_rescaled(self):
        sx = self.d["indices"]["SENSEX"]
        self.assertEqual(sx["members"], sorted(SensexMarket.MEMBERS))
        self.assertIn("anchor", sx["fit"]); self.assertIn("rescaled to 100%", sx["method"])
        truth = true_weights(self.m, self.m.prev_t); total = sum(truth[s] for s in SensexMarket.MEMBERS)
        for s in ("S00", "S01", "S02", "S04"):
            self.assertAlmostEqual(sx["weights_pct"][s], truth[s] / total * 100, delta=0.6, msg=s)
        self.assertAlmostEqual(sum(sx["weights_pct"].values()), 100, delta=0.1)
        self.assertTrue(daily.reliable(sx))

    def test_daily_notes_say_which_weights_are_anchored(self):
        result = {"drivers": self.d, "indices": [], "events": [], "coverage_gaps": [], "phase": "intraday", "vix": None}
        notes = "\n".join(daily.data_notes(result))
        self.assertIn("Sensex weights anchored to the NSE Indices Nifty 50 factsheet of 13 Nov 2026", notes)
        self.assertIn("weights are the factsheet-anchored Nifty 50 weights of those 30 stocks, rescaled to 100%", notes)
        self.assertIn("stock point contributions (estimated)", notes)


# ---------------------------------------------------------------- fix 2: estimated Sensex now

def bars(start, closes):
    t = instant(start)
    return [{"at": (t + timedelta(minutes=5 * k)).isoformat(), "close": c} for k, c in enumerate(closes)]


class SensexEstimateTests(unittest.TestCase):
    def test_ratio_is_fitted_on_todays_moves_and_guarded(self):
        nifty = [25000 * (1 + 0.001 * ((-1) ** k) * (k % 4 + 1)) for k in range(12)]
        sensex = [82000.0]
        for a, b in zip(nifty, nifty[1:]): sensex.append(sensex[-1] * (1 + 0.9 * (b / a - 1)))
        ends = {instant(r["at"]) + timedelta(minutes=5): r for r in bars("2026-11-18T09:15:00+05:30", nifty)}
        ratio, moves = daily.sensex_ratio(bars("2026-11-18T09:15:00+05:30", sensex), ends)
        self.assertAlmostEqual(ratio, 0.9, places=6); self.assertEqual(moves, 11)
        self.assertEqual(daily.sensex_ratio(bars("2026-11-18T09:15:00+05:30", sensex[:4]), ends), (1.0, 0))  # too few: 1:1
        wild = [82000.0]
        for a, b in zip(nifty, nifty[1:]): wild.append(wild[-1] * (1 + 3 * (b / a - 1)))
        self.assertEqual(daily.sensex_ratio(bars("2026-11-18T09:15:00+05:30", wild), ends)[0], 1.2)  # clamped

    def test_estimate_moves_the_shared_bar_by_nifty_change_since_then(self):
        est = daily.sensex_estimate([], {}, {"close": 82000.0}, {"close": 25000.0}, 82532.19, 24950.0, "2026-11-18T13:17:00+05:30")
        self.assertAlmostEqual(est["sensex_estimate"], 82000 * (1 - 0.002), places=2)  # 1:1 with no bars to fit
        self.assertAlmostEqual(est["sensex_estimate_pct"], round((81836 / 82532.19 - 1) * 100, 3), places=3)
        nifty = [25000 * (1 + 0.001 * ((-1) ** k) * (k % 4 + 1)) for k in range(12)]
        sensex = [82000.0]
        for a, b in zip(nifty, nifty[1:]): sensex.append(sensex[-1] * (1 + 0.9 * (b / a - 1)))
        ends = {instant(r["at"]) + timedelta(minutes=5): r for r in bars("2026-11-18T09:15:00+05:30", nifty)}
        est = daily.sensex_estimate(bars("2026-11-18T09:15:00+05:30", sensex), ends, {"close": 82000.0}, {"close": 25000.0}, 82532.19, 24950.0,
                                    "2026-11-18T13:17:00+05:30")
        self.assertAlmostEqual(est["sensex_estimate"], 82000 * (1 - 0.9 * 0.002), places=2)  # Nifty -0.2% since the bar, ratio 0.9
        self.assertEqual((est["sensex_ratio"], est["sensex_ratio_moves"]), (0.9, 11))

    def test_line_is_labelled_as_an_estimate_in_whole_points(self):
        m = {"at": "2026-11-18T13:00:00+05:30", "sensex_estimate": 81891.2, "sensex_estimate_pct": -0.776, "sensex_estimate_at": "2026-11-18T13:17:00+05:30",
             "sensex_ratio": 0.96, "sensex_ratio_moves": 45}
        line = daily.sensex_estimate_line({"same_moment": m})
        self.assertTrue(line.startswith("Estimated Sensex now ≈ 81,891 (-0.78%) at 13:17 IST - an estimate from live Nifty 50, not a Sensex quote"), line)
        self.assertIn("(Sensex at the 13:00 bar, moved by Nifty 50's NSE change since then x 0.96, Sensex/Nifty ratio fitted on today's 5-minute bars)", line)
        quote = daily.sensex_estimate_line({"same_moment": {**m, "sensex_estimate_base": "quote", "sensex_estimate_from": "2026-11-18T13:03:10+05:30"}})
        self.assertIn("(the 13:03 Sensex quote, moved by", quote)
        self.assertIsNone(daily.sensex_estimate_line({"same_moment": {"at": m["at"]}}))

    def test_sensex_and_india_daily_reports_print_it(self):
        for selected, command in ((("sensex",), "sensex"), (("nifty", "sensex", "banknifty"), "india-daily")):
            r = daily.report(command, build(selected), INTRADAY)
            m = r["same_moment"]
            self.assertEqual(m["sensex_estimate_at"], "2026-11-18T09:35:00+05:30"); self.assertEqual(m["sensex_ratio_moves"], 0)
            text = daily.markdown(r)
            # Round 5: one Sensex figure (the estimate, then the quote it starts from); the details move to the data notes.
            self.assertIn("Sensex ≈ ", text); self.assertIn("(estimate from live Nifty 50); last Yahoo quote ", text)
            self.assertIn("(1:1; too few of today's 5-minute moves to fit)", text)
            self.assertEqual(m["sensex_estimate_base"], "quote")  # the 09:20 quote sits at a Nifty bar end: start from it
            self.assertIn("at 09:20 IST, vs 17 Nov close", text)
            self.assertIn("Estimated Sensex now: the 09:20 Sensex quote (Yahoo) moved by the live NSE Nifty 50 change", text)  # data note
            self.assertIn("- Sensex ≈ ", daily.markdown(r, brief=True))

    def test_no_estimate_without_a_live_nse_nifty_after_the_bar(self):
        b = build(("sensex",))
        b["comparison_bars"]["now"]["as_of"] = "2026-11-18T09:20:00+05:30"  # NSE's Nifty is no newer than the shared bar
        r = daily.report("sensex", b, INTRADAY)
        self.assertIsNone(r["same_moment"].get("sensex_estimate")); self.assertNotIn("Estimated Sensex", daily.markdown(r))
        self.assertNotIn("Estimated Sensex", daily.markdown(daily.report("nifty", build(("nifty",)), INTRADAY)))

    def test_cp1252_console_gets_a_plain_tilde(self):
        raw, stream = cp1252_stdout()
        with patch.object(stream, "reconfigure", side_effect=ValueError):  # a console that cannot switch to UTF-8
            daily.emit("Estimated Sensex now ≈ 81,891", stream); stream.flush()
        self.assertIn(b"now ~ 81,891", raw.getvalue())

    def test_skills_quote_the_estimate_and_cap_the_web_lookup(self):
        for name in ("market", "sensex"):
            text = (ROOT / "skills" / name / "SKILL.md").read_text(encoding="utf-8")
            self.assertIn("Estimated Sensex now", text); self.assertIn("at most one search", text)
            self.assertIn("`Sensex today <date> live`", text); self.assertIn("Do not try site after site", text)
            self.assertNotIn("look up a current Sensex level on the web (BSE's site", text)
            desc = text.split("---")[1]
            self.assertNotIn(": ", desc.split("description:", 1)[1])


# ---------------------------------------------------------------- fix 3: plain wording

class WordingTests(unittest.TestCase):
    def test_direction_word_without_a_second_sign(self):
        r = {"mode": "live", "at": "2026-11-18T13:00:00+05:30", "session": "2026-11-18",
             "direction": {"word": "down", "change": -199.45, "change_pct": -0.79, "value": 25187.95},
             "previous_direction": {"session": "2026-11-17", "close": 25387.4, "change": 96.3, "change_pct": 0.38, "word": "up"}}
        first = mm.lead(r, premise="up")
        self.assertIn("Nifty 50 is **down** 199.45 points (-0.79%) at 25,187.95", first); self.assertNotIn("down** -", first)
        self.assertIn("closed **up** 96.30 points (+0.38%) at 25,387.40", mm.previous_line(r, premise="up"))
        flat = {**r, "direction": {**r["direction"], "word": "flat", "change": 0.0, "change_pct": 0.0}}
        self.assertIn("**flat** (+0.00%)", mm.lead(flat))

    def test_title_time_is_the_nse_time_the_body_uses(self):
        for selected, command in ((("sensex",), "sensex"), (("nifty", "sensex", "banknifty"), "india-daily")):
            r = daily.report(command, build(selected), INTRADAY + timedelta(minutes=2))  # report runs 2 minutes after NSE's snapshot
            title = daily.markdown(r).splitlines()[0]
            self.assertTrue(title.endswith("market hours, NSE data as of 09:35 IST"), title)
            self.assertNotIn("09:37", title)

    def test_one_like_for_like_wording(self):
        def line(quote_at):
            sx = {"index_id": "sensex", "status": "READY", "as_of": "2026-11-18T12:00:00+05:30", "value_at": quote_at, "change_from_previous_close_pct": -0.51}
            return daily.same_moment_line({"indices": [sx], "same_moment": {"at": "2026-11-18T11:45:00+05:30", "sensex_pct": -0.47, "nifty_pct": -0.49,
                                                                             "sensex_quote_at": quote_at}})
        near, far = line("2026-11-18T11:45:40+05:30"), line("2026-11-18T11:51:00+05:30")
        head = "compare like for like at the same 5-minute bar, ending 11:45 IST (Yahoo, both indices): Sensex "
        self.assertIn(head + "-0.51%", near); self.assertIn(head + "-0.47%", far)
        for text in (near, far):
            self.assertNotIn("a later moment", text); self.assertNotIn("at that time", text)

    def test_plain_condition_words(self):
        bundle = build(("nifty",))
        bars_ = bundle["indices"]["nifty"]["candles"]["rows"]; low = bundle["indices"]["nifty"]["previous"]["low"]
        bars_[3].update(open=low - 50, high=low - 40, low=low - 60, close=low - 45)
        bundle["indices"]["nifty"].pop("quote", None)
        text = daily.markdown(daily.report("nifty", bundle, INTRADAY))
        self.assertIn("(17 Nov low), the previous session's low - a sign of weakness.", text)
        self.assertIn(f"after a completed 5-minute close back above {low:,.2f}, the bearish condition no longer applies", text)
        for old in ("treats as the", "cancels (voids)", "voids"): self.assertNotIn(old, text)
        inside = daily.markdown(daily.report("banknifty", build(("banknifty",)), INTRADAY))
        self.assertNotIn("voids", inside)


if __name__ == "__main__":
    unittest.main()
