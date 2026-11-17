"""Parsers against small excerpts of real responses recorded on 17 November 2026.

Synthetic tests prove the logic; these prove the code still reads the formats the providers
actually send. When a provider changes its format, re-record the excerpt and update the
expectations deliberately. Excerpts are trimmed to a few dozen rows for format testing only.
"""
import csv
import io
import json
from pathlib import Path
import sys
import unittest
import zipfile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
from core import instant
from data import rss
import fno
from india_news import importance, subject_of
from public_market import chart
import stock_analysis as sa

FIX = ROOT / "tests" / "fixtures" / "real"
RECORDED = instant("2026-11-17T23:42:11+05:30")


def text(name):
    return (FIX / name).read_bytes()


class RealFormatTests(unittest.TestCase):
    def test_yahoo_daily_chart(self):
        r = chart(text("yahoo_chart_reliance_ns.json"), "RELIANCE.NS", "1d", RECORDED)
        self.assertGreaterEqual(len(r["bars"]), 25)
        self.assertEqual(r["bars"][-1]["at"][:10], "2026-11-17")
        self.assertTrue(all(b["low"] <= b["close"] <= b["high"] for b in r["bars"]))
        self.assertTrue(all(b["volume"] > 0 for b in r["bars"]))

    def test_yahoo_fundamentals_inr_and_usd_reporters(self):
        fx = json.dumps({"chart": {"result": [{"meta": {"currency": "INR", "regularMarketPrice": 93.46}}]}}).encode()
        fetch = lambda name: (lambda url: fx if "INR=X" in url else text(name))
        rel = sa.fundamentals("RELIANCE.NS", fetcher=fetch("yahoo_fundamentals_reliance.json"), now=RECORDED)
        self.assertNotIn("_currency", rel)
        self.assertEqual(rel["annualDilutedEPS"][-1]["date"], "2026-03-31")
        self.assertGreater(rel["annualOrdinarySharesNumber"][-1]["value"], 1e10)
        infy = sa.fundamentals("INFY.NS", fetcher=fetch("yahoo_fundamentals_infy.json"), now=RECORDED)
        self.assertEqual(infy["_currency"]["reported"], "USD")
        raw = json.loads(text("yahoo_fundamentals_infy.json"))
        usd_eps = next(r for r in raw["timeseries"]["result"] if r["meta"]["type"][0] == "annualDilutedEPS")["annualDilutedEPS"][-1]["reportedValue"]["raw"]
        self.assertAlmostEqual(infy["annualDilutedEPS"][-1]["value"], usd_eps * 93.46)
        shares = next(r for r in raw["timeseries"]["result"] if r["meta"]["type"][0] == "annualOrdinarySharesNumber")["annualOrdinarySharesNumber"][-1]["reportedValue"]["raw"]
        self.assertEqual(infy["annualOrdinarySharesNumber"][-1]["value"], shares)  # counts are never converted

    def test_nse_fo_bhavcopy(self):
        archive = io.BytesIO()
        with zipfile.ZipFile(archive, "w") as z:
            z.writestr("BhavCopy_NSE_FO_0_0_0_20261117_F_0000.csv", text("nse_fo_bhavcopy_nifty_20261117.csv"))
        contracts = fno.parse_bhavcopy(archive.getvalue(), "NIFTY")
        r = fno.analyse_chain(contracts, "NIFTY")
        self.assertEqual(r["expiry"], "2026-11-23")
        self.assertEqual(r["lot_size"], 65)
        self.assertAlmostEqual(r["spot"], 25387.40)
        self.assertTrue(0.3 < r["pcr_oi"] < 3)
        self.assertTrue(0.05 < r["atm_iv"] < 0.5)
        self.assertLess(abs(r["forward"] / r["future"]["price"] - 1), 0.005)
        strikes = [row["strike"] for row in r["chain"]]
        self.assertTrue(min(strikes) <= r["max_pain"] <= max(strikes) or r["max_pain"] in strikes)
        p = fno.payoff(r, [fno.parse_leg("SELL:CE:25800:1"), fno.parse_leg("BUY:CE:26000:1")])
        self.assertAlmostEqual(p["max_loss"], p["net_premium"] - 200 * 65, places=6)

    def test_nse_announcements_feed(self):
        feed = rss(text("nse_announcements.xml"), "https://nsearchives.nseindia.com/content/RSS/Online_announcements.xml", RECORDED, nse_local_time=True)
        self.assertGreaterEqual(len(feed["records"]), 30)
        subjects = [subject_of(r.get("description")) for r in feed["records"]]
        self.assertGreater(sum(s is not None for s in subjects), len(subjects) * 0.8)
        levels = {importance(s, r.get("description")) for s, r in zip(subjects, feed["records"])}
        self.assertTrue(levels <= {"material", "notable", "routine"})
        self.assertIn("routine", levels)

    def test_fixture_headers_are_the_current_nse_layout(self):
        header = next(csv.reader(io.StringIO(text("nse_fo_bhavcopy_nifty_20261117.csv").decode())))
        for column in ("TckrSymb", "XpryDt", "StrkPric", "OptnTp", "ClsPric", "OpnIntrst", "ChngInOpnIntrst", "NewBrdLotQty", "UndrlygPric"):
            self.assertIn(column, header)


if __name__ == "__main__":
    unittest.main()
