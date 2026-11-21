"""Offline contracts for the round-6 fixes (18 Nov 2026 review, just after the 15:30 close, before the evening bhavcopy).
Never contacts the network.

1. ORB after the close: Yahoo drops the 15:20/15:25 bars, so the data ends at the last bar of the unbroken run instead of
   blocking; the answer is a review ("ENTRY WINDOW CLOSED", what would have been, how each stop played out); a real gap
   before the end of the entry window blocks with the specific reason; --now replays an earlier date from the network.
2. F&O 'now' after the close: payoff and grid keep the 'If placed now' column, Δ and the ⚠ near-the-money line, priced at
   the NSE closing level from NSE Clearing's closing snapshot; one short option leads with what selling now would get.
3. Chain after the close: NSE's option chain (option-chain-v3) at today's close, or the bhavcopy with the not-yet-published
   note; no 1-SD line for the ended session; spot labelled as the NSE close; exact endpoint allow-listing.
4. Max loss after costs is never smaller than any expiry row (exit STT on in-the-money legs, exercise STT the worst case).
5. holdings --summary: the P&L what-if swaps every ⚠ price; the tax what-if only those that move the tax, labelled.
"""
import contextlib
from datetime import date, datetime, timedelta
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
import analysis_rules as ar
import fno
import holdings_review as hr
import public_market as pm
from test_fno import EXPIRY, LOT, RATE, SPOT, TRADE
from test_fno_round2 import CARD, LiveFeeds, card_file, plain_report, run
from test_fno_margin import span_xml, span_zip
from test_orb_quick import RANGE, day_bars, payload
from test_portfolio_valuation import run as pv_run

AFTER = datetime(2026, 11, 18, 17, 0, tzinfo=IST)   # after the close, before the evening bhavcopy
CLOSE_LEVEL, CLOSE_VOL = 24900.0, 0.16
STRIKES = (24500.0, 24750.0, 25000.0, 25250.0, 25500.0)
V3_CALL_OI = {24500: 30, 24750: 50, 25000: 120, 25250: 160, 25500: 400}     # contracts
V3_PUT_OI = {24500: 300, 24750: 180, 25000: 90, 25250: 40, 25500: 20}


def close_years():
    return fno.years_to_expiry(EXPIRY, datetime(2026, 11, 18, 15, 30, tzinfo=IST))


def all_indices(stamp="18-Nov-2026 15:30", nifty=CLOSE_LEVEL):
    rows = [{"index": "NIFTY 50", "last": nifty, "previousClose": SPOT, "variation": round(nifty - SPOT, 2),
             "percentChange": round((nifty / SPOT - 1) * 100, 2), "open": SPOT, "high": SPOT + 10, "low": nifty - 10},
            {"index": "INDIA VIX", "last": 15.56, "previousClose": 15.52, "variation": 0.04, "percentChange": 0.26}]
    return json.dumps({"timestamp": stamp, "data": rows}).encode()


def v3_chain(stamp="18-Nov-2026 15:40:00", level=CLOSE_LEVEL, expiry_text="23-11-2026"):
    years = close_years()
    forward = level * math.exp(RATE * years)
    data = []
    for k in STRIKES:
        row = {"strikePrice": k, "expiryDates": "23-Nov-2026"}
        for kind, oi in (("CE", V3_CALL_OI), ("PE", V3_PUT_OI)):
            price = round(round(fno.black76(kind, forward, k, years, CLOSE_VOL, RATE) / 0.05) * 0.05, 2)
            row[kind] = {"strikePrice": k, "expiryDate": expiry_text, "underlying": "NIFTY", "identifier": f"OPTIDXNIFTY{kind}{k}",
                         "openInterest": oi[int(k)], "changeinOpenInterest": 10, "totalTradedVolume": 500, "lastPrice": price,
                         "change": -1.0, "impliedVolatility": 16.0, "underlyingValue": level}
        data.append(row)
    return json.dumps({"records": {"data": data, "timestamp": stamp, "underlyingValue": level,
                                   "expiryDates": ["23-Nov-2026"], "strikePrices": [str(int(k)) for k in STRIKES]},
                       "filtered": {"data": []}}).encode()


def closing_span(level=CLOSE_LEVEL, created="202611181532"):
    """NSE Clearing's closing intraday file (i5 on 18 Nov 2026): the closing level and settlement prices."""
    years = close_years()
    forward = level * math.exp(RATE * years)
    pe = EXPIRY.replace("-", "")
    options = [(pe, kind[0], k, round(round(fno.black76(kind, forward, k, years, CLOSE_VOL, RATE) / 0.05) * 0.05, 2), [0.0] * 16)
               for kind in ("CE", "PE") for k in STRIKES]
    return span_zip(span_xml("NIFTY", level, futures=[(pe, forward, [0.0] * 16)], options=options, business=created[:8],
                             created=created), f"nsccl.{created[:8]}.i05.spn")


class PostCloseFeeds(LiveFeeds):
    """LiveFeeds (17 Nov bhavcopy; 18 Nov's not yet published) plus NSE's allIndices, the option chain and the closing SPAN file."""

    def __init__(self, *, chain=True, indices=True, span=True, **kw):
        super().__init__(**kw)
        self.chain, self.indices, self.span = chain, indices, span

    def __call__(self, url):
        if url == fno.NSE_ALL_INDICES:
            self.urls.append(url)
            if not self.indices:
                raise Block("NSE: source unavailable")
            return all_indices()
        if fno.OPTION_CHAIN_V3_RE.match(url):
            self.urls.append(url)
            if not self.chain:
                raise Block("PUBLIC: NSE option chain unavailable")
            return v3_chain()
        if "nsccl/span" in url:
            self.urls.append(url)
            if self.span and url.endswith("nsccl.20261118.i5.zip"):
                return closing_span()
            raise Block("PUBLIC: source unavailable")
        return super().__call__(url)


# ---------------- 1. ORB after the close ----------------

ORB_DAY = instant("2026-11-18T09:15:00+05:30")
AFTER_CLOSE = instant("2026-11-18T15:55:00+05:30")


def full_session(after_range, *, drop=("15:20", "15:25")):
    """RANGE (high 1372.30, low 1360.00), then `after_range`, padded flat to 15:25; bars at `drop` removed (as Yahoo does)."""
    rows = RANGE + after_range
    last = rows[-1][3]
    rows += [(last, last + 0.1, last - 0.1, last)] * (75 - len(rows))
    return [b for b in day_bars(ORB_DAY, rows) if b["at"][11:16] not in drop]


def orb_history(bars, now):
    h = pm.chart(payload(bars), "RELIANCE.NS", "5m", now)
    h["as_of"] = now.isoformat()
    return h


# a break by the 09:30 bar (completed 09:35, close 1,374 > 1,372.30), then the midpoint stop (1,366.15) by the 09:50 bar
TRIGGER_THEN_STOP = [(1364, 1375, 1363, 1374), (1374, 1376, 1371, 1372), (1372, 1373, 1369, 1370), (1370, 1371, 1368, 1369),
                     (1369, 1370, 1365, 1366)]
FLAT = [(1364, 1365, 1363, 1364)]


class OrbAfterCloseTests(unittest.TestCase):
    def test_missing_closing_bars_end_the_data_instead_of_blocking(self):
        bars = full_session(TRIGGER_THEN_STOP)
        self.assertEqual(bars[-1]["at"][11:16], "15:15")                          # Yahoo returned nothing after 15:15
        with self.assertRaisesRegex(Block, "incomplete session prefix"):          # the strict prefix still refuses
            ar.prefix(bars, AFTER_CLOSE)
        self.assertEqual(ar.data_end(bars, AFTER_CLOSE).strftime("%H:%M"), "15:20")
        r = pm.public_entry(orb_history(bars, AFTER_CLOSE), AFTER_CLOSE)
        self.assertEqual(r["state"], "triggered")
        self.assertEqual((r["session_over"], r["data_ends_at"], r["bars_completed"]), (True, "15:20", 73))
        self.assertTrue(r["entry_window"]["closed"])

    def test_a_late_gap_with_a_later_bar_still_ends_the_data_at_the_gap(self):
        bars = full_session(TRIGGER_THEN_STOP, drop=("15:20",))                  # 15:25 present, 15:20 missing (17 Nov pattern)
        r = pm.public_entry(orb_history(bars, AFTER_CLOSE), AFTER_CLOSE)
        self.assertEqual((r["state"], r["data_ends_at"]), ("triggered", "15:20"))

    def test_review_wording_after_the_close(self):
        r = pm.public_entry(orb_history(full_session(TRIGGER_THEN_STOP), AFTER_CLOSE), AFTER_CLOSE)
        rng = r["opening_range"]
        r["sizing"] = pm.range_sizes(range_high=rng["high"], range_low=rng["low"], risk=2000, side="BUY", product="MIS", entry=r["entry"])
        text = pm.entry_markdown(r)
        lines = text.splitlines()
        self.assertEqual(lines[0], "# RELIANCE.NS ORB BUY review — 2026-11-18 15:55 IST, session closed")
        self.assertTrue(lines[2].startswith("**ENTRY WINDOW CLOSED** — the session ended at 15:30 IST and the 09:30–14:30 IST entry "
                                            "window with it; nothing to act on now. What would have been: TRIGGERED at 09:35"), lines[2])
        mid = next(s for s in r["sizing"]["sizes"] if s["stop_rule"] == "range midpoint")
        self.assertIn(f"- Range midpoint stop ₹1,366.15 with {mid['quantity']:,} shares: the stop was reached first, by the bar completed "
                      f"at 09:55 IST, a loss of ≈ {pm.inr(mid['loss_at_stop'])} including charges.", text)
        self.assertIn("- The provider's 5-minute bars for the session end at 15:20 IST", text)
        self.assertIn("## Size (what it would have been)", text)
        self.assertIn(f"**{mid['quantity']:,}**", text)                            # the midpoint-stop size, bold
        self.assertIn("at the end of the data (bar 15:15, completed 15:20 IST)", text)
        self.assertNotIn("now (last completed close", text)

    def test_no_trigger_after_the_close_says_nothing_to_act_on(self):
        r = pm.public_entry(orb_history(full_session(FLAT), AFTER_CLOSE), AFTER_CLOSE)
        self.assertEqual(r["state"], "waiting")
        text = pm.entry_markdown(r)
        self.assertIn("**ENTRY WINDOW CLOSED**", text)
        self.assertIn("No completed 5-minute close above 1,372.30 inside the 09:30-14:30 IST entry window; nothing to act on today", text)
        self.assertNotIn("How it played out", text)
        self.assertIn("The provider's 5-minute bars for the session end at 15:20 IST", text)

    def test_a_gap_before_the_window_ends_blocks_with_the_specific_reason(self):
        bars = full_session(FLAT, drop=("12:00", "15:20", "15:25"))
        with self.assertRaisesRegex(Block, "bars for today stop at 12:00 .*before the 14:30 end of the entry window"):
            pm.public_entry(orb_history(bars, AFTER_CLOSE), AFTER_CLOSE)
        with tempfile.TemporaryDirectory() as tmp:
            src = Path(tmp) / "h.json"
            src.write_text(json.dumps(orb_history(bars, AFTER_CLOSE)), encoding="utf-8")
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                code = pm.main(["entry", "--input", str(src), "--now", AFTER_CLOSE.isoformat()])
        self.assertEqual(code, 2)
        blocked = json.loads(out.getvalue())
        self.assertEqual(blocked["status"], "BLOCKED")
        self.assertIn("stop at 12:00 (the 12:00 bar is missing)", blocked["reason"])
        # A trigger before the gap is still a complete answer.
        early = full_session(TRIGGER_THEN_STOP, drop=("12:00", "15:20", "15:25"))
        self.assertEqual(pm.public_entry(orb_history(early, AFTER_CLOSE), AFTER_CLOSE)["state"], "triggered")

    def test_during_the_session_a_missing_bar_still_blocks(self):
        at = instant("2026-11-18T11:02:00+05:30")
        bars = [b for b in day_bars(ORB_DAY, RANGE + FLAT * 30) if instant(b["at"]) < at and b["at"][11:16] != "10:30"]
        with self.assertRaisesRegex(Block, "incomplete session prefix"):
            pm.public_entry(orb_history(bars, at), at)

    def test_now_replays_an_earlier_date_from_the_network_and_refuses_today(self):
        day17 = instant("2026-11-17T09:15:00+05:30")
        rows = RANGE + TRIGGER_THEN_STOP
        rows += [(1366, 1366.1, 1365.9, 1366)] * (75 - len(rows))
        bars = day_bars(day17, rows) + full_session(FLAT)
        live = pm.chart(payload(bars), "RELIANCE.NS", "5m", AFTER_CLOSE)
        out = io.StringIO()
        with mock.patch.object(pm, "collect_chart", return_value=live), mock.patch.object(pm, "instant", side_effect=lambda t=None: instant(t) if t else AFTER_CLOSE), \
                contextlib.redirect_stdout(out):
            code = pm.main(["entry", "--symbol", "RELIANCE.NS", "--now", "2026-11-17T15:45:00+05:30", "--risk", "2000"])
        text = out.getvalue()
        self.assertEqual(code, 0, text)
        self.assertTrue(text.startswith("# RELIANCE.NS ORB BUY review — 2026-11-17 15:45 IST, session closed (replay)"), text[:200])
        self.assertIn("TRIGGERED at 09:35", text)
        out = io.StringIO()
        with mock.patch.object(pm, "collect_chart", return_value=live), mock.patch.object(pm, "instant", side_effect=lambda t=None: instant(t) if t else AFTER_CLOSE), \
                contextlib.redirect_stdout(out):
            code = pm.main(["entry", "--symbol", "RELIANCE.NS", "--now", "2026-11-18T11:00:00+05:30"])
        self.assertEqual(code, 2)
        self.assertIn("CLOCK: --now without --input replays an earlier date only", json.loads(out.getvalue())["reason"])
        replay = pm.replay_history(live, instant("2026-11-17T11:00:00+05:30"))
        self.assertTrue(all(instant(b["at"]) < instant("2026-11-17T11:00:00+05:30") for b in replay["bars"]))
        with self.assertRaisesRegex(Block, "no 5m bars for 10 Nov 2026"):
            pm.replay_history(live, instant("2026-11-10T15:45:00+05:30"))


# ---------------- 2 and 3. F&O after the close ----------------

class EndpointTests(unittest.TestCase):
    def test_option_chain_url_is_exact(self):
        url = fno.chain_v3_url("NIFTY", "2026-11-23")
        self.assertEqual(url, "https://www.nseindia.com/api/option-chain-v3?type=Indices&symbol=NIFTY&expiry=23-Nov-2026")
        self.assertTrue(fno.OPTION_CHAIN_V3_RE.match(url))
        self.assertEqual(fno.chain_v3_url("M&M", "2026-11-23"),
                         "https://www.nseindia.com/api/option-chain-v3?type=Equity&symbol=M%26M&expiry=23-Nov-2026")
        self.assertTrue(fno.OPTION_CHAIN_V3_RE.match(fno.chain_v3_url("M&M", "2026-11-23")))
        for bad in (url + "&x=1", url.replace("www.nseindia.com", "evil.example"), url.replace("https", "http"),
                    url.replace("Indices", "indices"), url.replace("23-Nov-2026", "2026-11-23"),
                    "https://www.nseindia.com/api/option-chain-indices?symbol=NIFTY"):
            self.assertIsNone(fno.OPTION_CHAIN_V3_RE.match(bad), bad)
            with self.assertRaisesRegex(Block, "unsupported source"):
                fno.nse_chain_request(bad)

    def test_public_fetch_routes_the_two_nse_endpoints(self):
        url = fno.chain_v3_url("NIFTY", "2026-11-23")
        with mock.patch.object(fno, "nse_chain_request", return_value=b"chain") as chain:
            self.assertEqual(fno.public_fetch(url), b"chain")
            chain.assert_called_once_with(url)
        import nse_live
        with mock.patch.object(nse_live, "fetch", return_value=b"indices") as idx:
            self.assertEqual(fno.public_fetch(fno.NSE_ALL_INDICES), b"indices")
            idx.assert_called_once_with(fno.NSE_ALL_INDICES)

    def test_option_chain_must_be_from_todays_close(self):
        good, at, level = fno.parse_chain_v3(v3_chain(), "NIFTY", EXPIRY, LOT, AFTER)
        self.assertEqual((at.strftime("%H:%M"), level), ("15:40", CLOSE_LEVEL))
        c = next(x for x in good if x["kind"] == "CE" and x["strike"] == 25500.0)
        self.assertEqual((c["oi"], c["oi_change"], c["trade_date"]), (400 * LOT, 10 * LOT, "2026-11-18"))   # contracts × lot = shares
        for raw in (v3_chain(stamp="18-Nov-2026 14:10:00"), v3_chain(stamp="17-Nov-2026 15:40:00")):
            with self.assertRaisesRegex(Block, "not from today's close"):
                fno.parse_chain_v3(raw, "NIFTY", EXPIRY, LOT, AFTER)
        with self.assertRaisesRegex(Block, "unexpected row"):
            fno.parse_chain_v3(v3_chain(expiry_text="29-12-2026"), "NIFTY", EXPIRY, LOT, AFTER)


class ChainAfterCloseTests(unittest.TestCase):
    def test_chain_reads_nse_option_chain_at_todays_close(self):
        feeds = PostCloseFeeds()
        code, text, err = run(["chain", "NIFTY", "--summary", "--no-vix"], feeds, clock=AFTER)
        self.assertEqual(code, 0, err)
        self.assertIn("— data 2026-11-18 close (NSE option chain, 18 Nov 15:30 close)", text.splitlines()[0])
        self.assertIn("- Spot 24,900.00 (Wed 18 Nov close, NSE)", text)
        self.assertIn("the official Wed 18 Nov bhavcopy (usually ~18:00–19:00 IST) will confirm them", text)
        pcr = sum(V3_PUT_OI.values()) / sum(V3_CALL_OI.values())
        self.assertIn(f"- PCR (OI) {fno.fmt(pcr)} (", text)
        self.assertNotIn("not yet published (usually", text.splitlines()[0])
        self.assertNotIn("Wed 18 Nov session (closed)", text)                    # no 1-SD line for the ended session
        self.assertIn("**Next session — Thu 19 Nov (tomorrow)**", text)
        self.assertIn("from the Wed 18 Nov close 24,900.00", text)                # expected move centred on today's close
        self.assertNotIn("Yahoo, unofficial", text)
        code, full, _ = run(["chain", "NIFTY", "--no-vix", "--format", "json"], PostCloseFeeds(), clock=AFTER)
        self.assertIn("today's (Wed 18 Nov) file is not yet published", json.loads(full)["participants"]["pending_note"])  # the 17 Nov file, dated
        code, raw, _ = run(["chain", "NIFTY", "--summary", "--no-vix", "--format", "json"], PostCloseFeeds(), clock=AFTER)
        data = json.loads(raw)
        self.assertAlmostEqual(data["pcr_oi"], pcr, places=9)
        self.assertEqual(data["data_date"], "2026-11-18")
        self.assertEqual(data["previous_date"], TRADE)                            # compared with the 17 Nov bhavcopy
        self.assertEqual(data["expiry_type"], "monthly")                          # calendar from the bhavcopy
        bhav = [u for u in feeds.urls if "BhavCopy" in u and TRADE.replace("-", "") in u]
        self.assertEqual(len(bhav), 1)                                            # the 17 Nov file is reused, not downloaded twice

    def test_fallback_to_the_bhavcopy_says_not_yet_published(self):
        code, text, err = run(["chain", "NIFTY", "--summary", "--no-vix"], PostCloseFeeds(chain=False), clock=AFTER)
        self.assertEqual(code, 0, err)
        self.assertEqual(text.splitlines()[0],
                         "**Today's (Wed 18 Nov) F&O bhavcopy is not yet published (usually ~18:00–19:00 IST); PCR, OI and max pain "
                         "below are the Tue 17 Nov close — re-run after that for today's PCR, OI and max pain.**")
        self.assertIn("· the NSE close 24,900.00 (Wed 18 Nov) (-0.40%)", text)
        self.assertNotIn("Yahoo, unofficial", text)
        self.assertNotIn("Wed 18 Nov session (closed)", text)
        self.assertIn("- Now (estimate, not live quotes): ATM", text)
        self.assertIn("from NSE's 15:32 IST closing option prices", text)
        self.assertIn("— from the NSE close 24,900.00 (Wed 18 Nov), snapshot IV", text)
        self.assertIn("1-sigma for the Thu 19 Nov session (tomorrow), from the NSE close 24,900.00 (Wed 18 Nov)", text)

    def test_yahoo_close_is_labelled_unofficial_when_nse_fails(self):
        code, text, _ = run(["chain", "NIFTY", "--summary", "--no-vix", "--no-snapshot"], PostCloseFeeds(chain=False, indices=False),
                            clock=AFTER)
        self.assertEqual(code, 0)
        self.assertIn("today's close 24,900.00 (Yahoo, unofficial)", text)

    def test_during_the_session_nothing_changes(self):
        feeds = PostCloseFeeds()
        code, text, _ = run(["chain", "NIFTY", "--summary", "--no-vix", "--no-snapshot"], feeds)   # 10:00 IST
        self.assertEqual(code, 0)
        self.assertFalse(any(fno.OPTION_CHAIN_V3_RE.match(u) or u == fno.NSE_ALL_INDICES for u in feeds.urls))
        self.assertIn("Yahoo, unofficial", text)


class PayoffAfterCloseTests(unittest.TestCase):
    def test_single_short_option_leads_with_what_selling_now_gets(self):
        with tempfile.TemporaryDirectory() as d:
            argv = ["payoff", "NIFTY", "--leg", "SELL:CE:25250:1", "--charges", card_file(d)]
            code, raw, err = run(argv + ["--format", "json"], PostCloseFeeds(), clock=AFTER)
            self.assertEqual(code, 0, err)
            pn = json.loads(raw)["payoff"]["placed_now"]
            self.assertEqual((pn["source"], pn["close"], pn["provider"], pn["level"]), ("nse_intraday_snapshot", True, "NSE", CLOSE_LEVEL))
            self.assertTrue(pn["snapshot"]["closing"])
            self.assertEqual(pn["at"], "2026-11-18T15:32:00+05:30")             # priced on the closing file's own clock, not re-timed
            years = close_years()
            settle = round(round(fno.black76("CE", CLOSE_LEVEL * math.exp(RATE * years), 25250.0, years, CLOSE_VOL, RATE) / 0.05) * 0.05, 2)
            self.assertAlmostEqual(pn["legs"][0]["now"], settle, places=6)       # exactly the closing file's own price
            code, text, _ = run(argv + ["--summary"], PostCloseFeeds(), clock=AFTER)
        lines = text.splitlines()
        self.assertTrue(lines[0].startswith("**Today's (Wed 18 Nov) F&O bhavcopy is not yet published (usually ~18:00–19:00 IST); "
                                            "'If placed now' is estimated at today's close"))
        self.assertIn("**If placed now — estimate at the NSE close NIFTY 24,900.00 (Wed 18 Nov) from NSE's 15:32 IST closing option "
                      "prices; market closed, so an order now fills at the next session's prices, not these", text)
        lead = next(l for l in lines if l.startswith("**Selling now would get about"))
        self.assertIn(f"₹{fno.fmt(pn['legs'][0]['now'])} a unit", lead)
        self.assertIn(f"not the ₹{fno.fmt(pn['legs'][0]['close'])} of the Tue 17 Nov close", lead)
        self.assertIn("| If placed now (estimate) | At the Tue 17 Nov close |", text)

    def test_no_lead_line_when_the_price_barely_moved_or_for_spreads(self):
        s = {"legs": [{"type": "CE", "side": "SELL", "units": -65, "lots": 1}], "lot_size": 65, "data_date": TRADE,
             "placed_now": {"legs": [{"now": 50.0, "close": 51.45}]}}
        self.assertIsNone(fno.single_leg_lead(s))
        s["placed_now"]["legs"][0]["now"] = 21.0
        self.assertTrue(fno.single_leg_lead(s).startswith("**Selling now would get about ₹21.00 a unit (≈ ₹1,365 for 1 lot of 65;"))
        s["legs"].append({"type": "CE", "side": "BUY", "units": 65, "lots": 1})
        self.assertIsNone(fno.single_leg_lead(s))

    def test_grid_keeps_delta_now_and_the_near_money_warning(self):
        with tempfile.TemporaryDirectory() as d:
            argv = ["grid", "NIFTY", "--structure", "spread", "--side", "put", "--width", "250", "--deltas", "0.45,0.2",
                    "--charges", card_file(d)]
            code, raw, err = run(argv + ["--format", "json"], PostCloseFeeds(), clock=AFTER)
            self.assertEqual(code, 0, err)
            data = json.loads(raw)
            self.assertTrue(data["placed_now"]["close"])
            near = [s_["strike"] for x in data["grid"]["rows"] for s_ in x["shorts"] if abs(s_["strike"] / CLOSE_LEVEL - 1) <= fno.NEAR_MONEY]
            self.assertTrue(near)                                                 # 25000 PE is 0.40% from 24,900
            code, text, _ = run(argv + ["--summary"], PostCloseFeeds(), clock=AFTER)
        self.assertIn("Δ is each short leg's delta now, at the NSE close NIFTY 24,900.00", text)
        self.assertIn(f"short put {fno.fmt(near[0], 0)} is now near the money — 0.40% from the NSE close NIFTY 24,900.00", text)
        self.assertIn("at the Tue 17 Nov close).", text)
        self.assertIn(" ⚠)", text)


# ---------------- 4. max loss after costs ----------------

CONDOR = ["SELL:PE:24750:1", "BUY:PE:24500:1", "SELL:CE:25250:1", "BUY:CE:25500:1"]


class MaxLossTests(unittest.TestCase):
    def test_max_loss_is_never_smaller_than_any_expiry_row(self):
        r = plain_report()
        p = fno.payoff(r, [fno.parse_leg(x) for x in CONDOR], card=CARD)
        a = p["after_costs"]
        rows = [x["pnl_after_costs"] for x in p["scenarios"]["expiry"]] + [t["expiry_pnl_after_costs"] for t in p["table"]]
        self.assertLessEqual(a["max_loss"], min(rows) + 1e-9)
        self.assertLess(a["max_loss"], a["max_loss_first"])                       # deeper than the long strike costs more
        self.assertIsNotNone(a["max_loss_level"])
        s = fno.payoff_summary(r, p)
        self.assertIn(f"max loss {fno.rupees(-a['max_loss'], 0)} after costs (at {fno.fmt(a['max_loss_level'], 0)}, the worst expiry level "
                      "shown", fno.payoff_summary_markdown(s))

    def test_exercise_stt_is_the_floor_for_an_in_the_money_long_leg(self):
        card = json.loads(json.dumps(CARD))
        card["segments"]["NFO:option"]["stt_sell"] = 0.0                         # squaring off would pay no STT at all
        card["option_brokerage"] = 0.0
        r = plain_report()
        p = fno.payoff(r, [fno.parse_leg("BUY:CE:25000:1")], card=card)
        row = next(t for t in p["table"] if t["price"] > 25000 + 50)
        exit_paid = row["expiry_pnl"] - row["expiry_pnl_after_costs"] - p["costs"]["entry_total"]
        # Squaring off would cost only exchange and SEBI fees with GST (~0.04%); exercise STT (0.125% of intrinsic) is dearer.
        self.assertAlmostEqual(exit_paid, (row["price"] - 25000.0) * LOT * fno.STT_EXERCISE, places=6)
        otm = next(t for t in p["table"] if t["price"] < 25000)
        self.assertAlmostEqual(otm["expiry_pnl"] - otm["expiry_pnl_after_costs"] - p["costs"]["entry_total"], 0.0, places=6)


# ---------------- 5. holdings what-if ----------------

class HoldingsWhatIfTests(unittest.TestCase):
    def test_pnl_what_if_swaps_every_flagged_price_and_tax_what_if_only_the_ones_that_move_it(self):
        r = pv_run([{"symbol": "ITC", "quantity": 200, "average_price": 380, "bought": "2023-06"},
                    {"symbol": "SBIN", "quantity": 40, "average_price": 600, "bought": "2026-10"}])
        m = r["totals"]["at_range_midpoint"]
        self.assertEqual([p["symbol"] for p in m["prices"]], ["ITC", "SBIN"])
        self.assertAlmostEqual(m["unrealised_pnl"], r["totals"]["unrealised_pnl"] - 200 * (420 - 380) - 40 * (960.65 - 600), places=2)
        lines = hr.summary(r).splitlines()
        value = next(i for i, l in enumerate(lines) if l.startswith("- **Value**"))
        self.assertEqual(lines[value + 1],
                         "  - ⚠ ITC and SBIN typed prices are outside what traded then; at ITC ~₹420 in Jun 2023 (Yahoo understates it "
                         f"before a demerger) and SBIN ~₹961 in Oct 2026: P&L {hr.inr(m['unrealised_pnl'])} ({hr.pct(m['unrealised_pnl_pct'])})"
                         + (f", return per year {hr.pct(m['returns']['annualised'])}" if (m.get("returns") or {}).get("annualised") is not None
                            else f", return {hr.pct(m['returns']['return'])} (under a year)" if (m.get("returns") or {}).get("return") is not None
                            else "")
                         + ("" if r["tax"]["hypothetical_sell_all"].get("at_range_midpoint") else " · tax figure unchanged"))
        tax_mid = r["tax"]["hypothetical_sell_all"].get("at_range_midpoint")
        if tax_mid:
            names = [p["symbol"] for p in tax_mid["prices"]]
            line = next(l for l in lines if l.startswith("  - ⚠ At "))
            self.assertEqual(line.endswith("the tax): tax " + hr.tax_range(tax_mid["estimated_total_tax_low"], tax_mid["estimated_total_tax"]))
                             or "ITC" in names, True)

    def test_largest_holding_and_top_sector_share_a_line_within_the_budget(self):
        r = pv_run([{"symbol": "ITC", "quantity": 200, "average_price": 380, "bought": "2023-06"},
                    {"symbol": "RELIANCE", "quantity": 30, "average_price": 2400, "bought": "2023-01"},
                    {"symbol": "INFY", "quantity": 15, "average_price": 1500},
                    {"symbol": "SBIN", "quantity": 40, "average_price": 600, "bought": "2026-10"}])
        lines = hr.summary(r).splitlines()
        largest = next(l for l in lines if l.startswith("- **Largest holding**"))
        self.assertIn(" · **top sector** ", largest)
        self.assertFalse(any(l.startswith("- **Top sector**") for l in lines))
        self.assertLessEqual(len(lines), 15)                                      # the round-5 budget (14) + round 7's 'Bonus applied' line
        ret = next(l for l in lines if l.startswith("- **Return per year**"))
        self.assertRegex(ret, r"^- \*\*Return per year\*\* [+-]\d+\.\d% vs Nifty 50 [+-]\d+\.\d% on the same dates \(XIRR on ")


if __name__ == "__main__":
    unittest.main()
