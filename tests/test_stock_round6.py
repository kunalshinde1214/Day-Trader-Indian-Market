"""Offline contracts for the round-6 stock fixes: one-off gains booked in other income (Reliance's June 2025 stake sale),
one-offs inside annual EPS growth (labour-code charges), pre-exceptional profit with the share
of joint ventures and associates), curated business peers and a listed-holdings sum of parts, quarter revenue YoY and QoQ
in comparisons, the screener's sector column, per-sector cap and debt in rupees, and the shareholding pledge trend, SAST
threshold and BSE pointer. Never contacts the network."""
from datetime import date, timedelta
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT / "tests"))
import release
import screener
import shareholding as sh
import stock_analysis as sa
import test_screener as tscr
import test_shareholding as tsh
from test_stock_round3 import results_xml
from test_stock_analysis import NOW

CR = 1e7


# ---------- helpers ----------

def quarter(day, other, pbt=None, profit=None, eps=None, exceptional=0.0, tax=None, revenue=None):
    """One quarter as results_xbrl() returns it (amounts in ₹ crore here, rupees in the result)."""
    pbt = pbt if pbt is not None else 30000
    return {"quarter": day, "other_income": other * CR, "pbt": pbt * CR, "profit": (profit if profit is not None else 20000) * CR,
            "profit_total": (profit if profit is not None else 20000) * CR * 1.1, "eps": eps, "exceptional": exceptional * CR,
            "tax": (tax if tax is not None else pbt * 0.24) * CR, "revenue": (revenue or 250000) * CR, "basis": "consolidated"}


# Reliance's consolidated quarters as filed on NSE (₹ crore): other income, pre-tax profit, profit to shareholders, diluted EPS.
RELIANCE = [quarter("2026-09-30", 5793, 38613, 25548, 18.88), quarter("2026-06-30", 6550, 30630, 20946, 15.48),
            quarter("2026-03-31", 4447, 27195, 16971, 12.54), quarter("2025-12-31", 4914, 29697, 18645, 13.78),
            quarter("2025-09-30", 4482, 29124, 18165, 13.42), quarter("2025-06-30", 15119, 37146, 26994, 19.95, tax=6363),
            quarter("2025-03-31", 4905, 29103, 19407, 14.34)]


def eps_series(fy25=51.47, fy26=59.69):
    return {"annualDilutedEPS": [{"date": "2023-03-31", "value": 46.0}, {"date": "2024-03-31", "value": 51.45},
                                 {"date": "2025-03-31", "value": fy25}, {"date": "2026-03-31", "value": fy26}]}


def bars(days=900, price=1363.0):
    start = date(2026, 11, 18) - timedelta(days=days)
    # Prices swing ±10% around `price`; the last close is `price` itself.
    return [{"at": (start + timedelta(days=k)).isoformat() + "T00:00:00+05:30",
             "close": price if k == days else price * (0.9 + 0.2 * (k % 50) / 50), "high": price, "low": price} for k in range(days + 1)]


# ---------- 1. Other-income spikes ----------

class OtherIncomeSpikeTests(unittest.TestCase):
    def test_reliance_june_2025_stake_sale_is_flagged_with_its_excess(self):
        spikes = sa.other_income_spikes(RELIANCE)
        self.assertEqual([s["quarter"] for s in spikes], ["2025-06-30"])
        s = spikes[0]
        self.assertAlmostEqual(s["usual"], 4909.5 * CR)  # median of the other six quarters
        self.assertAlmostEqual(s["excess"], (15119 - 4909.5) * CR)
        self.assertEqual(s["quarters_compared"], 6)
        self.assertEqual(sa.spike_words(s), "other income in the Jun 2025 quarter was ₹15,119 cr, about ₹10,210 cr above its usual "
                                            "₹4,910 cr (median of the other 6 quarters filed on NSE)")

    def test_lumpy_seasonal_immaterial_and_short_histories_are_not_flagged(self):
        # Adani Ports, Nov 2026: three quarters near ₹800 cr against ₹234-296 cr; lumpy, not a one-off.
        lumpy = [quarter(d, o, pbt=4000, profit=3000) for d, o in (("2025-06-30", 296), ("2025-09-30", 837), ("2025-12-31", 234),
                                                      ("2026-03-31", 752), ("2026-06-30", 853), ("2026-09-30", 269))]
        self.assertEqual(sa.other_income_spikes(lumpy), [])
        # The same quarter a year apart nearly as high: dividends received every March quarter.
        seasonal = [quarter(d, o, pbt=3000, profit=2200) for d, o in (("2025-03-31", 650), ("2025-06-30", 200), ("2025-09-30", 210),
                                                         ("2025-12-31", 190), ("2026-03-31", 1000), ("2026-06-30", 205))]
        self.assertEqual(sa.other_income_spikes(seasonal), [])
        # Each alone would be flagged: the rules, not the materiality test, keep them out.
        self.assertEqual(len(sa.other_income_spikes([q for q in lumpy if q["quarter"] not in ("2026-03-31", "2026-06-30")]
                                                    + [quarter("2026-03-31", 250, pbt=4000, profit=3000), quarter("2026-06-30", 280, pbt=4000, profit=3000)])), 1)
        self.assertEqual(len(sa.other_income_spikes([q for q in seasonal if q["quarter"] != "2025-03-31"]
                                                    + [quarter("2025-03-31", 195, pbt=3000, profit=2200)])), 1)
        # Triple the usual level but under a tenth of pre-tax profit: not material.
        small = [quarter(d, o, pbt=50000) for d, o in (("2025-12-31", 100), ("2026-03-31", 110), ("2026-06-30", 105),
                                                       ("2026-09-30", 95), ("2025-09-30", 330))]
        self.assertEqual(sa.other_income_spikes(small), [])
        self.assertEqual(sa.other_income_spikes(RELIANCE[:4]), [])  # three other quarters is too few for a usual level
        self.assertEqual(len(sa.other_income_spikes(RELIANCE[:5] + RELIANCE[5:6])), 1)

    def test_a_spike_inside_the_trailing_year_joins_the_trailing_one_offs(self):
        recent = [quarter("2026-09-30", 816, pbt=3200, profit=2393), quarter("2026-06-30", 146, pbt=2000, profit=1300),
                  quarter("2026-03-31", 184, pbt=1900, profit=1250), quarter("2025-12-31", 151, pbt=2100, profit=1400)]
        fund = {"other_income_spikes": [{"quarter": "2025-12-31", "other_income": 816 * CR, "usual": 146 * CR, "excess": 670 * CR}],
                "eps": {"value": 72.0, "basis": "trailing 12 months"}, "pe": 50.0, "market_cap": 3.2e12}
        recent[3]["other_income"] = 816 * CR
        note = sa.trailing_one_offs(fund, {"recent": recent}, 3600.0)
        self.assertIn("Dec 2025 quarter: other income of ₹816 cr, about ₹670 cr above its usual ₹146 cr (often a one-off gain)", note)
        shares = 3.2e12 / 3600.0
        self.assertIn(f"the trailing P/E would be about {3600.0 / (72.0 - 670 * CR / shares):,.1f} instead of 50.0", note)

    def test_a_spike_in_the_latest_quarter_gives_the_year_on_year_change_without_it(self):
        fund = {"other_income_spikes": [{"quarter": "2026-09-30", "other_income": 816 * CR, "usual": 146 * CR, "excess": 670 * CR,
                                         "quarters_compared": 5}],
                "quarter_profit_values": (2393 * CR, 1120 * CR), "latest_quarter": {"date": "2026-09-30"},
                "quarter_year_ago": "2025-09-30", "quarter_profit_yoy": 2393 / 1120 - 1}
        note = sa.flag_quarter_spike(fund)
        self.assertTrue(note.startswith("The latest quarter's profit includes unusually high other income, often a one-off gain: other income "
                                        "in the Sep 2026 quarter was ₹816 cr"))
        self.assertIn(f"would have changed about {sa.pct((2393 - 670) / 1120 - 1)} vs a year earlier instead of +113.7%", note)
        self.assertIn(note, fund["eps_flags"])
        self.assertIsNone(sa.flag_quarter_spike({"other_income_spikes": [], "quarter_profit_values": (1.0, 1.0)}))


# ---------- 2. One-offs inside annual EPS ----------

class AnnualOneOffTests(unittest.TestCase):
    def reliance_fund(self, with_band=True):
        fund = {"eps_growth": 59.69 / 51.47 - 1, "other_income_spikes": sa.other_income_spikes(RELIANCE)}
        if with_band:
            rows = eps_series()["annualDilutedEPS"]
            b = bars()
            fund["pe_band"] = screener.pe_band(b, rows)
            fund["_band_inputs"] = (b, rows, {})
        return fund

    def test_reliance_fy2026_growth_and_band_without_the_gain(self):
        fund = self.reliance_fund()
        note = sa.annual_one_offs(fund, {"quarters": RELIANCE}, eps_series(), annual=lambda: {})
        out = fund["annual_one_off"]
        rate = sa.usual_tax_rate(RELIANCE)
        self.assertAlmostEqual(rate, 0.24, places=3)
        shares = sa._shares_from([q for q in RELIANCE if "2025-04-01" <= q["quarter"] <= "2026-03-31"])
        excess = (15119 - 4909.5) * CR * (1 - rate)
        self.assertAlmostEqual(out["eps_adjusted"], 59.69 - excess / shares)
        self.assertAlmostEqual(out["growth_adjusted"], (59.69 - excess / shares) / 51.47 - 1)
        self.assertTrue(0.04 < out["growth_adjusted"] < 0.06)  # about +5%, against +16.0% reported
        self.assertTrue(note.startswith("FY2026 earnings include one-offs: other income in the Jun 2025 quarter was ₹15,119 cr"))
        self.assertIn("usually a one-off gain such as an investment or stake sale; check that quarter's results release", note)
        self.assertIn(f"FY2026 EPS would be about ₹{out['eps_adjusted']:,.2f} instead of ₹59.69, and EPS growth about "
                      f"{sa.pct(out['growth_adjusted'])} instead of +16.0%", note)
        band = out["band"]
        self.assertGreater(band["current"], fund["pe_band"]["current"])  # less EPS, higher P/E
        self.assertAlmostEqual(band["current"], bars()[-1]["close"] / (59.69 - excess / shares))
        self.assertIn("on that EPS today's P/E would be", note)
        self.assertNotIn("_band_inputs", fund)
        self.assertIn(note, fund["eps_flags"])

    def test_labour_code_charges_raise_growth_without_them(self):
        tcs = [quarter("2026-09-30", 1337, 17000, 12500, 34.5), quarter("2026-06-30", 1568, 17200, 12700, 35.1),
               quarter("2026-03-31", 1500, 16800, 12300, 34.0), quarter("2025-12-31", 1118, 13000, 10657, 29.5, exceptional=-3391),
               quarter("2025-09-30", 1300, 16500, 12075, 33.4, exceptional=-1135), quarter("2025-06-30", 1400, 17000, 12760, 35.3)]
        fund = {"eps_growth": 0.014, "other_income_spikes": sa.other_income_spikes(tcs)}
        self.assertEqual(fund["other_income_spikes"], [])
        series = {"annualDilutedEPS": [{"date": "2025-03-31", "value": 134.19}, {"date": "2026-03-31", "value": 136.07}]}
        note = sa.annual_one_offs(fund, {"quarters": tcs}, series, annual=lambda: {"2025-03-31": {"exceptional": 0.0}})
        out = fund["annual_one_off"]
        self.assertGreater(out["growth_adjusted"], 0.07)
        self.assertIn("exceptional charges of ₹1,135 cr in the Sep 2025 quarter; exceptional charges of ₹3,391 cr in the Dec 2025 quarter", note)
        self.assertIn("Without them (estimate: taxed at the usual 24% rate, per diluted share)", note)

    def test_one_offs_on_both_sides_that_change_nothing_are_left_out(self):
        # Grasim FY2026: charges in both years, growth the same with or without them.
        quarters = [quarter(d, 300, 2000, 1250, 18.0, exceptional=e) for d, e in
                    (("2026-09-30", 0), ("2026-06-30", 0), ("2026-03-31", -120), ("2025-12-31", -120), ("2025-09-30", 0), ("2025-06-30", 0))]
        series = {"annualDilutedEPS": [{"date": "2025-03-31", "value": 60.0}, {"date": "2026-03-31", "value": 72.0}]}
        fund = {"eps_growth": 0.2, "other_income_spikes": []}
        self.assertIsNone(sa.annual_one_offs(fund, {"quarters": quarters}, series,
                                             annual=lambda: {"2025-03-31": {"exceptional": -200 * CR, "profit": 4166 * CR, "eps": 60.0}}))
        self.assertNotIn("annual_one_off", fund)

    def test_sum_of_parts_worth_more_than_the_parent(self):
        holdings = {"rows": [{"symbol": "ULTRACEMCO", "name": "UltraTech Cement Limited", "stake_pct": 56.11, "market_cap": 3.65e12, "pe": 41.6,
                              "pattern_date": "2026-09-30", "quarterly": True}], "rest": "viscose fibre and chemicals"}
        s = sa.sum_of_parts(1.5e12, 42.8, holdings)
        line = sa.sotp_line("Grasim Industries Limited", s)
        self.assertIn("the listed stakes alone are worth more than the whole company, so the market values viscose fibre and chemicals at "
                      f"nothing and applies about a {sa.pct(1 - 1.5e12 / (0.5611 * 3.65e12), 0, sign=False)} discount to the stakes.", line)
        self.assertNotIn("₹-", line)
        self.assertNotIn("rest_pe", s)

    def test_a_mis_tagged_prior_year_profit_does_not_distort_its_shares(self):
        fund = {"eps_growth": 59.69 / 51.47 - 1, "other_income_spikes": sa.other_income_spikes(RELIANCE)}
        bad = {"2025-03-31": {"exceptional": -500 * CR, "profit": 147 * CR, "eps": 51.47}}  # profit far off its own EPS
        sa.annual_one_offs(fund, {"quarters": RELIANCE}, eps_series(), annual=lambda: bad)
        good = {"eps_growth": fund["eps_growth"], "other_income_spikes": fund["other_income_spikes"]}
        sa.annual_one_offs(good, {"quarters": RELIANCE}, eps_series(), annual=lambda: {"2025-03-31": {"exceptional": -500 * CR}})
        self.assertAlmostEqual(fund["annual_one_off"]["growth_adjusted"], good["annual_one_off"]["growth_adjusted"])
        self.assertTrue(0.0 < fund["annual_one_off"]["growth_adjusted"] < 0.05)

    def test_small_one_offs_banks_and_withheld_growth_are_left_alone(self):
        small = [dict(q) for q in RELIANCE]
        for q in small:
            q["other_income"] = 5000 * CR
        small[3]["exceptional"] = -100 * CR  # under 2% of the year's profit
        self.assertIsNone(sa.annual_one_offs({"eps_growth": 0.16}, {"quarters": small}, eps_series()))
        self.assertIsNone(sa.annual_one_offs({"bank_like": True, "other_income_spikes": sa.other_income_spikes(RELIANCE)},
                                             {"quarters": RELIANCE}, eps_series()))
        self.assertIsNone(sa.annual_one_offs({"growth_withheld": True}, {"quarters": RELIANCE}, eps_series()))


# ---------- 3. Pre-exceptional profit with the share of joint ventures and associates ----------

class PreExceptionalTests(unittest.TestCase):
    FACTS = {"RevenueFromOperations": 96920 * CR, "ProfitOrLossAttributableToOwnersOfParent": -5531 * CR,
             "ProfitBeforeExceptionalItemsAndTax": 374 * CR, "ExceptionalItemsBeforeTax": -6738 * CR, "ProfitBeforeTax": -6364 * CR,
             "TaxExpense": -884 * CR, "ProfitLossForPeriodFromContinuingOperations": -5480 * CR,
             "ShareOfProfitLossOfAssociatesAndJointVenturesAccountedForUsingEquityMethod": 29 * CR,
             "ProfitLossFromDiscontinuedOperationsAfterTax": 0, "ProfitLossForPeriod": -5451 * CR}

    def test_tmpv_share_of_joint_ventures_after_tax_is_added_back(self):
        q = sa.results_xbrl(results_xml("2026-07-01", "2026-09-30", self.FACTS))
        self.assertAlmostEqual(q["pbt_before_exceptional"], 403 * CR)
        self.assertAlmostEqual(q["pbt_before_exceptional_tagged"], 374 * CR)
        self.assertAlmostEqual(q["associates_share_after_tax"], 29 * CR)
        fund = {}
        note = sa.flag_latest_exceptional(fund, {"latest": q})
        self.assertIn("profit before exceptional items and tax was ₹403 cr (including the ₹29 cr share of joint ventures and associates, "
                      "which the filing adds after tax; ₹374 cr before it)", note)

    def test_share_already_inside_pre_tax_profit_is_not_added_twice(self):
        inside = {**self.FACTS, "ProfitLossForPeriod": -5480 * CR}  # the period's profit equals continuing operations
        q = sa.results_xbrl(results_xml("2026-07-01", "2026-09-30", inside))
        self.assertAlmostEqual(q["pbt_before_exceptional"], 374 * CR)
        self.assertIsNone(q["associates_share_after_tax"])
        self.assertIsNone(sa.share_after_tax(-5451 * CR, None, 29 * CR, 0.0))
        self.assertAlmostEqual(sa.share_after_tax(-5451 * CR, -5480 * CR, 29 * CR, None), 29 * CR)

    def test_signed_crore(self):
        self.assertEqual(sa.signed_crore(-29 * CR), "−₹29 cr")
        self.assertEqual(sa.signed_crore(403 * CR), "₹403 cr")


# ---------- 4. NSE quarters read back up to eight ----------

class QuarterHistoryTests(unittest.TestCase):
    def test_older_quarters_are_read_and_recent_stays_four(self):
        X = "https://nsearchives.nseindia.in/corporate/xbrl/INTEGRATED_FILING_INDAS_{}_WEB.xml"
        ends = ["2026-09-30", "2026-06-30", "2026-03-31", "2025-12-31", "2025-09-30", "2025-06-30", "2025-03-31"]
        starts = ["2026-07-01", "2026-04-01", "2026-01-01", "2025-10-01", "2025-07-01", "2025-04-01", "2025-01-01"]
        qe = [date.fromisoformat(e).strftime("%d-%b-%Y").upper() for e in ends]
        listing = {"data": [{"qe_Date": q, "type": "Integrated Filing- Financials", "consolidated": "Consolidated", "xbrl": X.format(i),
                             "broadcast_Date": "x"} for i, q in enumerate(qe)]}
        files = {X.format(i): results_xml(s, e, {"RevenueFromOperations": (100 + i) * 1e9, "ProfitOrLossAttributableToOwnersOfParent": 1e10,
                                                 "OtherIncome": 3e10 if e == "2025-06-30" else 1e9, "ProfitBeforeTax": 1.4e10})
                 for i, (s, e) in enumerate(zip(starts, ends))}

        def fetch(url):
            return json.dumps(listing).encode() if "integrated-filing-results" in url else files[url]
        nse = sa.nse_quarter_results("RELIANCE", fetch)
        self.assertEqual([q["quarter"] for q in nse["recent"]], ends[:4])
        self.assertEqual([q["quarter"] for q in nse["quarters"]], ends)
        self.assertEqual([s["quarter"] for s in sa.other_income_spikes(nse["quarters"])], ["2025-06-30"])
        fund = {}
        sa.apply_quarter_results(fund, nse)
        self.assertAlmostEqual(fund["quarter_revenue_qoq"], 100 / 101 - 1)
        self.assertNotIn("quarters", fund["quarter_nse"])

    def test_an_empty_first_page_falls_back_to_the_second(self):
        X = "https://nsearchives.nseindia.in/corporate/xbrl/INTEGRATED_FILING_INDAS_9_WEB.xml"
        page1 = {"data": [{"type": "Announcement"}], "totalCount": 25}
        page2 = {"data": [{"qe_Date": "30-SEP-2026", "type": "Integrated Filing- Financials", "consolidated": "Consolidated", "xbrl": X,
                           "broadcast_Date": "x"}], "totalCount": 25}

        def fetch(url):
            if "integrated-filing-results" in url:
                return json.dumps(page2 if "page=2" in url else page1).encode()
            return results_xml("2026-07-01", "2026-09-30", {"RevenueFromOperations": 1e11, "ProfitOrLossAttributableToOwnersOfParent": 1e10})
        self.assertEqual(sa.nse_quarter_results("COALINDIA", fetch)["latest"]["quarter"], "2026-09-30")


# ---------- 5. Business peers and listed holdings ----------

TABLE = {"checked": "2026-11-18", "companies": {
    "RELIANCE": {"peers": {"IOC": "refining", "BPCL": "refining", "BHARTIARTL": "telecom", "DMART": "retail"}, "holdings": ["JPL"],
                 "rest": "oil-to-chemicals and retail"},
    "SBIN": {"holdings": ["SBILIFE", "SBICARD"]}}}


class BusinessPeerTests(unittest.TestCase):
    def test_the_asset_file_is_valid_and_packaged(self):
        data = sa.load_business_peers()
        self.assertIn("RELIANCE", data["companies"])
        self.assertEqual(data["companies"]["RELIANCE"]["holdings"], ["JPL"])
        self.assertEqual(set(data["companies"]["RELIANCE"]["peers"]), {"IOC", "BPCL", "BHARTIARTL", "DMART", "TRENT"})
        for base, entry in data["companies"].items():
            self.assertNotIn(base, entry.get("peers") or {})
            self.assertNotIn(base, entry.get("holdings") or [])
        packaged = {p.relative_to(release.ROOT).as_posix() for p in release.release_files()}
        self.assertIn("assets/business_peers.json", packaged)
        self.assertFalse(any("__pycache__" in p or p.endswith(".pyc") for p in packaged))
        self.assertEqual(sa.load_business_peers(Path(tempfile.gettempdir()) / "no-such-business-peers.json"), {})

    def test_business_group_is_the_default_and_names_each_business(self):
        group = sa.business_group("RELIANCE", TABLE)
        self.assertEqual(group["kind"], "business")
        self.assertEqual([p["symbol"] for p in group["peers"]], ["IOC", "BPCL", "BHARTIARTL", "DMART"])
        self.assertIn("refining: IOC, BPCL; telecom: BHARTIARTL; retail: DMART", group["basis"])
        self.assertIsNone(sa.business_group("SBIN", TABLE))  # holdings only: index peers stay
        self.assertIsNone(sa.business_group("TCS", TABLE))
        with mock.patch.object(screener, "peers_for", side_effect=AssertionError("index peers must not load")), \
                mock.patch.object(screener, "snapshot", side_effect=lambda s, **k: {"symbol": s + ".NS", "pe": 10.0}):
            group, loaded = sa.load_peers("RELIANCE.NS", fetcher=None, now=NOW)
        self.assertEqual(group["kind"], "business")
        self.assertEqual(len(loaded), 5)
        line = sa.peer_line({"kind": "business", "rows": [{"symbol": "IOC.NS"}, {"symbol": "BHARTIARTL.NS"}], "medians": {"pe": 20.0, "roe": 0.2}})
        self.assertTrue(line.startswith("Business peers (IOC, BHARTIARTL): median P/E 20.0"))

    def test_sum_of_parts_values_the_stake_and_the_rest(self):
        holdings = {"rows": [{"symbol": "JPL", "name": "Jio Platforms Limited", "stake_pct": 64.48, "market_cap": 12.79e12, "pe": 40.3,
                              "pattern_date": "2026-10-27", "quarterly": False}], "rest": "oil-to-chemicals and retail"}
        s = sa.sum_of_parts(18.44e12, 22.5, holdings)
        self.assertAlmostEqual(s["total"], 0.6448 * 12.79e12)
        self.assertAlmostEqual(s["share_of_cap"], 0.6448 * 12.79 / 18.44)
        rest_earnings = 18.44e12 / 22.5 - 0.6448 * 12.79e12 / 40.3
        self.assertAlmostEqual(s["rest_pe"], (18.44e12 - 0.6448 * 12.79e12) / rest_earnings)
        line = sa.sotp_line("Reliance Industries Limited", s)
        self.assertTrue(line.startswith("Sum of parts (listed holdings at market value, before any holding-company discount): 64.48% of "
                                        "Jio Platforms Limited (JPL; NSE shareholding pattern filed on listing, dated 27 Oct 2026) × its market cap "
                                        "₹12.79 lakh cr = ₹8.25 lakh cr — 45% of Reliance Industries Limited's own market cap, leaving ₹10.19 lakh cr "
                                        "for oil-to-chemicals and retail"))
        self.assertIn(f"about {s['rest_pe']:.1f}× that part's share of trailing earnings (estimate)", line)
        missing = {"rows": [{"symbol": "JPL", "error": "no shareholding pattern on NSE for JPL"}]}
        self.assertIsNone(sa.sum_of_parts(18.44e12, 22.5, missing))
        self.assertIsNone(sa.sum_of_parts(None, 22.5, holdings))

    def test_listed_holding_reads_the_parent_stake_from_promoter_entities(self):
        master = json.dumps([{"date": "27-OCT-2026", "broadcastDate": "28-OCT-2026 10:00:00", "pr_and_prgrp": "64.48", "public_val": "35.52",
                              "xbrl": "https://nsearchives.nseindia.in/corporate/xbrl/SHP_9_WEB.xml", "name": "Jio Platforms Limited"}]).encode()
        parsed = {"company": "Jio Platforms Limited", "promoter_holders": [{"name": "RELIANCE INDUSTRIES LIMITED", "pct": 64.48, "shares": 1.0}]}

        def nse(url):
            return master if "share-holdings-master" in url else b"<xbrl/>"
        with mock.patch.object(sh, "parse_xbrl", return_value=parsed), \
                mock.patch.object(screener, "snapshot", return_value={"name": "Jio Platforms Limited", "market_cap_cr": 1278674.0, "pe": 40.3}):
            r = sa.listed_holding("Reliance Industries Limited", "JPL", fetcher=None, nse_fetcher=nse, now=NOW)
        self.assertEqual((r["stake_pct"], r["pattern_date"], r["quarterly"]), (64.48, "2026-10-27", False))
        self.assertAlmostEqual(r["market_cap"], 1278674.0 * CR)
        with mock.patch.object(sh, "parse_xbrl", return_value={"promoter_holders": [{"name": "Someone Else Ltd", "pct": 50.0}]}), \
                mock.patch.object(screener, "snapshot", return_value={"market_cap_cr": 1.0}):
            self.assertIsNone(sa.listed_holding("Reliance Industries Limited", "JPL", fetcher=None, nse_fetcher=nse, now=NOW)["stake_pct"])

    def test_load_holdings_keeps_failures_as_rows(self):
        def fake(parent, sym, **kw):
            if sym == "SBICARD":
                raise sa.Block("STOCK: no shareholding pattern on NSE for SBICARD")
            return {"symbol": sym, "name": "SBI Life", "stake_pct": 55.0, "market_cap": 1e12, "pe": 60.0, "pattern_date": "2026-09-30", "quarterly": True}
        with mock.patch.object(sa, "listed_holding", side_effect=fake):
            out = sa.load_holdings("SBIN", "State Bank of India", fetcher=None, nse_fetcher=None, now=NOW, table=TABLE)
        self.assertEqual([r["symbol"] for r in out["rows"]], ["SBILIFE", "SBICARD"])
        self.assertEqual(out["rows"][1]["error"], "no shareholding pattern on NSE for SBICARD")
        s = sa.sum_of_parts(8e12, 10.0, out)
        self.assertIn("not valued: SBICARD (no shareholding pattern on NSE for SBICARD)", sa.sotp_line("SBI", s))
        self.assertIsNone(sa.load_holdings("TCS", "TCS", fetcher=None, nse_fetcher=None, now=NOW, table=TABLE))


# ---------- 6. Comparison: quarter revenue and annual one-offs ----------

class CompareRound6Tests(unittest.TestCase):
    def row(self, sym, growth, adjusted=None, items=()):
        return {"symbol": sym, "name": sym, "price": 100.0, "price_date": "2026-11-18", "roe": 0.3, "net_margin": 0.15, "pe": 20.0,
                "pe_basis": "trailing 12 months", "revenue_growth": 0.1, "eps_growth": growth, "eps_growth_adjusted": adjusted,
                "annual_one_off": {"items": list(items)} if items else None, "quarter_revenue_yoy": 0.11, "quarter_revenue_qoq": 0.013,
                "quarter_profit_yoy": 0.15, "return_1y": -0.2, "dividend_yield_ex_specials": 0.03, "debt_to_equity": 0.1,
                "debt_date": "2026-09-30", "flags": [], "one_offs": []}

    def test_eps_cell_tradeoffs_and_quarter_revenue(self):
        rows = [self.row("TCS", 0.014, 0.084, [{"quarter": "2025-12-31", "kind": "exceptional", "amount": -3391 * CR}]),
                self.row("INFY", 0.111)]
        lines = sa.tradeoffs(rows)
        growth = next(x for x in lines if x.startswith("Growth"))
        self.assertIn("EPS: INFY +11.1%, TCS +1.4% (+8.4% without one-offs, estimate)", growth)
        self.assertIn("latest quarter revenue vs a year earlier: INFY +11.0% (+1.3% on the quarter before)", growth)
        self.assertIn("One-offs inside annual EPS growth (estimates without them, taxed at each company's usual rate) — TCS: +1.4% reported, "
                      "about +8.4% without exceptional charges of ₹3,391 cr (Dec 2025 quarter).", lines)
        table = "\n".join(sa.compare_table({"rows": rows}))
        self.assertIn("| +1.4% (≈+8.4% ex one-offs) | n/a / n/a | +11.0% / +1.3% |", table)
        self.assertIn("| +11.1% | n/a / n/a | +11.0% / +1.3% |", table)


# ---------- 7. Screener: sector, per-sector cap, debt in rupees ----------

class ScreenerRound6Tests(unittest.TestCase):
    SYMBOLS = ["ALPHA", "BETA", "GAMMA", "DELTA", "EPSILON"]

    def test_sector_cap_names_only_what_would_have_shown(self):
        r = screener.screen("nifty50", [], symbols=self.SYMBOLS, fetcher=tscr.Feed(), now=NOW, sort="pe", max_per_sector=1, limit=3)
        sectors = [x["industry"] for x in r["results"]]
        self.assertEqual(sorted(sectors), ["Financial Services", "Textiles"])
        self.assertEqual(r["matched"], 5)
        self.assertEqual(len(r["sector_capped"]), 3)  # the list never filled, so every capped stock would have shown
        text = screener.markdown(r)
        self.assertIn("| Stock | sector |", text)
        self.assertIn("At most 1 per sector, for a diversified list: left out ", text)
        self.assertIn("Rerun with --max-per-sector 0 to see every match.", text)
        r1 = screener.screen("nifty50", [], symbols=self.SYMBOLS, fetcher=tscr.Feed(), now=NOW, sort="pe", max_per_sector=1, limit=1)
        self.assertEqual(r1["sector_capped"], [])  # nothing capped before the one-row list filled

    def test_concentration_warning_without_a_cap(self):
        r = {"results": [{"industry": "Information Technology"}] * 6 + [{"industry": "Power"}] * 8, "max_per_sector": None}
        self.assertEqual(screener.sector_lines({**r, "results": r["results"][:6] + r["results"][6:9]}),
                         ["", "⚠ Concentrated: 6 of 9 are Information Technology. For a diversified shortlist, rerun with --max-per-sector 2 "
                              "(or pick across sectors)."])
        self.assertEqual(screener.sector_lines({"results": [{"industry": "A"}, {"industry": "B"}]}), [])

    def test_debt_in_rupees_comes_from_the_debt_equity_balance_sheet(self):
        self.assertEqual(screener.cell("debt_cr", 183002.4), "1,83,002")
        self.assertIn("debt_cr", screener.PRESETS["dividend"]["columns"])
        self.assertLess(screener.PRESETS["dividend"]["columns"].index("debt_to_equity"), screener.PRESETS["dividend"]["columns"].index("debt_cr"))
        f = sa.ratios({"annualTotalDebt": [{"date": "2026-03-31", "value": 491 * CR}],
                       "annualStockholdersEquity": [{"date": "2026-03-31", "value": 70000 * CR}]}, 300.0, NOW)
        self.assertAlmostEqual(f["total_debt"], 491 * CR)
        fund = {"debt_date": "2026-03-31"}
        sa.apply_balance_sheet(fund, {"latest": {"basis": "consolidated", "balance": {"date": "2026-09-30", "borrowings": 600 * CR, "equity": 7e11}}})
        self.assertAlmostEqual(fund["total_debt"], 600 * CR)

    def test_dividend_preset_short_list_caps_two_per_sector(self):
        seen = {}

        def fake(*args, **kw):
            seen.update(kw)
            return {"schema": "screen_v1", "universe": "nifty100", "as_of": NOW.isoformat(), "filters": [], "sort": None, "results": [],
                    "requested": 0, "evaluated": 0, "matched": 0, "missing_data": [], "failed": [], "truncated": False, "note": "n"}
        with mock.patch.object(screener, "screen", side_effect=fake), tempfile.TemporaryDirectory() as tmp:
            run = lambda n, *args: screener.main(["--universe", "nifty100", *args, "--out", str(Path(tmp) / f"{n}.md")])
            run(1, "--preset", "dividend", "--limit", "5")
            self.assertEqual((seen["max_per_sector"], seen["limit"]), (2, 5))
            run(2, "--preset", "dividend")
            self.assertEqual((seen["max_per_sector"], seen["limit"]), (None, 25))
            run(3, "--preset", "dividend", "--limit", "5", "--max-per-sector", "0")
            self.assertIsNone(seen["max_per_sector"])
            run(4, "--where", "pe<20", "--max-per-sector", "3")
            self.assertEqual(seen["max_per_sector"], 3)


# ---------- 8. Shareholding: pledge trend, SAST threshold, BSE pointer ----------

class ShareholdingRound6Tests(unittest.TestCase):
    def test_pledge_trend_lines(self):
        zero = {"pledge_trend": [{"quarter": "2026-09-30", "pledged_pct": 0.0, "encumbered_pct": 0.0},
                                 {"quarter": "2026-06-30", "pledged_pct": 0.0, "encumbered_pct": 0.0},
                                 {"quarter": "2025-03-31", "pledged_pct": None, "encumbered_pct": 0.0}]}
        self.assertEqual(sh.pledge_trend_line(zero), "Promoter pledge over Mar 2025–Sep 2026 (3 quarterly filings): none in any quarter, "
                                                     "and nothing otherwise encumbered.")
        rising = {"pledge_trend": [{"quarter": "2026-09-30", "pledged_pct": 12.0, "encumbered_pct": 20.0},
                                   {"quarter": "2026-06-30", "pledged_pct": 15.5, "encumbered_pct": 18.0},
                                   {"quarter": "2026-03-31", "pledged_pct": 4.0, "encumbered_pct": 4.0}]}
        self.assertEqual(sh.pledge_trend_line(rising),
                         "Pledged share of the promoter holding over Mar 2026–Sep 2026 (3 quarterly filings): 4.00% (Mar 2026) → 12.00% (Sep 2026), "
                         "peak 15.50% (Jun 2026); all encumbrance (pledges, non-disposal undertakings and other) 4.00% → 20.00%, peak 20.00% (Sep 2026).")
        self.assertIsNone(sh.pledge_trend_line({"pledge_trend": zero["pledge_trend"][:1]}))
        self.assertEqual(sh.pledge_trend([("2026-09-30", None), ("2026-06-30", {"pledge": {"pledged_pct_of_promoter": 1.0, "encumbered_pct_of_promoter": 2.0}})]),
                         [{"quarter": "2026-06-30", "pledged_pct": 1.0, "encumbered_pct": 2.0}])

    def test_report_reads_every_trend_quarter_and_digest_stays_short(self):
        r = tsh.report()
        self.assertEqual([x["quarter"] for x in r["pledge_trend"]], ["2026-09-30", "2026-06-30"])
        text = sh.summary(r)
        self.assertIn("agrees. Promoter pledge over Jun 2026–Sep 2026 (2 quarterly filings): none in any quarter, and nothing otherwise encumbered.", text)
        self.assertIn("Pledged share of the promoter holding by quarter: Sep 2026 0.00% · Jun 2026 0.00%.", sh.markdown(r))

    def test_every_trend_quarter_is_read_for_the_pledge_trend(self):
        older = "https://nsearchives.nseindia.in/corporate/xbrl/SHP_0_10042026_WEB.xml"
        rows = json.loads(tsh.master())
        rows[3]["xbrl"] = older  # the March 2026 filing, outside the two quarters compared
        feed = tsh.Feed()
        feed.files[tsh.MASTER] = json.dumps(rows).encode()
        feed.files[older] = tsh.shp(tsh.PREVIOUS_HOLDINGS, pledged=152133757, encumbered=152133757, day="2026-03-31")
        r = sh.shareholding("adaniports", fetcher=feed, clock=lambda: NOW)
        self.assertIn(older, feed.urls)
        self.assertEqual([x["quarter"] for x in r["pledge_trend"]], ["2026-09-30", "2026-06-30", "2026-03-31"])
        self.assertEqual(sh.pledge_trend_line(r), "Pledged share of the promoter holding over Mar 2026–Sep 2026 (3 quarterly filings): 10.00% (Mar 2026) "
                                                  "→ 0.00% (Sep 2026), peak 10.00% (Mar 2026); all encumbrance (pledges, non-disposal undertakings and "
                                                  "other) 10.00% → 0.00%, peak 10.00% (Mar 2026).")

    def test_older_format_reads_pledged_or_encumbered_shares(self):
        body = tsh.shp(tsh.LATEST_HOLDINGS).replace(
            b"</xbrli:xbrl>", b'<in-bse-shp:PledgedOrEncumberedNumberOfShares contextRef="C1" unitRef="shares" decimals="0">152133757'
                              b"</in-bse-shp:PledgedOrEncumberedNumberOfShares></xbrli:xbrl>")
        p = sh.parse_xbrl(body)["pledge"]
        self.assertAlmostEqual(p["encumbered_pct_of_promoter"], 10.0, places=3)

    def test_sast_threshold_and_bse_pointer(self):
        small = {"promoter_moves": [{"quarter": "2026-06-30", "pts": -1.99, "filings": [], "deals": {"deals": [], "checked": ["bulk_deals", "block_deals"]}}]}
        line = sh.move_lines(small)[0]
        self.assertIn("no promoter-group SAST (Reg. 29) filing on NSE in that window, and none was required for a net change of this size", line)
        self.assertIn("The trade may have been on BSE (its bulk and block deals are at https://www.bseindia.com/markets/equity/EQReports/tra_Bulk_Deals.aspx "
                      "and https://www.bseindia.com/markets/equity/EQReports/tra_Block_Deals.aspx; BSE blocks scripted access", line)
        big = {"promoter_moves": [{"quarter": "2026-06-30", "pts": -2.5, "filings": [], "deals": {"deals": [], "checked": ["bulk_deals"]}}]}
        self.assertNotIn("none was required", sh.move_lines(big)[0])


if __name__ == "__main__":
    unittest.main()
