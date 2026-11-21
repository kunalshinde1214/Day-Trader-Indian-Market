"""Round 7 (held-out market questions): previous-day bhavcopy retried and cached on disk, the stock-drivers gap stated,
NSE's evening previousDay relabel, India VIX official close and Yahoo previous close, sector drill-down, period
comparison, FII/FPI context with premise check, India-only cues, session-dated flows. Offline fakes only."""
from datetime import date, datetime, timedelta
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT / "tests"))
from core import Block, IST, instant
import daily
import market_movers as mm
import nse_live
from test_market_movers import DAYS, FLOWS, NOW, Market
from test_nse_live import holiday_json

AFTER = datetime(2026, 11, 18, 17, 20, tzinfo=IST)
NO_WAIT = patch.object(nse_live, "RETRY_DELAYS", (0.0, 0.0))


def bhav_rows(day, rows):
    """A minimal sec_bhavdata_full file: rows = [(symbol, previous close, close)]."""
    lines = ["SYMBOL, SERIES, DATE1, PREV_CLOSE, CLOSE_PRICE"] + [f"{s}, EQ, {day:%d-%b-%Y}, {p}, {c}" for s, p, c in rows]
    return ("\n".join(lines) + "\n").encode()


def flows(day="17-Nov-2026", fii=4195.38):
    return json.dumps([{"category": "DII", "date": day, "buyValue": "100", "sellValue": "50", "netValue": "50"},
                       {"category": "FII/FPI", "date": day, "buyValue": str(10000 + fii), "sellValue": "10000", "netValue": str(fii)}]).encode()


def nsdl_html(rows):
    """NSDL Monthly.aspx shape: per reporting date an Equity block (Stock Exchange, Primary market, Sub-total) then debt rows."""
    cell = "<td align='right'>{}</td>"
    out = ["<table><tr><th>Reporting Date</th></tr>"]
    for day, buy, sell, sub in rows:
        net = buy - sell
        fmt = (lambda v: f"({abs(v):.2f})" if v < 0 else f"{v:.2f}")
        out.append(f"<tr><td rowspan='25'>{day}</td><td rowspan='3'>Equity</td><td>Stock Exchange</td>" + cell.format(f"{buy:.2f}")
                   + cell.format(f"{sell:.2f}") + cell.format(fmt(net)) + cell.format("1.00") + "<td rowspan='25'> Rs.93.57</td></tr>")
        out.append("<tr><td>Primary market & others</td>" + cell.format("0.00") * 4 + "</tr>")
        out.append("<tr><td>Sub-total</td>" + cell.format(f"{buy:.2f}") + cell.format(f"{sell:.2f}") + cell.format(fmt(sub)) + cell.format("1.00") + "</tr>")
        out.append("<tr><td rowspan='3'>Debt-General Limit</td><td>Stock Exchange</td>" + cell.format("5.00") * 2 + cell.format("0.00") * 2 + "</tr>")
        out.append("<tr><td>Sub-total</td>" + cell.format("9.99") * 4 + "</tr>")
    return ("".join(out) + "</table>").encode()


NSDL = nsdl_html([("16-Nov-2026", 18571.96, 13007.07, 5564.90), ("17-Nov-2026", 16657.30, 11549.69, 5107.61),
                  ("18-Nov-2026", 21092.53, 15177.67, 11265.68)])


# ---------------------------------------------------------------- 1. previous-day bhavcopy: retry, disk cache, naming

class RetryAndCacheTests(unittest.TestCase):
    def setUp(self): nse_live.clear_cache()

    def test_refusals_are_retried_with_backoff_and_404_is_final(self):
        calls, waits = [], []
        def flaky(url):
            calls.append(url)
            if len(calls) < 3: raise Block("NSE: source refused the request")
            return b"ok"
        self.assertEqual(nse_live.fetch_retry(flaky, "u", sleep=waits.append), b"ok")
        self.assertEqual((len(calls), waits), (3, list(nse_live.RETRY_DELAYS)))
        def gone(url): calls.append(url); raise nse_live.Missing("404")
        calls.clear()
        with self.assertRaises(nse_live.Missing): nse_live.fetch_retry(gone, "u", sleep=waits.append)
        self.assertEqual(len(calls), 1)
        def refused(url): raise Block("NSE: source refused the request")
        with self.assertRaises(Block): nse_live.fetch_retry(refused, "u", sleep=lambda s: None)

    def test_past_bhavcopy_is_saved_and_read_back_without_a_download(self):
        day = date(2026, 11, 17); raw = bhav_rows(day, [("AAA", 100, 101), ("BBB", 50, 49)])
        with tempfile.TemporaryDirectory() as folder:
            first = []
            def once_refused(url):
                first.append(url)
                if len(first) == 1: raise Block("NSE: source refused the request")
                return raw
            got = nse_live.cached_bhavcopy(day, fetcher=once_refused, folder=folder, today=date(2026, 11, 18), sleep=lambda s: None)
            self.assertEqual((got, len(first)), (raw, 2))
            self.assertTrue((Path(folder) / "nse_bhavcopy" / "sec_bhavdata_full_17112026.csv").exists())
            nse_live.clear_cache()
            def down(url): raise Block("NSE: source refused the request")
            self.assertEqual(nse_live.cached_bhavcopy(day, fetcher=down, folder=folder, today=date(2026, 11, 18)), raw)

    def test_todays_bhavcopy_is_not_kept_and_a_wrong_or_damaged_file_is_refused(self):
        day = date(2026, 11, 18)
        with tempfile.TemporaryDirectory() as folder:
            nse_live.cached_bhavcopy(day, fetcher=lambda u: bhav_rows(day, [("AAA", 1, 2)]), folder=folder, today=day)
            self.assertFalse((Path(folder) / "nse_bhavcopy").exists())  # today's file may still be revised
            nse_live.clear_cache()
            with self.assertRaises(Block):  # a file for another date under this URL
                nse_live.cached_bhavcopy(date(2026, 11, 17), fetcher=lambda u: bhav_rows(day, [("AAA", 1, 2)]), folder=folder, today=day)
            path = Path(folder) / "nse_bhavcopy" / "sec_bhavdata_full_16112026.csv"; path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(b"<html>Resource not found</html>")
            good = bhav_rows(date(2026, 11, 16), [("AAA", 1, 2)])
            self.assertEqual(nse_live.cached_bhavcopy(date(2026, 11, 16), fetcher=lambda u: good, folder=folder, today=day), good)

    def test_movers_name_the_bhavcopy_and_say_rerun_when_drivers_are_missing(self):
        m = Market()
        def nse(url):
            if "sec_bhavdata_full" in url: raise Block("NSE: source refused the request")
            return m.nse(url)
        with NO_WAIT:
            r = mm.collect(nse_fetch=nse, flow_fetch=lambda u: FLOWS, yahoo_fetch=m.yahoo, clock=lambda: NOW, news=False)
        self.assertIn({"component": "previous-day bhavcopy", "status": "FAILED"}, r["coverage"])
        self.assertNotIn("constituents", [c["component"] for c in r["coverage"]])
        for text in (mm.summary(r), mm.markdown(r)):
            self.assertIn("⚠ stock contributions not collected — rerun (NSE's previous-day bhavcopy", text)
        self.assertIn("previous-day bhavcopy", mm.markdown(r).split("Not collected this run:")[1])

    def test_one_refusal_no_longer_loses_the_drivers(self):
        m, refused = Market(), []
        def nse(url):
            if "sec_bhavdata_full" in url and not refused:
                refused.append(url); raise Block("NSE: source refused the request")
            return m.nse(url)
        with NO_WAIT:
            r = mm.collect(nse_fetch=nse, flow_fetch=lambda u: FLOWS, yahoo_fetch=m.yahoo, clock=lambda: NOW, news=False)
        self.assertTrue(refused); self.assertIn("contributors", r); self.assertNotIn("drivers_missing", r)
        self.assertNotIn("⚠", mm.summary(r))


# ---------------------------------------------------------------- NSE's evening snapshot relabels previousDay as today

def snapshot(stamp, previous_day):
    rows = [{"key": "INDICES ELIGIBLE IN DERIVATIVES", "index": "NIFTY 50", "last": 25227.95, "variation": -159.45, "percentChange": -0.63,
             "previousClose": 25387.4, "open": 25323.15, "high": 25390.8, "low": 25141.8, "previousDay": previous_day},
            {"key": "BROAD MARKET INDICES", "index": "INDIA VIX", "last": 15.56, "variation": 0.04, "percentChange": 0.25,
             "previousClose": 15.52, "open": 15.52, "high": 16.03, "low": 14.59, "previousDay": previous_day}]
    return json.dumps({"data": rows, "timestamp": stamp}).encode()


class RelabelledPreviousDayTests(unittest.TestCase):
    def test_previous_day_equal_to_the_snapshot_day_is_dropped(self):
        s = nse_live.parse_all_indices(snapshot("18-Nov-2026 15:30", "18-Nov-2026"), AFTER)
        self.assertIsNone(s["indices"]["NIFTY 50"]["previous_day"]); self.assertTrue(s["previous_day_relabelled"])
        ok = nse_live.parse_all_indices(snapshot("18-Nov-2026 15:30", "17-Nov-2026"), AFTER)
        self.assertEqual(ok["indices"]["NIFTY 50"]["previous_day"], "2026-11-17"); self.assertFalse(ok["previous_day_relabelled"])

    def test_live_drivers_fall_back_to_the_calendar_not_todays_bhavcopy(self):
        nse_live.clear_cache()
        m, asked = Market(), []
        raw = json.loads(m.snapshot())
        for row in raw["data"]: row["previousDay"] = "18-Nov-2026"
        snap = nse_live.parse_all_indices(json.dumps(raw).encode(), NOW)
        def nse(url):
            asked.append(url)
            return holiday_json() if url == nse_live.HOLIDAYS else m.nse(url)
        d = mm.live_drivers(["NIFTY 50"], snap, nse_fetch=nse, yahoo_fetch=m.yahoo)
        self.assertIn(nse_live.BHAVCOPY.format("17112026"), asked)
        self.assertNotIn(nse_live.BHAVCOPY.format("18112026"), asked)
        self.assertIn("NIFTY 50", d["indices"])
        nse_live.clear_cache(); asked.clear()
        mm.live_drivers(["NIFTY 50"], snap, nse_fetch=nse, yahoo_fetch=m.yahoo, previous_session="2026-11-17")
        self.assertNotIn(nse_live.HOLIDAYS, asked)  # the caller's previous session is used first

    def test_missing_yahoo_charts_are_named_as_the_reason(self):
        nse_live.clear_cache()
        m = Market()
        snap = nse_live.parse_all_indices(m.snapshot(), NOW)
        def no_charts(url): raise Block("YAHOO: throttled")
        d = mm.live_drivers(["NIFTY 50"], snap, nse_fetch=m.nse, yahoo_fetch=no_charts, previous_session="2026-11-17")
        self.assertEqual(d["indices"], {})
        self.assertTrue(d["error"].startswith("Yahoo returned 0 of "), d["error"])
        report = {"phase": "close", "drivers": d, "context_errors": {}}
        self.assertIn("too few to estimate weights", daily.driver_gap(report, "nifty"))

    def test_previous_session_without_the_calendar_is_the_previous_weekday(self):
        def down(url): raise Block("NSE: source refused the request")
        nse_live.clear_cache()
        self.assertEqual(nse_live.previous_session(date(2026, 11, 16), fetcher=down), date(2026, 11, 13))
        nse_live.clear_cache()
        self.assertEqual(nse_live.previous_session(date(2026, 11, 11), fetcher=lambda u: holiday_json()), date(2026, 11, 9))  # 10 Nov holiday


# ---------------------------------------------------------------- daily: the drivers gap is printed, VIX official close

class DailyGapAndVixTests(unittest.TestCase):
    def report(self, drivers, phase="close", errors=None, vix=None):
        base = {"phase": phase, "indices": [{"index_id": "nifty", "status": "READY", "change_from_previous_close_points": -150.0}],
                "context_errors": errors or {}}
        if drivers is not None: base["drivers"] = drivers
        return base

    def test_gap_line_when_stock_drivers_are_missing(self):
        missing = {"at": "2026-11-18T15:30:00+05:30", "sectors_up": [], "sectors_down": [], "indices": {},
                   "error": "NSE previous-day bhavcopy unavailable after retries"}
        line = daily.driver_gap(self.report(missing), "nifty")
        self.assertTrue(line.startswith("⚠ stock contributions not collected — rerun (NSE previous-day bhavcopy unavailable after retries"))
        self.assertIsNone(daily.driver_gap(self.report({**missing, "indices": {"NIFTY 50": {"negative": []}}}), "nifty"))
        self.assertIsNone(daily.driver_gap(self.report(None), "nifty"))  # route without drivers (supplied input)
        self.assertIsNotNone(daily.driver_gap(self.report(None, errors={"drivers": "Sector and stock drivers unavailable this run"}), "nifty"))
        self.assertIsNone(daily.driver_gap(self.report(missing, phase="preopen"), "nifty"))
        self.assertIsNotNone(daily.driver_gap(self.report({**missing, "indices": {"NIFTY 50": {}}}), "banknifty"))

    def test_gap_line_reaches_full_and_brief_reports(self):
        from test_daily_round2 import build
        from test_daily_reports import INTRADAY
        def no_stocks(indices, snap, **kw):
            return {"at": snap["as_of"], "source": snap["page"], "sectors_up": [{"name": "Nifty Media", "change_pct": 0.5}],
                    "sectors_down": [{"name": "Nifty Metal", "change_pct": -1.7}], "sector_levels": {}, "indices": {},
                    "error": "NSE previous-day bhavcopy unavailable after retries"}
        for command, selected in (("nifty", ("nifty",)), ("india-daily", ("nifty", "sensex", "banknifty"))):
            r = daily.report(command, build(selected, drivers=no_stocks), INTRADAY)
            for brief in (False, True):
                self.assertIn("⚠ stock contributions not collected — rerun", daily.markdown(r, brief=brief), (command, brief))

    def test_after_the_close_vix_is_nse_official_close(self):
        from test_daily_enrich import FakeClosing, FakeNSE, yahoo_quote_fetcher
        from test_daily_close import after_close_fetcher
        from public_market import collect_daily
        nse_live.clear_cache()
        bundle = collect_daily(["nifty"], fetcher=after_close_fetcher(), clock=lambda: AFTER)
        daily.enrich_public(bundle, ["nifty"], AFTER, nse_fetch=FakeNSE(today_file=True), yahoo_fetch=yahoo_quote_fetcher(AFTER),
                            closing=FakeClosing(), drivers=lambda *a, **k: {"at": AFTER.isoformat(), "indices": {}, "sectors_up": [], "sectors_down": []},
                            records=lambda *a, **k: {}, news=False)
        self.assertEqual(bundle["vix"]["source"], "nse_ind_close_all")
        r = daily.report("nifty", bundle, AFTER, "close")
        from test_nse_live import close_value
        self.assertEqual(r["vix"]["close"], close_value("INDIA VIX", date(2026, 11, 18)))
        text = daily.markdown(r)
        self.assertIn("NSE official close)", text); self.assertIn("India VIX: NSE daily index report (official close).", text)

    def test_a_stalled_context_download_is_a_gap_not_a_hang(self):
        import threading, time as _time
        from test_daily_enrich import FakeClosing, FakeNSE, yahoo_quote_fetcher
        from test_daily_close import after_close_fetcher
        from public_market import collect_daily
        nse_live.clear_cache()
        release = threading.Event()
        def stalled(*a, **k):
            release.wait(5); raise Block("PUBLIC: stalled")
        bundle = collect_daily(["nifty"], fetcher=after_close_fetcher(), clock=lambda: AFTER)
        start = _time.perf_counter()
        try:
            with patch.object(daily, "CONTEXT_BUDGET", 0.2), patch.object(daily, "reference_chart", stalled):
                daily.enrich_public(bundle, ["nifty"], AFTER, nse_fetch=FakeNSE(today_file=True), yahoo_fetch=yahoo_quote_fetcher(AFTER),
                                    closing=FakeClosing(), drivers=lambda *a, **k: {"at": AFTER.isoformat(), "indices": {}, "sectors_up": [], "sectors_down": []},
                                    records=stalled, news=False)
        finally:
            release.set()
        self.assertLess(_time.perf_counter() - start, 3.0)
        self.assertNotIn("record_history", bundle["indices"]["nifty"]); self.assertNotIn("reference_bars", bundle)

    def test_cli_exit_does_not_wait_for_abandoned_downloads(self):
        import subprocess, time as _time
        code = ("import sys; sys.path.insert(0, r'%s'); from concurrent.futures import ThreadPoolExecutor; import time, daily; "
                "ThreadPoolExecutor(1).submit(time.sleep, 30); print('report'); daily.exit_now(0)") % (ROOT / "scripts")
        start = _time.perf_counter()
        r = subprocess.run([sys.executable, "-B", "-c", code], capture_output=True, text=True, timeout=60)
        self.assertEqual((r.returncode, r.stdout.strip()), (0, "report"))
        self.assertLess(_time.perf_counter() - start, 15.0)

    def test_frozen_yahoo_vix_candle_is_flagged(self):
        rows = [{"session": "2026-11-17", "close": 15.52}, {"session": "2026-11-18", "open": 15.52, "high": 16.03, "low": 14.59, "close": 15.52}]
        data = {"source": "yahoo_chart", "symbol": daily.VIX_SYMBOL, "as_of": AFTER.isoformat(), "rows": rows}
        v = daily.vix_report(data, AFTER, "close", "2026-11-18")
        self.assertTrue(v["possibly_stale"]); self.assertIn("may be stale", daily.vix_line(v))
        rows[-1]["close"] = 15.57
        self.assertFalse(daily.vix_report(data, AFTER, "close", "2026-11-18")["possibly_stale"])


# ---------------------------------------------------------------- 2/8. Yahoo previous close, Sensex row, VIX official close

def yahoo_daily(symbol, candles, price, at, chart_previous):
    q = {"open": [c[1] for c in candles], "high": [c[1] + 5 for c in candles], "low": [c[1] - 5 for c in candles], "close": [c[1] for c in candles]}
    meta = {"symbol": symbol, "currency": "INR", "regularMarketPrice": price, "regularMarketTime": int(at.timestamp()), "chartPreviousClose": chart_previous}
    return json.dumps({"chart": {"result": [{"meta": meta, "timestamp": [int(datetime(*c[0], 9, 15, tzinfo=IST).timestamp()) for c in candles],
                                              "indicators": {"quote": [q]}}]}}).encode()


class YahooPreviousCloseTests(unittest.TestCase):
    CANDLES = [((2026, 11, 16), 82104.80), ((2026, 11, 17), 82532.19), ((2026, 11, 18), 82031.81)]

    def test_previous_close_is_the_previous_daily_candle_not_chart_previous_close(self):
        at = datetime(2026, 11, 18, 15, 32, tzinfo=IST)
        fetch = lambda url: yahoo_daily("^BSESN", self.CANDLES, 82031.81, at, chart_previous=82104.80)
        q = nse_live.yahoo_quote("SENSEX", fetcher=fetch, clock=lambda: AFTER)
        self.assertEqual((q["previous_close"], q["previous_day"], q["change"], q["change_pct"]), (82532.19, "2026-11-17", -500.38, -0.61))
        self.assertIn("range=5d&interval=1d", nse_live.yahoo_quote.__doc__ + "range=5d&interval=1d")

    def test_movers_sensex_row_is_against_the_same_previous_session(self):
        m = Market()
        def yahoo(url):
            if "%5EBSESN" in url and "interval=1d" in url:
                return yahoo_daily("^BSESN", self.CANDLES, 82031.81, NOW, chart_previous=82104.80)
            return m.yahoo(url)
        nse_live.clear_cache()
        r = mm.collect(nse_fetch=m.nse, flow_fetch=lambda u: FLOWS, yahoo_fetch=yahoo, clock=lambda: NOW, news=False)
        row = next(x for x in r["indices"] if x["name"].startswith("Sensex"))
        self.assertEqual((row["change"], row["change_pct"]), (-500.38, -0.61))
        def skewed(url):
            if "%5EBSESN" in url and "interval=1d" in url:
                return yahoo_daily("^BSESN", self.CANDLES[:1] + self.CANDLES[2:], 82031.81, NOW, chart_previous=1.0)
            return m.yahoo(url)
        nse_live.clear_cache()
        r = mm.collect(nse_fetch=m.nse, flow_fetch=lambda u: FLOWS, yahoo_fetch=skewed, clock=lambda: NOW, news=False)
        self.assertFalse(any(x["name"].startswith("Sensex") for x in r["indices"]))
        self.assertTrue(any("Sensex row left out" in n for n in r["notes"]))


class ClosedMarket(Market):
    """The same synthetic market seen after the close: 15:30 snapshot, today's index file published with VIX's official close."""
    def snapshot(self):
        raw = json.loads(super().snapshot()); raw["timestamp"] = "18-Nov-2026 15:30"
        return json.dumps(raw).encode()

    def nse(self, url):
        if url == nse_live.INDEX_CLOSE.format("18112026"):
            return (b"Index Name,Index Date,Open Index Value,High Index Value,Low Index Value,Closing Index Value,Points Change,Change(%)\n"
                    b"India VIX,18-11-2026,15.52,16.025,14.5875,15.57,0.05,.29\n")
        return super().nse(url)


class MoversCloseTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        nse_live.clear_cache()
        cls.m = ClosedMarket()
        clock = lambda: AFTER
        cls.r = mm.collect(nse_fetch=cls.m.nse, flow_fetch=lambda u: FLOWS, yahoo_fetch=cls.m.yahoo, clock=clock, news=False)

    def test_vix_is_the_official_close_after_the_close(self):
        v = self.r["vix"]
        self.assertEqual((v["value"], v["change"], v["change_pct"]), (15.57, 0.05, 0.29))
        self.assertIn("India VIX 15.57, +0.05 (+0.29%) (NSE official close).", mm.summary(self.r))
        self.assertIn("| India VIX (official close) | 15.57 |", mm.markdown(self.r))

    def test_index_totals_are_not_printed_as_a_count_of_securities(self):
        raw = json.loads(self.m.snapshot()); raw.update(advances=2340, declines=7200, unchanged=10)
        nse_live.clear_cache()
        r = mm.collect(nse_fetch=lambda u: json.dumps(raw).encode() if u == nse_live.ALL_INDICES else self.m.nse(u),
                       flow_fetch=lambda u: FLOWS, yahoo_fetch=self.m.yahoo, clock=lambda: AFTER, news=False)
        self.assertNotIn("All NSE securities", mm.markdown(r)); self.assertNotIn("market_breadth", r)
        self.assertIn("not a count of securities", r["index_breadth_totals"]["scope"])


# ---------------------------------------------------------------- 6/8. flows dated to the session; FPI month to date; premise check

class FlowTests(unittest.TestCase):
    def test_a_past_session_never_shows_another_days_flows(self):
        m = Market(); nse_live.clear_cache()
        r = mm.collect("2026-11-16", nse_fetch=lambda u: NSDL if u == nse_live.NSDL_MONTHLY else m.nse(u) if "16112026" not in u else self.day16(m, u),
                       flow_fetch=lambda u: flows("17-Nov-2026"), yahoo_fetch=m.yahoo, clock=lambda: AFTER, news=False)
        self.assertNotIn("flows", r)
        text = mm.summary(r)
        self.assertIn("FII/DII for Mon 16 Nov 2026: not available - NSE's endpoint serves only the latest day (Tue 17 Nov 2026).", text)
        self.assertNotIn("come after the close", text); self.assertNotIn("+₹4,195.38", text)
        self.assertIn("the 17 Nov report (trades of 16 Nov): +₹5,107.61 cr", text)
        nse_live.clear_cache()
        same = mm.collect("2026-11-17", nse_fetch=m.nse, flow_fetch=lambda u: flows("17-Nov-2026"), yahoo_fetch=m.yahoo, clock=lambda: AFTER, news=False)
        self.assertEqual(same["flows"]["date"], "2026-11-17")
        self.assertIn("(provisional NSE data; NSE's figure for that session)", mm.summary(same))

    def test_a_past_session_reads_its_own_kept_charts(self):
        m = Market(); nse_live.clear_cache()
        with patch.object(mm, "yahoo_charts", wraps=mm.yahoo_charts) as charts:
            mm.collect("2026-11-16", nse_fetch=lambda u: NSDL if u == nse_live.NSDL_MONTHLY else m.nse(u) if "16112026" not in u else self.day16(m, u),
                       flow_fetch=lambda u: flows("17-Nov-2026"), yahoo_fetch=m.yahoo, clock=lambda: AFTER, news=False)
        kw = charts.call_args.kwargs
        self.assertEqual(kw["today"], date(2026, 11, 16))  # the session, not the calendar day of the run
        self.assertEqual((kw["now"].date(), kw["now"].hour), (date(2026, 11, 16), 23))  # after that session's close: final charts

    @staticmethod
    def day16(m, url):
        if url == nse_live.INDEX_CLOSE.format("16112026"):
            return (b"Index Name,Index Date,Open Index Value,High Index Value,Low Index Value,Closing Index Value,Points Change,Change(%)\n"
                    b"Nifty 50,16-11-2026,25150,25300,25100,25291.1,146.8,.58\n")
        if url == nse_live.BHAVCOPY.format("16112026"):
            return m.bhav(DAYS[1], 149, 74)
        raise nse_live.Missing("x")

    def test_nsdl_month_parses_negative_figures_and_checks_arithmetic(self):
        raw = nsdl_html([("02-Nov-2026", 100.0, 300.0, -150.0), ("03-Nov-2026", 500.0, 100.0, 400.0)])
        p = nse_live.parse_fpi_month(raw, AFTER)
        self.assertEqual([(r["reporting_date"], r["exchange_net_crore"], r["equity_net_crore"]) for r in p["rows"]],
                         [("2026-11-02", -200.0, -150.0), ("2026-11-03", 400.0, 400.0)])
        bad = raw.replace(b"(200.00)", b"(250.00)")
        with self.assertRaises(Block): nse_live.parse_fpi_month(bad, AFTER)
        with self.assertRaises(Block): nse_live.parse_fpi_month(nsdl_html([("19-Nov-2026", 1.0, 0.0, 1.0)]), AFTER)  # future
        self.assertTrue(nse_live.allowed(nse_live.NSDL_MONTHLY))
        self.assertFalse(nse_live.allowed(nse_live.NSDL_MONTHLY + "?x=1"))
        self.assertFalse(nse_live.allowed("https://www.fpi.nsdl.co.in/web/Reports/Latest.aspx"))

    def test_fpi_summary_and_indian_grouping(self):
        p = mm.fpi_summary(nse_live.parse_fpi_month(NSDL, AFTER))
        self.assertEqual((p["mtd_exchange_net_crore"], p["reports"], p["buying_reports"]), (16587.36, 3, 3))
        self.assertEqual(mm._lakh(100365.32), "+₹1,00,365.32"); self.assertEqual(mm._lakh(-4200), "-₹4,200.00"); self.assertEqual(mm._lakh(12.5), "+₹12.50")
        line = mm.fpi_line({"fpi": p, "mode": "live"})
        self.assertIn("month to date (NSDL, 3 reports 16-18 Nov; each covers the previous trading day): net +₹16,587.36 cr", line)

    def test_premise_check_corrects_fii_selling_when_they_bought(self):
        r = {"flows": {"date": "2026-11-17", "fii_net_crore": 4195.38}, "fpi": mm.fpi_summary(nse_live.parse_fpi_month(NSDL, AFTER))}
        line = mm.flow_premise_line(r, "selling")
        self.assertTrue(line.startswith("Correction: FIIs were net **buyers**, not sellers - FII/FPI net +₹4,195.38 cr on 17 Nov (NSE provisional)"), line)
        self.assertTrue(mm.flow_premise_line(r, "buying").startswith("Premise check: yes, FIIs were net buyers"))
        mixed = {**r, "flows": {"date": "2026-11-17", "fii_net_crore": -900.0}}
        self.assertTrue(mm.flow_premise_line(mixed, "selling").startswith("Premise check: mixed - FII/FPI net -₹900.00 cr"))
        self.assertIn("unverified", mm.flow_premise_line({}, "selling"))
        self.assertIsNone(mm.flow_premise_line(r, None))

    def test_correction_fits_the_days_direction(self):
        base = {"flows": {"date": "2026-11-20", "fii_net_crore": 10308.42, "dii_net_crore": -4859.8}}
        up = mm.flow_premise_line({**base, "direction": {"change": 286.3}}, "selling")
        self.assertIn("The market rose with them", up); self.assertNotIn("selling came from", up)
        down = mm.flow_premise_line({**base, "direction": {"change": -150.0}}, "selling")
        self.assertIn("Domestic institutions were the net sellers (DII net -₹4,859.80 cr)", down)
        plain = mm.flow_premise_line({"flows": {**base["flows"], "dii_net_crore": 50.0}, "direction": {"change": -150.0}}, "selling")
        self.assertIn("The selling came from elsewhere", plain)

    def test_fpi_context_waits_for_an_injected_source_regardless_of_load(self):
        import threading, time as _time
        joining, real_join = threading.Event(), mm._joined
        def join(handle, budget):
            joining.set(); return real_join(handle, budget)
        def slow(**kw):  # NSDL still answering when the report is ready to join it (a busy machine)
            joining.wait(10); _time.sleep(0.2); return nse_live.parse_fpi_month(NSDL, AFTER)
        with patch.object(mm, "FPI_BUDGET", 0.0), patch.object(nse_live, "fpi_month", slow), patch.object(mm, "_joined", join):
            m = Market(); nse_live.clear_cache()
            r = mm.collect(nse_fetch=lambda u: NSDL if u == nse_live.NSDL_MONTHLY else m.nse(u), flow_fetch=lambda u: flows(),
                           yahoo_fetch=m.yahoo, clock=lambda: NOW, news=False)
        self.assertIn("fpi", r)

    def test_premise_line_leads_the_summary(self):
        m = Market(); nse_live.clear_cache()
        r = mm.collect(nse_fetch=lambda u: NSDL if u == nse_live.NSDL_MONTHLY else m.nse(u), flow_fetch=lambda u: flows(),
                       yahoo_fetch=m.yahoo, clock=lambda: NOW, news=False)
        lines = mm.summary(r, flow_premise="selling").splitlines()
        self.assertTrue(lines[1].startswith("- Correction: FIIs were net **buyers**"), lines[1])
        self.assertIn("FPI equity via the exchanges, month to date", mm.summary(r))


# ---------------------------------------------------------------- 5. cues: Indian rupee, COMEX/MCX gold, Brent/WTI crude

class CueFilterTests(unittest.TestCase):
    def test_other_countries_currencies_and_markets_are_dropped(self):
        for headline, publisher in (("Rupee Closes at 276.55 Against US Dollar in the Interbank Market", "Bloom Pakistan"),
                                    ("Rupee closes at Rs 276.55 after one-paisa gain against US dollar", "UrduPoint"),
                                    ("Rupee records gain against US dollar", "Business Recorder"),
                                    ("Thai gold prices drop THB1,100 as bullion falls to THB60,750", "Nation Thailand"),
                                    ("Oman Crude Oil Price Drops by $0.54", "وكالة الأنباء العمانية"),
                                    ("Gold prices in Dubai fall today", "Gulf News"), ("Sri Lankan rupee weakens", "EconomyNext")):
            self.assertTrue(mm.foreign_cue(headline, publisher), headline)
        for headline, publisher in (("Rupee rises 7 paise to close at 93.44 against U.S. dollar", "The Hindu"),
                                    ("Gold Crashes To ₹1,31,313 On MCX", "Free Press Journal"),
                                    ("Brent Crude Slips To $91 Per Barrel As Supply Surplus Eases", "NDTV Profit"),
                                    ("Rupee Forwards Turn More Bullish Than Currency Forecasters", "Bloomberg.com")):
            self.assertFalse(mm.foreign_cue(headline, publisher), headline)
        # no country named anywhere: the rate itself (USD/INR trades near 90, PKR/LKR near 280-300) gives it away
        self.assertTrue(mm.foreign_cue("Rupee closes at 276.55 against US dollar in the interbank market", "Interbank Daily"))
        self.assertFalse(mm.foreign_cue("Rupee closes at 93.33 against US dollar", "Interbank Daily"))

    def test_market_cues_prefer_an_indian_publisher_and_skip_foreign_rupee(self):
        now = AFTER
        def item(title, publisher, minutes_ago):
            t = (now - timedelta(minutes=minutes_ago)).strftime("%a, %d %b %Y %H:%M:%S +0530")
            return f"<item><title>{title} - {publisher}</title><link>https://news.google.com/x{minutes_ago}</link><pubDate>{t}</pubDate></item>"
        feeds = {"rupee": [item("Rupee Closes at 276.55 Against US Dollar", "Bloom Pakistan", 5), item("Rupee records gain against US dollar", "Business Recorder", 10),
                           item("Rupee rises 7 paise to close at 93.44 against US dollar", "BusinessLine", 40)],
                 "gold": [item("Thai gold prices drop as bullion falls", "Nation Thailand", 3), item("Gold slips on MCX as dollar firms", "Some Blog", 20),
                          item("Gold falls Rs 3,000 on MCX as dollar firms", "Moneycontrol", 50)],
                 "crude": [item("Oman Crude Oil Price Drops by $0.54", "ONA", 2), item("Brent crude slips to $91 as stocks rise", "Reuters", 30)]}
        def fetch(url):
            key = "rupee" if "rupee" in url else "gold" if "gold" in url else "crude" if "crude" in url.lower() or "Brent" in url else None
            body = "".join(feeds.get(key, []))
            return f"<?xml version='1.0'?><rss><channel><title>x</title>{body}</channel></rss>".encode()
        cues = mm.market_cues(now, since=now - timedelta(hours=8), news_fetch=fetch)
        self.assertEqual(cues["Rupee"]["publisher"], "BusinessLine")
        self.assertEqual(cues["Gold"]["publisher"], "Moneycontrol")
        self.assertEqual(cues["Crude"]["publisher"], "Reuters")


# ---------------------------------------------------------------- 3. sector drill-down

METAL = [("SAIL", "Steel Authority of India Ltd.", 150.0, 143.6), ("HINDZINC", "Hindustan Zinc Ltd.", 500.0, 479.1),
         ("NATIONALUM", "National Aluminium Company Ltd.", 250.0, 240.0), ("VEDL", "Vedanta Ltd.", 450.0, 433.0),
         ("TATASTEEL", "Tata Steel Ltd.", 160.0, 157.8), ("JSL", "Jindal Stainless Ltd.", 700.0, 700.5)]


class SectorNSE:
    """NSE for the sector route: snapshot with sector rows, ind_niftymetallist.csv, bhavcopies for 17 Nov (and 18 Nov if published)."""
    def __init__(self, today_bhav=False, stamp="18-Nov-2026 15:30", previous_day="17-Nov-2026", refuse=()):
        self.today_bhav, self.stamp, self.previous_day, self.refuse, self.calls = today_bhav, stamp, previous_day, refuse, []

    def __call__(self, url):
        self.calls.append(url)
        if any(r in url for r in self.refuse): raise Block("NSE: source refused the request")
        if url == nse_live.ALL_INDICES:
            def row(name, pct, adv=None):
                prev = 1000.0; last = round(prev * (1 + pct / 100), 2)
                r = {"key": "SECTORAL INDICES", "index": name, "last": last, "variation": round(last - prev, 2), "percentChange": pct,
                     "previousClose": prev, "open": prev, "high": max(last, prev) + 1, "low": min(last, prev) - 1, "previousDay": self.previous_day}
                if adv: r.update(advances=str(adv[0]), declines=str(adv[1]), unchanged="0")
                return r
            rows = [row("NIFTY 50", -0.63), row("NIFTY METAL", -2.46, (1, 14)), row("NIFTY IT", -1.24), row("NIFTY PSU BANK", 0.59),
                    row("NIFTY CONSUMER DURABLES", -3.13), row("NIFTY PHARMA", 0.15)]
            return json.dumps({"data": rows, "timestamp": self.stamp}).encode()
        if url == nse_live.HOLIDAYS: return holiday_json()
        if url == nse_live.INDEX_LIST.format("niftymetal"):
            return ("Company Name,Industry,Symbol,Series,ISIN Code\n" + "".join(f"{n},Metals & Mining,{s},EQ,INE{k:03d}\n" for k, (s, n, _, _) in enumerate(METAL))
                    + "Dummy Ltd.,Metals & Mining,DUMMYMET,EQ,DUM000\n").encode()
        if url == nse_live.BHAVCOPY.format("17112026"):
            return bhav_rows(date(2026, 11, 17), [(s, p * 1.01, p) for s, _, p, _ in METAL])
        if url == nse_live.BHAVCOPY.format("18112026") and self.today_bhav:
            return bhav_rows(date(2026, 11, 18), [(s, p, c) for s, _, p, c in METAL])
        raise nse_live.Missing("NSE: no file for that date")


def quotes(prices, at=AFTER):
    def fetch(url):
        sym = url.split("/chart/")[1].split("?")[0].replace("%26", "&").removesuffix(".NS")
        if sym not in prices: raise Block("YAHOO: missing")
        meta = {"symbol": sym + ".NS", "regularMarketPrice": prices[sym], "regularMarketTime": int(at.timestamp())}
        return json.dumps({"chart": {"result": [{"meta": meta}]}}).encode()
    return fetch


class SectorRouteTests(unittest.TestCase):
    def setUp(self): nse_live.clear_cache()

    def test_natural_names_resolve_to_nse_sector_indices(self):
        for text, want in (("Nifty IT", "NIFTY IT"), ("IT stocks", "NIFTY IT"), ("it", "NIFTY IT"), ("metal stocks", "NIFTY METAL"),
                           ("Metals", "NIFTY METAL"), ("PSU banks", "NIFTY PSU BANK"), ("PSU bank stocks", "NIFTY PSU BANK"),
                           ("Bank Nifty", "NIFTY BANK"), ("banks", "NIFTY BANK"), ("private banks", "NIFTY PRIVATE BANK"),
                           ("real estate", "NIFTY REALTY"), ("auto", "NIFTY AUTO"), ("pharma shares", "NIFTY PHARMA"),
                           ("oil & gas", "NIFTY OIL & GAS"), ("FMCG", "NIFTY FMCG"), ("energy", "NIFTY ENERGY"), ("financial services", "NIFTY FINANCIAL SERVICES")):
            self.assertEqual(mm.resolve_sector(text)["index"], want, text)
        for bad in ("cement", "", "nifty", "the market"):
            with self.assertRaises(Block): mm.resolve_sector(bad)

    def test_official_bhavcopy_lists_every_constituent_sorted_with_rank_and_breadth(self):
        r = mm.sector_report("metal stocks", nse_fetch=SectorNSE(today_bhav=True), clock=lambda: AFTER, news=False)
        self.assertEqual([s["symbol"] for s in r["stocks"]], ["JSL", "TATASTEEL", "VEDL", "NATIONALUM", "HINDZINC", "SAIL"])
        self.assertEqual(r["stocks"][-1]["change_pct"], -4.27)
        self.assertEqual(r["breadth"], {"advances": 1, "declines": 5, "unchanged": 0})
        self.assertEqual((r["rank"]["position"], r["rank"]["of"]), (4, 5))  # 5 sector rows in the fixture; Metal 2nd weakest
        self.assertNotIn("DUMMYMET", [s["symbol"] for s in r["stocks"]] + r["missing"])  # an NSE placeholder is not a stock
        text = mm.sector_summary(r)
        self.assertTrue(text.startswith("Nifty Metal closed **down** 2.46% (-24.60 points) at 975.40 today, Wed 18 Nov 2026"), text)
        self.assertIn("the 2nd weakest of 5 NSE sector indices", text)
        self.assertIn("Fell most first, every constituent: SAIL -4.27%, HINDZINC -4.18%, NATIONALUM -4.00%, VEDL -3.78%, TATASTEEL -1.38%, JSL +0.07%.", text)
        self.assertIn("NSE's own count: 1 up, 14 down", text)
        self.assertIn("NSE bhavcopy of 18 Nov 2026: official close", text)
        self.assertIn("| SAIL | Steel Authority of India Ltd. | 143.60 | 150.00 | -4.27% |", mm.sector_markdown(r))

    def test_before_the_evening_bhavcopy_yahoo_prices_against_the_official_previous_close(self):
        prices = {s: c for s, _, _, c in METAL}; prices.pop("JSL")
        r = mm.sector_report("METAL", nse_fetch=SectorNSE(), quote_fetch=quotes(prices), clock=lambda: AFTER, news=False)
        self.assertEqual(r["missing"], ["JSL"]); self.assertEqual(r["stocks"][-1]["symbol"], "SAIL")
        self.assertIn("Yahoo last price (unofficial) vs NSE's official 17 Nov close (bhavcopy)", r["price_basis"])
        self.assertIn("this evening's bhavcopy", r["price_basis"])
        stale = mm.sector_report("METAL", nse_fetch=SectorNSE(), quote_fetch=quotes(prices, at=AFTER - timedelta(days=1)), clock=lambda: AFTER, news=False)
        self.assertEqual(stale["stocks"], [])  # quotes from another day are not today's moves
        self.assertIn("⚠ stock moves not collected — rerun", mm.sector_summary(stale))

    def test_relabelled_previous_day_uses_the_calendar_and_a_refused_bhavcopy_is_stated(self):
        nse = SectorNSE(previous_day="18-Nov-2026")
        prices = {s: c for s, _, _, c in METAL}
        r = mm.sector_report("METAL", nse_fetch=nse, quote_fetch=quotes(prices), clock=lambda: AFTER, news=False)
        self.assertEqual(r["previous_session"], "2026-11-17"); self.assertEqual(len(r["stocks"]), 6)
        nse_live.clear_cache()
        with NO_WAIT:
            r = mm.sector_report("METAL", nse_fetch=SectorNSE(refuse=("sec_bhavdata_full_17",)), quote_fetch=quotes(prices), clock=lambda: AFTER, news=False)
        self.assertIn("previous-day bhavcopy could not be downloaded after retries", mm.sector_summary(r))

    def test_cause_headlines_explanation_first_then_the_driver_and_foreign_or_old_items_dropped(self):
        now = AFTER
        def item(title, publisher, minutes_ago):
            t = (now - timedelta(minutes=minutes_ago)).strftime("%a, %d %b %Y %H:%M:%S +0530")
            return f"<item><title>{title} - {publisher}</title><link>https://news.google.com/{abs(hash(title))}</link><pubDate>{t}</pubDate></item>"
        sector = [item("Why is Russel Metals stock sliding today?", "Investing.com", 60),
                  item("NALCO, Hindustan Zinc, Jindal Steel: Why metal stocks are falling today", "Business Today", 150),
                  item("Metal stocks slide in afternoon trade", "Some Blog", 90),
                  item("Metal stocks to buy for 2027: top picks", "Livemint", 30),
                  item("Metal stocks rally to record high", "Old Wire", 26 * 60)]
        copper = [item("Copper sinks to five-month low as strong dollar weighs", "Reuters", 40), item("Copper rises in Shanghai", "SMM", 20)]
        def fetch(url):
            body = "".join(copper if "copper" in url.lower() else sector if "metal" in url.lower() else [])
            return f"<?xml version='1.0'?><rss><channel><title>x</title>{body}</channel></rss>".encode()
        r = mm.sector_report("metal", nse_fetch=SectorNSE(today_bhav=True), clock=lambda: AFTER, news_fetch=fetch)
        heads = [h["headline"] for h in r["headlines"]]
        self.assertEqual(heads[0], "NALCO, Hindustan Zinc, Jindal Steel: Why metal stocks are falling today")
        self.assertEqual(heads[1], "Copper sinks to five-month low as strong dollar weighs")
        for gone in ("Why is Russel Metals stock sliding today?", "Metal stocks to buy for 2027: top picks", "Copper rises in Shanghai", "Metal stocks rally to record high"):
            self.assertNotIn(gone, heads)
        self.assertIn("Possible causes (Google News headlines since the last close; unverified", mm.sector_summary(r))

    def test_cli_rejects_mixed_routes(self):
        import contextlib, io
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.assertEqual(mm.main(["--sector", "IT", "--period", "week"]), 2)
        self.assertIn("choose --sector or a period", out.getvalue())


# ---------------------------------------------------------------- 4. period comparison

PERIOD_CLOSES = {date(2026, 10, 30): {"Nifty 50": 24500.0, "Nifty Midcap 150": 22000.0, "Nifty Smallcap 250": 17500.0},
                 date(2026, 11, 13): {"Nifty 50": 25144.30, "Nifty Midcap 150": 22681.25, "Nifty Smallcap 250": 17940.55, "Nifty 500": 23734.2,
                                      "Nifty Bank": 61000.0, "Nifty Metal": 12647.9, "Nifty IT": 31931.15},
                 date(2026, 11, 18): {"Nifty 50": 25227.95, "Nifty Midcap 150": 22389.9, "Nifty Smallcap 250": 17667.5, "Nifty 500": 23630.25,
                                      "Nifty Bank": 62038.65, "Nifty Metal": 12222.3, "Nifty IT": 31262.3}}


class PeriodNSE:
    def __init__(self, today_file=True, holidays=()):
        self.today_file, self.holidays, self.calls = today_file, set(holidays), []

    def __call__(self, url):
        self.calls.append(url)
        if url == nse_live.HOLIDAYS: return holiday_json()
        if url == nse_live.ALL_INDICES:
            rows = [{"key": "X", "index": "NIFTY 50", "last": 25227.95, "variation": -159.45, "percentChange": -0.63, "previousClose": 25387.4,
                     "previousDay": "17-Nov-2026"},
                    {"key": "X", "index": "NIFTY MIDCAP 150", "last": 22389.9, "variation": -258.8, "percentChange": -1.14, "previousClose": 22648.7,
                     "previousDay": "17-Nov-2026"}]
            return json.dumps({"data": rows, "timestamp": "18-Nov-2026 15:30"}).encode()
        if "ind_close_all_" in url:
            d = datetime.strptime(url[-12:-4], "%d%m%Y").date()
            if d not in PERIOD_CLOSES or d in self.holidays or (d == date(2026, 11, 18) and not self.today_file): raise nse_live.Missing("x")
            head = "Index Name,Index Date,Open Index Value,High Index Value,Low Index Value,Closing Index Value,Points Change,Change(%)\n"
            return (head + "".join(f"{n},{d:%d-%m-%Y},{c},{c},{c},{c},0,0\n" for n, c in PERIOD_CLOSES[d].items())).encode()
        raise nse_live.Missing("x")


class PeriodTests(unittest.TestCase):
    def setUp(self): nse_live.clear_cache()

    def test_period_cut_dates(self):
        wed, mon = date(2026, 11, 18), date(2026, 11, 16)
        self.assertEqual(mm.period_cut("week", wed), mon); self.assertEqual(mm.period_cut("week", mon), mon)
        self.assertEqual(mm.period_cut("month", wed), date(2026, 11, 1)); self.assertEqual(mm.period_cut("ytd", wed), date(2026, 1, 1))
        self.assertEqual(mm.period_cut("since", wed, date(2026, 11, 13)), date(2026, 11, 14))
        with self.assertRaises(Block): mm.period_cut("since", wed)

    def test_this_week_is_since_fridays_close_with_official_closes(self):
        nse = PeriodNSE()
        r = mm.period_report("week", nse_fetch=nse, yahoo_fetch=lambda u: (_ for _ in ()).throw(Block("x")), clock=lambda: AFTER)
        self.assertEqual((r["base_session"], r["end_session"]), ("2026-11-13", "2026-11-18"))
        by = {x["index"]: x for x in r["rows"]}
        self.assertEqual((by["NIFTY 50"]["change_pct"], by["NIFTY MIDCAP 150"]["change_pct"], by["NIFTY SMALLCAP 250"]["change_pct"]), (0.33, -1.28, -1.52))
        self.assertEqual(by["NIFTY MIDCAP 150"]["vs_nifty_pp"], -1.61)
        self.assertEqual(sum(x["index"] == "NIFTY BANK" for x in r["rows"]), 1)
        text = mm.period_summary(r)
        self.assertTrue(text.startswith("**This week** (Fri 13 Nov close to Wed 18 Nov close): Nifty 50 +0.33% (25,144.30 to 25,227.95)."), text)
        self.assertIn("Nifty Midcap 150 -1.28% (1.61 percentage points worse than Nifty 50)", text)
        self.assertIn("both lagged the Nifty", text)
        self.assertIn("dated 13 Nov and 18 Nov 2026", text)
        self.assertIn("weakest Nifty Metal -3.36%", text)
        self.assertIn("| Nifty Smallcap 250 | 17,940.55 | 17,667.50 | -1.52% | -1.85 |", mm.period_markdown(r))

    def test_holiday_friday_moves_the_base_back_and_month_uses_the_last_close_of_october(self):
        nse = PeriodNSE(holidays={date(2026, 11, 13)})
        PERIOD_CLOSES[date(2026, 11, 12)] = PERIOD_CLOSES[date(2026, 11, 13)]
        try:
            r = mm.period_report("week", nse_fetch=nse, yahoo_fetch=lambda u: (_ for _ in ()).throw(Block("x")), clock=lambda: AFTER)
            self.assertEqual(r["base_session"], "2026-11-12")
        finally:
            del PERIOD_CLOSES[date(2026, 11, 12)]
        nse_live.clear_cache()
        m = mm.period_report("month", nse_fetch=PeriodNSE(), yahoo_fetch=lambda u: (_ for _ in ()).throw(Block("x")), clock=lambda: AFTER)
        self.assertEqual(m["base_session"], "2026-10-30")  # 31 Oct was a Saturday

    def test_end_falls_back_to_nse_level_when_todays_file_is_not_out(self):
        r = mm.period_report("week", nse_fetch=PeriodNSE(today_file=False), yahoo_fetch=lambda u: (_ for _ in ()).throw(Block("x")), clock=lambda: AFTER)
        self.assertEqual(r["end_basis"], "NSE closing level (15:30 snapshot)")
        self.assertIn("the end is the NSE closing level (15:30 snapshot) of 18 Nov", mm.period_summary(r))
        self.assertEqual({x["index"] for x in r["rows"]}, {"NIFTY 50", "NIFTY MIDCAP 150"})

    def test_sensex_from_yahoo_only_with_both_exact_dates(self):
        def yahoo(url):
            stamps = [int(datetime(2026, 11, d, 9, 15, tzinfo=IST).timestamp()) for d in (13, 18)]
            return json.dumps({"chart": {"result": [{"meta": {"symbol": "^BSESN"}, "timestamp": stamps,
                                                     "indicators": {"quote": [{"close": [81589.0, 82031.81]}]}}]}}).encode()
        r = mm.period_report("week", nse_fetch=PeriodNSE(), yahoo_fetch=yahoo, clock=lambda: AFTER)
        self.assertEqual(r["sensex"]["change_pct"], 0.54)
        self.assertIn("Sensex (Yahoo daily closes, unofficial): +0.54%.", mm.period_summary(r))


# ---------------------------------------------------------------- index lists: cached, labelled when stale

class IndexListTests(unittest.TestCase):
    def setUp(self): nse_live.clear_cache()

    def test_fresh_copy_reused_stale_copy_labelled_when_download_fails(self):
        csv_text = b"Company Name,Industry,Symbol,Series,ISIN Code\nA Ltd.,IT,AAA,EQ,INE1\nB Ltd.,IT,BBB,EQ,INE2\nDummy,IT,DUMMYX,EQ,DUM1\n"
        with tempfile.TemporaryDirectory() as folder:
            got = nse_live.index_members("niftyit", fetcher=lambda u: csv_text, folder=folder, today=date(2026, 11, 18))
            self.assertEqual([m["symbol"] for m in got["members"]], ["AAA", "BBB"])
            nse_live.clear_cache()
            def down(url): raise Block("NSE: source refused the request")
            with NO_WAIT:
                again = nse_live.index_members("niftyit", fetcher=down, folder=folder, today=date(2026, 11, 18))
                self.assertIn("cached copy", again["basis"])
                old = nse_live.index_members("niftyit", fetcher=down, folder=folder, today=date(2026, 12, 18))
            self.assertIn("today's download failed", old["basis"])
        with self.assertRaises(Block): nse_live.index_members("../x", fetcher=lambda u: csv_text)
        self.assertTrue(nse_live.allowed(nse_live.INDEX_LIST.format("nifty_privatebank")))


# ---------------------------------------------------------------- 0. backoff only against the network; a fake never falls through to it

class OfflineRetryTests(unittest.TestCase):
    def setUp(self): nse_live.clear_cache()

    def test_backoff_applies_to_the_live_fetch_only(self):
        self.assertEqual(nse_live.retry_delays(nse_live._LIVE_FETCH), nse_live.RETRY_DELAYS)
        self.assertEqual(nse_live.retry_delays(None), nse_live.RETRY_DELAYS)
        self.assertEqual(nse_live.retry_delays(lambda u: b""), (0.0, 0.0))
        calls, waits = [], []
        def flaky(url):
            calls.append(url)
            if len(calls) < 3: raise Block("NSE: source refused the request")
            return b"ok"
        self.assertEqual(nse_live.fetch_retry(flaky, "u", delays=(0.0, 0.0), sleep=waits.append), b"ok")
        self.assertEqual((len(calls), waits), (3, []))  # retried, never slept

    def test_injected_refusals_cost_no_wall_clock_time(self):
        import time as _time
        day = date(2026, 11, 17); tries = []
        def refused(url): tries.append(url); raise Block("NSE: source refused the request")
        with patch.object(nse_live.time, "sleep", side_effect=AssertionError("slept")), \
             patch.object(nse_live, "fetch_retry", wraps=nse_live.fetch_retry) as retry:
            start = _time.perf_counter()
            with self.assertRaises(Block): nse_live.cached_bhavcopy(day, fetcher=refused, today=date(2026, 11, 18))
            self.assertLess(_time.perf_counter() - start, 0.5)
        self.assertEqual(len(tries), 1 + len(nse_live.RETRY_DELAYS)); self.assertTrue(retry.called)

    def test_a_patched_nse_fetch_is_used_and_the_network_and_user_cache_are_never_touched(self):
        day = date(2026, 11, 17); raw = bhav_rows(day, [("AAA", 100, 101)])
        fake = lambda url: raw
        boom = AssertionError("network or user cache used")
        with patch.object(nse_live, "fetch", fake), patch.object(nse_live, "_bhav_fetch", side_effect=boom), \
             patch.object(nse_live, "cache_dir", side_effect=boom):
            self.assertEqual(mm.bhavcopy(day, fetcher=nse_live.fetch)["prices"]["AAA"]["close"], 101.0)


class StalledSourceTests(unittest.TestCase):
    def test_a_dripping_download_is_cut_off_at_the_total_limit(self):
        import io as _io
        class Drip:
            def __init__(self): self.n = 0
            def read(self, size):
                self.n += 1; return b"x"  # never ends, each read well inside a socket timeout
        clock = iter(range(0, 1000, 1))
        with patch.object(nse_live.time, "monotonic", lambda: next(clock)):
            with self.assertRaises(Block): nse_live.read_within(Drip(), 10 ** 6, total=30)
        self.assertEqual(nse_live.read_within(_io.BytesIO(b"abc" * 50000), 10 ** 6, total=30), b"abc" * 50000)
        self.assertEqual(len(nse_live.read_within(_io.BytesIO(b"z" * 200000), 100, total=30)), 65536)  # over the limit: caller rejects

    def test_a_stalled_call_becomes_a_block_quickly(self):
        import threading, time as _time
        release = threading.Event()
        start = _time.perf_counter()
        try:
            with self.assertRaises(Block): nse_live.bounded_call(release.wait, 0.2, 5)
        finally:
            release.set()
        self.assertLess(_time.perf_counter() - start, 2.0)
        self.assertEqual(nse_live.bounded_call(lambda x: x + 1, 1.0, 1), 2)
        with patch("public_market.request", side_effect=lambda url: _time.sleep(1) or b"{}"), patch.object(nse_live, "CALL_BUDGET", 0.2):
            with self.assertRaises(Block): nse_live.yahoo_quote("SENSEX", clock=lambda: AFTER)


# ---------------------------------------------------------------- 2. Yahoo VIX fallback: today's quote against the previous close

def vix_chart(at, candles, price):
    stamps = [int(datetime(*d, 9, 15, tzinfo=IST).timestamp()) for d, _ in candles]
    q = {k: [] for k in ("open", "high", "low", "close", "volume")}
    for _, (o, h, l, c) in candles:
        q["open"].append(o); q["high"].append(h); q["low"].append(l); q["close"].append(c); q["volume"].append(0)
    meta = {"symbol": "^INDIAVIX", "currency": "INR", "exchangeTimezoneName": "Asia/Kolkata", "dataGranularity": "1d",
            "regularMarketPrice": price, "regularMarketTime": int(at.timestamp()), "regularMarketDayHigh": 16.03,
            "regularMarketDayLow": 14.59, "chartPreviousClose": 18.68}
    return json.dumps({"chart": {"error": None, "result": [{"meta": meta, "timestamp": stamps, "indicators": {"quote": [q]}}]}}).encode()


class YahooVixTests(unittest.TestCase):
    # 18 Nov 2026 after the close: Yahoo's 18 Nov candle frozen at the open (15.52 = the 17 Nov close); its quote said 15.57.
    CANDLES = [((2026, 11, 16), (15.5, 15.94, 13.6, 15.68)), ((2026, 11, 17), (15.68, 15.68, 14.04, 15.52)),
               ((2026, 11, 18), (15.52, 16.03, 14.59, 15.52))]

    def test_frozen_candle_is_replaced_by_the_quote_against_the_previous_close(self):
        at = datetime(2026, 11, 18, 15, 43, tzinfo=IST)
        data = daily.collect_vix(fetcher=lambda url: vix_chart(at, self.CANDLES, 15.57), clock=lambda: AFTER)
        self.assertEqual(data["rows"][-2:][0], {"session": "2026-11-17", "open": 15.68, "high": 15.68, "low": 14.04, "close": 15.52})
        self.assertEqual(data["rows"][-1]["close"], 15.57)
        v = daily.vix_report(data, AFTER, "close", "2026-11-18")
        self.assertEqual((v["close"], v["previous_close"], v["change_points"], v["change_pct"]), (15.57, 15.52, 0.05, 0.322))
        self.assertFalse(v["possibly_stale"])
        line = daily.vix_line(v)
        self.assertIn("15.57", line); self.assertNotIn("+0.00", line); self.assertNotIn("NSE official close", line)

    def test_quote_used_only_when_its_previous_day_is_the_charts_previous_candle(self):
        at = datetime(2026, 11, 18, 15, 43, tzinfo=IST)
        def fetch(url):  # the 5-day quote response lacks the 17 Nov candle, so its base (16 Nov) is not the chart's
            return vix_chart(at, self.CANDLES[:1] + self.CANDLES[2:] if "range=5d" in url else self.CANDLES, 15.57)
        data = daily.collect_vix(fetcher=fetch, clock=lambda: AFTER)
        self.assertEqual(data["rows"][-1]["close"], 15.52); self.assertNotIn("basis", data)

    def test_a_quote_from_another_day_leaves_the_candles_alone(self):
        at = datetime(2026, 11, 17, 15, 43, tzinfo=IST)  # stale quote: older than the last candle
        data = daily.collect_vix(fetcher=lambda url: vix_chart(at, self.CANDLES, 15.4), clock=lambda: AFTER)
        self.assertEqual(data["rows"][-1]["close"], 15.52); self.assertNotIn("basis", data)
        self.assertTrue(daily.vix_report(data, AFTER, "close", "2026-11-18")["possibly_stale"])


# ---------------------------------------------------------------- 3. sector headlines: the sector's stocks, not a commodity word

class SectorHeadlineFilterTests(unittest.TestCase):
    def test_names_sector_needs_the_sector_as_stocks_or_index(self):
        for text in ("Metal stocks slide as copper falls", "Nifty Metal falls 2%", "IT shares extend decline", "Sensex falls as IT stocks drag",
                     "Tech stocks drag the market", "PSU banks rally on RBI move", "Bank Nifty hits record"):
            label_name = ("Nifty Metal" if "etal" in text or "copper" in text else "Nifty PSU Bank" if "PSU" in text
                          else "Nifty Bank" if "Bank" in text else "Nifty IT")
            self.assertTrue(mm.names_sector(text, label_name), text)
        for text, label_name in (("KGHM beats quarterly profit estimates on high metal prices", "Nifty Metal"),
                                 ("Red metal feels the blues as copper prices hit 7-month low", "Nifty Metal"),
                                 ("US banks report earnings", "Nifty PSU Bank"), ("It is a good day", "Nifty IT")):
            self.assertFalse(mm.names_sector(text, label_name), text)

    def test_foreign_company_results_and_other_sectors_dropped_from_a_metal_rise(self):
        up = datetime(2026, 11, 19, 17, 20, tzinfo=IST)
        def item(title, publisher, minutes_ago):
            t = (up - timedelta(minutes=minutes_ago)).strftime("%a, %d %b %Y %H:%M:%S +0530")
            return f"<item><title>{title} - {publisher}</title><link>https://news.google.com/{abs(hash(title))}</link><pubDate>{t}</pubDate></item>"
        copper = [item("KGHM beats quarterly profit estimates on high metal prices", "Kitco", 600),
                  item("Copper rebounds after crashing to lowest level since April 2025", "CNBC TV18", 500),
                  item("Red metal feels the blues! Voltas, Blue Star, other AC stocks jump up to 6% as copper prices hit 7-month low", "The Economic Times", 400),
                  item("AA (NYSE:AA) Production Records and Smelter Restarts Lift Alcoa as Metal Prices Ease", "Kalkine Media", 10),
                  item("Alcoa (NYSE:AA) rallies as aluminium prices climb", "Kalkine Media", 5),
                  item("Polycab India gains as margins improve on firm copper prices", "Business Standard", 6)]
        sector = [item("Metal stocks rise as copper recovers; Tata Steel, Hindalco gain", "Moneycontrol", 300)]
        def fetch(url):
            body = "".join(copper if "copper" in url.lower() else sector if "metal" in url.lower() else [])
            return f"<?xml version='1.0'?><rss><channel><title>x</title>{body}</channel></rss>".encode()
        rows = [{"symbol": s, "name": n, "change_pct": 1.0} for s, n, _, _ in METAL]
        spec = mm.resolve_sector("metal")
        items, failed = mm.sector_news(spec, rows, up, change=77.45, since=up - timedelta(hours=26), session="2026-11-19", news_fetch=fetch)
        heads = [h["headline"] for h in items]
        self.assertEqual(heads[:2], ["Metal stocks rise as copper recovers; Tata Steel, Hindalco gain",
                                     "Copper rebounds after crashing to lowest level since April 2025"])
        for gone in ("KGHM", "Voltas", "Alcoa", "NYSE", "Polycab"):
            self.assertFalse(any(gone in h for h in heads), (gone, heads))
        copper[:] = [item("Alcoa (NYSE:AA) rallies as aluminium prices climb", "Kalkine Media", 5)]  # the only driver headline
        items, _ = mm.sector_news(spec, rows, up, change=77.45, since=up - timedelta(hours=26), session="2026-11-19", news_fetch=fetch)
        self.assertFalse(any("Alcoa" in h["headline"] for h in items), items)
        copper[:] = [item("Polycab India gains as margins improve on firm copper prices", "Business Standard", 6)]  # another company's story
        items, _ = mm.sector_news(spec, rows, up, change=77.45, since=up - timedelta(hours=26), session="2026-11-19", news_fetch=fetch)
        self.assertFalse(any("Polycab" in h["headline"] for h in items), items)

    def test_stock_story_against_todays_move_and_bigger_than_it_is_dropped(self):
        h = "179% profit growth, yet down nearly 19%: Is Hindustan Copper worth a closer look?"
        self.assertEqual(mm.headline_direction(h), "down")
        self.assertTrue(mm.opposite_bigger_move(h, 1.49))
        self.assertFalse(mm.opposite_bigger_move(h, -18.5))  # on a day it did fall that much, it is today's story
        self.assertFalse(mm.opposite_bigger_move("Hindustan Copper falls 1% as copper slips", 1.49))
        self.assertFalse(mm.opposite_bigger_move("Hindustan Copper rises 2% as copper rebounds", 1.49))

    def test_forecasts_tips_and_social_posts_are_not_causes(self):
        for h in ("Gold adds resilience despite near-term headwinds, copper will hit $15,500/t in 2027 - UBS",
                  "Stock Of The Day Buy HDFC Bank Futures on decline: SL 800 Tgt 828, 835, 842", "Copper price forecast for next week",
                  "Stock of the day: Tata Motors", "Buy ICICI Bank, stop loss 1,250, target 1,340"):
            self.assertTrue(mm._outlook(h), h)
        self.assertFalse(mm._outlook("Copper rebounds after crashing to lowest level since April 2025"))
        import india_news
        now = datetime(2026, 11, 20, 19, 0, tzinfo=IST)
        def item(title, publisher):
            return f"<item><title>{title} - {publisher}</title><link>https://news.google.com/{abs(hash(title))}</link><pubDate>{now:%a, %d %b %Y %H:%M:%S +0530}</pubDate></item>"
        body = "".join([item("Closing Bell: Sensex settles 1,500 pts higher", "LinkedIn"), item("HDFC Bank tip of the day", "Facebook"),
                        item("Sensex jumps 900 points as banks rally", "Business Standard")])
        rss = f"<?xml version='1.0'?><rss><channel><title>x</title>{body}</channel></rss>".encode()
        got = india_news.search("Sensex", 24, fetcher=lambda url: rss, clock=lambda: now)
        self.assertEqual([h["publisher"] for h in got], ["Business Standard"])

    def test_cli_takes_a_multi_word_sector_unquoted(self):
        import contextlib, io
        seen = []
        def fake(text, **kw):
            seen.append(text); raise Block("MOVERS: stop here")
        out = io.StringIO()
        with patch.object(mm, "sector_report", side_effect=fake), contextlib.redirect_stdout(out):
            self.assertEqual(mm.main(["--sector", "metal", "stocks", "--summary"]), 2)
        self.assertEqual(seen, ["metal stocks"])


if __name__ == "__main__":
    unittest.main()
