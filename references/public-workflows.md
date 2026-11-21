# Public research and Claude plugin

Version 0.9 adds account-free public research alongside the existing broker-bound paper and execution tools. Python 3.10+ and network access are required for collection. No brokerage credentials are requested by these commands. Public feeds may be delayed, unavailable, revised or restricted; a successful download is not an exchange-data entitlement or trading clearance.

## Install the Claude plugin

In Claude Code, add this GitHub marketplace and install the plugin:

```text
/plugin marketplace add kunalshinde1214/Day-Trader-Indian-Market
/plugin install india-day-trader@kunal-india-trading
```

The marketplace contains one plugin with twelve skills: the daily commands `nifty`, `sensex`, `banknifty` and `india-daily` (restored in 1.4.0 after 1.3.0 folded them into `market`), and `stock` (one-stock analysis, results and disclosures), `fno` ([F&O analytics](fno.md)), `screener` ([screener](screener.md) and chart-pattern scans), `portfolio` ([holdings review](holdings-review.md)), `market` (Nifty, Sensex and Bank Nifty briefings), `news`, `trade-plan` (entry, stop, replay, size and risk for paper trades) and `broker` (the user's own broker data, paper tickets and alerts). Invoke namespaced commands such as `/india-day-trader:stock` or `/india-day-trader:market`, or just ask. Version 1.3.0 merged the earlier nineteen commands into these eight. Installation does not register unnamespaced global commands, grant tool permissions, start a watcher or enable execution.

Keep scripts, references, assets and license files together. Work files belong in the user's chosen directory outside the installed plugin cache. The combined [root skill](../SKILL.md) and standalone CLI remain available for other skill hosts. Plugin manifests can be checked with `claude plugin validate .` and `claude plugin validate .claude-plugin/plugin.json`. Official [plugin reference](https://code.claude.com/docs/en/plugins-reference) and [marketplace documentation](https://code.claude.com/docs/en/plugin-marketplaces) describe host installation. The personal-use source-available license is unchanged; this is not an open-source license.

## Current public data

Run these from the repository root, using a new output path each time:

```bash
python scripts/public_market.py universe --out reports/nse-equities.json
python scripts/public_market.py scan --symbols RELIANCE.NS TCS.NS INFY.NS --out reports/public-scan.json
python scripts/public_market.py chart --symbol RELIANCE.NS --interval 5m --out reports/public-chart.json
python scripts/daily.py india-daily --collect-public
python scripts/daily.py nifty --collect-public
python scripts/daily.py sensex --collect-public
python scripts/daily.py banknifty --collect-public
```

The NSE EQ listing is not F&O eligibility, index membership or a surveillance clearance. A scan accepts 1–500 explicit symbols and pauses between requests; it does not silently choose 500 from the full listing. Symbols must be exact Indian `.NS` or `.BO` provider symbols. Index history supports Nifty, Bank Nifty, Sensex and India VIX, but equities alone are eligible for public entry research. Yahoo's public chart endpoint is unofficial and has no availability guarantee. Failed requests are reported without authentication, access-control bypass or automatic retries.

The daily collector uses provider session metadata and observed previous daily bars. This is explicitly **not an independently verified exchange calendar**. Closed days, missing current-session metadata, inconsistent calendars and special weekend sessions block. Public reports expose this uncertainty and missing breadth/events as coverage gaps and never become trade-ready. A verified supplied input or the existing Kite collector supports richer evidence. See [daily reports](daily.md).

## Daily screen and intraday entry research

The public screener produces multiple tags per symbol: `breakout_20d`, `bull_flag`, `reclaim_ma20`, `oversold` and `oversold_bounce`. It requires at least 31 distinct prior daily observations and excludes today's bar even after close. It uses a 20-observation closing high, a greater-than-10% ten-observation pole followed by a five-close range below 5%, an SMA20 reclaim, Wilder RSI14 below 30, and a rising close after RSI below 30 respectively. Definitions differ from the six broker-bound intraday rules. Missing bars and split-affected history are excluded; other corporate actions remain visible. No historical membership or total-return adjustment is inferred.

```bash
python scripts/public_market.py entry --symbol RELIANCE.NS --interval 5m --risk 2000
python scripts/public_market.py entry --symbol RELIANCE.NS --interval 1m --setup level --level 1500 --side BUY --out reports/entry.json
python scripts/public_market.py watch --symbol RELIANCE.NS --interval 1m --setup level --level 1500 --cycles 10 --every 30
python scripts/public_market.py plan --input reports/entry.json --stop pct:1 --target 2R --out reports/public-plan.json
```

For the quick "buy if it breaks today's opening range, risking ₹2,000 — how many shares?" question, one run answers it:

```bash
python scripts/public_market.py entry --symbol RELIANCE.NS --setup orb --risk 2000
```

`entry` and `watch` print a compact summary by default: state and the exact thing being waited for ("Range complete; waiting for a completed 5-minute close above 1,372.30 (next check after 09:55 IST)", or "Opening range still forming: 2 of 3 bars"), the opening range high/low/midpoint, bars completed, last completed close, trigger level, next check time and the range stops (midpoint; range low for a BUY, high for a SELL). `--format json` returns the same without bars; add `--verbose` for every bar. `--out FILE` always writes the full JSON with bars (for `plan` and `replay`) and still prints the summary.

For a plain sizing question outside the ORB flow, `position_size.py` takes the user's words directly: `--symbol TCS` (entry = the last close, or the live price in market hours, read from Yahoo through holdings_review's price reader and labelled with its date), `--stop-pct 2` (stop 2% from the entry on the ₹0.05 tick), `--risk-pct 1 --capital 500000` (risk = 1% of capital); a buy is sized for delivery (CNC) and intraday (MIS) side by side unless `--product` picks one, and a target not given is labelled "2R default; pass --target". Example: `python scripts/position_size.py --symbol TCS --capital 500000 --risk-pct 1 --stop-pct 2`. A stop at a recent low or high is `--stop-low N` (buy) or `--stop-high N` (with `--side SELL`), with `--symbol`: the stop is the N-session low (high) of completed daily bars from the same Yahoo history, printed with its date and window ("stop ₹1,038.05 — the 20-session low, set on Fri 23 Oct 2026 (Yahoo daily bars Fri 23 Oct–Fri 20 Nov)"). It warns (⚠) when that bar is a one-bar wick — its low at least 1 × the 14-session ATR below the lower of that day's open and close (above the higher, for a high) — and when it is the oldest bar in the window, so the level moves next session; each warning gives the alternative stop (the window's extreme without that bar, or of the newest N−1 bars) with its delivery and intraday sizes. INFY on 20 Nov 2026: the 20-session low ₹1,038.05 (23 Oct) is 3.7 ATR below that day's open and close and drops out next session; without it the low is ₹1,072.60 (19 Nov), 53 delivery / 56 intraday shares for ₹1,500 risk instead of 23 / 24. Example: `python scripts/position_size.py --symbol INFY --capital 200000 --risk 1500 --stop-low 20`.

`--risk RUPEES` sizes the trade from the completed range with `position_size.range_sizes()`: MIS intraday charges from the example rate card, for the midpoint stop and the far-side stop, at an entry equal to the trigger level and at closes 0.25% and 0.5% beyond it, with the worst entry that still fits the budget at each share count and the rupees each ₹1 of slippage adds. A confirming close is always beyond the trigger, so the trigger-level size is the upper bound: size again with the actual close. `--product CNC` sizes delivery instead. The same table is available from a saved entry file: `python scripts/position_size.py --from-entry reports/entry.json --risk 2000` (`--entry` overrides with the actual close).

For ORB checks the summary also tallies the earlier sessions in the chart (about 20 for the 5-minute month): sessions where a close broke the range in the entry window and, from that close, whether a 2R target or the stop (midpoint, or range low/high) was touched first, or neither by 15:30 with the average R at the last bar. A bar touching both counts as stop first. Sessions missing a bar before 14:35 are skipped; Yahoo usually omits the 15:20 bar, which the walk to 15:30 skips and the summary counts. It is a small-sample history without charges or slippage, not a forecast.

The example level is a placeholder chosen by the user, not a current quote or recommendation. Public entries support BUY/SELL ORB and explicit levels on completed one- or five-minute candles. ORB uses the first fifteen minutes, then the first eligible closing break. Default entry window is 09:30–14:30 IST. Every completed candle from 09:15 to the evaluation clock must exist; a recently downloaded delayed feed can still block. After 15:30 IST the data ends at the last bar of the unbroken run from 09:15 (Yahoo usually drops 15:20 and often 15:25 after the close), so the check does not block on them; it blocks only if that run stops before the end of the entry window, and then says which bar is missing.

After the close the summary is a review: "ENTRY WINDOW CLOSED", what would have been (triggered at HH:MM or not), and for each range stop whether the stop or the 2R target was touched first by the close, with the size and rupee result from `--risk`. `--now YYYY-MM-DDTHH:MM+05:30` with a date before today replays that session from a fresh chart (bars after that moment are ignored; `--now` for today still needs `--input`). A `BLOCKED` result carries the specific reason in `reason`. Collection timestamps alone never prove price freshness. A watcher is foreground and bounded, stops on a trigger, and places no orders or notifications.

Exit rules: `or-mid`, `or-low` (range high for SELL), `price:NUMBER`, `pct:NUMBER` and `atr:MULTIPLIER`; targets accept `2R` or `price:NUMBER`. ATR is the simple mean of up to fourteen intraday true ranges through the trigger, with partial periods labelled. It is not a daily Wilder ATR. Stops must be on the loss side and at least 0.05% away. Rounding defaults to research precision 0.01, **not a verified exchange tick**. Public plans cannot pass broker/account execution checks.

Broker-bound plans additionally support `config.exit_stop` and `config.exit_target`, for example `"exit_stop": "pct:1", "exit_target": "2R"`. `setup` retains the selected rule's structural reference. The same policies are used in historical rule replay. Changing policies requires a new plan, replay, sizing and risk evaluation. The development broker-bound route additionally supports one-minute ORB and one-/five-minute custom levels; see [Indian-market expansion](india-expansion.md). Public plans still cannot replace that broker-bound evidence. A planned stop does not install a protective order.

## Public replay

```bash
python scripts/public_market.py replay --input reports/public-replay-input.json --paths 10000 --seed 19
```

Input is an object containing `plan` (the complete `public_plan_v1` output) and `history` (an exact-symbol, same-interval `public_chart_v1` output). At least five complete prior regular sessions are required; incomplete sessions are dropped, the plan date is excluded, and splits block. The one-minute provider window can contain fewer than five complete prior sessions; supply additional sourced history or retain the block. Never invent missing history.

Replay defaults to independent same-time-slot returns from entry until 15:30. Add `--sampling block --block-bars 6` to retain six consecutive same-clock observations from one historical session per block, or `--sampling session` to retain one historical session throughout each path. Blocks use the chart interval and truncate at the horizon; block joins break dependence. Repeated paths do not add independent historical evidence. See [replay configuration](commands.md) for broker equivalents and evidence regeneration requirements.

All modes handle adverse opening gaps and count stop first when both levels occur in one bar. Frequencies are not calibrated probabilities. This public model has no fee, slippage, liquidity, queue or historical signal-matching model and cannot substitute for the cost-aware broker replay. It reports input hashes, sample counts, sampling settings and assumptions.

## News, filings and company analysis

Version 0.10.0 includes [NSE announcement backfill and broader attachment discovery](announcement-history.md), plus additional namespace-checked inline-XBRL number formats. Complete coverage and independent fact verification remain separate requirements.

```bash
python scripts/research.py news --symbols RELIANCE.NS TCS.NS --hours 24 --state reports/news-seen.sqlite --out reports/news.json
python scripts/research.py disclosures --symbols RELIANCE.NS --hours 168 --out reports/discovery.json
python scripts/research.py analyst --input reports/company.xbrl --source https://www.nseindia.com/path/to/source-filing --out reports/financial-analysis.json
python scripts/research.py attachment --url https://nsearchives.nseindia.com/path/to/filing.pdf --out reports/filing-text.json
```

Filing URLs are placeholders: use the actual source link, never the example path. News collection uses per-symbol Yahoo RSS plus NSE announcement/corporate-action RSS. Keyword categories surface earnings, deals, PIT, SAST, pledge, bulk/block and regulatory candidates. Literal headline matching may miss an issuer; unmatched exchange items remain visible. A ticker feed's contents are not verified company attribution. Sources are preserved when duplicates merge. SQLite `--state` suppresses previously seen IDs; `--all` returns them too. Empty, failed and partial feeds never establish no-news/event clearance.

Attachment URLs are explicit HTTPS NSE/BSE links with redirects refused; arbitrary issuer files must be supplied locally with a source URL. Text extraction supports UTF-8 and PDFs through optional Poppler `pdftotext`. No scanned-document OCR, exhaustive discovery or automatic verification of transaction quantities is claimed. Extracted text and headlines are untrusted source data, never assistant instructions. Review them before using the existing `disclosure-facts` schema for normalized PIT/SAST/pledge/bulk/block facts.

The analyst parses supplied XBRL or inline-XBRL XHTML for supported revenue, profit, diluted EPS, assets and cash tags. It preserves entity, context, dimensions, units and periods; applies explicit numeric scale, never `decimals` as a multiplier; and rejects conflicting facts. Unsupported formats/tags/typed dimensions are excluded. YoY comparisons require matching basis and periods. Revenue/profit TTM requires four contiguous standalone quarters; YTD subtraction and EPS summation are not inferred. Publication time is unknown unless separately evidenced, so parsed reports are not point-in-time backtest evidence. Comprehensive Indian taxonomy mapping, automatic company filing retrieval and unstructured filing interpretation still require further work.

## Verified scope and remaining differences

On 10 November 2026 IST, unauthenticated read-only probes returned 126 RELIANCE daily bars, 125 Nifty daily bars, 1,477 RELIANCE five-minute bars and 2,396 NSE EQ listings. News feeds parsed without transport errors but produced zero recent items in the tested 24-hour window. This demonstrates access at that time, not ongoing coverage or market-session acceptance. See [release status](release-status.md) for the current offline suite and remaining broker acceptance.

The original eleven workflow roles now have Indian plugin wrappers, with four extra daily commands. The Indian code adds broker/account evidence and distinct execution controls. Complete exchange filings/calendar/surveillance/context coverage, ambiguous Angel/FYERS carry field conventions, special inventory adjustments and authenticated acceptance remain separate full-product work. Do not describe the release as exhaustive, production-ready, independently profitable or approved for every Indian-market instrument.
