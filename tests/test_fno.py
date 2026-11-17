"""Offline contracts for F&O analytics; never contacts the network."""
import contextlib
import csv
from datetime import date, datetime, time as dtime
import io
import json
import math
from pathlib import Path
import sys
import tempfile
import unittest
import xml.dom.minidom
import zipfile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
from core import Block, IST
import fno

TRADE = "2026-11-17"
EXPIRY = "2026-11-23"
SPOT, VOL, RATE, LOT = 25000.0, 0.15, 0.065, 65
AS_OF = datetime.combine(date.fromisoformat(TRADE), dtime(15, 30), IST)
YEARS = fno.years_to_expiry(EXPIRY, AS_OF)
FORWARD = SPOT * math.exp(RATE * YEARS)
FIELDS = ["TradDt", "BizDt", "Sgmt", "Src", "FinInstrmTp", "FinInstrmId", "ISIN", "TckrSymb", "SctySrs", "XpryDt", "FininstrmActlXpryDt",
          "StrkPric", "OptnTp", "FinInstrmNm", "OpnPric", "HghPric", "LwPric", "ClsPric", "LastPric", "PrvsClsgPric", "UndrlygPric",
          "SttlmPric", "OpnIntrst", "ChngInOpnIntrst", "TtlTradgVol", "TtlTrfVal", "TtlNbOfTxsExctd", "SsnId", "NewBrdLotQty", "Rmks",
          "Rsvd1", "Rsvd2", "Rsvd3", "Rsvd4"]
# Call OI concentrated at 25500, put OI at 24500; most total pain is avoided at 25000.
CALL_OI = {24500: 20, 24750: 30, 25000: 60, 25250: 80, 25500: 200}
PUT_OI = {24500: 220, 24750: 90, 25000: 70, 25250: 30, 25500: 10}


def row(**kw):
    base = {k: "" for k in FIELDS}
    base.update(TradDt=TRADE, BizDt=TRADE, Sgmt="FO", Src="NSE", TckrSymb="NIFTY", UndrlygPric=f"{SPOT}", NewBrdLotQty=str(LOT), SsnId="F1")
    base.update({k: str(v) for k, v in kw.items()})
    return base


def bhavcopy(untraded=(), extra=()):
    rows = []
    for kind, book in (("CE", CALL_OI), ("PE", PUT_OI)):
        for strike, oi_lots in book.items():
            price = round(fno.black76(kind, FORWARD, strike, YEARS, VOL, RATE), 2)
            traded = (kind, strike) not in untraded
            rows.append(row(FinInstrmTp="IDO", XpryDt=EXPIRY, StrkPric=f"{strike:.2f}", OptnTp=kind, ClsPric=price,
                            PrvsClsgPric=round(price * (0.9 if kind == "CE" else 1.1), 2), SttlmPric=price,
                            OpnIntrst=oi_lots * LOT, ChngInOpnIntrst=oi_lots * LOT // 4, TtlTradgVol=1000 if traded else 0))
    rows.append(row(FinInstrmTp="IDO", XpryDt=TRADE, StrkPric="25000.00", OptnTp="CE", ClsPric=1, PrvsClsgPric=1, SttlmPric=1,
                    OpnIntrst=0, ChngInOpnIntrst=0, TtlTradgVol=0))  # expired today, must be excluded
    rows.append(row(FinInstrmTp="IDF", XpryDt=EXPIRY, ClsPric=round(FORWARD, 2), PrvsClsgPric=24950, SttlmPric=round(FORWARD, 2),
                    OpnIntrst=6500, ChngInOpnIntrst=-650, TtlTradgVol=500))
    rows.append(row(TckrSymb="RELIANCE", FinInstrmTp="STO", XpryDt=EXPIRY, StrkPric="1300.00", OptnTp="CE", ClsPric=10,
                    PrvsClsgPric=9, SttlmPric=10, OpnIntrst=500, ChngInOpnIntrst=0, TtlTradgVol=1, NewBrdLotQty=500))
    rows.extend(extra)
    text = io.StringIO()
    writer = csv.DictWriter(text, FIELDS)
    writer.writeheader()
    writer.writerows(rows)
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w") as z:
        z.writestr(f"BhavCopy_NSE_FO_0_0_0_{TRADE.replace('-', '')}_F_0000.csv", text.getvalue())
    return out.getvalue()


def report(**kw):
    contracts = fno.parse_bhavcopy(bhavcopy(**kw), "NIFTY")
    return fno.analyse_chain(contracts, "NIFTY", rate=RATE, width=10)


class FnoTests(unittest.TestCase):
    def test_symbol_normalisation(self):
        self.assertEqual(fno.underlying("bank nifty"), "BANKNIFTY")
        self.assertEqual(fno.underlying("reliance.ns"), "RELIANCE")
        with self.assertRaises(Block):
            fno.underlying("NIFTY; rm -rf /")

    def test_black76_put_call_parity_and_iv_roundtrip(self):
        c = fno.black76("CE", FORWARD, 25200, YEARS, VOL, RATE)
        p = fno.black76("PE", FORWARD, 25200, YEARS, VOL, RATE)
        self.assertAlmostEqual(c - p, math.exp(-RATE * YEARS) * (FORWARD - 25200), places=6)
        for kind in ("CE", "PE"):
            price = fno.black76(kind, FORWARD, 24800, YEARS, 0.22, RATE)
            self.assertAlmostEqual(fno.implied_vol(kind, price, FORWARD, 24800, YEARS, RATE), 0.22, places=4)
        self.assertIsNone(fno.implied_vol("CE", 0.01, FORWARD, 20000, YEARS, RATE))  # below intrinsic

    def test_greeks_signs_and_bounds(self):
        call = fno.greeks("CE", FORWARD, 25000, YEARS, VOL, RATE)
        put = fno.greeks("PE", FORWARD, 25000, YEARS, VOL, RATE)
        self.assertTrue(0 < call["delta"] < 1 and -1 < put["delta"] < 0)
        self.assertAlmostEqual(call["delta"] - put["delta"], 1.0, places=9)
        self.assertGreater(call["gamma"], 0)
        self.assertLess(call["theta"], 0)
        self.assertGreater(call["vega"], 0)

    def test_chain_summary(self):
        r = report()
        self.assertEqual(r["expiry"], EXPIRY)
        self.assertEqual(r["expiries"], [EXPIRY])
        self.assertAlmostEqual(r["forward"], FORWARD, delta=1.0)
        self.assertAlmostEqual(r["atm_iv"], VOL, places=2)
        self.assertAlmostEqual(r["pcr_oi"], sum(PUT_OI.values()) / sum(CALL_OI.values()))
        self.assertEqual(r["max_pain"], 25000)
        self.assertEqual(r["resistance_oi"][0], 25500)
        self.assertEqual(r["support_oi"][0], 24500)
        self.assertEqual(r["lot_size"], LOT)
        self.assertEqual(r["future"]["buildup"], "short covering")
        self.assertAlmostEqual(r["straddle"], fno.price_of(r["_books"][0][25000]) + fno.price_of(r["_books"][1][25000]))
        self.assertFalse(r["execution_eligible"])
        self.assertFalse(r["submitted"])

    def test_buildup_classification(self):
        r = report()
        self.assertIn("long build-up", r["call_buildup"])   # call price up, OI up
        self.assertIn("short build-up", r["put_buildup"])   # put price down, OI up
        c = {"prev_close": 10, "close": 12, "volume": 1, "oi_change": -5}
        self.assertEqual(fno.buildup(c), "short covering")
        self.assertEqual(fno.buildup({**c, "close": 8}), "long unwinding")
        self.assertIsNone(fno.buildup({**c, "volume": 0}))

    def test_untraded_strikes_get_no_iv(self):
        r = report(untraded={("CE", 25500)})
        row = next(x for x in r["chain"] if x["strike"] == 25500)
        self.assertIsNone(row["ce"]["iv"])
        self.assertIsNone(row["ce"]["close"])
        self.assertIsNotNone(row["ce"]["stale_close"])

    def test_iron_condor_payoff(self):
        r = report()
        legs = [fno.parse_leg(x) for x in ("SELL:CE:25250:1", "BUY:CE:25500:1", "SELL:PE:24750:1", "BUY:PE:24500:1")]
        p = fno.payoff(r, legs)
        calls, puts = r["_books"]
        credit = (calls[25250]["close"] - calls[25500]["close"] + puts[24750]["close"] - puts[24500]["close"]) * LOT
        self.assertAlmostEqual(p["net_premium"], credit, places=6)
        self.assertAlmostEqual(p["max_profit"], credit, places=6)
        self.assertAlmostEqual(p["max_loss"], credit - 250 * LOT, places=6)
        self.assertEqual(len(p["breakevens"]), 2)
        self.assertAlmostEqual(p["breakevens"][0], 24750 - credit / LOT, places=4)
        self.assertAlmostEqual(p["breakevens"][1], 25250 + credit / LOT, places=4)
        self.assertIsNotNone(p["position_greeks"])

    def test_unbounded_and_zero_bounded_extremes(self):
        r = report()
        long_call = fno.payoff(r, [fno.parse_leg("BUY:CE:25000:1")])
        self.assertIsNone(long_call["max_profit"])
        short_call = fno.payoff(r, [fno.parse_leg("SELL:CE:25000:1")])
        self.assertIsNone(short_call["max_loss"])
        long_put = fno.payoff(r, [fno.parse_leg("BUY:PE:25000:2")])
        self.assertAlmostEqual(long_put["max_profit"], (25000 - r["_books"][1][25000]["close"]) * 2 * LOT, places=6)
        self.assertTrue(long_put["extreme_at_zero"]["profit"])
        fut = fno.payoff(r, [fno.parse_leg("BUY:FUT::1")])
        self.assertAlmostEqual(fut["position_greeks"]["delta"], LOT)

    def test_leg_validation(self):
        for bad in ("BUY:CE::1", "HOLD:CE:25000:1", "BUY:CE:25000:0", "BUY:XX:25000:1"):
            with self.subTest(bad=bad), self.assertRaises(Block):
                fno.parse_leg(bad)
        r = report(untraded={("CE", 25500)})
        with self.assertRaisesRegex(Block, "did not trade"):
            fno.payoff(r, [fno.parse_leg("BUY:CE:25500:1")])
        self.assertEqual(fno.payoff(r, [fno.parse_leg("BUY:CE:25500:1:12.5")])["legs"][0]["price"], 12.5)
        with self.assertRaisesRegex(Block, "not listed"):
            fno.payoff(r, [fno.parse_leg("BUY:CE:25333:1")])

    def test_unknown_symbol_and_expiry(self):
        with self.assertRaisesRegex(Block, "no F&O contracts"):
            fno.parse_bhavcopy(bhavcopy(), "NOTASTOCK")
        contracts = fno.parse_bhavcopy(bhavcopy(), "NIFTY")
        with self.assertRaisesRegex(Block, "choose from"):
            fno.analyse_chain(contracts, "NIFTY", expiry="2030-01-01")

    def test_fetch_walks_back_over_missing_days(self):
        calls = []

        def fetcher(url):
            calls.append(url)
            if "20261117" in url:
                return bhavcopy()
            raise Block("PUBLIC: source unavailable")
        contracts, url = fno.fetch_bhavcopy("NIFTY", fetcher=fetcher, today=date(2026, 11, 19))
        self.assertIn("20261117", url)
        self.assertEqual(len(calls), 3)  # 19th, 18th, 17th
        with self.assertRaisesRegex(Block, "no NSE F&O bhavcopy"):
            fno.fetch_bhavcopy("NIFTY", fetcher=lambda u: (_ for _ in ()).throw(Block("x")), today=date(2026, 11, 19), lookback=3)

    def test_input_chain_mode(self):
        rows = [{"type": k, "strike": s, "last": round(fno.black76(k, FORWARD, s, YEARS, VOL, RATE), 2), "prev_close": 1,
                 "oi": 1000, "oi_change": 10, "volume": 50} for k in ("CE", "PE") for s in (24750, 25000, 25250)]
        data = {"schema": "fno_chain_v1", "underlying": "NIFTY", "as_of": f"{EXPIRY}T10:00:00+05:30", "source": "test connector",
                "spot": SPOT, "lot_size": LOT, "expiry": EXPIRY, "rows": rows}
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "chain.json"
            path.write_text(json.dumps(data), encoding="utf-8")
            contracts, source, as_of = fno.load_input(path, "NIFTY")
            r = fno.analyse_chain(contracts, "NIFTY", as_of=as_of, rate=RATE, source=source)
            self.assertEqual(r["expiry"], EXPIRY)  # expiry-day chains stay usable
            self.assertEqual(r["source"], "test connector")
            path.write_text(json.dumps({**data, "underlying": "BANKNIFTY"}), encoding="utf-8")
            with self.assertRaises(Block):
                fno.load_input(path, "NIFTY")

    def test_svg_chart_is_valid_and_marks_breakevens(self):
        r = report()
        p = fno.payoff(r, [fno.parse_leg("BUY:CE:25000:1"), fno.parse_leg("BUY:PE:25000:1")])
        svg = fno.svg_chart(r, p)
        xml.dom.minidom.parseString(svg.encode())
        self.assertEqual(svg.count("BE "), 2)

    def test_markdown_reports(self):
        r = report()
        text = fno.chain_markdown(r, {"vix": 14.0, "date": TRADE, "percentile_1y": 0.4, "low_1y": 10, "high_1y": 25})
        for part in ("## Summary", "PCR (open interest)", "Max pain", "India VIX", "## Chain around the money", "not investment advice"):
            self.assertIn(part, text)
        p = fno.payoff(r, [fno.parse_leg("SELL:CE:25250:1")])
        self.assertIn("unlimited", fno.payoff_markdown(r, p))

    def test_cli_refuses_to_overwrite_output(self):
        with tempfile.TemporaryDirectory() as d, contextlib.redirect_stderr(io.StringIO()):
            out = Path(d) / "existing.md"
            out.write_text("keep", encoding="utf-8")
            data = {"schema": "fno_chain_v1", "underlying": "NIFTY", "as_of": f"{TRADE}T12:00:00+05:30", "spot": SPOT, "lot_size": LOT,
                    "expiry": EXPIRY, "rows": [{"type": "CE", "strike": 25000, "last": 200, "oi": 1, "volume": 1},
                                               {"type": "PE", "strike": 25000, "last": 190, "oi": 1, "volume": 1}]}
            chain = Path(d) / "chain.json"
            chain.write_text(json.dumps(data), encoding="utf-8")
            self.assertEqual(fno.main(["chain", "NIFTY", "--input", str(chain), "--out", str(out), "--no-vix"]), 2)
            self.assertEqual(out.read_text(encoding="utf-8"), "keep")


if __name__ == "__main__":
    unittest.main()
