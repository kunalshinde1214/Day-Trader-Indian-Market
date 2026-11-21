"""Offline contracts for the round-7 fixes (19 Nov 2026 held-out review, after the close). Never contacts the network.

1. NSE's option chain at the close: empty placeholder entries (BANKNIFTY PE stubs from 67,400 up: strike 0, no underlying,
   no expiry) are skipped, missing figures and zero OI are tolerated, and a rejected chain prints its reason instead of
   falling back silently; OI walls are labelled against the level quoted now; the chain summary drops participant OI and
   the VIX percentile unless they are extreme.
2. SPAN file by time of day: after the close, today's closing intraday file (the one Zerodha's calculator matched to the
   rupee on 19 Nov 2026); exposure on today's close.
3. Expiry 'moved' labels from NSE's holiday list; ties go to Tuesday.
4. Cash needed (margin + premium paid) for net-debit positions.
5. holdings --summary: 'Bonus applied' line, long-term/short-term split, every ⚠ counted.
6. position_size: --symbol, --stop-pct, --risk-pct, delivery and intraday in one run, a labelled 2R target.
7. 'Now' after the close is labelled as NSE Clearing's closing prices.
"""
import contextlib
from datetime import date, datetime
import io
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
from core import Block, IST, instant
import fno
import fno_margin
import holdings_review as hr
import position_size as ps
from test_fno import EXPIRY, LOT, RATE, SPOT, TRADE
from test_fno_round2 import HOLIDAYS, LiveFeeds, card_file, run
from test_fno_margin import span_xml, span_zip
from test_pf_round6 import AFTER, CLOSE_LEVEL, PostCloseFeeds, STRIKES, all_indices, closing_span, v3_chain
import test_bonuses
import test_buy_price_check as tbp
import test_holdings_review as thr

NOW19 = datetime(2026, 11, 19, 18, 5, tzinfo=IST)
BN_LOT = 30
# NSE's option-chain-v3 for BANKNIFTY 23 Nov 2026 at the 19 Nov close (15:40 stamp), a subset of the real rows:
# (strike, CE (OI, ΔOI, volume, last, change), PE (… ) or "STUB" for NSE's empty placeholder entry).
BN_ROWS = [
    (60000, (8724, -861, 2720, 3141.8, 1167.85), (37233, -4368, 160906, 10.5, -28.35)),
    (61000, (14184, -2800, 17296, 2155, 1079.25), (41802, 8118, 190818, 19.85, -119.4)),
    (61500, (6460, -2101, 26696, 1666.35, 958.5), (22834, 6615, 142014, 35.95, -234.05)),
    (62000, (16693, -17198, 217335, 1201, 775.1), (36923, 10171, 292507, 72.35, -415.15)),
    (62500, (18158, -9223, 387757, 791, 557.65), (23274, 17267, 200228, 155.6, -635.3)),
    (63000, (28839, 538, 445648, 479.85, 361.2), (21276, 16616, 110977, 332, -845.75)),
    (63500, (18654, 1503, 230013, 244.5, 186.3), (1533, 1208, 8781, 606, -1000.25)),
    (64000, (32621, 10035, 245746, 111.55, 81.55), (1651, 1083, 3175, 970, -1107.35)),
    (64500, (13680, 2763, 93769, 44, 27.9), (125, 0, 0, 0, 0)),
    (65000, (28703, 3150, 128168, 19, 8), (1827, -44, 388, 1866.75, -1208.45)),
    (66000, (14311, 659, 39775, 5.85, 0.35), (116, -31, 75, 2849.15, -1096.25)),
    (67400, (0, 0, 0, 0, 0), "STUB"),
    (67600, (0, 0, 0, 0, 0), "STUB"),
]
# The real stub, verbatim (BANKNIFTY 67,400 PE on 19 Nov 2026).
STUB = {"PChange": 0, "buyPrice1": 0, "buyQuantity1": 0, "change": 0, "changeinOpenInterest": 0, "expiryDate": None, "identifier": None,
        "impliedVolatility": 0, "lastPrice": 0, "openInterest": 0, "optionType": None, "pChange": 0, "pchangeinOpenInterest": 0,
        "sellPrice1": 0, "sellQuantity1": 0, "strikePrice": 0, "totalBuyQuantity": 0, "totalSellQuantity": 0, "totalTradedVolume": 0,
        "underlying": None, "underlyingValue": 0}


def bn_chain(rows=BN_ROWS, *, stamp="19-Nov-2026 15:40:00", level=63063, mutate=None):
    data = []
    for k, ce, pe in rows:
        row = {"strikePrice": k, "expiryDates": "23-Nov-2026"}
        for side, x in (("CE", ce), ("PE", pe)):
            if x == "STUB":
                row[side] = dict(STUB)
            elif x is not None:
                oi, doi, vol, last, chg = x
                row[side] = {"strikePrice": k, "expiryDate": "23-11-2026", "underlying": "BANKNIFTY", "openInterest": oi,
                             "changeinOpenInterest": doi, "totalTradedVolume": vol, "lastPrice": last, "change": chg,
                             "underlyingValue": level}
        if mutate:
            mutate(row)
        data.append(row)
    return json.dumps({"records": {"data": data, "timestamp": stamp, "underlyingValue": level}, "filtered": {"data": []}}).encode()


def stub_v3_chain():
    """The round-6 NIFTY close chain plus NSE's empty placeholder entries and an entry missing optional fields."""
    doc = json.loads(v3_chain())
    rows = doc["records"]["data"]
    rows.append({"strikePrice": 26500.0, "CE": dict(STUB), "PE": dict(STUB)})
    rows.append({"strikePrice": 26600.0, "PE": dict(STUB)})
    del rows[0]["CE"]["change"], rows[0]["CE"]["changeinOpenInterest"]
    return json.dumps(doc).encode()


class StubChainFeeds(PostCloseFeeds):
    def __init__(self, chain_bytes, **kw):
        super().__init__(**kw)
        self.chain_bytes = chain_bytes

    def __call__(self, url):
        if fno.OPTION_CHAIN_V3_RE.match(url):
            self.urls.append(url)
            return self.chain_bytes
        return super().__call__(url)


# ---------------- 1. the close chain ----------------

class CloseChainParseTests(unittest.TestCase):
    def test_real_banknifty_rows_with_empty_stubs_parse(self):
        contracts, at, level = fno.parse_chain_v3(bn_chain(), "BANKNIFTY", "2026-11-23", BN_LOT, NOW19)
        self.assertEqual((at.strftime("%H:%M"), level), ("15:40", 63063))
        self.assertFalse([c for c in contracts if c["strike"] in (67400, 67600) and c["kind"] == "PE"])   # stubs skipped
        self.assertEqual(len(contracts), 2 * 11 + 2)                              # 11 full rows + the two real (zero) CE entries
        ce = {c["strike"]: c for c in contracts if c["kind"] == "CE"}
        pe = {c["strike"]: c for c in contracts if c["kind"] == "PE"}
        self.assertEqual(ce[64000]["oi"], 32621 * BN_LOT)
        self.assertEqual((pe[64500]["oi"], pe[64500]["close"]), (125 * BN_LOT, None))   # untraded: no price, OI kept
        self.assertEqual((ce[67400]["oi"], ce[67400]["close"]), (0.0, None))           # zero OI is not an error
        # Independent PCR and max pain over the parsed books.
        call_oi = sum(x[1][0] for x in BN_ROWS)
        put_oi = sum(x[2][0] for x in BN_ROWS if x[2] != "STUB")
        r = fno.analyse_chain(contracts, "BANKNIFTY", expiry="2026-11-23", as_of=datetime(2026, 11, 19, 15, 30, tzinfo=IST), rate=RATE)
        self.assertAlmostEqual(r["pcr_oi"], put_oi / call_oi, places=12)
        strikes = sorted({x[0] for x in BN_ROWS})
        pain = min(strikes, key=lambda s: sum(x[1][0] * max(0, s - x[0]) for x in BN_ROWS)
                   + sum(x[2][0] * max(0, x[0] - s) for x in BN_ROWS if x[2] != "STUB"))
        self.assertEqual(r["max_pain"], pain)

    def test_benign_shape_variations_are_tolerated(self):
        def drop_fields(row):
            for side in ("CE", "PE"):
                c = row.get(side)
                if isinstance(c, dict) and c.get("underlying") and row["strikePrice"] == 63000:
                    for key in ("change", "changeinOpenInterest", "openInterest", "strikePrice"):   # strike from the row
                        c.pop(key, None)
        contracts, _, _ = fno.parse_chain_v3(bn_chain(mutate=drop_fields), "BANKNIFTY", "2026-11-23", BN_LOT, NOW19)
        c = next(x for x in contracts if x["strike"] == 63000 and x["kind"] == "CE")
        self.assertEqual((c["oi"], c["oi_change"], c["prev_close"], c["close"]), (0.0, 0.0, None, 479.85))
        # A row that is not a dict, a side that is null or empty, and a stub with only some fields are all skipped.
        doc = json.loads(bn_chain())
        doc["records"]["data"] += [None, {"strikePrice": 68000, "CE": None, "PE": {}},
                                   {"strikePrice": 68100, "PE": {"strikePrice": None, "underlying": "", "expiryDate": None}}]
        self.assertEqual(len(fno.parse_chain_v3(json.dumps(doc).encode(), "BANKNIFTY", "2026-11-23", BN_LOT, NOW19)[0]), 24)

    def test_real_mismatches_are_rejected_with_the_reason(self):
        def other(row):
            if row["strikePrice"] == 62000:
                row["PE"]["underlying"] = "NIFTY"
        with self.assertRaisesRegex(Block, r"unexpected row in the NSE option chain \(NIFTY 23-11-2026 PE at strike 62,000; wanted BANKNIFTY"):
            fno.parse_chain_v3(bn_chain(mutate=other), "BANKNIFTY", "2026-11-23", BN_LOT, NOW19)

        def wrong_strike(row):
            if row["strikePrice"] == 62000:
                row["CE"]["strikePrice"] = 62100
        with self.assertRaisesRegex(Block, r"CE strike 62100 in the row for strike 62000"):
            fno.parse_chain_v3(bn_chain(mutate=wrong_strike), "BANKNIFTY", "2026-11-23", BN_LOT, NOW19)
        with self.assertRaisesRegex(Block, r"not from today's close \(stamped 19-Nov-2026 14:10:00\)"):
            fno.parse_chain_v3(bn_chain(stamp="19-Nov-2026 14:10:00"), "BANKNIFTY", "2026-11-23", BN_LOT, NOW19)
        with self.assertRaisesRegex(Block, "no records"):
            fno.parse_chain_v3(b'{"records": {}}', "BANKNIFTY", "2026-11-23", BN_LOT, NOW19)
        with self.assertRaisesRegex(Block, "shows no open interest"):
            zero = [(k, (0, 0, 10, 5, 0), (0, 0, 10, 5, 0)) for k in (62000, 62500, 63000, 63500)]
            fno.parse_chain_v3(bn_chain(zero), "BANKNIFTY", "2026-11-23", BN_LOT, NOW19)

    def test_nse_chain_report_returns_the_reason(self):
        report = fno.analyse_chain(fno.parse_bhavcopy(LiveFeeds()(fno.BHAVCOPY.format(d=TRADE.replace("-", ""))), "NIFTY"), "NIFTY",
                                   rate=RATE)
        fresh, why = fno.nse_chain_report(report, "NIFTY", fetcher=lambda url: stub_v3_chain(), now=AFTER)
        self.assertIsNone(why)
        self.assertEqual(fresh["data_date"], "2026-11-18")
        fresh, why = fno.nse_chain_report(report, "NIFTY", fetcher=lambda url: v3_chain(expiry_text="29-12-2026"), now=AFTER)
        self.assertIsNone(fresh)
        self.assertIn("unexpected row in the NSE option chain (NIFTY 29-12-2026", why)
        fresh, why = fno.nse_chain_report(report, "NIFTY", fetcher=lambda url: (_ for _ in ()).throw(Block("PUBLIC: NSE option chain unavailable")),
                                          now=AFTER)
        self.assertEqual((fresh, why), (None, "NSE option chain unavailable"))


class CloseChainCliTests(unittest.TestCase):
    def test_stub_rows_no_longer_force_the_stale_bhavcopy(self):
        code, text, err = run(["chain", "NIFTY", "--summary", "--no-vix"], StubChainFeeds(stub_v3_chain()), clock=AFTER)
        self.assertEqual(code, 0, err)
        self.assertIn("— data 2026-11-18 close (NSE option chain, 18 Nov 15:30 close)", text.splitlines()[0])
        self.assertNotIn("not yet published", text)
        self.assertNotIn("was not used", text)

    def test_a_rejected_chain_says_why(self):
        code, text, err = run(["chain", "NIFTY", "--summary", "--no-vix"], StubChainFeeds(v3_chain(expiry_text="29-12-2026")), clock=AFTER)
        self.assertEqual(code, 0, err)
        lines = text.splitlines()
        self.assertTrue(lines[0].startswith("**Today's (Wed 18 Nov) F&O bhavcopy is not yet published"))
        self.assertTrue(lines[1].startswith("**NSE's option chain at today's close was not used: unexpected row in the NSE option chain "
                                            "(NIFTY 29-12-2026"), lines[1])
        self.assertIn("so the figures below are the last bhavcopy's (Tue 17 Nov).", lines[1])
        code, raw, _ = run(["chain", "NIFTY", "--no-vix", "--format", "json"], StubChainFeeds(v3_chain(stamp="18-Nov-2026 14:00:00")),
                           clock=AFTER)
        self.assertIn("not from today's close (stamped 18-Nov-2026 14:00:00)", json.loads(raw)["chain_rejected"])
        code, text, _ = run(["chain", "NIFTY", "--no-vix"], PostCloseFeeds(chain=False), clock=AFTER)  # full report too
        self.assertIn("**NSE's option chain at today's close was not used: NSE option chain unavailable**", text)


class WallsAndTrimTests(unittest.TestCase):
    def test_walls_are_labelled_against_the_level_quoted_now(self):
        code, raw, err = run(["chain", "NIFTY", "--summary", "--no-vix", "--format", "json"], PostCloseFeeds(chain=False), clock=AFTER)
        self.assertEqual(code, 0, err)
        s = json.loads(raw)
        where, calls, puts = s["oi_walls"]
        level = s["now_estimate"]["level"] if (s.get("now_estimate") or {}).get("available") else s["live"]["level"]
        self.assertNotEqual(level, s["spot"])
        self.assertTrue(where.startswith("the NSE close"))
        self.assertTrue(all(k >= level for k in calls) and all(k <= level for k in puts))
        code, text, _ = run(["chain", "NIFTY", "--summary", "--no-vix"], PostCloseFeeds(chain=False), clock=AFTER)
        self.assertIn(f"- Call OI walls above {where}: ", text)
        self.assertNotIn("Call OI walls above spot", text)
        code, full, _ = run(["chain", "NIFTY", "--no-vix"], PostCloseFeeds(chain=False), clock=AFTER)
        self.assertIn(f"- **Highest call OI above {where} (resistance by OI):**", full)
        # When the chain's own spot is the level quoted now, it stays 'spot'.
        code, text, _ = run(["chain", "NIFTY", "--summary", "--no-vix"], PostCloseFeeds(), clock=AFTER)
        self.assertIn("- Call OI walls above spot: ", text)

    def test_vix_percentile_and_participants_only_when_extreme(self):
        vix = {"vix": 15.8, "date": "2026-11-19", "percentile_1y": 0.68, "low_1y": 9.15, "high_1y": 27.89, "note": "NSE's close; …"}
        self.assertEqual(fno.vix_summary_text(vix), "India VIX 15.80 (close 2026-11-19)")
        self.assertIn("95% 1-year percentile", fno.vix_summary_text(dict(vix, percentile_1y=0.95)))
        self.assertIn("5% 1-year percentile", fno.vix_summary_text(dict(vix, percentile_1y=0.05)))
        po = lambda share: {"status": "ok", "rows": {"FII": {"fut_long_pct": share}}}
        self.assertEqual([fno.participants_notable(po(x)) for x in (0.32, 0.25, 0.80, 0.5)], [False, True, True, False])
        self.assertFalse(fno.participants_notable({"status": "unavailable", "note": "x"}))
        code, raw, _ = run(["chain", "NIFTY", "--summary", "--format", "json"], PostCloseFeeds(), clock=AFTER)
        v = json.loads(raw)["vix"]
        self.assertEqual(v["vix"], 15.56)                                         # NSE's close, ranked in the history
        self.assertNotIn("_closes", v)
        self.assertEqual((v["high_1y"], v["percentile_1y"]), (15.56, 1.0))       # Yahoo's stale 18.00 bar for today is replaced
        with mock.patch.object(fno, "VIX_EXTREME", -0.01):                        # a middling percentile: summary leaves it out
            code, text, _ = run(["chain", "NIFTY", "--summary"], PostCloseFeeds(), clock=AFTER)
        self.assertNotIn("Who holds index F&O", text)                             # FIIs 33% long: full report only
        self.assertNotIn("1-year percentile", text)
        self.assertIn("- India VIX 15.56 (close 2026-11-18)", text)
        self.assertIn("participant-wise OI", text.splitlines()[-2])
        code, full, _ = run(["chain", "NIFTY"], PostCloseFeeds(), clock=AFTER)
        self.assertIn("## Participant-wise OI", full)


class EveningVixTests(unittest.TestCase):
    def test_nse_vix_close_also_after_the_bhavcopy_is_out(self):
        class Evening(PostCloseFeeds):
            def __call__(self, url):
                if url == fno.NSE_ALL_INDICES:
                    self.urls.append(url)
                    return all_indices(stamp="17-Nov-2026 15:30")
                return super().__call__(url)
        evening = datetime(2026, 11, 17, 22, 20, tzinfo=IST)                   # the 17 Nov bhavcopy is today's
        yahoo = {"vix": 14.0, "date": "2026-11-17", "requested_date": "2026-11-17", "exact_date": True, "note": None,
                 "percentile_1y": 0.75, "low_1y": 12.0, "high_1y": 15.0, "_closes": [13.0, 12.0, 15.0, 14.0]}
        with mock.patch.object(fno, "vix_context", return_value=yahoo):
            code, raw, err = run(["chain", "NIFTY", "--summary", "--format", "json"], Evening(), clock=evening)
        self.assertEqual(code, 0, err)
        v = json.loads(raw)["vix"]
        self.assertEqual((v["vix"], v["date"], v["source"]), (15.56, "2026-11-17", "NSE allIndices"))   # Yahoo's bar says 14.00
        self.assertIn("whose last bar still shows 14.00", v["note"])
        self.assertEqual((v["percentile_1y"], v["high_1y"]), (1.0, 15.56))   # today's Yahoo bar replaced by NSE's close
        self.assertNotIn("_closes", v)


# ---------------- 2. SPAN file by time of day ----------------

def span_for(created, symbol="NIFTY", level=CLOSE_LEVEL):
    return closing_span(level=level, created=created)


class SpanTimeOfDayTests(unittest.TestCase):
    def fetcher(self, files):
        seen = []

        def fetch(url):
            seen.append(url)
            for key, raw in files.items():
                if url.endswith(key):
                    return raw
            raise Block("PUBLIC: SPAN file unavailable")
        fetch.seen = seen
        return fetch

    def test_after_the_close_the_closing_intraday_file_sets_the_margin(self):
        f = self.fetcher({"nsccl.20261118.i5.zip": span_for("202611181532"), "nsccl.20261117.s.zip": span_for("202611171701")})
        latest, margin = fno_margin.fetch_margin_spans("NIFTY", TRADE, date(2026, 11, 18), fetcher=f, after_close=True)
        self.assertEqual((latest["file_label"], margin["file_label"], margin["margin_basis"]),
                         ("2026-11-18 intraday file i5", "2026-11-18 intraday file i5", "closing"))
        self.assertTrue(fno_margin.closing_file(margin, date(2026, 11, 18)))
        # During the session (or before the open) the begin-of-day file still sets it.
        latest, margin = fno_margin.fetch_margin_spans("NIFTY", TRADE, date(2026, 11, 18), fetcher=f, after_close=False)
        self.assertEqual(margin["file_label"], f"{TRADE} end-of-day file")
        self.assertIsNone(margin.get("margin_basis"))

    def test_before_the_closing_file_is_out_it_falls_back_to_begin_of_day(self):
        f = self.fetcher({"nsccl.20261118.i4.zip": span_for("202611181403"), "nsccl.20261117.s.zip": span_for("202611171701")})
        latest, margin = fno_margin.fetch_margin_spans("NIFTY", TRADE, date(2026, 11, 18), fetcher=f, after_close=True)
        self.assertEqual(margin["file_label"], f"{TRADE} end-of-day file")
        self.assertFalse(fno_margin.closing_file(latest, date(2026, 11, 18)))
        yesterday = dict(fno_margin.parse_span(span_for("202611171532"), "NIFTY"), status="ok", file_label="2026-11-17 intraday file i5")
        self.assertTrue(fno_margin.closing_file(yesterday, date(2026, 11, 17)))
        self.assertFalse(fno_margin.closing_file(yesterday, date(2026, 11, 18)))     # yesterday's closing file is not today's

    def test_after_the_close_margin_and_exposure_use_todays_close(self):
        with tempfile.TemporaryDirectory() as d:
            code, raw, err = run(["payoff", "NIFTY", "--leg", "SELL:CE:25250:1", "--format", "json", "--charges", card_file(d)],
                                 PostCloseFeeds(), clock=AFTER)
        self.assertEqual(code, 0, err)
        m = json.loads(raw)["payoff"]["margin"]
        self.assertEqual((m["margin_basis"], m["underlying_close"]), ("closing", CLOSE_LEVEL))   # not the 17 Nov close 25,000
        self.assertAlmostEqual(m["exposure"], 0.02 * CLOSE_LEVEL * LOT, places=6)
        with tempfile.TemporaryDirectory() as d:
            code, text, _ = run(["payoff", "NIFTY", "--leg", "SELL:CE:25250:1", "--summary", "--charges", card_file(d)],
                                PostCloseFeeds(), clock=AFTER)
        self.assertIn("NSE Clearing's closing margin parameters (15:32 IST today), the ones Zerodha's calculator used after the close", text)


# ---------------- 3. expiry 'moved' labels ----------------

def contract(expiry, kind="CE"):
    return {"kind": kind, "expiry": expiry, "strike": 25000.0 if kind != "FUT" else None, "trade_date": TRADE}


class ExpiryCalendarTests(unittest.TestCase):
    FIN = ["2026-11-23", "2026-12-29", "2027-01-25"]          # FINNIFTY's three monthlies on 18 Nov 2026

    def test_holiday_list_decides_the_move_and_ties_go_to_tuesday(self):
        cal = fno.expiry_calendar([contract(e) for e in self.FIN], TRADE, HOLIDAYS)
        cal = fno.annotate_holidays(cal, HOLIDAYS)
        self.assertEqual(cal["usual_weekday"], "Tuesday")
        rows = {x["expiry"]: x for x in cal["expiries"]}
        self.assertEqual((rows["2026-11-23"]["moved"], rows["2026-11-23"]["usual_date"], rows["2026-11-23"]["holiday"]),
                         (True, "2026-11-24", "Prakash Gurpurb Sri Guru Nanak Dev"))
        self.assertFalse(rows["2026-12-29"]["moved"])
        self.assertEqual((rows["2027-01-25"]["moved"], rows["2027-01-25"]["usual_date"]), (True, "2027-01-26"))   # 2027 list not out
        self.assertIn(fno.MOVED_NOTE, fno.expiry_text(rows["2027-01-25"], "Tuesday"))
        plain = fno.expiry_calendar([contract(e) for e in self.FIN], TRADE)              # no holiday list: 1–1 tie → Tuesday
        self.assertEqual(plain["usual_weekday"], "Tuesday")
        self.assertFalse(next(x for x in plain["expiries"] if x["expiry"] == "2026-12-29")["moved"])

    def test_a_single_moved_expiry_is_explained_by_the_holiday_list(self):
        cal = fno.expiry_calendar([contract("2026-11-23")], TRADE, HOLIDAYS)
        self.assertEqual((cal["usual_weekday"], cal["expiries"][0]["usual_date"]), ("Tuesday", "2026-11-24"))
        self.assertIsNone(fno.expiry_calendar([contract("2026-11-23")], TRADE)["usual_weekday"])        # without it: no guess

    def test_stand_in_skips_weekends_and_needs_a_holiday_on_the_weekday(self):
        hol = {"days": {date(2026, 12, 21): "A", date(2026, 12, 22): "B"}, "years": [2026]}
        self.assertEqual(fno.holiday_stand_in(date(2026, 12, 18), 1, hol), date(2026, 12, 22))   # Fri for Tue, Mon also shut
        self.assertIsNone(fno.holiday_stand_in(date(2026, 12, 18), 1, {"days": {date(2026, 12, 22): "B"}, "years": [2026]}))
        self.assertIsNone(fno.holiday_stand_in(date(2026, 11, 23), 1, None))
        self.assertIsNone(fno.holiday_stand_in(date(2026, 11, 30), 1, HOLIDAYS))                 # Tue 1 Dec is a trading day
        weekly = ["2026-12-01", "2026-12-08", "2026-12-21", "2026-12-29"]                        # Mon 21 Dec for a Tue holiday
        cal = fno.expiry_calendar([contract(e) for e in weekly], TRADE,
                                  {"days": {date(2026, 12, 22): "Test Holiday"}, "years": [2026]})
        row = next(x for x in cal["expiries"] if x["expiry"] == "2026-12-21")
        self.assertEqual((row["type"], row["moved"], row["usual_date"]), ("weekly", True, "2026-12-22"))

    def test_cli_expiries_and_payoff_headers(self):
        code, text, _ = run(["expiries", "NIFTY"], LiveFeeds())
        self.assertIn("- 2026-11-23 — monthly, Monday — moved from Tuesday 2026-11-24, NSE trading holiday (Prakash Gurpurb", text)
        self.assertNotIn("moved from Monday", text)


# ---------------- 4. cash needed ----------------

class CashNeededTests(unittest.TestCase):
    def test_debit_adds_the_premium_to_the_margin(self):
        m = {"available": True, "total": 33168.59, "broker_low": 29670.0}
        low, high, paid = fno.cash_needed(m, -8429.0)
        self.assertEqual((round(low), round(high), paid), (38099, 41598, 8429.0))
        self.assertEqual(fno.cash_text((low, high, paid)), "cash needed ≈ margin + premium paid ₹8,429 = ₹38,099–₹41,598")
        self.assertIsNone(fno.cash_needed(m, 22481.0))                                        # a net credit
        self.assertIsNone(fno.cash_needed({"available": False}, -100.0))
        self.assertEqual(fno.cash_needed({"available": True, "total": 0.0, "broker_low": None}, -500.0), (500.0, 500.0, 500.0))

    def test_payoff_prints_cash_needed_for_a_bull_call_spread(self):
        with tempfile.TemporaryDirectory() as d:
            argv = ["payoff", "NIFTY", "--leg", "BUY:CE:24750:1", "--leg", "SELL:CE:25000:1", "--charges", card_file(d)]
            code, raw, err = run(argv + ["--format", "json"], PostCloseFeeds(), clock=AFTER)
            self.assertEqual(code, 0, err)
            p = json.loads(raw)["payoff"]
            code, text, _ = run(argv + ["--summary"], PostCloseFeeds(), clock=AFTER)
        paid = -p["placed_now"]["outcome"]["net_premium"]
        m = p["margin"]
        row = next(l for l in text.splitlines() if l.startswith("| Cash needed (margin + premium paid) |"))
        high = fno_margin.inr(m["total"] + paid)
        self.assertIn(f"₹{high} (₹{fno_margin.inr(paid)} premium)", row.split("|")[2])
        code, text, _ = run(["payoff", "NIFTY", "--leg", "SELL:CE:25250:1", "--summary"], PostCloseFeeds(), clock=AFTER)
        self.assertNotIn("Cash needed", text)                                                 # a credit: margin only


# ---------------- 5. holdings summary ----------------

class HoldingsSummaryTests(unittest.TestCase):
    def test_bonus_line_and_term_split(self):
        r = test_bonuses.run(test_bonuses.RELIANCE)
        brief = hr.summary(r)
        self.assertIn("- **Bonus applied** (shares and average cost adjusted, total cost unchanged): RELIANCE 30 → 60 (1:1, Oct 2024)", brief)
        lt = r["tax"]["unrealised"]["long"]["gain"]
        sold = next(l for l in brief.splitlines() if l.startswith("- **If you sold everything today**"))
        self.assertTrue(sold.endswith(f" · all {hr.inr(lt)} long-term"), sold)
        self.assertNotIn("Bonus applied", next(l for l in brief.splitlines() if "item" in l and "check" in l or "Nothing flagged" in l))

    def test_term_text_variants(self):
        r = {"tax": {"unrealised": {"long": {"gain": 12000.0}, "short": {"gain": -3000.0}}}}
        self.assertEqual(hr.term_text(r, {}), " · ₹12,000 long-term, -₹3,000 short-term")
        r = {"tax": {"unrealised": {"long": {"gain": None}, "short": {"gain": 500.0}}}}
        self.assertEqual(hr.term_text(r, {"undated_symbols": ["INFY"]}), " · ₹500 short-term; INFY undated, term unknown")
        self.assertEqual(hr.term_text({"tax": {"unrealised": {}}}, {}), "")

    def test_every_flagged_price_is_counted(self):
        r = {"holdings": [{"symbol": "SBIN", "price_warning": "x", "price_checks": [{}, {}]},
                          {"symbol": "ITC", "price_warning": "y", "price_checks": [{}]},
                          {"symbol": "OLDCO", "price_stale": True}],
             "sold_price_flags": {"TCS": 1}}
        groups = hr.flags(r)
        self.assertEqual(groups, [("buy/sell price", ["SBIN ×2", "ITC", "TCS"]), ("stale price", ["OLDCO"])])
        self.assertEqual(sum(hr.flag_count(x) for _, s in groups for x in s), 5)

    def test_sold_out_symbol_with_a_flagged_price_is_counted_in_the_summary(self):
        tb = (thr.TRADEBOOK_HEAD + thr.trade("SBIN", "2026-10-01", "buy", 10, 600) + thr.trade("SBIN", "2026-10-20", "sell", 10, 970)
              + thr.trade("TCS", "2026-10-01", "buy", 5, 100))
        src = hr.parse_text(tb, "tb.csv")
        r = hr.review(src, None, fetcher=tbp.Feed({"SBIN": (tbp.SBIN_BARS, ())}), clock=lambda: tbp.NOW, sleep=lambda _: None)
        self.assertEqual(r["sold_price_flags"], {"SBIN": 1})                     # the ₹600 buy is outside 937.50–960
        brief = hr.summary(r)
        self.assertIn("buy/sell price", brief)
        self.assertIn("- **⚠ 1 item to check**: buy/sell price SBIN", brief)        # sold in full, still counted


# ---------------- 6. position size ----------------

def ps_run(argv, feed=None):
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        code = ps.main(argv, fetcher=feed or thr.Feed(prices={"TCS": (2300.0, 2342.7)}), clock=lambda: thr.NOW)
    return code, out.getvalue(), err.getvalue()


class PositionSizeTests(unittest.TestCase):
    def test_one_command_for_capital_risk_pct_stop_pct_and_symbol(self):
        code, text, err = ps_run(["--symbol", "TCS", "--capital", "500000", "--risk-pct", "1", "--stop-pct", "2"])
        self.assertEqual(code, 0, err)
        self.assertIn("Entry ₹2,342.70 — TCS last close, Tue 17 Nov 2026 (Yahoo daily bar) · stop ₹2,295.85 (2% below entry) · "
                      "risk ₹5,000 (1% of ₹5,00,000)", text)
        cnc = ps.size(entry=2342.7, stop=2295.85, risk=5000, product="CNC", capital=500000)
        mis = ps.size(entry=2342.7, stop=2295.85, risk=5000, product="MIS", capital=500000)
        self.assertIn(f"**Delivery (CNC): {cnc['quantity']} shares** · **Intraday (MIS): {mis['quantity']} shares**", text)
        self.assertEqual((cnc["quantity"], mis["quantity"]), (95, 103))                     # the reviewer's hand check
        self.assertIn("target ₹2,436.40 (2R default; pass --target)", text)
        code, raw, _ = ps_run(["--symbol", "TCS.NS", "--risk", "5000", "--stop-pct", "2", "--format", "json"])
        data = json.loads(raw)
        self.assertEqual([x["product"] for x in data["sizes"]], ["CNC", "MIS"])
        self.assertEqual(data["inputs"]["price_quote"]["date"], "2026-11-17")
        self.assertFalse(data["submitted"])

    def test_given_target_is_not_labelled_default_and_single_product_still_works(self):
        code, text, _ = ps_run(["--entry", "100", "--stop", "98", "--risk", "1000", "--target", "105", "--product", "CNC"])
        self.assertEqual(code, 0)
        self.assertIn("target ₹105.00", text)
        self.assertNotIn("2R default", text)
        code, text, _ = ps_run(["--entry", "100", "--stop", "98", "--risk", "1000", "--product", "MIS"])
        self.assertIn("target ₹104.00 (2R default; pass --target)", text)
        code, text, _ = ps_run(["--entry", "100", "--stop-pct", "2", "--risk", "1000", "--side", "SELL"])   # a short is intraday only
        self.assertIn("# Position size — SELL MIS (intraday)", text)
        self.assertIn("stop ₹102.00 (2% above entry)", text)

    def test_stop_pct_rounds_to_the_tick_and_bad_inputs_block(self):
        self.assertEqual(ps.stop_from_pct(2342.7, 2, "BUY"), 2295.85)
        self.assertEqual(ps.stop_from_pct(101.03, 1, "SELL"), 102.05)
        for argv, msg in ((["--entry", "100", "--stop", "98"], "give --risk"),
                          (["--entry", "100", "--stop", "98", "--risk-pct", "1"], "needs --capital"),
                          (["--entry", "100", "--stop", "98", "--risk", "1", "--risk-pct", "1", "--capital", "9"], "not both"),
                          (["--entry", "100", "--stop", "98", "--stop-pct", "2", "--risk", "500"], "--stop or --stop-pct"),
                          (["--entry", "100", "--stop-pct", "60", "--risk", "500"], "between 0 and 50"),
                          (["--risk", "500", "--stop-pct", "2"], "--entry PRICE or --symbol"),
                          (["--symbol", "NOSUCH", "--risk", "500", "--stop-pct", "2"], "no price for NOSUCH")):
            code, _, err = ps_run(argv)
            self.assertEqual(code, 2, argv)
            self.assertIn(msg, err)


# ---------------- 7. 'now' label after the close ----------------

class ClosingPriceLabelTests(unittest.TestCase):
    def test_closing_prices_are_labelled(self):
        with tempfile.TemporaryDirectory() as d:
            code, text, _ = run(["payoff", "NIFTY", "--leg", "SELL:CE:25250:1", "--summary", "--charges", card_file(d)],
                                PostCloseFeeds(), clock=AFTER)
        self.assertIn(f"live quotes ({fno.CLOSING_PRICE_NOTE})", text)
        self.assertEqual(fno.CLOSING_PRICE_NOTE, "NSE Clearing's closing prices; the bhavcopy close can differ slightly")
        code, text, _ = run(["chain", "NIFTY", "--summary", "--no-vix"], PostCloseFeeds(chain=False), clock=AFTER)
        self.assertIn(f"closing option prices ({fno.CLOSING_PRICE_NOTE})", text)


if __name__ == "__main__":
    unittest.main()
