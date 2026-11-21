"""Offline contracts for the round-3 stock fixes: NSE retries, honest shareholding comparisons, new-listing patterns,
split-symbol digests, distorted year-ago quarters, rupee growth for dollar reporters, news gaps, time limits for the
optional sections, and screener footnotes. Never contacts the network."""
from contextlib import redirect_stdout
import io
import json
from pathlib import Path
import sys
import time
import unittest
import urllib.error

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT / "tests"))
from core import Block
import screener
import shareholding as sh
import stock_analysis as sa
import test_screener as ts
import test_shareholding as tsh
import test_stock_analysis as tsa
from test_stock_analysis import NOW


# ---------- 1. NSE retries ----------

class Opener:
    def __init__(self, *outcomes):
        self.outcomes, self.calls = list(outcomes), 0

    def __call__(self, req, timeout):
        self.calls += 1
        out = self.outcomes.pop(0)
        if isinstance(out, BaseException):
            raise out
        return out


class NseRetryTests(unittest.TestCase):
    URL = "https://www.nseindia.in/api/corporate-pledgedata?index=equities&symbol=ADANIPORTS"

    def test_transient_failures_are_retried_with_backoff(self):
        waits, opener = [], Opener(urllib.error.URLError("reset"), TimeoutError("slow"), b"ok")
        self.assertEqual(sh.nse_request(self.URL, opener=opener, sleep=waits.append), b"ok")
        self.assertEqual((opener.calls, waits), (3, [0.5, 1.5]))

    def test_gives_up_after_three_attempts_and_never_retries_a_404(self):
        opener = Opener(*[urllib.error.URLError("down")] * 3)
        with self.assertRaisesRegex(Block, "source unavailable"):
            sh.nse_request(self.URL, opener=opener, sleep=lambda _: None)
        self.assertEqual(opener.calls, 3)
        missing = Opener(urllib.error.HTTPError(self.URL, 404, "not found", {}, None), b"never")
        with self.assertRaises(Block):
            sh.nse_request(self.URL, opener=missing, sleep=lambda _: None)
        self.assertEqual(missing.calls, 1)
        throttled = Opener(urllib.error.HTTPError(self.URL, 429, "slow down", {}, None), b"ok")
        self.assertEqual(sh.nse_request(self.URL, opener=throttled, sleep=lambda _: None), b"ok")

    def test_results_list_pages_are_allowed_only_there(self):
        self.assertTrue(sh.allowed("https://www.nseindia.in/api/integrated-filing-results?index=equities&symbol=INFY&page=2"))
        for bad in ("https://www.nseindia.in/api/integrated-filing-results?index=equities&symbol=INFY&page=1",
                    "https://www.nseindia.in/api/integrated-filing-results?index=equities&symbol=INFY&page=22",
                    "https://www.nseindia.in/api/corporate-pledgedata?index=equities&symbol=INFY&page=2",
                    "https://www.nseindia.in/api/integrated-filing-results?index=equities&page=2&symbol=INFY"):
            self.assertFalse(sh.allowed(bad), bad)


# ---------- 2. Shareholding: honest comparisons, holder count, pledge date, new listings ----------

OLDER = "https://nsearchives.nseindia.in/corporate/xbrl/SHP_0_10042026_WEB.xml"


def holders_feed(previous=None, older=None):
    feed = tsh.Feed(latest=tsh.with_holders(tsh.shp(tsh.LATEST_HOLDINGS, pledged=0, encumbered=0), tsh.HOLDERS[:4]),
                    previous=previous or tsh.with_holders(tsh.shp(tsh.PREVIOUS_HOLDINGS, pledged=0, encumbered=0, day="2026-06-30"), tsh.HOLDERS[:2]))
    rows = json.loads(tsh.master())
    if older:
        rows[3]["xbrl"] = OLDER
        feed.files[OLDER] = older
    feed.files[tsh.MASTER] = json.dumps(rows).encode()
    return feed


class ShareholdingRound3Tests(unittest.TestCase):
    def test_new_above_one_percent_only_when_the_previous_filing_was_read(self):
        r = tsh.report(holders_feed())
        self.assertTrue(r["previous_read"])
        self.assertIn("SBI Mutual Fund (all schemes) 1.45% (new above 1%)", sh.holders_line(r))
        r = tsh.report(holders_feed(previous=b"<html>not xbrl</html>"))
        self.assertFalse(r["previous_read"])
        self.assertIsNone(r["compared_quarter"])
        line = sh.holders_line(r)
        self.assertNotIn("new above 1%", line)
        self.assertIn("change vs the previous quarter (Jun 2026) unavailable: its filing could not be read", line)
        self.assertNotIn("vs 30 Jun 2026", sh.headline(r))

    def test_an_unreadable_previous_quarter_falls_back_to_the_next_older_one_and_says_so(self):
        older = tsh.with_holders(tsh.shp(tsh.PREVIOUS_HOLDINGS, pledged=0, encumbered=0, day="2026-03-31"), tsh.HOLDERS[:1])
        r = tsh.report(holders_feed(previous=b"<html>not xbrl</html>", older=older))
        self.assertEqual((r["previous_quarter"], r["compared_quarter"]), ("2026-06-30", "2026-03-31"))
        self.assertIn("2026-06-30", r["errors"])
        self.assertIn("unchanged vs 31 Mar 2026", sh.headline(r))
        line = sh.holders_line(r)
        self.assertIn("Goldman Sachs GQG Partners International Opportunities Fund 1.72% (new above 1%)", line)
        self.assertIn("changes are vs Mar 2026 because the Jun 2026 filing could not be read", line)
        text = sh.markdown(r)
        self.assertIn("compared with 31 Mar 2026", text)

    def test_summary_shows_holder_count_and_the_pledge_disclosure_dates(self):
        text = sh.summary(tsh.report())
        self.assertIn("Shareholders: 10,70,249", text)
        self.assertIn("NSE's promoter-pledge disclosure (as of 30 Sep 2026, updated 17 Nov 2026) agrees.", text)
        self.assertLessEqual(len(text.splitlines()), 7)
        feed = tsh.Feed()
        feed.files[tsh.PLEDGE] = json.dumps({"data": [{"shp": "30-Sep-2026", "broadcastDt": "17-Nov-2026 16:31:09", "percPromoterShares": "4.10",
                                                       "percTotShares": "2.70"}]}).encode()
        self.assertIn("shows 4.10% of promoter shares pledged", sh.summary(tsh.report(feed)))

    def test_a_new_listing_uses_the_pattern_filed_on_listing(self):
        url = "https://nsearchives.nseindia.in/corporate/xbrl/SHP_1747552_28102026022548_WEB.xml"
        holdings = {"ShareholdingPatternMember": (9209030830, "1"), "ShareholdingOfPromoterAndPromoterGroupMember": (5937841645, "0.6448"),
                    "PublicShareholdingMember": (3271189185, "0.3552"), "InstitutionsForeignMember": (2996374746, "0.3254"),
                    "ForeignDirectInvestmentMember": (2943831717, "0.3197")}
        body = tsh.with_holders(tsh.shp(holdings, day="2026-10-27"),
                                [("Ril", "DetailsOfSharesHeldByOthersIndianShareholdersAxis", "RELIANCE INDUSTRIES LIMITED", "0.6448", True, None),
                                 ("Meta", "DetailsOfSharesHeldByForeignDirectInvestmentAxis", "JAADHU HOLDINGS, LLC", "0.0969", False, None)])
        master = json.dumps([{"date": "27-OCT-2026", "broadcastDate": "28-OCT-2026 16:55:43", "pr_and_prgrp": "64.48", "public_val": "35.52",
                              "xbrl": url, "name": "Jio Platforms Limited", "symbol": "JPL"}]).encode()
        files = {"https://www.nseindia.in/api/corporate-share-holdings-master?index=equities&symbol=JPL": master, url: body}

        def fetch(u):
            if u not in files:
                raise Block("NSE: source unavailable")
            return files[u]
        r = sh.shareholding("JPL", fetcher=fetch, clock=lambda: NOW)
        self.assertEqual((r["filing_kind"], r["quarter"], r["next_filing_due"]), ("adhoc", "2026-10-27", "2027-01-21"))
        text = sh.summary(r)
        self.assertIn("shareholding pattern dated 27 Oct 2026 (filed 28 Oct 2026; filed on listing or for a share issue)", text)
        self.assertIn("NSE lists no quarterly shareholding pattern for JPL yet (the first, for the quarter to 31 Dec 2026, is due by 21 Jan 2027)", text)
        self.assertIn("Promoter: Reliance Industries Limited holds all 64.48% of the promoter holding (about 5,93,79,83,079 of 9,20,90,30,830 shares).", text)
        self.assertIn("Foreign institutions 32.54% — of which foreign direct investment 31.97%", text)
        self.assertNotIn("quarter ended", text)
        files[next(iter(files))] = b"[]"
        with self.assertRaisesRegex(Block, "no quarterly shareholding filings for JPL, and no pattern filed on listing either"):
            sh.shareholding("JPL", fetcher=fetch, clock=lambda: NOW)


# ---------- 3. Split symbols and distorted year-ago quarters ----------

class SplitSymbolDigestTests(unittest.TestCase):
    def test_summary_mode_prints_each_successors_own_digest(self):
        entry = {"old": "SPLITCO", "kind": "demerger", "date": "2025-10-14", "reason": "Split Co demerged its trucks.",
                 "successors": [{"symbol": "ALPHA", "what": "Alpha: cars"}, {"symbol": "BETA", "what": "Beta: trucks"}],
                 "source": "https://example.invalid/splitco"}
        original = sa.analyse

        def analyse(symbol, **k):
            if symbol == "SPLITCO":
                raise sa.SymbolChanged(entry)
            return original(symbol, **{**k, "fetcher": ts.Feed(), "news_fetcher": ts.Feed(), "clock": lambda: NOW})
        sa.analyse = analyse
        out = io.StringIO()
        try:
            with redirect_stdout(out):
                self.assertEqual(sa.main(["SPLITCO", "--summary"]), 0)
        finally:
            sa.analyse = original
        text = out.getvalue()
        self.assertTrue(text.startswith("> ⚠ SPLITCO no longer trades"))
        self.assertEqual(text.count("no longer trades"), 1)
        for sym in ("ALPHA", "BETA"):
            self.assertIn(f"{sym} Ltd ({sym}.NS) — prices as of", text)
        self.assertEqual(text.count("Valuation: P/E"), 2)       # full digests, not a thin table
        self.assertEqual(text.count("Trend: "), 2)
        self.assertEqual(text.count("Research only; not investment advice."), 1)
        self.assertIn("# Comparison — ALPHA, BETA", text)


TMCV_NSE = {"latest": {"quarter": "2026-09-30", "profit": 12.57e9, "revenue": 253.38e9},
            "year_ago": {"quarter": "2025-09-30", "profit": -8.67e9, "revenue": 185.85e9, "pbt": -5.49e9, "tax": 3.06e9,
                         "exceptional": -0.10e9, "other_income": 1.72e9}}


class DistortedBaseTests(unittest.TestCase):
    def fund(self, latest=12.57e9, ago=-8.67e9):
        return {"quarter_profit_values": (latest, ago), "quarter_year_ago": "2025-09-30"}

    def test_tax_on_a_pre_tax_loss_flags_the_turnaround(self):
        f = self.fund()
        note = sa.flag_distorted_base(f, TMCV_NSE)
        self.assertEqual(note, "The year-ago quarter (2025-09-30) was distorted by one-offs (tax of ₹306 cr on a pre-tax loss of ₹549 cr); "
                               "the swing overstates the turnaround.")

    def test_exceptional_charges_and_negative_other_income_count_too(self):
        nse = {"year_ago": {**TMCV_NSE["year_ago"], "tax": -1.37e9, "exceptional": -3.0e9, "other_income": -18.65e9}}
        note = sa.flag_distorted_base(self.fund(), nse)
        self.assertIn("exceptional charges of ₹300 cr", note)
        self.assertIn("negative other income of ₹1,865 cr", note)

    def test_no_flag_without_a_sign_change_or_one_offs(self):
        self.assertIsNone(sa.flag_distorted_base(self.fund(ago=5e9), TMCV_NSE))
        clean = {"year_ago": {**TMCV_NSE["year_ago"], "tax": -1.37e9, "exceptional": 0.0, "other_income": 1.72e9}}
        self.assertIsNone(sa.flag_distorted_base(self.fund(), clean))
        self.assertIsNone(sa.flag_distorted_base({**self.fund(), "quarter_note": "already explained"}, TMCV_NSE))
        # A gain a year ago that flattered a profit now lost: the decline is overstated, said that way.
        boosted = {"year_ago": {"quarter": "2025-09-30", "profit": 9e9, "pbt": 6e9, "tax": -3e9, "exceptional": 4e9, "other_income": 1e9}}
        note = sa.flag_distorted_base(self.fund(latest=-2e9, ago=9e9), boosted)
        self.assertTrue(note.endswith("the swing overstates the decline."))
        self.assertIn("exceptional gains of ₹400 cr", note)

    def test_the_report_carries_the_caution_from_the_filings(self):
        class TurnaroundFeed(ts.Feed):
            files = {RESULTS: json.dumps({"data": [filing("30-SEP-2026", 1), filing("30-SEP-2025", 2)]}).encode(),
                     X.format(1): results_xml("2026-07-01", "2026-09-30", {"RevenueFromOperations": 253.38e9, "ProfitOrLossAttributableToOwnersOfParent": 12.57e9}),
                     X.format(2): results_xml("2025-07-01", "2025-09-30", {"RevenueFromOperations": 185.85e9, "ProfitBeforeTax": -5.49e9, "TaxExpense": 3.06e9,
                                                                           "ProfitOrLossAttributableToOwnersOfParent": -8.67e9})}

            def __call__(self, url):
                return self.files[url] if url in self.files else super().__call__(url)
        feed = TurnaroundFeed()
        r = sa.analyse("ALPHA", fetcher=feed, news_fetcher=feed, clock=lambda: NOW, peers=False)
        self.assertEqual(r["fundamentals"]["quarter_profit_yoy_text"], "swung from ₹867 cr loss to ₹1,257 cr profit")
        self.assertIn("⚠ The year-ago quarter (2025-09-30) was distorted by one-offs (tax of ₹306 cr on a pre-tax loss of ₹549 cr); "
                      "the swing overstates the turnaround.", sa.summary(r))

    def test_results_xbrl_reads_the_one_off_lines_and_integrated_eps(self):
        body = results_xml("2025-07-01", "2025-09-30", {"RevenueFromOperations": 185.85e9, "OtherIncome": 1.72e9, "ProfitBeforeTax": -5.49e9,
                                                         "TaxExpense": 3.06e9, "ExceptionalItemsBeforeTax": -0.1e9,
                                                         "ProfitOrLossAttributableToOwnersOfParent": -8.67e9,
                                                         "DilutedEarningsLossPerShareFromContinuingAndDiscontinuedOperations": -2.36})
        q = sa.results_xbrl(body)
        self.assertEqual((q["pbt"], q["tax"], q["exceptional"], q["other_income"], q["eps"]), (-5.49e9, 3.06e9, -0.1e9, 1.72e9, -2.36))


def results_xml(start, end, facts):
    parts = ['<?xml version="1.0"?><xbrli:xbrl xmlns:xbrli="http://www.xbrl.org/2003/instance" xmlns:in="http://x">',
             f'<xbrli:context id="P"><xbrli:entity><xbrli:identifier scheme="s">1</xbrli:identifier></xbrli:entity>'
             f'<xbrli:period><xbrli:startDate>{start}</xbrli:startDate><xbrli:endDate>{end}</xbrli:endDate></xbrli:period></xbrli:context>']
    parts += [f'<in:{k} contextRef="P">{v}</in:{k}>' for k, v in facts.items()]
    return ("".join(parts) + "</xbrli:xbrl>").encode()


# ---------- 4. Rupee growth for dollar reporters ----------

RESULTS = "https://www.nseindia.in/api/integrated-filing-results?index=equities&symbol=ALPHA"
X = "https://nsearchives.nseindia.in/corporate/xbrl/INTEGRATED_FILING_INDAS_{}_WEB.xml"


def filing(qe, n):
    return {"qe_Date": qe, "type": "Integrated Filing- Financials", "consolidated": "Consolidated", "xbrl": X.format(n), "broadcast_Date": "x"}


class DollarFeed(ts.Feed):
    """A company Yahoo shows in USD, with rupee results on NSE: the March 2025 filing sits on the list's page 2."""
    def __init__(self, page2=True, **kw):
        super().__init__(eps={"ALPHA": 0.5}, currency={"ALPHA": "USD"}, **kw)
        self.files = {
            RESULTS: json.dumps({"data": [filing("30-SEP-2026", 1), filing("30-SEP-2025", 2), filing("31-MAR-2026", 3)], "totalCount": 24}).encode(),
            X.format(1): results_xml("2026-07-01", "2026-09-30", {"RevenueFromOperations": 4.9e11, "ProfitOrLossAttributableToOwnersOfParent": 7.8e10}),
            X.format(2): results_xml("2025-07-01", "2025-09-30", {"RevenueFromOperations": 4.4e11, "ProfitOrLossAttributableToOwnersOfParent": 7.4e10}),
            X.format(3): results_xml("2025-04-01", "2026-03-31", {"RevenueFromOperations": 1.7865e12, "ProfitOrLossAttributableToOwnersOfParent": 2.944e11,
                                                                  "DilutedEarningsLossPerShareFromContinuingAndDiscontinuedOperations": 71.46}),
            X.format(4): results_xml("2024-04-01", "2025-03-31", {"RevenueFromOperations": 1.6299e12, "ProfitOrLossAttributableToOwnersOfParent": 2.6713e11,
                                                                  "DilutedEarningsLossPerShareFromContinuingAndDiscontinuedOperations": 64.34})}
        if page2:
            self.files[RESULTS + "&page=2"] = json.dumps({"data": [filing("31-MAR-2025", 4)], "totalCount": 24}).encode()
        self.urls = []

    def __call__(self, url):
        self.urls.append(url)
        if url in self.files:
            return self.files[url]
        return super().__call__(url)


class RupeeGrowthTests(unittest.TestCase):
    def run_one(self, feed):
        return sa.analyse("ALPHA", fetcher=feed, news_fetcher=feed, clock=lambda: NOW, peers=False)

    def test_dollar_reporter_growth_comes_from_its_rupee_filings(self):
        feed = DollarFeed()
        f = self.run_one(feed)["fundamentals"]
        self.assertIn(RESULTS + "&page=2", feed.urls)
        self.assertAlmostEqual(f["revenue_growth"], 1.7865 / 1.6299 - 1)
        self.assertAlmostEqual(round(f["revenue_growth"] * 100, 1), 9.6)
        self.assertAlmostEqual(f["eps_growth"], 71.46 / 64.34 - 1)
        self.assertIn("NSE consolidated annual results in rupees (revenue and EPS)", f["growth_basis"])
        self.assertIn("FY2026 revenue ₹1,78,650 cr against ₹1,62,990 cr in FY2025, diluted EPS ₹71.46 against ₹64.34", f["currency_note"])
        text = sa.summary(self.run_one(DollarFeed()))
        self.assertIn("revenue +9.6%, EPS +11.1% (FY2026 vs FY2025; rupee results filed on NSE)", text)
        self.assertIn("| Revenue growth (FY2026 vs FY2025) (rupee results filed on NSE) | +9.6% |", sa.markdown(self.run_one(DollarFeed())))

    def test_without_the_older_year_the_converted_figures_stay_labelled(self):
        f = self.run_one(DollarFeed(page2=False))["fundamentals"]
        self.assertIsNone(f.get("growth_basis"))
        self.assertEqual(f["currency_label"], "USD terms, converted at today's rate")

    def test_rupee_eps_from_profit_when_the_filing_has_no_eps(self):
        fund = {"revenue_growth": 0.093}
        series = {"_currency": {"reported": "USD"}, "annualTotalRevenue": [{"date": "2025-03-31", "value": 1}, {"date": "2026-03-31", "value": 1}],
                  "annualDilutedAverageShares": [{"date": "2025-03-31", "value": 4.15e9}, {"date": "2026-03-31", "value": 4.12e9}]}
        years = {"2026-03-31": {"revenue": 1.7865e12, "profit": 2.944e11, "eps": None, "basis": "consolidated"},
                 "2025-03-31": {"revenue": 1.6299e12, "profit": 2.6713e11, "eps": None, "basis": "consolidated"}}
        sa.rupee_growth(fund, series, years)
        self.assertAlmostEqual(fund["eps_growth"], (2.944e11 / 4.12e9) / (2.6713e11 / 4.15e9) - 1)
        self.assertIsNone(sa.rupee_growth({}, {"annualTotalRevenue": series["annualTotalRevenue"]}, years))  # rupee reporter: untouched

    def test_compare_and_screen_say_where_growth_comes_from(self):
        r = sa.compare(["ALPHA", "GAMMA"], fetcher=DollarFeed(), clock=lambda: NOW)
        text = sa.compare_markdown(r)
        self.assertIn("ALPHA: financials reported in USD on Yahoo and converted at each period's average USD/INR; revenue and EPS growth come "
                      "from the company's rupee results filed on NSE.", text)
        screen = screener.screen("nifty50", [], symbols=["ALPHA", "GAMMA"], fetcher=DollarFeed(), now=NOW)
        alpha = next(x for x in screen["results"] if x["symbol"] == "ALPHA.NS")
        self.assertAlmostEqual(alpha["revenue_growth"], 1.7865 / 1.6299 - 1)
        self.assertIn("revenue and EPS growth come from the company's rupee results filed on NSE", screener.markdown(screen))
        quiet = DollarFeed()
        screener.screen("nifty50", [], symbols=["ALPHA"], fetcher=quiet, now=NOW, columns=["pe", "roe"])
        self.assertFalse(any("integrated-filing" in u for u in quiet.urls))  # no growth column, no NSE requests


# ---------- 5. News gaps and 7. time limits ----------

class Slow:
    """A test feed that stalls on some URLs (a slow public source)."""
    def __init__(self, feed, stall, seconds=2.0):
        self.feed, self.stall, self.seconds = feed, stall, seconds

    def __call__(self, url):
        if any(s in url for s in self.stall):
            time.sleep(self.seconds)
        return self.feed(url)


class GapAndTimeLimitTests(unittest.TestCase):
    def test_failed_news_is_a_named_gap_not_a_silent_omission(self):
        feed = tsa.Feed(fail=("news.google.com", "feeds.finance.yahoo.com", "nsearchives"))
        r = tsa.run(feed)
        self.assertIn("news", r["errors"])
        text = tsa.summary(r)
        self.assertNotIn("Latest headline", text)
        self.assertIn("news (source unavailable)", text.splitlines()[-2])
        self.assertNotIn("news (", tsa.summary(tsa.run(tsa.Feed())))

    def test_slow_optional_sections_become_timed_out_gaps(self):
        original = sa.GRACE
        sa.GRACE = 0.1
        try:
            feed = Slow(ts.Feed(), stall=("/BETA", "news.google.com"), seconds=5.0)
            started = time.monotonic()
            r = sa.analyse("ALPHA", fetcher=feed, news_fetcher=feed, clock=lambda: NOW, peer_symbols=["BETA"], optional_budget=0.5)
            elapsed = time.monotonic() - started
        finally:
            sa.GRACE = original
        self.assertLess(elapsed, 4.0)  # did not wait for the 5-second stalled requests
        self.assertIn("timed out", r["errors"]["peers"])
        self.assertIsNone(r["peers"])
        self.assertTrue(r["news"]["timed_out"])
        text = sa.summary(r)
        self.assertIn("peers (timed out), news (timed out).", text)
        self.assertIn("Valuation: P/E", text)  # the core report is complete

    def test_optional_sections_run_alongside_the_core_data(self):
        feed = Slow(ts.Feed(), stall=("/BETA",), seconds=0.6)
        started = time.monotonic()
        r = sa.analyse("ALPHA", fetcher=feed, news_fetcher=feed, clock=lambda: NOW, peer_symbols=["BETA"], optional_budget=10)
        self.assertLess(time.monotonic() - started, 5)
        self.assertEqual([x["symbol"] for x in r["peers"]["rows"]], ["BETA.NS"])
        self.assertNotIn("peers", r["errors"])

    def test_guard_refuses_new_requests_after_the_deadline(self):
        opt = sa.OptionalSections(0.01)
        time.sleep(0.6)
        with self.assertRaisesRegex(Block, "time limit"):
            opt.guard(lambda url: b"x", "PUBLIC")("https://query1.finance.yahoo.com/x")
        self.assertIs(sa.OptionalSections(None).guard(len, "PUBLIC"), len)

    def test_live_fetchers_never_stack_retries(self):
        if sa.REQUEST_RETRIES:
            self.assertFalse(getattr(sa.live_fetcher(), "retrying", False))
        else:
            self.assertTrue(getattr(sa.live_fetcher(), "retrying", False))
        self.assertEqual(sa.live_nse().keywords, {"timeout": sa.CORE_TIMEOUT, "attempts": 3})


# ---------- new listings ----------

class NewListingFeed(tsa.Feed):
    def __call__(self, url):
        body = super().__call__(url)
        if "/v8/finance/chart/EXAMPLE" in url:
            data = json.loads(body)
            data["chart"]["result"][0]["meta"]["firstTradeDate"] = data["chart"]["result"][0]["timestamp"][0]
            return json.dumps(data).encode()
        return body


class NewListingTests(unittest.TestCase):
    def test_a_recent_listing_is_analysed_from_a_few_sessions(self):
        r = tsa.run(NewListingFeed(closes=[100.0 + k for k in range(15)]), peers=False)
        t = r["technical"]
        self.assertTrue(t["new_listing"])
        self.assertIsNone(t["sma50"])
        text = tsa.summary(r)
        self.assertIn("⚠ Listed recently: only 15 sessions of prices", text)
        self.assertIn("+14.0% since the first close", text)
        self.assertIn("Trend: too little history to call (15 sessions).", text)
        self.assertIn("; market cap ₹", text)
        with self.assertRaises(Block):  # without a recent first-trade date a short history is an incomplete feed
            tsa.run(tsa.Feed(closes=[100.0] * 15), peers=False)


# ---------- 6. Screener footnotes ----------

class ScreenerFootnoteTests(unittest.TestCase):
    def screen(self, columns):
        r = screener.screen("nifty50", [], symbols=["ALPHA", "GAMMA"], fetcher=ts.Feed(), now=NOW, columns=columns)
        r["preset"] = "dividend"
        r["results"][0]["demerger_notes"] = ["FY2026 vs FY2025 straddles the demerger of ITC Hotels Limited (from 2025-01-06), so annual growth "
                                             "compares different businesses and is not shown; the latest quarter against a year earlier is used instead."]
        return screener.markdown(r)

    def test_footer_once_and_the_demerger_note_only_with_a_growth_column(self):
        text = self.screen(list(screener.PRESETS["dividend"]["columns"]))
        self.assertEqual(text.count("Ratios come from company financials"), 1)
        self.assertNotIn("straddles the demerger", text)
        self.assertIn("straddles the demerger", self.screen(["pe", "revenue_growth"]))

    def test_first_partial_year_note_follows_roe_and_pe_columns(self):
        r = screener.screen("nifty50", [], symbols=["ALPHA"], fetcher=ts.Feed(), now=NOW, columns=["roe"])
        r["results"][0]["demerger_notes"] = ["TMCV received its business in the demerger from TMPV ...; annual growth, annual ROE and the P/E history are not meaningful."]
        self.assertIn("annual ROE", screener.markdown(r))
        self.assertNotIn("annual ROE", screener.markdown(r, columns=["price", "return_1y"]))


if __name__ == "__main__":
    unittest.main()
