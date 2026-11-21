"""Round 8 (held-out review): fund houses matched across quarters, older-format promoter rows, government lines and
promoter notes in the shareholding report; listed lenders inside consolidated debt, multi-year growth, the reason for a
low ROE and demerger P/E cautions in the stock report; sector exclusion and saved snapshots in the screener.
Offline: every feed is synthetic, and the network is refused outright while these tests run."""
from contextlib import redirect_stderr
from datetime import date, timedelta
import io
import json
from pathlib import Path
import socket
import sys
import tempfile
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT / "tests"))
from core import Block, instant
import screener
import shareholding as sh
import stock_analysis as sa
import test_shareholding as tsh
import test_stock_analysis as tsa
import test_screener as tsc

CR = 1e7


def setUpModule():
    # Any attempt to open a connection fails the test run instead of reaching the network.
    global _NO_NET
    _NO_NET = mock.patch.object(socket, "create_connection", side_effect=AssertionError("network used in an offline test"))
    _NO_NET.start()


def tearDownModule():
    _NO_NET.stop()


# ---------- 1. Shareholding: fund houses, older formats, government, promoter notes ----------

def holders_xbrl(holders, holdings=None, day="2026-09-30", scale="1", notes=None, sbo=()):
    """tsh.shp() plus named holders; scale '100' writes every percentage as a percentage (the early-2025 format)."""
    holdings = dict(holdings or tsh.LATEST_HOLDINGS)
    if scale == "100":
        holdings = {k: (v[0], str(round(float(v[1]) * 100, 4))) for k, v in holdings.items()}
    body = tsh.with_holders(tsh.shp(holdings, pledged=0, encumbered=0, day=day), holders)
    extra = ""
    if notes:
        extra += f'<in-bse-shp:DisclosureOfNotesOnShareholdingPatternExplanatoryTextBlock contextRef="MainI">{notes}</in-bse-shp:DisclosureOfNotesOnShareholdingPatternExplanatoryTextBlock>'
    for i, (owner, registered) in enumerate(sbo):
        cid = f"SBO{i}"
        extra += (f'<xbrli:context id="{cid}"><xbrli:entity><xbrli:identifier scheme="s">1</xbrli:identifier></xbrli:entity>'
                  f'<xbrli:period><xbrli:instant>{day}</xbrli:instant></xbrli:period><xbrli:scenario><xbrldi:typedMember '
                  f'dimension="in-bse-shp:SignificantBeneficialOwnersAxis"><in-bse-shp:D>{cid}</in-bse-shp:D></xbrldi:typedMember>'
                  f'</xbrli:scenario></xbrli:context><in-bse-shp:NameOfSignificantBeneficialOwners contextRef="{cid}">{owner}'
                  f'</in-bse-shp:NameOfSignificantBeneficialOwners><in-bse-shp:NameOfRegisteredOwner contextRef="{cid}">{registered}'
                  '</in-bse-shp:NameOfRegisteredOwner>')
    return body.replace(b"</xbrli:xbrl>", extra.encode() + b"</xbrli:xbrl>")


MF, INS, NPS, FPI = ("DetailsOfSharesHeldByMutualFundsOrUTIAxis", "DetailsOfSharesHeldByInsuranceCompaniesAxis",
                     "DetailsOfSharesHeldByProvidentFundsOrPensionFundsAxis", "DetailsOfSharesHeldByInstitutionsForeignPortfolioInvestorOneAxis")
SEP = [("A1", INS, "LICI INDEX PLUS FLEXI SMART GROWTH FUND", "0.0446", False, None),
       ("A2", MF, "SBI LARGE CAP FUND", "0.0260", False, None),
       ("A3", NPS, "NPS TRUST A/C - SBI PF NPS JEEVAN SWARNA RETIREMEN", "0.0193", False, None),
       ("A4", MF, "ICICI PRUDENTIAL NIFTY ETF", "0.0178", False, None),
       ("A5", MF, "HDFC TRUSTEE COMPANY LIMITED-HDFC FLEXI CAP FUND", "0.0142", False, None),
       ("A6", FPI, "GOVERNMENT OF SINGAPORE - E", "0.0104", False, None),
       ("A7", MF, "KOTAK FLEXICAP FUND", "0.0102", False, None)]
JUN = [("B1", INS, "LICI NEW PENSION PLUS SECURED FUND", "0.0414", False, None),
       ("B2", MF, "SBI ELSS TAX SAVER FUND", "0.0253", False, None),
       ("B3", NPS, "NPS TRUST A/C - SBI PF NPS JEEVAN SWARNA RETIREMEN", "0.0185", False, None),
       ("B4", MF, "ICICI PRUDENTIAL MUTUAL FUND - ICICI PRUDENTIAL NI", "0.0168", False, None),
       ("B5", MF, "HDFC MUTUAL FUND - HDFC TECHNOLOGY FUND", "0.0136", False, None),
       ("B6", FPI, "GOVERNMENT OF SINGAPORE", "0.0103", False, None),
       ("B7", FPI, "GOVERNMENT PENSION FUND GLOBAL", "0.0105", False, None)]


def house_report(latest=SEP, previous=JUN):
    return tsh.report(tsh.Feed(latest=holders_xbrl(latest), previous=holders_xbrl(previous, tsh.PREVIOUS_HOLDINGS, day="2026-06-30")))


class FundHouseTests(unittest.TestCase):
    def test_houses(self):
        self.assertEqual(sh.fund_house("LICI INDEX PLUS FLEXI SMART GROWTH FUND", "insurer"), ("LIC", "LIC (all schemes)"))
        self.assertEqual(sh.fund_house("Life Insurance Corporation of India - P & GS Fund", "insurer"), ("LIC", "LIC (all schemes)"))
        self.assertEqual(sh.fund_house("SBI LARGE CAP FUND", "mutual fund"), ("SBI", "SBI Mutual Fund (all schemes)"))
        self.assertEqual(sh.fund_house("HDFC TRUSTEE COMPANY LIMITED-HDFC FLEXI CAP FUND", "mutual fund")[0], "HDFC")
        self.assertEqual(sh.fund_house("Nippon Life India Trustee Ltd-A/C Nippon India Large Cap Fund", "mutual fund")[0], "Nippon India")
        self.assertEqual(sh.fund_house("NPS TRUST- A/C ICICI PRUDENTIAL PENSION FUND SCHEM", "provident/pension fund"),
                         ("NPS:ICICI Prudential", "NPS Trust (ICICI Prudential pension fund schemes)"))
        self.assertEqual(sh.fund_house("SBI LIFE INSURANCE CO. LTD", "insurer"), ("SBI", None))  # keeps its filed name
        self.assertIsNone(sh.fund_house("GOOGLE INTERNATIONAL LLC", "foreign portfolio investor"))
        self.assertIsNone(sh.fund_house("SBI LARGE CAP FUND", "foreign portfolio investor"))  # houses only for domestic funds

    def test_a_renamed_scheme_is_not_new(self):
        r = house_report()
        rows = {h["name"]: h for h in r["large_public_holders"]}
        self.assertAlmostEqual(rows["LIC (all schemes)"]["previous_pct"], 4.14)
        self.assertEqual(rows["LIC (all schemes)"]["filed_as"], ["LICI INDEX PLUS FLEXI SMART GROWTH FUND"])
        self.assertAlmostEqual(rows["SBI Mutual Fund (all schemes)"]["previous_pct"], 2.53)
        self.assertAlmostEqual(rows["HDFC Mutual Fund (all schemes)"]["previous_pct"], 1.36)
        self.assertAlmostEqual(rows["GOVERNMENT OF SINGAPORE - E"]["previous_pct"], 1.03)  # same entity, suffix aside
        line = sh.holders_line(r)
        self.assertIn("LIC (all schemes) 4.46% (+0.32 pts)", line)
        self.assertIn("ICICI Prudential Mutual Fund (all schemes) 1.78% (+0.10 pts)", line)
        self.assertNotIn("LIC (all schemes) 4.46% (new", line)
        self.assertEqual(line.count("(new above 1%)"), 0)  # Kotak is the 7th holder, beyond the six shown
        self.assertIn("No longer named above 1%: Government Pension Fund Global (was 1.05%)", line)
        text = sh.markdown(r)
        self.assertIn("| LIC (all schemes) (named in the filing as Lici Index Plus Flexi Smart Growth Fund) | insurer | 4.46% | 4.14% |", text)
        self.assertIn("| Kotak Mutual Fund (all schemes) (named in the filing as Kotak Flexicap Fund) | mutual fund | 1.02% | below 1% or not named |", text)

    def test_a_house_new_this_quarter_is_new(self):
        r = house_report(latest=[("K1", MF, "KOTAK FLEXICAP FUND", "0.0302", False, None)] + SEP[:2])
        self.assertIn("Kotak Mutual Fund (all schemes) 3.02% (new above 1%)", sh.holders_line(r))

    def test_two_schemes_of_one_house_add_up(self):
        rows, _ = sh.match_large_holders([{"name": "SBI ARBITRAGE FUND", "pct": 1.2, "kind": "mutual fund"},
                                          {"name": "SBI NIFTY 50 ETF", "pct": 1.1, "kind": "mutual fund"}],
                                         [{"name": "SBI Mutual Fund", "pct": 2.0, "kind": "mutual fund"}])
        self.assertEqual(len(rows), 1)
        self.assertAlmostEqual(rows[0]["pct"], 2.3)
        self.assertAlmostEqual(rows[0]["previous_pct"], 2.0)
        self.assertEqual(rows[0]["filed_as"], ["SBI ARBITRAGE FUND", "SBI NIFTY 50 ETF"])


OLD_PROMOTERS = [("P1", "DetailsOfSharesHeldByOthersIndianShareholdersAxis", "Alpha Holdings Limited", "0.5000", False, None),
                 ("P2", "DetailsOfSharesHeldByOtherForeignShareholdersAxis", "Beta Investments Pte Ltd", "0.1603", False, None),
                 ("H1", "DetailsOfSharesHeldByOtherNonInstitutionsAxis", "HUF", "0.0004", False, None)]


class OlderFormatTests(unittest.TestCase):
    def test_percentages_filed_as_percentages_are_scaled(self):
        parsed = sh.parse_xbrl(holders_xbrl(SEP, scale="100").replace(b">0.0446<", b">4.46<"))
        self.assertAlmostEqual(parsed["categories"]["promoter"]["pct"], 66.03)
        self.assertAlmostEqual(parsed["categories"]["total"]["pct"], 100.0)
        lic = next(h for h in parsed["large_public_holders"] if h["name"].startswith("LICI"))
        self.assertAlmostEqual(lic["pct"], 4.46)

    def test_unmarked_promoter_rows_are_read_when_they_add_up(self):
        parsed = sh.parse_xbrl(holders_xbrl(OLD_PROMOTERS + SEP[:1]))
        self.assertTrue(parsed["promoters_named"])
        self.assertEqual([h["name"] for h in parsed["promoter_holders"]], ["Alpha Holdings Limited", "Beta Investments Pte Ltd"])
        self.assertEqual([h["name"] for h in parsed["large_public_holders"]], ["LICI INDEX PLUS FLEXI SMART GROWTH FUND"])

    def test_unmarked_rows_that_do_not_add_up_name_nobody(self):
        parsed = sh.parse_xbrl(holders_xbrl(OLD_PROMOTERS[:1] + SEP[:1]))
        self.assertFalse(parsed["promoters_named"])
        self.assertEqual(parsed["promoter_holders"], [])
        self.assertNotIn("Alpha Holdings Limited", [h["name"] for h in parsed["large_public_holders"]])  # never a public holder
        after = {"total_shares": 100.0, "promoters_named": True, "promoter_holders": [{"name": "Alpha Holdings Limited", "pct": 50.0, "shares": 50.0}]}
        self.assertEqual(sh.promoter_changes(parsed, after), [])  # not "not listed → 50%"
        m = {"holder_changes": [], "unnamed_filings": ["2025-03-31"]}
        self.assertIn("The Mar 2025 filing (older format) does not mark which named holders are promoters", sh.changes_words(m))

    def test_a_category_row_is_not_a_holder(self):
        body = holders_xbrl([("H9", "DetailsOfSharesHeldByOtherNonInstitutionsAxis", "HUF", "0.0400", False, None)])
        body = body.replace(b"</xbrli:xbrl>", b'<in-bse-shp:CategoryOfOtherNonInstitutions contextRef="D_H9">HUF</in-bse-shp:CategoryOfOtherNonInstitutions></xbrli:xbrl>')
        self.assertEqual(sh.parse_xbrl(body)["large_public_holders"], [])


class GovernmentTests(unittest.TestCase):
    def test_government_lines_apart_from_dii(self):
        holdings = {**tsh.LATEST_HOLDINGS, "GovernmentsMember": (100, "0.0600"), "CentralGovernmentOrPresidentOfIndiaMember": (80, "0.0500"),
                    "StateGovernmentsOrGovernorsMember": (20, "0.0100")}
        r = tsh.report(tsh.Feed(latest=holders_xbrl([], holdings)))
        rows = {x["key"]: x for x in r["rows"]}
        self.assertAlmostEqual(rows["government"]["pct"], 6.0)
        self.assertEqual(rows["government"]["label"], "Government (public category, apart from DII)")
        self.assertAlmostEqual(rows["central_government"]["pct"], 5.0)
        self.assertIn("Government (public category, apart from DII) 6.00%", sh.summary(r))
        # One kind of government holding only: no sub-line repeating the total.
        holdings.update({"CentralGovernmentOrPresidentOfIndiaMember": (100, "0.0600"), "StateGovernmentsOrGovernorsMember": (0, "0")})
        rows = {x["key"] for x in tsh.report(tsh.Feed(latest=holders_xbrl([], holdings)))["rows"]}
        self.assertNotIn("central_government", rows)

    def test_government_named_holder(self):
        rows = sh.large_public_holders({("DetailsOfSharesHeldByOtherNonInstitutionsAxis", "1"): {"name": "President of India", "fraction": "0.49"}})
        self.assertEqual(rows[0]["kind"], "government")


VEHICLES = {"Alpha Holdings Limited": {"note": "holding company of the Alpha family", "checked": "2026-11-21"}}


class PromoterNoteTests(unittest.TestCase):
    def parsed(self):
        promoters = [("P1", "DetailsOfSharesHeldByOthersIndianShareholdersAxis", "ALPHA HOLDINGS LIMITED", "0.4000", True, None),
                     ("P2", "DetailsOfSharesHeldByOtherForeignShareholdersAxis", "BETA INVESTMENTS PTE LTD", "0.2000", True, None),
                     ("P3", "DetailsOfSharesHeldByOthersIndianShareholdersAxis", "GAMMA TRADING LLP", "0.0603", True, None)]
        notes = ("1. Beta Investments Pte Ltd is person acting in concert with Alpha Holdings Limited, Promoter of the Company. "
                 "2. Gamma Trading LLP " + "holds shares under a long legal arrangement " * 8 + "and is a promoter group entity.")
        return sh.parse_xbrl(holders_xbrl(promoters, notes=notes, sbo=[("Ravi Alpha", "Alpha Holdings Limited"), ("Mira Alpha", "Alpha Holdings Limited")]))

    def test_notes_come_only_from_the_filing_or_the_curated_list(self):
        p = self.parsed()
        notes = sh.promoter_notes(p["promoter_holders"], p, VEHICLES)
        self.assertIn("holding company of the Alpha family [curated list, checked 2026-11-21]", notes["ALPHA HOLDINGS LIMITED"])
        self.assertIn("significant beneficial owners named in the filing: Ravi Alpha, Mira Alpha", notes["ALPHA HOLDINGS LIMITED"])
        self.assertIn('filing note: "Beta Investments Pte Ltd is person acting in concert with Alpha Holdings Limited, Promoter of the Company"',
                      notes["BETA INVESTMENTS PTE LTD"])
        self.assertNotIn("GAMMA TRADING LLP", notes)  # a long legal sentence is not a description, and nothing is guessed
        self.assertEqual(sh.promoter_notes(p["promoter_holders"], {}, {}), {})

    def test_summary_lists_every_promoter_entity(self):
        p = self.parsed()
        r = {"promoter_holders": p["promoter_holders"], "rows": [{"key": "promoter", "pct": 66.03}], "total_shares": None,
             "promoter_notes": sh.promoter_notes(p["promoter_holders"], p, VEHICLES)}
        line = sh.promoter_holder_line(r)
        self.assertTrue(line.startswith("Promoter entities (3, promoter group 66.03% in all): Alpha Holdings Limited 40.00%; "
                                        "Beta Investments PTE LTD 20.00%; Gamma Trading LLP 6.03%."), line)
        self.assertIn("About them: Alpha Holdings Limited: holding company of the Alpha family", line)
        many = [{"name": f"Holder {i}", "pct": 10.0 - i * 0.5, "shares": None} for i in range(11)]
        line = sh.promoter_holder_line({"promoter_holders": many, "rows": [{"key": "promoter", "pct": 80.0}], "total_shares": None})
        self.assertIn("Holder 7 6.50%; and 3 more promoter entities with 16.50% together.", line)


# ---------- 2. Stock: lenders inside consolidated debt ----------

def lt_fund(**extra):
    return {"total_debt": 140300 * CR, "debt_to_equity": 140300 / 113029, "debt_date": "2026-09-30", "bank_like": False, **extra}


def lt_holdings(**ltf):
    return {"rows": [{"symbol": "LTM", "name": "LTM Limited", "stake_pct": 69.01, "bank_like": False, "debt_cr": 2000.0, "equity_cr": 25000.0},
                     {"symbol": "LTF", "name": "L&T Finance Limited", "stake_pct": 65.93, "bank_like": True, "debt_cr": 123869.0,
                      "equity_cr": 29267.0, "debt_date": "2026-09-30", **ltf},
                     {"symbol": "LTTS", "error": "unavailable"}]}


class LenderDebtTests(unittest.TestCase):
    def test_lt_finance_inside_lt(self):
        x = sa.lender_debt(lt_fund(), lt_holdings())
        self.assertAlmostEqual(x["share"], 123869 / 140300)
        self.assertAlmostEqual(x["ex_de"], (140300 - 123869) / (113029 - 0.6593 * 29267), places=4)
        self.assertEqual([s["symbol"] for s in x["subsidiaries"]], ["LTF"])
        text = sa.lender_debt_words({**lt_fund(), "lender_debt": x})
        self.assertIn("Debt/equity of 1.24 is consolidated and mostly a lending book: L&T Finance Limited (LTF, 66% owned; borrowings ₹1.24 lakh cr "
                      "at 30 Sep 2026), a lender, is about 88% of the group's borrowings of ₹1.40 lakh cr at 30 Sep 2026.", text)
        self.assertIn("debt/equity would be about 0.18 (estimate)", text)

    def test_no_note_when_it_does_not_apply(self):
        self.assertIsNone(sa.lender_debt(lt_fund(), lt_holdings(stake_pct=45.0)))   # an associate, not consolidated
        self.assertIsNone(sa.lender_debt(lt_fund(), lt_holdings(bank_like=False)))  # not a lender
        self.assertIsNone(sa.lender_debt(lt_fund(), lt_holdings(debt_cr=10000.0)))  # a small part of the group's debt
        self.assertIsNone(sa.lender_debt(lt_fund(bank_like=True), lt_holdings()))
        self.assertIsNone(sa.lender_debt(lt_fund(), None))
        # Different dates can put the lender above the group's figure: the share is said, no netted ratio.
        x = sa.lender_debt(lt_fund(), lt_holdings(debt_cr=150000.0))
        self.assertIsNone(x["ex_de"])
        self.assertIn("about 100% of the group's borrowings", sa.lender_debt_words({**lt_fund(), "lender_debt": x}))

    def test_curated_holdings_include_major_listed_subsidiaries(self):
        table = sa.load_business_peers()["companies"]
        self.assertIn("LTF", table["LT"]["holdings"])
        self.assertIn("HDBFS", table["HDFCBANK"]["holdings"])
        self.assertIn("HINDZINC", table["VEDL"]["holdings"])
        self.assertEqual(table["BHARTIARTL"]["holdings"], ["BHARTIHEXA", "INDUSTOWER"])


# ---------- 3. Stock: multi-year growth ----------

def asian_backfill():
    rows = [("2024-03-31", 35495, 5460), ("2023-03-31", 34489, 4106), ("2022-03-31", 29101, 3031)]
    return {"filings": [{"date": d, "revenue": r * CR, "profit": p * CR, "basis": "consolidated", "eps": 1.0} for d, r, p in rows]}


ASIAN_YEARS = {"2026-03-31": {"revenue": 35584 * CR, "profit": 4325 * CR, "basis": "consolidated"},
               "2025-03-31": {"revenue": 33906 * CR, "profit": 3667 * CR, "basis": "consolidated"}}


class MultiYearTests(unittest.TestCase):
    def test_asian_paints_slow_trend(self):
        g = sa.multi_year_growth({}, {}, ASIAN_YEARS, asian_backfill(), "ASIANPAINT", demergers=[])
        self.assertEqual((g["start"], g["end"]), ("2023-03-31", "2026-03-31"))
        self.assertAlmostEqual(g["revenue_cagr"], (35584 / 34489) ** (1 / 3) - 1)
        self.assertAlmostEqual(g["profit_cagr"], (4325 / 4106) ** (1 / 3) - 1)
        self.assertEqual(sa.cagr_words(g), "3-year CAGR FY2023–FY2026: revenue +1.0% a year, profit attributable to shareholders +1.7% a year "
                                            "(NSE consolidated results filings)")

    def test_one_year_carrying_the_growth_is_named(self):
        years = {"2026-03-31": {"revenue": 470 * CR, "profit": 76 * CR, "basis": "consolidated"},
                 "2025-03-31": {"revenue": 450 * CR, "profit": 70 * CR, "basis": "consolidated"}}
        backfill = {"filings": [{"date": "2024-03-31", "revenue": 410 * CR, "profit": 64 * CR, "basis": "consolidated"},
                                {"date": "2023-03-31", "revenue": 200 * CR, "profit": 45 * CR, "basis": "consolidated"}]}
        g = sa.multi_year_growth({}, {}, years, backfill, "HDFCBANK", demergers=[])
        self.assertEqual(g["jump"]["year"], "2024-03-31")
        self.assertIn("most of it in one year (FY2024 revenue +105%), so the average hides a step change such as a merger or acquisition",
                      sa.cagr_words(g))
        self.assertIsNone(sa.multi_year_growth({}, {}, ASIAN_YEARS, asian_backfill(), "X", demergers=[])["jump"])

    def test_yahoo_when_the_filings_do_not_reach(self):
        series = {"annualTotalRevenue": [{"date": f"{y}-03-31", "value": v} for y, v in ((2023, 800.0), (2024, 900.0), (2025, 1000.0), (2026, 1100.0))],
                  "annualNetIncome": [{"date": "2023-03-31", "value": -5.0}, {"date": "2026-03-31", "value": 110.0}]}
        g = sa.multi_year_growth({}, series, ASIAN_YEARS, None, "X", demergers=[])  # NSE has only two years
        self.assertEqual(g["source"], "Yahoo annual statements")
        self.assertAlmostEqual(g["revenue_cagr"], (1100 / 800) ** (1 / 3) - 1)
        self.assertIsNone(g["profit_cagr"])
        self.assertIn("profit attributable to shareholders n/m (loss in FY2023)", sa.cagr_words(g))
        mixed = {**ASIAN_YEARS, "2023-03-31": {**ASIAN_YEARS["2025-03-31"], "basis": "standalone"}}
        self.assertEqual(sa.multi_year_growth({}, series, mixed, None, "X", demergers=[])["source"], "Yahoo annual statements")
        self.assertIsNone(sa.multi_year_growth({}, {}, {}, None, "X", demergers=[]))
        self.assertIsNone(sa.multi_year_growth({"growth_withheld": True}, series, ASIAN_YEARS, asian_backfill(), "X", demergers=[]))

    def test_a_demerger_after_the_first_year_withholds_it(self):
        ev = [{"parent": "VEDL", "record_date": "2026-05-01", "cost_split_pct": {"parent": 52.34, "resulting": 47.66}}]
        g = sa.multi_year_growth({}, {}, ASIAN_YEARS, asian_backfill(), "VEDL", demergers=ev)
        self.assertIn("withheld", g)
        self.assertIn("not comparable", sa.cagr_words(g))
        small = [{**ev[0], "cost_split_pct": {"parent": 98.1, "resulting": 1.9}}]
        self.assertNotIn("withheld", sa.multi_year_growth({}, {}, ASIAN_YEARS, asian_backfill(), "VEDL", demergers=small))

    def test_compare_shows_three_year_growth(self):
        rows = []
        for sym, rev, profit in (("ASIANPAINT", 0.010, 0.017), ("PIDILITIND", 0.074, 0.244)):
            g = {"start": "2023-03-31", "end": "2026-03-31", "revenue_cagr": rev, "profit_cagr": profit, "source": "NSE", "years": 3}
            rows.append({"symbol": sym, "price": 1.0, "roe": 0.2, "net_margin": 0.1, "debt_to_equity": 0.2, "pe": 50.0, "revenue_growth": 0.05,
                         "eps_growth": 0.1, "return_1y": 0.0, "dividend_yield_ex_specials": 0.01, "quarter_profit_yoy": None, **sa.multi_year_fields(g)})
        growth = next(x for x in sa.tradeoffs(rows) if x.startswith("Growth"))
        self.assertIn("revenue a year over three years (FY2023–FY2026): PIDILITIND +7.4%, ASIANPAINT +1.0%", growth)
        self.assertIn("profit attributable to shareholders a year over three years: PIDILITIND +24.4%, ASIANPAINT +1.7%", growth)
        table = "\n".join(sa.compare_table({"rows": rows}))
        self.assertIn("| +1.0% / +1.7% |", table)


# ---------- 4. Stock: why ROE is low; the latest quarter's revenue ----------

def tata_series():
    return {"quarterlyStockholdersEquity": [{"date": "2026-09-30", "value": 22102 * CR}],
            "annualGoodwillAndOtherIntangibleAssets": [{"date": "2023-03-31", "value": 10875 * CR}, {"date": "2026-03-31", "value": 19017 * CR}],
            "quarterlyGoodwillAndOtherIntangibleAssets": [{"date": "2026-09-30", "value": 18938 * CR}],
            "quarterlyCashCashEquivalentsAndShortTermInvestments": [{"date": "2026-09-30", "value": 3229 * CR}]}


class LowRoeTests(unittest.TestCase):
    def test_acquisition_goodwill(self):
        text = sa.low_roe_reason({"roe": 0.074, "net_income": 1542 * CR}, tata_series(), 0.216)
        self.assertEqual(text, "ROE 7.4% is well below the peer median of 21.6%; likely reason from the balance sheet: goodwill and other intangibles "
                               "of ₹18,938 cr are 86% of shareholders' equity (30 Sep 2026), up from ₹10,875 cr at 31 Mar 2023, which points to "
                               "acquisitions; on the ₹3,164 cr of equity without them, the same profit would be a return of about 49%.")

    def test_only_when_well_below_peers(self):
        self.assertIsNone(sa.low_roe_reason({"roe": 0.18}, tata_series(), 0.216))
        self.assertIsNone(sa.low_roe_reason({"roe": 0.15}, tata_series(), 0.24))  # 9 points below, but not well below in proportion
        self.assertIsNone(sa.low_roe_reason({"roe": 0.074}, tata_series(), None))
        self.assertIsNone(sa.low_roe_reason({"roe": 0.074, "bank_like": True}, tata_series(), 0.216))
        self.assertIsNone(sa.low_roe_reason({"roe": 0.074}, {"quarterlyStockholdersEquity": tata_series()["quarterlyStockholdersEquity"]}, 0.216))

    def test_cash_or_neither(self):
        series = {**tata_series(), "quarterlyGoodwillAndOtherIntangibleAssets": [{"date": "2026-09-30", "value": 100 * CR}],
                  "quarterlyCashCashEquivalentsAndShortTermInvestments": [{"date": "2026-09-30", "value": 11000 * CR}]}
        self.assertIn("cash and short-term investments of ₹11,000 cr are 50% of equity", sa.low_roe_reason({"roe": 0.05}, series, 0.2))
        series["quarterlyCashCashEquivalentsAndShortTermInvestments"] = [{"date": "2026-09-30", "value": 100 * CR}]
        self.assertIn("neither goodwill nor cash is a large part of equity", sa.low_roe_reason({"roe": 0.05, "net_margin": 0.03}, series, 0.2))

    def test_summary_shows_the_latest_quarter_revenue_and_three_years(self):
        payload = tsa.fundamentals_payload(
            quarterlyTotalRevenue=[("2025-09-30", 100.0), ("2026-06-30", 110.0), ("2026-09-30", 120.0)],
            quarterlyNetIncome=[("2025-09-30", 20.0), ("2026-06-30", 25.0), ("2026-09-30", 30.0)])
        r = tsa.run(tsa.Feed(fundamentals=payload), peers=False, news=False, holdings=False, pe_history=False, dividends=False)
        text = sa.summary(r)
        self.assertIn("latest quarter revenue +20.0% and profit attributable to shareholders +50.0% vs a year earlier (2026-09-30)", text)
        self.assertIn("3-year CAGR FY2023–FY2026: revenue +11.2% a year", text)
        self.assertIn("| Revenue CAGR (3 years, FY2023–FY2026, Yahoo annual statements) | +11.2% |", sa.markdown(r))


# ---------- 5. Demerger parents: P/E on EPS that still includes the demerged businesses ----------

VEDL = [{"parent": "VEDL", "resulting_company": "Vedanta Aluminium Metal, Vedanta Power (one share of each)", "record_date": "2026-05-01",
         "appointed_date": "2026-05-01", "cost_split_pct": {"parent": 52.34, "resulting": 47.66}}]


class DemergerPeTests(unittest.TestCase):
    def test_trailing_eps_across_the_demerger(self):
        eps = {"date": "2026-09-30", "value": 54.8, "basis": "trailing 12 months"}
        quarters = [{"date": "2026-06-30", "value": 12.0}, {"date": "2026-09-30", "value": 9.3}]
        text = sa.demerger_pe_flag("VEDL", eps, 238.04, quarters, demergers=VEDL, today=date(2026, 11, 21))
        self.assertIn("includes about 7 months of Vedanta Aluminium Metal, Vedanta Power before the 01 May 2026 demerger", text)
        self.assertIn("48% of the pre-demerger cost went to the demerged business", text)
        self.assertIn("the P/E would be about 6.4", text)  # 238.04 / (4 × 9.3); the June quarter began before the demerger
        text = sa.demerger_pe_flag("VEDL", eps, 238.04, quarters[:1], demergers=VEDL, today=date(2026, 11, 21))
        self.assertNotIn("annualised", text)  # the June quarter still holds a month of the demerged businesses

    def test_no_caution_when_it_does_not_apply(self):
        late = {"date": "2027-06-30", "value": 30.0, "basis": "trailing 12 months"}
        self.assertIsNone(sa.demerger_pe_flag("VEDL", late, 238.0, demergers=VEDL))  # the whole year is after it
        small = [{**VEDL[0], "cost_split_pct": {"parent": 98.1, "resulting": 1.9}}]
        self.assertIsNone(sa.demerger_pe_flag("VEDL", {"date": "2026-09-30", "value": 1.0}, 1.0, demergers=small))
        self.assertIsNone(sa.demerger_pe_flag("VEDL", {"date": "2026-09-30", "value": 1.0}, 1.0, demergers=VEDL, today=date(2026, 4, 1)))
        self.assertIsNone(sa.demerger_pe_flag("OTHER", {"date": "2026-09-30", "value": 1.0}, 1.0, demergers=VEDL))

    def test_screener_flags_it_too(self):
        with mock.patch.object(sa, "demerger_pe_flag", return_value="P/E caution: test") as flag:
            snap = screener.snapshot("ALPHA", fetcher=tsc.Feed(), now=tsa.NOW)
        self.assertEqual(flag.call_args.args[0], "ALPHA")
        self.assertIn("P/E caution: test", snap["eps_flags"])
        self.assertEqual(snap["pe_demerger_note"], "P/E caution: test")


# ---------- 6. Screener: sectors left out, saved snapshots ----------

class CountingFeed(tsc.Feed):
    def __init__(self, **kw):
        super().__init__(**kw)
        self.urls = []

    def __call__(self, url):
        self.urls.append(url)
        return super().__call__(url)


IST_SAT = instant("2026-11-21T18:00:00+05:30")


class ExcludeSectorTests(unittest.TestCase):
    def test_excluded_sector_is_not_fetched(self):
        feed = CountingFeed()
        r = screener.screen("nifty500", ["pe<100"], fetcher=feed, now=tsa.NOW, exclude_sectors=["financial SERVICES"], cache=False)
        self.assertEqual(sorted(x["symbol"] for x in r["results"]), ["DELTA.NS", "EPSILON.NS"])
        self.assertEqual(r["excluded_by_sector"], ["ALPHA", "BETA", "GAMMA"])
        self.assertFalse([u for u in feed.urls if "/ALPHA" in u or "/BETA" in u])
        text = screener.markdown(r)
        self.assertIn("Sectors left out: financial SERVICES (3 stocks, not fetched).", text)
        self.assertIn("Run time", text)

    def test_unknown_sector_is_reported(self):
        r = screener.screen("nifty500", ["pe<100"], fetcher=tsc.Feed(), now=tsa.NOW, exclude_sectors=["Financials"], cache=False)
        self.assertEqual(r["evaluated"], 5)
        self.assertIn("no stock in this universe has the sector 'Financials'", screener.markdown(r))
        self.assertTrue(screener.sector_matches("Oil Gas & Consumable Fuels", ["oil gas and consumable fuels"]))
        self.assertFalse(screener.sector_matches("", ["x"]))

    def test_cli_flag(self):
        with mock.patch.object(screener, "screen", return_value={**screener.screen("nifty500", [], fetcher=tsc.Feed(), now=tsa.NOW, cache=False)}) as run, \
                mock.patch("sys.stdout"), redirect_stderr(io.StringIO()):
            screener.main(["--universe", "nifty500", "--exclude-sector", "Financial Services", "--exclude-sector", "Realty", "--no-cache"])
        self.assertEqual(run.call_args.kwargs["exclude_sectors"], ["Financial Services", "Realty"])
        self.assertIs(run.call_args.kwargs["cache"], False)


class SnapshotCacheTests(unittest.TestCase):
    def test_settled_sessions(self):
        self.assertEqual(screener.last_settled(IST_SAT).isoformat(), "2026-11-20T16:30:00+05:30")  # Friday's close
        monday = instant("2026-11-23T08:00:00+05:30")
        self.assertEqual(screener.last_settled(monday).isoformat(), "2026-11-20T16:30:00+05:30")
        self.assertTrue(screener.in_session(instant("2026-11-23T11:00:00+05:30")))
        self.assertFalse(screener.in_session(IST_SAT))

    def test_a_second_run_reads_saved_snapshots(self):
        with tempfile.TemporaryDirectory() as tmp:
            first = CountingFeed()
            r1 = screener.screen("nifty500", ["pe<100"], fetcher=first, now=IST_SAT, cache=tmp)
            self.assertEqual(r1["from_cache"], 0)
            second = CountingFeed()
            r2 = screener.screen("nifty500", ["pe<100"], fetcher=second, now=IST_SAT + timedelta(hours=1), cache=tmp)
            self.assertEqual(r2["from_cache"], 5)
            self.assertFalse([u for u in second.urls if "/v8/finance/chart/" in u or "fundamentals-timeseries" in u])
            self.assertEqual([x["symbol"] for x in r1["results"]], [x["symbol"] for x in r2["results"]])
            self.assertIn("5 of 5 stocks read from snapshots saved after the last completed session", screener.markdown(r2))
            # Monday's session makes them stale; during market hours the cache is not touched at all.
            monday = instant("2026-11-23T17:00:00+05:30")
            self.assertIsNone(screener.cached_snapshot(tmp, "DELTA", VARIANT, monday))
            self.assertIsNone(screener.cached_snapshot(tmp, "DELTA", VARIANT, instant("2026-11-23T11:00:00+05:30")))
            self.assertIsNotNone(screener.cached_snapshot(tmp, "DELTA", VARIANT, IST_SAT + timedelta(hours=2)))

    def test_failures_are_not_saved(self):
        with tempfile.TemporaryDirectory() as tmp:
            screener.save_snapshot(tmp, "X", "v", IST_SAT, {"fundamentals_failed": True})
            screener.save_snapshot(tmp, "Y", "v", instant("2026-11-23T11:00:00+05:30"), {"price": 1.0})
            self.assertEqual(list(Path(tmp).iterdir()), [])  # nothing saved yet
            early = instant("2026-11-20T08:00:00+05:30")  # Friday before the open
            screener.save_snapshot(tmp, "E", "v", early, {"price": 2.0})
            self.assertEqual(screener.cached_snapshot(tmp, "E", "v", early + timedelta(minutes=30)), {"price": 2.0})
            self.assertIsNone(screener.cached_snapshot(tmp, "E", "v", instant("2026-11-20T17:00:00+05:30")))  # Friday's session since
            screener.save_snapshot(tmp, "Z", "v", IST_SAT, {"price": 1.0})
            self.assertEqual(screener.cached_snapshot(tmp, "Z", "v", IST_SAT), {"price": 1.0})
            self.assertIsNone(screener.cached_snapshot(tmp, "Z", "v", IST_SAT + timedelta(hours=21)))  # too old whatever the calendar

    def test_tests_and_offline_runs_never_use_the_user_cache(self):
        with mock.patch.object(screener, "snapshot_store", side_effect=AssertionError("user cache touched")):
            screener.screen("nifty500", ["pe<100"], fetcher=tsc.Feed(), now=IST_SAT)


VARIANT = "d0g1"  # no dividend history; NSE rupee growth on (the default columns show revenue growth)


if __name__ == "__main__":
    unittest.main()
