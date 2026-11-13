"""Read-only broker snapshot CLI; credentials remain in local environment variables."""
from __future__ import annotations
import argparse
import json
from pathlib import Path
import sys

from core import Block, BROKERS
from kite_readonly import KiteReadOnly
from upstox_readonly import UpstoxReadOnly
from dhan_readonly import DhanReadOnly
from angel_readonly import AngelReadOnly
from fyers_readonly import FyersReadOnly
from trader import read


def adapter(name):
    if name not in BROKERS:
        raise Block("BROKER_UNSUPPORTED: implemented adapters are kite, upstox, dhan, angel and fyers")
    return {"kite": KiteReadOnly, "upstox": UpstoxReadOnly, "dhan": DhanReadOnly, "angel": AngelReadOnly, "fyers": FyersReadOnly}[name]()


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=["capabilities", "instruments", "candles", "quotes", "account", "margin"])
    parser.add_argument("--broker", required=True)
    parser.add_argument("--out")
    parser.add_argument("--symbols", nargs="+")
    parser.add_argument("--instrument", help="Selected normalized instrument JSON from this broker's master")
    parser.add_argument("--from", dest="start")
    parser.add_argument("--to", dest="end")
    parser.add_argument("--interval", choices=["minute", "5minute", "day"], default="5minute")
    parser.add_argument("--intraday", action="store_true", help="Upstox current-day candles")
    parser.add_argument("--payload")
    a = parser.parse_args(argv)
    try:
        client = adapter(a.broker)
        if a.command == "capabilities":
            print(json.dumps({"broker": a.broker, "instruments": True, "candles": True, "quotes": True,
                "account": True, "margin_estimates": True, "streaming": True, "live_execution": False,
                "streaming_runner": "scripts/streaming.py" if a.broker=="kite" else "scripts/market_stream.py",
                "streaming_authenticated_tested": False,
                "authenticated_integration_tested": False}))
            return 0
        if not a.out:
            raise Block("OUTPUT: --out required for private local snapshots")
        out = Path(a.out)
        if out.exists():
            raise Block("OUTPUT: choose a new filename; snapshots are never overwritten")
        if a.command == "instruments":
            result = client.instruments()
        elif a.command == "candles":
            if not a.instrument:
                raise Block("INPUT: candles requires --instrument")
            instrument = read(a.instrument)
            if instrument.get("broker") != a.broker:
                raise Block("BROKER: instrument belongs to a different broker")
            if a.intraday and a.broker != "upstox":
                raise Block("INPUT: only Upstox uses --intraday; other adapters need --from/--to")
            if not a.intraday and not (a.start and a.end):
                raise Block("INPUT: candles requires --from and --to")
            kwargs = {"intraday": True} if a.intraday else {}
            if a.broker == "dhan":
                kwargs["instrument_type"] = instrument.get("broker_instrument")
            result = client.candles(instrument["token"], instrument["symbol"], instrument["exchange"],
                                    a.start, a.end, a.interval, **kwargs)
        elif a.command == "quotes":
            result = client.quotes(a.symbols or [])
        elif a.command == "account":
            result = client.account()
        else:
            if not a.payload:
                raise Block("INPUT: margin requires --payload from size")
            result = client.margin(read(a.payload))
        # Encode before opening, so invalid data cannot leave a half-written JSON snapshot.
        encoded = json.dumps(result, indent=2, allow_nan=False)
        out.parent.mkdir(parents=True, exist_ok=True)
        with out.open("x", encoding="utf-8") as stream:
            stream.write(encoded)
        print(json.dumps({"status": "saved", "source": a.broker, "path": str(out), "orders_submitted": 0}))
        return 0
    except Block as exc:
        print(json.dumps({"status": "BLOCK", "reason": str(exc), "orders_submitted": 0}))
        return 2
    except (ValueError, KeyError, TypeError, IndexError, OSError, OverflowError):
        print(json.dumps({"status": "BLOCK", "reason": "Invalid local input or broker response; raw content suppressed.", "orders_submitted": 0}))
        return 2


if __name__ == "__main__":
    sys.exit(main())
