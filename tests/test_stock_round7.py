"""Round 7 (held-out review): bank fundamentals from NSE filings, split-safe annual EPS and P/E-history guards, confirmed
revenue growth, NSE share counts, P/B history and one-offs for lenders, business peers, shareholding encumbrance and
screener debt precision. Offline: every feed is synthetic."""
import json
from pathlib import Path
import sys
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT / "tests"))
from core import Block
import screener
import shareholding as sh
import stock_analysis as sa
import test_shareholding as tsh
from test_stock_round3 import results_xml

CR = 1e7


def bank_xml(quarter_end, year_start=None, facts=None, year=None, balance=None):
    """A bank-format results XBRL: quarter facts on 'Q', optional full-year facts on 'Y', balance sheet on 'I'."""
    q_start = {"03-31": "01-01", "06-30": "04-01", "09-30": "07-01", "12-31": "10-01"}[quarter_end[5:]]
    ctx = lambda cid, period: (f'<xbrli:context id="{cid}"><xbrli:entity><xbrli:identifier scheme="s">1</xbrli:identifier></xbrli:entity>'
                               f'<xbrli:period>{period}</xbrli:period></xbrli:context>')
    parts = ['<?xml version="1.0"?><xbrli:xbrl xmlns:xbrli="http://www.xbrl.org/2003/instance" xmlns:in="http://x">',
             ctx("Q", f"<xbrli:startDate>{quarter_end[:5]}{q_start}</xbrli:startDate><xbrli:endDate>{quarter_end}</xbrli:endDate>"),
             ctx("I", f"<xbrli:instant>{quarter_end}</xbrli:instant>")]
    if year_start:
        parts.append(ctx("Y", f"<xbrli:startDate>{year_start}</xbrli:startDate><xbrli:endDate>{quarter_end}</xbrli:endDate>"))
    parts += [f'<in:{k} contextRef="Q">{v}</in:{k}>' for k, v in (facts or {}).items()]
    parts += [f'<in:{k} contextRef="Y">{v}</in:{k}>' for k, v in (year or {}).items()]
    parts += [f'<in:{k} contextRef="I">{v}</in:{k}>' for k, v in (balance or {}).items()]
    return ("".join(parts) + "</xbrli:xbrl>").encode()


def bank_facts(profit, eps, exceptional=0.0, income=1000 * CR):
    return {"Income": income, "ProfitLossForThePeriod": profit, "ProfitLossAfterTaxesMinorityInterestAndShareOfProfitLossOfAssociates": profit,
            "DilutedEarningsPerShareAfterExtraordinaryItems": eps, "ExceptionalItems": exceptional, "OtherIncome": 100 * CR}


# ---------- 1. Bank filings: profit, EPS and the balance sheet ----------

class BankFilingTests(unittest.TestCase):
    def test_bank_results_give_profit_eps_and_equity(self):
        body = bank_xml("2026-03-31", "2025-04-01",
                        {**bank_facts(20351 * CR, 13.18), "ProfitLossForThePeriod": 20000 * CR},
                        balance={"Capital": 1539 * CR, "ReservesAndSurplus": 579975 * CR, "Borrowings": 588484 * CR, "Deposits": 3099638 * CR})
        q = sa.results_xbrl(body)
        self.assertEqual(q["profit"], 20351 * CR)  # attributable to shareholders, not the period's total
        self.assertEqual(q["eps"], 13.18)
        self.assertEqual(q["balance"], {"date": "2026-03-31", "borrowings": 588484 * CR, "equity": 581514 * CR, "format": "bank"})

    def test_nbfc_and_ind_as_balance_sheets(self):
        nbfc = sa.balance_from({"EquityAttributableToOwnersOfParent": 123180 * CR, "DebtSecurities": 244831 * CR, "Borrowings": 186672 * CR,
                                "SubordinatedLiabilities": 725 * CR, "Deposits": 69754 * CR}, "2026-09-30")
        self.assertEqual(nbfc["format"], "nbfc")
        self.assertAlmostEqual(nbfc["borrowings"], (244831 + 186672 + 725) * CR)  # deposits are not debt
        bare = sa.balance_from({"EquityAttributableToOwnersOfParent": 5.0}, "2026-09-30")
        self.assertEqual((bare["equity"], bare["borrowings"], bare["format"]), (5.0, None, "ind_as"))
        self.assertIsNone(sa.balance_from({"Deposits": 1.0}, "2026-09-30"))
        # Only an Ind AS balance sheet with borrowings replaces Yahoo's debt/equity.
        fund = {"debt_date": "2026-03-31", "market_cap": 1e12}
        for sheet in (nbfc, bare, {**nbfc, "format": "bank"}):
            self.assertIsNone(sa.apply_balance_sheet(dict(fund), {"latest": {"balance": sheet}}))

    def test_old_bank_annual_filing_gives_its_eps(self):
        body = ('<?xml version="1.0"?><xbrli:xbrl xmlns:xbrli="http://www.xbrl.org/2003/instance" xmlns:f="http://x">'
                '<f:Income contextRef="OneD">571588400000</f:Income><f:Income contextRef="FourD">2046661000000</f:Income>'
                '<f:DilutedEarningsPerShareAfterExtraordinaryItems contextRef="OneD">22.46</f:DilutedEarningsPerShareAfterExtraordinaryItems>'
                '<f:DilutedEarningsPerShareAfterExtraordinaryItems contextRef="FourD">82.27</f:DilutedEarningsPerShareAfterExtraordinaryItems>'
                '<f:ProfitLossAfterTaxesMinorityInterestAndShareOfProfitLossOfAssociates contextRef="FourD">459971100000</f:ProfitLossAfterTaxesMinorityInterestAndShareOfProfitLossOfAssociates>'
                '</xbrli:xbrl>').encode()
        y = sa.annual_xbrl(body)
        self.assertEqual((y["eps"], y["profit"]), (82.27, 459971100000))

    def test_backfill_says_unreadable_rather_than_timed_out(self):
        listing = json.dumps([{"toDate": "31-Mar-2024", "consolidated": "Consolidated", "period": "Annual",
                               "xbrl": "https://nsearchives.nseindia.in/corporate/xbrl/BANKING_1.xml", "broadCastDate": "20-Apr-2024 18:00:00"}]).encode()
        files = {sa.NSE_ANNUAL_LIST.format("ALPHA"): listing,
                 "https://nsearchives.nseindia.in/corporate/xbrl/BANKING_1.xml": results_xml("2023-04-01", "2024-03-31", {"Income": 1.0})}

        def fetch(url):
            if url not in files:
                raise Block("PUBLIC: source unavailable")
            return files[url]
        out = sa.load_backfill("ALPHA", "ALPHA.NS", [{"at": "2026-11-13", "close": 1.0}], nse_fetcher=fetch, fetcher=fetch, now=None)
        self.assertEqual(out, {"error": "NSE's annual results filings gave no usable EPS"})


# ---------- 2. Annual results: the consolidated year before on page 2 ----------

RESULTS = "https://www.nseindia.in/api/integrated-filing-results?index=equities&symbol=ALPHA"
X = "https://nsearchives.nseindia.in/corporate/xbrl/INTEGRATED_FILING_INDAS_{}_WEB.xml"


def filing(qe, n, basis="Consolidated"):
    return {"qe_Date": qe, "type": "Integrated Filing- Financials", "consolidated": basis, "xbrl": X.format(n), "broadcast_Date": "x"}


class AnnualPageTests(unittest.TestCase):
    def test_consolidated_march_on_page_two_is_read_although_page_one_has_it_standalone(self):
        page1 = {"data": [filing("31-MAR-2026", 1), filing("31-MAR-2025", 2, "Standalone")], "totalCount": 40}
        page2 = {"data": [filing("31-MAR-2025", 3)], "totalCount": 40}
        files = {RESULTS: json.dumps(page1).encode(), RESULTS + "&page=2": json.dumps(page2).encode(),
                 X.format(1): results_xml("2025-04-01", "2026-03-31", {"RevenueFromOperations": 54364 * CR, "ProfitOrLossAttributableToOwnersOfParent": 354 * CR}),
                 X.format(2): results_xml("2024-04-01", "2025-03-31", {"RevenueFromOperations": 1.0}),
                 X.format(3): results_xml("2024-04-01", "2025-03-31", {"RevenueFromOperations": 20243 * CR, "ProfitOrLossAttributableToOwnersOfParent": 527 * CR})}
        nse = sa.nse_quarter_results("ALPHA", lambda url: files[url], details=False)
        years = sa.nse_annual_results(nse, lambda url: files[url])
        self.assertEqual(sorted(years), ["2025-03-31", "2026-03-31"])
        self.assertEqual(years["2025-03-31"]["revenue"], 20243 * CR)


# ---------- 3. Revenue growth confirmed by the filings ----------

def revenue_series(newest, before):
    return {"annualTotalRevenue": [{"date": "2025-03-31", "value": before}, {"date": "2026-03-31", "value": newest}]}


class RevenueConfirmTests(unittest.TestCase):
    def years(self, newest=54364 * CR, before=20243 * CR):
        return {"2026-03-31": {"revenue": newest, "profit": 354 * CR, "basis": "consolidated"},
                "2025-03-31": {"revenue": before, "profit": 527 * CR, "basis": "consolidated"}}

    def test_a_real_jump_is_shown_from_the_filings(self):
        fund = {"revenue_growth": 54364 / 20243 - 1, "growth_period": "FY2026 vs FY2025", "net_margin": 0.0065, "eps_flags": []}
        note = sa.check_revenue(fund, revenue_series(54364 * CR, 20243 * CR), {}, self.years())
        self.assertTrue(fund["revenue_check"]["confirmed"])
        self.assertAlmostEqual(fund["revenue_growth"], 54364 / 20243 - 1)
        self.assertEqual(fund["net_margin"], 0.0065)  # not replaced
        self.assertEqual(fund["eps_flags"], [])  # no ⚠: the growth is real
        self.assertIn("confirmed by NSE's consolidated annual results (₹20,243 cr to ₹54,364 cr): real growth", note)

    def test_without_the_year_before_yahoos_growth_stands(self):
        fund = {"revenue_growth": 1.686, "growth_period": "FY2026 vs FY2025", "eps_flags": []}
        sa.check_revenue(fund, revenue_series(54364 * CR, 20243 * CR), {}, {"2026-03-31": self.years()["2026-03-31"]})
        self.assertEqual(fund["revenue_growth"], 1.686)
        self.assertIn("could not be read to confirm the base", fund["revenue_note"])

    def test_a_mismatched_latest_year_is_still_replaced(self):
        fund = {"revenue_growth": 1.686, "growth_period": "FY2026 vs FY2025", "eps_flags": []}
        sa.check_revenue(fund, revenue_series(54364 * CR, 20243 * CR), {}, self.years(newest=50000 * CR))
        self.assertNotIn("confirmed", fund["revenue_check"])
        self.assertAlmostEqual(fund["revenue_growth"], 50000 / 20243 - 1)
        self.assertIn("does not match the company's filings", fund["eps_flags"][0])


# ---------- 4. Annual EPS on today's share basis, and when a P/E history is not shown ----------

HDFC_SPLITS = [("2025-08-26", 2.0)]


def hdfc_rows():
    eps = [("2023-03-31", 88.68), ("2024-03-31", 44.16), ("2025-03-31", 44.82), ("2026-03-31", 45.75)]
    ni = [("2023-03-31", 49544.69), ("2024-03-31", 62265.66), ("2025-03-31", 67350.83), ("2026-03-31", 70479.34)]
    shares = [("2023-03-31", 1115.95), ("2024-03-31", 1519.38), ("2025-03-31", 1530.44), ("2026-03-31", 1539.33)]
    rows = lambda pairs, k: [{"date": d, "value": v * k} for d, v in pairs]
    return rows(eps, 1), rows(ni, CR), rows(shares, CR)


class RestateTests(unittest.TestCase):
    def test_an_unrestated_year_is_put_on_todays_basis(self):
        eps, ni, shares = hdfc_rows()
        rows, fixed = sa.restate_annual_eps(eps, ni, shares, 1539.33 * CR, HDFC_SPLITS)
        self.assertEqual(fixed, ["2023-03-31"])
        self.assertAlmostEqual(rows[0]["value"], 44.34)
        self.assertEqual([r["value"] for r in rows[1:]], [44.16, 44.82, 45.75])  # already restated: untouched
        self.assertEqual(sa.restate_annual_eps(eps, ni, shares, 1539.33 * CR, []), (eps, []))

    def test_ratios_use_the_restated_series(self):
        eps, ni, shares = hdfc_rows()
        series = {"annualDilutedEPS": eps, "annualNetIncome": ni, "annualOrdinarySharesNumber": shares,
                  "annualTotalRevenue": [{"date": r["date"], "value": 1e12} for r in eps]}
        f = sa.ratios(series, 808.15, sa.instant("2026-11-18T16:00:00+05:30"), HDFC_SPLITS)
        self.assertEqual(f["annual_eps_restated"], ["2023-03-31"])
        self.assertAlmostEqual(series["annualDilutedEPS"][0]["value"], 44.34)
        self.assertEqual(f["shares_date"], "2026-03-31")

    def test_band_problems(self):
        eps, ni, _ = hdfc_rows()
        band = {"current": 17.7, "p25": 17.3, "median": 19.4, "p75": 21.4}
        self.assertIsNone(sa.band_problem(band, [{**eps[0], "value": 44.34}] + eps[1:], ni, {"net_margin": 0.15, "pe": 17.7}))
        self.assertIn("a break in the EPS history (annual EPS ×0.50 from FY2023 to FY2024 while net profit moved ×1.26)",
                      sa.band_problem(band, eps, ni, {"net_margin": 0.15}))
        broken = {"current": 17.7, "p25": 9.4, "median": 18.3, "p75": 20.7}
        self.assertIn("25th percentile 9.4 against a median of 18.3", sa.band_problem(broken, [], [], {}))
        self.assertIn("net margin 0.7%", sa.band_problem(band, [], [], {"net_margin": 0.007, "pe": 40}))
        self.assertIn("P/E 704", sa.band_problem(band, [], [], {"net_margin": 0.05, "pe": 703.8}))
        self.assertIsNone(sa.band_problem(None, eps, ni, {"net_margin": 0.001}))
        # Edges: exactly at the limits is still shown.
        self.assertIsNone(sa.band_problem(band, [], [], {"net_margin": sa.LOW_MARGIN, "pe": sa.HIGH_PE}))
        self.assertIsNone(sa.band_problem({**band, "p25": sa.BAND_BREAK * band["median"]}, [], [], {}))

    def test_summary_says_why_there_is_no_history_and_lists_the_gap(self):
        r = {"fundamentals": {"pe_history_note": "NSE's annual results list was unavailable"},
             "errors": {"pe_history": "P/E history starts ..."}}
        self.assertEqual(sa.gap_label("pe_history", r), "P/E history (NSE's annual results list was unavailable)")
        r["fundamentals"]["pe_band_dropped"] = "earnings too thin for a P/E history (net margin 0.7%, P/E 704): small changes"
        self.assertEqual(sa.gap_label("pe_history", r), "P/E history (earnings too thin for a P/E history (net margin 0.7%, P/E 704))")


# ---------- 5. Yahoo's statements against the NSE filings ----------

def hdfc_nse():
    q = lambda d, p, e, eq=None: {"quarter": d, "profit": p * CR, "eps": e, "basis": "consolidated",
                                  "balance": {"date": d, "equity": eq * CR, "borrowings": None, "format": "bank"} if eq else None}
    recent = [q("2026-09-30", 20700, 13.41, 603469), q("2026-06-30", 19245, 12.47), q("2026-03-31", 20351, 13.18, 581514), q("2025-12-31", 19807, 12.82)]
    return {"latest": {**recent[0]}, "recent": recent, "quarters": recent + [q("2025-09-30", 19611, 12.71, 539144)]}


def hdfc_years():
    return {"2026-03-31": {"profit": 76026 * CR, "revenue": 495463 * CR, "eps": 49.28, "basis": "consolidated",
                           "balance": {"date": "2026-03-31", "equity": 581514 * CR}},
            "2025-03-31": {"profit": 70792 * CR, "revenue": 470000 * CR, "eps": 92.40, "basis": "consolidated",
                           "balance": {"date": "2025-03-31", "equity": 500000 * CR}}}


def hdfc_series():
    eps, ni, shares = hdfc_rows()
    return {"annualNetIncome": ni, "annualDilutedEPS": [{**eps[0], "value": 44.34}] + eps[1:], "annualOrdinarySharesNumber": shares,
            "annualStockholdersEquity": [{"date": "2025-03-31", "value": 767689 * CR}, {"date": "2026-03-31", "value": 816740 * CR}]}


class StatementCheckTests(unittest.TestCase):
    def fund(self):
        return {"roe": 0.089, "pb": 1.52, "pe": 17.7, "market_cap": 12.44e12, "eps": {"date": "2026-03-31", "value": 45.75, "basis": "last financial year"},
                "eps_growth": 0.021, "roe_basis": "last financial year's profit ÷ average equity", "eps_flags": []}

    def test_hdfc_bank_ratios_come_from_the_filings(self):
        fund, series = self.fund(), hdfc_series()
        note = sa.cross_check_statements(fund, series, hdfc_nse(), hdfc_years(), 808.15, HDFC_SPLITS)
        self.assertAlmostEqual(fund["roe"], 76026 / ((581514 + 500000) / 2))
        self.assertAlmostEqual(fund["pb"], 12.44e12 / (603469 * CR))
        self.assertEqual(fund["book_date"], "2026-09-30")
        self.assertAlmostEqual(fund["pe"], 808.15 / (13.41 + 12.47 + 13.18 + 12.82))
        self.assertEqual(fund["eps"]["basis"], "trailing 12 months, NSE results")
        self.assertAlmostEqual(fund["net_margin"], 76026 / 495463)
        # The filings' EPS on Yahoo's basis: FY2025's pre-bonus ₹92.40 halved.
        self.assertAlmostEqual(series["annualDilutedEPS"][-2]["value"], 46.20)
        self.assertAlmostEqual(fund["eps_growth"], 49.28 / 46.20 - 1)
        self.assertEqual([c["item"] for c in fund["statement_check"]["checks"]], ["equity", "profit"])
        self.assertIn("shareholders' equity at 31 Mar 2026 ₹8,16,740 cr on Yahoo against ₹5,81,514 cr filed", note)
        self.assertIn("(Yahoo's figures gave ROE 8.9%, P/B 1.52, P/E 17.7)", note)
        self.assertEqual(fund["eps_flags"][0], note)

    def test_figures_that_agree_change_nothing(self):
        fund, series = self.fund(), hdfc_series()
        series["annualNetIncome"][-1]["value"] = 76026 * CR * 1.05  # 5%: inside the 10% limit, and no other item is off
        series["annualStockholdersEquity"][-1]["value"] = 581514 * CR * 1.09
        self.assertIsNone(sa.cross_check_statements(fund, series, hdfc_nse(), hdfc_years(), 808.15, HDFC_SPLITS))
        self.assertEqual((fund["roe"], fund["pb"], fund["pe"]), (0.089, 1.52, 17.7))

    def test_trailing_sum_needs_four_consecutive_quarters_on_one_share_basis(self):
        nse = hdfc_nse()
        self.assertAlmostEqual(sa.nse_trailing(nse)["profit"], (20700 + 19245 + 20351 + 19807) * CR)
        split = hdfc_nse()
        split["recent"][3] = {**split["recent"][3], "eps": 25.64}  # a pre-bonus quarter
        self.assertIsNone(sa.nse_trailing(split)["eps"])
        short = hdfc_nse()
        short["recent"] = short["recent"][:3]
        self.assertIsNone(sa.nse_trailing(short))


# ---------- 6. Share count from the NSE shareholding pattern ----------

class ShareCountTests(unittest.TestCase):
    def test_market_cap_on_the_newer_nse_count(self):
        fund = {"market_cap": 366.0 * 920.8 * CR, "pb": 10.78, "fcf_yield": 0.01, "shares_date": "2026-03-31"}
        note = sa.apply_share_count(fund, {"shares": 965.0 * CR, "date": "2026-09-30"}, 366.0)
        self.assertAlmostEqual(fund["market_cap"], 366.0 * 965.0 * CR)
        self.assertAlmostEqual(fund["pb"], 10.78 * 965.0 / 920.8)
        self.assertAlmostEqual(fund["fcf_yield"], 0.01 * 920.8 / 965.0)
        self.assertIn("Market cap uses the 965.00 cr shares in NSE's shareholding pattern for 30 Sep 2026 (₹3.53 lakh cr)", note)
        self.assertIn("gave ₹3.37 lakh cr", note)

    def test_small_or_older_differences_keep_yahoos_count(self):
        base = {"market_cap": 100.0 * 622.6 * CR, "pb": 5.5, "shares_date": "2026-09-30"}
        for count in ({"shares": 622.6 * CR * 1.009, "date": "2026-09-30"}, {"shares": 700 * CR, "date": "2026-06-30"}, None, TypeError("x")):
            fund = dict(base)
            self.assertIsNone(sa.apply_share_count(fund, count, 100.0))
            self.assertEqual(fund, base)


# ---------- 7. Lenders: P/B history and one-offs in the year before ----------

def bars(n=400, close=100.0, start="2025-03-01"):
    from datetime import date, timedelta
    d0 = date.fromisoformat(start)
    return [{"at": (d0 + timedelta(days=i)).isoformat(), "close": close} for i in range(n)]


class LenderTests(unittest.TestCase):
    def test_pb_band(self):
        b = bars()
        band = sa.pb_band(b, {"2025-03-31": 50.0, "2025-09-30": 40.0}, today_book=50.0)
        self.assertEqual(band["start"], "2025-05-15")  # 45 days after the first balance sheet
        self.assertAlmostEqual(band["current"], 2.0)
        self.assertAlmostEqual(band["max"], 2.5)
        self.assertAlmostEqual(band["min"], 2.0)
        self.assertAlmostEqual(band["percentile"], sum(1 for x in b if "2025-05-15" <= x["at"] < "2025-11-14") / band["sessions"], delta=0.01)
        self.assertIsNone(sa.pb_band(b[:100], {"2025-03-31": 50.0}))

    def test_book_points_skip_yahoo_when_its_equity_failed(self):
        series = hdfc_series()
        pts = sa.book_points(series, hdfc_nse(), hdfc_years(), trust_yahoo=False)
        self.assertEqual(sorted(pts), ["2025-03-31", "2025-09-30", "2026-03-31", "2026-09-30"])
        self.assertAlmostEqual(pts["2026-03-31"], 581514 * CR / (1539.33 * CR))
        self.assertAlmostEqual(sa.book_points(series, hdfc_nse(), hdfc_years())["2025-03-31"], 500000 / 1530.44)  # NSE first

    def test_kotak_one_off_in_the_year_before_is_named(self):
        q = lambda d, p, e, exc=0.0: {"quarter": d, "profit": p * CR, "eps": e, "exceptional": exc * CR, "pbt": p * CR * 1.33,
                                      "tax": p * CR * 0.33, "other_income": 2500 * CR, "basis": "consolidated"}
        quarters = [q("2026-09-30", 5645, 5.67), q("2026-06-30", 5480, 5.51), q("2026-03-31", 5423, 5.45, 368),
                    q("2025-12-31", 4924, 4.95), q("2025-09-30", 4468, 22.47), q("2025-06-30", 4472, 22.49)]
        self.assertAlmostEqual(sa._shares_from(quarters), 5645 * CR / 5.67, delta=2 * CR)  # newest basis, not a blend
        series = {"annualDilutedEPS": [{"date": "2025-03-31", "value": 22.26}, {"date": "2026-03-31", "value": 19.39}]}
        fund = {"eps_growth": 19.39 / 22.26 - 1, "bank_like": True, "other_income_spikes": [], "eps_flags": []}
        annual = {"2025-03-31": {"exceptional": 3803 * CR, "profit": 22126 * CR, "eps": 111.29},
                  "2026-03-31": {"exceptional": 368 * CR, "profit": 19288 * CR, "eps": 19.39}}
        note = sa.annual_one_offs(fund, {"quarters": quarters}, series, annual=lambda: annual)
        out = fund["annual_one_off"]
        self.assertEqual(out["items"], [])
        self.assertEqual(out["previous_items"][0]["amount"], 3803 * CR)
        self.assertGreater(out["growth_adjusted"], -0.03)
        self.assertIn("FY2026 EPS growth is measured against a FY2025 that included one-offs: exceptional gains of ₹3,803 cr in FY2025 "
                      "(full-year figure)", note)
        row = sa.compare_report([{"symbol": "KOTAKBANK.NS", "name": "K", "fundamentals": {**fund, "bank_like": True}, "technical": {
            "last_close": 1, "last_close_date": "2026-11-18", "returns": {"1Y": 0.1}}}, {"symbol": "X", "error": "e"}], ["KOTAKBANK", "X"],
            sa.instant("2026-11-18T16:00:00+05:30"))
        line = next(x for x in row["tradeoffs"] if x.startswith("One-offs inside annual"))
        self.assertIn("exceptional gains of ₹3,803 cr (FY2025, the year before)", line)

    def test_a_small_one_off_alone_in_the_year_before_says_nothing(self):
        quarters = [{"quarter": d, "profit": 5000 * CR, "eps": 5.0, "exceptional": 0.0, "basis": "consolidated"}
                    for d in ("2026-09-30", "2026-06-30", "2026-03-31", "2025-12-31", "2025-09-30", "2025-06-30")]
        series = {"annualDilutedEPS": [{"date": "2025-03-31", "value": 19.0}, {"date": "2026-03-31", "value": 20.0}]}
        fund = {"eps_growth": 20 / 19 - 1, "bank_like": True, "other_income_spikes": []}
        self.assertIsNone(sa.annual_one_offs(fund, {"quarters": quarters}, series, annual=lambda: {"2025-03-31": {"exceptional": 100 * CR}}))

    def test_compare_table_shows_pb_for_lenders(self):
        band = {"current": 2.06, "percentile": 0.26, "start": "2025-05-15", "p25": 2.1, "p75": 2.4, "median": 2.2}
        row = {"symbol": "HDFCBANK", "price": 808.15, "roe": 0.138, "bank_like": True, "pb": 2.06, "pb_band": band, "pe": 15.6,
               "pe_basis": "trailing 12 months, NSE results", "revenue_growth": 0.05, "eps_growth": 0.067, "quarter_profit_yoy": 0.056,
               "return_1y": -0.18, "dividend_yield_ex_specials": 0.016}
        table = "\n".join(sa.compare_table({"rows": [row]}))
        self.assertIn("| P/B | P/B vs own history |", table)
        self.assertIn("| 2.06 | 2.06: cheaper than 74% of days since May 2025 |", table)
        self.assertIn("15.6 (TTM)", table)
        lines = sa.tradeoffs([row, {**row, "symbol": "KOTAKBANK", "pb": 2.53, "pb_band": {**band, "percentile": 0.28}}])
        value = next(x for x in lines if x.startswith("Value"))
        self.assertIn("P/B today: HDFCBANK 2.06, KOTAKBANK 2.53", value)
        self.assertIn("P/B against its own history: HDFCBANK cheaper than on 74% of trading days since May 2025", value)
        plain = "\n".join(sa.compare_table({"rows": [{**row, "bank_like": False, "debt_to_equity": 0.1}]}))
        self.assertNotIn("P/B", plain)


# ---------- 8. Peers ----------

class PeerTests(unittest.TestCase):
    def test_financial_services_split_by_business(self):
        kind = lambda n: screener.business_kind(n, "Financial Services")
        self.assertEqual(kind("Bajaj Finance Ltd."), "lending companies (NBFCs and housing finance)")
        self.assertEqual(kind("Cholamandalam Investment and Finance Company Ltd."), "lending companies (NBFCs and housing finance)")
        self.assertEqual(kind("SBI Cards and Payment Services Ltd."), "lending companies (NBFCs and housing finance)")
        self.assertEqual(kind("HDFC Bank Ltd."), "banks")
        self.assertEqual(kind("SBI Life Insurance Company Ltd."), "insurers")
        self.assertEqual(kind("Power Finance Corporation Ltd."), "state infrastructure lenders")
        self.assertEqual(kind("REC Ltd."), "state infrastructure lenders")
        self.assertEqual(kind("HDFC Asset Management Company Ltd."), "capital-market companies")
        self.assertIsNone(kind("Bajaj Finserv Ltd."))
        self.assertIsNone(screener.business_kind("Bajaj Finance Ltd.", "Consumer Services"))

    def test_industry_peers_narrowed_to_the_same_kind(self):
        rows = [("Bajaj Finance Ltd.", "BAJFINANCE"), ("Shriram Finance Ltd.", "SHRIRAMFIN"), ("Muthoot Finance Ltd.", "MUTHOOTFIN"),
                ("Cholamandalam Investment and Finance Company Ltd.", "CHOLAFIN"), ("HDFC Bank Ltd.", "HDFCBANK"),
                ("Power Finance Corporation Ltd.", "PFC"), ("SBI Life Insurance Company Ltd.", "SBILIFE")]
        csv = "Company Name,Industry,Symbol,Series,ISIN Code\n" + "".join(f"{n},Financial Services,{s},EQ,X\n" for n, s in rows)

        def fetch(url):
            if "nifty500" in url:
                return csv.encode()
            raise Block("PUBLIC: source unavailable")
        group = screener.peers_for("BAJFINANCE.NS", fetcher=fetch)
        self.assertEqual([p["symbol"] for p in group["peers"]], ["SHRIRAMFIN", "MUTHOOTFIN", "CHOLAFIN"])
        self.assertIn("lending companies (NBFCs and housing finance) only", group["basis"])

    def test_curated_business_peers(self):
        self.assertIn("SWIGGY", sa.business_group("ETERNAL")["basis"])
        self.assertIn("ETERNAL", [p["symbol"] for p in sa.business_group("SWIGGY")["peers"]])
        peers = [p["symbol"] for p in sa.business_group("BAJFINANCE")["peers"]]
        self.assertTrue({"SHRIRAMFIN", "CHOLAFIN", "MUTHOOTFIN", "SBICARD", "LTF"} <= set(peers))
        self.assertNotIn("BAJAJFINSV", peers)  # a group company is never a business peer

    def test_loss_making_peers_are_listed_not_dropped(self):
        own = {"symbol": "ETERNAL.NS", "pe": 703.8, "roe": 0.012, "market_cap_cr": 353000}
        loaded = ({"basis": "b", "kind": "chosen", "peers": [{"symbol": s} for s in ("SWIGGY", "NAUKRI", "NYKAA")]},
                  [{"symbol": "SWIGGY.NS", "pe": None, "loss_making": True, "roe": -0.2, "market_cap_cr": 100000},
                   {"symbol": "NAUKRI.NS", "pe": 42.5, "roe": 0.05, "market_cap_cr": 90000},
                   {"symbol": "NYKAA.NS", "pe": 311.5, "roe": 0.1, "market_cap_cr": 60000}])
        c = sa.peer_comparison("ETERNAL.NS", own, loaded=loaded)
        self.assertEqual(c["loss_making"], ["SWIGGY"])
        self.assertIn("loss-making, P/E n/m: SWIGGY", sa.peer_line(c))
        c["size_mismatch"] = True
        self.assertTrue(sa.peer_line(c).endswith("; ⚠ the company is much larger than every peer, so the comparison is weak."))
        self.assertNotIn("median ROE 2.6% — ⚠", sa.peer_line(c))


# ---------- 9. Screener: debt with and without leases, precision at a threshold ----------

class ScreenerDebtTests(unittest.TestCase):
    def test_threshold_precision(self):
        self.assertEqual(screener.cell("debt_to_equity", 0.0996, [0.1]), "0.0996")
        self.assertEqual(screener.cell("debt_to_equity", 0.0996), "0.10")
        self.assertEqual(screener.cell("debt_to_equity", 0.08, [0.1]), "0.08")
        self.assertEqual(screener.cell("debt_to_equity", 0.1, [0.1]), "0.10")
        self.assertEqual(screener.cell("roe", 0.14996, [0.15]), "14.996%")
        self.assertEqual(screener.cell("roe", 0.2, [0.15]), "20.0%")
        self.assertEqual(screener.filter_limits({"filters": ["debt_to_equity<0.1", "roe>15", "bad"]}), {"debt_to_equity": [0.1], "roe": [0.15]})

    def test_borrowings_without_leases(self):
        series = {"annualTotalDebt": [{"date": "2026-03-31", "value": 11283 * CR}],
                  "annualStockholdersEquity": [{"date": "2026-03-31", "value": 95000 * CR}],
                  "annualCapitalLeaseObligations": [{"date": "2026-03-31", "value": 11283 * CR}],
                  "annualOrdinarySharesNumber": [{"date": "2026-03-31", "value": 361.8 * CR}],
                  "annualTotalRevenue": [{"date": "2026-03-31", "value": 1e12}]}
        f = sa.ratios(series, 3000.0, sa.instant("2026-11-18T16:00:00+05:30"))
        self.assertEqual((f["lease_liabilities"], f["borrowings_ex_leases"]), (11283 * CR, 0.0))
        del series["annualCapitalLeaseObligations"]
        self.assertIsNone(sa.ratios(series, 3000.0, sa.instant("2026-11-18T16:00:00+05:30"))["borrowings_ex_leases"])
        cols = screener.display_columns({"filters": ["debt_to_equity<0.1"]}, ["debt_cr", "pe"])
        self.assertEqual(cols, ("debt_cr", "borrowings_cr", "pe", "debt_to_equity"))
        self.assertEqual(screener.FIELDS["debt_cr"], "debt incl. leases (₹ cr)")


# ---------- 10. Shareholding: no data is not none, NDUs are not pledges, lenders' filings ----------

class ShareholdingRound7Tests(unittest.TestCase):
    def test_missing_pledge_figures_are_no_data(self):
        r = {"pledge_trend": [{"quarter": "2026-09-30", "pledged_pct": 0.0, "encumbered_pct": 99.99},
                              {"quarter": "2026-06-30", "pledged_pct": 0.0, "encumbered_pct": 99.99},
                              {"quarter": "2025-03-31", "pledged_pct": None, "encumbered_pct": 99.99},
                              {"quarter": "2024-12-31", "pledged_pct": None, "encumbered_pct": 99.99}]}
        line = sh.pledge_trend_line(r)
        self.assertIn("none in the 2 quarters that report it (no separate pledge figure for Dec 2024, Mar 2025", line)
        self.assertNotIn("none in any quarter", line)

    def test_ndu_is_not_called_a_pledge(self):
        r = {"pledge": {"pledged_pct_of_promoter": 0.0, "other_encumbered_pct_of_promoter": 100.0},
             "encumbrance_flags": {"pledged": False, "non_disposal": True, "other": False}}
        line = sh.pledge_line(r)
        self.assertIn("another 100.00% is encumbered without a formal pledge (the filing ticks non-disposal undertakings (NDUs))", line)
        self.assertIn("not to sell or pledge the shares", line)
        self.assertEqual(sh.encumbrance_kinds({"non_disposal": True, "other": True}), "non-disposal undertakings (NDUs) and other encumbrance")

    def test_lenders_encumbrance_filings(self):
        raw = json.dumps({"data": [
            {"acqType": "Equity shares", "acquisitionMode": "Others", "acqSaleType": "Acquisition", "acquirerDate": "17-SEP-2026 to 17-SEP-2026",
             "timestamp": "28-Sep-2026 16:19", "acquirerName": "Citicorp International Limited (as trustee for the holders of the Bonds)",
             "promoterType": "N", "totAcqShare": "51.93", "totAftShare": "51.93"},
            {"acqType": "Equity shares", "acquisitionMode": "Others", "acqSaleType": "Sale", "acquirerDate": "07-AUG-2026 to 07-AUG-2026",
             "timestamp": "17-Aug-2026 15:19", "acquirerName": "Axis Trustee Services Limited (in its capacity as the offshore security agent)",
             "promoterType": "N", "totSaleShare": "54.72", "totAftShare": "0"},
            {"acqType": "Shares in the nature of encumbrance", "acquisitionMode": "Creation of encumbrance", "acqSaleType": "Acquisition",
             "acquirerDate": "31-MAY-2022 to 31-MAY-2022", "timestamp": None, "acquirerName": "Some Lender", "promoterType": "N",
             "totAcqShare": "69.68", "totAftShare": "69.68"},
            {"acqType": "Equity shares", "acquisitionMode": "Market purchase", "acqSaleType": "Acquisition", "acquirerDate": "01-AUG-2026 to 01-AUG-2026",
             "timestamp": "02-Aug-2026 10:00", "acquirerName": "A Mutual Fund", "promoterType": "N", "totAcqShare": "5.1"},
            {"acqType": "Equity shares", "acquisitionMode": "Inter-se transfer", "acqSaleType": "Acquisition", "acquirerDate": "01-JUL-2026 to 01-JUL-2026",
             "timestamp": "02-Jul-2026 10:00", "acquirerName": "Promoter Trust", "promoterType": "Y", "totAcqShare": "1"}]}).encode()
        events = sh.encumbrance_events(raw)
        self.assertEqual([(e["date"], e["action"], e["pct"]) for e in events],
                         [("2026-09-17", "created", 51.93), ("2026-08-07", "released", 54.72), ("2022-05-31", "created", 69.68)])
        r = {"encumbrance_events": events[:2], "pledge_trend": [{"quarter": "2026-09-30"}, {"quarter": "2024-12-31"}]}
        line = sh.encumbrance_line(r)
        self.assertIn("over Dec 2024–Sep 2026 (SAST disclosures on NSE; 1 created, 1 released)", line)
        self.assertIn("17 Sep 2026 Citicorp International Limited took security over 51.93% of the shares", line)
        self.assertIn("07 Aug 2026 Axis Trustee Services Limited released security over 54.72% of the shares", line)
        self.assertIsNone(sh.encumbrance_line({}))

    def test_a_company_without_promoters_is_read(self):
        holdings = {k: v for k, v in tsh.LATEST_HOLDINGS.items() if k != "ShareholdingOfPromoterAndPromoterGroupMember"}
        holdings["PublicShareholdingMember"] = (tsh.TOTAL, "1")
        parsed = sh.parse_xbrl(tsh.shp(holdings))
        self.assertEqual(parsed["categories"]["promoter"]["pct"], 0.0)
        feed = tsh.Feed(latest=tsh.shp(holdings), previous=tsh.shp(holdings, day="2026-06-30"))
        master = json.loads(feed.files[tsh.MASTER])
        for row in master:
            row["pr_and_prgrp"] = "0"
        feed.files[tsh.MASTER] = json.dumps(master).encode()
        r = tsh.report(feed)
        self.assertTrue(r["no_promoter"])
        self.assertEqual(sh.headline(r), "No promoter group (a professionally managed company), so there is no promoter holding to pledge.")
        with self.assertRaises(Block):
            sh.parse_xbrl(tsh.shp({"ShareholdingPatternMember": (1, "1")}))


# ---------- 11. Equity allotted after the shareholding pattern (Bajaj Finance's QIP) ----------

def announcement(text, when="17-Nov-2026 08:25:32", desc="Allotment of Securities"):
    return {"an_dt": when, "desc": desc, "attchmntText": text}


BAJAJ_ANNOUNCEMENTS = [
    announcement("Bajaj Finance Limited has informed the Exchange regarding allotment of 114146341 securities pursuant to Qualified "
                 "Institution Placement at its meeting held on November 17, 2026"),
    announcement("Bajaj Finance Limited has informed the Exchange regarding allotment of 114146341 securities pursuant to Qualified "
                 "Institution Placement at its meeting held on November 17, 2026", when="18-Nov-2026 09:00:00"),  # repeated
    announcement("Bajaj Finance Limited has informed the Exchange regarding allotment of 5,74,25,740 securities pursuant to Preferential "
                 "Issue at its meeting held on November 10, 2026", when="10-Nov-2026 12:58:49"),
    announcement("Bajaj Finance Limited has informed the Exchange regarding allotment of 2,06,950 securities pursuant to Non Convertible "
                 "Securities at its meeting held on October 30, 2026", when="30-Oct-2026 14:53:37"),
    announcement("allotment of 9,138,522 equity shares under ESOS 2007", when="22-Oct-2026 10:00:00"),
    announcement("allotment of 40,000 equity shares under the employee stock option scheme", when="29-Sep-2026 10:00:00"),  # before the pattern
    announcement("Outcome of board meeting", desc="Outcome of Board Meeting"),
    announcement("allotment of 25,000 securities pursuant to Non-Convertible Debentures (NCDs) placed with qualified institutional buyers",
                 when="12-Nov-2026 10:00:00"),  # debt, though it names qualified institutions
    announcement("allotment of 61,000 equity shares under ESOP 2016 at its meeting held on September 30, 2026",
                 when="01-Oct-2026 10:00:00"),  # on the pattern's own date: already in its count
]


class AllotmentTests(unittest.TestCase):
    def test_only_equity_after_the_pattern_counts(self):
        out = sa.equity_allotments(BAJAJ_ANNOUNCEMENTS, "2026-09-30")
        self.assertEqual([(a["date"], a["shares"], a["kind"], a["counted"]) for a in out],
                         [("2026-10-22", 9138522, "employee stock options", True),
                          ("2026-11-10", 57425740, "preferential issue", False),  # securities: shares or warrants?
                          ("2026-11-17", 114146341, "QIP", True)])
        self.assertEqual(sa.equity_allotments(None, "2026-09-30"), [])
        self.assertEqual(sa.equity_allotments(BAJAJ_ANNOUNCEMENTS, "2026-11-17"), [])
        said = sa.equity_allotments([announcement("allotment of 2,500 equity shares on preferential basis", when="05-Oct-2026 10:00:00")],
                                    "2026-09-30")
        self.assertEqual((said[0]["shares"], said[0]["counted"]), (2500, True))
        self.assertEqual(sa._meeting_day("held on 3rd November, 2026"), sa.date(2026, 11, 3))

    def test_share_count_adds_announced_allotments(self):
        from unittest import mock
        listing = json.dumps([{"date": "30-SEP-2026", "xbrl": "https://nsearchives.nseindia.in/corporate/xbrl/SHP_1.xml",
                               "pr_and_prgrp": "54.7", "public_val": "45.3", "broadcastDate": "09-OCT-2026 10:00:00"}]).encode()
        files = {sh.api_url("/api/corporate-share-holdings-master", "ALPHA"): listing,
                 "https://nsearchives.nseindia.in/corporate/xbrl/SHP_1.xml": b"<xbrl/>",
                 sh.api_url("/api/corporate-announcements", "ALPHA"): json.dumps(BAJAJ_ANNOUNCEMENTS).encode()}

        def fetch(url):
            if url not in files:
                raise Block("NSE: source unavailable")
            return files[url]
        self.assertTrue(sh.allowed(sh.api_url("/api/corporate-announcements", "ALPHA")))
        self.assertFalse(sh.allowed(sh.api_url("/api/corporate-announcements", "ALPHA") + "&from_date=01-01-2026"))
        patch = mock.patch("shareholding.parse_xbrl", return_value={"total_shares": 6225954304})
        patch.start()
        self.addCleanup(patch.stop)
        count = sa.nse_share_count("ALPHA", fetch)
        self.assertEqual(count["pattern_shares"], 6225954304)
        self.assertEqual(count["shares"], 6225954304 + 9138522 + 114146341)
        self.assertTrue(count["announcements_read"])
        del files[sh.api_url("/api/corporate-announcements", "ALPHA")]
        quiet = sa.nse_share_count("ALPHA", fetch)
        self.assertEqual((quiet["shares"], quiet["allotments"], quiet["announcements_read"]), (6225954304, [], False))

    def test_market_cap_after_a_qip(self):
        yahoo = 622.2 * CR
        fund = {"market_cap": 1092.85 * yahoo, "pb": 5.52, "fcf_yield": 0.02, "shares_date": "2026-09-30"}
        count = {"shares": 622.6 * CR + 114146341, "date": "2026-09-30", "pattern_shares": 622.6 * CR, "announcements_read": True,
                 "allotments": [{"date": "2026-11-10", "shares": 57425740, "kind": "preferential issue", "counted": False},
                                {"date": "2026-11-17", "shares": 114146341, "kind": "QIP", "counted": True}]}
        note = sa.apply_share_count(fund, count, 1092.85)
        self.assertAlmostEqual(fund["market_cap"], 1092.85 * (622.6 * CR + 114146341))
        self.assertAlmostEqual(fund["pb"], 5.52 * 622.6 / 622.2)  # book has not seen the QIP money yet
        self.assertAlmostEqual(fund["fcf_yield"], 0.02 * yahoo / (622.6 * CR + 114146341))
        self.assertIn("Market cap uses the 634.01 cr shares from NSE's shareholding pattern for 30 Sep 2026 (622.60 cr) plus equity "
                      "allotted since in NSE announcements (11.41 cr (QIP, 17 Nov 2026)) (₹6.93 lakh cr)", note)
        self.assertIn("gave ₹6.80 lakh cr", note)
        self.assertIn("Not counted: 5.74 cr securities in a preferential issue (10 Nov 2026)", note)
        # Within 1% of Yahoo's count, allotments or not, nothing changes.
        small = {"market_cap": 100.0 * yahoo, "pb": 5.5}
        self.assertIsNone(sa.apply_share_count(small, {**count, "shares": yahoo * 1.009}, 100.0))
        # No allotments announced: the pattern's count, and the note says the announcements were checked.
        plain = {"market_cap": 366.0 * 920.8 * CR}
        note = sa.apply_share_count(plain, {"shares": 965.0 * CR, "date": "2026-09-30", "announcements_read": True, "allotments": []}, 366.0)
        self.assertIn("NSE's announcements show no equity allotted since.", note)


# ---------- 12. Gaps in Yahoo's years and a newer year on NSE (Nestlé India) ----------

def nestle_series():
    row = lambda pairs: [{"date": d, "value": v} for d, v in pairs]
    return {"annualTotalRevenue": row([("2021-12-31", 14665 * CR), ("2022-12-31", 16790 * CR), ("2025-03-31", 20078 * CR)]),
            "annualNetIncome": row([("2021-12-31", 2118 * CR), ("2022-12-31", 2391 * CR), ("2025-03-31", 3208 * CR)]),
            "annualDilutedEPS": row([("2021-12-31", 10.99), ("2022-12-31", 12.40), ("2025-03-31", 16.63)]),
            "annualStockholdersEquity": row([("2021-12-31", 1946 * CR), ("2022-12-31", 2459 * CR), ("2025-03-31", 4010 * CR)]),
            "annualOrdinarySharesNumber": row([("2025-03-31", 192.8 * CR)])}


def nestle_years():
    return {"2026-03-31": {"revenue": 23155 * CR, "profit": 0.0, "profit_total": 3499 * CR, "eps": 18.15, "basis": "consolidated",
                           "balance": {"date": "2026-03-31", "equity": 5157 * CR}},
            "2025-03-31": {"revenue": 20202 * CR, "profit": 3208 * CR, "eps": 33.27, "basis": "consolidated",
                           "balance": {"date": "2025-03-31", "equity": 4010 * CR}}}


NESTLE_BONUS = [("2025-08-08", 2.0)]


class YearGapTests(unittest.TestCase):
    def test_no_growth_or_average_equity_across_missing_years(self):
        f = sa.ratios(nestle_series(), 1303.4, sa.instant("2026-11-19T16:00:00+05:30"))
        self.assertIsNone(f["revenue_growth"])
        self.assertIsNone(f["eps_growth"])
        self.assertIsNone(f["growth_period"])
        self.assertAlmostEqual(f["roe"], 3208 / 4010)  # year-end equity, not an average with Dec 2022
        self.assertEqual(f["year_gap"], {"latest": "2025-03-31", "before": "2022-12-31"})
        self.assertTrue(sa.one_year_apart({"date": "2026-03-31"}, {"date": "2025-03-31"}))
        self.assertFalse(sa.one_year_apart({"date": "2024-03-31"}, {"date": "2022-12-31"}))  # a 15-month year
        self.assertFalse(sa.one_year_apart({"date": "x"}, {}))

    def test_the_newer_nse_year_replaces_yahoos(self):
        series = nestle_series()
        fund = sa.ratios(series, 1303.4, sa.instant("2026-11-19T16:00:00+05:30"))
        fund["market_cap"] = 1303.4 * 192.8 * CR
        note = sa.newer_nse_year(fund, series, nestle_years, 1303.4, NESTLE_BONUS)
        self.assertAlmostEqual(fund["revenue_growth"], 23155 / 20202 - 1)
        self.assertAlmostEqual(fund["eps_growth"], 18.15 / (33.27 / 2) - 1)  # the year before put on the post-bonus basis
        self.assertAlmostEqual(fund["roe"], 3499 / ((5157 + 4010) / 2))
        self.assertAlmostEqual(fund["net_margin"], 3499 / 23155)
        self.assertEqual(fund["growth_period"], "FY2026 vs FY2025")
        self.assertEqual(series["annualDilutedEPS"][-1], {"date": "2026-03-31", "value": 18.15, "source": "NSE annual results"})
        self.assertIn("Yahoo's statements stop at FY2025, but the company has filed FY2026 results on NSE", note)
        self.assertIn("ROE 76.3%", note)
        self.assertIn(note, fund["eps_flags"])

    def test_nothing_changes_when_yahoo_is_current_or_nse_is_missing(self):
        series = nestle_series()
        fund = sa.ratios(series, 1303.4, sa.instant("2026-11-19T16:00:00+05:30"))
        before = dict(fund)
        self.assertIsNone(sa.newer_nse_year(fund, series, {"2025-03-31": nestle_years()["2025-03-31"]}, 1303.4))
        self.assertIsNone(sa.newer_nse_year(fund, series, {}, 1303.4))
        self.assertIsNone(sa.newer_nse_year(fund, series, lambda: 1 / 0, 1303.4))
        self.assertIsNone(sa.newer_nse_year(fund, {**series, "_currency": {"reported": "USD"}}, nestle_years, 1303.4))
        self.assertEqual(fund, before)

    def test_an_untagged_attributable_profit_is_the_total(self):
        body = bank_xml("2026-09-30", facts={"Income": 6782 * CR, "ProfitLossForPeriod": 1010 * CR,
                                             "ProfitOrLossAttributableToOwnersOfParent": 0, "DilutedEarningsPerShareAfterExtraordinaryItems": 5.2})
        q = sa.results_xbrl(body)
        if "ProfitOrLossAttributableToOwnersOfParent" in sa.PROFIT_TAGS:
            self.assertEqual(q["profit"], 1010 * CR)
        self.assertEqual(sa._filed_profit({"profit": 0.0, "profit_total": 5.0}), 5.0)
        self.assertEqual(sa._filed_profit({"profit": -3.0, "profit_total": 5.0}), -3.0)


# ---------- 13. The headline P/E beside the P/E without one-offs ----------

class OneOffPeTests(unittest.TestCase):
    def test_summary_gives_both_when_they_differ(self):
        import test_stock_analysis as tsa
        r = tsa.run(tsa.Feed(), peers=False)
        f = r["fundamentals"]
        f["pe"], f["pe_ex_one_offs"] = 32.4, 46.1
        self.assertIn(", about 46.1 without one-offs (before tax; see ⚠)", sa.summary(r))
        f["pe_ex_one_offs"] = 32.4 * (1 + sa.PE_ONE_OFF_GAP * 0.9)
        self.assertNotIn("without one-offs (before tax", sa.summary(r))

    def test_eps_one_off_growth_goes_in_the_period_bracket(self):
        import test_stock_analysis as tsa
        r = tsa.run(tsa.Feed(), peers=False)
        f = r["fundamentals"]
        f["revenue_basis"], f["annual_one_off"] = "NSE annual results", {"growth_adjusted": 0.131}
        line = next(l for l in sa.summary(r).splitlines() if l.startswith("Quality and growth"))
        self.assertIn("(NSE filings), EPS", line)
        self.assertIn("; EPS about +13.1% without one-offs, estimate)", line)


class DuplicatedEpsTests(unittest.TestCase):
    def test_a_discontinued_eps_copied_from_continuing_is_not_added(self):
        def body(disc, combined, disc_profit=None):
            facts = {"DilutedEarningsLossPerShareFromContinuingOperations": 429.01, "DilutedEarningsLossPerShareFromDiscontinuedOperations": disc,
                     "DilutedEarningsLossPerShareFromContinuingAndDiscontinuedOperations": combined, "ProfitLossForPeriod": 134882 * CR}
            if disc_profit is not None:
                facts["ProfitLossFromDiscontinuedOperationsAfterTax"] = disc_profit
            return ('<?xml version="1.0"?><xbrli:xbrl xmlns:xbrli="http://www.xbrl.org/2003/instance" xmlns:f="http://x">'
                    + "".join(f'<f:{k} contextRef="FourD">{v}</f:{k}>' for k, v in facts.items()) + '</xbrli:xbrl>').encode()
        self.assertEqual(sa.annual_xbrl(body(429.01, 858.02))["eps"], 429.01)  # Maruti Suzuki FY2024
        self.assertEqual(sa.annual_xbrl(body(12.5, 441.51))["eps"], 441.51)  # a real discontinued business
        self.assertEqual(sa.annual_xbrl(body(429.01, 858.02, 13488 * CR))["eps"], 858.02)


# ---------- 14. Back-fill: parsed filings kept on disk for live runs ----------

class FilingStoreTests(unittest.TestCase):
    def setUp(self):
        import tempfile
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        sa._FILED.clear()
        self.addCleanup(sa._FILED.clear)

    def test_a_filing_read_once_is_not_fetched_again(self):
        url = "https://nsearchives.nseindia.in/corporate/xbrl/INDAS_9.xml"
        calls = []
        body = ('<?xml version="1.0"?><xbrli:xbrl xmlns:xbrli="http://www.xbrl.org/2003/instance" xmlns:f="http://x">'
                '<f:DilutedEarningsPerShareAfterExtraordinaryItems contextRef="FourD">82.27</f:DilutedEarningsPerShareAfterExtraordinaryItems>'
                '</xbrli:xbrl>').encode()

        def fetch(u):
            calls.append(u)
            return body
        first = sa._read_filing(fetch, {"url": url}, self.tmp.name)
        sa._FILED.clear()  # a new run: memory is empty, the store is not

        def fail(u):
            raise Block("NSE: source unavailable")
        self.assertEqual(sa._read_filing(fail, {"url": url}, self.tmp.name), first)
        self.assertEqual(first["eps"], 82.27)
        self.assertEqual(len(calls), 1)
        sa._FILED.clear()
        with self.assertRaises(Block):  # without a store nothing is remembered across runs
            sa._read_filing(fail, {"url": url})

    def test_the_saved_list_stands_in_when_nse_does_not_answer(self):
        from datetime import date
        listing = json.dumps([{"toDate": "31-Mar-2024", "consolidated": "Consolidated", "period": "Annual",
                               "xbrl": "https://nsearchives.nseindia.in/corporate/xbrl/INDAS_1.xml", "broadCastDate": "20-Apr-2024 18:00:00"}]).encode()
        rows = sa.stored_annual_filings("ALPHA", lambda u: listing, self.tmp.name, date(2026, 11, 1))
        self.assertEqual(rows[0]["date"], "2024-03-31")

        def fail(u):
            raise Block("NSE: source unavailable")
        self.assertEqual(sa.stored_annual_filings("ALPHA", fail, self.tmp.name, date(2026, 12, 1)), rows)
        with self.assertRaises(Block):  # too old to stand in
            sa.stored_annual_filings("ALPHA", fail, self.tmp.name, date(2027, 6, 1))
        with self.assertRaises(Block):
            sa.stored_annual_filings("ALPHA", fail, None, date(2026, 12, 1))

    def test_store_location(self):
        import os
        from unittest import mock
        with mock.patch.dict(os.environ, {sa.CACHE_ENV: "off"}):
            self.assertIsNone(sa.filing_store())
        with mock.patch.dict(os.environ, {sa.CACHE_ENV: self.tmp.name}):
            self.assertEqual(sa.filing_store(), Path(self.tmp.name) / "nse_annual_filings")


if __name__ == "__main__":
    unittest.main()
