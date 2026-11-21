"""Offline contracts for the shareholding-pattern / promoter-pledge report and the NSE disclosure feed parsing; never contacts the network."""
from contextlib import redirect_stdout
import functools
import io
import json
from pathlib import Path
import sys
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
from core import Block, instant
import research
import shareholding as sh

NOW = instant("2026-11-18T10:00:00+05:30")
MASTER = "https://www.nseindia.in/api/corporate-share-holdings-master?index=equities&symbol=ADANIPORTS"
PLEDGE = "https://www.nseindia.in/api/corporate-pledgedata?index=equities&symbol=ADANIPORTS"
LATEST = "https://nsearchives.nseindia.in/corporate/xbrl/SHP_2_10102026_WEB.xml"
PREVIOUS = "https://nsearchives.nseindia.in/corporate/xbrl/SHP_1_10072026_WEB.xml"


def context(cid, member=None, typed=False):
    dims = (f'<xbrli:segment><xbrldi:explicitMember dimension="in-bse-shp:CategoryOfShareholdersAxis">in-bse-shp:{member}</xbrldi:explicitMember>'
            + ('<xbrldi:typedMember dimension="in-bse-shp:X"><in-bse-shp:D>1</in-bse-shp:D></xbrldi:typedMember>' if typed else "")
            + "</xbrli:segment>") if member else ""
    return (f'<xbrli:context id="{cid}"><xbrli:entity><xbrli:identifier scheme="s">532921</xbrli:identifier>{dims}</xbrli:entity>'
            f'<xbrli:period><xbrli:instant>2026-09-30</xbrli:instant></xbrli:period></xbrli:context>')


def shp(holdings, pledged=None, encumbered=None, flags=("false", "false", "false"), day="2026-09-30"):
    """A minimal NSE shareholding-pattern XBRL: holdings = {member: (shares, fraction)}."""
    parts = ['<?xml version="1.0" encoding="UTF-8"?><xbrli:xbrl xmlns:xbrli="http://www.xbrl.org/2003/instance" '
             'xmlns:xbrldi="http://xbrl.org/2006/xbrldi" xmlns:in-bse-shp="http://www.bseindia.com/xbrl/shp">', context("MainI")]
    for n, (member, (shares, fraction)) in enumerate(holdings.items()):
        cid = f"C{n}"
        parts.append(context(cid, member))
        parts.append(f'<in-bse-shp:NumberOfShares contextRef="{cid}" unitRef="shares" decimals="0">{shares}</in-bse-shp:NumberOfShares>')
        parts.append(f'<in-bse-shp:ShareholdingAsAPercentageOfTotalNumberOfShares contextRef="{cid}" unitRef="pure" decimals="4">{fraction}</in-bse-shp:ShareholdingAsAPercentageOfTotalNumberOfShares>')
        if member == "ShareholdingOfPromoterAndPromoterGroupMember":
            if pledged is not None:
                parts.append(f'<in-bse-shp:NumberOfSharesEncumberedUnderPledged contextRef="{cid}" unitRef="shares" decimals="0">{pledged}</in-bse-shp:NumberOfSharesEncumberedUnderPledged>')
            if encumbered is not None:
                parts.append(f'<in-bse-shp:NumberOfSharesEncumbered contextRef="{cid}" unitRef="shares" decimals="0">{encumbered}</in-bse-shp:NumberOfSharesEncumbered>')
        if member == "ShareholdingPatternMember":
            parts.append(f'<in-bse-shp:NumberOfShareholders contextRef="{cid}" unitRef="pure" decimals="0">1070249</in-bse-shp:NumberOfShareholders>')
    parts.append(context("Dim", "MutualFundsOrUTIMember", typed=True))  # named holders: must not overwrite the category total
    parts.append('<in-bse-shp:ShareholdingAsAPercentageOfTotalNumberOfShares contextRef="Dim" unitRef="pure" decimals="4">0.0101</in-bse-shp:ShareholdingAsAPercentageOfTotalNumberOfShares>')
    for tag, value in zip(("WhetherAnySharesHeldByPromotersAreEncumberedUnderPledged", "WhetherAnySharesHeldByPromotersAreEncumberedUnderNonDisposalUndertaking",
                           "WhetherAnySharesHeldByPromotersAreEncumberedOtherThanByWayOfPledgeOrNDU"), flags):
        parts.append(f'<in-bse-shp:{tag} contextRef="MainI">{value}</in-bse-shp:{tag}>')
    parts.append(f'<in-bse-shp:DateOfReport contextRef="MainI">{day}</in-bse-shp:DateOfReport><in-bse-shp:Symbol contextRef="MainI">ADANIPORTS</in-bse-shp:Symbol>')
    parts.append("</xbrli:xbrl>")
    return "".join(parts).encode()


TOTAL = 2303959098
LATEST_HOLDINGS = {
    "ShareholdingPatternMember": (TOTAL, "1"), "ShareholdingOfPromoterAndPromoterGroupMember": (1521337573, "0.6603"),
    "PublicShareholdingMember": (782621525, "0.3397"), "InstitutionsForeignMember": (367464336, "0.1595"),
    "InstitutionsForeignPortfolioInvestorCategoryOneMember": (344147864, "0.1494"), "InstitutionsForeignPortfolioInvestorCategoryTwoMember": (23316472, "0.0101"),
    "InstitutionsDomesticMember": (305318319, "0.1325"), "MutualFundsOrUTIMember": (113967952, "0.0495"),
    "InsuranceCompaniesMember": (165000000, "0.0717"), "GovernmentsMember": (0, "0"), "NonInstitutionsMember": (109834850, "0.0477"),
    "ResidentIndividualShareholdersHoldingNominalShareCapitalUpToRsTwoLakhMember": (71900000, "0.0312"),
}
PREVIOUS_HOLDINGS = {**LATEST_HOLDINGS, "InstitutionsForeignMember": (358966000, "0.1558"), "InstitutionsDomesticMember": (313300000, "0.1360"),
                     "InstitutionsForeignPortfolioInvestorCategoryOneMember": (335650000, "0.1457"), "MutualFundsOrUTIMember": (120700000, "0.0524")}


def master(rows=None):
    rows = rows or [
        {"date": "30-SEP-2026", "broadcastDate": "10-OCT-2026 15:51:00", "pr_and_prgrp": "66.03", "public_val": "33.97", "xbrl": LATEST,
         "name": "Adani Ports and Special Economic Zone Limited", "revisedData": "N"},
        {"date": "30-JUN-2026", "broadcastDate": "10-JUL-2026 20:40:37", "pr_and_prgrp": "66.03", "public_val": "33.97", "xbrl": PREVIOUS,
         "name": "Adani Ports and Special Economic Zone Limited"},
        {"date": "23-DEC-2025", "broadcastDate": "24-DEC-2025 10:53:08", "pr_and_prgrp": "68.02", "public_val": "31.98", "xbrl": "https://x/pre-issue.xml"},
        {"date": "31-MAR-2026", "broadcastDate": "10-APR-2026 11:38:33", "pr_and_prgrp": "68.02", "public_val": "31.98", "xbrl": None}]
    return json.dumps(rows).encode()


class Feed:
    def __init__(self, latest=None, previous=None, pledge=True, fail=()):
        self.files = {MASTER: master(), LATEST: latest or shp(LATEST_HOLDINGS, pledged=0, encumbered=0),
                      PREVIOUS: previous or shp(PREVIOUS_HOLDINGS, pledged=0, encumbered=0, day="2026-06-30")}
        if pledge:
            self.files[PLEDGE] = json.dumps({"data": [{"shp": "30-Sep-2026", "broadcastDt": "17-Nov-2026 16:31:09", "percPromoterHolding": "    66.03",
                                                       "percPromoterShares": "     0.00", "percTotShares": "     0.00", "totPromoterShares": "0",
                                                       "percSharesPledged": "0.64"}]}).encode()
        self.fail = set(fail)
        self.urls = []

    def __call__(self, url):
        self.urls.append(url)
        if url in self.fail or url not in self.files:
            raise Block("NSE: source unavailable")
        return self.files[url]


def report(feed=None):
    return sh.shareholding("adaniports", fetcher=feed or Feed(), clock=lambda: NOW)


class ShareholdingTests(unittest.TestCase):
    def test_latest_quarter_categories_and_quarter_on_quarter_change(self):
        r = report()
        self.assertEqual((r["quarter"], r["previous_quarter"], r["filed"]), ("2026-09-30", "2026-06-30", "2026-10-10"))
        rows = {x["key"]: x for x in r["rows"]}
        self.assertAlmostEqual(rows["promoter"]["pct"], 66.03)
        self.assertAlmostEqual(rows["fii"]["pct"], 15.95)
        self.assertAlmostEqual(rows["fii"]["change_pts"], 0.37)
        self.assertAlmostEqual(rows["dii"]["change_pts"], -0.35)
        self.assertAlmostEqual(rows["mf"]["pct"], 4.95)  # the category total, not a named holder's 1.01%
        self.assertAlmostEqual(rows["non_institutions"]["pct"], 4.77)
        self.assertNotIn("government", rows)  # zero in both quarters: noise
        self.assertNotIn("fpi", rows)  # all foreign institutional holding is FPI here, so the "of which" line would repeat it
        self.assertEqual(r["holders"], 1070249)
        self.assertEqual([t["quarter"] for t in r["promoter_trend"]], ["2026-09-30", "2026-06-30", "2026-03-31"])  # ad-hoc 23 Dec filing skipped

    def test_pledge_from_the_filing_and_the_nse_disclosure(self):
        r = report(Feed(latest=shp(LATEST_HOLDINGS, pledged=132730505, encumbered=140000000, flags=("true", "true", "false"))))
        p = r["pledge"]
        self.assertAlmostEqual(p["pledged_pct_of_promoter"], 132730505 / 1521337573 * 100)
        self.assertAlmostEqual(p["pledged_pct_of_total"], 132730505 / TOTAL * 100)
        self.assertAlmostEqual(p["other_encumbered_shares"], 140000000 - 132730505)
        text = sh.markdown(r)
        self.assertIn("8.72% of promoter shares are pledged (5.76% of all shares)", text)
        self.assertIn("another 0.48% is encumbered without a formal pledge", text)
        self.assertIn("NSE pledge disclosure (SEBI SAST Reg. 31) as of 30 Sep 2026", text)

    def test_no_pledge_is_said_plainly_and_summary_is_short(self):
        r = report()
        self.assertEqual(sh.headline(r), "Promoters hold 66.03% (unchanged vs 30 Jun 2026). 0.00% of promoter shares are pledged; none otherwise encumbered.")
        text = sh.summary(r)
        self.assertLessEqual(len(text.splitlines()), 7)
        self.assertIn("Foreign institutions 15.95% (+0.37 pts)", text)
        self.assertIn("Promoter holding over Mar 2026–Sep 2026: 68.02% → 66.03%, peak 68.02% (Mar 2026)", text)
        self.assertIn("biggest fall -1.99 pts in the quarter to Jun 2026", text)
        self.assertIn("Domestic institutions 13.25% (-0.35 pts) — mutual funds 4.95% (-0.29 pts), insurers 7.17%", text)

    def test_unreadable_filing_falls_back_to_nse_totals_and_says_so(self):
        r = report(Feed(latest=b"<html>not xbrl</html>"))
        self.assertEqual([x["key"] for x in r["rows"]], ["promoter", "public"])
        self.assertIn("2026-09-30", r["errors"])
        self.assertIn("## Data gaps", sh.markdown(r))
        r = report(Feed(pledge=False))
        self.assertIn("pledge_disclosure", r["errors"])
        self.assertIsNotNone(r["pledge"])  # the filing's own pledge figures still stand

    def test_old_format_institutions_are_split_into_domestic_and_foreign(self):
        old = {"ShareholdingPatternMember": (1000, "1"), "ShareholdingOfPromoterAndPromoterGroupMember": (600, "0.6"),
               "PublicShareholdingMember": (400, "0.4"), "InstitutionsMember": (250, "0.25"),
               "InstitutionsForeignPortfolioInvestorCategoryOneMember": (100, "0.1")}
        parsed = sh.parse_xbrl(shp(old))
        self.assertAlmostEqual(parsed["categories"]["dii"]["pct"], 15.0)
        self.assertAlmostEqual(parsed["categories"]["fii"]["pct"], 10.0)

    def test_entities_and_bad_symbols_are_refused(self):
        with self.assertRaises(Block):
            sh.parse_xbrl(b'<!DOCTYPE x [<!ENTITY a "b">]><xbrl/>')
        with self.assertRaises(Block):
            sh.shareholding("ADANI PORTS; rm", fetcher=Feed(), clock=lambda: NOW)
        empty = Feed()
        empty.files[MASTER] = b"[]"
        with self.assertRaisesRegex(Block, "no quarterly shareholding filings"):
            sh.shareholding("ADANIPORTS", fetcher=empty, clock=lambda: NOW)

    def test_only_allowlisted_nse_endpoints_are_fetched(self):
        good = [MASTER, PLEDGE, LATEST, "https://www.nseindia.in/api/corporates-corporateActions?index=equities&symbol=M%26M",
                "https://nsearchives.nseindia.in/corporate/xbrl/INTEGRATED_FILING_INDAS_1750537_30102026093254_WEB.xml",
                "https://www.nseindia.in/api/integrated-filing-results?index=equities&symbol=RELIANCE"]
        for url in good:
            self.assertTrue(sh.allowed(url), url)
        bad = ["http://www.nseindia.in/api/corporate-pledgedata?index=equities&symbol=X", "https://www.nseindia.in/api/quote-equity?symbol=X",
               "https://www.nseindia.in/api/corporate-pledgedata?index=equities&symbol=X&extra=1", "https://evil.example/corporate/xbrl/SHP_1.xml",
               "https://nsearchives.nseindia.in/corporate/xbrl/SHP_1.xml?x=1", "https://nsearchives.nseindia.in/content/other.xml"]
        for url in bad:
            self.assertFalse(sh.allowed(url), url)
            with self.assertRaises(Block):
                sh.nse_request(url)

    def test_cli_json_and_summary(self):
        out = io.StringIO()
        offline = functools.partial(sh.shareholding, fetcher=Feed(), clock=lambda: NOW)
        with mock.patch.object(sh, "shareholding", offline), redirect_stdout(out):
            self.assertEqual(sh.main(["ADANIPORTS", "--format", "json"]), 0)
        data = json.loads(out.getvalue())
        self.assertEqual(data["schema"], "shareholding_v1")
        self.assertFalse(data["execution_eligible"])


class DisclosureFeedTests(unittest.TestCase):
    """research.py disclosures: NSE items carry local timestamps and the company name as title; nothing may claim 'no events'."""
    ANNOUNCEMENTS = ("<rss><channel><item><title>JSW Steel Limited</title>"
                     "<link>https://nsearchives.nseindia.com/corporate/JSWSTEEL_18112026093700_SAST.pdf</link>"
                     "<description>JSW Steel Limited has informed the Exchange about disclosure under SEBI Takeover Regulations |SUBJECT: Disclosure under SAST</description>"
                     "<pubDate>18-Nov-2026 09:37:12</pubDate></item>"
                     "<item><title>No Link Ltd</title><link/><description>x |SUBJECT: Declaration of NAV</description><pubDate>18-Nov-2026 09:47:00</pubDate></item>"
                     "</channel></rss>").encode()

    def fetch(self, url):
        if "Online_announcements" in url:
            return self.ANNOUNCEMENTS
        return b"<rss><channel></channel></rss>"

    def test_nse_local_times_parse_and_symbols_come_from_attachment_names(self):
        r = research.news(["JSWSTEEL.NS"], NOW, fetcher=self.fetch, disclosure_only=True)
        self.assertEqual(len(r["records"]), 1)
        rec = r["records"][0]
        self.assertEqual(rec["headline"], "JSW Steel Limited: Disclosure under SAST")
        self.assertEqual(rec["symbols"], ["JSWSTEEL.NS"])
        self.assertIn("SAST", rec["categories"])
        self.assertEqual(r["unparsed_items"], 1)  # the item without a link
        self.assertEqual(r["clearance"], "none")
        self.assertIn("does not mean there were no filings", r["clearance_note"])
        self.assertIn("shareholding.py", r["clearance_note"])


def named(cid, axis, name, fraction, promoter=False, row_kind=None):
    """A named holder as NSE files it: the name in a duration context, the holding in an instant context, both
    carrying the same typed-dimension value."""
    def ctx(i, period):
        return (f'<xbrli:context id="{i}"><xbrli:entity><xbrli:identifier scheme="s">532921</xbrli:identifier></xbrli:entity>'
                f'<xbrli:period>{period}</xbrli:period><xbrli:scenario><xbrldi:typedMember dimension="in-bse-shp:{axis}">'
                f'<in-bse-shp:D>{cid}</in-bse-shp:D></xbrldi:typedMember></xbrli:scenario></xbrli:context>')
    parts = [ctx("D_" + cid, "<xbrli:startDate>2026-07-01</xbrli:startDate><xbrli:endDate>2026-09-30</xbrli:endDate>"),
             ctx(cid, "<xbrli:instant>2026-09-30</xbrli:instant>"),
             f'<in-bse-shp:NameOfTheShareholder contextRef="D_{cid}">{name}</in-bse-shp:NameOfTheShareholder>',
             f'<in-bse-shp:ShareholdingAsAPercentageOfTotalNumberOfShares contextRef="{cid}" unitRef="pure" decimals="4">{fraction}</in-bse-shp:ShareholdingAsAPercentageOfTotalNumberOfShares>']
    if promoter:
        parts.append(f'<in-bse-shp:TypeOfPromoterShareholding contextRef="D_{cid}">Promoter Group</in-bse-shp:TypeOfPromoterShareholding>')
    if row_kind:
        parts.append(f'<in-bse-shp:WhetherACategoryOrMoreThan1PercentageOfShareholding contextRef="D_{cid}">{row_kind}</in-bse-shp:WhetherACategoryOrMoreThan1PercentageOfShareholding>')
    return "".join(parts)


HOLDERS = [("Ins15", "DetailsOfSharesHeldByInsuranceCompaniesAxis", "Life Insurance Corporation Of India", "0.0643", False, None),
           ("Fpi15", "DetailsOfSharesHeldByInstitutionsForeignPortfolioInvestorOneAxis",
            "Goldman Sachs Trust Ii - Goldman Sachs Gqg Partners International Opportunities Fund", "0.0172", False, None),
           ("Mf15", "DetailsOfSharesHeldByMutualFundsOrUTIAxis", "Sbi Nifty 50 Etf", "0.0145", False, None),
           ("Fpi16", "DetailsOfSharesHeldByInstitutionsForeignPortfolioInvestorOneAxis", "New World Fund Inc", "0.0128", False, None),
           ("Prom15", "DetailsOfSharesHeldByOtherForeignShareholdersAxis", "Carmichael Rail And Port Singapore Holdings", "0.0624", True, None),
           ("Cat16", "DetailsOfSharesHeldByOtherNonInstitutionsAxis", "Body Corp-Ltd Liability Partnership", "0.0004", False, "Category"),
           ("Small", "DetailsOfSharesHeldByMutualFundsOrUTIAxis", "Small Fund", "0.0080", False, None)]


def with_holders(body, holders):
    return body.replace(b"</xbrli:xbrl>", "".join(named(*h) for h in holders).encode() + b"</xbrli:xbrl>")


class Round2ShareholdingTests(unittest.TestCase):
    def feed(self):
        trend = [("30-SEP-2026", "66.03"), ("30-JUN-2026", "66.03"), ("31-MAR-2026", "68.02"), ("31-DEC-2025", "68.02"),
                 ("30-SEP-2025", "65.89"), ("30-JUN-2025", "65.89"), ("31-MAR-2025", "65.89"), ("31-DEC-2024", "65.89")]
        feed = Feed(latest=with_holders(shp(LATEST_HOLDINGS, pledged=0, encumbered=0), HOLDERS),
                    previous=with_holders(shp(PREVIOUS_HOLDINGS, pledged=0, encumbered=0, day="2026-06-30"),
                                          [h if h[0] != "Fpi15" else (*h[:3], "0.0193", False, None) for h in HOLDERS if h[0] != "Fpi16"]))
        rows = [{"date": d, "broadcastDate": "10-OCT-2026 15:51:00", "pr_and_prgrp": p, "public_val": "33.97",
                 "xbrl": LATEST if i == 0 else PREVIOUS if i == 1 else None, "name": "Adani Ports and Special Economic Zone Limited"}
                for i, (d, p) in enumerate(trend)]
        feed.files[MASTER] = master(rows)
        return feed

    def test_public_holders_above_one_percent_are_named(self):
        r = report(self.feed())
        names = [h["name"] for h in r["large_public_holders"]]
        self.assertEqual(names[0], "Life Insurance Corporation Of India")
        self.assertEqual(len(names), 4)  # not the promoter, not the category row, not the 0.8% fund
        self.assertEqual(r["large_public_holders"][0]["kind"], "insurer")
        gqg = r["large_public_holders"][1]
        self.assertAlmostEqual(gqg["previous_pct"], 1.93)
        line = sh.holders_line(r)
        self.assertIn("Life Insurance Corporation Of India 6.43%", line)
        self.assertIn("Goldman Sachs GQG Partners International Opportunities Fund 1.72% (-0.21 pts)", line)
        self.assertIn("SBI Mutual Fund (all schemes) 1.45%", line)
        self.assertIn("New World Fund Inc 1.28% (new above 1%)", line)
        self.assertIn("## Public shareholders above 1%", sh.markdown(r))

    def test_trend_shows_peak_low_and_the_largest_quarterly_move(self):
        r = report(self.feed())
        t = r["promoter_trend_shape"]
        self.assertEqual((t["peak"]["promoter_pct"], t["trough"]["promoter_pct"]), (68.02, 65.89))
        self.assertAlmostEqual(t["largest_move"]["pts"], 2.13)
        self.assertEqual(t["largest_move"]["quarter"], "2025-12-31")
        text = sh.summary(r)
        self.assertIn("Promoter holding over Dec 2024–Sep 2026: 65.89% → 66.03%, peak 68.02% (Mar 2026), low 65.89% (Dec 2024)", text)
        self.assertIn("biggest rise +2.13 pts in the quarter to Dec 2025, biggest fall -1.99 pts in the quarter to Jun 2026", text)
        self.assertAlmostEqual(t["largest_fall"]["pts"], -1.99)
        self.assertIn("Public holders above 1%:", text)
        self.assertLessEqual(len(text.splitlines()), 8)  # every promoter entity is named on one line
        flat = sh.trend_shape([{"quarter": "2026-09-30", "promoter_pct": 50.0}, {"quarter": "2026-06-30", "promoter_pct": 50.0}])
        self.assertTrue(flat["flat"])
        self.assertIsNone(flat["largest_move"])

    def test_results_xbrl_for_finance_companies_is_allowed(self):
        for kind in ("NBFC_INDAS", "BANKING", "LI", "GI"):
            self.assertTrue(sh.allowed(f"https://nsearchives.nseindia.in/corporate/xbrl/INTEGRATED_FILING_{kind}_1739400_15102026080724_WEB.xml"), kind)
        self.assertFalse(sh.allowed("https://nsearchives.nseindia.in/corporate/xbrl/INTEGRATED_FILING_GOVERNANCE_1_WEB.xml"))
        self.assertFalse(sh.allowed("https://nsearchives.nseindia.in/corporate/xbrl/OTHER_1_WEB.xml"))


if __name__ == "__main__":
    unittest.main()
