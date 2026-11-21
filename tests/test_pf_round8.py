"""Offline contracts for the round-8 fixes (21 Nov 2026 held-out review, a Saturday before a Monday expiry). Never contacts the network.

1. Expiry-day margin: the extra 2% exposure on short index options is printed apart from the normal figure, with the entry
   day it assumes; the ±10% band is claimed for the normal part only; hedged index positions carry the broker range
   (Zerodha's fixed per-unit exposure base) instead of the band; at a weekend the next session's begin-of-day SPAN file.
2. One session to expiry: a lead line naming the expiry, the sessions left, the move and the next expiries; the chain
   summary shows one 1-SD figure (around the forward) instead of three ranges.
3. Iron fly grid (--structure fly) and ATM-relative strikes in payoff legs.
4. position_size --stop-low/--stop-high: the N-session low/high, dated, with ⚠ for a one-bar wick and for a level that
   leaves the window next session, each with the alternative stop and size.
5. holdings --summary: why a sell-everything tax is ₹0; a known bonus or split before the purchase is named as not applied.
6. STT from 1 Apr 2026 (Finance Act 2026): exercise 0.15%; earlier data dates use the card's earlier rates.
"""
import contextlib
from datetime import date, datetime, timedelta
import io
import json
from pathlib import Path
import socket
import sys
import tempfile
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from core import Block, IST
import fno
import fno_margin
import holdings_review as hr
import position_size as ps
from test_fno import EXPIRY, LOT, SPOT, TRADE, bhavcopy, report as eod_report
from test_fno_round2 import CARD, card_file, run
from test_fno_margin import SpanFeeds, bhav_span
import test_bonuses

SATURDAY = datetime(2026, 11, 21, 15, 45, tzinfo=IST)        # market closed; the next session (Mon 23 Nov) is the expiry
MONDAY_OPEN = datetime(2026, 11, 23, 11, 0, tzinfo=IST)
FRIDAY_EVENING = datetime(2026, 11, 20, 18, 30, tzinfo=IST)


def no_network():
    """Patch socket.create_connection so any real connection attempt fails loudly."""
    def refuse(*a, **k):
        raise AssertionError("network used in an offline test")
    return mock.patch.object(socket, "create_connection", refuse)


def span_dict(symbol, close, options):
    """A parsed SPAN file as fno_margin.parse_span returns it: options {(expiry, type, strike): (price, ra)}."""
    return {"status": "ok", "symbol": symbol, "underlying_close": close, "futures": {}, "som_rate": 0.0, "file": "test.spn",
            "file_label": "test file", "business_date": "2026-11-20", "price_scan": None,
            "options": {k: {"price": p, "ra": list(ra)} for k, (p, ra) in options.items()}}


ZERO = [0.0] * 16
# The two reviewed positions with their 20 Nov 2026 closes; risk arrays chosen so SPAN equals what Zerodha showed on Sat 21 Nov
# (BANKNIFTY bear put spread SPAN 0; NIFTY iron fly SPAN ₹13,628.55 = the 23 Nov begin-of-day file's scan 4,362.80 + net
# premium 9,265.75).
BN_CLOSE, NF_CLOSE = 64635.20, 25800.60
BPS = span_dict("BANKNIFTY", BN_CLOSE, {("2026-11-23", "PE", 64500.0): (393.05, ZERO), ("2026-11-23", "PE", 64000.0): (195.45, ZERO)})
BPS_LEGS = [{"type": "PE", "strike": 64500, "units": 30}, {"type": "PE", "strike": 64000, "units": -30}]
FLY_RA = [-4362.80 / 65] + [0.0] * 15
FLY = span_dict("NIFTY", NF_CLOSE, {("2026-11-23", "CE", 25800.0): (102.55, FLY_RA), ("2026-11-23", "PE", 25800.0): (130.05, ZERO),
                                    ("2026-11-23", "CE", 26000.0): (37.80, ZERO), ("2026-11-23", "PE", 25600.0): (52.25, ZERO)})
FLY_LEGS = [{"type": "CE", "strike": 25800, "units": -65}, {"type": "PE", "strike": 25800, "units": -65},
            {"type": "CE", "strike": 26000, "units": 65}, {"type": "PE", "strike": 25600, "units": 65}]
ZERODHA_21NOV = {"bps": 33422.0, "fly": 13629.0 + 62393.0}


# ---------------- 1. expiry-day margin ----------------

class ExpiryDayMarginTests(unittest.TestCase):
    def test_extra_is_split_out_with_the_entry_day_and_no_band_on_the_total(self):
        m = fno_margin.estimate(FLY, FLY_LEGS, expiry="2026-11-23", trade_day="2026-11-23", index=True, today="2026-11-21")
        normal = 0.02 * NF_CLOSE * 130
        self.assertAlmostEqual(m["span"], 13628.55, places=2)
        self.assertAlmostEqual(m["expiry_extra"], normal, places=2)
        self.assertAlmostEqual(m["normal_exposure"], normal, places=2)
        self.assertAlmostEqual(m["normal_total"], 13628.55 + normal, places=2)
        self.assertAlmostEqual(m["total"], 13628.55 + 2 * normal, places=2)
        self.assertFalse(m["entry_is_today"])
        text = fno_margin.margin_text(m)
        self.assertIn(f"indicative ≈₹{fno_margin.inr(m['normal_total'])} normal (SPAN ₹13,629 + exposure ₹67,082)", text)
        self.assertIn("+ ₹67,082 expiry-day extra 2% on short index options (SEBI circular SEBI/HO/MRD/TPD-1/P/CIR/2024/132)", text)
        self.assertIn(f"= ₹{fno_margin.inr(m['total'])} if you enter on Mon 23 Nov (expiry day; the next session", text)
        self.assertIn(f"broker calculators show the lower ₹{fno_margin.inr(m['normal_total'])} before that day", text)
        self.assertNotIn("±10%", text)                        # hedged: the range, not the band, and never on the total
        hedged = fno_margin.hedged_text(m)
        self.assertIn(f"₹{fno_margin.inr(m['broker_low_normal'])}–₹{fno_margin.inr(m['normal_total'])} before expiry day", hedged)
        self.assertIn(f"₹{fno_margin.inr(m['broker_low'])}–₹{fno_margin.inr(m['total'])} with the expiry-day extra", hedged)
        # Zerodha's Saturday figure (the normal one) is inside the normal range.
        self.assertLessEqual(m["broker_low_normal"], ZERODHA_21NOV["fly"])
        self.assertGreaterEqual(m["normal_total"], ZERODHA_21NOV["fly"])

    def test_bear_put_spread_matches_zerodhas_fixed_exposure_base(self):
        m = fno_margin.estimate(BPS, BPS_LEGS, expiry="2026-11-23", trade_day="2026-11-23", index=True, today="2026-11-21")
        self.assertEqual(m["span"], 0.0)
        self.assertAlmostEqual(m["normal_total"], 0.02 * BN_CLOSE * 30, places=2)        # ₹38,781
        self.assertAlmostEqual(m["broker_low_normal"], ZERODHA_21NOV["bps"], delta=1.0)  # ₹33,422 = 2% × ₹55,704 × 30
        self.assertLess(m["broker_low_normal"], m["normal_total"] * 0.88)                 # outside a ±10% band of the total
        # Without the per-unit base the old ratio would have left Zerodha's figure outside the range.
        self.assertGreater(m["normal_total"] * 0 + m["span"] + m["normal_exposure"] * fno_margin.BROKER_EXPOSURE_SEEN, ZERODHA_21NOV["bps"])

    def test_hedged_index_position_carries_the_range_not_the_band(self):
        m = fno_margin.estimate(FLY, FLY_LEGS, expiry="2026-11-23", trade_day="2026-11-20", index=True)
        self.assertEqual(fno_margin.margin_text(m), f"indicative ₹{fno_margin.inr(m['total'])} (SPAN ₹13,629 + exposure ₹67,082; "
                                                    f"{fno_margin.HEDGED_LABEL})")
        self.assertIn(f"expect roughly ₹{fno_margin.inr(m['broker_low'])}–₹{fno_margin.inr(m['total'])}", fno_margin.hedged_text(m))

    def test_naked_short_on_expiry_day_keeps_the_band_for_the_normal_part_only(self):
        naked = span_dict("NIFTY", NF_CLOSE, {("2026-11-23", "CE", 25800.0): (102.55, [5000 / 65] + [0.0] * 15)})
        m = fno_margin.estimate(naked, [{"type": "CE", "strike": 25800, "units": -65}], expiry="2026-11-23", trade_day="2026-11-23",
                                index=True, today="2026-11-23")
        self.assertTrue(m["entry_is_today"])
        text = fno_margin.margin_text(m)
        self.assertIn("brokers' figures typically within ±10% of this part", text)
        self.assertIn("to enter today (expiry day); brokers add the extra today too", text)
        self.assertNotIn(fno_margin.LABEL, text)

    def test_before_expiry_day_no_extra_and_the_held_note(self):
        m = fno_margin.estimate(FLY, FLY_LEGS, expiry="2026-11-23", trade_day="2026-11-20", index=True, today="2026-11-20")
        self.assertEqual(m["expiry_extra"], 0.0)
        self.assertAlmostEqual(m["total"], m["normal_total"], places=6)
        self.assertNotIn("expiry-day extra", fno_margin.margin_text(m))
        self.assertEqual(fno_margin.held_extra_text(m), "Held into expiry day (Mon 23 Nov), the short index legs add a further 2% "
                                                         "exposure that day, ≈₹67,082 (SEBI circular SEBI/HO/MRD/TPD-1/P/CIR/2024/132)")
        stock = fno_margin.estimate(dict(FLY, symbol="RELIANCE"), FLY_LEGS, expiry="2026-11-23", trade_day="2026-11-23", index=False)
        self.assertEqual(stock["expiry_extra"], 0.0)                                     # stocks: no expiry-day add-on
        self.assertIsNone(fno_margin.held_extra_text(stock))

    def test_weekend_uses_the_next_sessions_begin_of_day_file(self):
        served = {}

        def fetch(url):
            served.setdefault("urls", []).append(url)
            if url in served.get("files", {}):
                return served["files"][url]
            raise Block("PUBLIC: source unavailable")
        raw = bhav_span()[0]
        i1 = fno_margin.SPAN_URL.format(d="20261123", kind="i1")
        s = fno_margin.SPAN_URL.format(d="20261117", kind="s")
        served["files"] = {i1: raw, s: raw}
        latest, margin = fno_margin.fetch_margin_spans("NIFTY", TRADE, date(2026, 11, 21), fetcher=fetch, market_day=False,
                                                       next_session="2026-11-23")
        self.assertEqual(margin["file_label"], "2026-11-23 begin-of-day file i1")
        self.assertEqual((margin["margin_basis"], margin["next_session"]), ("next_session", "2026-11-23"))
        served["files"] = {s: raw}                                                       # i1 not out yet: the last s file
        latest, margin = fno_margin.fetch_margin_spans("NIFTY", TRADE, date(2026, 11, 21), fetcher=fetch, market_day=False,
                                                       next_session="2026-11-23")
        self.assertEqual(margin["file_label"], f"{TRADE} end-of-day file")
        self.assertIsNone(margin.get("margin_basis"))
        # During the session: today's i1 before the data date's s file.
        self.assertEqual([u for u, _ in fno_margin.begin_of_day_candidates(TRADE, date(2026, 11, 18))][0],
                         fno_margin.SPAN_URL.format(d="20261118", kind="i1"))

    def test_saturday_payoff_names_the_entry_day_and_splits_the_margin(self):
        with tempfile.TemporaryDirectory() as d, no_network():
            argv = ["payoff", "NIFTY", "--leg", "SELL:CE:25250:1", "--leg", "BUY:CE:25500:1", "--charges", card_file(d)]
            code, raw, err = run(argv + ["--format", "json"], SpanFeeds(), clock=SATURDAY)
            self.assertEqual(code, 0, err)
            m = json.loads(raw)["payoff"]["margin"]
            self.assertEqual(m["entry_day"], "2026-11-23")
            self.assertAlmostEqual(m["expiry_extra"], 0.02 * SPOT * LOT, places=2)
            code, text, _ = run(argv + ["--summary"], SpanFeeds(), clock=SATURDAY)
            line = next(l for l in text.splitlines() if l.startswith("- Margin"))
            self.assertIn("expiry-day extra 2%", line)
            self.assertIn("if you enter on Mon 23 Nov (expiry day; the next session, assumed as the entry day)", line)
            self.assertNotIn("±10%", line)
            # With the 23 Nov begin-of-day file published (Friday evening), the weekend run uses it and says so.
            class WithNextI1(SpanFeeds):
                def __call__(self, url):
                    if url.endswith("nsccl.20261123.i1.zip"):
                        self.urls.append(url)
                        return self.span_raw
                    return super().__call__(url)
            code, text, _ = run(argv + ["--summary"], WithNextI1(), clock=SATURDAY)
            self.assertIn("NSE Clearing's start-of-day margin parameters for Mon 23 Nov", text)
            # The same run after the close on Friday assumes the same Monday entry.
            code, raw, err = run(argv + ["--format", "json"], SpanFeeds(), clock=FRIDAY_EVENING)
            self.assertEqual(json.loads(raw)["payoff"]["margin"]["entry_day"], "2026-11-23")

    def test_grid_margin_column_splits_the_extra(self):
        m = fno_margin.estimate(FLY, FLY_LEGS, expiry="2026-11-23", trade_day="2026-11-23", index=True, today="2026-11-21")
        self.assertEqual(fno.margin_cell(m), f"{fno_margin.inr(m['normal_total'])} + 67,082 expiry day")
        note = fno.expiry_day_margin_note(m)
        self.assertIn("on Mon 23 Nov, the next session (which this grid assumes)", note)
        self.assertIn("Broker calculators show only the normal figure before that day.", note)
        plain = fno_margin.estimate(FLY, FLY_LEGS, expiry="2026-11-23", trade_day="2026-11-20", index=True)
        self.assertEqual(fno.margin_cell(plain), fno_margin.inr(plain["total"]))
        self.assertIsNone(fno.expiry_day_margin_note(plain))


# ---------------- 2. one session to expiry ----------------

class OneSessionTests(unittest.TestCase):
    def ctx(self, now):
        return fno.session_context(TRADE, now, {"days": fno.parse_holidays(json.dumps({"CM": [], "FO": [
            {"tradingDate": "24-Nov-2026", "weekDay": "Tuesday", "description": "Prakash Gurpurb Sri Guru Nanak Dev"}]}).encode()),
            "years": [2026]}, live_chain=False)

    def test_sessions_left(self):
        hol = {"days": {date(2026, 11, 24): "Prakash Gurpurb Sri Guru Nanak Dev"}, "years": [2026]}
        cases = [(SATURDAY, 1), (FRIDAY_EVENING, 1), (datetime(2026, 11, 20, 11, 0, tzinfo=IST), 2),
                 (datetime(2026, 11, 19, 11, 0, tzinfo=IST), 3), (MONDAY_OPEN, 1), (datetime(2026, 11, 23, 8, 0, tzinfo=IST), 1),
                 (datetime(2026, 11, 23, 16, 0, tzinfo=IST), 0)]
        for now, want in cases:
            with self.subTest(now=now):
                ctx = fno.session_context(TRADE, now, hol)
                self.assertEqual(fno.sessions_left("2026-11-23", ctx, hol), want)

    def test_lead_line_only_at_one_session(self):
        hol = {"days": {date(2026, 11, 24): "Prakash Gurpurb Sri Guru Nanak Dev"}, "years": [2026]}
        r = {"expiry": "2026-11-23", "expiry_moved": False, "expiry_calendar": [
            {"expiry": "2026-11-23", "type": "monthly"}, {"expiry": "2026-12-01", "type": "weekly"},
            {"expiry": "2026-12-08", "type": "weekly"}, {"expiry": "2026-12-29", "type": "monthly"}]}
        for now, want in ((datetime(2026, 11, 20, 11, 0, tzinfo=IST), None),
                          (SATURDAY, "**Expires Mon 23 Nov — 1 session left**; next expiries: Tue 1 Dec (weekly), Tue 29 Dec (monthly)."),
                          (MONDAY_OPEN, "**Expires today, Mon 23 Nov — the final session is in progress**; next expiries: Tue 1 Dec "
                                        "(weekly), Tue 29 Dec (monthly).")):
            with self.subTest(now=now):
                ctx = fno.session_context(TRADE, now, hol)
                self.assertEqual(fno.expiry_lead(dict(r, session=ctx, sessions_left=fno.sessions_left(r["expiry"], ctx, hol))), want)

    def test_saturday_payoff_and_chain_lead_with_the_expiry_line(self):
        with tempfile.TemporaryDirectory() as d, no_network():
            code, text, err = run(["payoff", "NIFTY", "--leg", "SELL:CE:25250:1", "--summary", "--no-margin", "--charges", card_file(d)],
                                  SpanFeeds(), clock=SATURDAY)
            self.assertEqual(code, 0, err)
            first = text.splitlines()[0]
            self.assertEqual(first, "**Expires Mon 23 Nov — 1 session left** (moved from Tue 24 Nov, NSE trading holiday "
                                    "(Prakash Gurpurb Sri Guru Nanak Dev)); next expiry: Tue 29 Dec (monthly).")
            self.assertIn("**NIFTY payoff, expiry 2026-11-23 (monthly)**", text)           # the move is not repeated in the title
            self.assertIn("(around the forward ", text)
            code, text, err = run(["chain", "NIFTY", "--summary", "--no-vix", "--no-participants"], SpanFeeds(), clock=SATURDAY)
            self.assertEqual(code, 0, err)
            self.assertTrue(text.startswith("**Expires Mon 23 Nov — 1 session left**"), text[:200])
            moves = [l for l in text.splitlines() if "1 SD" in l or "1-sigma" in l or "Expected move" in l]
            self.assertEqual(len(moves), 1, moves)                                         # one figure, not three ranges
            self.assertIn("- Expected move for the Mon 23 Nov expiry session: 1 SD ±", moves[0])
            self.assertIn("around the forward", moves[0])
            self.assertNotIn("by ATM straddle", moves[0])
            self.assertNotIn("Expiry 2026-11-23 is a monthly", text)                      # folded into the lead line
            self.assertIn("**Next session — Mon 23 Nov, the expiry session**", text)

    def test_more_sessions_keep_the_usual_lines(self):
        with no_network():
            code, text, err = run(["chain", "NIFTY", "--summary", "--no-vix", "--no-participants"], SpanFeeds(),
                                  clock=datetime(2026, 11, 17, 18, 0, tzinfo=IST))
        self.assertEqual(code, 0, err)
        self.assertNotIn("session left", text)
        line = next(l for l in text.splitlines() if l.startswith("- Expected move to expiry"))
        self.assertIn("by ATM straddle", line)
        self.assertIn("around the forward", line)


# ---------------- 3. iron fly and relative strikes ----------------

class RelativeStrikeTests(unittest.TestCase):
    def test_parse_and_offsets(self):
        leg = fno.parse_leg("buy:pe:atm-1:1")
        self.assertEqual((leg["relative"], leg["strike"]), ("ATM-1", None))
        for spec, step, want in (("ATM", 100, 0), ("ATM-1", 100, -100), ("ATM+2", 50, 100), ("ATM-1-500", 100, -600),
                                 ("ATM-500PTS", 100, -500), ("ATM+200P", 100, 200), ("ATM+1+50", 100, 150)):
            with self.subTest(spec=spec):
                self.assertEqual(fno.relative_offset(spec, step), want)
        for bad in ("BUY:PE:ATMX:1", "BUY:PE:ATM-:1", "BUY:FUT:ATM:1", "BUY:PE:ATM-1-2-3:1"):
            with self.subTest(bad=bad), self.assertRaises(Block):
                fno.parse_leg(bad)

    def test_resolution_against_the_chain(self):
        r = eod_report()                                   # strikes 24,500–25,500 every 250; spot 25,000
        legs, note = fno.resolve_relative(r, [fno.parse_leg("BUY:PE:ATM-1:1"), fno.parse_leg("SELL:PE:ATM-1-250:1"),
                                              fno.parse_leg("SELL:CE:25500:1")])
        self.assertEqual([l["strike"] for l in legs], [24750.0, 24500.0, 25500.0])
        self.assertEqual(note, "ATM 25,000 = the strike nearest spot 25,000.00 (the Tue 17 Nov close); strike step 250: "
                               "ATM-1 → 24,750 PE, ATM-1-250 → 24,500 PE")
        with self.assertRaises(Block) as e:
            fno.resolve_relative(r, [fno.parse_leg("BUY:PE:ATM-500:1")])
        self.assertIn("nearest listed: 24,500", str(e.exception))
        self.assertIn("write points as ATM-500PTS", str(e.exception))
        self.assertEqual(fno.resolve_relative(r, [fno.parse_leg("BUY:PE:25000:1")])[1], None)

    def test_one_command_payoff_prints_the_resolved_strikes(self):
        with tempfile.TemporaryDirectory() as d, no_network():
            argv = ["payoff", "NIFTY", "--leg", "BUY:PE:ATM-1:1", "--leg", "SELL:PE:ATM-1-250:1", "--summary", "--no-margin",
                    "--charges", card_file(d)]
            code, text, err = run(argv, SpanFeeds(), clock=datetime(2026, 11, 17, 18, 0, tzinfo=IST))
            self.assertEqual(code, 0, err)
            self.assertIn("**Strikes:** ATM 25,000 = the strike nearest spot 25,000.00", text)
            self.assertIn("BUY 1 lot PE 24750", text)
            self.assertIn("SELL 1 lot PE 24500", text)
            code, _, err = run(["payoff", "--leg", "BUY:PE:ATM:1", "--no-margin"], SpanFeeds(), clock=datetime(2026, 11, 17, 18, 0, tzinfo=IST))
            self.assertEqual(code, 2)
            self.assertIn("name the underlying", err)


class IronFlyTests(unittest.TestCase):
    def test_fly_rows_sell_atm_and_buy_wings(self):
        r = eod_report()
        g = fno.grid(r, "fly", 500, card=CARD)
        self.assertEqual(len(g["rows"]), 1)
        self.assertEqual(g["rows"][0]["legs"], ["SELL:CE:25000:1", "SELL:PE:25000:1", "BUY:CE:25500:1", "BUY:PE:24500:1"])
        self.assertEqual(g["atm"], 25000.0)
        self.assertEqual(fno.width_text(g), "500 points from the ATM 25,000")
        # Default widths for NIFTY: 100/150/200/300 all land on the nearest listed wings here (250 apart), so one row survives,
        # labelled with the width the listed strikes give.
        g = fno.grid(r, "fly", None, card=CARD)
        self.assertEqual(g["widths"], [250.0])
        self.assertTrue(g["width_reason"].startswith("default for NIFTY"))
        self.assertTrue(all(x["legs"][:2] == ["SELL:CE:25000:1", "SELL:PE:25000:1"] for x in g["rows"]))

    def test_cli_grid_fly_and_row(self):
        with tempfile.TemporaryDirectory() as d, no_network():
            base = ["grid", "NIFTY", "--structure", "fly", "--width", "500", "--no-margin", "--charges", card_file(d)]
            code, text, err = run(base + ["--summary"], SpanFeeds(), clock=datetime(2026, 11, 17, 18, 0, tzinfo=IST))
            self.assertEqual(code, 0, err)
            self.assertIn("**NIFTY iron fly grid — expiry 2026-11-23, wings 500 points from the ATM 25,000, 1 lot = 65**", text)
            self.assertIn("Wings widen down the rows", text)
            code, text, err = run(base + ["--row", "1", "--summary"], SpanFeeds(), clock=datetime(2026, 11, 17, 18, 0, tzinfo=IST))
            self.assertEqual(code, 0, err)
            self.assertTrue(text.startswith("Grid row 1 (iron fly, wings 500 points from the ATM 25,000): --leg SELL:CE:25000:1"), text[:120])


# ---------------- 4. position size at the N-session low ----------------

def bar(d, o, h, l, c):
    return {"date": d, "open": o, "high": h, "low": l, "close": c}


def sessions(end, n, holidays=(date(2026, 11, 10),)):
    """The last `n` weekdays to `end`, skipping the days Yahoo had no INFY bar for (10 Nov 2026)."""
    out, day = [], end
    while len(out) < n:
        if day.weekday() < 5 and day not in holidays:
            out.append(day)
        day -= timedelta(days=1)
    return out[::-1]


INFY_DAYS = sessions(date(2026, 11, 20), 30)
# A quiet tape near 1,100 (true range ≈ 20) with one spike: 23 Oct opened 1,187, closed 1,153.80 and printed a low of 1,038.05 —
# the reviewed INFY bar — as the oldest bar of the 20-session window.
INFY_BARS = [bar(d, 1100, 1110, 1090, 1100) for d in INFY_DAYS]
SPIKE = INFY_DAYS.index(date(2026, 10, 23))
INFY_BARS[SPIKE] = bar(date(2026, 10, 23), 1187.0, 1197.8, 1038.05, 1153.8)
INFY_BARS[-2] = bar(date(2026, 11, 19), 1096.3, 1097.0, 1072.6, 1091.9)
INFY_BARS[-1] = bar(date(2026, 11, 20), 1087.0, 1118.8, 1081.7, 1098.0)


def chart_payload(symbol, bars):
    stamp = lambda d: int(datetime.combine(d, datetime.min.time(), IST).replace(hour=9, minute=15).timestamp())
    return json.dumps({"chart": {"error": None, "result": [{
        "meta": {"symbol": symbol, "currency": "INR", "exchangeTimezoneName": "Asia/Kolkata", "dataGranularity": "1d"},
        "timestamp": [stamp(b["date"]) for b in bars],
        "indicators": {"quote": [{"open": [b["open"] for b in bars], "high": [b["high"] for b in bars], "low": [b["low"] for b in bars],
                                  "close": [b["close"] for b in bars], "volume": [1000] * len(bars)}]}}]}}).encode()


def ps_run(argv, bars=INFY_BARS):
    def feed(url):
        if "/chart/INFY.NS?" in url:
            return chart_payload("INFY.NS", bars)
        raise Block("PUBLIC: source unavailable")
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err), no_network():
        code = ps.main(argv, fetcher=feed, clock=lambda: SATURDAY)
    return code, out.getvalue(), err.getvalue()


class StopLowTests(unittest.TestCase):
    def test_session_extreme_flags_wick_and_drop(self):
        x = ps.session_extreme(INFY_BARS, 20, "BUY")
        self.assertEqual((x["stop"], x["date"]), (1038.05, "2026-10-23"))
        self.assertTrue(x["wick"])
        self.assertTrue(x["drops"])
        self.assertEqual([(a["why"], a["stop"], a["date"]) for a in x["alternatives"]],
                         [("wick", 1072.6, "2026-11-19"), ("drops", 1072.6, "2026-11-19")])
        # 21 sessions: the spike is no longer the oldest bar, still a wick.
        y = ps.session_extreme(INFY_BARS, 21, "BUY")
        self.assertTrue(y["wick"])
        self.assertFalse(y["drops"])
        # A plain low: no warnings; the most recent of equal lows is used.
        calm = [bar(d, 1100, 1110, 1090, 1100) for d in INFY_DAYS]
        z = ps.session_extreme(calm, 20, "BUY")
        self.assertEqual((z["stop"], z["date"], z["wick"], z["drops"], z["alternatives"]), (1090, "2026-11-20", False, False, []))
        # A short: the N-session high.
        h = ps.session_extreme(INFY_BARS, 20, "SELL")
        self.assertEqual((h["stop"], h["date"], h["kind"]), (1197.8, "2026-10-23", "high"))
        # While the session is open, today's forming bar is left out.
        live = ps.session_extreme(INFY_BARS, 5, "BUY", live=True)
        self.assertEqual(live["window"], ("2026-11-13", "2026-11-19"))
        with self.assertRaises(Block):
            ps.session_extreme(INFY_BARS[:10], 20, "BUY")

    def test_cli_names_the_date_and_gives_the_alternative_size(self):
        code, text, err = ps_run(["--symbol", "INFY", "--capital", "200000", "--risk", "1500", "--stop-low", "20"])
        self.assertEqual(code, 0, err)
        self.assertIn("stop ₹1,038.05 — the 20-session low, set on Fri 23 Oct 2026 (Yahoo daily bars Fri 23 Oct–Fri 20 Nov)", text)
        warn = [l for l in text.splitlines() if l.startswith("- ⚠")]
        self.assertEqual(len(warn), 1, warn)                                   # wick and drop share one alternative: one line
        alt = {p: ps.size(entry=1098.0, stop=1072.6, risk=1500, product=p, capital=200000)["quantity"] for p in ("CNC", "MIS")}
        self.assertIn("is a one-bar wick", warn[0])
        self.assertIn("drops out next session", warn[0])
        self.assertIn(f"₹1,072.60 (Thu 19 Nov 2026); at that stop: delivery {alt['CNC']} shares · intraday {alt['MIS']} shares.", warn[0])
        self.assertLess(text.index("⚠"), text.index("**Delivery (CNC)"))        # up front
        code, raw, err = ps_run(["--symbol", "INFY", "--risk", "1500", "--stop-low", "20", "--format", "json"])
        self.assertEqual(code, 0, err)
        rule = json.loads(raw)["inputs"]["stop_rule"]
        self.assertEqual((rule["stop"], rule["date"], rule["sessions"]), (1038.05, "2026-10-23", 20))

    def test_only_the_drop_warning_when_the_low_is_not_a_wick(self):
        bars = [dict(b) for b in INFY_BARS]
        bars[SPIKE] = bar(date(2026, 10, 23), 1070, 1080, 1060, 1065)
        code, text, err = ps_run(["--symbol", "INFY", "--risk", "1500", "--stop-low", "20"], bars)
        self.assertEqual(code, 0, err)
        warn = [l for l in text.splitlines() if l.startswith("- ⚠")]
        self.assertEqual(len(warn), 1)
        self.assertIn("drops out of the 20-session window next session", warn[0])
        self.assertNotIn("wick", warn[0])

    def test_wick_threshold_is_one_atr(self):
        calm = [bar(d, 1100, 1110, 1090, 1100) for d in INFY_DAYS]
        for low, flagged in ((1075, True), (1085, False)):              # 25 vs 15 below the body; ATR ≈ 21.1 and 20.4
            with self.subTest(low=low):
                bars = calm[:-1] + [bar(INFY_DAYS[-1], 1100, 1110, low, 1100)]
                x = ps.session_extreme(bars, 20, "BUY")
                self.assertEqual((x["stop"], x["wick"]), (low, flagged))
                self.assertAlmostEqual(x["wick_atr"], (1100 - low) / ps.atr(bars), places=9)

    def test_bad_combinations_block(self):
        for argv, msg in ((["--symbol", "INFY", "--risk", "1500", "--stop-low", "20", "--stop", "1000"], "one stop rule"),
                          (["--entry", "1100", "--risk", "1500", "--stop-low", "20"], "need --symbol"),
                          (["--symbol", "INFY", "--risk", "1500", "--stop-high", "20"], "for a SELL"),
                          (["--symbol", "INFY", "--risk", "1500", "--side", "SELL", "--stop-low", "20"], "for a BUY")):
            with self.subTest(argv=argv):
                code, _, err = ps_run(argv)
                self.assertEqual(code, 2)
                self.assertIn(msg, err)


# ---------------- 5. holdings summary ----------------

class HoldingsTaxReasonTests(unittest.TestCase):
    def test_zero_tax_reason(self):
        r = {"tax": {"rates": {"ltcg_exemption": 125000.0}}}
        d = {"short_term_gains": 0, "short_term_losses": 17144.0, "long_term_gains": 224400.0, "long_term_losses": 113900.0,
             "short_term_loss_set_off_against_long_term": 17144.0, "long_term_after_set_off": 93356.0, "taxable_long_term": 0,
             "taxable_short_term": 0}
        self.assertEqual(hr.zero_tax_reason(r, {"detail": d}),
                         "₹17,144 short-term loss set off against long-term gains; ₹93,356 net long-term within the ₹1.25 lakh exemption")
        within = {"long_term_gains": 50000.0, "long_term_after_set_off": 50000.0, "taxable_long_term": 0, "taxable_short_term": 0}
        self.assertEqual(hr.zero_tax_reason(r, {"detail": within}), "₹50,000 long-term within the ₹1.25 lakh exemption")
        losses = {"short_term_losses": 900.0, "long_term_losses": 100.0, "taxable_long_term": 0, "taxable_short_term": 0}
        self.assertEqual(hr.zero_tax_reason(r, {"detail": losses}), "no net gain to tax: losses only")
        self.assertIsNone(hr.zero_tax_reason(r, {}))

    def test_bonus_before_purchase_is_named_not_applied_and_zero_tax_explained(self):
        holdings = [{"symbol": "RELIANCE", "quantity": 30, "average_price": 1200, "bought": "2025-01-15"}]
        with no_network():
            r = test_bonuses.run(holdings)
        row = r["holdings"][0]
        self.assertEqual([e["date"][:7] for e in row["corporate_action_before_purchase"]], ["2024-10"])
        self.assertIsNone(row["corporate_action_applied"])
        brief = hr.summary(r)
        self.assertIn("- **Bonus/split not applied**: RELIANCE 1:1 bonus Oct 2024 predates purchase (Jan 2025)", brief)
        sold = next(l for l in brief.splitlines() if l.startswith("- **If you sold everything today**"))
        self.assertIn("tax ₹0 (", sold)
        self.assertIn("long-term within the ₹1.25 lakh exemption)", sold)

    def test_line_budget_and_merged_bonus_line(self):
        with no_network():
            applied = test_bonuses.run(test_bonuses.RELIANCE)
        before = len(hr.summary(applied).splitlines())
        r = dict(applied, holdings=[dict(h, corporate_action_before_purchase=[{"kind": "bonus", "date": "2024-12-03", "ratio": "1:1 bonus"}],
                                         first_purchase="2025-12-01") for h in applied["holdings"]])
        brief = hr.summary(r)
        self.assertEqual(len(brief.splitlines()), before)                       # merged into the applied line: +0
        self.assertIn(" · not applied: RELIANCE 1:1 bonus Dec 2024 predates purchase (Dec 2025)", brief)


# ---------------- 6. STT ----------------

class SttTests(unittest.TestCase):
    def test_card_carries_the_2026_rates_and_their_source(self):
        card = json.loads((ROOT / "assets" / "charges.example.json").read_text(encoding="utf-8"))
        self.assertEqual(card["stt_exercise"], 0.0015)
        self.assertEqual(card["segments"]["NFO:option"]["stt_sell"], 0.0015)
        self.assertEqual(card["segments"]["NFO:future"]["stt_sell"], 0.0005)
        self.assertIn("Finance Act 2026", card["stt_source"])
        self.assertEqual(card["stt_before"]["effective_until"], "2026-03-31")
        self.assertEqual(fno.STT_EXERCISE, 0.0015)

    def test_rates_follow_the_data_date(self):
        card = fno.load_charge_card()
        old = fno.rates_on(card, "2026-03-31")
        self.assertEqual((old["segments"]["NFO:option"]["stt_sell"], old["segments"]["NFO:future"]["stt_sell"], old["stt_exercise"]),
                         (0.001, 0.0002, 0.00125))
        self.assertIs(fno.rates_on(card, "2026-04-01"), card)
        self.assertIs(fno.rates_on(CARD, "2025-01-01"), CARD)                    # a card without history is used as is

    def test_exercise_stt_in_the_exit_cost(self):
        r = eod_report()
        card = fno.load_charge_card()
        p = fno.payoff(r, [fno.parse_leg("BUY:CE:24500:1")], card=card)
        self.assertIn("exercise STT (0.15% of intrinsic value, rates from 1 Apr 2026 under the Finance Act 2026)", p["costs"]["basis"])
        old = fno.payoff(r, [fno.parse_leg("BUY:CE:24500:1")], card=fno.rates_on(card, "2026-03-31"))
        self.assertIn("exercise STT (0.125% of intrinsic value)", old["costs"]["basis"])


if __name__ == "__main__":
    unittest.main()
