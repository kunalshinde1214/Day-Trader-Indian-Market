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


def row(trade=TRADE, spot=SPOT, **kw):
    base = {k: "" for k in FIELDS}
    base.update(TradDt=trade, BizDt=trade, Sgmt="FO", Src="NSE", TckrSymb="NIFTY", UndrlygPric=f"{spot}", NewBrdLotQty=str(LOT), SsnId="F1")
    base.update({k: str(v) for k, v in kw.items()})
    return base


def bhavcopy(untraded=(), extra=(), *, trade=TRADE, spot=SPOT, vol=VOL, call_oi=CALL_OI, put_oi=PUT_OI, tweaks=None):
    """Synthetic NSE F&O bhavcopy priced with Black-76; `tweaks` maps (type, strike) to column overrides."""
    years = fno.years_to_expiry(EXPIRY, datetime.combine(date.fromisoformat(trade), dtime(15, 30), IST))
    forward = spot * math.exp(RATE * years)
    rows = []
    for kind, book in (("CE", call_oi), ("PE", put_oi)):
        for strike, oi_lots in book.items():
            price = round(fno.black76(kind, forward, strike, years, vol, RATE), 2)
            traded = (kind, strike) not in untraded
            fields = dict(FinInstrmTp="IDO", XpryDt=EXPIRY, StrkPric=f"{strike:.2f}", OptnTp=kind, ClsPric=price,
                          PrvsClsgPric=round(price * (0.9 if kind == "CE" else 1.1), 2), SttlmPric=price,
                          OpnIntrst=oi_lots * LOT, ChngInOpnIntrst=oi_lots * LOT // 4, TtlTradgVol=1000 if traded else 0)
            fields.update((tweaks or {}).get((kind, strike), {}))
            rows.append(row(trade, spot, **fields))
    rows.append(row(trade, spot, FinInstrmTp="IDO", XpryDt=trade, StrkPric="25000.00", OptnTp="CE", ClsPric=1, PrvsClsgPric=1, SttlmPric=1,
                    OpnIntrst=0, ChngInOpnIntrst=0, TtlTradgVol=0))  # expired today, must be excluded
    rows.append(row(trade, spot, FinInstrmTp="IDF", XpryDt=EXPIRY, ClsPric=round(forward, 2), PrvsClsgPric=24950, SttlmPric=round(forward, 2),
                    OpnIntrst=6500, ChngInOpnIntrst=-650, TtlTradgVol=500))
    rows.append(row(trade, spot, TckrSymb="RELIANCE", FinInstrmTp="STO", XpryDt=EXPIRY, StrkPric="1300.00", OptnTp="CE", ClsPric=10,
                    PrvsClsgPric=9, SttlmPric=10, OpnIntrst=500, ChngInOpnIntrst=0, TtlTradgVol=1, NewBrdLotQty=500))
    rows.extend(extra)
    text = io.StringIO()
    writer = csv.DictWriter(text, FIELDS)
    writer.writeheader()
    writer.writerows(rows)
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w") as z:
        z.writestr(f"BhavCopy_NSE_FO_0_0_0_{trade.replace('-', '')}_F_0000.csv", text.getvalue())
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
        self.assertGreater(r["call_oi_breakdown"]["added"]["long build-up"], 0)   # call price up, OI up
        self.assertGreater(r["put_oi_breakdown"]["added"]["short build-up"], 0)   # put price down, OI up
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



PREV = "2026-11-13"  # Friday; Monday 2026-11-16 is treated as a holiday by the fake fetcher
PREV_SPOT, PREV_VOL = 24900.0, 0.17
# Max pain on the previous day is 24750 (pain 21,250 lots x points vs 37,500 at 24500 and 50,000 at 25000).
PREV_CALL_OI = {24500: 30, 24750: 120, 25000: 100, 25250: 60, 25500: 40}
PREV_PUT_OI = {24500: 150, 24750: 60, 25000: 20, 25250: 10, 25500: 5}
NOW = datetime(2026, 11, 18, 10, 0, tzinfo=IST)
VIX_DAYS = ["2026-11-12", "2026-11-13", "2026-11-16", "2026-11-17", "2026-11-18"]
VIX_CLOSES = [13.0, 12.0, 15.0, 14.0, 18.0]


def vix_chart(days, closes):
    stamps = [int(datetime.combine(date.fromisoformat(d), dtime(9, 15), IST).timestamp()) for d in days]
    return json.dumps({"chart": {"error": None, "result": [{
        "meta": {"symbol": "^INDIAVIX", "currency": "INR", "exchangeTimezoneName": "Asia/Kolkata", "dataGranularity": "1d"},
        "timestamp": stamps, "indicators": {"quote": [{"open": closes, "high": [c + 0.5 for c in closes], "low": [c - 0.5 for c in closes],
                                                       "close": closes, "volume": [None] * len(closes)}]}}]}}).encode()


class FakeFeeds:
    """Serves the TRADE and PREV bhavcopies and a VIX history; everything else is unavailable. Records requested URLs."""

    def __init__(self, previous=True):
        self.urls, self.previous = [], previous

    def __call__(self, url):
        self.urls.append(url)
        if "INDIAVIX" in url:
            return vix_chart(VIX_DAYS, VIX_CLOSES)
        if TRADE.replace("-", "") in url:
            return bhavcopy()
        if self.previous and PREV.replace("-", "") in url:
            return bhavcopy(trade=PREV, spot=PREV_SPOT, vol=PREV_VOL, call_oi=PREV_CALL_OI, put_oi=PREV_PUT_OI)
        raise Block("PUBLIC: source unavailable")


def run_cli(argv, fetcher):
    out = io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(io.StringIO()):
        code = fno.main(argv, fetcher=fetcher, clock=lambda: NOW)
    return code, out.getvalue()


SPREAD = ["--leg", "SELL:CE:25000:1", "--leg", "BUY:CE:25250:1"]


class FnoImprovementTests(unittest.TestCase):
    def test_oi_breakdown_is_signed_and_reconciles_to_total(self):
        tweaks = {("CE", 25500): {"ChngInOpnIntrst": -40 * LOT},                         # price up, OI down: short covering
                  ("CE", 24500): {"ChngInOpnIntrst": -8 * LOT, "PrvsClsgPric": 99999},   # price down, OI down: long unwinding
                  ("PE", 24500): {"ChngInOpnIntrst": -30 * LOT, "PrvsClsgPric": 0.05},   # price up, OI down: short covering
                  ("CE", 25250): {"ChngInOpnIntrst": 5 * LOT, "TtlTradgVol": 0}}         # no trade: unclassified
        r = fno.analyse_chain(fno.parse_bhavcopy(bhavcopy(tweaks=tweaks), "NIFTY"), "NIFTY", rate=RATE)
        for side in ("call", "put"):
            b = r[f"{side}_oi_breakdown"]
            with self.subTest(side=side):
                self.assertTrue(all(v >= 0 for v in b["added"].values()))
                self.assertTrue(all(v <= 0 for v in b["reduced"].values()))
                self.assertAlmostEqual(b["added_total"] + b["reduced_total"] + b["unclassified"], b["net"])
                self.assertAlmostEqual(b["net"], r[f"{side}_oi_change"])
        calls = r["call_oi_breakdown"]
        self.assertEqual(calls["reduced"]["short covering"], -40 * LOT)
        self.assertEqual(calls["reduced"]["long unwinding"], -8 * LOT)
        self.assertEqual(calls["unclassified"], 5 * LOT)
        self.assertEqual(r["put_oi_breakdown"]["reduced"]["short covering"], -30 * LOT)
        text = fno.chain_markdown(r)
        self.assertIn("| Reduced: short covering (price up, OI down) | -40 | -30 |", text)
        net_calls = fno.signed_contracts(r["call_oi_change"], LOT)
        self.assertIn(f"| **Net OI change** (= summary change) | {net_calls} |", text)
        self.assertIn(f"/ {net_calls} |", text)  # the same number in the summary table

    def test_day_over_day_walks_back_and_compares_same_expiry(self):
        feeds = FakeFeeds()
        r = report()
        dod = fno.compare_previous(r, "NIFTY", fetcher=feeds)
        self.assertEqual(dod["status"], "ok")
        self.assertEqual(dod["previous_date"], PREV)
        self.assertEqual([u.split("_")[-3] for u in feeds.urls], ["20261116", "20261113"])  # Monday holiday, weekend skipped
        m = dod["metrics"]
        prev_pcr = sum(PREV_PUT_OI.values()) / sum(PREV_CALL_OI.values())
        self.assertAlmostEqual(m["pcr_oi"]["previous"], prev_pcr)
        self.assertAlmostEqual(m["pcr_oi"]["change"], r["pcr_oi"] - prev_pcr)
        self.assertEqual((m["max_pain"]["previous"], m["max_pain"]["current"], m["max_pain"]["change"]), (24750, 25000, 250))
        self.assertAlmostEqual(m["atm_iv"]["previous"], PREV_VOL, places=2)
        self.assertAlmostEqual(m["atm_iv"]["change"], VOL - PREV_VOL, places=2)
        self.assertEqual(m["call_oi"]["change"], (sum(CALL_OI.values()) - sum(PREV_CALL_OI.values())) * LOT)
        self.assertEqual(m["put_oi"]["change"], (sum(PUT_OI.values()) - sum(PREV_PUT_OI.values())) * LOT)
        self.assertEqual(m["spot"]["change"], SPOT - PREV_SPOT)
        r["day_over_day"] = dod
        text = fno.chain_markdown(r)
        self.assertIn(f"## Day over day (same expiry {EXPIRY})", text)
        self.assertIn("| Max pain | 24,750 | 25,000 | +250 |", text)

    def test_day_over_day_unavailable_or_skipped_is_reported(self):
        r = report()
        dod = fno.compare_previous(r, "NIFTY", fetcher=FakeFeeds(previous=False))
        self.assertEqual(dod["status"], "unavailable")
        self.assertIn("unavailable", dod["note"])
        with self.assertRaisesRegex(Block, "before the data date"):
            fno.compare_previous(r, "NIFTY", fetcher=FakeFeeds(), on=TRADE)
        code, text = run_cli(["chain", "NIFTY", "--no-vix"], FakeFeeds(previous=False))
        self.assertEqual(code, 0)
        self.assertIn("day-over-day comparison skipped", text)
        self.assertIn("## Chain around the money", text)
        feeds = FakeFeeds()
        code, text = run_cli(["chain", "NIFTY", "--no-vix", "--no-compare"], feeds)
        self.assertEqual(code, 0)
        self.assertEqual(len(feeds.urls), 2)  # the 18th (missing) and the 17th only; no previous-day download
        self.assertIn("--no-compare", text)

    def test_input_mode_compares_only_with_previous_date(self):
        rows = [{"type": k, "strike": s, "last": round(fno.black76(k, FORWARD, s, YEARS, VOL, RATE), 2), "prev_close": 1,
                 "oi": 1000 * LOT, "oi_change": 10, "volume": 50} for k in ("CE", "PE") for s in CALL_OI]
        data = {"schema": "fno_chain_v1", "underlying": "NIFTY", "as_of": f"{TRADE}T15:30:00+05:30", "source": "test connector",
                "spot": SPOT, "lot_size": LOT, "expiry": EXPIRY, "rows": rows}
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "chain.json"
            path.write_text(json.dumps(data), encoding="utf-8")
            feeds = FakeFeeds()
            code, text = run_cli(["chain", "NIFTY", "--input", str(path), "--no-vix", "--format", "json"], feeds)
            self.assertEqual(code, 0)
            self.assertEqual(json.loads(text)["day_over_day"]["status"], "skipped")
            self.assertEqual(feeds.urls, [])
            code, text = run_cli(["chain", "NIFTY", "--input", str(path), "--no-vix", "--format", "json", "--previous-date", "2026-11-16"], feeds)
            dod = json.loads(text)["day_over_day"]
            self.assertEqual((dod["status"], dod["previous_date"]), ("ok", PREV))
            self.assertIn("supplied chain", dod["note"])

    def test_vix_uses_the_chain_trading_date(self):
        fetch = lambda url: vix_chart(VIX_DAYS, VIX_CLOSES)
        v = fno.vix_context(fetcher=fetch, now=NOW, on=TRADE)
        self.assertEqual((v["vix"], v["date"], v["exact_date"], v["note"]), (14.0, TRADE, True, None))
        self.assertAlmostEqual(v["percentile_1y"], 3 / 4)  # 14 against 13, 12, 15, 14; the newer 18.0 is excluded
        self.assertEqual(v["high_1y"], 15.0)
        missing = fno.vix_context(fetcher=fetch, now=NOW, on="2026-11-15")
        self.assertEqual((missing["vix"], missing["date"], missing["exact_date"]), (12.0, "2026-11-13", False))
        self.assertIn("2026-11-15", missing["note"])
        self.assertEqual(fno.vix_context(fetcher=fetch, now=NOW)["vix"], 18.0)  # no date: latest close
        with self.assertRaises(Block):
            fno.vix_context(fetcher=fetch, now=NOW, on="2025-01-01")
        code, text = run_cli(["chain", "NIFTY", "--no-compare"], FakeFeeds())
        self.assertIn("| India VIX | 14.00 (close 2026-11-17)", text)

    def test_payoff_table_rows_include_strikes_breakevens_and_spot(self):
        r = report()
        for legs in (["SELL:CE:25000:1", "BUY:CE:25250:1"], ["BUY:CE:25000:1"],
                     ["SELL:CE:25250:1", "BUY:CE:25500:1", "SELL:PE:24750:1", "BUY:PE:24500:1"]):
            with self.subTest(legs=legs):
                p = fno.payoff(r, [fno.parse_leg(x) for x in legs])
                prices = [t["price"] for t in p["table"]]
                self.assertTrue(15 <= len(prices) <= 20, len(prices))
                self.assertEqual(prices, sorted(set(prices)))
                self.assertTrue({fno.parse_leg(x)["strike"] for x in legs} <= set(prices))
                self.assertTrue({round(b, 2) for b in p["breakevens"]} <= set(prices))
                self.assertIn(round(SPOT, 2), prices)
                for t in p["table"]:
                    if "breakeven" in t["marks"]:
                        self.assertAlmostEqual(t["expiry_pnl"], 0, delta=LOT * 0.01)
                text = fno.payoff_markdown(r, p)
                self.assertIn("| Underlying | At expiry ₹ | Today (T+0) ₹ | Note |", text)
                body = text.split("| ---: | ---: | ---: | --- |")[1].strip().split("\n\n")[0].splitlines()
                self.assertEqual(len(body), len(p["table"]))

    def test_probabilities_match_closed_form_lognormal(self):
        r = report()
        p = fno.payoff(r, [fno.parse_leg("BUY:CE:25000:1")])
        pr = p["probabilities"]
        self.assertTrue(pr["available"])
        self.assertEqual(pr["label"], "implied by option prices, not a forecast")
        F, sigma, T = r["forward"], r["atm_iv"], r["_years"]
        be = 25000 + r["_books"][0][25000]["close"]
        self.assertAlmostEqual(p["breakevens"][0], be, places=6)
        sd = sigma * math.sqrt(T)
        closed = 0.5 * (1 + math.erf((math.log(F / be) - sd * sd / 2) / sd / math.sqrt(2)))  # N(d2) at the breakeven
        self.assertAlmostEqual(pr["profit"], closed, places=9)
        self.assertAlmostEqual(pr["above_breakevens"][0]["prob_above"], closed, places=9)
        lo, hi = pr["range_1sd"]
        self.assertAlmostEqual(fno.prob_above(lo, F, sigma, T) - fno.prob_above(hi, F, sigma, T), math.erf(1 / math.sqrt(2)), places=9)
        self.assertAlmostEqual(math.sqrt(lo * hi), F * math.exp(-sd * sd / 2), places=6)
        # A short strangle profits between its breakevens; a long put profits below its single breakeven.
        strangle = fno.payoff(r, [fno.parse_leg("SELL:CE:25250:1"), fno.parse_leg("SELL:PE:24750:1")])
        b1, b2 = strangle["breakevens"]
        self.assertAlmostEqual(strangle["probabilities"]["profit"], fno.prob_above(b1, F, sigma, T) - fno.prob_above(b2, F, sigma, T), places=9)
        put = fno.payoff(r, [fno.parse_leg("BUY:PE:25000:1")])
        self.assertAlmostEqual(put["probabilities"]["profit"], 1 - fno.prob_above(put["breakevens"][0], F, sigma, T), places=9)
        # Independent cross-check: midpoint integration of the lognormal density over a fine grid.
        calls, puts = r["_books"]
        value = lambda s: -LOT * (max(0.0, s - 25250) - calls[25250]["close"]) - LOT * (max(0.0, 24750 - s) - puts[24750]["close"])
        density = lambda s: math.exp(-((math.log(s / F) + sd * sd / 2) ** 2) / (2 * sd * sd)) / (s * sd * math.sqrt(2 * math.pi))
        low, high, n = F * math.exp(-8 * sd), F * math.exp(8 * sd), 20000
        h = (high - low) / n
        numeric = sum(density(low + (i + 0.5) * h) * h for i in range(n) if value(low + (i + 0.5) * h) > 0)
        self.assertAlmostEqual(strangle["probabilities"]["profit"], numeric, places=3)

    def test_today_curve_is_sum_of_leg_black76_values(self):
        r = report()
        p = fno.payoff(r, [fno.parse_leg(x) for x in ("SELL:CE:25250:1", "BUY:CE:25500:2", "SELL:PE:24750:1", "BUY:FUT::1")])
        calls, puts = r["_books"]
        F, T = r["forward"], r["_years"]
        for row in p["table"]:
            s, expected = row["price"], 0.0
            for leg in p["legs"]:
                if leg["type"] == "FUT":
                    expected += leg["units"] * (r["future"]["price"] * s / SPOT - leg["price"])
                    continue
                book = calls if leg["type"] == "CE" else puts
                iv = fno.implied_vol(leg["type"], book[leg["strike"]]["close"], F, leg["strike"], T, RATE)
                expected += leg["units"] * (fno.black76(leg["type"], F * s / SPOT, leg["strike"], T, iv, RATE) - leg["price"])
            self.assertAlmostEqual(row["today_pnl"], expected, places=6)
        at_spot = next(t for t in p["table"] if "spot" in t["marks"])
        self.assertAlmostEqual(at_spot["today_pnl"], 0.0, delta=LOT * 0.01)  # priced at market IVs: no P&L at today's spot
        self.assertEqual(len(p["today_curve"]), len(p["curve"]))
        svg = fno.svg_chart(r, p)
        xml.dom.minidom.parseString(svg.encode())
        self.assertIn("Today (T+0)", svg)
        self.assertIn("At expiry", svg)
        self.assertIn('stroke-dasharray="7 5"', svg)

    def test_chain_summary_mode(self):
        code, text = run_cli(["chain", "NIFTY", "--summary"], FakeFeeds())
        self.assertEqual(code, 0)
        lines = [l for l in text.strip().splitlines() if l.strip()]
        self.assertTrue(8 <= len(lines) <= 14, len(lines))
        self.assertIn(f"data {TRADE}", lines[0])
        self.assertIn(f"Max pain 25,000 (+250 vs {PREV})", text)
        self.assertRegex(text, r"PCR \(OI\) [0-9.]+ \([+-][0-9.]+ vs 2026-11-13\)")
        self.assertRegex(text, r"ATM IV 15\.0% \(-2\.0 pts vs 2026-11-13\)")
        self.assertIn("India VIX 14.00 (close 2026-11-17)", text)
        self.assertIn("Call OI walls above spot: 25,500", text)
        self.assertIn("put OI walls below spot: 24,500", text)
        self.assertNotIn("## Chain around the money", text)
        code, raw = run_cli(["chain", "NIFTY", "--summary", "--format", "json"], FakeFeeds())
        data = json.loads(raw)
        self.assertEqual((data["schema"], data["max_pain_change"], data["previous_date"]), ("fno_chain_summary_v1", 250, PREV))
        self.assertFalse(data["execution_eligible"])

    def test_payoff_summary_mode(self):
        code, text = run_cli(["payoff", "NIFTY", *SPREAD, "--summary"], FakeFeeds())
        self.assertEqual(code, 0)
        lines = [l for l in text.strip().splitlines() if l.strip()]
        self.assertTrue(8 <= len(lines) <= 12, len(lines))
        for part in ("Net premium received", "Max profit ₹", "max loss ₹", "Breakeven(s):", "P above",
                     "Probability of profit at expiry", "implied by option prices, not a forecast", "68% implied range"):
            self.assertIn(part, text)
        code, raw = run_cli(["payoff", "NIFTY", *SPREAD, "--summary", "--format", "json"], FakeFeeds())
        data = json.loads(raw)
        self.assertTrue(0 < data["probability_of_profit"] < 1)
        self.assertAlmostEqual(data["max_loss"], data["net_premium"] - 250 * LOT, places=6)

    def test_full_payoff_report_has_probabilities_and_today_chart(self):
        with tempfile.TemporaryDirectory() as d:
            chart = Path(d) / "spread.svg"
            code, text = run_cli(["payoff", "NIFTY", *SPREAD, "--chart", str(chart)], FakeFeeds())
            self.assertEqual(code, 0)
            self.assertIn("## Probabilities at expiry (implied by option prices, not a forecast)", text)
            self.assertIn("Today (T+0)", chart.read_text(encoding="utf-8"))
            self.assertEqual(run_cli(["payoff", "NIFTY", *SPREAD, "--chart", str(chart)], FakeFeeds())[0], 2)  # never overwrites


if __name__ == "__main__":
    unittest.main()
