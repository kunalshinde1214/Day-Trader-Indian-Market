"""After-close official value and the closed-day message for the public daily route."""
from datetime import date, datetime, timedelta
import json
from pathlib import Path
import sys
import unittest
from urllib.parse import parse_qs, unquote, urlsplit

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT / "tests"))
from core import Block, IST, instant
import daily
from public_market import collect_daily
from test_daily_reports import payload

AFTER = instant("2026-11-18T16:00:00+05:30")
SATURDAY = instant("2026-11-21T10:00:00+05:30")
OFFICIAL = 25444.55


def after_close_fetcher(with_today_bar=True):
    def fetch(url):
        symbol = unquote(urlsplit(url).path.rsplit("/", 1)[-1])
        interval = parse_qs(urlsplit(url).query)["interval"][0]
        data = payload(symbol, interval, AFTER)
        result = data["chart"]["result"][0]
        q = result["indicators"]["quote"][0]
        if interval == "1d" and with_today_bar:
            result["timestamp"].append(int(datetime(2026, 11, 18, 9, 15, tzinfo=IST).timestamp()))
            for k, v in (("open", 25400.0), ("high", 25520.0), ("low", 25380.0), ("close", OFFICIAL), ("volume", 0)):
                q[k].append(v)
        if interval == "5m":
            start = AFTER.replace(hour=9, minute=15, second=0, microsecond=0)
            result["timestamp"] = [int((start + timedelta(minutes=5 * k)).timestamp()) for k in range(75)]
            closes = [25400.0 + k for k in range(75)]
            q.update(open=closes, high=[c + 5 for c in closes], low=[c - 5 for c in closes], close=closes, volume=[0] * 75)
        return json.dumps(data).encode()
    return fetch


class AfterCloseTests(unittest.TestCase):
    def report(self, **kw):
        bundle = collect_daily(["nifty"], fetcher=after_close_fetcher(**kw), clock=lambda: AFTER)
        return daily.report("nifty", bundle, AFTER, "close")

    def test_official_daily_close_replaces_the_last_five_minute_bar(self):
        r = self.report()
        index = r["indices"][0]
        self.assertEqual(index["value"], OFFICIAL)
        self.assertEqual(index["value_basis"], "official_daily_close")
        text = daily.markdown(r)
        self.assertIn("18 Nov close: **25,444.55**", text)
        self.assertNotIn("official close may differ", text)

    def test_without_the_daily_bar_the_five_minute_value_stays_labelled(self):
        r = self.report(with_today_bar=False)
        index = r["indices"][0]
        self.assertEqual(index["value_basis"], "last_completed_candle_close")
        self.assertIn("official close may differ", daily.markdown(r))


class ClosedDayTests(unittest.TestCase):
    def test_weekend_names_the_last_session_and_the_closing_command(self):
        def fetch(url):
            symbol = unquote(urlsplit(url).path.rsplit("/", 1)[-1])
            interval = parse_qs(urlsplit(url).query)["interval"][0]
            data = payload(symbol, interval, instant("2026-11-17T16:00:00+05:30"))  # provider still shows Tuesday's session
            return json.dumps(data).encode()
        with self.assertRaises(Block) as caught:
            collect_daily(["nifty"], fetcher=fetch, clock=lambda: SATURDAY)
        message = str(caught.exception)
        self.assertIn("last session was 2026-11-17", message)
        self.assertIn("--collect-close --session 2026-11-17", message)


if __name__ == "__main__":
    unittest.main()
