"""Offline contracts for the round-5 stock fixes: trailing EPS across a split or bonus (put on today's share basis and
cross-checked against trailing net profit ÷ today's shares), the SAST acquisition mode worded by what the filings show,
NSE bulk/block deals behind a promoter move with no SAST filing, trend-aware level wording, hidden summary cautions,
'all effectively debt-free' in comparisons, and routine items not picked as the headline. Never contacts the network."""
from datetime import date
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT / "tests"))
import screener
import shareholding as sh
import stock_analysis as sa
import test_shareholding as tsh
import test_stock_analysis as tsa
from test_stock_round4 import SAST, SAST_ROWS, row
from test_stock_analysis import NOW

CR = 1e7


def series(**columns):
    """{name: [(date, value), ...]} -> the fundamentals series shape ratios() reads."""
    return {name: [{"date": d, "value": float(v)} for d, v in rows] for name, rows in columns.items()}


# ---------- 1. Trailing EPS across a split or bonus ----------

TRENT_SHARES = 533232301
TRENT_PRICE = 3175.50


def trent_series(**override):
    """Yahoo's TRENT figures on 18 Nov 2026: a 1:2 bonus (3-for-2) with record date 4 Jun 2026. Yahoo restated the last
    four quarters and the annual EPS but its trailing EPS (27.99) mixes share bases, and the Sep 2025 quarter stayed at
    the pre-bonus ₹10.60."""
    data = dict(
        trailingDilutedEPS=[("2026-03-31", 32.246667), ("2026-06-30", 33.916667), ("2026-09-30", 27.986667)],
        quarterlyDilutedEPS=[("2025-09-30", 10.6), ("2025-12-31", 9.613333), ("2026-03-31", 7.506667), ("2026-06-30", 9.73), ("2026-09-30", 8.7)],
        quarterlyNetIncome=[("2025-09-30", 376.86 * CR), ("2025-12-31", 512.77 * CR), ("2026-03-31", 400.33 * CR), ("2026-06-30", 519.01 * CR),
                            ("2026-09-30", 463.77 * CR)],
        trailingNetIncome=[("2026-09-30", 1895.88 * CR)],
        quarterlyOrdinarySharesNumber=[("2026-03-31", 533231191), ("2026-09-30", TRENT_SHARES)],
        annualDilutedEPS=[("2025-03-31", 29.006667), ("2026-03-31", 32.246667)],
        annualNetIncome=[("2025-03-31", 1546.72 * CR), ("2026-03-31", 1719.65 * CR)],
        annualDilutedAverageShares=[("2025-03-31", 533231191), ("2026-03-31", 533231191)],
        annualTotalRevenue=[("2025-03-31", 16880 * CR), ("2026-03-31", 19850 * CR)],
        annualStockholdersEquity=[("2025-03-31", 6100 * CR), ("2026-03-31", 7300 * CR)],
    )
    data.update(override)
    return series(**{k: v for k, v in data.items() if v is not None})


NONE_NEEDED = ("and none was required for a net change of this size: SAST Reg. 29(2) asks a promoter group for a disclosure when its "
               "holding moves by 2% of the shares or more (counted from its last disclosure, so smaller moves can add up to one)")

class SplitEpsTests(unittest.TestCase):
    def test_trent_trailing_eps_uses_net_profit_on_todays_shares(self):
        f = sa.ratios(trent_series(), TRENT_PRICE, NOW, [("2026-06-04", 1.5)])
        self.assertAlmostEqual(f["eps"]["value"], 35.55, delta=0.05)
        self.assertAlmostEqual(f["pe"], 89.3, delta=0.3)  # Screener.in: about 89-90
        self.assertEqual(f["eps"]["replaced"], 27.986667)
        self.assertEqual(f["eps"]["basis"], "trailing 12 months, net profit ÷ current shares")
        flag = next(x for x in f["eps_flags"] if "share count" in x)
        self.assertIn("Trailing EPS ₹27.99 does not match trailing net profit ÷ today's share count", flag)
        self.assertIn("53.32 cr shares = ₹35.55", flag)
        self.assertIn("3-for-2 split/bonus on 04 Jun 2026 falls inside the trailing year", flag)
        self.assertIn("(P/E 89.3 instead of 113.5)", flag)
        self.assertFalse(f["eps_stale"])  # still a trailing figure for the staleness rule

    def test_without_split_data_the_quarters_still_settle_it(self):
        # The screener and peers had no split list before; the restated quarters agree with net profit ÷ shares.
        f = sa.ratios(trent_series(), TRENT_PRICE, NOW)
        self.assertAlmostEqual(f["pe"], 89.3, delta=0.3)
        self.assertIn("the last four quarters sum to ₹35.55, so the reported figure looks wrong", " ".join(f["eps_flags"]))

    def test_unrestated_quarter_is_put_on_todays_share_basis(self):
        s = trent_series()
        rows, fixed = sa.adjust_quarter_eps(s["quarterlyDilutedEPS"], s["quarterlyNetIncome"], TRENT_SHARES, [("2026-06-04", 1.5)])
        self.assertEqual(fixed, ["2025-09-30"])
        self.assertAlmostEqual(rows[0]["value"], 10.6 / 1.5)
        self.assertEqual([r["value"] for r in rows[1:]], [r["value"] for r in s["quarterlyDilutedEPS"][1:]])  # already restated
        # No ratio known (bare dates), or no net profit to check against: nothing is guessed.
        self.assertEqual(sa.adjust_quarter_eps(s["quarterlyDilutedEPS"], s["quarterlyNetIncome"], TRENT_SHARES, ["2026-06-04"])[1], [])
        self.assertEqual(sa.adjust_quarter_eps(s["quarterlyDilutedEPS"], [], TRENT_SHARES, [("2026-06-04", 1.5)])[1], [])

    def test_unrestated_quarters_inside_the_trailing_year_are_scaled_before_the_quarter_check(self):
        # Yahoo left all three pre-bonus quarters unrestated: their sum is not today's basis until scaled.
        quarters = [("2025-12-31", 14.42), ("2026-03-31", 11.26), ("2026-06-30", 9.73), ("2026-09-30", 8.7)]
        s = trent_series(quarterlyDilutedEPS=quarters, trailingDilutedEPS=[("2026-09-30", 34.41)])
        rows, fixed = sa.adjust_quarter_eps(s["quarterlyDilutedEPS"], s["quarterlyNetIncome"], TRENT_SHARES, [("2026-06-04", 1.5)])
        self.assertEqual(fixed, ["2025-12-31", "2026-03-31"])
        self.assertAlmostEqual(sum(r["value"] for r in rows), 35.55, delta=0.05)
        f = sa.ratios(s, TRENT_PRICE, NOW, [("2026-06-04", 1.5)])
        self.assertEqual(f["quarters_split_adjusted"], ["2025-12-31", "2026-03-31"])
        self.assertAlmostEqual(f["eps"]["value"], 34.41)  # within 10% of ₹35.55: Yahoo's figure stands
        self.assertFalse(any("share count" in x for x in f["eps_flags"]))

    def test_agreeing_figures_are_left_alone(self):
        f = sa.ratios(trent_series(trailingDilutedEPS=[("2026-09-30", 35.3)]), TRENT_PRICE, NOW, [("2026-06-04", 1.5)])
        self.assertEqual(f["eps"]["basis"], "trailing 12 months")
        self.assertAlmostEqual(f["eps"]["value"], 35.3)
        self.assertFalse(any("share count" in x for x in f["eps_flags"]))

    def test_discontinued_profit_keeps_the_reported_eps_and_says_why(self):
        # EPS (continuing operations) agrees with its quarters; net profit includes a large discontinued gain.
        s = trent_series(trailingDilutedEPS=[("2026-09-30", 35.55)], trailingNetIncome=[("2026-09-30", 2600 * CR)])
        f = sa.ratios(s, TRENT_PRICE, NOW)
        self.assertAlmostEqual(f["eps"]["value"], 35.55)
        flag = next(x for x in f["eps_flags"] if "share count" in x)
        self.assertIn("the reported EPS is kept", flag)
        self.assertIn("discontinued operations", flag)

    def test_bare_split_dates_still_mark_a_split_after_the_statements(self):
        f = sa.ratios(trent_series(), TRENT_PRICE, NOW, ["2026-11-01"])
        self.assertEqual(f["split_after_statements"], ["2026-11-01"])

    def test_share_changes_adds_a_listed_bonus_yahoo_missed(self):
        history = {"corporate_actions": {"splits": {}}}
        self.assertEqual(sa.share_changes("HDFCBANK.NS", history), [("2025-08-26", 2.0)])
        yahoo = {"corporate_actions": {"splits": {"1": {"date": int(sa.datetime(2025, 8, 26, 9, 15, tzinfo=sa.IST).timestamp()),
                                                        "numerator": 2, "denominator": 1}}}}
        self.assertEqual(sa.share_changes("HDFCBANK.NS", yahoo), [("2025-08-26", 2.0)])  # not counted twice
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "bonuses.json"
            path.write_text(json.dumps({"bonuses": [{"symbol": "X", "ratio": {"new_shares": 4, "per_held_shares": 1},
                                                     "split_before_bonus": {"new_shares": 2, "per_old_shares": 1}, "record_date": "2025-06-16"},
                                                    {"symbol": "BAD", "ratio": {}}]}), encoding="utf-8")
            self.assertEqual(sa.listed_bonuses(path), {"X": [("2025-06-16", 10.0)]})  # Bajaj Finance's split + bonus: 10×
            self.assertEqual(sa.listed_bonuses(Path(d) / "missing.json"), {})

    def test_screener_rows_check_eps_with_the_split_list(self):
        seen = []
        real = sa.ratios

        def spy(series_, price, now, splits=None):
            seen.append(splits)
            return real(series_, price, now, splits)
        feed = tsa.Feed(splits=[int(tsa.sessions(520)[-100].timestamp())])
        with mock.patch.object(sa, "ratios", spy):
            screener.snapshot("EXAMPLE", fetcher=feed, now=NOW)
        self.assertEqual(len(seen), 1)
        self.assertEqual([r for _, r in seen[0]], [2.0])

    def test_compare_table_marks_the_net_profit_basis_as_ttm(self):
        report = {"rows": [row("TRENT", pe=89.3, pe_basis="trailing 12 months, net profit ÷ current shares", price=3175.5,
                               price_date="2026-11-18", name="Trent", market_cap_cr=169000)]}
        self.assertIn("| 89.3 (TTM) |", "\n".join(sa.compare_table(report)))


# ---------- 2. SAST mode, bulk/block deals and promoter entities ----------

def adani_move(filings, before=2160138945, after=2303959098):
    return {"quarter": "2025-12-31", "from_quarter": "2025-09-30", "pts": 2.13, "filings": filings,
            "shares_before": before, "shares_after": after}


class SastModeTests(unittest.TestCase):
    def filings(self):
        sast = sh.sast_filings(json.dumps({"data": [
            {"acqSaleType": "Acquisition", "acquirerName": "Carmichael Rail and Port Singapore HOlding Pvt Ltd", "promoterType": "Y",
             "acquisitionMode": "Open Market", "totAcqShare": "6.24", "acquirerDate": "11-SEP-2023 to 23-DEC-2025", "timestamp": "29-Dec-2025 11:02"},
            {"acqSaleType": "Acquisition", "acquirerName": "Emerging Market Investment DMCC", "promoterType": "Y",
             "acquisitionMode": "Open Market", "totAcqShare": "0.67", "acquirerDate": "11-SEP-2023 to 23-DEC-2025", "timestamp": "29-Dec-2025 11:02"}]}).encode())
        return sh.filings_in_window(sast, "2025-12-31")

    def test_new_shares_matching_the_stake_are_called_an_allotment(self):
        found = self.filings()
        label = sh.mode_label(found[0], adani_move(found))
        self.assertEqual(label, "allotment of new shares: share count 216.01 cr → 230.40 cr, +14,38,20,153, likely a preferential issue")
        self.assertEqual(sh.mode_label(found[1], adani_move(found)), "mode as filed: open market")  # 0.67% is not the issue
        self.assertEqual(sh.mode_label(found[0], adani_move(found, after=2160138945)), "mode as filed: open market")  # no new shares
        self.assertEqual(sh.mode_label({**found[0], "mode": None}, {}), None)
        line = sh.move_lines({"promoter_moves": [adani_move(found)]})[0]
        self.assertIn("Carmichael Rail and Port Singapore Holding PVT LTD acquired 6.24% (allotment of new shares: share count "
                      "216.01 cr → 230.40 cr, +14,38,20,153, likely a preferential issue; filed 29 Dec 2025)", line)
        self.assertIn("Emerging Market Investment DMCC acquired 0.67% (mode as filed: open market; filed 29 Dec 2025)", line)
        self.assertIn("Check the company's allotment announcement for Oct–Dec 2025.", line)
        self.assertNotIn("(open market", line)  # never a bare assertion of NSE's field

    def test_promoter_entity_changes_match_names_across_filings(self):
        before = {"total_shares": 2303959098, "promoter_holders": [
            {"name": "Worldwide Emerging Market Holding Limited", "pct": 3.74, "shares": 86168070},
            {"name": "Carmichael Rail And Port Singapore Holding Pte. Ltd.", "pct": 6.24, "shares": 143820153},
            {"name": "Gone Trading Ltd", "pct": 0.5, "shares": 11519795}]}
        after = {"total_shares": 2303959098, "promoter_holders": [
            {"name": "Worldwide Emerging Market Holding Ltd", "pct": 1.75, "shares": 40308405},
            {"name": "Carmichael Rail And Port Singapore Holdings Pte. Ltd.", "pct": 6.24, "shares": 143820153},
            {"name": "New Promoter Co", "pct": 0.3, "shares": 6911877}]}
        changes = sh.promoter_changes(before, after)
        self.assertEqual([(c["name"], c["before_pct"] is not None, c["after_pct"] is not None) for c in changes],
                         [("Worldwide Emerging Market Holding Ltd", True, True), ("Gone Trading Ltd", True, False), ("New Promoter Co", False, True)])
        self.assertAlmostEqual(changes[0]["pts"], -1.99, places=2)
        self.assertTrue(sh.same_entity("Carmichael Rail and Port Singapore HOlding Pvt Ltd", "Carmichael Rail And Port Singapore Holdings Pte. Ltd."))
        self.assertFalse(sh.same_entity("Adani Tradeline Private Limited", "Adani Rail Infra Private Limited"))
        text = sh.changes_words({"holder_changes": changes})
        self.assertIn("Worldwide Emerging Market Holding LTD 3.74% → 1.75%", text)
        self.assertIn("Gone Trading LTD 0.50% → not listed", text)


DEALS_BLOCK = sh.deals_url("block_deals", "ADANIPORTS", date(2026, 4, 1), date(2026, 6, 30))
DEALS_BULK = sh.deals_url("bulk_deals", "ADANIPORTS", date(2026, 4, 1), date(2026, 6, 30))
MARCH = "https://nsearchives.nseindia.in/corporate/xbrl/SHP_0_10042026_WEB.xml"
WEMH = ("PromW", "DetailsOfSharesHeldByOtherForeignShareholdersAxis", "Worldwide Emerging Market Holding Limited")


def block_deal(client, side, qty=45859665, price=1400.0, day="12-MAY-2026"):
    return {"BD_DT_DATE": day, "BD_SYMBOL": "ADANIPORTS", "BD_CLIENT_NAME": client, "BD_BUY_SELL": side, "BD_QTY_TRD": qty,
            "BD_TP_WATP": price, "BD_REMARKS": None}


class DealFeedTests(unittest.TestCase):
    def feed(self, deals=None):
        """Sep 2026 (latest), Jun 2026 and Mar 2026 filings readable; the promoter fell 1.99 pts in the Jun quarter with no SAST filing."""
        feed = tsh.Feed(latest=tsh.with_holders(tsh.shp(tsh.LATEST_HOLDINGS, pledged=0, encumbered=0), [(*WEMH, "0.0175", True, None)]),
                        previous=tsh.with_holders(tsh.shp(tsh.PREVIOUS_HOLDINGS, pledged=0, encumbered=0, day="2026-06-30"), [(*WEMH, "0.0175", True, None)]))
        feed.files[MARCH] = tsh.with_holders(tsh.shp(tsh.PREVIOUS_HOLDINGS, pledged=0, encumbered=0, day="2026-03-31"), [(*WEMH, "0.0374", True, None)])
        rows = [{"date": d, "broadcastDate": "10-OCT-2026 15:51:00", "pr_and_prgrp": p, "public_val": "33.97", "xbrl": x,
                 "name": "Adani Ports and Special Economic Zone Limited"}
                for d, p, x in (("30-SEP-2026", "66.03", tsh.LATEST), ("30-JUN-2026", "66.03", tsh.PREVIOUS), ("31-MAR-2026", "68.02", MARCH))]
        feed.files[tsh.MASTER] = tsh.master(rows)
        feed.files[SAST] = json.dumps(SAST_ROWS).encode()
        if deals is not None:
            feed.files[DEALS_BULK] = json.dumps({"data": []}).encode()
            feed.files[DEALS_BLOCK] = json.dumps({"data": deals}).encode()
        return feed

    def test_promoter_block_sale_is_named_with_the_buyer(self):
        feed = self.feed([block_deal("WORLDWIDE EMERGING MARKET HOLDING LTD", "SELL"),
                          block_deal("NEW WORLD FUND INC", "BUY", qty=30000000), block_deal("SMALLCAP WORLD FUND INC", "BUY", qty=15859665),
                          block_deal("SOMEONE ELSE", "BUY", qty=5000, price=1390.0)])
        r = tsh.report(feed)
        move = r["promoter_moves"][0]
        self.assertEqual((move["quarter"], round(move["pts"], 2), move["filings"]), ("2026-06-30", -1.99, []))
        self.assertIn(DEALS_BLOCK, feed.urls)
        text = sh.summary(r)
        self.assertIn("no promoter-group SAST (Reg. 29) filing on NSE in that window, " + NONE_NEEDED + "; NSE's bulk and block-deal records for Apr–Jun 2026: "
                      "Worldwide Emerging Market Holding LTD sold 4.59 cr shares at ₹1,400.00 in a block deal on 12 May 2026 "
                      "(to New World Fund Inc 3.00 cr, Smallcap World Fund Inc 1.59 cr).", text)
        self.assertIn("Promoter entities that changed between the two filings: Worldwide Emerging Market Holding Limited 3.74% → 1.75%.", text)
        self.assertLessEqual(len(text.splitlines()), 7)

    def test_no_promoter_deals_says_so_and_keeps_the_pointer(self):
        text = sh.summary(tsh.report(self.feed([block_deal("SOMEONE ELSE", "BUY")])))
        self.assertIn("NSE's bulk and block-deal records list no deals by promoter entities in Apr–Jun 2026. The trade may have been on BSE "
                      "(its bulk and block deals are at https://www.bseindia.com/markets/equity/EQReports/tra_Bulk_Deals.aspx", text)
        self.assertIn("or off-market, so check those and the company's announcements for that window.", text)

    def test_unreachable_deal_lists_fall_back_to_the_pointer(self):
        text = sh.summary(tsh.report(self.feed()))
        self.assertIn("no promoter-group SAST (Reg. 29) filing on NSE in that window, " + NONE_NEEDED + "; check NSE's SAST, preferential-allotment "
                      "and block-deal filings for Apr–Jun 2026.", text)
        self.assertNotIn("Data gaps", text)  # the extra filings and deal lists are optional

    def test_deals_endpoint_is_allowlisted_narrowly(self):
        self.assertTrue(sh.allowed(DEALS_BLOCK))
        self.assertTrue(sh.allowed(DEALS_BULK))
        self.assertEqual(DEALS_BLOCK, "https://www.nseindia.in/api/historicalOR/bulk-block-short-deals?optionType=block_deals"
                                      "&symbol=ADANIPORTS&from=01-04-2026&to=30-06-2026")
        base = "https://www.nseindia.in/api/historicalOR/bulk-block-short-deals?"
        for bad in (base + "optionType=short_deals&symbol=ADANIPORTS&from=01-04-2026&to=30-06-2026",
                    base + "optionType=block_deals&symbol=ADANIPORTS&from=01-04-2025&to=30-06-2026",  # over a year
                    base + "optionType=block_deals&symbol=ADANIPORTS&from=30-06-2026&to=01-04-2026",  # reversed
                    base + "optionType=block_deals&symbol=ADANIPORTS&from=2026-04-01&to=30-06-2026",
                    base + "optionType=block_deals&symbol=ADANIPORTS&from=01-04-2026&to=30-06-2026&x=1",
                    base + "optionType=block_deals&symbol=ADANI%20PORTS&from=01-04-2026&to=30-06-2026",
                    base.replace("www.nseindia.in", "evil.example") + "optionType=block_deals&symbol=ADANIPORTS&from=01-04-2026&to=30-06-2026",
                    "https://www.nseindia.in/api/historical/bulk-deals?symbol=ADANIPORTS&from=01-04-2026&to=30-06-2026"):
            with self.subTest(bad=bad):
                self.assertFalse(sh.allowed(bad))


# ---------- 3. Levels by trend, and no hidden cautions ----------

def tech(trend, price, support=252.30, resistance=301.85, atr14=9.0):
    return {"trend": trend, "last_close": price, "support": support, "resistance": resistance, "atr14": atr14}


class LevelWordingTests(unittest.TestCase):
    def test_downtrend_testing_its_low(self):
        self.assertEqual(sa.levels_words(tech("downtrend", 252.50)),
                         "a close above ₹301.85 would break the downtrend; price is testing ₹252.30 now, and a close below it would extend the decline")
        self.assertEqual(sa.levels_words(tech("downtrend", 275.0)),
                         "a close above ₹301.85 would break the downtrend; below ₹252.30 would extend the decline")

    def test_uptrend_keeps_its_wording_unless_a_level_is_being_tested(self):
        up = tech("uptrend", 3175.5, support=2676.2, resistance=3262.0, atr14=85.7)
        self.assertEqual(sa.levels_words(up), "above ₹3,262.00 extends the move, below ₹2,676.20 weakens it")
        near = {**up, "last_close": 3230.0}
        self.assertEqual(sa.levels_words(near), "price is testing ₹3,262.00 now, and a close above it would extend the move; "
                                                "below ₹2,676.20 would weaken the picture")
        self.assertIn("would break the range upward", sa.levels_words(tech("mixed / sideways", 275.0)))

    def test_outlook_cases_follow_the_trend(self):
        t = {**tech("downtrend", 252.50), "closes": [250 + (k % 7) for k in range(300)], "close_dates": ["2026-01-01"] * 300,
             "resistance_60d": 320.0, "high_52w": 410.0, "support_60d": 240.0, "sma200": 300.0, "rsi14": 35}
        o = sa.outlook(t)
        self.assertTrue(o["bull_case"].startswith("A daily close above ₹301.85 (20-day high) would break the downtrend"))
        self.assertTrue(o["bear_case"].startswith("Price is testing the 20-day low of ₹252.30 now; a daily close below ₹252.30 (20-day low) "
                                                  "would extend the decline"))

    def test_summary_prints_a_fifth_caution_instead_of_hiding_it(self):
        r = tsa.run(tsa.Feed(), peers=False)
        r["fundamentals"]["eps_flags"] = [f"Caution number {n}." for n in range(1, 6)]
        text = sa.summary(r)
        self.assertIn("⚠ Caution number 5.", text)
        self.assertNotIn("more caution", text)
        r["fundamentals"]["eps_flags"] = [f"Caution number {n}: details that run on for a while." for n in range(1, 8)]
        text = sa.summary(r)
        self.assertIn("⚠ Also: Caution number 5; Caution number 6; Caution number 7 (details in the full report).", text)
        self.assertNotIn("⚠ Caution number 5", text)

    def test_brief_caution_cuts_at_the_first_clause(self):
        self.assertEqual(sa.brief_caution("FY2026 net profit ₹82,390 cr is about ₹223.64 a share: the rest."),
                         "FY2026 net profit ₹82,390 cr is about ₹223.64 a share")
        long = "word " * 40
        self.assertTrue(sa.brief_caution(long).endswith("…"))
        self.assertLessEqual(len(sa.brief_caution(long)), 111)


# ---------- 4. Comparison balance sheet and the headline pick ----------

class CompareAndHeadlineTests(unittest.TestCase):
    def test_all_low_debt_is_not_ranked(self):
        rows = [row("TCS", debt_to_equity=0.10), row("INFY", debt_to_equity=0.05), row("HCLTECH", debt_to_equity=0.08)]
        sheet = next(x for x in sa.tradeoffs(rows) if x.startswith("Balance sheet"))
        self.assertEqual(sheet, "Balance sheet — all effectively debt-free (debt/equity at 30 Sep 2026: INFY 0.05, HCLTECH 0.08, TCS 0.10).")
        rows[0]["debt_to_equity"] = 0.30
        sheet = next(x for x in sa.tradeoffs(rows) if x.startswith("Balance sheet"))
        self.assertIn("Balance sheet (best first) — debt/equity at 30 Sep 2026: INFY 0.05, HCLTECH 0.08, TCS 0.30", sheet)

    def test_routine_conference_update_is_not_the_headline(self):
        rss = """<?xml version="1.0"?><rss><channel>
<item><title>Example Industries NSE:EXAMPLE JM Financial Conference Update - Kalkine India</title><link>https://news.google.com/rss/articles/k</link><pubDate>Fri, 13 Nov 2026 08:00:00 GMT</pubDate></item>
<item><title>Example Industries discloses investor meeting schedule - scanx.trade</title><link>https://news.google.com/rss/articles/s</link><pubDate>Fri, 13 Nov 2026 07:00:00 GMT</pubDate></item>
<item><title>Example Industries wins Rs 2,000 crore order - Mint</title><link>https://news.google.com/rss/articles/m</link><pubDate>Fri, 13 Nov 2026 05:00:00 GMT</pubDate></item>
</channel></rss>""".encode()

        class Feed(tsa.Feed):
            def __call__(self, url):
                return rss if "news.google.com" in url else super().__call__(url)
        r = tsa.run(Feed(), peers=False)
        self.assertEqual(r["news"]["headline"]["headline"], "Example Industries wins Rs 2,000 crore order")
        self.assertEqual(len(r["news"]["items"]), 3)  # routine items stay in the list
        self.assertTrue(sa.ROUTINE_HEADLINE.search("Reliance Industries Limited: Analysts/Institutional Investor Meet/Con. Call Updates"))
        self.assertFalse(sa.ROUTINE_HEADLINE.search("Reliance Industries updates guidance after Q2"))


if __name__ == "__main__":
    unittest.main()
