"""Offline contracts for the round-4 stock fixes: trade-offs by dimension without a tally, one-offs in trailing
earnings, the newest balance sheet with its date, exceptional items in the latest quarter, a deeper P/E history from
NSE annual filings, quote-page headlines, lakh-crore and Indian digit grouping, profit attributable to shareholders
against total profit, pointers for large promoter moves, and dividend-streak gaps. Never contacts the network."""
import json
from pathlib import Path
import sys
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT / "tests"))
from core import Block, instant
import screener
import shareholding as sh
import stock_analysis as sa
import test_screener as ts
import test_shareholding as tsh
import test_stock_analysis as tsa
from test_stock_analysis import NOW
from test_stock_round3 import results_xml

CR = 1e7  # rupees in a crore


# ---------- 1. Trade-offs: one line per dimension, no tally ----------

def row(symbol, **kw):
    base = {"symbol": symbol, "roe": 0.2, "net_margin": 0.1, "pe": 20.0, "pe_basis": "trailing 12 months", "revenue_growth": 0.05,
            "eps_growth": 0.05, "quarter_profit_yoy": 0.05, "return_1y": 0.0, "dividend_yield_ex_specials": 0.02,
            "debt_to_equity": 0.3, "debt_date": "2026-09-30", "growth_period": "FY2026 vs FY2025"}
    return {**base, **kw}


class TradeoffTests(unittest.TestCase):
    def rows(self):
        return [row("TCS", roe=0.487, net_margin=0.184, pe=16.8, revenue_growth=0.046, eps_growth=0.014, return_1y=-0.229,
                    dividend_yield_ex_specials=0.028, debt_to_equity=0.10,
                    one_offs=[{"quarter": "2025-12-31", "exceptional": -3391 * CR, "other_income": 1118 * CR,
                               "negative_other_income": False, "profit": 10657 * CR}]),
                row("INFY", roe=0.311, net_margin=0.164, pe=14.7, revenue_growth=0.096, eps_growth=0.111, return_1y=-0.278,
                    dividend_yield_ex_specials=0.046, debt_to_equity=0.05),
                row("HCLTECH", roe=0.23, net_margin=0.128, pe=20.1, revenue_growth=0.112, eps_growth=-0.043, return_1y=-0.151,
                    dividend_yield_ex_specials=0.044, debt_to_equity=0.08)]

    def test_no_tally_and_one_line_per_dimension(self):
        lines = sa.tradeoffs(self.rows())
        self.assertFalse(any(x.startswith("Leads") or " of 8 measures" in x for x in lines))
        heads = [x.split(" (best first")[0] for x in lines if "(best first" in x]
        self.assertEqual(heads, ["Quality", "Value", "Growth", "Momentum", "Income"])
        self.assertIn("Balance sheet — all effectively debt-free", "\n".join(lines))  # every debt/equity under 0.15: not ranked
        quality = next(x for x in lines if x.startswith("Quality"))
        self.assertIn("ROE: TCS 48.7%, INFY 31.1%, HCLTECH 23.0%; net margin: TCS 18.4%, INFY 16.4%, HCLTECH 12.8%", quality)
        self.assertIn("P/E today: INFY 14.7, TCS 16.8, HCLTECH 20.1", next(x for x in lines if x.startswith("Value")))
        self.assertIn("No single stock wins", lines[-1])

    def test_revenue_growth_is_ranked_in_rupees(self):
        growth = next(x for x in sa.tradeoffs(self.rows()) if x.startswith("Growth"))
        self.assertIn("annual figures FY2026 vs FY2025, in rupees", growth)
        self.assertIn("revenue: HCLTECH +11.2%, INFY +9.6%, TCS +4.6%", growth)
        self.assertIn("EPS: INFY +11.1%, TCS +1.4%, HCLTECH -4.3%", growth)

    def test_withheld_growth_and_banks_are_left_out_and_named(self):
        rows = self.rows()
        rows[2].update(growth_withheld="demerger", revenue_growth=0.5)
        rows.append(row("HDFCBANK", bank_like=True, debt_to_equity=None))
        lines = sa.tradeoffs(rows)
        growth = next(x for x in lines if x.startswith("Growth"))
        self.assertNotIn("HCLTECH +50.0%", growth)
        self.assertIn("(HCLTECH: annual growth not comparable, demerger)", growth)
        sheet = next(x for x in lines if x.startswith("Balance sheet"))
        self.assertIn("all effectively debt-free (debt/equity at 30 Sep 2026: INFY 0.05, HCLTECH 0.08, TCS 0.10)", sheet)
        self.assertNotIn("HDFCBANK 0", sheet)
        self.assertIn("(HDFCBANK: not meaningful for banks and financials)", sheet)

    def test_balance_sheet_dates_shown_per_stock_when_they_differ(self):
        rows = self.rows()
        rows[0]["debt_date"] = "2026-03-31"
        sheet = next(x for x in sa.tradeoffs(rows) if x.startswith("Balance sheet"))
        self.assertIn("TCS 0.10 at 31 Mar 2026", sheet)

    def test_one_offs_get_their_own_line(self):
        lines = sa.tradeoffs(self.rows())
        one = next(x for x in lines if x.startswith("One-offs"))
        self.assertIn("TCS: Dec 2025 quarter exceptional charges of ₹3,391 cr, other income net of them −₹2,273 cr", one)
        self.assertNotIn("INFY:", one)

    def test_trailing_one_offs_from_the_quarter_filings(self):
        recent = [{"quarter": "2026-09-30", "profit": 13884 * CR, "exceptional": 0.0, "other_income": 1337 * CR},
                  {"quarter": "2026-06-30", "profit": 13349 * CR, "exceptional": -668 * CR, "other_income": 1568 * CR},  # 5%: not a one-off
                  {"quarter": "2026-03-31", "profit": 13718 * CR, "exceptional": 0.0, "other_income": -1500 * CR},
                  {"quarter": "2025-12-31", "profit": 10657 * CR, "exceptional": -3391 * CR, "other_income": 1118 * CR}]
        price, shares = 2400.0, 3.6e9
        fund = {"eps": {"value": 140.0, "basis": "trailing 12 months"}, "pe": price / 140.0, "market_cap": price * shares}
        note = sa.trailing_one_offs(fund, {"recent": recent}, price)
        self.assertEqual([x["quarter"] for x in fund["one_offs"]], ["2026-03-31", "2025-12-31"])
        self.assertIn("Dec 2025 quarter: exceptional charges of ₹3,391 cr, other income net of them −₹2,273 cr", note)
        self.assertIn("Mar 2026 quarter: other income of −₹1,500 cr", note)
        adjusted = 140.0 + (3391 + 1500) * CR / shares
        self.assertIn(f"trailing P/E would be about {price / adjusted:.1f} instead of {price / 140.0:.1f}", note)
        self.assertIsNone(sa.trailing_one_offs({}, {"recent": recent[:2]}, price))


# ---------- 2. The newest balance sheet, and exceptional items in the latest quarter ----------

def tmpv_series():
    return {"annualTotalDebt": [{"date": "2026-03-31", "value": 81395 * CR}],
            "quarterlyTotalDebt": [{"date": "2026-03-31", "value": 81395 * CR}, {"date": "2026-09-30", "value": 88772 * CR}],
            "annualStockholdersEquity": [{"date": "2025-03-31", "value": 116144 * CR}, {"date": "2026-03-31", "value": 112068 * CR}],
            "quarterlyStockholdersEquity": [{"date": "2026-09-30", "value": 105126 * CR}],
            "annualOrdinarySharesNumber": [{"date": "2026-03-31", "value": 3.68e9}]}


class BalanceSheetTests(unittest.TestCase):
    def test_debt_to_equity_uses_the_half_year_balance_sheet_and_says_when(self):
        date_, debt, equity = sa.balance_sheet(tmpv_series())
        self.assertEqual(date_, "2026-09-30")
        f = sa.ratios(tmpv_series(), 253.0, NOW)
        self.assertAlmostEqual(f["debt_to_equity"], 88772 / 105126)
        self.assertEqual(f["debt_date"], "2026-09-30")
        self.assertEqual(sa.debt_words(f), "0.84 at 30 Sep 2026")
        old = tmpv_series()
        del old["quarterlyTotalDebt"]
        f = sa.ratios(old, 253.0, NOW)  # no September debt: the March sheet, with both figures from that date
        self.assertAlmostEqual(f["debt_to_equity"], 81395 / 112068)
        self.assertEqual(sa.debt_words(f), "0.73 at 31 Mar 2026")
        self.assertEqual(sa.debt_words({"bank_like": True}), "n/m (financial)")

    def test_summary_prints_the_balance_sheet_date(self):
        text = sa.summary(tsa.run(tsa.Feed(), peers=False))
        self.assertIn("debt/equity 0.50 at 31 Mar 2026", text)

    def test_results_xbrl_reads_the_balance_sheet_and_totals(self):
        body = results_xml("2026-07-01", "2026-09-30", {"RevenueFromOperations": 9692e8, "ProfitOrLossAttributableToOwnersOfParent": -5531 * CR,
                                                         "ProfitLossForPeriod": -5451 * CR, "ExceptionalItemsBeforeTax": -6738 * CR,
                                                         "ProfitBeforeExceptionalItemsAndTax": 374 * CR})
        body = body.replace(b"</xbrli:xbrl>", (
            b'<xbrli:context id="I"><xbrli:entity><xbrli:identifier scheme="s">1</xbrli:identifier></xbrli:entity>'
            b'<xbrli:period><xbrli:instant>2026-09-30</xbrli:instant></xbrli:period></xbrli:context>'
            b'<in:BorrowingsNoncurrent contextRef="I">743140000000</in:BorrowingsNoncurrent>'
            b'<in:BorrowingsCurrent contextRef="I">51790000000</in:BorrowingsCurrent>'
            b'<in:EquityAttributableToOwnersOfParent contextRef="I">1051260000000</in:EquityAttributableToOwnersOfParent>'
            b'<in:Equity contextRef="I">1118620000000</in:Equity></xbrli:xbrl>'))
        q = sa.results_xbrl(body)
        self.assertEqual(q["balance"], {"date": "2026-09-30", "borrowings": 79493 * CR, "equity": 105126 * CR, "format": "ind_as"})
        self.assertEqual((q["profit"], q["profit_total"], q["pbt_before_exceptional"]), (-5531 * CR, -5451 * CR, 374 * CR))
        self.assertIsNone(sa.results_xbrl(results_xml("2026-07-01", "2026-09-30", {"RevenueFromOperations": 1.0}))["balance"])

    def test_nse_balance_sheet_only_when_newer_than_yahoo(self):
        nse = {"latest": {"basis": "consolidated", "balance": {"date": "2026-09-30", "borrowings": 79493 * CR, "equity": 105126 * CR}}}
        fund = {"debt_to_equity": 0.73, "debt_date": "2026-03-31", "market_cap": 93198 * CR, "book_date": "2026-03-31", "pb": 0.83}
        self.assertEqual(sa.apply_balance_sheet(fund, nse), "2026-09-30")
        self.assertAlmostEqual(fund["debt_to_equity"], 79493 / 105126)
        self.assertAlmostEqual(fund["pb"], 93198 / 105126)
        self.assertIn("NSE consolidated results filing", fund["debt_basis"])
        same = {"debt_to_equity": 0.84, "debt_date": "2026-09-30"}
        self.assertIsNone(sa.apply_balance_sheet(same, nse))
        self.assertEqual(same["debt_to_equity"], 0.84)
        self.assertIsNone(sa.apply_balance_sheet({"bank_like": True}, nse))

    def test_exceptional_items_in_the_latest_quarter_are_named(self):
        fund = {"eps_flags": ["older flag"]}
        nse = {"latest": {"quarter": "2026-09-30", "profit": -5531 * CR, "exceptional": -6738 * CR, "pbt_before_exceptional": 374 * CR}}
        note = sa.flag_latest_exceptional(fund, nse)
        self.assertEqual(note, "The latest quarter's loss attributable to shareholders (₹5,531 cr, quarter to 2026-09-30) includes ₹6,738 cr "
                               "of exceptional charges; profit before exceptional items and tax was ₹374 cr; adding the charges back with "
                               "no tax effect, the profit would be about ₹1,207 cr.")
        self.assertEqual(fund["eps_flags"][0], note)  # first, so the summary always shows it
        small = {"latest": {"quarter": "2026-09-30", "profit": 13349 * CR, "exceptional": -668 * CR}}
        self.assertIsNone(sa.flag_latest_exceptional({}, small))
        # The trailing one-offs note does not repeat the latest quarter.
        fund2 = {"latest_exceptional": {"quarter": "2026-09-30"}}
        recent = [{"quarter": "2026-09-30", "profit": -5531 * CR, "exceptional": -6738 * CR, "other_income": 1281 * CR}]
        self.assertIsNone(sa.trailing_one_offs(fund2, {"recent": recent}, 250.0))
        self.assertEqual(len(fund2["one_offs"]), 1)

    def test_trailing_quarters_are_read_between_latest_and_year_ago(self):
        X = "https://nsearchives.nseindia.in/corporate/xbrl/INTEGRATED_FILING_INDAS_{}_WEB.xml"
        qe = ["30-SEP-2026", "30-JUN-2026", "31-MAR-2026", "31-DEC-2025", "30-SEP-2025"]
        listing = {"data": [{"qe_Date": q, "type": "Integrated Filing- Financials", "consolidated": "Consolidated", "xbrl": X.format(i),
                             "broadcast_Date": "x"} for i, q in enumerate(qe)]}
        ends = ["2026-09-30", "2026-06-30", "2026-03-31", "2025-12-31", "2025-09-30"]
        starts = ["2026-07-01", "2026-04-01", "2026-01-01", "2025-10-01", "2025-07-01"]
        files = {X.format(i): results_xml(s, e, {"RevenueFromOperations": 1e11, "ProfitOrLossAttributableToOwnersOfParent": 1e10,
                                                 "ExceptionalItemsBeforeTax": -3e9 if i == 3 else 0.0})
                 for i, (s, e) in enumerate(zip(starts, ends))}

        def fetch(url):
            return json.dumps(listing).encode() if "integrated-filing-results" in url else files[url]
        nse = sa.nse_quarter_results("TCS", fetch)
        self.assertEqual([q["quarter"] for q in nse["recent"]], ends[:4])
        self.assertEqual(nse["year_ago"]["quarter"], "2025-09-30")


# ---------- 3. Deeper P/E history from NSE annual filings ----------

LIST_URL = "https://www.nseindia.in/api/corporates-financial-results?index=equities&symbol=EXAMPLE&period=Annual"
OLD = "https://nsearchives.nseindia.in/corporate/xbrl/INDAS_{}_WEB.xml"


def annual_body(eps, revenue=4e11, quarter_revenue=1e11, context_dates=None):
    """An old-style annual XBRL: facts on 'OneD'/'FourD' with the contexts missing (FY2019-FY2022) or mis-dated."""
    ctx = ""
    if context_dates:
        ctx = (f'<xbrli:context id="FourD"><xbrli:entity><xbrli:identifier scheme="s">1</xbrli:identifier></xbrli:entity><xbrli:period>'
               f'<xbrli:startDate>{context_dates[0]}</xbrli:startDate><xbrli:endDate>{context_dates[1]}</xbrli:endDate></xbrli:period></xbrli:context>')
    return ('<?xml version="1.0"?><xbrli:xbrl xmlns:xbrli="http://www.xbrl.org/2003/instance" xmlns:f="http://x">' + ctx
            + f'<f:RevenueFromOperations contextRef="OneD">{quarter_revenue}</f:RevenueFromOperations>'
            + f'<f:RevenueFromOperations contextRef="FourD">{revenue}</f:RevenueFromOperations>'
            + '<f:DilutedEarningsLossPerShareFromContinuingAndDiscontinuedOperations contextRef="OneD">2.5</f:DilutedEarningsLossPerShareFromContinuingAndDiscontinuedOperations>'
            + f'<f:DilutedEarningsLossPerShareFromContinuingAndDiscontinuedOperations contextRef="FourD">{eps}</f:DilutedEarningsLossPerShareFromContinuingAndDiscontinuedOperations>'
            + '</xbrli:xbrl>').encode()


def listing_row(fy_end, n, basis="Consolidated", filed=None):
    return {"toDate": fy_end, "consolidated": basis, "period": "Annual", "xbrl": OLD.format(n),
            "broadCastDate": filed or ("20-May-" + fy_end[-4:] + " 18:00:00")}


class AnnualBackfillTests(unittest.TestCase):
    def test_annual_xbrl_reads_the_cumulative_column_by_name(self):
        y = sa.annual_xbrl(annual_body(63.29, context_dates=("2024-01-01", "2024-03-31")))  # mis-dated, as Infosys FY2024
        self.assertEqual(y["eps"], 63.29)
        self.assertEqual(y["revenue"], 4e11)
        self.assertEqual(sa.annual_xbrl(annual_body(38.91))["eps"], 38.91)  # contexts missing, as FY2019-FY2022 files
        with self.assertRaises(Block):
            sa.annual_xbrl(annual_body(10.0, revenue=1e11, quarter_revenue=1e11))  # 'year' no bigger than the quarter
        with self.assertRaises(Block):
            sa.annual_xbrl(b'<x:xbrl xmlns:x="y"></x:xbrl>')

    def test_filing_list_prefers_consolidated_and_the_latest_revision(self):
        rows = [listing_row("31-Mar-2024", 1), listing_row("31-Mar-2024", 2, filed="25-May-2024 10:00:00"),
                listing_row("31-Mar-2023", 3), listing_row("31-Mar-2022", 4, basis="Non-Consolidated"),
                listing_row("31-Mar-2022", 5, basis="Non-Consolidated"), listing_row("31-Mar-2021", 6, basis="Non-Consolidated"),
                listing_row("31-Mar-2021", 7), listing_row("31-Dec-2020", 8),
                # More standalone years than consolidated (Reliance filed FY2022 standalone only): consolidated still wins.
                listing_row("31-Mar-2020", 9, basis="Non-Consolidated"), listing_row("31-Mar-2019", 10, basis="Non-Consolidated"),
                {"toDate": "31-Mar-2017", "consolidated": "Consolidated", "xbrl": "https://nsearchives.nseindia.in/corporate/xbrl/-"}]
        got = sa.annual_filings("EXAMPLE", lambda url: json.dumps(rows).encode())
        self.assertEqual([(f["date"], f["url"]) for f in got], [("2024-03-31", OLD.format(2)), ("2023-03-31", OLD.format(3)),
                                                                ("2021-03-31", OLD.format(7))])
        self.assertTrue(all(f["basis"] == "consolidated" for f in got))

    def test_merge_adjusts_for_later_bonuses_and_checks_the_overlap(self):
        yahoo = [{"date": "2023-03-31", "value": 49.3}, {"date": "2024-03-31", "value": 51.45}, {"date": "2025-03-31", "value": 51.47}]
        filings = [{"date": "2021-03-31", "filed": "2021-04-30", "eps": 75.21, "basis": "consolidated"},
                   {"date": "2023-03-31", "filed": "2023-04-21", "eps": 98.59, "basis": "consolidated"},
                   {"date": "2024-03-31", "filed": "2024-04-22", "eps": 102.90, "basis": "consolidated"}]
        splits = [("2017-09-07", 2.0), ("2024-10-28", 2.0)]  # Reliance: bonuses in 2017 and 2024
        rows, note = sa.merge_annual_eps(yahoo, filings, splits)
        self.assertEqual(rows[0], {"date": "2021-03-31", "value": 75.21 / 2, "source": "NSE annual results"})
        self.assertEqual([r["date"] for r in rows], ["2021-03-31", "2023-03-31", "2024-03-31", "2025-03-31"])
        self.assertIn("annual EPS for FY2021-FY2021 from NSE consolidated annual results filings, adjusted for later splits and bonuses", note)
        self.assertIn("checked against Yahoo for FY2023, FY2024", note)
        self.assertIn("no consolidated figure for FY2022, so the sessions it would cover are left out", note)
        none, reason = sa.merge_annual_eps(yahoo, filings, [])  # without the 2024 bonus the filings read twice Yahoo's EPS
        self.assertIsNone(none)
        self.assertIn("does not match Yahoo's for FY2023, FY2024", reason)
        self.assertIsNone(sa.merge_annual_eps(yahoo, filings[:1], splits)[0])  # nothing to check against
        self.assertIsNone(sa.merge_annual_eps([], filings, splits)[0])

    def test_pe_band_leaves_a_missing_year_out(self):
        bars = [{"at": d.isoformat(), "close": 100.0} for d in tsa.sessions(1300)]
        eps = [{"date": "2021-03-31", "value": 10.0}, {"date": "2023-03-31", "value": 20.0}, {"date": "2024-03-31", "value": 20.0},
               {"date": "2025-03-31", "value": 20.0}, {"date": "2026-03-31", "value": 20.0}]
        stretched = screener.pe_band(bars, eps)
        gapped = screener.pe_band(bars, eps, max_age_days=430)
        self.assertLess(gapped["sessions"], stretched["sessions"])
        self.assertEqual(stretched["max"], gapped["max"])  # FY2021's EPS still counts in its own year

    def test_deep_band_reaches_five_years_with_medians(self):
        bars = [{"at": d.isoformat(), "close": 100.0 * 1.0004 ** k} for k, d in enumerate(tsa.sessions(2400))]
        yahoo = [{"date": f"{y}-03-31", "value": v} for y, v in ((2023, 4.0), (2024, 4.5), (2025, 5.0), (2026, 5.5))]
        filings = [{"date": f"{y}-03-31", "filed": f"{y}-05-01", "eps": v, "basis": "consolidated"}
                   for y, v in ((2018, 2.5), (2019, 2.8), (2020, 3.0), (2021, 3.3), (2022, 3.6), (2023, 4.0))]
        fund = {"pe_band": screener.pe_band(bars, yahoo)}
        self.assertEqual(fund["pe_band"]["start"][:7], "2023-05")
        band = sa.deep_pe_band(fund, {"filings": filings, "bars": bars, "splits": []}, yahoo)
        self.assertIs(fund["pe_band"], band)
        self.assertLess(band["start"], "2022-01-01")
        self.assertGreaterEqual(band["calendar_years"], 4.5)
        self.assertIsNotNone(band["median_5y"])
        self.assertLess(band["long"]["start"], "2019-01-01")
        self.assertIn("5-year median", sa.median_words(band))
        self.assertIn("median since", sa.median_words(band))
        self.assertIn("FY2018-FY2022 from NSE consolidated annual results filings", band["basis"])
        failed = {"pe_band": "kept"}
        self.assertIsNone(sa.deep_pe_band(failed, {"error": "NSE's annual results list was unavailable"}, yahoo))
        self.assertEqual(failed, {"pe_band": "kept", "pe_history_note": "NSE's annual results list was unavailable"})

    def test_allowlist_takes_the_annual_list_and_older_results_files(self):
        self.assertTrue(sh.allowed(LIST_URL))
        self.assertEqual(sh.api_url("/api/corporates-financial-results", "EXAMPLE"), LIST_URL)
        self.assertTrue(sh.allowed("https://nsearchives.nseindia.in/corporate/xbrl/INDAS_104589_1099938_19042024112830.xml"))
        self.assertTrue(sh.allowed("https://www.nseindia.in/api/corporate-sast-reg29?index=equities&symbol=ADANIPORTS"))
        for bad in ("https://www.nseindia.in/api/corporates-financial-results?index=equities&symbol=EXAMPLE",
                    "https://www.nseindia.in/api/corporates-financial-results?index=equities&symbol=EXAMPLE&period=Quarterly",
                    LIST_URL + "&page=2", "https://nsearchives.nseindia.in/corporate/xbrl/OTHER_1.xml"):
            self.assertFalse(sh.allowed(bad), bad)


class BackfillFeed(tsa.Feed):
    """The single-stock test feed plus NSE's annual-results list and two older filings."""
    def __init__(self, list_ok=True):
        super().__init__(closes=[100 * 1.0004 ** k for k in range(1300)],
                         fundamentals=tsa.fundamentals_payload(annualDilutedEPS=[("2023-03-31", 9.0), ("2024-03-31", 9.5), ("2025-03-31", 10.0),
                                                                                 ("2026-03-31", 11.0)]))
        self.files = {OLD.format(1): annual_body(8.0), OLD.format(2): annual_body(8.5), OLD.format(3): annual_body(9.0)}
        if list_ok:
            self.files[LIST_URL] = json.dumps([listing_row("31-Mar-2021", 1), listing_row("31-Mar-2022", 2), listing_row("31-Mar-2023", 3)]).encode()

    def __call__(self, url):
        self.urls.append(url)
        return self.files[url] if url in self.files else super().__call__(url)


class DeepHistoryReportTests(unittest.TestCase):
    def test_report_uses_the_back_filled_history(self):
        r = tsa.run(BackfillFeed(), peers=False)
        band = r["fundamentals"]["pe_band"]
        self.assertLess(band["start"], "2022-01-01")
        self.assertIsNotNone(band["median_5y"])
        text = sa.summary(r)
        self.assertIn(f"since {sa.month_year(band['start'])}", text)
        self.assertIn(f"5-year median {band['median_5y']:.1f}", text)
        self.assertIn("FY2021-FY2022 from NSE consolidated annual results filings", sa.markdown(r))

    def test_without_the_back_fill_the_band_keeps_its_start_date(self):
        r = tsa.run(BackfillFeed(list_ok=False), peers=False)
        f = r["fundamentals"]
        self.assertTrue(f["pe_band"]["start"].startswith("2023-05"))
        self.assertIn("since May 2023", sa.summary(r))
        self.assertIn("P/E history starts May 2023: older annual EPS could not be added (NSE's annual results list was unavailable).",
                      sa.markdown(r))

    def test_the_back_fill_can_be_switched_off(self):
        feed = BackfillFeed()
        tsa.run(feed, peers=False, pe_history=False)
        self.assertNotIn(LIST_URL, feed.urls)


# ---------- 4. Quote pages are not headlines ----------

class QuotePageTests(unittest.TestCase):
    def test_quote_page_titles_are_left_out(self):
        match = sa.headline_matcher("RELIANCE", "Reliance Industries Limited", aliases={}, demergers=[])
        for title in ("Reliance Industries Limited (RELIANCE.NS) stock price, news, quote and history",
                      "Reliance Industries (RELIANCE.BO) quote & history",
                      "RELIANCE.NS: Reliance Industries Limited (RELIANCE.NS) stock quote"):
            self.assertEqual(sa.headline_verdict(title, match), "tips", title)
        self.assertEqual(sa.headline_verdict("Reliance Industries Q2 profit rises 40%", match), "keep")


# ---------- 5. Lakh crore and Indian digit grouping ----------

class IndianNumberTests(unittest.TestCase):
    def test_grouping_and_lakh_crore(self):
        self.assertEqual(sa.indian(1837980), "18,37,980")
        self.assertEqual(sa.indian(999), "999")
        self.assertEqual(sa.indian(-123456789), "-12,34,56,789")
        self.assertEqual(sa.crore(1837980 * CR), "₹18.38 lakh cr")
        self.assertEqual(sa.crore(93401 * CR), "₹93,401 cr")
        self.assertEqual(sa.crore_words(178650 * CR), "₹1,78,650 cr")
        self.assertEqual(sa.crore(None), "n/a")
        self.assertEqual(screener.cell("market_cap_cr", 239114.0), "2,39,114")
        self.assertEqual(sh.indian(-2074, sign=True), "-2,074")
        self.assertEqual(sh.indian(1070249), "10,70,249")

    def test_summary_market_cap_in_lakh_crore(self):
        feed = tsa.Feed(fundamentals=tsa.fundamentals_payload(annualOrdinarySharesNumber=[("2026-03-31", 1.35e10)]))
        text = sa.summary(tsa.run(feed, peers=False))
        self.assertRegex(text, r"market cap ₹\d+\.\d\d lakh cr\.")
        self.assertNotRegex(text, r"₹\d{1,3},\d{3},\d{3}")


# ---------- 6. Profit attributable to shareholders, and total profit ----------

class ProfitLabelTests(unittest.TestCase):
    def test_label_and_total_profit(self):
        nse = {"latest": {"quarter": "2026-09-30", "profit": 25548 * CR, "profit_total": 30000 * CR, "revenue": 1.0, "basis": "consolidated"},
               "year_ago": {"quarter": "2025-09-30", "profit": 18165 * CR, "profit_total": 22422 * CR, "revenue": 1.0, "basis": "consolidated"}}
        fund = {"latest_quarter": {"date": "2026-06-30", "value": 1.0}}
        sa.apply_quarter_results(fund, nse)
        self.assertAlmostEqual(fund["quarter_profit_yoy"], 25548 / 18165 - 1)
        self.assertEqual(sa.total_profit_words(fund), f"; total profit incl. minority interests {sa.pct(30000 / 22422 - 1)}")
        same = {"quarter_profit_yoy": 0.1, "quarter_total_yoy": 0.101}
        self.assertEqual(sa.total_profit_words(same), "")
        swing = {"quarter_profit_values": (-5531.0, 76170.0), "quarter_total_values": (-5451.0, 76248.0),
                 "quarter_total_yoy_text": "swung from ₹76,248 cr profit to ₹5,451 cr loss"}
        self.assertEqual(sa.total_profit_words(swing), "")  # the same swing in other words: not repeated
        swing["quarter_total_values"] = (100.0, 76248.0)
        swing["quarter_total_yoy_text"] = "fell"
        self.assertEqual(sa.total_profit_words(swing), "; total profit incl. minority interests fell")

    def test_summary_names_profit_attributable_to_shareholders(self):
        text = sa.summary(tsa.run(tsa.Feed(), peers=False))
        self.assertIn("latest quarter profit attributable to shareholders", text)


# ---------- 7. Large promoter moves point to the filings ----------

SAST = "https://www.nseindia.in/api/corporate-sast-reg29?index=equities&symbol=ADANIPORTS"
SAST_ROWS = {"data": [
    {"acqSaleType": "Acquisition", "acquirerName": "Carmichael Rail and Port Singapore HOlding Pvt Ltd", "promoterType": "Y",
     "acquisitionMode": "Open Market", "totAcqShare": "6.24", "acquirerDate": "11-SEP-2023 to 23-DEC-2025", "timestamp": "29-Dec-2025 11:02",
     "attachement": "https://nsearchives.nseindia.in/corporate/x.zip"},
    {"acqSaleType": "Sale", "acquirerName": "GQG Partners LLC", "promoterType": "N", "acquisitionMode": "Open Market",
     "totSaleShare": "0.43", "acquirerDate": "17-AUG-2023 to 09-OCT-2025", "timestamp": "28-Oct-2025 14:19"},
    {"acqSaleType": "Acquisition", "acquirerName": "Old Promoter Entity", "promoterType": "Y", "totAcqShare": "1.0",
     "acquirerDate": "-", "timestamp": "05-Jan-2026 10:00"}]}


class PromoterMoveTests(unittest.TestCase):
    def test_moves_over_one_point_are_found(self):
        rows = [{"quarter": "2026-09-30", "promoter_pct": 66.03}, {"quarter": "2026-06-30", "promoter_pct": 66.03},
                {"quarter": "2026-03-31", "promoter_pct": 68.02}, {"quarter": "2025-12-31", "promoter_pct": 68.02},
                {"quarter": "2025-09-30", "promoter_pct": 65.89}, {"quarter": "2025-06-30", "promoter_pct": 65.5}]
        moves = sh.promoter_moves(rows)
        self.assertEqual([(m["quarter"], round(m["pts"], 2)) for m in moves], [("2026-06-30", -1.99), ("2025-12-31", 2.13)])

    def test_sast_filings_in_the_quarter_are_named(self):
        sast = sh.sast_filings(json.dumps(SAST_ROWS).encode())
        self.assertEqual(sast[0]["pct"], 6.24)
        self.assertEqual(sast[1]["type"], "sale")
        hits = sh.filings_in_window(sast, "2025-12-31")
        self.assertEqual([h["acquirer"] for h in hits], ["Carmichael Rail and Port Singapore HOlding Pvt Ltd", "Old Promoter Entity"])
        self.assertEqual(sh.filings_in_window(sast, "2026-06-30"), [])
        self.assertEqual(sh.quarter_start(sh.date.fromisoformat("2026-03-31")).isoformat(), "2026-01-01")
        r = {"promoter_moves": [{"quarter": "2025-12-31", "pts": 2.13, "filings": hits},
                                {"quarter": "2026-06-30", "pts": -1.99, "filings": []},
                                {"quarter": "2025-03-31", "pts": 1.5, "filings": None}]}
        found, empty, unknown = sh.move_lines(r)
        self.assertIn("Promoter stake +2.13 pts in the Dec 2025 quarter: NSE SAST (Reg. 29) filings by the promoter group in that window: "
                      "Carmichael Rail and Port Singapore Holding PVT LTD acquired 6.24% (mode as filed: open market; filed 29 Dec 2025)", found)
        self.assertIn("Check NSE's preferential-allotment and block-deal filings for Oct–Dec 2025 too.", found)
        self.assertIn("Promoter stake -1.99 pts in the Jun 2026 quarter: no promoter-group SAST (Reg. 29) filing on NSE in that window, "
                      "and none was required for a net change of this size", empty)
        self.assertIn("; check NSE's SAST, preferential-allotment and block-deal filings for Apr–Jun 2026.", empty)
        self.assertIn("check NSE's SAST, preferential-allotment and block-deal filings for Jan–Mar 2025.", unknown)

    def test_report_matches_moves_to_filings_and_stays_short(self):
        feed = tsh.Round2ShareholdingTests.feed(None)
        feed.files[SAST] = json.dumps(SAST_ROWS).encode()
        r = tsh.report(feed)
        dec = next(m for m in r["promoter_moves"] if m["quarter"] == "2025-12-31")
        self.assertEqual(dec["filings"][0]["acquirer"], "Carmichael Rail and Port Singapore HOlding Pvt Ltd")
        text = sh.summary(r)
        self.assertIn("Promoter stake +2.13 pts in the Dec 2025 quarter: NSE SAST (Reg. 29) filings", text)
        self.assertLessEqual(len(text.splitlines()), 8)  # every promoter entity is named on one line
        self.assertIn("- Promoter stake +2.13 pts", sh.markdown(r))
        quiet = tsh.Round2ShareholdingTests.feed(None)  # SAST data unavailable: still a pointer, no data-gap noise
        text = sh.summary(tsh.report(quiet))
        self.assertIn("check NSE's SAST, preferential-allotment and block-deal filings for Oct–Dec 2025", text)
        self.assertNotIn("Data gaps", text)

    def test_no_sast_request_without_a_large_move(self):
        feed = tsh.Round2ShareholdingTests.feed(None)
        r = sh.shareholding("adaniports", fetcher=feed, clock=lambda: NOW, trend=2)
        self.assertEqual(r["promoter_moves"], [])
        self.assertNotIn(SAST, feed.urls)


# ---------- 8. Dividend streaks across a data gap ----------

ACTIONS = "https://www.nseindia.in/api/corporates-corporateActions?index=equities&symbol=EXAMPLE"


class StreakGapTests(unittest.TestCase):
    def chart(self, pays):
        return tsa.DividendRecordTests.chart(None, pays)

    def pays(self):
        return {y: 10.0 for y in range(2017, 2027) if y != 2020}

    def test_gap_filled_by_nse_keeps_the_streak(self):
        raw = self.chart(self.pays())
        nse = json.dumps([{"exDate": "14-Aug-2019", "subject": "Interim Dividend - Rs 16.50 Per Share"},
                          {"exDate": "01-Aug-2016", "subject": "Dividend - Rs 5 Per Share"}]).encode()
        d = sa.dividend_record("EXAMPLE.NS", fetcher=lambda url: raw, now=NOW, nse_fetcher=lambda url: nse)
        self.assertEqual(d["years_paid_in_a_row"], 9)
        self.assertTrue(d["streak_resolved_from_nse"])
        self.assertIn("FY2019-20 dividend missing from Yahoo's history; NSE's corporate actions show one", d["streak_note"])

    def test_unresolved_gap_is_marked(self):
        raw = self.chart(self.pays())
        nse = json.dumps([{"exDate": "19-May-2020", "subject": "Interim Dividend - Rs 16.5 Per Share"},
                          {"exDate": "31-Oct-2018", "subject": "Interim Dividend - Rs 20 Per Share"}]).encode()
        d = sa.dividend_record("EXAMPLE.NS", fetcher=lambda url: raw, now=NOW, nse_fetcher=lambda url: nse)
        self.assertEqual(d["years_paid_in_a_row"], 6)
        self.assertEqual(d["streak_note"], "streak may be understated (data gap FY2019-20: no dividend ex-date that year on Yahoo or NSE, "
                                           "though the years either side paid)")
        d = sa.dividend_record("EXAMPLE.NS", fetcher=lambda url: raw, now=NOW)
        self.assertIn("no dividend ex-date that year on Yahoo,", d["streak_note"])
        full = self.chart({y: 10.0 for y in range(2017, 2027)})
        self.assertIsNone(sa.dividend_record("EXAMPLE.NS", fetcher=lambda url: full, now=NOW)["streak_note"])
        two = self.chart({y: 10.0 for y in range(2017, 2027) if y not in (2020, 2021)})  # two empty years: a real break
        self.assertIsNone(sa.dividend_record("EXAMPLE.NS", fetcher=lambda url: two, now=NOW)["streak_note"])

    def test_screen_marks_the_streak(self):
        r = {"universe": "custom", "as_of": NOW.isoformat(), "filters": ["dividend_years>=5"], "sort": None, "evaluated": 1, "requested": 1,
             "matched": 1, "truncated": False, "missing_data": [], "failed": [], "note": "n",
             "results": [{"symbol": "HINDZINC.NS", "name": "Hindustan Zinc Limited", "price_date": "2026-11-13", "dividend_years": 7,
                          "streak_note": "streak may be understated (data gap FY2019-20)"}]}
        text = screener.markdown(r, columns=["dividend_years"])
        self.assertIn("| Hindustan Zinc Limited (HINDZINC) ‡ | 7 |", text)
        self.assertIn("‡ Years paid in a row: HINDZINC: streak may be understated (data gap FY2019-20).", text)


if __name__ == "__main__":
    unittest.main()
