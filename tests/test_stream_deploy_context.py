import copy
from datetime import timedelta
import io
import json
from pathlib import Path
import struct
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch
import xml.etree.ElementTree as ET

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
from core import Block, fingerprint, instant
from streaming import decode, instruments_by_token, run, read_quote, connect, main as stream_main
from deployment import render, save, verify, main as deploy_main, NS
from daily_context import membership, collect_breadth, collect_news, enrich
from daily import demo, report, main as daily_main
from daily_collect import collect
from trader import write, read, runtime_context

NOW = instant("2026-11-06T09:35:00+05:30")
INSTRUMENT = {"broker": "kite", "exchange": "NSE", "symbol": "DEMO", "token": "123", "kind": "equity", "lot_size": 1, "tick_size": .05, "master_date": "2026-11-06"}


def packet(token=123, price=12345, trade=None, exchange=None):
    b = bytearray(184)
    struct.pack_into("!II", b, 0, token, price)
    struct.pack_into("!I", b, 44, int((trade or NOW).timestamp()))
    struct.pack_into("!I", b, 60, int((exchange or NOW).timestamp()))
    return struct.pack("!HH", 1, 184) + b


class StreamingTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.w = Path(self.tmp.name)
        self.mapping = instruments_by_token([INSTRUMENT], NOW)
    def tearDown(self): self.tmp.cleanup()

    def test_binary_quote_uses_trade_epoch_in_ist_and_paise(self):
        r = decode(packet(), self.mapping, NOW)[0]
        self.assertEqual(r["price"], 123.45); self.assertEqual(r["as_of"], NOW.isoformat())
        self.assertEqual(r["instrument_hash"], fingerprint(INSTRUMENT))

    def test_multiple_packets_and_heartbeat(self):
        raw = packet(); frame = struct.pack("!H", 2) + raw[2:] + raw[2:]
        self.assertEqual(len(decode(frame, self.mapping, NOW)), 2)
        self.assertEqual(decode(b"\x00", self.mapping, NOW), [])

    def test_rejects_truncation_extra_data_short_packets_and_zero_price(self):
        for raw in (packet()[:-1], packet()+b"x", b"", b"\x00\x01", struct.pack("!HH", 1, 8)+b"0"*8, packet(price=0), struct.pack("!H", 0)):
            with self.subTest(raw_length=len(raw)), self.assertRaises(Block): decode(raw, self.mapping, NOW)

    def test_rejects_unknown_tokens_future_and_reversed_clocks(self):
        for raw in (packet(token=99), packet(exchange=NOW+timedelta(seconds=1)), packet(trade=NOW+timedelta(seconds=1))):
            with self.assertRaises(Block): decode(raw, self.mapping, NOW)

    def test_master_requires_unique_current_kite_tradable_instruments(self):
        for values in ([], [INSTRUMENT]*2, [{**INSTRUMENT,"broker":"dhan"}], [{**INSTRUMENT,"master_date":"2026-11-05"}], [{**INSTRUMENT,"kind":"index"}]):
            with self.assertRaises(Block): instruments_by_token(values, NOW)

    def write_snapshot(self, **patches):
        value = {"source":"kite_websocket","as_of":NOW.isoformat(),"state":"connected","quotes":{"123":decode(packet(),self.mapping,NOW)[0]},**patches}
        write(self.w/"state/kite-stream.json",value); return value

    def test_read_quote_requires_heartbeat_trade_freshness_and_identity(self):
        self.write_snapshot(); self.assertEqual(read_quote(self.w,INSTRUMENT,NOW)["price"],123.45)
        self.write_snapshot(state="disconnected")
        with self.assertRaises(Block): read_quote(self.w,INSTRUMENT,NOW)
        self.write_snapshot(as_of=(NOW-timedelta(seconds=6)).isoformat())
        with self.assertRaises(Block): read_quote(self.w,INSTRUMENT,NOW)
        value=self.write_snapshot(); value["quotes"]["123"]["as_of"]=(NOW-timedelta(seconds=61)).isoformat(); write(self.w/"state/kite-stream.json",value)
        with self.assertRaises(Block): read_quote(self.w,INSTRUMENT,NOW)

    def test_changed_instrument_hash_requires_resubscription(self):
        self.write_snapshot()
        with self.assertRaises(Block): read_quote(self.w,{**INSTRUMENT,"tick_size":1},NOW)

    def test_stream_selected_in_runtime_context_never_falls_back_to_rest(self):
        write(self.w/"config.json",{"quote_feed":"kite_stream","snapshot_max_age_seconds":60}); write(self.w/"instrument.json",INSTRUMENT)
        self.write_snapshot()
        with patch("trader.instant",return_value=NOW): self.assertEqual(runtime_context(self.w,{})["quote"]["price"],123.45)
        self.write_snapshot(state="stopped")
        with patch("trader.instant",return_value=NOW), self.assertRaises(Block): runtime_context(self.w,{"quote":{"price":999}})

    def test_run_subscribes_full_mode_preserves_live_quote_then_clears_on_stop(self):
        elapsed=[0]; observed=[]; conn=Mock()
        def receive():
            elapsed[0]+=1
            if elapsed[0]==2: observed.append(read_quote(self.w,INSTRUMENT,NOW))
            return 2,packet()
        conn.receive.side_effect=receive
        result=run(self.w,[INSTRUMENT],duration=3,connector=lambda:conn,clock=lambda:NOW,monotonic=lambda:elapsed[0])
        self.assertEqual(conn.send.call_args_list[0].args[0],{"a":"subscribe","v":[123]})
        self.assertEqual(conn.send.call_args_list[1].args[0],{"a":"mode","v":["full",[123]]})
        self.assertEqual(len(observed),1); self.assertEqual(result["received_quotes"],3)
        self.assertEqual(read(self.w/"state/kite-stream.json")["quotes"],{}); conn.close.assert_called_once()

    def test_disconnect_retries_are_bounded_and_cached_quote_is_unavailable(self):
        elapsed=[0]; sleeps=[]; conn=Mock(); conn.receive.side_effect=Block("private secret provider response")
        def sleep(n): sleeps.append(n); elapsed[0]+=n
        r=run(self.w,[INSTRUMENT],duration=100,retries=2,connector=lambda:conn,clock=lambda:NOW,monotonic=lambda:elapsed[0],sleep=sleep)
        self.assertEqual(r["connection_attempts"],3); self.assertEqual(sleeps,[2,4]); self.assertEqual(r["reason"],"retry_limit")
        self.assertNotIn("secret",(self.w/"state/kite-stream.json").read_text())

    def test_order_postback_never_becomes_a_fill_or_order_action(self):
        elapsed=[0]; conn=Mock()
        def receive(): elapsed[0]+=1; return 1,json.dumps({"type":"order","data":{"order_id":"private"}})
        conn.receive.side_effect=receive
        r=run(self.w,[INSTRUMENT],duration=2,connector=lambda:conn,clock=lambda:NOW,monotonic=lambda:elapsed[0])
        self.assertEqual(r["received_quotes"],0); self.assertEqual(r["orders_submitted"],0)
        self.assertFalse((self.w/"state/journal.sqlite").exists())

    def test_stop_latch_prevents_connect_and_cli_clear_requires_lock(self):
        write(self.w/"state/stream-stop.json",{}); connector=Mock()
        self.assertEqual(run(self.w,[INSTRUMENT],connector=connector,clock=lambda:NOW)["reason"],"requested_stop")
        connector.assert_not_called()
        with patch("sys.stdout",new_callable=io.StringIO): self.assertEqual(stream_main(["clear-stop","--workdir",str(self.w)]),0)
        self.assertFalse((self.w/"state/stream-stop.json").exists())

    def test_interruption_clears_quotes_and_releases_lock(self):
        conn=Mock(); conn.receive.side_effect=KeyboardInterrupt
        with self.assertRaises(KeyboardInterrupt): run(self.w,[INSTRUMENT],connector=lambda:conn,clock=lambda:NOW)
        self.assertEqual(read(self.w/"state/kite-stream.json")["state"],"stopped")
        from service import ProcessLock
        with ProcessLock(self.w/"state/kite-stream.lock"): pass

    def test_transport_suppresses_private_failure_and_refuses_redirects(self):
        module=Mock(); module.create_connection.side_effect=RuntimeError("SECRET_URL")
        with patch.dict("sys.modules",{"websocket":module}), patch.dict("os.environ",{"KITE_API_KEY":"dummy","KITE_ACCESS_TOKEN":"dummy"}):
            with self.assertRaises(Block) as caught: connect()
        self.assertNotIn("SECRET",str(caught.exception)); self.assertEqual(module.create_connection.call_args.kwargs["redirect_limit"],0)

    def test_invalid_runtime_bounds_do_not_connect(self):
        for kw in ({"duration":0},{"duration":43201},{"retries":6},{"retries":True}):
            with self.assertRaises(Block): run(self.w,[INSTRUMENT],clock=lambda:NOW,**kw)

    def test_capabilities_distinguish_stream_runner_from_rest_and_execution(self):
        from broker import main
        for broker in ("kite","upstox","dhan","angel","fyers"):
            with patch("sys.stdout",new_callable=io.StringIO) as output:
                self.assertEqual(main(["capabilities","--broker",broker]),0)
                result=json.loads(output.getvalue())
            self.assertTrue(result["streaming"]); self.assertFalse(result["live_execution"])
            self.assertEqual(result["streaming_runner"],"scripts/streaming.py" if broker=="kite" else "scripts/market_stream.py")
            self.assertFalse(result["streaming_authenticated_tested"])

    def test_non_upgrade_response_is_closed_and_rejected(self):
        module=Mock(); module.create_connection.return_value.getstatus.return_value=302
        with patch.dict("sys.modules",{"websocket":module}), patch.dict("os.environ",{"KITE_API_KEY":"dummy","KITE_ACCESS_TOKEN":"dummy"}):
            with self.assertRaises(Block): connect()
        module.create_connection.return_value.close.assert_called()

    def test_subscription_errors_are_redacted(self):
        module=Mock(); ws=module.create_connection.return_value; ws.getstatus.return_value=101
        ws.send.side_effect=RuntimeError("private token URL")
        with patch.dict("sys.modules",{"websocket":module}), patch.dict("os.environ",{"KITE_API_KEY":"dummy","KITE_ACCESS_TOKEN":"dummy"}):
            connection=connect()
            with self.assertRaises(Block) as caught: connection.send({"a":"subscribe","v":[123]})
        self.assertNotIn("private",str(caught.exception))

    def test_session_rollover_stops_without_reconnecting(self):
        current=[NOW]; elapsed=[0]; conn=Mock()
        def receive(): current[0]+=timedelta(days=1); elapsed[0]+=1; return None
        conn.receive.side_effect=receive
        r=run(self.w,[INSTRUMENT],duration=100,connector=lambda:conn,clock=lambda:current[0],monotonic=lambda:elapsed[0])
        self.assertEqual(r["reason"],"session_rollover"); self.assertEqual(r["connection_attempts"],1)

    def test_out_of_order_exchange_timestamp_does_not_replace_latest_quote(self):
        elapsed=[0]; conn=Mock(); observed=[]
        def receive():
            elapsed[0]+=1
            if elapsed[0]==2: return 2,packet(price=9999,trade=NOW-timedelta(seconds=1),exchange=NOW-timedelta(seconds=1))
            if elapsed[0]==3: observed.append(read_quote(self.w,INSTRUMENT,NOW)["price"])
            return 2,packet()
        conn.receive.side_effect=receive
        run(self.w,[INSTRUMENT],duration=3,connector=lambda:conn,clock=lambda:NOW,monotonic=lambda:elapsed[0])
        self.assertEqual(observed,[123.45])

    def test_tick_bursts_do_not_write_a_snapshot_for_every_packet(self):
        elapsed=[0.0]; conn=Mock()
        def receive(): elapsed[0]+=0.01; return 2,packet()
        conn.receive.side_effect=receive
        with patch("streaming.write",wraps=write) as writer:
            r=run(self.w,[INSTRUMENT],duration=2,connector=lambda:conn,clock=lambda:NOW,monotonic=lambda:elapsed[0])
        self.assertGreaterEqual(r["received_quotes"],190); self.assertLess(writer.call_count,10)


class DeploymentTests(unittest.TestCase):
    def spec(self,target="windows",component="monitor"):
        paths={"python":"C:\\Program Files\\Python\\python.exe","skill_dir":"D:\\My Skill","workdir":"D:\\Trading workspace","master":"D:\\Trading workspace\\master.json"} if target=="windows" else {"python":"/opt/python/bin/python","skill_dir":"/opt/My Skill","workdir":"/home/trader/work","master":"/home/trader/master.json"}
        return {"schema":"readonly_deployment_v1","target":target,"name":"india-monitor","component":component,"windows_user":"PC\\Trader", "cycles":12,"interval":5,"max_failures":3,"broker_poll":True,"duration":3600,"retries":3,**paths}

    def test_windows_xml_has_only_readonly_action_least_privilege_and_no_restart(self):
        files=render(self.spec()); root=ET.fromstring(files["india-monitor.xml"])
        def find(s): return root.find(".//"+"/".join("{"+NS+"}"+v for v in s.split("/"))).text
        self.assertEqual(find("Principal/RunLevel"),"LeastPrivilege"); self.assertEqual(find("LogonTrigger/UserId"),"PC\\Trader")
        self.assertIn('"D:\\My Skill\\scripts\\service.py" run',find("Exec/Arguments"))
        self.assertNotIn("live.py",files["india-monitor.xml"]); self.assertNotIn("RestartOnFailure",files["india-monitor.xml"])
        self.assertFalse(json.loads(files["operator-actions.json"])["installed"])

    def test_systemd_user_service_has_no_automatic_restart_and_stop_latch(self):
        body=render(self.spec("systemd"))["india-monitor.service"]
        self.assertIn("Restart=no",body); self.assertIn("WantedBy=default.target",body)
        self.assertIn('service.py" "stop"',body); self.assertNotIn("User=root",body)

    def test_systemd_paths_escape_specifiers_and_environment_expansion(self):
        spec=self.spec("systemd"); spec["workdir"]="/home/trader/$cash%literal"
        body=render(spec)["india-monitor.service"]
        self.assertIn('"/home/trader/$$cash%%literal"',body)

    def test_stream_service_points_only_to_bounded_streaming_runner(self):
        for target in ("windows","systemd"):
            files=render(self.spec(target,"stream")); body=files["india-monitor.xml" if target=="windows" else "india-monitor.service"]
            self.assertIn("streaming.py",body); self.assertIn("3600",body); self.assertNotIn("live.py",body)

    def test_live_components_traversal_control_chars_and_shell_injection_are_rejected(self):
        for change in ({"component":"live"},{"name":"abc;evil"},{"workdir":"..\\secret"},{"python":"C:\\bad\nname"},{"cycles":9000},{"broker_poll":"true"}):
            with self.subTest(change=change), self.assertRaises(Block): render({**self.spec(),**change})

    def test_environment_file_is_a_path_never_embedded_secret(self):
        spec=self.spec("systemd"); spec["environment_file"]="/home/trader/private.env"
        self.assertIn('EnvironmentFile="/home/trader/private.env"',render(spec)["india-monitor.service"])
        with self.assertRaises(Block): render({**self.spec(),"environment_file":"C:\\private.env"})

    def test_artifacts_are_exclusive_hashed_and_tamper_detected(self):
        with tempfile.TemporaryDirectory() as d:
            out=Path(d)/"deployment"; save(self.spec(),out)
            self.assertEqual(verify(out)["status"],"PASS")
            with self.assertRaises(Block): save(self.spec(),out)
            (out/"india-monitor.xml").write_text("changed")
            with self.assertRaises(Block): verify(out)

    def test_cli_only_renders_and_verifies_no_service_install(self):
        with tempfile.TemporaryDirectory() as d, patch("sys.stdout",new_callable=io.StringIO), patch("subprocess.run") as called:
            w=Path(d); write(w/"spec.json",self.spec())
            self.assertEqual(deploy_main(["render","--spec",str(w/"spec.json"),"--out",str(w/"out")]),0)
            self.assertEqual(deploy_main(["verify","--out",str(w/"out")]),0); called.assert_not_called()


class DailyContextTests(unittest.TestCase):
    def setUp(self):
        self.bundle,self.now=demo(); self.master="instrument_token,tradingsymbol,exchange,segment,instrument_type\n123,DEMO,NSE,NSE,EQ\n456,SECOND,NSE,NSE,EQ\n"
        self.csv=b"Symbol,Series,ISIN Code\nDEMO,EQ,IN0001\nSECOND,EQ,IN0002\n"
        self.client=Mock(); self.client.quotes.return_value={"source":"kite","missing":[],"data":{
            "NSE:DEMO":{"instrument_token":123,"last_price":102,"last_trade_time":"2026-11-06 09:34:50","ohlc":{"close":100}},
            "NSE:SECOND":{"instrument_token":456,"last_price":99,"last_trade_time":"2026-11-06 09:34:50","ohlc":{"close":100}}}}
        self.members={"nifty":{"index_id":"nifty","membership_date":"2026-11-06","source":"reviewed_fixture","as_of":NOW.isoformat(),"membership_verified":True,"members":["DEMO","SECOND"]}}

    def breadth(self,**kwargs): return collect_breadth(self.client,self.master,"nifty",self.bundle["session"],NOW,**kwargs)

    def test_public_membership_never_claims_verified_effective_date(self):
        b=self.breadth(fetcher=lambda _:self.csv)
        self.assertFalse(b["membership_verified"]); self.assertEqual(len(b["observations"]),2)
        self.bundle["indices"]["nifty"]["breadth"]=b
        report_breadth=report("nifty",self.bundle,NOW)["indices"][0]["breadth"]
        self.assertEqual(report_breadth["status"],"PARTIAL")
        self.assertEqual((report_breadth["advancing"],report_breadth["declining"]),(1,1))

    def test_reviewed_membership_can_produce_complete_breadth(self):
        self.bundle["indices"]["nifty"]["breadth"]=self.breadth(supplied=self.members)
        self.assertEqual(report("nifty",self.bundle,NOW)["indices"][0]["breadth"]["status"],"COMPLETE")

    def test_missing_stale_and_wrong_token_members_remain_missing(self):
        for change in ({"last_trade_time":"2026-11-06 09:30:00"},{"instrument_token":999},{"last_price":0}):
            self.client.quotes.return_value["data"]["NSE:SECOND"].update(change)
            b=self.breadth(supplied=self.members)
            self.assertEqual(b["unavailable_symbols"],["SECOND"]); self.assertEqual(len(b["observations"]),1)

    def test_sensex_membership_is_not_inferred_from_nse(self):
        with self.assertRaises(Block): membership("sensex",NOW,fetcher=lambda _:self.csv)

    def test_preopen_and_close_do_not_mislabel_live_quotes_as_final_closes(self):
        for phase in ("preopen","close"):
            bundle,now=demo(phase)
            with self.assertRaises(Block): collect_breadth(self.client,self.master,"nifty",bundle["session"],now,supplied=self.members)
        self.client.quotes.assert_not_called()

    def test_duplicate_wrong_index_and_undated_membership_block(self):
        for change in ({"members":["DEMO","DEMO"]},{"index_id":"sensex"},{"membership_date":"2026-11-05"},{"membership_verified":False}):
            with self.assertRaises(Block): membership("nifty",NOW,{"nifty":{**self.members["nifty"],**change}})

    def test_news_collects_today_without_asserting_complete_event_calendar(self):
        raw=b'<rss><channel><item><title>Result released</title><link>https://example.org/filing</link><pubDate>Fri, 06 Nov 2026 09:00:00 +0530</pubDate></item><item><title>old</title><link>https://example.org/old</link><pubDate>Thu, 05 Nov 2026 09:00:00 +0530</pubDate></item></channel></rss>'
        e=collect_news(self.bundle["session"],NOW,lambda _:raw)
        self.assertEqual(len(e["items"]),1); self.assertEqual(e["coverage"],"partial")

    def test_news_unknown_dates_remain_unparsed(self):
        e=collect_news(self.bundle["session"],NOW,lambda _:b'<rss><channel><item><title>Undated</title></item></channel></rss>')
        self.assertEqual(e["items"],[]); self.assertEqual(e["unparsed_count"],1)

    def test_enrichment_failure_preserves_index_report_and_reviewed_events(self):
        events=copy.deepcopy(self.bundle["events"])
        enrich(self.bundle,self.client,self.master,["nifty","sensex"],clock=lambda:NOW,fetcher=Mock(side_effect=Block("down")))
        self.assertEqual(self.bundle["events"],events)
        r=report("india-daily",self.bundle,NOW); self.assertEqual(r["coverage"]["available"],3)
        self.assertGreaterEqual(len(r["coverage_gaps"]),2)

    def test_unexpected_master_symbol_is_not_substituted(self):
        b=collect_breadth(self.client,self.master.replace("123,DEMO", "123,OTHER"),"nifty",self.bundle["session"],NOW,supplied=self.members)
        self.assertIn("DEMO",b["unavailable_symbols"])

    def test_context_cli_flags_never_fetch_during_demo(self):
        with patch("daily_collect.collect") as collector, patch("sys.stdout",new_callable=io.StringIO):
            self.assertEqual(daily_main(["/nifty","--demo","--with-context"]),2); collector.assert_not_called()

    def test_quote_time_is_checked_after_network_request(self):
        self.client.quotes.return_value["data"]["NSE:DEMO"]["last_trade_time"]="2026-11-06 09:35:02"
        later=NOW+timedelta(seconds=3)
        b=self.breadth(supplied=self.members,clock=lambda:later)
        self.assertEqual(len(b["observations"]),2); self.assertEqual(b["as_of"],later.isoformat())

    def test_news_time_is_checked_after_network_request(self):
        raw=b'<rss><channel><item><title>New</title><link>https://example.org/new</link><pubDate>Fri, 06 Nov 2026 09:35:02 +0530</pubDate></item></channel></rss>'
        e=collect_news(self.bundle["session"],NOW,lambda _:raw,lambda:NOW+timedelta(seconds=3))
        self.assertEqual(len(e["items"]),1)

    def test_context_network_request_crossing_session_boundary_blocks(self):
        with self.assertRaises(Block): self.breadth(supplied=self.members,clock=lambda:NOW.replace(hour=16))
        with self.assertRaises(Block): collect_news(self.bundle["session"],NOW,lambda _:b'<rss/>',lambda:NOW+timedelta(days=1))


if __name__ == "__main__": unittest.main()
