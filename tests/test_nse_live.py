"""NSE live index levels, holiday calendar/expiries and dated index-close history. Offline fakes only."""
from datetime import date, datetime, timedelta
import io
import json
from pathlib import Path
import sys
import unittest
from unittest.mock import patch
import urllib.error

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
from core import Block, IST, instant
import nse_live

NOW = instant("2026-11-18T09:45:30+05:30")
HOLIDAYS = [("02-Oct-2026", "Mahatma Gandhi Jayanti"), ("20-Oct-2026", "Dussehra"), ("08-Nov-2026", "Diwali Laxmi Pujan*"),
            ("10-Nov-2026", "Diwali-Balipratipada"), ("24-Nov-2026", "Prakash Gurpurb Sri Guru Nanak Dev"), ("25-Dec-2026", "Christmas")]


def holiday_json():
    rows = [{"tradingDate": d, "weekDay": "", "description": t, "Sr_no": n} for n, (d, t) in enumerate(HOLIDAYS, 1)]
    return json.dumps({"CM": rows, "FO": rows, "CD": []}).encode()


def all_indices_json(stamp="18-Nov-2026 09:45", nifty=(25233.5, 25387.4), bank=(61898.85, 62273.25), vix=(15.81, 15.52), extra=()):
    def row(name, last, prev, key="INDICES ELIGIBLE IN DERIVATIVES", adv=None):
        change = round(last - prev, 2)
        r = {"key": key, "index": name, "indexSymbol": name, "last": last, "variation": change, "percentChange": round(change / prev * 100, 2),
             "open": prev, "high": max(last, prev) + 5, "low": min(last, prev) - 5, "previousClose": prev, "previousDay": "17-Nov-2026"}
        if adv: r.update(advances=str(adv[0]), declines=str(adv[1]), unchanged=str(adv[2]))
        return r
    rows = [row("NIFTY 50", *nifty, adv=(6, 44, 0)), row("NIFTY BANK", *bank, adv=(2, 10, 0)),
            row("INDIA VIX", *vix, key="BROAD MARKET INDICES"), row("NIFTY IT", 31465.95, 31654.1, key="SECTORAL INDICES"),
            row("NIFTY METAL", 12342.85, 12530.8, key="SECTORAL INDICES"), row("NIFTY PHARMA", 28231.55, 28158.3, key="SECTORAL INDICES")]
    rows += [row(*e) for e in extra]
    return json.dumps({"data": rows, "timestamp": stamp, "advances": 1434, "declines": 8108, "unchanged": 43}).encode()


def sessions_until(end, count, cal_days=None):
    """Weekday sessions not in HOLIDAYS plus the 8 Nov special session, newest last."""
    listed = {datetime.strptime(d, "%d-%b-%Y").date() for d, _ in HOLIDAYS}
    out, d = [], end
    while len(out) < count:
        if d == date(2026, 11, 8) or (d.weekday() < 5 and d not in listed): out.append(d)
        d -= timedelta(days=1)
    return list(reversed(out))


def close_value(name, d):
    base = {"NIFTY 50": 24000.0, "NIFTY BANK": 57000.0, "INDIA VIX": 15.0}[name]
    return round(base + (d - date(2026, 10, 1)).days * 10, 2)


def index_csv(d):
    lines = ["Index Name,Index Date,Open Index Value,High Index Value,Low Index Value,Closing Index Value,Points Change,Change(%),Volume"]
    for name, label in (("NIFTY 50", "Nifty 50"), ("NIFTY BANK", "Nifty Bank"), ("INDIA VIX", "India VIX"), ("NIFTY IT", "Nifty IT")):
        close = close_value(name if name in ("NIFTY 50", "NIFTY BANK", "INDIA VIX") else "NIFTY 50", d)
        lines.append(f"{label},{d:%d-%m-%Y},{close - 20},{close + 30},{close - 40},{close},10,0.04,-")
    return ("\n".join(lines) + "\n").encode()


class FakeNSE:
    """Serves the allowlisted NSE URLs from fixtures and records every request."""
    def __init__(self, snapshot=None, today_file=False, missing=(), fail=()):
        self.calls, self.snapshot, self.today_file, self.missing, self.fail = [], snapshot, today_file, set(missing), set(fail)

    def __call__(self, url):
        self.calls.append(url)
        if not nse_live.allowed(url): raise Block("NSE: unsupported endpoint")
        if any(f in url for f in self.fail): raise Block("NSE: source refused the request")
        if url == nse_live.HOLIDAYS: return holiday_json()
        if url == nse_live.ALL_INDICES: return self.snapshot or all_indices_json()
        if "ind_close_all_" in url:
            d = datetime.strptime(url[-12:-4], "%d%m%Y").date()
            valid = set(sessions_until(date(2026, 11, 18) if self.today_file else date(2026, 11, 17), 40))
            if d not in valid or d in self.missing: raise nse_live.Missing("NSE: no file for that date")
            return index_csv(d)
        raise nse_live.Missing("NSE: no file for that date")


class AllowlistTests(unittest.TestCase):
    def test_only_exact_nse_urls_are_allowed(self):
        for url in (nse_live.ALL_INDICES, nse_live.HOLIDAYS, nse_live.INDEX_CLOSE.format("17112026"), nse_live.NIFTY50_LIST,
                    nse_live.BHAVCOPY.format("17112026")):
            self.assertTrue(nse_live.allowed(url), url)
        for url in ("https://www.nseindia.com/api/allIndices?x=1", "http://www.nseindia.com/api/allIndices", "https://evil.example/api/allIndices",
                    "https://www.nseindia.com/api/quote-equity?symbol=RELIANCE", "https://nsearchives.nseindia.com/content/indices/ind_close_all_1711.csv",
                    "https://user@www.nseindia.com/api/allIndices", "https://www.nseindia.com:8443/api/allIndices"):
            self.assertFalse(nse_live.allowed(url), url)
            with self.assertRaises(Block): nse_live.fetch(url)

    def test_api_request_goes_to_the_exact_in_twin_without_redirects_and_with_size_limit(self):
        seen = []
        class Opener:
            def __init__(self, body): self.body = body
            def open(self, req, timeout):
                seen.append((req.full_url, timeout))
                class R(io.BytesIO):
                    def __enter__(s): return s
                    def __exit__(s, *a): return False
                return R(self.body)
        with patch("urllib.request.build_opener", return_value=Opener(b"{}")) as build:
            self.assertEqual(nse_live.fetch(nse_live.ALL_INDICES), b"{}")
            self.assertIsInstance(build.call_args.args[0], nse_live.NoRedirect)
        self.assertEqual(seen[0], ("https://www.nseindia.in/api/allIndices", nse_live.TIMEOUT))
        with patch("urllib.request.build_opener", return_value=Opener(b"x" * (nse_live.LIMIT + 1))):
            with self.assertRaises(Block): nse_live.fetch(nse_live.ALL_INDICES)
        with self.assertRaises(Block): nse_live.NoRedirect().redirect_request(None, None, 301, "", {}, "https://elsewhere")

    def test_http_404_means_missing_file_other_errors_block(self):
        class Opener:
            def __init__(self, code): self.code = code
            def open(self, req, timeout): raise urllib.error.HTTPError(req.full_url, self.code, "x", {}, None)
        with patch("urllib.request.build_opener", return_value=Opener(404)):
            with self.assertRaises(nse_live.Missing): nse_live.fetch(nse_live.INDEX_CLOSE.format("24112026"))
        with patch("urllib.request.build_opener", return_value=Opener(403)):
            with self.assertRaises(Block) as caught: nse_live.fetch(nse_live.ALL_INDICES)
            self.assertNotIsInstance(caught.exception, nse_live.Missing)


class LiveTests(unittest.TestCase):
    def setUp(self): nse_live.clear_cache()

    def test_snapshot_has_nse_timestamp_levels_breadth_and_rejects_bad_rows(self):
        bad = ("NIFTY FAKE", 100.0, 90.0)
        raw = json.loads(all_indices_json(extra=[bad])); raw["data"][-1]["variation"] = 99  # arithmetic mismatch
        snap = nse_live.parse_all_indices(json.dumps(raw).encode(), NOW)
        self.assertEqual(snap["as_of"], "2026-11-18T09:45:00+05:30"); self.assertEqual(snap["page"], nse_live.LIVE_PAGE)
        nifty = snap["indices"]["NIFTY 50"]
        self.assertEqual((nifty["last"], nifty["previous_close"], nifty["advances"], nifty["declines"]), (25233.5, 25387.4, 6, 44))
        self.assertEqual(snap["rejected_rows"], ["NIFTY FAKE"]); self.assertNotIn("NIFTY FAKE", snap["indices"])
        with self.assertRaises(Block): nse_live.parse_all_indices(all_indices_json(stamp="18-Nov-2026 10:00"), NOW)

    def test_quote_prefers_nse_and_falls_back_to_yahoo_meta_price_labelled(self):
        q = nse_live.quote("nifty", fetcher=FakeNSE(), clock=lambda: NOW)
        self.assertEqual((q["provider"], q["last"], q["source"]), ("NSE", 25233.5, nse_live.LIVE_PAGE))
        meta = {"symbol": "^NSEI", "currency": "INR", "regularMarketPrice": 25240.0, "chartPreviousClose": 25387.4,
                "regularMarketTime": int(NOW.timestamp()) - 30, "regularMarketDayHigh": 25390.0, "regularMarketDayLow": 25200.0}
        yahoo = lambda url: json.dumps({"chart": {"result": [{"meta": meta}]}}).encode()
        q = nse_live.quote("nifty", fetcher=FakeNSE(fail=["allIndices"]), yahoo_fetcher=yahoo, clock=lambda: NOW)
        self.assertEqual((q["provider"], q["last"], q["fallback_reason"]), ("Yahoo", 25240.0, "NSE live index data unavailable"))
        self.assertAlmostEqual(q["change"], -147.4)


class CalendarTests(unittest.TestCase):
    def setUp(self):
        nse_live.clear_cache(); self.cal = nse_live.holiday_calendar(fetcher=FakeNSE(), clock=lambda: NOW)

    def test_holidays_names_special_sessions_and_trading_days(self):
        self.assertEqual(self.cal["special_sessions"], ["2026-11-08"])
        self.assertFalse(nse_live.is_trading_day(self.cal, date(2026, 11, 24)))
        self.assertEqual(nse_live.next_trading_day(self.cal, date(2026, 11, 23)), date(2026, 11, 25))
        self.assertEqual(nse_live.previous_trading_day(self.cal, date(2026, 11, 11)), date(2026, 11, 9))
        self.assertIsNone(nse_live.holiday_name(self.cal, date(2026, 11, 8)))  # Muhurat session, not a closure
        with self.assertRaises(Block): nse_live.is_trading_day(self.cal, date(2027, 1, 4))

    def test_monthly_expiry_moves_back_over_guru_nanak_jayanti(self):
        rows = nse_live.upcoming_expiries(self.cal, date(2026, 11, 18))
        monthly = [r for r in rows if r["kind"] == "monthly"][0]
        self.assertEqual((monthly["nominal"], monthly["date"], monthly["moved"]), ("2026-11-24", "2026-11-23", True))
        self.assertEqual(monthly["reason"], "Prakash Gurpurb Sri Guru Nanak Dev")
        lines = nse_live.calendar_lines(nse_live.calendar_summary(self.cal, date(2026, 11, 18)))
        self.assertIn("Tue 24 Nov: NSE closed (Prakash Gurpurb Sri Guru Nanak Dev).", lines)
        self.assertIn("Nifty 50 and Bank Nifty monthly expiry moves from Tue 24 Nov (Prakash Gurpurb Sri Guru Nanak Dev) to Mon 23 Nov.", lines)

    def test_holiday_list_is_downloaded_once_per_run(self):
        fake = FakeNSE()
        for _ in range(3): nse_live.holiday_calendar(fetcher=fake, clock=lambda: NOW)
        self.assertEqual(fake.calls.count(nse_live.HOLIDAYS), 1)


class HistoryTests(unittest.TestCase):
    def setUp(self): nse_live.clear_cache()

    def test_history_uses_nse_files_including_muhurat_and_skips_missing_dates(self):
        cal = nse_live.holiday_calendar(fetcher=FakeNSE(), clock=lambda: NOW)
        fake = FakeNSE(missing=[date(2026, 11, 3)])
        hist = nse_live.index_history(["NIFTY 50"], date(2026, 11, 17), 21, cal=cal, fetcher=fake)
        expected = [d for d in sessions_until(date(2026, 11, 17), 23) if d != date(2026, 11, 3)][-21:]
        self.assertEqual(hist["sessions"], [d.isoformat() for d in expected])
        self.assertIn("2026-11-08", hist["sessions"]); self.assertEqual(hist["special_sessions_included"], ["2026-11-08"])
        self.assertIn("2026-11-03", hist["dates_without_file"])
        closes = [r["close"] for r in hist["series"]["NIFTY 50"]]
        self.assertEqual(closes, [close_value("NIFTY 50", d) for d in expected])
        self.assertFalse(any("ind_close_all_10112026" in u or "ind_close_all_14112026" in u for u in fake.calls))  # holidays/weekends not requested
        self.assertLessEqual(len([u for u in fake.calls if "ind_close_all" in u]), 25)


class Round2Tests(unittest.TestCase):
    def setUp(self): nse_live.clear_cache()

    def test_bank_nifty_list_is_allowlisted(self):
        self.assertTrue(nse_live.allowed(nse_live.NIFTYBANK_LIST))
        self.assertFalse(nse_live.allowed("https://nsearchives.nseindia.com/content/indices/ind_niftyitlist.pdf"))

    def test_vix_percent_rounding_is_tolerated_but_real_mismatches_are_not(self):
        raw = json.loads(all_indices_json())
        vix = next(r for r in raw["data"] if r["index"] == "INDIA VIX")
        vix.update(last=15.76, previousClose=15.52, variation=0.24, percentChange=1.51)  # NSE rounds the level, not the percent (1.546)
        snap = nse_live.parse_all_indices(json.dumps(raw).encode(), NOW)
        self.assertIn("INDIA VIX", snap["indices"]); self.assertEqual(snap["rejected_rows"], [])
        vix.update(percentChange=1.9)
        self.assertEqual(nse_live.parse_all_indices(json.dumps(raw).encode(), NOW)["rejected_rows"], ["INDIA VIX"])
        nifty = next(r for r in raw["data"] if r["index"] == "NIFTY 50"); nifty["percentChange"] += 0.05
        self.assertIn("NIFTY 50", nse_live.parse_all_indices(json.dumps(raw).encode(), NOW)["rejected_rows"])

    def test_52_week_range_is_kept(self):
        raw = json.loads(all_indices_json())
        raw["data"][1].update(yearHigh=62442.2, yearLow=49954.85)
        bank = nse_live.parse_all_indices(json.dumps(raw).encode(), NOW)["indices"]["NIFTY BANK"]
        self.assertEqual((bank["year_high"], bank["year_low"]), (62442.2, 49954.85))
        self.assertIsNone(nse_live.parse_all_indices(all_indices_json(), NOW)["indices"]["NIFTY 50"]["year_high"])

    def test_bank_nifty_calendar_lines_drop_nifty_weekly_expiries(self):
        summary = nse_live.calendar_summary(nse_live.holiday_calendar(fetcher=FakeNSE(), clock=lambda: NOW), date(2026, 11, 18))
        self.assertIn("Next weekly expiry: Tue 1 Dec (Nifty 50 weekly).", nse_live.calendar_lines(summary))
        bank = nse_live.calendar_lines(summary, weekly=False)
        self.assertFalse(any("weekly" in line for line in bank))
        self.assertIn("Nifty 50 and Bank Nifty monthly expiry moves from Tue 24 Nov (Prakash Gurpurb Sri Guru Nanak Dev) to Mon 23 Nov.", bank)


if __name__ == "__main__":
    unittest.main()
