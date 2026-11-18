"""Readable daily reports: day change, dated plain headlines, India VIX, brief mode, Sensex routing, UTF-8 output."""
import contextlib
from datetime import date, datetime, timedelta
import io
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch
from urllib.parse import parse_qs, unquote, urlsplit

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
from core import Block, IST, instant
import closing_context as cc
import daily
from daily import collect_vix, demo, emit, main, markdown, report
from public_market import collect_daily

PREOPEN = instant("2026-11-18T08:30:00+05:30")
INTRADAY = instant("2026-11-18T09:35:00+05:30")
LAST = {"^NSEI": (25291.10, 25387.40, 25267.30, 25501.35), "^BSESN": (82104.79, 82532.19, 82091.90, 82897.49),
        "^NSEBANK": (61575.90, 62273.25, 61488.00, 62442.20), "^INDIAVIX": (15.68, 15.52, 14.04, 15.68)}


def sessions(n=25, end=date(2026, 11, 17)):
    days, d = [], end
    while len(days) < n:
        if d.weekday() < 5: days.append(d)
        d -= timedelta(days=1)
    return list(reversed(days))


def payload(symbol, interval, now):
    """A Yahoo v8 chart response shaped like the live one, with fixed last-two closes."""
    before, last, low, high = LAST[symbol]
    if interval == "1d":
        stamps, rows = [], []
        for k, d in enumerate(sessions()):
            stamps.append(int(datetime(d.year, d.month, d.day, 9, 15, tzinfo=IST).timestamp()))
            close = last if k == 24 else before if k == 23 else before * (0.9 + k * 0.004)
            rows.append((close, max(close, high if k == 24 else close * 1.002), min(close, low if k == 24 else close * 0.998), close))
    else:
        start = now.replace(hour=9, minute=15, second=0, microsecond=0)
        stamps = [int((start + timedelta(minutes=5 * k)).timestamp()) for k in range(4)]
        rows = [(last + 10 * k, last + 10 * k + 15, last + 10 * k - 5, last + 10 * k + 8) for k in range(4)]
    open_, high_, low_, close_ = (list(x) for x in zip(*rows))
    meta = {"symbol": symbol, "currency": "INR", "exchangeTimezoneName": "Asia/Kolkata", "dataGranularity": interval,
            "currentTradingPeriod": {"regular": {"start": int(now.replace(hour=9, minute=15, second=0, microsecond=0).timestamp()),
                                                 "end": int(now.replace(hour=15, minute=30, second=0, microsecond=0).timestamp())}}}
    return {"chart": {"error": None, "result": [{"meta": meta, "timestamp": stamps, "indicators": {"quote": [
        {"open": open_, "high": high_, "low": low_, "close": close_, "volume": [0] * len(stamps)}]}}]}}


def fetcher(now, broken=()):
    def fetch(url):
        symbol = unquote(urlsplit(url).path.rsplit("/", 1)[-1]); interval = parse_qs(urlsplit(url).query)["interval"][0]
        if symbol in broken: raise Block("PUBLIC: source unavailable")
        return json.dumps(payload(symbol, interval, now)).encode()
    return fetch


def public_bundle(now, selected=("nifty", "sensex", "banknifty"), vix=True):
    bundle = collect_daily(list(selected), fetcher=fetcher(now), clock=lambda: now)
    if vix: bundle["vix"] = collect_vix(fetcher=fetcher(now), clock=lambda: now)
    return bundle


def cp1252_stdout():
    raw = io.BytesIO()
    return raw, io.TextIOWrapper(raw, encoding="cp1252", errors="strict", newline="\n")


class DayChangeTests(unittest.TestCase):
    def test_preopen_shows_last_session_close_points_percent_date_and_range(self):
        r = report("nifty", public_bundle(PREOPEN), PREOPEN)
        s = r["indices"][0]["last_session"]
        self.assertEqual((s["date"], s["previous_date"]), ("2026-11-17", "2026-11-16"))
        self.assertAlmostEqual(s["change_points"], 96.30, places=2); self.assertAlmostEqual(s["change_pct"], 0.381, places=3)
        self.assertEqual((s["low"], s["high"]), (25267.30, 25501.35))
        row = r["comparison"][0]
        self.assertEqual((row["basis"], row["date"], row["value"]), ("last_session_close", "2026-11-17", 25387.40))
        self.assertIn("Last close on 17 Nov: **25,387.40**, +96.30 (+0.38%) vs 16 Nov. 17 Nov range: 25,267.30-25,501.35.", markdown(r))

    def test_india_daily_comparison_table_lists_all_three_indices_and_vix(self):
        r = report("india-daily", public_bundle(PREOPEN), PREOPEN)
        self.assertEqual([c["index_id"] for c in r["comparison"]], ["nifty", "sensex", "banknifty"])
        self.assertTrue(all(c["change_pct"] is not None for c in r["comparison"]))
        text = markdown(r)
        self.assertIn("| Index | Date | Close | Change | Change % | Day range |", text)
        self.assertIn("| Nifty 50 | 17 Nov | 25,387.40 | +96.30 | +0.38% | 25,267.30-25,501.35 |", text)
        self.assertIn("| Bank Nifty | 17 Nov | 62,273.25 | +697.35 | +1.13% |", text)
        self.assertIn("| India VIX | 17 Nov | 15.52 | -0.16 | -1.02% |", text)

    def test_market_hours_change_is_against_previous_close(self):
        r = report("nifty", public_bundle(INTRADAY), INTRADAY)
        item = r["indices"][0]
        self.assertEqual(item["value"], 25387.40 + 38)
        self.assertAlmostEqual(item["change_from_previous_close_points"], 38.0)
        self.assertAlmostEqual(item["change_from_previous_close_pct"], 0.15, places=2)
        self.assertEqual(item["value_at"], "2026-11-18T09:35:00+05:30")
        self.assertIn("At 09:35 IST (last completed 5-minute bar): **25,425.40**, +38.00 (+0.15%) vs 17 Nov close 25,387.40.", markdown(r))

    def test_change_is_unavailable_not_invented_without_an_earlier_close(self):
        b, now = demo("preopen"); b["indices"]["nifty"]["history"]["rows"].pop(0)
        r = report("nifty", b, now)
        self.assertIsNone(r["indices"][0]["last_session"]["change_pct"])
        self.assertIn("change n/a (earlier close not supplied)", markdown(r))


class PlainHeadlineTests(unittest.TestCase):
    def test_no_internal_labels_dated_levels_and_formatted_numbers(self):
        for now in (PREOPEN, INTRADAY):
            text = markdown(report("india-daily", public_bundle(now), now))
            for label in ("previous_session_close", "PREOPEN_CONTEXT", "INSIDE_REFERENCE_RANGE", "last_completed_candle_close", "Previous High"):
                self.assertNotIn(label, text)
            self.assertIn("(17 Nov high)", text); self.assertIn("(17 Nov low)", text)
            self.assertRegex(text, r"20-day average of closes to 17 Nov: \d{1,3}(,\d{3})*\.\d{2} ")
            self.assertNotRegex(text, r"\d{5}\.\d{3}")

    def test_coverage_boilerplate_collapsed_into_one_data_notes_section(self):
        text = markdown(report("india-daily", public_bundle(INTRADAY), INTRADAY))
        self.assertEqual(text.count("## Data notes"), 1)
        self.assertNotIn("Coverage:", text)
        self.assertEqual(text.count("Yahoo Finance public charts"), 1)
        self.assertEqual(text.count("Advance/decline breadth not available"), 1)
        self.assertEqual(text.count("Spot-index points only"), 1)


class VixTests(unittest.TestCase):
    def test_collect_vix_uses_public_chart_contract(self):
        v = collect_vix(fetcher=fetcher(PREOPEN), clock=lambda: PREOPEN)
        self.assertEqual(v["symbol"], "^INDIAVIX"); self.assertEqual(v["source"], "yahoo_public_chart")
        self.assertEqual(v["rows"][-1]["session"], "2026-11-17")

    def test_vix_close_change_and_date_in_single_report(self):
        r = report("nifty", public_bundle(PREOPEN), PREOPEN)
        v = r["vix"]
        self.assertEqual((v["status"], v["date"], v["close"], v["previous_date"]), ("OBSERVED", "2026-11-17", 15.52, "2026-11-16"))
        self.assertAlmostEqual(v["change_points"], -0.16); self.assertFalse(v["provisional"])
        self.assertIn("India VIX (17 Nov): 15.52, -0.16 (-1.02%) vs 16 Nov.", markdown(r))

    def test_failed_or_stale_vix_is_a_note_not_a_block(self):
        b = public_bundle(PREOPEN, vix=False); b["vix_error"] = "Public India VIX chart unavailable or inconsistent"
        r = report("india-daily", b, PREOPEN)
        self.assertEqual(r["coverage"]["blocked"], 0); self.assertEqual(r["vix"]["status"], "UNAVAILABLE")
        self.assertIn("India VIX: Public India VIX chart unavailable", markdown(r))
        b = public_bundle(PREOPEN); later = PREOPEN + timedelta(seconds=daily.VIX_MAX_AGE + 1)
        b["vix"]["as_of"] = PREOPEN.isoformat()
        self.assertEqual(daily.vix_report(b["vix"], later, "preopen", "2026-11-18")["status"], "UNAVAILABLE")
        b["vix"]["symbol"] = "^NSEI"
        self.assertEqual(daily.vix_report(b["vix"], PREOPEN, "preopen", "2026-11-18")["status"], "UNAVAILABLE")

    def test_cli_public_route_attaches_vix_and_survives_vix_failure(self):
        bundle = public_bundle(PREOPEN, vix=False)
        vix = collect_vix(fetcher=fetcher(PREOPEN), clock=lambda: PREOPEN)
        with patch("public_market.collect_daily", return_value=bundle) as collector, patch("daily.collect_vix", return_value=vix), \
             patch("daily.instant", side_effect=lambda value=None: instant(value) if value is not None else PREOPEN), \
             contextlib.redirect_stdout(io.StringIO()) as out:
            self.assertEqual(main(["india-daily", "--collect-public", "--format", "json"]), 0)
        self.assertEqual(json.loads(out.getvalue())["vix"]["close"], 15.52)
        self.assertEqual(collector.call_args.args[0], ["nifty", "sensex", "banknifty"])
        with patch("public_market.collect_daily", return_value=public_bundle(PREOPEN, vix=False)), patch("daily.collect_vix", side_effect=Block("PUBLIC: down")), \
             patch("daily.instant", side_effect=lambda value=None: instant(value) if value is not None else PREOPEN), \
             contextlib.redirect_stdout(io.StringIO()) as out:
            self.assertEqual(main(["nifty", "--collect-public"]), 0)
        self.assertIn("India VIX: Public India VIX chart unavailable", out.getvalue())


class BriefTests(unittest.TestCase):
    def lines(self, text): return [line for line in text.splitlines() if line.strip()]

    def test_brief_is_short_answer_first_in_every_phase(self):
        for phase in ("preopen", "intraday", "close"):
            b, now = demo(phase)
            for command, limit in (("nifty", 8), ("sensex", 8), ("india-daily", 12)):
                with self.subTest(phase=phase, command=command):
                    text = markdown(report(command, b, now), brief=True)
                    lines = self.lines(text)
                    self.assertLessEqual(len(lines), limit); self.assertGreaterEqual(len(lines), 5)
                    self.assertIn("SYNTHETIC DEMO", lines[0])
                    self.assertRegex(lines[1] if command != "india-daily" else text, r"[+-]\d[\d,]*\.\d{2} \([+-]\d+\.\d{2}%\)|\| [+-]\d")
                    self.assertLess(len(text), len(markdown(report(command, b, now))) / 1.5)

    def test_public_brief_leads_with_dated_change(self):
        lines = self.lines(markdown(report("nifty", public_bundle(PREOPEN), PREOPEN), brief=True))
        self.assertEqual(lines[1], "- Last close (17 Nov): 25,387.40, +96.30 (+0.38%); range 25,267.30-25,501.35.")
        self.assertLessEqual(len(lines), 10)


class SensexTests(unittest.TestCase):
    def test_public_sensex_is_labelled_as_yahoo_data(self):
        text = markdown(report("sensex", public_bundle(PREOPEN, ("sensex",)), PREOPEN))
        self.assertIn("Sensex (Yahoo data; no official BSE feed)", text)
        self.assertIn("Sensex is Yahoo data, not an official BSE feed.", text)
        combined = markdown(report("india-daily", public_bundle(PREOPEN), PREOPEN))
        self.assertIn("| Sensex (Yahoo) | 17 Nov |", combined)

    def test_sensex_only_closing_request_explains_public_route_without_nse_calls(self):
        with patch.object(cc, "collect") as collector, contextlib.redirect_stdout(io.StringIO()) as out:
            self.assertEqual(main(["sensex", "--collect-close", "--session", "2026-11-17"]), 2)
            collector.assert_not_called()
        text = out.getvalue()
        self.assertIn("not in the NSE closing report", text); self.assertIn("daily.py sensex --collect-public", text)
        self.assertNotIn("BLOCKED", text)
        with patch.object(cc, "collect") as collector, contextlib.redirect_stdout(io.StringIO()) as out:
            self.assertEqual(main(["sensex", "--collect-close", "--session", "2026-11-17", "--format", "json"]), 2)
        self.assertEqual(json.loads(out.getvalue())["alternative_command"], cc.SENSEX_ROUTE)

    def test_closing_context_cli_sensex_only_writes_explanation(self):
        with tempfile.TemporaryDirectory() as d, contextlib.redirect_stdout(io.StringIO()) as out, patch.object(cc, "fetch") as fetch:
            path = Path(d) / "sensex.md"
            self.assertEqual(cc.main(["--session", "2026-11-17", "--indices", "sensex", "--out", str(path)]), 2)
            fetch.assert_not_called()
            self.assertIn("--collect-public", path.read_text(encoding="utf-8")); self.assertIn("--collect-public", out.getvalue())

    def test_combined_closing_report_explains_missing_sensex(self):
        result = ClosingFixture().collect()
        self.assertIn("--collect-public", result["indices"]["sensex"]["reason"])
        text = cc.markdown(result)
        self.assertIn("| Sensex | not in NSE report |", text); self.assertIn("daily.py sensex --collect-public", text)


class ClosingFixture:
    now = instant("2026-11-18T08:30:00+05:30")
    index = (b"Index Name,Index Date,Open Index Value,High Index Value,Low Index Value,Closing Index Value,Points Change,Change(%)\n"
             b"Nifty 50,17-11-2026,25277.30,25501.35,25267.30,25387.40,96.30,0.38\n")
    cash = b"SYMBOL, SERIES, DATE1, PREV_CLOSE, CLOSE_PRICE\nUP, EQ, 17-Nov-2026, 100, 110\nDOWN, EQ, 17-Nov-2026, 100, 90\n"
    flow = [{"category": "DII", "date": "17-Nov-2026", "buyValue": "100", "sellValue": "50", "netValue": "50"},
            {"category": "FII/FPI", "date": "17-Nov-2026", "buyValue": "4300", "sellValue": "100", "netValue": "4200"}]
    gift = {"giftnifty": {"SYMBOL": "NIFTY", "INSTRUMENTTYPE": "FUTIDX", "OPTIONTYPE": "-", "STRIKEPRICE": 0, "TIMESTMP": "18-Nov-2026 08:29",
                          "EXPIRYDATE": "23-Nov-2026", "LASTPRICE": 25243, "DAYCHANGE": -103.5, "PERCHANGE": -0.41}}

    def fetch(self, url):
        if url == cc.FLOWS: return json.dumps(self.flow).encode()
        if url == cc.STATUS: return json.dumps(self.gift).encode()
        return self.index if url.startswith(cc.INDEX_BASE) else self.cash

    def collect(self, selected=("nifty", "sensex", "banknifty")):
        return cc.collect("2026-11-17", selected=list(selected), fetcher=self.fetch, clock=lambda: self.now)


class ClosingReadabilityTests(unittest.TestCase):
    def test_closing_report_formats_change_points_and_dates(self):
        text = cc.markdown(ClosingFixture().collect(["nifty"]))
        self.assertIn("# Market close - Tue 17 Nov 2026", text)
        self.assertIn("| Nifty 50 | 25,387.40 | +96.30 | +0.38% | 25,267.30-25,501.35 |", text)
        self.assertIn("FII/DII (17 Nov): DII net ₹50.00 crore; FII/FPI net ₹4,200.00 crore.", text)

    def test_closing_brief_is_short(self):
        text = cc.markdown(ClosingFixture().collect(), brief=True)
        lines = [line for line in text.splitlines() if line.strip()]
        self.assertLessEqual(len(lines), 10)
        self.assertIn("Nifty 50 (17 Nov): 25,387.40, +96.30 (+0.38%)", text)


class EncodingTests(unittest.TestCase):
    def test_emit_writes_rupee_sign_through_cp1252_console(self):
        raw, stream = cp1252_stdout()
        emit("net ₹4,195.38 crore", stream); stream.flush()
        self.assertEqual(raw.getvalue().decode("utf-8"), "net ₹4,195.38 crore\n")

    def test_emit_falls_back_for_streams_that_cannot_be_reconfigured(self):
        class Legacy(io.StringIO):
            encoding = "cp1252"
            def write(self, text): text.encode(self.encoding); return super().write(text)
        stream = Legacy()
        emit("net ₹4,195.38 crore", stream)
        self.assertEqual(stream.getvalue(), "net Rs 4,195.38 crore\n")

    def test_closing_cli_prints_rupee_report_on_cp1252_stdout(self):
        raw, stream = cp1252_stdout()
        with patch.object(cc, "collect", return_value=ClosingFixture().collect(["nifty"])), patch("sys.stdout", stream):
            self.assertEqual(main(["nifty", "--collect-close", "--session", "2026-11-17"]), 0)
        stream.flush()
        text = raw.getvalue().decode("utf-8")
        self.assertIn("₹4,200.00 crore", text); self.assertNotIn("BLOCKED", text)

    def test_programming_and_encoding_errors_are_not_reported_as_data_blocks(self):
        with patch("daily.markdown", side_effect=TypeError("bug in renderer")), contextlib.redirect_stdout(io.StringIO()):
            with self.assertRaises(TypeError): main(["nifty", "--demo"])
        with patch("daily.report", side_effect=UnicodeEncodeError("charmap", "₹", 0, 1, "undefined")), contextlib.redirect_stdout(io.StringIO()):
            with self.assertRaises(UnicodeEncodeError): main(["nifty", "--demo"])

    def test_data_blocks_still_report_their_reason(self):
        with contextlib.redirect_stdout(io.StringIO()) as out:
            self.assertEqual(main(["nifty", "--demo", "--now", "2026-11-06T09:35:00+05:30"]), 2)
        self.assertEqual(json.loads(out.getvalue())["reason"], "DEMO: use its labelled fixed clock")
        with tempfile.TemporaryDirectory() as d, contextlib.redirect_stdout(io.StringIO()) as out:
            bad = Path(d) / "bad.json"; bad.write_text("{}", encoding="utf-8")
            self.assertEqual(main(["nifty", "--input", str(bad), "--now", "2026-11-06T09:35:00+05:30"]), 2)
        self.assertIn("INPUT: missing or invalid field", json.loads(out.getvalue())["reason"])


class DocumentationTests(unittest.TestCase):
    def test_daily_reference_documents_public_route_and_plugin_command_names(self):
        text = (ROOT / "references" / "daily.md").read_text(encoding="utf-8")
        for phrase in ("--collect-public", "India VIX", "--brief", "/india-day-trader:nifty", "Yahoo"):
            self.assertIn(phrase, text)
        self.assertNotIn("The four skill aliases and matching Python commands are implemented", text)


if __name__ == "__main__": unittest.main()
