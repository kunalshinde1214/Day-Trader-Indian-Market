"""Offline contracts for the round-3 portfolio/F&O fixes (18 Nov 2026 review). Never contacts the network.

1. Day-over-day and participant-OI comparisons use the previous trading day from the NSE holiday calendar, retry its file
   once, and label any multi-session gap (never "on the day"); a comparison window spanning an expiry is flagged.
2. holdings_review retries failed prices once and marks P&L, XIRR and tax "not a full figure" when a price is still missing.
3. chain shows a "Now (estimate)" line from NSE Clearing's intraday snapshot during market hours and its summary leads with
   the next session's dated range (today's range from yesterday's close is left out).
4. Margin is computed on NSE Clearing's begin-of-day SPAN file (reconciled to a broker calculator to the rupee) and shown
   with a ±10% band; the intraday file is reported alongside.
5. holdings --summary: plain midpoint wording and the long-term loss left over.
6. grid --summary (six columns), the monthly-only note and one ±SD basis for the stress table and the 68% range.
7. The snapshot's own option prices set the level that moves to live are measured from, not the file's index level.
"""
from collections import Counter
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
from core import Block, IST
import fno
import fno_margin
import holdings_review as hr
from test_fno import (EXPIRY, LOT, PREV, PREV_CALL_OI, PREV_PUT_OI, PREV_SPOT, PREV_VOL, RATE, SPOT, TRADE, VIX_CLOSES, VIX_DAYS,
                      bhavcopy, row, vix_chart)
from test_fno_round2 import HOLIDAYS, LIVE_AT, LIVE_LEVEL, card_file, participant_csv, run
from test_fno_margin import SNAP_LEVEL, SNAP_VOL, SpanFeeds, bhav_span, independent_margin, span_xml, span_zip
from test_portfolio_valuation import CHARTS, Feed, NOW as HR_NOW, QUESTION
from test_holdings_review import holdings_doc

# NSE's calendar for this test: Monday 16 Nov 2026 is a normal trading day; 24 Nov is the Guru Nanak Jayanti holiday.
CALENDAR_JSON = json.dumps({"CM": [], "FO": [
    {"tradingDate": "24-Nov-2026", "weekDay": "Tuesday", "description": "Prakash Gurpurb Sri Guru Nanak Dev"}]}).encode()
CALENDAR = {"days": fno.parse_holidays(CALENDAR_JSON), "years": [2026], "source": "test"}
MONDAY = "2026-11-16"


def expiring_today(oi):
    """A NIFTY option expiring on the data date (17 Nov) with `oi` units still open."""
    return row(FinInstrmTp="IDO", XpryDt=TRADE, StrkPric="24800.00", OptnTp="PE", ClsPric=1, PrvsClsgPric=3, SttlmPric=1,
               OpnIntrst=oi, ChngInOpnIntrst=-oi, TtlTradgVol=50)


class CalendarFeeds:
    """Bhavcopies for 17, 16 and 13 Nov and participant OI for the same days; the Monday files can fail a set number of
    times (a transient failure) or for good. Records every URL."""

    def __init__(self, *, monday_fails=0, monday_participant_fails=0, calendar=CALENDAR_JSON, expiring_oi=650):
        self.monday_fails, self.monday_participant_fails = monday_fails, monday_participant_fails
        self.calendar, self.expiring_oi, self.urls, self.count = calendar, expiring_oi, [], Counter()

    def __call__(self, url):
        self.urls.append(url)
        if url == fno.NSE_HOLIDAYS:
            if self.calendar is None:
                raise Block("PUBLIC: source unavailable")
            return self.calendar
        if "INDIAVIX" in url:
            return vix_chart(VIX_DAYS, VIX_CLOSES)
        if "fao_participant_oi_" in url:
            day = url.rsplit("_", 1)[1][:8]
            if day == "17112026":
                return participant_csv("Nov 17, 2026")
            if day == "16112026":
                self.count["participant16"] += 1
                if self.count["participant16"] <= self.monday_participant_fails:
                    raise Block("PUBLIC: source unavailable")
                return participant_csv("Nov 16, 2026", fii_long_delta=2000)
            if day == "13112026":
                return participant_csv("Nov 13, 2026", fii_long_delta=5000)
            raise Block("PUBLIC: source unavailable")
        if "BhavCopy" in url:
            day = url.split("_")[-3]
            if day == TRADE.replace("-", ""):
                return bhavcopy(extra=[expiring_today(self.expiring_oi)])
            if day == MONDAY.replace("-", ""):
                self.count["bhav16"] += 1
                if self.count["bhav16"] <= self.monday_fails:
                    raise Block("PUBLIC: source unavailable")
                return bhavcopy(trade=MONDAY, spot=24950.0, vol=0.16, call_oi=PREV_CALL_OI, put_oi=PREV_PUT_OI)
            if day == PREV.replace("-", ""):
                return bhavcopy(trade=PREV, spot=PREV_SPOT, vol=PREV_VOL, call_oi=PREV_CALL_OI, put_oi=PREV_PUT_OI)
        raise Block("PUBLIC: source unavailable")


def bhav_days(feeds):
    return [u.split("_")[-3] for u in feeds.urls if "BhavCopy" in u]


def eod():
    return fno.analyse_chain(fno.parse_bhavcopy(bhavcopy(), "NIFTY"), "NIFTY", rate=RATE)


# ---------------- 1. previous trading day from the calendar ----------------

class PreviousSessionTests(unittest.TestCase):
    def test_missing_previous_file_is_retried_then_labelled_as_a_multi_session_gap(self):
        feeds = CalendarFeeds(monday_fails=99)
        dod = fno.compare_previous(eod(), "NIFTY", fetcher=feeds, holidays=CALENDAR)
        self.assertEqual(bhav_days(feeds), ["20261116", "20261116", "20261113"])           # tried twice, then the walk back
        self.assertEqual((dod["status"], dod["previous_date"], dod["expected_previous_date"], dod["sessions"]),
                         ("ok", PREV, MONDAY, 2))
        self.assertEqual(dod["gap_note"], "Mon 16 Nov bhavcopy unavailable (tried twice); compared with Fri 13 Nov instead (2 sessions)")
        with mock.patch.object(fno, "FII_LONG_EXTREME", 0.5):  # summary shows the participant line only when notable
            code, text, err = run(["chain", "NIFTY", "--summary", "--no-vix"], CalendarFeeds(monday_fails=99, monday_participant_fails=99))
        self.assertEqual(code, 0, err)
        self.assertIn("- ⚠ Mon 16 Nov bhavcopy unavailable (tried twice); compared with Fri 13 Nov instead (2 sessions)", text)
        self.assertIn("Mon 16 Nov participant OI file unavailable (tried twice); compared with Fri 13 Nov instead (2 sessions)", text)
        self.assertRegex(text, r"- PCR \(OI\) [0-9.]+ \([+-][0-9.]+ vs 2026-11-13, 2 sessions\)")
        who = next(l for l in text.splitlines() if l.startswith("- Who holds index F&O"))
        self.assertIn("Tue 17 Nov vs Fri 13 Nov", who)
        self.assertIn("net short 65,123 (down 5,000 since Fri 13 Nov (2 sessions))", who)
        self.assertNotIn("on the day", who)
        code, full, _ = run(["chain", "NIFTY", "--no-vix"], CalendarFeeds(monday_fails=99, monday_participant_fails=99))
        self.assertIn("| Measure | 2026-11-13 | 2026-11-17 | Change (2 sessions) |", full)
        self.assertIn("Δ vs 2026-11-13, 2 sessions", full)

    def test_a_transient_failure_is_retried_and_compares_with_the_previous_session(self):
        feeds = CalendarFeeds(monday_fails=1)
        dod = fno.compare_previous(eod(), "NIFTY", fetcher=feeds, holidays=CALENDAR)
        self.assertEqual(bhav_days(feeds), ["20261116", "20261116"])
        self.assertEqual((dod["previous_date"], dod["sessions"], dod["gap_note"]), (MONDAY, 1, None))
        po = fno.fetch_participant_oi(TRADE, fetcher=CalendarFeeds(monday_participant_fails=1), holidays=CALENDAR)
        self.assertEqual((po["previous_date"], po["sessions"], po["gap_note"]), (MONDAY, 1, None))
        self.assertIn("(down 2,000 on the day)", fno.participant_summary_line(po))
        code, text, _ = run(["chain", "NIFTY", "--summary", "--no-vix"], CalendarFeeds(monday_fails=1, monday_participant_fails=1))
        self.assertRegex(text, r"- PCR \(OI\) [0-9.]+ \([+-][0-9.]+ vs 2026-11-16\)")
        self.assertNotIn("unavailable (tried twice)", text)

    def test_a_listed_holiday_is_skipped_without_a_request_or_a_gap(self):
        feeds = CalendarFeeds()
        dod = fno.compare_previous(eod(), "NIFTY", fetcher=feeds, holidays=HOLIDAYS)  # 16 Nov listed as a holiday
        self.assertEqual(bhav_days(feeds), ["20261113"])
        self.assertEqual((dod["previous_date"], dod["sessions"], dod["gap_note"]), (PREV, 1, None))
        po = fno.fetch_participant_oi(TRADE, fetcher=feeds, holidays=HOLIDAYS)
        self.assertEqual((po["previous_date"], po["sessions"], po["gap_note"]), (PREV, 1, None))
        self.assertIn("on the day", fno.participant_summary_line(po))

    def test_an_expiry_inside_the_window_is_flagged(self):
        code, text, _ = run(["chain", "NIFTY", "--summary", "--no-vix"], CalendarFeeds())
        self.assertIn("NIFTY options expired on Tue 17 Nov, inside the Mon 16 Nov–Tue 17 Nov comparison window: much of the option "
                      "OI change is expiring contracts closing or rolling, not fresh positioning", text)
        code, text, _ = run(["chain", "NIFTY", "--summary", "--no-vix"], CalendarFeeds(expiring_oi=0))
        self.assertNotIn("expired on", text)     # nothing left open in it, so nothing to close or roll
        self.assertEqual(fno.expiry_caveat({"BANKNIFTY": ["2026-11-23"]}, MONDAY, TRADE), None)


# ---------------- 2 and 5. holdings: retry, missing prices, plain tax wording ----------------

class FlakyFeed(Feed):
    """The test_portfolio_valuation feed, with the first `fails` requests for each listed symbol failing."""

    def __init__(self, flaky, fails=1):
        super().__init__()
        self.flaky, self.fails, self.seen = set(flaky), fails, Counter()

    def __call__(self, url):
        name = url.split("/chart/")[1].split("?")[0] if "/chart/" in url else ""
        sym = name.replace("%5E", "^")[:-3] if name.endswith(".NS") else name
        if sym in self.flaky:
            self.seen[sym] += 1
            if self.seen[sym] <= self.fails:
                self.urls.append(url)
                raise Block("PUBLIC: source unavailable")
        return super().__call__(url)


def review(feed, holdings=QUESTION):
    pauses = []
    r = hr.review(holdings_doc(holdings), None, fetcher=feed, clock=lambda: HR_NOW, sleep=pauses.append)
    return r, pauses


class HoldingsPriceTests(unittest.TestCase):
    def test_a_failed_price_is_retried_once_at_the_end(self):
        r, pauses = review(FlakyFeed({"SBIN", "ITCHOTELS"}, fails=1))
        self.assertNotIn("SBIN", r["errors"])
        self.assertNotIn("ITCHOTELS", r["errors"])
        self.assertEqual(r["totals"]["unpriced_symbols"], [])
        self.assertIn(1.0, pauses)
        sbin = next(h for h in r["holdings"] if h["symbol"] == "SBIN")
        self.assertEqual(sbin["last_price"], 1110.0)

    def test_a_price_still_missing_makes_every_total_not_a_full_figure(self):
        r, _ = review(FlakyFeed({"SBIN"}, fails=99))
        self.assertIn("failed twice", r["errors"]["SBIN"])
        self.assertEqual(r["totals"]["unpriced_symbols"], ["SBIN"])
        self.assertEqual(r["tax"]["hypothetical_sell_all"]["unpriced_symbols"], ["SBIN"])
        self.assertEqual(r["returns"]["unpriced"], ["SBIN"])
        brief = hr.summary(r)
        lines = brief.splitlines()
        value = next(l for l in lines if l.startswith("- **Value**"))
        ret = next(l for l in lines if l.startswith("- **Return"))
        tax = next(l for l in lines if l.startswith("- **If you sold everything today**"))
        for line in (value, ret, tax):
            self.assertIn("excludes SBIN (price unavailable), not a full figure", line)
        self.assertIn("(3 of 4 holdings priced)", value)
        md = hr.markdown(r)
        self.assertIn("⚠ Excludes SBIN (price unavailable) — not a full figure", md)
        self.assertIn("— excludes SBIN (price unavailable), not a full figure |", md)
        # Nothing missing: no such words.
        self.assertNotIn("not a full figure", hr.summary(review(Feed())[0]))

    def test_midpoint_and_long_term_loss_in_plain_words(self):
        r, _ = review(Feed())
        brief = hr.summary(r)
        self.assertIn("at ITC ~₹420 in Jun 2023 (Yahoo understates it before a demerger) and SBIN ~₹961 in Oct 2026: P&L", brief)
        self.assertNotIn("at the middle of that range", brief)
        loss, _ = review(Feed(), [{"symbol": "INFY", "quantity": 15, "average_price": 1500, "bought": "2023-01"}])
        h = loss["tax"]["hypothetical_sell_all"]
        self.assertEqual(h["detail"]["unused_long_term_loss"], 15 * (1500 - 1120))
        self.assertIn("  - Net long-term loss left over: ₹5,700 — it cannot reduce short-term gains, but can be carried forward "
                      "against long-term gains for up to 8 years", hr.summary(loss))


# ---------------- 3 and 7. 'now' from NSE's intraday snapshot ----------------

def offset_snapshot(created="202611180945", offset=60.0):
    """An intraday SPAN file whose options and future are priced with NIFTY at SNAP_LEVEL but whose index level reads
    SNAP_LEVEL + offset — as in nsccl.20261118.i02, where the two are not taken at the same instant."""
    at = datetime.strptime(created, "%Y%m%d%H%M").replace(tzinfo=IST)
    years = fno.years_to_expiry(EXPIRY, at)
    forward = SNAP_LEVEL * math.exp(RATE * years)
    pe = EXPIRY.replace("-", "")
    options = [(pe, kind[0], k, round(round(fno.black76(kind, forward, k, years, SNAP_VOL, RATE) / 0.05) * 0.05, 2), [0.0] * 16)
               for kind in ("CE", "PE") for k in (24500.0, 24750.0, 25000.0, 25250.0, 25500.0)]
    span = fno_margin.parse_span(span_zip(span_xml("NIFTY", SNAP_LEVEL + offset, futures=[(pe, forward, [0.0] * 16)], options=options,
                                                   business=created[:8], created=created)), "NIFTY")
    span.update(status="ok", url="test", file_label=f"{created[:4]}-{created[4:6]}-{created[6:8]} intraday file i2")
    return span


class SnapshotLevelTests(unittest.TestCase):
    def test_moves_to_live_start_from_the_level_the_option_prices_imply(self):
        r = eod()
        snap = fno.snapshot_report(offset_snapshot(), r, now=LIVE_AT)
        self.assertEqual(snap["_snapshot"]["level"], SNAP_LEVEL + 60)
        self.assertAlmostEqual(snap["_snapshot"]["implied_level"], SNAP_LEVEL, delta=1.5)
        now = fno.reprice_report(snap, {"level": LIVE_LEVEL, "at": LIVE_AT.isoformat()}, close_report=r)
        t1 = fno.years_to_expiry(EXPIRY, LIVE_AT)
        f1 = LIVE_LEVEL * (r["forward"] / r["spot"]) ** (t1 / r["_years"])     # the close's carry, from the live level
        iv = next(x for x in snap["_rows"] if x["strike"] == 25250)["ce"]["iv"]
        self.assertAlmostEqual(now["_books"][0][25250]["close"], fno.black76("CE", f1, 25250, t1, iv, RATE), delta=0.06)
        naive = fno.black76("CE", f1 * SNAP_LEVEL / (SNAP_LEVEL + 60), 25250, t1, iv, RATE)   # moved from the file's level instead
        self.assertGreater(abs(now["_books"][0][25250]["close"] - naive), 10)
        self.assertIn("NIFTY 25,010.00 in the file, but its option and futures prices imply ≈24,9", now["_placed_now"]["basis"])
        # In step: no offset, no remark.
        same = fno.snapshot_report(offset_snapshot(offset=0.0), r, now=LIVE_AT)
        self.assertEqual(same["_snapshot"]["implied_level"], SNAP_LEVEL)

    def test_chain_shows_now_estimate_and_leads_with_the_next_session(self):
        feeds = SpanFeeds(snapshot="202611180945")
        code, text, err = run(["chain", "NIFTY", "--summary", "--no-vix"], feeds)
        self.assertEqual(code, 0, err)
        now_line = next(l for l in text.splitlines() if l.startswith("- Now (estimate"))
        self.assertRegex(now_line, r"^- Now \(estimate, not live quotes\): ATM 2[45],[0-9]{3} straddle [0-9.,]+ \(CE [0-9.,]+ \+ PE [0-9.,]+\) · "
                                   r"ATM IV 18\.[0-9]% — from NSE's 09:45 IST intraday option prices, moved "
                                   r"to live NIFTY 24,900\.00 \(09:55 IST\)$")
        lines = text.splitlines()
        at = lambda prefix: next(i for i, l in enumerate(lines) if l.startswith(prefix))
        self.assertLess(at("- Now (estimate"), at("- 1-sigma to the Thu 19 Nov close (tomorrow), from live 24,900.00"))
        self.assertLess(at("- PCR (OI)"), at("**Next session — Thu 19 Nov (tomorrow)**"))   # round 5: its own block
        self.assertLess(at("**Next session"), at("- 1-sigma to the Thu 19 Nov close"))
        self.assertNotIn("(in progress)", text)
        self.assertIn("at the close · now ≈ 18.", text)
        code, raw, _ = run(["chain", "NIFTY", "--summary", "--no-vix", "--format", "json"], SpanFeeds(snapshot="202611180945"))
        n = json.loads(raw)["now_estimate"]
        self.assertAlmostEqual(n["straddle"], n["ce"] + n["pe"], places=6)
        self.assertAlmostEqual(n["atm_iv"], SNAP_VOL, delta=0.003)
        self.assertEqual((n["source"], n["snapshot_at"][11:16]), ("nse_intraday_snapshot", "09:45"))
        code, full, _ = run(["chain", "NIFTY", "--no-vix"], SpanFeeds(snapshot="202611180945"))
        self.assertIn("| Now (estimate, not live quotes) | ATM ", full)
        quiet = SpanFeeds(snapshot="202611180945")
        code, text, _ = run(["chain", "NIFTY", "--summary", "--no-vix", "--no-snapshot"], quiet)
        self.assertNotIn("Now (estimate", text)
        self.assertFalse(any("nsccl/span" in u for u in quiet.urls))
        # Evening: no 'now' line and no SPAN download.
        evening = SpanFeeds(snapshot="202611180945")
        code, text, _ = run(["chain", "NIFTY", "--summary", "--no-vix"], evening, clock=datetime(2026, 11, 17, 20, 0, tzinfo=IST))
        self.assertNotIn("Now (estimate", text)
        self.assertFalse(any("nsccl/span" in u for u in evening.urls))


# ---------------- 4. margin on the begin-of-day file, with a band ----------------

# NSE Clearing nsccl.20261117.s.spn (identical arrays and prices in nsccl.20261118.i1.spn): the reviewer's BANKNIFTY 23 Nov
# iron condor (grid row 3) and a naked short NIFTY 25800 CE. Zerodha's basket calculator on 18 Nov 2026 ~12:00 IST:
# SPAN ₹14,235.50 (net option value ₹5,299.50) and ₹1,26,338; the 11:03 intraday file i02 gives ₹14,314 and ₹1,15,271.
BANKNIFTY_17NOV = [
    ("20261123", "C", 63200, 192.7, [-4.84, 192.7, -1017.26, -826.38, 192.7, 192.7, -2709.08, -2683.7, 192.7, 192.7, -4614.73, -4613.73, 192.7, 192.7, -3641.8, 105.03]),
    ("20261123", "P", 61400, 197.95, [-18.27, 178.79, 197.95, 197.95, -995.23, -813.23, 197.95, 197.95, -2691.82, -2674.08, 197.95, 197.95, -4604.65, -4604.34, 93.12, -3638.51]),
    ("20261123", "C", 63700, 105.1, [-4.9, 105.1, -798.55, -555.01, 105.1, 105.1, -2357.94, -2307.1, 105.1, 105.1, -4237.98, -4235.25, 105.1, 105.1, -3509.33, 62.63]),
    ("20261123", "P", 60900, 108.9, [-13.47, 108.9, 108.9, 108.9, -765.5, -533.48, 108.9, 108.9, -2327.08, -2289.65, 108.9, 108.9, -4219.82, -4218.87, 53.17, -3503.6]),
]
NIFTY_17NOV = [("20261123", "C", 25800, 51.45, [-0.08, 51.45, -396.7, -338.77, 51.45, 51.45, -1108.93, -1105.19, 51.45, 51.45, -1892.22, -1892.17, 51.45, 51.45, -1488.62, 26.71])]


def real_17nov(symbol, close, scan, options):
    span = fno_margin.parse_span(span_zip(span_xml(symbol, close, options=options, scan=scan, business="20261117")), symbol)
    span.update(status="ok", url="test", file_label="2026-11-17 end-of-day file")
    return span


class BeginOfDayMarginTests(unittest.TestCase):
    def test_reproduces_the_broker_calculator_on_the_begin_of_day_file(self):
        condor = real_17nov("BANKNIFTY", 62273.25, 5791.41, BANKNIFTY_17NOV)
        legs = [{"type": "CE", "strike": 63200, "units": -30}, {"type": "PE", "strike": 61400, "units": -30},
                {"type": "CE", "strike": 63700, "units": 30}, {"type": "PE", "strike": 60900, "units": 30}]
        m = fno_margin.estimate(condor, legs, expiry="2026-11-23", trade_day="2026-11-18", index=True)
        self.assertAlmostEqual(m["net_option_value"], -5299.50, places=2)
        self.assertAlmostEqual(m["span"], 14235.50, delta=0.5)                   # broker: ₹14,235.50
        self.assertAlmostEqual(m["exposure"], 0.02 * 62273.25 * 60, places=2)    # NSE rule: each short leg, 2% × close × units
        naked = real_17nov("NIFTY", 25387.40, 2361.03, NIFTY_17NOV)
        m = fno_margin.estimate(naked, [{"type": "CE", "strike": 25800, "units": -65}], expiry="2026-11-23", trade_day="2026-11-18", index=True)
        self.assertAlmostEqual(m["span"], 126338.0, delta=1.0)                     # broker: ₹1,26,338
        self.assertAlmostEqual(m["exposure"], 0.02 * 25387.40 * 65, places=2)
        self.assertEqual(fno_margin.margin_text(m), f"indicative ₹{fno_margin.inr(m['total'])} (SPAN ₹1,26,339 + exposure ₹33,004; "
                                                    "brokers' figures typically within ±10%; your broker's basket margin is final)")

    def test_fetch_spans_uses_begin_of_day_for_margin_and_latest_for_prices(self):
        feeds = SpanFeeds(snapshot="202611181103")
        latest, margin = fno_margin.fetch_spans("NIFTY", TRADE, date(2026, 11, 18), fetcher=feeds)
        self.assertEqual((latest["file_label"], margin["file_label"]), ("2026-11-18 intraday file i2", "2026-11-17 end-of-day file"))
        self.assertIs(margin["latest"], latest)
        self.assertTrue(fno_margin.is_intraday(latest))
        self.assertFalse(fno_margin.is_intraday(margin))
        self.assertFalse(fno_margin.is_intraday({"file_label": "2026-11-18 intraday file i1"}))
        self.assertEqual([u for u, _ in fno_margin.begin_of_day_candidates(TRADE, date(2026, 11, 18))],   # round 8: today's i1 first
                         ["https://nsearchives.nseindia.com/archives/nsccl/span/nsccl.20261118.i1.zip",
                          "https://nsearchives.nseindia.com/archives/nsccl/span/nsccl.20261117.s.zip"])
        # Evening of the data date: one file serves both.
        latest, margin = fno_margin.fetch_spans("NIFTY", TRADE, date(2026, 11, 17), fetcher=SpanFeeds())
        self.assertIs(latest, margin)

    def test_payoff_margin_comes_from_the_begin_of_day_file_while_prices_come_from_the_snapshot(self):
        _, options, futures = bhav_span()
        with tempfile.TemporaryDirectory() as d:
            argv = ["payoff", "NIFTY", "--leg", "SELL:CE:25250:1", "--leg", "BUY:CE:25500:1", "--format", "json", "--charges", card_file(d)]
            code, raw, err = run(argv, SpanFeeds(snapshot="202611181103"), clock=datetime(2026, 11, 18, 11, 30, tzinfo=IST))
            self.assertEqual(code, 0, err)
            p = json.loads(raw)["payoff"]
            span, exposure = independent_margin([("CE", 25250, -LOT), ("CE", 25500, LOT)], options, futures)
            self.assertAlmostEqual(p["margin"]["span"], span, places=2)
            self.assertAlmostEqual(p["margin"]["exposure"], exposure, places=2)
            self.assertEqual(p["placed_now"]["source"], "nse_intraday_snapshot")
            self.assertEqual(p["margin"]["intraday"]["file"], "nsccl.20261118.i02.spn")
            code, text, _ = run(["payoff", "NIFTY", "--leg", "SELL:CE:25250:1", "--leg", "BUY:CE:25500:1", "--charges", card_file(d)],
                                SpanFeeds(snapshot="202611181103"), clock=datetime(2026, 11, 18, 11, 30, tzinfo=IST))
            self.assertIn("brokers have charged hedged positions less, see the range; your broker's basket margin is final", text)  # round 8
            self.assertIn("NSE's 11:03 IST intraday file (nsccl.20261118.i02.spn) re-computes SPAN at ₹", text)
            self.assertIn("| Margin blocked (SPAN + exposure) | indicative ₹", text)

    def test_begin_of_day_file_missing_falls_back_to_the_intraday_file_and_says_so(self):
        feeds = SpanFeeds(span=False, snapshot="202611181103")
        latest, margin = fno_margin.fetch_spans("NIFTY", TRADE, date(2026, 11, 18), fetcher=feeds)
        self.assertEqual(margin["file_label"], "2026-11-18 intraday file i2")
        self.assertIn("begin-of-day SPAN file was unavailable", margin["bod_note"])


# ---------------- 6. grid summary, monthly-only note, one ±SD basis ----------------

class GridSummaryTests(unittest.TestCase):
    def test_compact_grid_summary(self):
        evening = datetime(2026, 11, 17, 20, 0, tzinfo=IST)
        with tempfile.TemporaryDirectory() as d:
            argv = ["grid", "NIFTY", "--structure", "condor", "--width", "250", "--charges", card_file(d)]
            code, raw, err = run(argv + ["--format", "json"], SpanFeeds(), clock=evening)
            self.assertEqual(code, 0, err)
            rows = json.loads(raw)["grid"]["rows"]
            code, text, err = run(argv + ["--summary"], SpanFeeds(), clock=evening)
            self.assertEqual(code, 0, err)
        table = [l for l in text.splitlines() if l.startswith("| ") and not l.startswith("| ---")]
        # Round 4: breakevens and P&L at -1 / +1 SD added (8 columns).
        self.assertEqual(table[0], "| # | Short / long strikes | Credit ₹ | Max loss ₹ | Breakevens | Profit chance | P&L at −1 / +1 SD ₹ | Margin ₹ |")
        self.assertEqual(len(table), 1 + len(rows))
        for i, (line, x) in enumerate(zip(table[1:], rows), 1):
            cells = [c.strip() for c in line.strip("|").split("|")]
            self.assertEqual(len(cells), 8)
            self.assertEqual(cells[0], str(i))
            self.assertEqual(cells[2], fno.fmt(x["credit_after_costs"], 0))
            self.assertEqual(cells[3], fno.fmt(-x["max_loss_after_costs"], 0))
            self.assertEqual(cells[5], fno.pct(x["pop_after_costs"]))
            self.assertEqual(cells[7], fno_margin.inr(x["margin"]["total"]))
        self.assertIn("Nifty has no weekly options in this data (one expiry a month); using the 23 Nov monthly.", text)
        self.assertIn("At the Tue 17 Nov close (end of day)", text)
        self.assertLess(len(text.splitlines()), 20)

    def test_monthly_only_wording(self):
        self.assertEqual(fno.monthly_only_note({"has_weeklies": False, "underlying": "BANKNIFTY", "expiry": "2026-11-23"}),
                         "Bank Nifty has had no weekly options since Nov 2024; using the 23 Nov monthly.")
        self.assertIsNone(fno.monthly_only_note({"has_weeklies": True, "underlying": "NIFTY", "expiry": "2026-11-23"}))
        r = fno.analyse_chain(fno.parse_bhavcopy(bhavcopy(), "NIFTY"), "BANKNIFTY", kind="weekly", rate=RATE)
        self.assertTrue(r["selection_note"].startswith("Bank Nifty has had no weekly options since Nov 2024; using the 23 Nov monthly"))
        with tempfile.TemporaryDirectory() as d:
            code, text, _ = run(["grid", "NIFTY", "--structure", "condor", "--width", "250", "--row", "1", "--summary",
                                 "--charges", card_file(d)], SpanFeeds(), clock=datetime(2026, 11, 17, 20, 0, tzinfo=IST))
        self.assertEqual(text.splitlines()[1], "Nifty has no weekly options in this data (one expiry a month); using the 23 Nov monthly.")

    def test_stress_sd_levels_are_the_68_percent_range_ends_now_and_at_the_close(self):
        with tempfile.TemporaryDirectory() as d:
            code, raw, err = run(["payoff", "NIFTY", "--leg", "SELL:CE:25250:1", "--format", "json", "--charges", card_file(d)],
                                 SpanFeeds(snapshot="202611180945"))
        self.assertEqual(code, 0, err)
        p = json.loads(raw)["payoff"]
        for block, rng in ((p["scenarios"], p["probabilities"]["range_1sd"]), (p["placed_now"]["scenarios"], p["placed_now"]["range_1sd"])):
            levels = {x["sd"]: x["level"] for x in block["expiry"]}
            self.assertAlmostEqual(levels[-1], rng[0], places=6)
            self.assertAlmostEqual(levels[1], rng[1], places=6)
            self.assertIn("so ±1 SD are the ends of the 68% range", block["basis"])


if __name__ == "__main__":
    unittest.main()
