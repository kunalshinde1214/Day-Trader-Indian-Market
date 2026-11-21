"""Offline tests for the quick ORB path: compact entry output, range-based sizing with charges, and the ORB history tally."""
import contextlib
from datetime import timedelta
import io
import json
from pathlib import Path
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
from core import instant
import position_size as ps
from public_market import chart, main, orb_tally, public_entry, session_view

TODAY = instant("2026-11-18T09:33:00+05:30")


def day_bars(day, rows):
    """5-minute bars from 09:15 for (open, high, low, close) rows."""
    start = day.replace(hour=9, minute=15, second=0, microsecond=0)
    return [{"at": (start + timedelta(minutes=5 * k)).isoformat(), "open": o, "high": h, "low": l, "close": c, "volume": 1000}
            for k, (o, h, l, c) in enumerate(rows)]


def payload(rows, selected="RELIANCE.NS"):
    return {"chart": {"error": None, "result": [{"meta": {"symbol": selected, "currency": "INR", "exchangeTimezoneName": "Asia/Kolkata",
                                                          "dataGranularity": "5m"},
                                                 "timestamp": [int(instant(b["at"]).timestamp()) for b in rows],
                                                 "indicators": {"quote": [{k: [b[k] for b in rows] for k in ("open", "high", "low", "close", "volume")}]}}]}}


# Opening range 09:15-09:30: high 1372.30, low 1360.00 (mid 1366.15). The 09:30 bar is still forming at 09:33.
RANGE = [(1365, 1372.3, 1362, 1368), (1368, 1370, 1360, 1361), (1361, 1366, 1360.5, 1364)]
FORMING = [(1364, 1371, 1363, 1370)]


def history(rows, now):
    h = chart(payload(rows), "RELIANCE.NS", "5m", now)
    h["as_of"] = now.isoformat()
    return h


def session(day, after_range):
    """A full prior session: the range above, then `after_range` bars, padded flat to 15:25."""
    rows = RANGE + after_range
    last = rows[-1][3]
    rows += [(last, last + 0.1, last - 0.1, last)] * (75 - len(rows))
    return day_bars(day, rows)


class EntryViewTests(unittest.TestCase):
    def test_complete_range_waiting_reason_and_levels(self):
        h = history(day_bars(TODAY, RANGE + FORMING), TODAY)
        r = public_entry(h, TODAY)
        self.assertEqual(r["state"], "waiting")
        self.assertEqual(r["reason"], "Range complete; waiting for a completed 5-minute close above 1,372.30 (next check after 09:35 IST)")
        self.assertEqual(r["opening_range"], {"high": 1372.3, "low": 1360.0, "mid": 1366.15, "complete": True, "bars": 3, "of": 3,
                                              "window": "09:15-09:30 IST"})
        self.assertEqual((r["bars_completed"], r["trigger_level"], r["next_check_at"]), (3, 1372.3, "09:35"))
        self.assertEqual(r["stops"], {"or-mid": 1366.15, "or-low": 1360.0})
        self.assertEqual(r["last_close"], {"price": 1364.0, "bar": "09:25", "completed_at": "09:30"})

    def test_forming_range_triggered_and_closed_window(self):
        early = TODAY.replace(minute=27)
        r = public_entry(history(day_bars(TODAY, RANGE[:2]), early), early)
        self.assertTrue(r["reason"].startswith("Opening range still forming: 2 of 3 5-minute bars complete"))
        self.assertFalse(r["opening_range"]["complete"])
        self.assertIsNone(r["stops"])
        later = TODAY.replace(minute=35)
        r = public_entry(history(day_bars(TODAY, RANGE + [(1364, 1375, 1363, 1374)]), later), later)
        self.assertEqual(r["state"], "triggered")
        self.assertEqual(r["reason"], "Triggered: the 5-minute bar completed at 09:35 closed at 1,374.00, above 1,372.30")
        self.assertIsNone(r["next_check_at"])
        late = TODAY.replace(hour=14, minute=40)
        flat = RANGE + [(1364, 1365, 1363, 1364)] * 62
        r = public_entry(history(day_bars(TODAY, flat), late), late)
        self.assertEqual(r["state"], "waiting")
        self.assertIn("nothing to act on today", r["reason"])
        self.assertIsNone(r["next_check_at"])


class EntryCliTests(unittest.TestCase):
    def run_cli(self, *extra):
        with tempfile.TemporaryDirectory() as tmp:
            src = Path(tmp) / "h.json"
            src.write_text(json.dumps(history(day_bars(TODAY, RANGE + FORMING), TODAY)), encoding="utf-8")
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                code = main(["entry", "--input", str(src), "--now", TODAY.isoformat(), *extra])
            return code, out.getvalue()

    def test_default_output_is_a_compact_summary_without_bars(self):
        code, text = self.run_cli()
        self.assertEqual(code, 0)
        self.assertLess(len(text), 4000)
        self.assertNotIn('"bars"', text)
        for needle in ("**WAITING** — Range complete; waiting for a completed 5-minute close above 1,372.30",
                       "high ₹1,372.30 · low ₹1,360.00 · mid ₹1,366.15", "| Bars completed today | 3 (5-minute) |",
                       "| Next check | after 09:35 IST", "range midpoint ₹1,366.15 · range low ₹1,360.00", "## ORB history"):
            self.assertIn(needle, text)

    def test_json_drops_bars_unless_verbose(self):
        _, text = self.run_cli("--format", "json")
        data = json.loads(text)
        self.assertNotIn("bars", data)
        self.assertEqual(data["opening_range"]["mid"], 1366.15)
        _, text = self.run_cli("--format", "json", "--verbose")
        self.assertEqual(len(json.loads(text)["bars"]), 4)

    def test_risk_sizes_both_range_stops_with_charges(self):
        _, text = self.run_cli("--risk", "2000")
        mid = ps.size(entry=1372.3, stop=1366.15, risk=2000)
        low = ps.size(entry=1372.3, stop=1360.0, risk=2000)
        # Round 5: the bold size is for a close one tick (₹0.10 at this price) above the trigger; the at-trigger size is a reference.
        mid1 = ps.size(entry=1372.4, stop=1366.15, risk=2000)
        low1 = ps.size(entry=1372.4, stop=1360.0, risk=2000)
        self.assertIn(f"| range midpoint ₹1,366.15 | **{mid1['quantity']}** (₹6.25 a share, loss {ps.inr(mid1['loss_at_stop'])}) | {mid['quantity']} |", text)
        self.assertIn(f"| range low ₹1,360.00 | **{low1['quantity']}** (₹12.40 a share", text)
        self.assertIn(f"| {low['quantity']} |", text)
        self.assertIn("a close further above raises the risk per share", text)
        _, js = self.run_cli("--risk", "2000", "--format", "json")
        sizes = json.loads(js)["sizing"]["sizes"]
        self.assertEqual([s["quantity"] for s in sizes], [mid["quantity"], low["quantity"]])
        self.assertTrue(all(x["quantity"] < s["quantity"] for s in sizes for x in s["later_closes"]))  # a higher close means fewer shares

    def test_risk_before_the_range_completes_says_when(self):
        with tempfile.TemporaryDirectory() as tmp:
            early = TODAY.replace(minute=27)
            src = Path(tmp) / "h.json"
            src.write_text(json.dumps(history(day_bars(TODAY, RANGE[:2]), early)), encoding="utf-8")
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                self.assertEqual(main(["entry", "--input", str(src), "--now", early.isoformat(), "--risk", "2000"]), 0)
            self.assertIn("Sizing needs the completed opening range (after 09:30 IST)", out.getvalue())

    def test_out_file_keeps_bars_for_plan_and_position_size_reads_it(self):
        with tempfile.TemporaryDirectory() as tmp:
            src, dest = Path(tmp) / "h.json", Path(tmp) / "e.json"
            src.write_text(json.dumps(history(day_bars(TODAY, RANGE + FORMING), TODAY)), encoding="utf-8")
            with contextlib.redirect_stdout(io.StringIO()) as out:
                self.assertEqual(main(["entry", "--input", str(src), "--now", TODAY.isoformat(), "--out", str(dest)]), 0)
            self.assertIn("Full JSON (with bars, for plan/replay) written to", out.getvalue())
            self.assertEqual(len(json.loads(dest.read_text(encoding="utf-8"))["bars"]), 4)
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                self.assertEqual(ps.main(["--from-entry", str(dest), "--risk", "2000", "--format", "json"]), 0)
            sizes = json.loads(buf.getvalue())["sizes"]
            self.assertEqual([s["stop"] for s in sizes], [1366.15, 1360.0])
            self.assertEqual(sizes[0]["quantity"], ps.size(entry=1372.3, stop=1366.15, risk=2000)["quantity"])

    def test_risk_rejected_for_other_commands(self):
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(main(["plan", "--input", "unused.json", "--risk", "2000"]), 2)


class WorstEntryTests(unittest.TestCase):
    def test_worst_entry_is_the_last_price_that_fits(self):
        card, seg = ps.load_card(ps.DEFAULT_CARD)
        for side, entry, stop in (("BUY", 1372.3, 1360.0), ("SELL", 1360.0, 1372.3)):
            q = ps.size(entry=entry, stop=stop, risk=2000, side=side)["quantity"]
            worst = ps.worst_entry(qty=q, stop=stop, risk=2000, side=side)
            loss = lambda e: q * abs(e - stop) + ps.charges(e, stop, q, side, "MIS", card, seg)["total"]
            step = 0.01 if side == "BUY" else -0.01
            self.assertLessEqual(loss(worst), 2000)
            self.assertGreater(loss(worst + step), 2000)

    def test_bad_range_blocks(self):
        with self.assertRaises(Exception):
            ps.range_sizes(range_high=100, range_low=100, risk=2000)


class OrbTallyTests(unittest.TestCase):
    def test_counts_target_and_stop_first_with_a_missing_late_bar(self):
        d1, d2, d3 = (TODAY - timedelta(days=k) for k in (3, 2, 1))
        win = session(d1, [(1364, 1375, 1363, 1374)] + [(1374, 1400, 1373, 1399)])        # triggers at 1374, then 2R (mid 1389.7, low 1402) hit
        loss = session(d2, [(1364, 1375, 1363, 1374)] + [(1374, 1374.5, 1355, 1356)])     # triggers, then falls through both stops
        none = session(d3, [(1364, 1366, 1363, 1365)])                                   # never closes above 1372.30
        loss = [b for b in loss if not b["at"].endswith("T15:20:00+05:30")]               # Yahoo often omits the 15:20 bar
        rows = win + loss + none + day_bars(TODAY, RANGE)
        t = orb_tally(rows, TODAY)
        self.assertEqual((t["sessions"], t["triggers"], t["skipped_incomplete"]), (3, 2, 0))
        self.assertEqual(t["stops"]["or-mid"], {"target_first": 1, "stop_first": 1, "neither": 0, "neither_r_at_close": []})
        self.assertEqual(t["stops"]["or-low"]["stop_first"], 1)
        self.assertEqual(t["stops"]["or-low"]["target_first"], 0)  # 2R from the range low is 1,402; the bar reached 1,400
        self.assertEqual(t["stops"]["or-low"]["neither"], 1)
        self.assertEqual(t["sessions_with_late_gaps"], 1)
        self.assertIn("not a forecast", t["note"])

    def test_session_missing_a_bar_before_the_window_end_is_skipped(self):
        d1 = TODAY - timedelta(days=1)
        rows = [b for b in session(d1, []) if not b["at"].endswith("T11:00:00+05:30")] + day_bars(TODAY, RANGE)
        t = orb_tally(rows, TODAY)
        self.assertEqual((t["sessions"], t["skipped_incomplete"]), (0, 1))


if __name__ == "__main__":
    unittest.main()
