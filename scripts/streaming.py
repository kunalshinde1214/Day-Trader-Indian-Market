"""Bounded read-only Kite WebSocket quotes. No order actions or candle synthesis."""
from datetime import datetime
import argparse
import json
import os
from pathlib import Path
import struct
import sys
import time
from urllib.parse import urlencode

from core import Block, IST, fresh, instant, integer, positive, fingerprint, validate_instrument
from service import ProcessLock
from trader import read, write


def instruments_by_token(instruments, now):
    if not isinstance(instruments, list) or not 1 <= len(instruments) <= 500:
        raise Block("STREAM: select 1..500 tradable instruments")
    result = {}
    for i in instruments:
        validate_instrument(i, now)
        token = integer(i["token"])
        if i["broker"] != "kite" or token > 0xffffffff or token in result:
            raise Block("STREAM: unique Kite tokens from a same-day master required")
        result[token] = i
    return result


def decode(data, instruments, now):
    if not isinstance(data, bytes) or not 1 <= len(data) <= 1024 * 1024:
        raise Block("STREAM: invalid binary frame")
    if len(data) == 1: return []  # Provider heartbeat, not a price update.
    count = struct.unpack_from("!H", data)[0]
    if not 1 <= count <= 500: raise Block("STREAM: invalid packet count")
    offset = 2; result = []
    for _ in range(count):
        if offset + 2 > len(data): raise Block("STREAM: truncated packet length")
        length = struct.unpack_from("!H", data, offset)[0]; offset += 2
        # Only full tradable-instrument packets carry the required trade timestamp.
        if length != 184 or offset + length > len(data): raise Block("STREAM: full tradable quote packet required")
        packet = data[offset:offset + length]; offset += length
        token, paise = struct.unpack_from("!II", packet)
        if token not in instruments: raise Block("STREAM: unrequested instrument")
        last_trade = datetime.fromtimestamp(struct.unpack_from("!I", packet, 44)[0], IST)
        exchange = datetime.fromtimestamp(struct.unpack_from("!I", packet, 60)[0], IST)
        if exchange > now or last_trade > exchange or exchange.date() != now.date():
            raise Block("STREAM: invalid exchange/trade clock")
        i = instruments[token]
        result.append({"source": "kite_websocket", "broker": "kite", **{k: i[k] for k in ("exchange", "symbol", "token")},
                       "price": float(positive(paise) / 100), "as_of": last_trade.isoformat(),
                       "exchange_as_of": exchange.isoformat(), "received_at": now.isoformat(), "instrument_hash": fingerprint(i)})
    if offset != len(data): raise Block("STREAM: trailing binary data")
    return result


def connect():
    # Optional runtime dependency; ordinary paper analysis needs only stdlib.
    try:
        import websocket
    except ImportError: raise Block("STREAM: install requirements-streaming.txt in your local environment") from None
    key, token = os.environ.get("KITE_API_KEY"), os.environ.get("KITE_ACCESS_TOKEN")
    if not key or not token: raise Block("STREAM: local Kite credentials required")
    websocket.enableTrace(False)
    ws = None
    try:
        ws = websocket.create_connection("wss://ws.kite.trade/?" + urlencode({"api_key": key, "access_token": token}),
                                         timeout=10, redirect_limit=0, suppress_origin=True, enable_multithread=True)
        if ws.getstatus() != 101:
            ws.close(timeout=1)
            raise Block("STREAM: WebSocket upgrade required")
        ws.settimeout(1)
    except Exception:
        if ws:
            try: ws.close(timeout=1)
            except Exception: pass
        raise Block("STREAM: connection failed; private provider details suppressed") from None

    class Connection:
        def send(self, value):
            try: ws.send(json.dumps(value))
            except Exception: raise Block("STREAM: subscription send failed") from None
        def receive(self):
            try: return ws.recv_data(control_frame=True)
            except websocket.WebSocketTimeoutException: return None
            except Exception: raise Block("STREAM: connection lost") from None
        def close(self): ws.close(timeout=1)
    return Connection()


def run(workdir, instruments, *, duration=3600, retries=3, connector=connect, clock=instant, monotonic=time.monotonic, sleep=time.sleep):
    if type(duration) is not int or not 1 <= duration <= 43200 or type(retries) is not int or not 0 <= retries <= 5:
        raise Block("STREAM: duration 1..43200 seconds and retries 0..5 required")
    w = Path(workdir); mapping = instruments_by_token(instruments, clock()); path = w / "state/kite-stream.json"
    started = monotonic(); day = clock().date(); quotes = {}; attempts = 0; received = 0; reason = "duration_limit"
    def persist(state):
        write(path, {"source": "kite_websocket", "as_of": clock().isoformat(), "state": state, "quotes": quotes,
                     "connection_attempts": attempts, "received_quotes": received, "submitted": False})
    with ProcessLock(w / "state/kite-stream.lock"):
        try:
            while monotonic() - started < duration:
                if (w / "state/stream-stop.json").exists(): reason = "requested_stop"; break
                if clock().date() != day: reason = "session_rollover"; break
                connection = None; attempts += 1; quotes.clear(); persist("connecting")
                try:
                    connection = connector()
                    connection.send({"a": "subscribe", "v": list(mapping)})
                    connection.send({"a": "mode", "v": ["full", list(mapping)]})
                    persist("connected")
                    last_flush = None
                    while monotonic() - started < duration:
                        if (w / "state/stream-stop.json").exists() or clock().date() != day: break
                        frame = connection.receive()
                        if frame is not None:
                            opcode, data = frame
                            if opcode == 8: raise Block("STREAM: provider closed")
                            if opcode == 2:
                                for quote in decode(data, mapping, clock()):
                                    previous = quotes.get(quote["token"])
                                    if previous and instant(quote["exchange_as_of"]) < instant(previous["exchange_as_of"]): continue
                                    quotes[quote["token"]] = quote; received += 1
                            elif opcode == 1:
                                if len(data) > 65536: raise Block("STREAM: oversized text message")
                                message = json.loads(data)
                                if not isinstance(message, dict) or message.get("type") == "error": raise Block("STREAM: provider error")
                                # Order postbacks are deliberately not fills or execution authority.
                            elif opcode not in {9, 10}: raise Block("STREAM: unknown frame type")
                        # Keep the latest quotes in memory; cap atomic file writes
                        # during tick bursts while retaining the five-second heartbeat.
                        if last_flush is None or monotonic() - last_flush >= 0.5:
                            persist("connected"); last_flush = monotonic()
                    if (w / "state/stream-stop.json").exists(): reason = "requested_stop"
                    elif clock().date() != day: reason = "session_rollover"
                    break
                except (Block, ValueError, TypeError, KeyError, OSError, OverflowError):
                    quotes.clear(); persist("disconnected")
                    if attempts > retries: reason = "retry_limit"; break
                    remaining = duration - (monotonic() - started)
                    if remaining > 0: sleep(min(2 ** attempts, 30, remaining))
                finally:
                    if connection:
                        try: connection.close()
                        except Exception: pass
        finally:
            quotes.clear(); persist("stopped")
    return {"reason": reason, "connection_attempts": attempts, "received_quotes": received, "orders_submitted": 0}


def read_quote(workdir, instrument, now, max_age=60):
    validate_instrument(instrument, now)
    snapshot = read(Path(workdir) / "state/kite-stream.json")
    fresh(snapshot["as_of"], now, 5, "stream process heartbeat")
    if instrument["broker"] != "kite" or snapshot.get("source") != "kite_websocket" or snapshot.get("state") != "connected":
        raise Block("STREAM: current connected Kite stream required")
    quote = snapshot["quotes"][instrument["token"]]
    if quote.get("instrument_hash") != fingerprint(instrument): raise Block("STREAM: instrument changed; resubscribe")
    if any(quote.get(k) != instrument[k] for k in ("broker", "exchange", "symbol", "token")) or quote.get("source") != "kite_websocket":
        raise Block("STREAM: quote identity mismatch")
    for field in ("as_of", "exchange_as_of", "received_at"): fresh(quote[field], now, max_age, "stream quote")
    positive(quote["price"])
    return quote


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("command", choices=["run", "stop", "clear-stop", "status"]); p.add_argument("--workdir", required=True)
    p.add_argument("--master"); p.add_argument("--duration", type=int, default=3600); p.add_argument("--retries", type=int, default=3)
    a = p.parse_args(argv); w = Path(a.workdir)
    try:
        if a.command == "run":
            if not a.master: raise Block("STREAM: selected master required")
            result = run(w, read(a.master)["instruments"], duration=a.duration, retries=a.retries)
        elif a.command == "stop":
            write(w / "state/stream-stop.json", {"as_of": instant().isoformat()}); result = {"stop_requested": True}
        elif a.command == "clear-stop":
            with ProcessLock(w / "state/kite-stream.lock"): (w / "state/stream-stop.json").unlink(missing_ok=True)
            result = {"stop_latch_cleared": True}
        else:
            result = read(w / "state/kite-stream.json")
            result.pop("quotes", None)
            try: fresh(result["as_of"], instant(), 5, "stream heartbeat")
            except Block: result["state"] = "stale"
            result["process_liveness_verified"] = False
        print(json.dumps(result)); return 2 if result.get("reason") == "retry_limit" else 0
    except (Block, ValueError, KeyError, TypeError, OSError, OverflowError, KeyboardInterrupt):
        print(json.dumps({"status": "BLOCK_OR_STOPPED", "reason": "Stream unavailable; private details suppressed", "submitted": False})); return 2


if __name__ == "__main__": sys.exit(main())
