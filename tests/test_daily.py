import copy
from datetime import timedelta
import io
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
from core import Block, instant
from daily import demo, report, markdown, main, COMMANDS
from daily_collect import collect, index_master, NAMES


class DailyTests(unittest.TestCase):
    def setUp(self): self.b, self.now = demo()
    def result(self, command="india-daily"): return report(command, self.b, self.now)
    def item(self): return self.b["indices"]["nifty"]
    def first(self): return self.result()["indices"][0]

    def test_all_four_commands_all_three_phases(self):
        for phase in ("preopen", "intraday", "close"):
            b, now = demo(phase)
            for command in COMMANDS:
                with self.subTest(phase=phase, command=command):
                    r = report("/" + command, b, now, phase)
                    self.assertEqual(len(r["indices"]), 3 if command == "india-daily" else 1)
                    self.assertEqual(r["coverage"]["blocked"], 0)
                    self.assertEqual(r["mode"], "SYNTHETIC_DEMO")
                    self.assertFalse(r["submitted"])
                    self.assertIn("SYNTHETIC DEMO", markdown(r))

    def test_single_commands_do_not_require_other_indices(self):
        del self.b["indices"]["sensex"]
        self.assertEqual(self.result("nifty")["coverage"]["available"], 1)
        self.assertEqual(self.result()["coverage"]["blocked"], 1)

    def test_unknown_command_blocked(self):
        with self.assertRaises(Block): self.result("crude")

    def test_stale_quote_blocks_only_affected_index(self):
        self.item()["quote"] = {"source": "test", "as_of": (self.now - timedelta(seconds=61)).isoformat(), "value": 25000}
        self.assertEqual(self.first()["status"], "BLOCKED")
        self.assertEqual(self.result()["coverage"]["available"], 2)

    def test_wrong_identity_and_price_basis(self):
        for key, value in (("index_id", "sensex"), ("price_basis", "futures_rupees")):
            b = copy.deepcopy(self.b); b["indices"]["nifty"][key] = value
            self.assertEqual(report("nifty", b, self.now)["status"], "BLOCKED")

    def test_missing_intervening_or_latest_bar_blocks(self):
        for index in (0, 1, -1):
            b = copy.deepcopy(self.b); b["indices"]["nifty"]["candles"]["rows"].pop(index)
            self.assertEqual(report("nifty", b, self.now)["status"], "BLOCKED")

    def test_duplicate_out_of_order_and_future_bars_block(self):
        for mutation in ("duplicate", "reverse", "future"):
            b = copy.deepcopy(self.b); rows = b["indices"]["nifty"]["candles"]["rows"]
            if mutation == "duplicate": rows.append(rows[-1])
            if mutation == "reverse": rows.reverse()
            if mutation == "future": rows[-1]["at"] = (self.now + timedelta(minutes=5)).isoformat()
            self.assertEqual(report("nifty", b, self.now)["status"], "BLOCKED")

    def test_forming_bar_does_not_trigger_scenario(self):
        row = {"at": self.now.isoformat(), "open": 26000, "high": 27000, "low": 25900, "close": 26900}
        self.item()["candles"]["rows"].append(row)
        self.assertEqual(self.first()["completed_bars"], 4)
        self.assertEqual(self.first()["condition"], "INSIDE_REFERENCE_RANGE")

    def test_zero_index_volume_is_accepted_and_no_vwap_is_fabricated(self):
        for row in self.item()["candles"]["rows"]: row["volume"] = 0
        r = self.first()
        self.assertNotEqual(r["status"], "BLOCKED")
        self.assertNotIn("vwap", r["levels"])
        self.assertFalse(r["trade_ready"])

    def test_previous_session_date_is_not_inferred(self):
        self.item()["previous"]["session"] = "2026-11-04"
        self.assertEqual(self.first()["status"], "BLOCKED")

    def test_holiday_weekend_and_missing_exchange_calendar_block(self):
        self.b["session"]["is_open_day"] = False
        with self.assertRaises(Block): self.result()
        self.b, self.now = demo(); self.b["session"]["exchanges"] = ["NSE"]
        self.assertEqual(self.result()["indices"][1]["status"], "BLOCKED")
        self.b["session"]["date"] = "2026-11-07"
        with self.assertRaises(Block): report("nifty", self.b, instant("2026-11-07T09:35:00+05:30"))

    def test_explicit_phase_cannot_show_close_before_close(self):
        with self.assertRaises(Block): report("nifty", self.b, self.now, "close")

    def test_closing_report_requires_complete_session(self):
        b, now = demo("close"); b["indices"]["sensex"]["candles"]["rows"].pop()
        r = report("india-daily", b, now)
        self.assertEqual(r["indices"][1]["status"], "BLOCKED")
        self.assertEqual(r["indices"][0]["completed_bars"], 75)

    def add_breadth(self):
        self.item()["breadth"] = {"source": "test", "as_of": self.now.isoformat(), "membership_date": "2026-11-06", "membership_source": "test_master",
            "members": ["A", "B", "C"], "observations": [
                {"symbol": s, "as_of": self.now.isoformat(), "previous_session": "2026-11-05", "previous_close": 100, "value": v} for s, v in (("A", 101), ("B", 99))]}

    def test_partial_breadth_counts_missing_not_zero(self):
        self.add_breadth(); br = self.first()["breadth"]
        self.assertEqual(br["missing"], ["C"]); self.assertEqual(br["advancing"], 1); self.assertEqual(br["declining"], 1)
        self.assertEqual(br["unchanged"], 0); self.assertEqual(br["status"], "PARTIAL")

    def test_unknown_member_does_not_pollute_breadth(self):
        self.add_breadth(); self.item()["breadth"]["observations"][0]["symbol"] = "D"
        self.assertEqual(self.first()["breadth"]["status"], "UNAVAILABLE")

    def test_complete_source_contract_can_produce_ready_report(self):
        self.add_breadth(); self.item()["breadth"]["members"] = ["A", "B"]
        prev = self.item()["previous"]["close"]
        dates = []; day = self.now.date() - timedelta(days=1)
        while len(dates) < 20:
            if day.weekday() < 5: dates.append(day.isoformat())
            day -= timedelta(days=1)
        dates.reverse()
        self.item()["history"] = {"source": "test", "as_of": self.now.isoformat(), "rows": [
            {"session": d, "close": prev - 19 + k, "available_at": d + "T16:00:00+05:30"} for k, d in enumerate(dates)]}
        self.b["events"]["coverage"] = "reviewed_complete"
        r = self.result("nifty")
        self.assertEqual(r["status"], "READY")
        self.assertAlmostEqual(r["indices"][0]["prior_sma20"], prev - 9.5)
        self.item()["history"]["rows"][-1]["close"] += 1
        self.assertEqual(self.result("nifty")["status"], "BLOCKED")

    def test_history_cannot_include_future_release_or_today(self):
        prev = self.item()["previous"]["close"]
        self.item()["history"] = {"source": "test", "as_of": self.now.isoformat(), "rows": [
            {"session": "2026-11-05", "close": prev, "available_at": (self.now + timedelta(seconds=1)).isoformat()}]}
        self.assertEqual(self.first()["status"], "BLOCKED")
        self.item()["history"]["rows"][0].update(session="2026-11-06", available_at=self.now.isoformat())
        self.assertEqual(self.first()["status"], "BLOCKED")

    def test_stale_constituent_does_not_produce_fresh_breadth(self):
        self.add_breadth(); self.item()["breadth"]["observations"][0]["as_of"] = (self.now - timedelta(minutes=2)).isoformat()
        self.assertEqual(self.first()["breadth"]["status"], "UNAVAILABLE")

    def test_missing_events_do_not_mean_no_event_risk(self):
        del self.b["events"]
        self.assertIn("Event calendar unavailable", self.result()["coverage_gaps"][0])

    def test_future_event_release_rejected_without_leaking_partial_items(self):
        self.b["events"]["items"] = [{"headline": "Test", "source": "test", "published_at": (self.now + timedelta(seconds=1)).isoformat()}]
        self.assertEqual(self.result()["events"], [])
        self.assertIn("unavailable", self.result()["coverage_gaps"][0])

    def test_fixture_label_survives_missing_flag(self):
        del self.b["synthetic"]
        self.assertEqual(self.result()["mode"], "SYNTHETIC_DEMO")

    def test_markdown_escapes_external_headlines(self):
        self.b["events"]["items"] = [{"headline": "<script>evil</script>\n[click](url)", "source": "test", "published_at": self.now.isoformat()}]
        rendered = markdown(self.result())
        self.assertNotIn("<script>", rendered); self.assertNotIn("\n[click]", rendered)

    def test_report_cli_writes_utf8_and_refuses_overwrite(self):
        with tempfile.TemporaryDirectory() as d, patch("sys.stdout", new_callable=io.StringIO):
            out = Path(d) / "daily.md"
            self.assertEqual(main(["/india-daily", "--demo", "--out", str(out)]), 0)
            self.assertIn("Bank Nifty", out.read_text(encoding="utf-8"))
            self.assertEqual(main(["/india-daily", "--demo", "--out", str(out)]), 2)

    def test_cli_all_shortcuts_json(self):
        for cmd in COMMANDS:
            with patch("sys.stdout", new_callable=io.StringIO) as out:
                self.assertEqual(main(["/" + cmd, "--demo", "--format", "json"]), 0)
                self.assertEqual(json.loads(out.getvalue())["command"], "/" + cmd)

    def test_one_command_collection_uses_selected_index_without_actual_network(self):
        with tempfile.TemporaryDirectory() as d:
            calendar = Path(d) / "calendar.json"; calendar.write_text(json.dumps(self.b["session"]))
            with patch("daily.instant", side_effect=lambda value=None: instant(value) if value is not None else self.now), patch("daily_collect.collect", return_value=self.b) as collector, patch("sys.stdout", new_callable=io.StringIO) as output:
                self.assertEqual(main(["/nifty", "--collect-kite", "--calendar", str(calendar), "--format", "json"]), 0)
                self.assertEqual(json.loads(output.getvalue())["command"], "/nifty")
                self.assertEqual(collector.call_args.args[2], ["nifty"])

    def test_collection_rejects_historical_clock_and_mixed_input_modes(self):
        with patch("daily_collect.collect") as collector, patch("sys.stdout", new_callable=io.StringIO):
            self.assertEqual(main(["/nifty", "--collect-kite", "--now", self.now.isoformat()]), 2)
            self.assertEqual(main(["/nifty", "--demo", "--input", "something.json"]), 2)
            collector.assert_not_called()

    def test_extreme_prices_block_without_invalid_json_or_division_by_zero(self):
        for value in ("1e-999", "1e999"):
            with self.subTest(value=value):
                self.b, self.now = demo()
                self.item()["previous"].update({k: value for k in ("open", "high", "low", "close")})
                result = self.result("nifty")
                self.assertEqual(result["status"], "BLOCKED")
                json.dumps(result, allow_nan=False)

    def test_unrepresentable_percentage_blocks_index(self):
        self.item()["previous"].update({k: "1e-300" for k in ("open", "high", "low", "close")})
        self.item()["quote"] = {"source": "test", "as_of": self.now.isoformat(), "value": "1e300"}
        self.assertEqual(self.result("nifty")["status"], "BLOCKED")


class CollectorTests(unittest.TestCase):
    def setUp(self):
        self.b, self.now = demo(); self.calls = []
        self.master = "instrument_token,tradingsymbol,exchange,segment\n1,NIFTY 50,NSE,INDICES\n2,NIFTY BANK,NSE,INDICES\n3,SENSEX,BSE,INDICES\n"

    def client(self):
        parent = self
        class Client:
            def request(self, method, path, **kwargs):
                parent.calls.append((method, path))
                if path == "/instruments": return parent.master
                if path.endswith("/day"):
                    if kwargs["query"]["to"] != "2026-11-05 23:59:59":
                        raise AssertionError("Daily collection must exclude today's forming daily candle")
                    return {"candles": [["2026-11-05T09:15:00+05:30", 100, 102, 98, 101, 0]]}
                return {"candles": [[r["at"], r["open"], r["high"], r["low"], r["close"], 0] for r in parent.b["indices"]["nifty"]["candles"]["rows"]]}
        return Client()

    def test_exact_spot_master_not_future_or_stock(self):
        values = index_master(self.master, list(NAMES))
        self.assertEqual(values["sensex"]["token"], "3")
        with self.assertRaises(Block): index_master(self.master.replace("INDICES", "NFO-FUT"), ["nifty"])
        with self.assertRaises(Block): index_master(self.master + "4,NIFTY 50,NSE,INDICES\n", ["nifty"])

    def test_collector_reads_only_then_report_handles_index_candles(self):
        b = collect(self.client(), self.b["session"], list(NAMES), lambda: self.now)
        self.assertEqual(len(b["indices"]), 3)
        self.assertTrue(all(method == "GET" for method, _ in self.calls))
        r = report("india-daily", b, self.now)
        self.assertEqual(r["coverage"]["available"], 3)

    def test_failure_of_one_index_does_not_silently_drop_it(self):
        client = self.client(); original = client.request
        def fail(method, path, **kwargs):
            if "/3/" in path: raise Block("unavailable")
            return original(method, path, **kwargs)
        client.request = fail
        b = collect(client, self.b["session"], list(NAMES), lambda: self.now)
        self.assertIn("sensex", b["collection_errors"])
        self.assertEqual(report("india-daily", b, self.now)["coverage"]["blocked"], 1)

    def test_preopen_does_not_request_intraday_bars(self):
        b, now = demo("preopen")
        result = collect(self.client(), b["session"], ["nifty"], lambda: now)
        self.assertNotIn("candles", result["indices"]["nifty"])
        self.assertFalse(any(path.endswith("/5minute") for _, path in self.calls))


if __name__ == "__main__": unittest.main()
