"""Offline contracts for the round-5 portfolio/F&O fixes (18 Nov 2026 review, market open). Never contacts the network.

1. holdings --summary: a ⚠ typed buy price gets one what-if line under the value — P&L, return per year, the holding and the
   sell-everything tax at the middle of the period's range — and ⚠ beside it in best/worst; the summary stays within 12 lines.
2. grid: arguments are checked before any download; --width defaults per index (stocks: two strike steps) and the header says
   so; during market hours each short leg shows its delta now and a short strike near the live level is flagged.
3. ORB entry: entry-window line, exposure (checked against --capital), the bold size for a close one tick past the trigger.
4. chain/payoff --summary: plain wording (no Black-76, file names or futures-implied level), a "Next session" block sized with
   the now ATM IV, the straddle as a multiple of 1 SD, max pain from the live level; "Already in this trade" only with --held.
"""
import contextlib
from datetime import date, datetime
from decimal import Decimal
import io
import json
import math
from pathlib import Path
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from core import IST
import holdings_review as hr
import fno
import position_size as ps
import public_market as pm
from test_buy_price_check import run as bp_run
from test_portfolio_valuation import QUESTION, REPORT, run as pv_run
from test_fno_round2 import LiveFeeds, card_file, run, LIVE_LEVEL
from test_fno_margin import SpanFeeds
from test_orb_quick import RANGE, FORMING, TODAY as ORB_TODAY, day_bars, history


# ---------------- 1. holdings what-if at the range midpoint ----------------

class HoldingsWhatIfTests(unittest.TestCase):
    def test_single_sbin_flips_the_headline_and_is_marked(self):
        r = bp_run([{"symbol": "SBIN", "quantity": 40, "average_price": 600, "bought": "2026-10"}])
        m = r["totals"]["at_range_midpoint"]
        value = r["totals"]["current_value"]
        mid = (Decimal("937.5") + Decimal("983.8")) / 2                   # Oct 2026 low-high
        self.assertEqual(m["invested"], float(40 * mid))
        self.assertAlmostEqual(m["unrealised_pnl"], value - float(40 * mid), places=2)
        self.assertEqual([p["symbol"] for p in m["prices"]], ["SBIN"])
        brief = hr.summary(r)
        lines = brief.splitlines()
        value_at = next(i for i, l in enumerate(lines) if l.startswith("- **Value**"))
        self.assertIn("**Only priced holding** SBIN ⚠", lines[value_at])
        what_if = lines[value_at + 1]
        self.assertTrue(what_if.startswith("  - ⚠ SBIN typed price is outside what traded then; at SBIN ~₹961 in Oct 2026: P&L "), what_if)
        self.assertIn(f"P&L {hr.inr(m['unrealised_pnl'])} ({hr.pct(m['unrealised_pnl_pct'])})", what_if)
        self.assertIn("(under a year)", what_if)                        # 48 days: the simple return, not annualised
        tax = r["tax"]["hypothetical_sell_all"]["at_range_midpoint"]
        sold = next(i for i, l in enumerate(lines) if l.startswith("- **If you sold"))   # round 6: the tax what-if sits under it
        self.assertEqual(lines[sold + 1], f"  - ⚠ At SBIN ~₹961 in Oct 2026: tax {hr.tax_range(tax['estimated_total_tax_low'], tax['estimated_total_tax'])}")

    def test_reviewer_portfolio_return_and_line_budget(self):
        m = REPORT["totals"]["at_range_midpoint"]
        self.assertEqual({p["symbol"] for p in m["prices"]}, {"ITC", "SBIN"})    # the prices that drive the tax figure
        by = {h["symbol"]: h for h in REPORT["holdings"]}
        shift = 200 * (420 - 380) + 40 * (960.65 - 600)
        self.assertAlmostEqual(m["invested"], REPORT["totals"]["invested_priced"] + shift, places=2)
        holdings = {h["symbol"]: h for h in m["holdings"]}
        self.assertAlmostEqual(holdings["SBIN"]["pnl"], by["SBIN"]["pnl"] - 40 * (960.65 - 600), places=2)
        # Return per year at the midpoint: XIRR on the same dates with the swapped buy amounts.
        flows = [(date(2023, 1, 1), -72000), (date(2023, 6, 1), -200 * 420), (date(2026, 10, 1), -40 * 960.65),
                 (date(2026, 11, 18), by["RELIANCE"]["current_value"] + by["ITC"]["current_value"] + by["SBIN"]["current_value"])]
        self.assertAlmostEqual(m["returns"]["annualised"], hr.xirr(flows), places=4)
        self.assertLess(m["returns"]["annualised"], REPORT["returns"]["portfolio"]["annualised"])
        brief = hr.summary(REPORT)
        self.assertIn(f"return per year {hr.pct(m['returns']['annualised'])}", brief)
        self.assertNotIn("- **Best**", brief)                              # best/worst share the value line
        self.assertIn("**Best** RELIANCE", brief)
        # Reviewer layout (no cost-split sub-bullet): title, value, what-if, day, return, largest, sector, sold, loss,
        # demerged, flags, footer = 12; + the 'Bonus applied' line (round 7) when a bonus or split was applied = 13.
        live = pv_run(QUESTION, feed=None)
        self.assertLessEqual(len([l for l in hr.summary(live).splitlines() if l.strip()]), 13)
        md = hr.markdown(REPORT)
        self.assertIn("| ⚠ If the typed prices outside what traded then were mid-range (ITC ~₹420, SBIN ~₹961) | P&L ", md)

    def test_twelve_lines_with_one_doubtful_price_and_a_loss_line(self):
        # The reviewer's 18 Nov case: SBIN's price drives the tax, ITC's does not; a long-term loss line remains.
        r = pv_run([{"symbol": "ITC", "quantity": 200, "average_price": 380, "bought": "2023-06"},
                    {"symbol": "RELIANCE", "quantity": 30, "average_price": 2400, "bought": "2023-01"},
                    {"symbol": "INFY", "quantity": 15, "average_price": 1500},
                    {"symbol": "SBIN", "quantity": 40, "average_price": 600, "bought": "2026-10"}])
        lines = [l for l in hr.summary(r).splitlines() if l.strip()]
        self.assertLessEqual(len(lines), 13)                                      # 12 + the round-7 'Bonus applied' line

    def test_doubtful_price_that_does_not_move_the_tax_still_moves_the_pnl(self):
        r = pv_run([{"symbol": "ITC", "quantity": 10, "average_price": 380, "bought": "2023-06"}])
        self.assertNotIn("at_range_midpoint", r["tax"]["hypothetical_sell_all"])
        m = r["totals"]["at_range_midpoint"]
        self.assertAlmostEqual(m["unrealised_pnl"], r["totals"]["unrealised_pnl"] - 10 * (420 - 380), places=2)
        self.assertTrue(hr.summary(r).splitlines()[3].endswith(" · tax figure unchanged"))

    def test_the_what_if_uses_the_prices_that_drive_the_tax(self):
        # As on 18 Nov: SBIN's typed price drives the tax, ITC's (also ⚠) does not. Round 6: the P&L what-if swaps every ⚠
        # price (ITC and SBIN, combined effect); the tax what-if, under the sell-everything line, swaps SBIN only and says so.
        r = pv_run([{"symbol": "ITC", "quantity": 10, "average_price": 380, "bought": "2023-06"},
                    {"symbol": "SBIN", "quantity": 40, "average_price": 600, "bought": "2026-10"}])
        self.assertEqual([p["symbol"] for p in r["tax"]["hypothetical_sell_all"]["at_range_midpoint"]["prices"]], ["SBIN"])
        m = r["totals"]["at_range_midpoint"]
        self.assertEqual([p["symbol"] for p in m["prices"]], ["ITC", "SBIN"])
        self.assertAlmostEqual(m["unrealised_pnl"], r["totals"]["unrealised_pnl"] - 40 * (960.65 - 600) - 10 * (420 - 380), places=2)
        lines = hr.summary(r).splitlines()
        what_if = next(l for l in lines if "outside what traded then" in l)
        self.assertTrue(what_if.startswith("  - ⚠ ITC and SBIN typed prices are outside what traded then; at ITC ~₹420"), what_if)
        tax = next(l for l in lines if l.startswith("  - ⚠ At "))
        self.assertTrue(tax.startswith("  - ⚠ At SBIN ~₹961 in Oct 2026 (the only ⚠ price that changes the tax): tax "), tax)

    def test_no_what_if_without_a_doubtful_price_or_when_lots_do_not_explain_the_holding(self):
        clean = bp_run([{"symbol": "SBIN", "quantity": 40, "average_price": 950, "bought": "2026-10"}])
        self.assertNotIn("at_range_midpoint", clean["totals"])
        self.assertNotIn("If SBIN's real", hr.summary(clean))
        # 40 held, but the dated lot covers only 10: the typed lot may not be what the average cost is built from.
        partial = bp_run([{"symbol": "SBIN", "quantity": 40, "average_price": 600}],
                         lots=[{"symbol": "SBIN", "quantity": 10, "price": 600, "date": "2026-10-05"}])
        self.assertNotIn("at_range_midpoint", partial["totals"])


# ---------------- 2. grid: validation first, default width, delta now ----------------

class GridRound5Tests(unittest.TestCase):
    def test_bad_arguments_fail_before_any_download(self):
        for argv in (["grid", "NIFTY", "--structure", "condor", "--deltas", "0.7"],
                     ["grid", "NIFTY", "--structure", "condor", "--deltas", "x"],
                     ["grid", "NIFTY", "--structure", "condor", "--width", "-5"],
                     ["grid", "NIFTY", "--structure", "condor", "--row", "0"],
                     ["grid", "NIFTY", "--structure", "condor", "--chart", "x.svg"],
                     ["chain", "NIFTY", "--date", "18-11-2026"],
                     ["payoff", "NIFTY", "--leg", "SELL:CE:25800:1", "--expiry", "23 Nov"],
                     ["payoff", "NIFTY", "--leg", "SELL:XX:25800:1"]):
            with self.subTest(argv=argv):
                feeds = LiveFeeds()
                code, _, err = run(argv, feeds)
                self.assertEqual(code, 2)
                self.assertTrue(err.startswith("BLOCKED - "), err)
                self.assertEqual(feeds.urls, [])
        with tempfile.TemporaryDirectory() as d:
            out = Path(d) / "exists.md"
            out.write_text("x", encoding="utf-8")
            feeds = LiveFeeds()
            code, _, err = run(["grid", "NIFTY", "--structure", "condor", "--out", str(out)], feeds)
            self.assertEqual((code, feeds.urls), (2, []))
            self.assertIn("output exists", err)

    def test_default_width_per_index_and_stocks(self):
        with tempfile.TemporaryDirectory() as d:
            argv = ["grid", "NIFTY", "--structure", "condor", "--charges", card_file(d), "--no-margin"]
            code, text, err = run(argv + ["--summary"], LiveFeeds())
            self.assertEqual(code, 0, err)
            self.assertIn("iron condor grid — expiry 2026-11-23, wings 100 points (default for NIFTY; change with --width), 1 lot = 65**", text)
            code, raw, _ = run(argv + ["--format", "json"], LiveFeeds())
            g = json.loads(raw)["grid"]
            self.assertEqual((g["width"], g["width_default"]), (100.0, "default for NIFTY"))
            code, text, _ = run(argv + ["--width", "250", "--summary"], LiveFeeds())
            self.assertIn("wings 250 points, 1 lot", text)
            self.assertNotIn("default for", text)
        self.assertEqual({k: fno.DEFAULT_WIDTHS[k] for k in ("NIFTY", "BANKNIFTY", "FINNIFTY", "MIDCPNIFTY")},
                         {"NIFTY": 100, "BANKNIFTY": 500, "FINNIFTY": 100, "MIDCPNIFTY": 50})
        from test_fno_round2 import plain_report
        r = plain_report()
        self.assertEqual(fno.strike_step(r), 250)
        self.assertEqual(fno.default_width("RELIANCE", r), (500.0, "default: 2 strike steps of 250"))
        self.assertEqual(fno.default_width("BANKNIFTY", r), (500.0, "default for BANKNIFTY"))
        with self.assertRaisesRegex(fno.Block, "--width"):
            fno.grid(r, "condor", None)                                     # the library call still needs a width

    def test_short_leg_delta_now_and_near_the_money_flag(self):
        with tempfile.TemporaryDirectory() as d:
            argv = ["grid", "NIFTY", "--structure", "condor", "--width", "250", "--charges", card_file(d), "--no-margin"]
            code, text, err = run(argv + ["--summary"], SpanFeeds(snapshot="202611180945"))
            self.assertEqual(code, 0, err)
            code, raw, _ = run(argv + ["--format", "json"], SpanFeeds(snapshot="202611180945"))
        rows = json.loads(raw)["grid"]["rows"]
        table = [l for l in text.splitlines() if l.startswith("| 1 |")]
        s = rows[0]["shorts"]
        self.assertIn(f"{s[0]['strike']:,.0f}/", table[0])
        self.assertIn(f"(Δ {s[0]['delta']:.2f}", table[0])
        self.assertIn("Δ is each short leg's delta now, at live NIFTY 24,900.00; ⚠ = short strike within 0.5% of that level.", text)
        # A short put 0.1% from the live level is flagged; one 0.6% away is not.
        g = {"rows": [{"shorts": [{"type": "PE", "strike": 61800, "delta": 0.47, "delta_close": 0.35},
                                  {"type": "CE", "strike": 62800, "delta": 0.20, "delta_close": 0.33}]},
                      {"shorts": [{"type": "PE", "strike": 61450, "delta": 0.38, "delta_close": 0.30}]}]}
        lines = fno.near_money_lines(g, 61839.45, "BANKNIFTY")
        self.assertEqual(lines, ["⚠ Row 1: short put 61,800 is now near the money — 0.06% from live BANKNIFTY 61,839.45 "
                                 "(Δ 0.47 now, 0.35 at the close)."])
        self.assertEqual(fno.short_now_cell(g["rows"][0]["shorts"][0], 61839.45), "Δ 0.47 ⚠")
        self.assertEqual(fno.short_now_cell(g["rows"][1]["shorts"][0], 61839.45), "Δ 0.38")


# ---------------- 3. ORB entry ----------------

def entry_cli(now, rows, *extra):
    with tempfile.TemporaryDirectory() as tmp:
        src = Path(tmp) / "h.json"
        src.write_text(json.dumps(history(day_bars(ORB_TODAY, rows), now)), encoding="utf-8")
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = pm.main(["entry", "--input", str(src), "--now", now.isoformat(), *extra])
        return code, out.getvalue()


class OrbRound5Tests(unittest.TestCase):
    def test_entry_window_minutes_left_and_closed(self):
        code, text = entry_cli(ORB_TODAY, RANGE + FORMING)
        self.assertEqual(code, 0)
        self.assertIn("(next check after 09:35 IST) · entry window closes 14:30 IST (297 min left).", text)
        late = ORB_TODAY.replace(hour=14, minute=40)
        code, text = entry_cli(late, RANGE + [(1364, 1365, 1363, 1364)] * 62)
        self.assertIn("nothing to act on today · entry window (09:30–14:30 IST) closed for today.", text)
        r = pm.public_entry(history(day_bars(ORB_TODAY, RANGE + FORMING), ORB_TODAY), ORB_TODAY)
        self.assertEqual(r["entry_window"], {"from": "09:30", "to": "14:30", "closed": False, "minutes_left": 297})

    def test_bold_size_one_tick_past_the_trigger_with_the_trigger_as_reference(self):
        _, text = entry_cli(ORB_TODAY, RANGE + FORMING, "--risk", "2000")
        tick = ps.size(entry=1372.4, stop=1366.15, risk=2000)
        at = ps.size(entry=1372.3, stop=1366.15, risk=2000)
        self.assertLess(tick["quantity"], at["quantity"])                  # a higher fill buys fewer shares
        self.assertIn("| Stop | **Close one tick above, ₹1,372.40** | At the trigger ₹1,372.30 (reference) |", text)
        self.assertIn(f"| range midpoint ₹1,366.15 | **{tick['quantity']}** (₹6.25 a share, loss {ps.inr(tick['loss_at_stop'])}) | "
                      f"{at['quantity']} |", text)
        self.assertIn("tick ₹0.10 at this price, from NSE price-band tick sizes", text)
        _, js = entry_cli(ORB_TODAY, RANGE + FORMING, "--risk", "2000", "--format", "json")
        data = json.loads(js)
        self.assertEqual(data["sizing_tick"]["entry_assumed"], 1372.4)
        self.assertEqual(data["sizing"]["entry_assumed"], 1372.3)          # unchanged for position_size --from-entry
        _, text = entry_cli(ORB_TODAY, RANGE + FORMING, "--risk", "2000", "--tick", "0.05")
        self.assertIn("**Close one tick above, ₹1,372.35**", text)
        self.assertEqual([pm.nse_tick(p) for p in (100, 500, 1372.3, 6000, 15000, 30000)], [0.01, 0.05, 0.10, 0.50, 1.00, 5.00])

    def test_triggered_says_when_a_stop_has_since_been_reached(self):
        now = ORB_TODAY.replace(minute=41)
        rows = RANGE + [(1364, 1375, 1363, 1374), (1374, 1374.5, 1365, 1366)]   # triggers at 09:35, then dips to 1,365
        code, text = entry_cli(now, rows, "--risk", "2000")
        self.assertEqual(code, 0)
        self.assertIn("— inside the range (₹1,360.00–₹1,372.30); since the 09:35 trigger the price has reached the range-midpoint stop "
                      "₹1,366.15 (completed bars).", text)
        self.assertNotIn("range-low stop", text)                           # the trigger bar's own low (1,363) is before entry
        self.assertNotIn("entry window", text.splitlines()[2])              # no window line once triggered
        size = text.split("## Size")[1]
        self.assertLess(size.index("- Exposure up to"), size.index("⚠ Sized at the confirming close ₹1,374.00"))   # with the per-stop bullets
        self.assertIn("| Stop | Confirming close ₹1,374.00 |", size)
        self.assertNotIn("Entry assumed at the trigger level", size)
        code, text = entry_cli(ORB_TODAY.replace(minute=36), rows[:-1])
        self.assertNotIn("since the", text)                                 # nothing completed after the trigger yet

    def test_exposure_line_and_capital_check(self):
        tick = ps.size(entry=1372.4, stop=1366.15, risk=2000)
        _, text = entry_cli(ORB_TODAY, RANGE + FORMING, "--risk", "2000")
        self.assertIn(f"- Exposure up to {pm.lakh(tick['exposure'])} ({tick['quantity']} shares, range midpoint stop; est. intraday margin "
                      f"{ps.inr(tick['capital_needed'])}) — pass --capital to check it fits your account.", text)
        _, text = entry_cli(ORB_TODAY, RANGE + FORMING, "--risk", "2000", "--capital", "100000")
        self.assertIn(f"— ⚠ {tick['exposure'] / 100000:.1f}× your capital ₹1,00,000: it fits only with MIS intraday leverage", text)
        _, text = entry_cli(ORB_TODAY, RANGE + FORMING, "--risk", "2000", "--capital", "50000")
        self.assertIn("— ⚠ the est. margin is more than your capital ₹50,000: take fewer shares.", text)
        _, text = entry_cli(ORB_TODAY, RANGE + FORMING, "--risk", "2000", "--capital", "500000")
        self.assertIn("— within your capital ₹5,00,000.", text)
        code, text = entry_cli(ORB_TODAY, RANGE + FORMING, "--capital", "100000")   # --capital needs --risk
        self.assertEqual(code, 2)
        self.assertEqual((pm.lakh(436391), pm.lakh(12500000), pm.lakh(87278)), ("₹4.36 lakh", "₹1.25 crore", "₹87,278"))


# ---------------- 4. chain and payoff summaries ----------------

class ChainSummaryRound5Tests(unittest.TestCase):
    def chain(self, *extra):
        code, text, err = run(["chain", "NIFTY", "--summary", "--no-vix", *extra], SpanFeeds(snapshot="202611180945"))
        self.assertEqual(code, 0, err)
        return text

    def test_plain_words_and_the_next_session_block(self):
        text = self.chain()
        for jargon in ("Black-76", ".spn", "futures-implied", "NSE Clearing's 09:45"):
            self.assertNotIn(jargon, text)
        lines = text.splitlines()
        head = lines.index("**Next session — Thu 19 Nov (tomorrow)**")
        self.assertTrue(lines[head + 1].startswith("- 1-sigma to the Thu 19 Nov close (tomorrow), from live 24,900.00 at 09:55 IST"))
        self.assertIn(" — at the now ATM IV ", lines[head + 1])
        self.assertTrue(lines[head + 2].startswith("- OI walls near spot"))
        full = run(["chain", "NIFTY", "--no-vix"], SpanFeeds(snapshot="202611180945"))[1]
        for kept in ("Black-76", "nsccl.20261118.i02.spn", "Futures-implied level at expiry"):
            self.assertIn(kept, full)

    def test_next_session_range_uses_the_now_iv(self):
        raw = run(["chain", "NIFTY", "--summary", "--no-vix", "--format", "json"], SpanFeeds(snapshot="202611180945"))[1]
        data = json.loads(raw)
        n = data["now_estimate"]
        live = [m for m in data["session_ranges"] if m["centre_label"].startswith("live ")]
        self.assertEqual(len(live), 1)
        m = live[0]
        self.assertEqual(m["iv_source"], "now")
        self.assertAlmostEqual(m["iv"], n["atm_iv"], places=12)
        self.assertNotAlmostEqual(m["iv_close"], n["atm_iv"], places=3)    # the snapshot IV differs from the close's
        self.assertAlmostEqual(m["points"], LIVE_LEVEL * n["atm_iv"] * math.sqrt(m["sessions"] / fno.TRADING_DAYS), places=6)
        self.assertIn(f"at the now ATM IV {fno.pct(n['atm_iv'])}", self.chain())
        # Without a 'now' estimate the ranges stay on the close's IV and say so.
        self.assertIs(fno.ranges_at_now_iv(data["session_ranges"], {"available": False}), data["session_ranges"])

    def test_straddle_as_sd_and_max_pain_from_live(self):
        text = self.chain()
        raw = json.loads(run(["chain", "NIFTY", "--summary", "--no-vix", "--format", "json"], SpanFeeds(snapshot="202611180945"))[1])
        n = raw["now_estimate"]
        ratio = n["straddle"] / ((n["range_1sd"][1] - n["range_1sd"][0]) / 2)
        self.assertIn(f"by the now ATM straddle (≈ {ratio:.1f} SD)", text)
        self.assertTrue(0.7 < ratio < 0.9)                                   # ≈ √(2/π) for fairly priced options
        line = next(l for l in text.splitlines() if l.startswith("- Max pain"))
        e = raw["max_pain"] / n["level"] - 1
        self.assertTrue(line.endswith(f"{abs(e) * 100:.1f}% {'below' if e < 0 else 'above'} live 24,900 (09:55 IST)"), line)
        self.assertIn("spot (the Tue 17 Nov close)", line)
        self.assertIsNone(fno.straddle_sd(None, [1, 2]))
        self.assertEqual(fno.pain_distance({"max_pain_vs_spot": 0.0, "_live": True}), " — at spot")


class PayoffSummaryRound5Tests(unittest.TestCase):
    def test_held_line_only_with_held_and_no_file_names(self):
        with tempfile.TemporaryDirectory() as d:
            argv = ["payoff", "NIFTY", "--leg", "SELL:CE:25250:1", "--summary", "--charges", card_file(d)]
            _, plain, _ = run(argv, SpanFeeds(snapshot="202611180945"))
            _, held, _ = run(argv + ["--held"], SpanFeeds(snapshot="202611180945"))
            _, full, _ = run(argv[:-3] + ["--charges", card_file(d)], SpanFeeds(snapshot="202611180945"))
        self.assertNotIn("Already in this trade", plain)
        self.assertIn("- Already in this trade from the Tue 17 Nov close? Model P&L now ", held)
        for jargon in ("Black-76", ".spn"):
            self.assertNotIn(jargon, plain)
        self.assertIn("- How 'now' is estimated: option prices from NSE's 09:45 IST intraday snapshot, each moved to live NIFTY 24,900.00 "
                      "(09:55 IST) at its own implied volatility.", plain)
        self.assertIn("NSE Clearing's start-of-day margin parameters from the Tue 17 Nov close", plain)
        self.assertIn("T+0 at the live level 24,900.00 (09:55 IST)", full)   # the full report keeps the mark
        self.assertIn(".spn", full)


if __name__ == "__main__":
    unittest.main()
