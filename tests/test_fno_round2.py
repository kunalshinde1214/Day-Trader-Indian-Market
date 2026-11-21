"""Offline contracts for the F&O round-2 fixes: dated session ranges, live-level labels, NSE holiday names,
participant-wise OI, --weekly fallback and UTF-8 output, strike grids, costs/skew/EV/stress in payoffs, and
strikes given without an underlying. Never contacts the network."""
import contextlib
from datetime import date, datetime, time as dtime, timedelta
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
from core import Block, IST
import fno
from test_fno import (EXPIRY, FakeFeeds, LOT, MONTHLY_ONLY, NOW, PREV, RATE, SPOT, TRADE, VOL, bhavcopy, option_rows, row)

CARD = {"broker": "test", "source": "test card", "verified_on": "2026-10-31", "status": "test", "brokerage_rate": 0.0003,
        "brokerage_cap": 20, "option_brokerage": 20, "gst_rate": 0.18, "sebi_rate": 0.000001,
        "segments": {"NFO:future": {"stt_sell": 0.0005, "exchange_rate": 0.0000183, "stamp_buy": 0.00002},
                     "NFO:option": {"stt_sell": 0.0015, "exchange_rate": 0.0003553, "stamp_buy": 0.00003}}}
HOLIDAYS_JSON = json.dumps({"CM": [], "FO": [
    {"tradingDate": "16-Nov-2026", "weekDay": "Monday", "description": "Test Holiday"},
    {"tradingDate": "24-Nov-2026", "weekDay": "Tuesday", "description": "Prakash Gurpurb Sri Guru Nanak Dev"},
    {"tradingDate": "25-Dec-2026", "weekDay": "Friday", "description": "Christmas"}]}).encode()
HOLIDAYS = {"days": fno.parse_holidays(HOLIDAYS_JSON), "years": [2026], "source": "test"}
LIVE_AT = datetime(2026, 11, 18, 9, 55, tzinfo=IST)
LIVE_LEVEL = 24900.0

PARTICIPANT_17 = """\"\"Participant wise Open Interest (no. of contracts) in Equity Derivatives as on Nov 17, 2026\"\",,,,,,,,,,,,,,
Client Type,Future Index Long,Future Index Short,Future Stock Long,Future Stock Short       ,Option Index Call Long,Option Index Put Long,Option Index Call Short,Option Index Put Short,Option Stock Call Long,Option Stock Put Long,Option Stock Call Short,Option Stock Put Short,Total Long Contracts      ,Total Short Contracts
Client,170907,115613,3180794,391846,2383850,2546860,2360166,2891975,2253990,1212779,1277236,1226403,11749181,8263239
DII,53126,30387,373203,4296063,5214,114354,350,1200,17855,42968,477319,35480,606720,4840799
FII,65568,130691,3629602,2645357,657681,1019332,774511,649719,347966,437009,468149,356677,6157158,5025104
Pro,28628,41538,807203,657536,829209,780407,740927,918059,1431984,1189236,1829091,1263432,5066667,5450583
TOTAL,318229,318229,7990802,7990802,3875954,4460953,3875954,4460953,4051795,2881992,4051795,2881992,23579725,23579725
"""


def participant_csv(day_text, fii_long_delta=0):
    """The 17 Nov file shifted: FII index futures long changes by `delta` and the Client long offsets it (totals balance)."""
    text = PARTICIPANT_17.replace("Nov 17, 2026", day_text)
    text = text.replace("FII,65568,", f"FII,{65568 - fii_long_delta},").replace("Client,170907,", f"Client,{170907 + fii_long_delta},")
    return text.encode()


def yahoo_quote(symbol="^NSEI", price=LIVE_LEVEL, at=LIVE_AT, previous=SPOT):
    return json.dumps({"chart": {"error": None, "result": [{"meta": {
        "symbol": symbol, "currency": "INR", "exchangeTimezoneName": "Asia/Kolkata", "regularMarketPrice": price,
        "regularMarketTime": int(at.timestamp()), "chartPreviousClose": previous}}]}}).encode()


class LiveFeeds(FakeFeeds):
    """FakeFeeds plus the NSE holiday master, a Yahoo ^NSEI quote, participant OI files and a multi-expiry bhavcopy."""

    def __init__(self, *, quote=True, participants=True, **kw):
        super().__init__(**kw)
        self.quote, self.participants = quote, participants

    def __call__(self, url):
        if url == fno.NSE_HOLIDAYS:
            self.urls.append(url)
            return HOLIDAYS_JSON
        if "query1.finance.yahoo.com" in url and "%5ENSEI" in url:
            self.urls.append(url)
            if not self.quote:
                raise Block("PUBLIC: source unavailable")
            return yahoo_quote()
        if "fao_participant_oi_" in url:
            self.urls.append(url)
            if self.participants and "17112026" in url:
                return participant_csv("Nov 17, 2026")
            if self.participants and "13112026" in url:
                return participant_csv("Nov 13, 2026", fii_long_delta=5000)
            raise Block("PUBLIC: source unavailable")
        if TRADE.replace("-", "") in url and "BhavCopy" in url:
            self.urls.append(url)
            return bhavcopy(extra=option_rows(MONTHLY_ONLY[1:]))
        return super().__call__(url)


def run(argv, fetcher, clock=NOW):
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        code = fno.main(argv, fetcher=fetcher, clock=lambda: clock)
    return code, out.getvalue(), err.getvalue()


def card_file(d):
    path = Path(d) / "card.json"
    path.write_text(json.dumps(CARD), encoding="utf-8")
    return str(path)


def plain_report(**kw):
    return fno.analyse_chain(fno.parse_bhavcopy(bhavcopy(**kw), "NIFTY"), "NIFTY", rate=RATE)


# ---------------- 1. time-of-day labels ----------------

class SessionLabelTests(unittest.TestCase):
    def ctx(self, now, data=TRADE, holidays=HOLIDAYS):
        return fno.session_context(data, now, holidays)

    def test_market_open_gives_today_in_progress_and_tomorrow_from_live(self):
        r = plain_report()
        ctx = self.ctx(NOW)
        self.assertEqual((ctx["phase"], ctx["next_session"], ctx["next_unstarted_session"]), ("open", "2026-11-18", "2026-11-19"))
        self.assertAlmostEqual(ctx["data_age_hours"], 18.5)
        live = {"level": LIVE_LEVEL, "at": LIVE_AT.isoformat(), "source": "test"}
        today, tomorrow = fno.session_ranges(r, ctx, live, HOLIDAYS)
        iv = r["atm_iv"]
        self.assertEqual((today["session"], today["status"], today["sessions"]), ("2026-11-18", "in progress", 1))
        self.assertAlmostEqual(today["points"], SPOT * iv / math.sqrt(252), places=9)
        self.assertEqual(today["centre"], SPOT)
        rest = (15.5 - (9 + 55 / 60)) / 6.25  # 09:55 -> 15:30 out of a 6¼-hour session
        self.assertAlmostEqual(tomorrow["sessions"], rest + 1, places=9)
        self.assertEqual((tomorrow["session"], tomorrow["status"], tomorrow["centre"]), ("2026-11-19", "tomorrow", LIVE_LEVEL))
        self.assertAlmostEqual(tomorrow["points"], LIVE_LEVEL * iv * math.sqrt((rest + 1) / 252), places=9)
        self.assertIn("1-sigma for the Wed 18 Nov session (in progress), from the Tue 17 Nov close", fno.range_text(today))
        self.assertIn("to the Thu 19 Nov close (tomorrow), from live 24,900.00 at 09:55 IST", fno.range_text(tomorrow))
        self.assertIn("(covers the rest of today, about 5h 35m of trading, plus tomorrow)", fno.range_text(tomorrow))
        self.assertNotIn("(covers", fno.range_text(today))  # one session: the label already names it
        # Without a live level: two sessions from the close.
        _, fallback = fno.session_ranges(r, ctx, None, HOLIDAYS)
        self.assertEqual((fallback["sessions"], fallback["centre"]), (2, SPOT))
        self.assertAlmostEqual(fallback["points"], SPOT * iv * math.sqrt(2 / 252), places=9)
        self.assertIn("(no live level)", fno.range_text(fallback))

    def test_evening_pre_open_and_holidays(self):
        r = plain_report()
        evening = self.ctx(datetime(2026, 11, 17, 20, 0, tzinfo=IST))
        (only,) = fno.session_ranges(r, evening, None, HOLIDAYS)
        self.assertEqual((only["session"], only["status"]), ("2026-11-18", "tomorrow"))
        self.assertIsNone(fno.data_age_line(evening))
        pre = self.ctx(datetime(2026, 11, 18, 8, 30, tzinfo=IST))
        (only,) = fno.session_ranges(r, pre, None, HOLIDAYS)
        self.assertEqual((only["session"], only["status"]), ("2026-11-18", "today, opens 09:15"))
        # Monday 23 Nov evening: Tuesday 24 Nov is Guru Nanak Jayanti, so the next session is Wednesday.
        holiday = self.ctx(datetime(2026, 11, 23, 19, 0, tzinfo=IST), data="2026-11-23")
        self.assertEqual(holiday["next_session"], "2026-11-25")
        self.assertEqual(fno.sessions_between(date(2026, 11, 13), date(2026, 11, 18), HOLIDAYS), 2)  # 16th is a (test) holiday
        self.assertEqual(fno.sessions_between(date(2026, 11, 13), date(2026, 11, 18), None), 3)
        on_holiday = self.ctx(datetime(2026, 11, 24, 11, 0, tzinfo=IST), data="2026-11-23")
        self.assertEqual((on_holiday["phase"], on_holiday["today_holiday"]), ("holiday or weekend", "Prakash Gurpurb Sri Guru Nanak Dev"))
        self.assertIsNone(fno.data_age_line(on_holiday))
        # After today's close without today's bhavcopy: the close centres one session to tomorrow; today's (ended) session
        # gets no range (round 6).
        after = self.ctx(datetime(2026, 11, 18, 17, 0, tzinfo=IST))
        live = {"level": LIVE_LEVEL, "at": datetime(2026, 11, 18, 15, 30, tzinfo=IST).isoformat(), "source": "test"}
        (second,) = fno.session_ranges(r, after, live, HOLIDAYS)
        self.assertEqual((second["session"], second["sessions"]), ("2026-11-19", 1))

    def test_data_age_line_and_cli_live_labels(self):
        with tempfile.TemporaryDirectory() as d:
            code, text, _ = run(["chain", "NIFTY", "--summary", "--no-vix"], LiveFeeds())
            self.assertEqual(code, 0)
            lines = text.splitlines()
            self.assertEqual(lines[0], "**END-OF-DAY data from Tue 17 Nov close, 18 hours old; the live chain needs your broker connector "
                                       "(--input).** NSE is open: today's (Wed 18 Nov) session is in progress.")
            self.assertIn("- Spot 25,000.00 (Tue 17 Nov close) · live 24,900.00 at 09:55 IST (-0.40%, Yahoo, unofficial)", text)
            self.assertIn("1-sigma to the Thu 19 Nov close (tomorrow), from live 24,900.00 at 09:55 IST", text)
            code, raw, _ = run(["chain", "NIFTY", "--summary", "--no-vix", "--format", "json"], LiveFeeds())
            data = json.loads(raw)
            self.assertEqual([m["session"] for m in data["session_ranges"]], ["2026-11-18", "2026-11-19"])
            self.assertEqual(data["live"]["level"], LIVE_LEVEL)
            # No live quote: the tomorrow range falls back to two sessions from the close; --no-live skips both lookups.
            code, text, _ = run(["chain", "NIFTY", "--summary", "--no-vix"], LiveFeeds(quote=False))
            self.assertIn("from the Tue 17 Nov close (no live level)", text)
            feeds = LiveFeeds()
            run(["chain", "NIFTY", "--summary", "--no-vix", "--no-live"], feeds)
            self.assertFalse(any("yahoo" in u or "holiday" in u for u in feeds.urls))
            # An input chain during market hours is live: no END-OF-DAY line.
            rows = [{"type": k, "strike": s, "last": round(fno.black76(k, SPOT * 1.001, s, 5 / 365, VOL, RATE), 2), "oi": 650, "volume": 10}
                    for k in ("CE", "PE") for s in (24750, 25000, 25250)]
            chain = Path(d) / "c.json"
            chain.write_text(json.dumps({"schema": "fno_chain_v1", "underlying": "NIFTY", "as_of": "2026-11-18T10:00:00+05:30",
                                         "spot": SPOT, "lot_size": LOT, "expiry": EXPIRY, "rows": rows}), encoding="utf-8")
            code, text, _ = run(["chain", "NIFTY", "--input", str(chain), "--summary", "--no-vix", "--no-participants"], LiveFeeds())
            self.assertEqual(code, 0)
            self.assertNotIn("END-OF-DAY", text)

    def test_payoff_labels_t0_and_remarks_at_live(self):
        with tempfile.TemporaryDirectory() as d:
            card = card_file(d)
            code, text, _ = run(["payoff", "NIFTY", "--leg", "SELL:CE:25250:1", "--summary", "--held", "--charges", card], LiveFeeds())
            self.assertEqual(code, 0)
            self.assertNotIn("₹unlimited", text)
            self.assertIn("| Maximum loss at expiry | unlimited (grows with the move) | unlimited |", text)
            self.assertIn("- Already in this trade from the Tue 17 Nov close? Model P&L now +₹", text)
            code, raw, _ = run(["payoff", "NIFTY", "--leg", "SELL:CE:25250:1", "--format", "json", "--charges", card], LiveFeeds())
            data = json.loads(raw)
            rep, leg = data["report"], data["payoff"]["legs"][0]
            years = fno.years_to_expiry(EXPIRY, LIVE_AT)
            expected = -LOT * (fno.black76("CE", rep["forward"] * LIVE_LEVEL / SPOT, 25250, years, leg["iv"], RATE) - leg["price"])
            self.assertAlmostEqual(data["payoff"]["live_mark"]["pnl"], expected, places=6)
            # No live quote: the T+0 line names the close it is priced at.
            code, text, _ = run(["payoff", "NIFTY", "--leg", "SELL:CE:25250:1", "--summary", "--charges", card], LiveFeeds(quote=False))
            self.assertIn("- Today (T+0) P&L at the Tue 17 Nov close 25,000.00: ₹0 (entry is priced at that close)", text)
            self.assertNotIn("at current spot", text)


# ---------------- 2. holiday names ----------------

class HolidayNameTests(unittest.TestCase):
    def test_moved_expiry_names_the_nse_holiday_or_falls_back(self):
        contracts = fno.parse_bhavcopy(bhavcopy(extra=option_rows(MONTHLY_ONLY[1:])), "NIFTY")
        named = fno.analyse_chain(contracts, "NIFTY", rate=RATE, holidays=HOLIDAYS)
        self.assertEqual(named["expiry_holiday"], "Prakash Gurpurb Sri Guru Nanak Dev")
        self.assertIn("moved from Tuesday 2026-11-24, NSE trading holiday (Prakash Gurpurb Sri Guru Nanak Dev)", fno.chain_markdown(named))
        self.assertNotIn(fno.MOVED_NOTE, fno.chain_markdown(named))
        plain = fno.analyse_chain(contracts, "NIFTY", rate=RATE)  # holiday list unavailable
        self.assertIn("moved from Tuesday 2026-11-24, " + fno.MOVED_NOTE, fno.chain_markdown(plain))
        # 26 Jan 2027 is outside this year's list: keep the cautious wording for that expiry.
        jan = next(x for x in named["expiry_calendar"] if x["expiry"] == "2027-01-25")
        self.assertIsNone(jan.get("holiday"))
        self.assertIn(fno.MOVED_NOTE, fno.expiry_text(jan, "Tuesday"))

    def test_holiday_parse_and_cli_expiries(self):
        self.assertEqual(fno.parse_holidays(HOLIDAYS_JSON)[date(2026, 11, 24)], "Prakash Gurpurb Sri Guru Nanak Dev")
        with self.assertRaises(Block):
            fno.parse_holidays(b'{"CM": []}')
        self.assertIsNone(fno.fetch_holidays(lambda url: (_ for _ in ()).throw(Block("down"))))
        code, text, _ = run(["expiries", "NIFTY"], LiveFeeds())
        self.assertIn("- 2026-11-23 — monthly, Monday — moved from Tuesday 2026-11-24, NSE trading holiday (Prakash Gurpurb", text)
        code, text, _ = run(["payoff", "NIFTY", "--leg", "BUY:CE:25000:1", "--summary"], LiveFeeds())
        self.assertIn("(monthly, moved from 2026-11-24 — NSE trading holiday (Prakash Gurpurb Sri Guru Nanak Dev))", text)


# ---------------- 3. participant-wise OI ----------------

class ParticipantOiTests(unittest.TestCase):
    def test_parse_and_day_change(self):
        po = fno.parse_participant_oi(PARTICIPANT_17.encode(), "2026-11-17")
        fii = po["rows"]["FII"]
        self.assertEqual((fii["fut_long"], fii["fut_short"], fii["fut_net"]), (65568, 130691, 65568 - 130691))
        self.assertAlmostEqual(fii["fut_long_pct"], 65568 / (65568 + 130691))
        self.assertEqual((fii["call_net"], fii["put_net"]), (657681 - 774511, 1019332 - 649719))
        self.assertEqual(po["rows"]["Client"]["fut_net"], 170907 - 115613)
        with self.assertRaisesRegex(Block, "not 2026-11-18"):
            fno.parse_participant_oi(PARTICIPANT_17.encode(), "2026-11-18")
        with self.assertRaisesRegex(Block, "do not balance"):
            fno.parse_participant_oi(PARTICIPANT_17.replace("FII,65568,", "FII,95568,").encode())
        # NSE's files are sometimes a contract off; that is tolerated.
        self.assertEqual(fno.parse_participant_oi(PARTICIPANT_17.replace("FII,65568,", "FII,65569,").encode())["rows"]["FII"]["fut_long"], 65569)
        feeds = LiveFeeds()
        result = fno.fetch_participant_oi(TRADE, fetcher=feeds, holidays=HOLIDAYS)
        self.assertEqual((result["status"], result["date"], result["previous_date"]), ("ok", TRADE, PREV))  # 16th skipped as a holiday
        self.assertEqual(result["rows"]["FII"]["change"]["fut_long"], 5000)
        self.assertEqual(result["rows"]["FII"]["change"]["fut_net"], 5000)
        self.assertEqual(result["rows"]["Client"]["change"]["fut_net"], -5000)
        line = fno.participant_summary_line(result)
        self.assertIn("FIIs are 33% long in index futures, net short 65,123 (down 5,000 on the day)", line)
        self.assertIn("clients (retail and other non-institutional) net long 55,294 (down 5,000 on the day) in index futures", line)
        self.assertIn("FIIs in index options: net short 116,830 calls (unchanged on the day), net long 369,613 puts", line)
        self.assertNotIn("Δ", line)
        section = "\n".join(fno.participant_section(result))
        self.assertIn("| FII | 65,568 | 130,691 | 33% | -65,123 (+5,000) |", section)
        missing = fno.fetch_participant_oi("2026-11-20", fetcher=feeds, holidays=HOLIDAYS)
        self.assertEqual(missing["status"], "unavailable")

    def test_chain_reports_carry_participants(self):
        with mock.patch.object(fno, "FII_LONG_EXTREME", 0.5):  # summary shows the participant line only when notable
            code, text, _ = run(["chain", "NIFTY", "--summary", "--no-vix"], LiveFeeds())
        self.assertIn("- Who holds index F&O (NSE participant OI, all indices and expiries, Tue 17 Nov vs Fri 13 Nov; contracts): "
                      "FIIs are 33% long in index futures", text)
        code, text, _ = run(["chain", "NIFTY", "--no-vix"], LiveFeeds())
        self.assertIn("## Participant-wise OI (NSE, all index futures and options)", text)
        code, text, _ = run(["chain", "NIFTY", "--no-vix", "--summary"], LiveFeeds(participants=False))
        self.assertNotIn("Participant OI:", text)   # an unavailable participant file is full-report detail, not summary noise
        feeds = LiveFeeds()
        run(["chain", "NIFTY", "--no-vix", "--summary", "--no-participants"], feeds)
        self.assertFalse(any("participant" in u for u in feeds.urls))


# ---------------- 4. --weekly fallback and UTF-8 output ----------------

class WeeklyFallbackTests(unittest.TestCase):
    def test_weekly_without_weeklies_exits_zero_with_note_and_utf8(self):
        raw = io.BytesIO()
        stream = io.TextIOWrapper(raw, encoding="cp1252", errors="strict")
        err_raw = io.BytesIO()
        err_stream = io.TextIOWrapper(err_raw, encoding="cp1252", errors="strict")
        old = sys.stdout, sys.stderr
        sys.stdout, sys.stderr = stream, err_stream
        try:
            code = fno.main(["chain", "NIFTY", "--weekly", "--summary", "--no-vix"], fetcher=LiveFeeds(), clock=lambda: NOW)
            blocked = fno.main(["chain", "NOTASTOCK", "--no-vix"], fetcher=LiveFeeds(), clock=lambda: NOW)
            stream.flush()
            err_stream.flush()
        finally:
            sys.stdout, sys.stderr = old
        text = raw.getvalue().decode("utf-8")
        self.assertEqual((code, blocked), (0, 2))
        self.assertIn("**Note:** NIFTY has no weekly expiries — NSE lists one expiry a month", text)
        self.assertIn("— data 2026-11-17 close", text)  # the em dash survives a cp1252 console
        self.assertIn("BLOCKED - FNO: NOTASTOCK has no F&O contracts", err_raw.getvalue().decode("utf-8"))


# ---------------- 5. strike grid ----------------

STRIKES = [23500 + 250 * i for i in range(13)]
A, B, C = 0.15, -0.40, 2.0
FWD = SPOT * math.exp(RATE * fno.years_to_expiry(EXPIRY, datetime.combine(date.fromisoformat(TRADE), dtime(15, 30), IST)))
YRS = fno.years_to_expiry(EXPIRY, datetime.combine(date.fromisoformat(TRADE), dtime(15, 30), IST))


def true_smile(k):
    x = math.log(k / FWD)
    return A + B * x + C * x * x


def smile_report(smile=true_smile):
    tweaks = {(kind, k): {"ClsPric": round(fno.black76(kind, FWD, k, YRS, smile(k), RATE), 2), "TtlTradgVol": 100}
              for kind in ("CE", "PE") for k in STRIKES}
    oi = {k: 50 for k in STRIKES}
    return fno.analyse_chain(fno.parse_bhavcopy(bhavcopy(call_oi=oi, put_oi=oi, tweaks=tweaks), "NIFTY"), "NIFTY", rate=RATE)


class GridTests(unittest.TestCase):
    def test_condor_grid_rows_costs_and_tradeoffs(self):
        r = smile_report()
        g = fno.grid(r, "condor", 500, deltas=(0.35, 0.25, 0.15, 0.08), card=CARD)
        self.assertGreaterEqual(len(g["rows"]), 3)
        calls = dict(fno.otm_traded(r, "CE"))
        for x in g["rows"]:
            short_pe, short_ce = x["shorts"]
            self.assertEqual((short_pe["type"], short_ce["type"]), ("PE", "CE"))
            self.assertEqual(short_ce["strike"], min(calls, key=lambda k: abs(calls[k] - x["target_delta"])))
            self.assertEqual([w["strike"] for w in x["wings"]], [short_pe["strike"] - 500, short_ce["strike"] + 500])
            legs = [fno.parse_leg(l) for l in x["legs"]]
            calls_b, puts_b = r["_books"]
            prices = [(calls_b if l["type"] == "CE" else puts_b)[l["strike"]]["close"] for l in legs]
            credit = sum((1 if l["side"] == "SELL" else -1) * p * LOT for l, p in zip(legs, prices))
            charges = sum(fno.trade_charges(CARD, l["type"], l["side"], p, LOT)["total"] for l, p in zip(legs, prices))
            self.assertAlmostEqual(x["credit"], credit, places=6)
            self.assertAlmostEqual(x["credit_after_costs"], credit - charges, places=6)
            self.assertLess(x["max_loss_after_costs"], credit - 500 * LOT)  # worse than the gross max loss
            self.assertAlmostEqual(x["ev_after_costs"], fno.payoff(r, legs, card=CARD)["expected_value"]["after_costs"], places=6)
            self.assertLess(x["ev_after_costs"], 0)
            self.assertEqual(len(x["breakevens_after_costs"]), 2)
        rows = sorted(g["rows"], key=lambda x: -x["target_delta"])
        for a, b in zip(rows, rows[1:]):  # further out: less credit, higher chance of profit, lower chance of max loss
            self.assertGreater(a["credit_after_costs"], b["credit_after_costs"])
            self.assertLess(a["pop_after_costs"], b["pop_after_costs"])
            self.assertGreater(a["max_loss_chance"], b["max_loss_chance"])
        text = fno.grid_markdown(r, g)
        self.assertIn("iron condor grid", text)
        self.assertIn("not a recommendation", text)
        self.assertIn("Margin is not estimated", text)
        self.assertNotIn("₹unlimited", text)

    def test_strangle_spread_and_validation(self):
        r = smile_report()
        strangle = fno.grid(r, "strangle", deltas=(0.2, 0.1), card=CARD)
        self.assertTrue(all(x["max_loss_after_costs"] is None and x["max_loss_chance"] is None for x in strangle["rows"]))
        self.assertIn("| unlimited |", fno.grid_markdown(r, strangle))
        spread = fno.grid(r, "spread", 250, deltas=(0.2,), card=CARD)
        self.assertEqual([x["side"] for x in spread["rows"]], ["call", "put"])
        self.assertEqual([len(x["legs"]) for x in spread["rows"]], [2, 2])
        self.assertIn("### Bear call spreads", fno.grid_markdown(r, spread))
        with self.assertRaisesRegex(Block, "--width"):
            fno.grid(r, "condor", None)
        with tempfile.TemporaryDirectory() as d:
            code, text, err = run(["grid", "NIFTY", "--structure", "condor", "--width", "250", "--monthly", "--charges", card_file(d)], LiveFeeds())
            self.assertEqual(code, 0, err)
            self.assertIn("# NIFTY iron condor grid — expiry 2026-11-23", text)
            self.assertTrue(text.startswith("**END-OF-DAY data"))
            code, _, err = run(["grid", "NIFTY", "--structure", "condor", "--deltas", "0.7"], LiveFeeds())
            self.assertEqual(code, 2)


# ---------------- 6. payoff completeness ----------------

class PayoffCompletenessTests(unittest.TestCase):
    def test_charges_closed_form(self):
        sell = fno.trade_charges(CARD, "CE", "SELL", 100.0, 65)
        self.assertAlmostEqual(sell["stt"], 6500 * 0.0015)
        self.assertAlmostEqual(sell["stamp"], 0.0)
        self.assertAlmostEqual(sell["total"], 20 + 9.75 + 6500 * 0.0003553 + 6500 * 1e-6 + 0.18 * (20 + 6500 * 0.0003553 + 6500 * 1e-6))
        buy = fno.trade_charges(CARD, "PE", "BUY", 100.0, 65)
        self.assertAlmostEqual((buy["stt"], buy["stamp"]), (0.0, 6500 * 0.00003))
        fut = fno.trade_charges(CARD, "FUT", "BUY", 25000.0, 65)
        self.assertEqual(fut["brokerage"], 20)  # 0.03% of ₹16.25 lakh is above the ₹20 cap

    def test_short_call_costs_breakeven_ev_and_stress(self):
        r = plain_report()
        p = fno.payoff(r, [fno.parse_leg("SELL:CE:25250:1")], card=CARD)
        price = r["_books"][0][25250]["close"]
        entry = fno.trade_charges(CARD, "CE", "SELL", price, LOT)["total"]
        self.assertAlmostEqual(p["after_costs"]["net_premium"], price * LOT - entry, places=6)
        # Breakeven after costs: -q(s-K) + q·p - entry - exit(s) = 0, exit = buy back at intrinsic (s-K).
        g, seg = CARD["gst_rate"], CARD["segments"]["NFO:option"]
        per_unit = 1 + (seg["exchange_rate"] + CARD["sebi_rate"]) * (1 + g) + seg["stamp_buy"]
        be = 25250 + (LOT * price - entry - 20 * (1 + g)) / (LOT * per_unit)
        self.assertAlmostEqual(p["after_costs"]["breakevens"][0], be, places=6)
        self.assertAlmostEqual(p["after_costs"]["max_profit"], LOT * price - entry - 20 * (1 + g), places=6)
        self.assertIsNone(p["after_costs"]["max_loss"])
        self.assertIsNone(p["probabilities"]["max_loss_chance"])
        # Expected value at implied prices: only interest on the premium before costs.
        ev = p["expected_value"]
        self.assertAlmostEqual(ev["before_costs"], -LOT * price * (math.exp(RATE * r["_years"]) - 1), places=6)
        self.assertLess(ev["after_costs"], ev["before_costs"] - entry)
        # Stress: expiry P&L at the lognormal median F·e^(−σ²T/2) × e^(±kσ√T) — so ±1 SD are exactly the 68% range's ends —
        # and next-morning gaps at spot·e^(±3σ/√252).
        sd = r["atm_iv"] * math.sqrt(r["_years"])
        for x in p["scenarios"]["expiry"]:
            level = r["forward"] * math.exp(-0.5 * sd * sd + x["sd"] * sd)
            self.assertAlmostEqual(x["level"], level, places=6)
            self.assertAlmostEqual(x["pnl"], -LOT * (max(0.0, level - 25250) - price), places=6)
        ends = {x["sd"]: x["level"] for x in p["scenarios"]["expiry"]}
        self.assertAlmostEqual(ends[-1], p["probabilities"]["range_1sd"][0], places=6)
        self.assertAlmostEqual(ends[1], p["probabilities"]["range_1sd"][1], places=6)
        up = p["scenarios"]["gaps"][0]
        level = SPOT * math.exp(3 * r["atm_iv"] / math.sqrt(252))
        iv = p["legs"][0]["iv"]
        later = r["_years"] - 1 / 365
        expected = -LOT * (fno.black76("CE", r["forward"] * level / SPOT, 25250, later, iv, RATE) - price) - entry
        self.assertAlmostEqual(up["pnl"], expected, places=6)
        shock = p["scenarios"]["gaps"][2]
        down = SPOT * math.exp(-3 * r["atm_iv"] / math.sqrt(252))
        expected = -LOT * (fno.black76("CE", r["forward"] * down / SPOT, 25250, later, iv + 0.05, RATE) - price) - entry
        self.assertAlmostEqual(shock["pnl"], expected, places=6)
        self.assertIn("Margin is not estimated", p["margin_note"])
        self.assertNotIn("margin_estimate", p)

    def test_max_loss_chance_closed_form_flat_and_skew(self):
        r = smile_report()
        legs = [fno.parse_leg(x) for x in ("SELL:CE:25250:1", "BUY:CE:25750:1", "SELL:PE:24750:1", "BUY:PE:24250:1")]
        p = fno.payoff(r, legs, card=CARD)
        pr, F, T, sm = p["probabilities"], r["forward"], r["_years"], r["_smile"]
        flat = lambda k: fno.prob_above(k, F, r["atm_iv"], T)
        skew = lambda k: fno.prob_above_smile(k, F, T, sm)
        self.assertAlmostEqual(pr["max_loss_chance_flat"], (1 - flat(24250)) + flat(25750), places=6)
        self.assertAlmostEqual(pr["max_loss_chance"], (1 - skew(24250)) + skew(25750), places=6)
        b1, b2 = p["after_costs"]["breakevens"]
        self.assertAlmostEqual(pr["profit_after_costs"], skew(b1) - skew(b2), places=6)
        self.assertAlmostEqual(pr["profit_after_costs_flat"], flat(b1) - flat(b2), places=6)
        self.assertNotAlmostEqual(pr["profit_after_costs"], pr["profit_after_costs_flat"], places=3)  # skew matters here
        text = fno.payoff_markdown(r, p)
        self.assertIn("| Chance of the maximum loss |", text)
        self.assertIn("## Stress: ±1/2/3 SD at expiry and overnight gaps", text)
        self.assertIn("| Probability of profit at expiry, after costs |", text)


class SkewTests(unittest.TestCase):
    def test_fit_recovers_the_smile(self):
        r = smile_report()
        sm = r["_smile"]
        self.assertIsNotNone(sm)
        self.assertAlmostEqual(sm["a"], A, delta=0.002)
        self.assertAlmostEqual(sm["b"], B, delta=0.03)
        self.assertAlmostEqual(sm["c"], C, delta=0.5)
        self.assertLess(sm["rms"], 0.001)

    def test_digital_matches_closed_form_and_black76_strike_derivative(self):
        r = smile_report()
        F, T, sm = r["forward"], r["_years"], r["_smile"]
        disc = math.exp(-RATE * T)
        for k in (24400.0, 24900.0, 25300.0, 25700.0):
            x = math.log(k / F)
            vol = sm["a"] + sm["b"] * x + sm["c"] * x * x
            slope = (sm["b"] + 2 * sm["c"] * x) / k
            sd = vol * math.sqrt(T)
            d1 = (math.log(F / k) + sd * sd / 2) / sd
            closed = fno.ncdf(d1 - sd) - F * fno.npdf(d1) * math.sqrt(T) * slope
            self.assertAlmostEqual(fno.prob_above_smile(k, F, T, sm), closed, places=12)
            h = 0.01
            call = lambda s: fno.black76("CE", F, s, T, fno.smile_vol(sm, s), RATE)
            numeric = -(call(k + h) - call(k - h)) / (2 * h * disc)
            self.assertAlmostEqual(fno.prob_above_smile(k, F, T, sm), numeric, places=5)
            # Against the true smile used to price the fixture.
            true_vol = true_smile(k)
            true_slope = (B + 2 * C * math.log(k / FWD)) / k
            sd = true_vol * math.sqrt(T)
            d1 = (math.log(F / k) + sd * sd / 2) / sd
            self.assertAlmostEqual(fno.prob_above_smile(k, F, T, sm), fno.ncdf(d1 - sd) - F * fno.npdf(d1) * math.sqrt(T) * true_slope, delta=0.003)
        # Put skew (σ falls as K rises) puts more probability above a strike than the flat N(d2) at that strike's own IV.
        k = 25500.0
        own = fno.prob_above(k, F, fno.smile_vol(sm, k), T)
        self.assertGreater(fno.prob_above_smile(k, F, T, sm), own)

    def test_flat_smile_equals_lognormal_and_short_chain_falls_back(self):
        r = smile_report(lambda k: VOL)
        sm = r["_smile"]
        for k in (24500.0, 25000.0, 25600.0):
            self.assertAlmostEqual(fno.prob_above_smile(k, r["forward"], r["_years"], sm), fno.prob_above(k, r["forward"], VOL, r["_years"]), places=3)
        p = fno.payoff(plain_report(call_oi={25000: 10}, put_oi={25000: 10}), [fno.parse_leg("BUY:CE:25000:1")], card=CARD)
        self.assertFalse(p["probabilities"]["skew_used"])
        self.assertIn("flat ATM IV", p["probabilities"]["skew_basis"])
        self.assertAlmostEqual(p["probabilities"]["profit_skew"], p["probabilities"]["profit"], places=12)


# ---------------- 8. strikes without an underlying ----------------

class StrikeInferenceTests(unittest.TestCase):
    def raw(self):
        fin = [row(TckrSymb="FINNIFTY", spot=27000, FinInstrmTp="IDO", XpryDt=EXPIRY, StrkPric="25250.00", OptnTp=k, ClsPric=1, PrvsClsgPric=1,
                   SttlmPric=1, OpnIntrst=10, ChngInOpnIntrst=0, TtlTradgVol=1, NewBrdLotQty=60) for k in ("CE", "PE")]
        bank = [row(TckrSymb="BANKNIFTY", spot=52000, FinInstrmTp="IDO", XpryDt=EXPIRY, StrkPric="52000.00", OptnTp="CE", ClsPric=1,
                    PrvsClsgPric=1, SttlmPric=1, OpnIntrst=10, ChngInOpnIntrst=0, TtlTradgVol=1, NewBrdLotQty=30)]
        return bhavcopy(extra=fin + bank)

    def test_strike_spec_and_inference(self):
        self.assertEqual(fno.strike_spec("25800 CE"), (25800.0, "CE"))
        self.assertEqual(fno.strike_spec("25800ce"), (25800.0, "CE"))
        self.assertEqual(fno.strike_spec("25800 put"), (25800.0, "PE"))
        self.assertEqual(fno.strike_spec("25800"), (25800.0, None))
        self.assertIsNone(fno.strike_spec("NIFTY"))
        symbol, note, pool = fno.infer_underlying(self.raw(), [25250.0])
        self.assertEqual(symbol, "NIFTY")
        self.assertIn("Underlying assumed: NIFTY — strike 25,250 is 1.0% from NIFTY's Tue 17 Nov close 25,000.00", note)
        self.assertIn("also listed on FINNIFTY (27,000, 6.7% away)", note)
        self.assertEqual(fno.infer_underlying(self.raw(), [52000.0])[0], "BANKNIFTY")
        with self.assertRaisesRegex(Block, "name the underlying"):
            fno.infer_underlying(self.raw(), [90000.0])

    def test_cli_accepts_a_bare_strike(self):
        feed = lambda url: self.raw() if "BhavCopy" in url and "20261117" in url else (_ for _ in ()).throw(Block("x"))
        code, text, err = run(["payoff", "--leg", "SELL:CE:25250:1", "--summary"], feed)
        self.assertEqual(code, 0, err)
        self.assertIn("**Note:** Underlying assumed: NIFTY", text)
        self.assertIn("**NIFTY payoff", text)
        code, text, err = run(["chain", "25250CE", "--summary", "--no-vix", "--no-compare"], feed)
        self.assertEqual(code, 0, err)
        self.assertIn("**Note:** Underlying assumed: NIFTY", text)
        self.assertRegex(text, r"- 25,250 CE: close [0-9.]+, IV 15\.0%, delta 0\.[0-9]{2}, OI 80 contracts")
        code, _, err = run(["chain", "--summary"], feed)
        self.assertEqual(code, 2)
        self.assertIn("name the underlying", err)


if __name__ == "__main__":
    unittest.main()
