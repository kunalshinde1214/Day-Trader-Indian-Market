"""Offline contracts for the F&O round-2b fixes: indicative SPAN + exposure margin from NSE Clearing's SPAN file format
(scripts/fno_margin.py), the 'if placed now' re-price, grid rows with margin / return on margin / ±SD P&L and --row,
and plain wording. Never contacts the network: SPAN files are built here in the real NSCCL .spn layout."""
from datetime import date, datetime
import io
import json
import math
from pathlib import Path
import sys
import tempfile
import unittest
import zipfile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from core import Block, IST
import fno
import fno_margin
from test_fno import EXPIRY, LOT, RATE, SPOT, TRADE, bhavcopy
from test_fno_round2 import CARD, LIVE_AT, LIVE_LEVEL, LiveFeeds, card_file, run, yahoo_quote

# ---------------- SPAN fixtures in NSE Clearing's .spn layout ----------------

SCENARIOS = [(0, 1), (0, -1), (1 / 3, 1), (1 / 3, -1), (-1 / 3, 1), (-1 / 3, -1), (2 / 3, 1), (2 / 3, -1), (-2 / 3, 1), (-2 / 3, -1),
             (1, 1), (1, -1), (-1, 1), (-1, -1), (2, 0), (-2, 0)]


def ra_xml(values, delta="0.5000"):
    return "<ra><r>1</r>" + "".join(f"<a>{v:.2f}</a>" for v in values) + f"<d>{delta}</d></ra>"


def span_xml(symbol, close, *, futures=(), options=(), scan=None, som=0, business="20260918", extra_before="", created=None):
    """A SPAN file with the same tags, nesting and blank-line layout as nsccl.YYYYMMDD.s.spn.
    futures: [(yyyymmdd, price, ra)]; options: [(yyyymmdd, 'C'|'P', strike, price, ra)]."""
    scan = scan or close * 0.09
    scan_rate = f"<scanRate>\n<r>1</r>\n<priceScan>{scan:.2f}</priceScan>\n<volScan>0.04</volScan>\n</scanRate>\n"
    phy = (f"<phyPf>\n\n<pfId>490</pfId>\n<pfCode>{symbol}</pfCode>\n<name>{symbol}</name>\n<currency>INR</currency>\n<cvf>1.00</cvf>\n\n"
           f"<phy>\n<cId>1</cId>\n<pe>00000000</pe>\n<p>{close:.6f}</p>\n<d>0.000000</d>\n<v>0.000000</v>\n<cvf>1.00</cvf>\n<sc>1.00000</sc>\n\n"
           f"{scan_rate}\n{ra_xml([0.0] * 16, '1.00000')}\n</phy>\n</phyPf>")
    futs = "".join(f"\n<fut>\n\n<cId>{i + 1}</cId>\n\n<pe>{pe}</pe>\n\n<p>{p:.2f}</p>\n\n<d>1.00000000</d>\n\n<v>0.16</v>\n\n<cvf>1.00</cvf>\n\n"
                   f"{scan_rate}\n\n{ra_xml(ra, '1.00000')}\n\n</fut>\n" for i, (pe, p, ra) in enumerate(futures))
    fut = (f"<futPf>\n\n<pfId>491</pfId>\n\n<pfCode>{symbol}</pfCode>\n\n<name>{symbol}</name>\n\n<currency>INR</currency>\n\n"
           f"<undPf>\n\n<exch>NSE</exch>\n\n<pfId>490</pfId>\n\n<pfCode>{symbol}</pfCode>\n\n<pfType>PHY</pfType>\n\n</undPf>\n{futs}</futPf>")
    by_series = {}
    for pe, o, k, p, ra in options:
        by_series.setdefault(pe, []).append((o, k, p, ra))
    series = ""
    for pe, opts in by_series.items():
        series += (f"\n<series>\n\n<pe>{pe}</pe>\n\n<v>0.15851</v>\n\n<volSrc> </volSrc>\n\n<setlDate>{pe}</setlDate>\n\n<t>0.03</t>\n\n"
                   f"<cvf>1.00</cvf>\n\n<sc>1.00000</sc>\n\n<undC>\n\n<exch>NSE</exch>\n\n<pfId>490</pfId>\n\n<cId>1</cId>\n\n</undC>\n\n"
                   f"{scan_rate}\n")
        for i, (o, k, p, ra) in enumerate(opts):
            series += f"\n<opt>\n\n<cId>{i + 1}</cId>\n\n<o>{o}</o>\n\n<k>{k:.2f}</k>\n\n<p>{p:.2f}</p>\n\n<d>0.5</d>\n\n<v>0.1585</v>\n\n{ra_xml(ra)}\n\n</opt>\n"
        series += "\n</series>\n"
    oop = (f"<oopPf>\n\n<pfId>492</pfId>\n\n<pfCode>{symbol}</pfCode>\n\n<name>{symbol}</name>\n\n<exercise>EURO</exercise>\n\n"
           f"<currency>INR</currency>\n\n<cvf>1.00</cvf>\n\n<priceModel>BS</priceModel>\n\n<undPf>\n\n<exch>NSE</exch>\n\n<pfId>490</pfId>\n\n"
           f"<pfCode>{symbol}</pfCode>\n\n<pfType>PHY</pfType>\n\n</undPf>\n{series}</oopPf>")
    cc = (f"<ccDef>\n<cc>{symbol}</cc>\n<name>{symbol}</name>\n<currency>INR</currency>\n<somMeth>GROSS</somMeth>\n"
          f"<pfLink>\n<exch>NSE</exch>\n<pfId>492</pfId>\n<pfCode>{symbol}</pfCode>\n<pfType>OOP</pfType>\n</pfLink>\n"
          f"<somTiers>\n<tier>\n<tn>0</tn>\n<rate>\n<r>1</r>\n<val>{som}</val>\n</rate>\n</tier>\n</somTiers>\n"
          f"<dSpread>\n<spread>1</spread>\n<chargeMeth>F</chargeMeth>\n<rate>\n<r>1</r>\n<val>451.000000</val>\n</rate>\n</dSpread>\n</ccDef>")
    return (f"<?xml version=\"1.0\"?>\n \n<spanFile>\n<fileFormat>4.00</fileFormat>\n<created>{created or business + '1651'}</created>\n \n\n<definitions>\n"
            f"<currencyDef>\n<currency>INR</currency>\n<symbol>I</symbol>\n<name>Indian Rupee</name>\n<decimalPos>2</decimalPos>\n</currencyDef>\n"
            f"</definitions>\n\n\n<pointInTime>\n<date>{business}</date>\n<isSetl>1</isSetl>\n<setlQualifier>final</setlQualifier>\n\n\n"
            f"<clearingOrg>\n<ec>NSCCL</ec>\n<isContractScale>1</isContractScale>\n\n{extra_before}{phy}{fut}{oop}\n{cc}\n"
            f"<interSpreads>\n<dSpread>\n<spread></spread>\n</dSpread>\n</interSpreads>\n</clearingOrg>\n</pointInTime>\n</spanFile>\n")


def span_zip(xml, name="nsccl.20260918.s.spn"):
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr(name, xml)
    return out.getvalue()


# Real NSE Clearing values, nsccl.20260918.s.spn (18 Sep 2026): NIFTY close 23,346.40, scan range 2,171.22, the 29 Sep 2026
# monthly future and six options. Published worked example (marketcalls.in "Python tutorial on SPAN margin calculator using
# marginism", re-run against a live broker SPAN calculator: SPAN equal to within ₹0.50) for one 65-unit lot:
REAL_OPTIONS = [
    ("20260929", "C", 23400, 146.90, [-30.10, 86.88, -521.70, -464.99, 146.90, 146.90, -1186.66, -1178.58, 146.90, 146.90, -1902.76, -1902.24, 146.90, 146.90, -1425.71, 88.14]),
    ("20260929", "P", 23300, 129.80, [-37.36, 78.17, 129.80, 129.80, -500.37, -440.55, 129.80, 129.80, -1158.84, -1151.89, 129.80, 129.80, -1875.81, -1875.57, 74.54, -1416.37]),
    ("20260929", "C", 23350, 173.90, [-29.79, 86.92, -538.21, -487.26, 173.90, 173.90, -1210.05, -1203.30, 173.90, 173.90, -1927.40, -1926.99, 173.90, 173.90, -1434.37, 96.95]),
    ("20260929", "P", 23350, 149.65, [-37.86, 78.85, 149.65, 149.65, -518.52, -464.37, 149.65, 149.65, -1184.41, -1178.60, 149.65, 149.65, -1902.49, -1902.30, 82.65, -1425.73]),
    ("20260929", "C", 23700, 40.45, [-28.29, 40.45, -411.11, -317.78, 40.45, 40.45, -1020.50, -999.13, 40.45, 40.45, -1723.95, -1721.99, 40.45, 40.45, -1362.62, 40.45]),
    ("20260929", "P", 23000, 50.70, [-31.00, 50.70, 50.70, 50.70, -381.04, -286.76, 50.70, 50.70, -979.38, -960.69, 50.70, 50.70, -1684.72, -1683.72, 36.85, -1349.23]),
]
REAL_FUTURE = ("20260929", 23378.50, [8.10, 8.10, -716.77, -716.77, 732.97, 732.97, -1441.64, -1441.64, 1457.83, 1457.83, -2166.50, -2166.50, 2182.70, 2182.70, -1519.39, 1525.06])
PUBLISHED = {  # name: (legs, SPAN, exposure)
    "short 23400 CE": ([("CE", 23400, -65)], 133227.90, 30350.32),
    "short 23300 PE": ([("PE", 23300, -65)], 130364.65, 30350.32),
    "long 23400 CE": ([("CE", 23400, 65)], 0.00, 0.00),
    "short straddle 23350": ([("CE", 23350, -65), ("PE", 23350, -65)], 136584.50, 60700.64),
    "bull call 23400/23700": ([("CE", 23400, 65), ("CE", 23700, -65)], 0.00, 30350.32),
    "bull put 23300/23000": ([("PE", 23300, -65), ("PE", 23000, 65)], 17611.75, 30350.32),
    "long future + 23000 PE": ([("FUT", None, 65), ("PE", 23000, 65)], 29138.20, 30392.05),
}


def real_span():
    decoy = span_xml("NIFTYFPI", 1606.35).split("<clearingOrg>")[1].split("<interSpreads>")[0]  # a similar code must not match
    decoy = decoy.replace("<ec>NSCCL</ec>\n<isContractScale>1</isContractScale>\n\n", "")
    xml = span_xml("NIFTY", 23346.40, futures=[REAL_FUTURE], options=REAL_OPTIONS, scan=2171.22, extra_before=decoy)
    span = fno_margin.parse_span(span_zip(xml), "NIFTY")
    span.update(status="ok", url="test", file_label="2026-09-18 end-of-day file")
    return span


def legs_of(spec):
    return [{"type": t, "strike": k, "units": u} for t, k, u in spec]


# ---------------- synthetic SPAN for the test bhavcopy (Black-76 scenario re-pricing) ----------------

def scenario_ra(kind, forward, strike, years, vol, price, scan):
    out = []
    for move, dv in SCENARIOS:
        f = forward + move * scan
        if kind == "FUT":
            value = f
        else:
            value = fno.black76(kind, f, strike, years, vol * (1 + 0.04 * dv / 0.15) if dv else vol, RATE)
        loss = price - value
        out.append(round(loss * (0.35 if abs(move) == 2 else 1.0), 2))  # the file carries two decimals
    return out


def bhav_span(symbol="NIFTY", som=0, file_close=SPOT):
    """A SPAN file for every contract in the test bhavcopy, priced at its closes."""
    contracts = fno.parse_bhavcopy(bhavcopy(), "NIFTY")
    years = fno.years_to_expiry(EXPIRY, datetime(2026, 11, 17, 15, 30, tzinfo=IST))
    forward = SPOT * math.exp(RATE * years)
    scan = SPOT * 0.09
    pe = EXPIRY.replace("-", "")
    options = [(pe, c["kind"][0], c["strike"], c["close"], scenario_ra(c["kind"], forward, c["strike"], years, 0.15, c["close"], scan))
               for c in contracts if c["kind"] in ("CE", "PE") and c["expiry"] == EXPIRY]
    fut = next(c for c in contracts if c["kind"] == "FUT")
    futures = [(pe, fut["close"], scenario_ra("FUT", fut["close"], None, years, 0.15, fut["close"], scan))]
    return span_zip(span_xml(symbol, file_close, futures=futures, options=options, scan=scan, som=som, business="20261117"),
                    "nsccl.20261117.s.spn"), options, futures


def independent_margin(spec, options, futures, rate=0.02):
    """SPAN + exposure for [(type, strike, units)] straight from the fixture arrays (no fno_margin code)."""
    losses, nov, exposure = [0.0] * 16, 0.0, 0.0
    for kind, strike, units in spec:
        if kind == "FUT":
            _, price, ra = futures[0]
        else:
            _, _, _, price, ra = next(o for o in options if o[1] == kind[0] and o[2] == strike)
            nov += units * price
        losses = [x + units * r for x, r in zip(losses, ra)]
        if kind == "FUT":
            exposure += abs(units) * price * rate
        elif units < 0:
            exposure += abs(units) * SPOT * rate
    span = max(0.0, max(0.0, max(losses)) - nov)
    return span, exposure


VIX_PREV, VIX_NOW = 14.0, 15.4


SNAP_LEVEL, SNAP_VOL = 24950.0, 0.18


def snapshot_span(created="202611180945"):
    """Today's intraday SPAN file (i2) as NSE publishes it: market prices at its creation time, NIFTY at SNAP_LEVEL."""
    at = datetime.strptime(created, "%Y%m%d%H%M").replace(tzinfo=IST)
    years = fno.years_to_expiry(EXPIRY, at)
    forward = SNAP_LEVEL * math.exp(RATE * years)
    pe = EXPIRY.replace("-", "")
    options = [(pe, kind[0], k, round(round(fno.black76(kind, forward, k, years, SNAP_VOL, RATE) / 0.05) * 0.05, 2), [0.0] * 16)
               for kind in ("CE", "PE") for k in (24500.0, 24750.0, 25000.0, 25250.0, 25500.0)]
    return span_zip(span_xml("NIFTY", SNAP_LEVEL, futures=[(pe, forward, [0.0] * 16)], options=options, business=created[:8],
                             created=created), f"nsccl.{created[:8]}.i02.spn")


class SpanFeeds(LiveFeeds):
    """LiveFeeds plus NSE Clearing SPAN files: today's intraday files are missing, the data date's end-of-day file is served;
    optionally a live India VIX quote."""

    def __init__(self, *, span=True, vix=False, file_close=SPOT, snapshot=None, **kw):
        super().__init__(**kw)
        self.span_raw = bhav_span(file_close=file_close)[0] if span else None
        self.vix, self.snapshot = vix, snapshot

    def __call__(self, url):
        if "nsccl/span" in url:
            self.urls.append(url)
            if self.span_raw and url.endswith(f"nsccl.{TRADE.replace('-', '')}.s.zip"):
                return self.span_raw
            if self.snapshot and url.endswith(f"nsccl.{self.snapshot[:8]}.i2.zip"):
                return snapshot_span(self.snapshot)
            raise Block("PUBLIC: source unavailable")
        if self.vix and "%5EINDIAVIX" in url and "range=1d" in url:
            self.urls.append(url)
            return json.dumps({"chart": {"result": [{"meta": {"symbol": "^INDIAVIX", "currency": "INR", "regularMarketPrice": VIX_NOW,
                                                              "chartPreviousClose": VIX_PREV, "regularMarketTime": int(LIVE_AT.timestamp())}}]}}).encode()
        return super().__call__(url)


# ---------------- 1. SPAN parsing and the margin arithmetic ----------------

class SpanMarginTests(unittest.TestCase):
    def test_reproduces_published_broker_checked_figures_on_the_real_file_values(self):
        span = real_span()
        self.assertEqual((span["business_date"], span["underlying_close"], span["price_scan"]), ("2026-09-18", 23346.40, 2171.22))
        self.assertEqual(len(span["options"]), 6)
        for name, (spec, span_figure, exposure) in PUBLISHED.items():
            m = fno_margin.estimate(span, legs_of(spec), expiry="2026-09-29", trade_day="2026-09-18", index=True)
            self.assertTrue(m["available"], name)
            self.assertAlmostEqual(m["span"], span_figure, places=2, msg=name)
            self.assertAlmostEqual(m["exposure"], exposure, places=2, msg=name)
            self.assertAlmostEqual(m["total"], span_figure + exposure, places=2, msg=name)

    def test_scan_risk_net_option_value_and_short_option_minimum(self):
        span = real_span()
        m = fno_margin.estimate(span, legs_of([("CE", 23400, -65)]), expiry="2026-09-29", trade_day="2026-09-18", index=True)
        self.assertAlmostEqual(m["scan_risk"], 1902.76 * 65, places=2)        # worst scenario: up 3/3 with vol up (scenario 11)
        self.assertEqual(m["worst_scenario"], 11)
        self.assertAlmostEqual(m["net_option_value"], -146.90 * 65, places=2)  # short premium adds to SPAN
        # A non-zero short-option minimum (NSCCL files carry 0) binds when it exceeds the scan risk.
        span["som_rate"] = 3000.0
        m = fno_margin.estimate(span, legs_of([("CE", 23700, -65), ("CE", 23400, 65)]), expiry="2026-09-29", trade_day="2026-09-18", index=True)
        self.assertAlmostEqual(m["short_option_minimum"], 3000.0 * 65)
        self.assertAlmostEqual(m["span"], 3000.0 * 65 - (146.90 - 40.45) * 65, places=2)

    def test_exposure_rules(self):
        span = real_span()
        rate = lambda leg, **kw: fno_margin.exposure_rate(leg, close=23346.40, **{"index": True, "expiry": "2026-09-29",
                                                                                   "trade_day": "2026-09-18", **kw})[0]
        short = lambda kind, k: {"type": kind, "strike": k, "units": -65}
        self.assertEqual(rate(short("CE", 23400)), 0.02)
        self.assertEqual(rate(short("CE", 23346.40 * 1.11)), 0.03)                      # >10% OTM call
        self.assertEqual(rate(short("PE", 23346.40 * 0.89)), 0.03)                      # >10% OTM put
        self.assertEqual(rate(short("PE", 23346.40 * 1.2)), 0.02)                       # deep in the money: base rate
        self.assertEqual(rate(short("CE", 23400), expiry="2027-07-29"), 0.05)           # more than 9 months
        self.assertAlmostEqual(rate(short("CE", 23400), trade_day="2026-09-29"), 0.04)  # expiry day +2%
        self.assertIsNone(rate({"type": "CE", "strike": 23400, "units": 65}))           # long option: none
        self.assertEqual(rate({"type": "FUT", "strike": None, "units": -65}), 0.02)
        self.assertEqual(rate(short("CE", 23400), index=False), 0.035)
        self.assertEqual(rate(short("CE", 23346.40 * 1.31), index=False), 0.0525)
        m = fno_margin.estimate(span, legs_of([("CE", 23400, -65)]), expiry="2026-09-29", trade_day="2026-09-29", index=True)
        self.assertAlmostEqual(m["exposure"], 0.04 * 23346.40 * 65, places=2)

    def test_never_guesses(self):
        span = real_span()
        missing = fno_margin.estimate(span, legs_of([("CE", 99999, -65)]), expiry="2026-09-29", trade_day="2026-09-18", index=True)
        self.assertFalse(missing["available"])
        self.assertTrue(missing["note"].startswith("margin unavailable — check your broker's basket margin"))
        calendar = fno_margin.estimate(span, [{"type": "CE", "strike": 23400, "units": -65},
                                              {"type": "CE", "strike": 23400, "units": 65, "expiry": "2026-10-27"}],
                                       expiry="2026-09-29", trade_day="2026-09-18", index=True)
        self.assertIn("more than one expiry", calendar["note"])
        down = fno_margin.fetch_span("NIFTY", "2026-11-17", date(2026, 11, 18), fetcher=lambda u: (_ for _ in ()).throw(Block("x")))
        self.assertEqual(down["status"], "unavailable")
        self.assertIn("margin unavailable — check your broker's basket margin", down["note"])
        self.assertFalse(fno_margin.estimate(down, legs_of([("CE", 23400, -65)]), expiry="2026-09-29", trade_day="2026-09-18", index=True)["available"])
        garbled = fno_margin.fetch_span("NIFTY", "2026-11-17", date(2026, 11, 17), fetcher=lambda u: b"PK not a zip")
        self.assertEqual(garbled["status"], "unavailable")
        self.assertIn("could not be read", garbled["note"])
        with self.assertRaisesRegex(Block, "not in the file"):
            fno_margin.parse_span(span_zip(span_xml("BANKNIFTY", 50000.0)), "NIFTY")
        with self.assertRaisesRegex(Block, "16 scenarios"):
            fno_margin.parse_span(span_zip(span_xml("NIFTY", 23346.4, options=[("20260929", "C", 23400, 1.0, [1.0] * 15)])), "NIFTY")
        with self.assertRaisesRegex(Block, "not a SPAN"):
            fno_margin.parse_span(b"<html></html>", "NIFTY")

    def test_file_order_and_request_allow_list(self):
        during = [u for u, _ in fno_margin.candidates("2026-11-17", date(2026, 11, 18))]
        self.assertEqual(during[0], "https://nsearchives.nseindia.com/archives/nsccl/span/nsccl.20261118.i6.zip")
        self.assertEqual(during[-2:], ["https://nsearchives.nseindia.com/archives/nsccl/span/nsccl.20261118.i1.zip",
                                       "https://nsearchives.nseindia.com/archives/nsccl/span/nsccl.20261117.s.zip"])
        evening = [u for u, _ in fno_margin.candidates("2026-11-18", date(2026, 11, 18))]
        self.assertTrue(evening[0].endswith("nsccl.20261118.s.zip"))
        weekend = [u for u, _ in fno_margin.candidates("2026-11-20", date(2026, 11, 21), market_day=False)]
        self.assertEqual(weekend, ["https://nsearchives.nseindia.com/archives/nsccl/span/nsccl.20261120.s.zip"])
        for bad in ("https://nsearchives.nseindia.com/content/fo/x.zip", "http://nsearchives.nseindia.com/archives/nsccl/span/nsccl.20261117.s.zip",
                    "https://evil.example/archives/nsccl/span/nsccl.20261117.s.zip"):
            with self.assertRaisesRegex(Block, "unsupported source"):
                fno_margin.span_request(bad)
        self.assertEqual(fno_margin.inr(156003.4), "1,56,003")
        self.assertEqual(fno_margin.inr(12345678), "1,23,45,678")
        self.assertEqual(fno_margin.inr(999), "999")


# ---------------- 2. margin in payoff and grid output ----------------

class MarginOutputTests(unittest.TestCase):
    def test_payoff_reports_indicative_margin_and_return_on_margin(self):
        _, options, futures = bhav_span()
        with tempfile.TemporaryDirectory() as d:
            # Evening (market closed): no 'now' block, margin from the data date's end-of-day file.
            evening = datetime(2026, 11, 17, 20, 0, tzinfo=IST)
            code, raw, err = run(["payoff", "NIFTY", "--leg", "SELL:CE:25250:1", "--leg", "BUY:CE:25500:1", "--format", "json",
                                  "--charges", card_file(d)], SpanFeeds(), clock=evening)
            self.assertEqual(code, 0, err)
            p = json.loads(raw)["payoff"]
            span, exposure = independent_margin([("CE", 25250, -LOT), ("CE", 25500, LOT)], options, futures)
            self.assertTrue(p["margin"]["available"])
            self.assertAlmostEqual(p["margin"]["span"], span, places=2)
            self.assertAlmostEqual(p["margin"]["exposure"], exposure, places=2)
            self.assertAlmostEqual(p["return_on_margin"], p["after_costs"]["max_profit"] / (span + exposure), places=9)
            self.assertNotIn("placed_now", p)
            # An intraday SPAN file carries an intraday underlying price; exposure stays on the exchange close (25,000).
            code, raw, _ = run(["payoff", "NIFTY", "--leg", "SELL:CE:25250:1", "--format", "json", "--charges", card_file(d)],
                               SpanFeeds(file_close=24000.0), clock=evening)
            self.assertAlmostEqual(json.loads(raw)["payoff"]["margin"]["exposure"], 0.02 * SPOT * LOT, places=6)
            code, text, _ = run(["payoff", "NIFTY", "--leg", "SELL:CE:25250:1", "--leg", "BUY:CE:25500:1", "--summary",
                                 "--charges", card_file(d)], SpanFeeds(), clock=evening)
            # Round 8: a hedged index spread carries the observed broker range, not the ±10% band.
            self.assertIn(f"- Margin indicative ₹{fno_margin.inr(span + exposure)} (SPAN ₹{fno_margin.inr(span)} + exposure ₹{fno_margin.inr(exposure)}; "
                          "brokers have charged hedged positions less, see the range; your broker's basket margin is final) — NSE Clearing's start-of-day "
                          "margin parameters from the Tue 17 Nov close, the ones broker calculators matched", text)
            self.assertNotIn(".spn", text)   # round 5: file names stay in the full report
            self.assertIn("Return on margin if held to expiry", text)
            # SPAN file unreachable: say so, never a number.
            code, text, _ = run(["payoff", "NIFTY", "--leg", "SELL:CE:25250:1", "--summary", "--charges", card_file(d)],
                                SpanFeeds(span=False), clock=evening)
            self.assertIn("- Margin unavailable — check your broker's basket margin", text)
            self.assertNotIn("Margin ≈", text)
            feeds = SpanFeeds()
            run(["payoff", "NIFTY", "--leg", "SELL:CE:25250:1", "--summary", "--no-margin", "--charges", card_file(d)], feeds, clock=evening)
            self.assertFalse(any("nsccl" in u for u in feeds.urls))

    def test_grid_rows_carry_margin_rom_and_sd_pnl_and_row_prints_the_payoff(self):
        _, options, futures = bhav_span()
        evening = datetime(2026, 11, 17, 20, 0, tzinfo=IST)
        with tempfile.TemporaryDirectory() as d:
            card = card_file(d)
            argv = ["grid", "NIFTY", "--structure", "spread", "--width", "250", "--deltas", "0.30", "--charges", card]
            code, raw, err = run(argv + ["--format", "json"], SpanFeeds(), clock=evening)
            self.assertEqual(code, 0, err)
            rows = json.loads(raw)["grid"]["rows"]
            self.assertEqual([x["side"] for x in rows], ["call", "put"])
            for x in rows:
                spec = [("CE" if ":CE:" in l else "PE", float(l.split(":")[2]), (-1 if l.startswith("SELL") else 1) * LOT) for l in x["legs"]]
                span, exposure = independent_margin(spec, options, futures)
                self.assertAlmostEqual(x["margin"]["total"], span + exposure, places=2)
                self.assertAlmostEqual(x["return_on_margin"], x["max_profit_after_costs"] / (span + exposure), places=9)
                p = fno.payoff(fno.analyse_chain(fno.parse_bhavcopy(bhavcopy(), "NIFTY"), "NIFTY", rate=RATE),
                               [fno.parse_leg(l) for l in x["legs"]], card=CARD)
                for s in p["scenarios"]["expiry"]:
                    self.assertAlmostEqual(x["sd_pnl"][str(s["sd"])][1], s["pnl_after_costs"], places=6)
            code, text, _ = run(argv, SpanFeeds(), clock=evening)
            self.assertIn("| # | Target Δ |", text)
            self.assertIn("| Margin ₹ (indicative) | Return on margin | P&L at expiry at −2 / −1 / +1 / +2 SD ₹ |", text)
            self.assertIn("| 1 | 0.30 |", text)
            self.assertIn("| 2 | 0.30 |", text)
            self.assertIn(fno_margin.inr(rows[0]["margin"]["total"]), text)
            for x in rows:  # SD cells in −2, −1, +1, +2 order
                cell = " / ".join(fno.signed(x["sd_pnl"][k][1], 0) for k in ("-2", "-1", "1", "2"))
                self.assertIn(f"| {cell} |", text)
            self.assertIn("add `--row N --chart FILE.svg`", text)
            # --row prints that row's full payoff, and writes the chart.
            chart = Path(d) / "row2.svg"
            code, text, err = run(argv + ["--row", "2", "--chart", str(chart)], SpanFeeds(), clock=evening)
            self.assertEqual(code, 0, err)
            self.assertTrue(text.startswith(f"Grid row 2 (credit spread, target delta 0.30): --leg {rows[1]['legs'][0]}"))
            self.assertIn("# NIFTY strategy payoff — expiry 2026-11-23", text)
            self.assertIn("## P&L by underlying price", text)
            self.assertIn("<svg", chart.read_text(encoding="utf-8"))
            code, text, _ = run(argv + ["--row", "2", "--summary"], SpanFeeds(), clock=evening)
            self.assertIn("Margin indicative ₹", text)
            self.assertEqual(run(argv + ["--row", "3"], SpanFeeds(), clock=evening)[0], 2)
            self.assertEqual(run(argv + ["--chart", str(Path(d) / "x.svg")], SpanFeeds(), clock=evening)[0], 2)


# ---------------- 3. if placed now ----------------

class PlacedNowTests(unittest.TestCase):
    def eod(self):
        return fno.analyse_chain(fno.parse_bhavcopy(bhavcopy(), "NIFTY"), "NIFTY", rate=RATE)

    def test_reprice_uses_live_level_time_left_and_close_iv(self):
        r = self.eod()
        live = {"level": LIVE_LEVEL, "at": LIVE_AT.isoformat()}
        now = fno.reprice_report(r, live)
        t1 = fno.years_to_expiry(EXPIRY, LIVE_AT)
        f1 = LIVE_LEVEL * (r["forward"] / SPOT) ** (t1 / r["_years"])
        iv = next(x for x in r["_rows"] if x["strike"] == 25250)["ce"]["iv"]
        expected = round(round(fno.black76("CE", f1, 25250, t1, iv, RATE) / 0.05) * 0.05, 2)
        self.assertAlmostEqual(now["_books"][0][25250]["close"], expected, places=6)
        self.assertLess(expected, r["_books"][0][25250]["close"])     # spot fell 100 points: the call is cheaper now
        self.assertEqual(now["spot"], LIVE_LEVEL)
        self.assertAlmostEqual(now["_years"], t1, places=12)
        self.assertAlmostEqual(now["forward"], f1, delta=1.0)
        self.assertAlmostEqual(now["atm_iv"], r["atm_iv"], delta=0.003)
        scaled = fno.reprice_report(r, live, vix={"ratio": 1.1, "level": 15.4, "previous_close": 14.0})
        self.assertAlmostEqual(scaled["atm_iv"], r["atm_iv"] * 1.1, delta=0.003)
        self.assertIn("scaled by India VIX's move today, 14.00 → 15.40, +10.0%", scaled["_placed_now"]["basis"])
        self.assertIsNone(fno.reprice_report(r, {"level": LIVE_LEVEL, "at": "2026-11-23T15:30:00+05:30"}))  # no time left

    def test_payoff_leads_with_placed_now_and_keeps_the_close_labelled(self):
        with tempfile.TemporaryDirectory() as d:
            card = card_file(d)
            legs = ["--leg", "SELL:CE:25250:1"]
            code, raw, err = run(["payoff", "NIFTY", *legs, "--format", "json", "--charges", card], SpanFeeds(vix=True))
            self.assertEqual(code, 0, err)
            data = json.loads(raw)
            pn, p = data["payoff"]["placed_now"], data["payoff"]
            self.assertTrue(pn["available"])
            self.assertAlmostEqual(pn["iv_scale"], VIX_NOW / VIX_PREV)
            leg = pn["legs"][0]
            self.assertEqual(leg["close"], p["legs"][0]["price"])
            self.assertAlmostEqual(leg["iv_now"], leg["iv_close"] * VIX_NOW / VIX_PREV, delta=0.002)
            entry = fno.trade_charges(CARD, "CE", "SELL", leg["now"], LOT)["total"]
            self.assertAlmostEqual(pn["outcome"]["net_premium"], leg["now"] * LOT - entry, places=6)
            self.assertNotAlmostEqual(pn["outcome"]["net_premium"], p["after_costs"]["net_premium"], places=0)
            self.assertAlmostEqual(pn["return_on_margin"], pn["outcome"]["max_profit"] / p["margin"]["total"], places=9)
            code, text, _ = run(["payoff", "NIFTY", *legs, "--summary", "--charges", card], SpanFeeds(vix=True))
            lines = text.splitlines()
            head = next(i for i, l in enumerate(lines) if l.startswith("**If placed now"))
            self.assertLess(head, 4)
            self.assertIn("| For the lots shown, after costs | If placed now (estimate) | At the Tue 17 Nov close |", text)
            self.assertIn(f"| SELL 1 lot CE 25250 premium | ≈ {fno.fmt(leg['now'])}", text)
            self.assertIn("not live option quotes", text)
            code, text, _ = run(["payoff", "NIFTY", *legs, "--charges", card], SpanFeeds(vix=True))
            self.assertLess(text.index("## If placed now"), text.index("## At the Tue 17 Nov close (end-of-day data, for reference)"))
            self.assertLess(text.index("## At the Tue 17 Nov close"), text.index("## Probabilities at expiry"))
            # No live quote, or market closed: no 'now' block.
            code, text, _ = run(["payoff", "NIFTY", *legs, "--summary", "--charges", card], SpanFeeds(quote=False))
            self.assertNotIn("If placed now", text)

    def test_nse_intraday_snapshot_prices_lead_when_fresh(self):
        with tempfile.TemporaryDirectory() as d:
            argv = ["payoff", "NIFTY", "--leg", "SELL:CE:25250:1", "--format", "json", "--charges", card_file(d)]
            code, raw, err = run(argv, SpanFeeds(snapshot="202611180945"))
            self.assertEqual(code, 0, err)
            pn = json.loads(raw)["payoff"]["placed_now"]
            self.assertEqual(pn["source"], "nse_intraday_snapshot")
            self.assertEqual(pn["snapshot"]["level"], SNAP_LEVEL)
            leg = pn["legs"][0]
            self.assertAlmostEqual(leg["iv_now"], SNAP_VOL, delta=0.003)                  # the snapshot's IV, not the close's 15%
            t1 = fno.years_to_expiry(EXPIRY, LIVE_AT)                                      # moved to the live level at 09:55
            snap_at = datetime(2026, 11, 18, 9, 45, tzinfo=IST)
            f_snap = SNAP_LEVEL * math.exp(RATE * fno.years_to_expiry(EXPIRY, snap_at))
            f1 = LIVE_LEVEL * (f_snap / SNAP_LEVEL) ** (t1 / fno.years_to_expiry(EXPIRY, snap_at))
            self.assertAlmostEqual(leg["now"], fno.black76("CE", f1, 25250, t1, SNAP_VOL, RATE), delta=0.3)
            self.assertIn("option prices from NSE Clearing's 09:45 IST intraday snapshot", pn["basis"])
            code, text, _ = run(argv[:4] + ["--summary", "--charges", card_file(d)], SpanFeeds(snapshot="202611180945"))
            self.assertIn("from NSE's 09:45 IST intraday option prices, not live quotes", text)
            # The begin-of-day file (created the evening before) or a stale snapshot falls back to the model from the close.
            # Today's i1 is created the evening before; a file from an earlier day (even one stamped in session hours) is not today's prices.
            for created in ("202611172053", "202611171500"):
                feeds = SpanFeeds()
                fetch = lambda url, c=created, f=feeds: snapshot_span(c) if url.endswith("nsccl.20261118.i1.zip") else f(url)
                code, raw, _ = run(argv, fetch)
                self.assertEqual(json.loads(raw)["payoff"]["placed_now"]["source"], "model_from_close", created)
            code, raw, _ = run(argv, SpanFeeds(snapshot="202611180945"), clock=datetime(2026, 11, 18, 13, 30, tzinfo=IST))  # 3h45m old
            self.assertEqual(json.loads(raw)["payoff"]["placed_now"]["source"], "model_from_close")

    def test_grid_now_rows_and_eod_reference(self):
        with tempfile.TemporaryDirectory() as d:
            argv = ["grid", "NIFTY", "--structure", "spread", "--width", "250", "--deltas", "0.30", "--charges", card_file(d)]
            code, raw, err = run(argv + ["--format", "json"], SpanFeeds())
            self.assertEqual(code, 0, err)
            data = json.loads(raw)
            self.assertEqual(data["placed_now"]["level"], LIVE_LEVEL)
            self.assertEqual(len(data["grid"]["rows"]), 2)
            for x in data["grid"]["rows"]:
                self.assertIn("eod", x)
                eod = fno.payoff(fno.analyse_chain(fno.parse_bhavcopy(bhavcopy(), "NIFTY"), "NIFTY", rate=RATE),
                                 [fno.parse_leg(l) for l in x["legs"]], card=CARD)
                self.assertAlmostEqual(x["eod"]["credit_after_costs"], eod["after_costs"]["net_premium"], places=6)
                self.assertNotAlmostEqual(x["credit_after_costs"], eod["after_costs"]["net_premium"], places=0)
                now = fno.reprice_report(fno.analyse_chain(fno.parse_bhavcopy(bhavcopy(), "NIFTY"), "NIFTY", rate=RATE),
                                         {"level": LIVE_LEVEL, "at": LIVE_AT.isoformat()})
                fresh = fno.payoff(now, [fno.parse_leg(l) for l in x["legs"]], card=CARD)
                self.assertAlmostEqual(x["credit_after_costs"], fresh["after_costs"]["net_premium"], places=6)
                self.assertEqual(x["sd_pnl"]["1"][1], fno.outcome(fresh)["sd_pnl"][1][1])
            # Strikes come from the close, so --row N names the same legs whatever the live level does between runs.
            moved = SpanFeeds()
            moved_quote = lambda url, base=moved: (yahoo_quote(price=24700.0) if "%5ENSEI" in url else base(url))
            code, raw2, _ = run(argv + ["--format", "json"], moved_quote)
            self.assertEqual([x["legs"] for x in json.loads(raw2)["grid"]["rows"]], [x["legs"] for x in data["grid"]["rows"]])
            code, text, _ = run(argv, SpanFeeds())
            self.assertLess(text.index("## If placed now — model estimate at live NIFTY 24,900.00"),
                            text.index("## Same strikes at the Tue 17 Nov close (end-of-day data, for reference)"))


# ---------------- 4. plain wording ----------------

class PlainWordingTests(unittest.TestCase):
    def test_net_text(self):
        self.assertEqual(fno.net_text(-116830, 69819, " calls"), "net short 116,830 calls (down 69,819 on the day)")
        self.assertEqual(fno.net_text(369613, 66399, " puts"), "net long 369,613 puts (up 66,399 on the day)")
        self.assertEqual(fno.net_text(-1000, -3000), "net short 1,000 (was net long 2,000 the day before)")
        self.assertEqual(fno.net_text(500, None), "net long 500")
        self.assertEqual(fno.net_text(500, 0), "net long 500 (unchanged on the day)")

    def test_session_and_forward_wording(self):
        m = {"sessions": 0.79 + 1, "rest_of_today": 0.79, "status": "tomorrow", "session": "2026-11-19"}
        self.assertEqual(fno.sessions_text(m), "covers the rest of today, about 4h 56m of trading, plus tomorrow")
        self.assertEqual(fno.sessions_text({**m, "sessions": 0.79 + 2, "status": "next session", "session": "2026-11-20"}),
                         "covers the rest of today, about 4h 56m of trading, plus 2 more sessions to Fri 20 Nov")
        self.assertEqual(fno.sessions_text({"sessions": 2, "rest_of_today": 0.0, "status": "tomorrow", "session": "2026-11-19"}),
                         "covers 2 trading sessions")
        code, text, _ = run(["chain", "NIFTY", "--summary", "--no-vix", "--no-participants"], LiveFeeds())
        self.assertNotIn("futures-implied level at expiry", text)   # round 5: in the full report only
        self.assertIn("Futures-implied level at expiry", run(["chain", "NIFTY", "--no-vix", "--no-participants"], LiveFeeds())[1])
        self.assertNotIn("· forward", text)
        self.assertNotIn("rest of today 0.", text)


if __name__ == "__main__":
    unittest.main()
