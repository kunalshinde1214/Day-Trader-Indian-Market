# Changelog

## 1.5.0 — 21 November 2026

Built from eight rounds of independent scoring: six on 15 everyday questions (market, stocks, portfolio, F&O, sizing), then two on 30 questions the skill had never seen, with every number checked against NSE files, company filings, Screener.in and a broker margin calculator.

- **Live NSE levels and calendar:** daily reports use NSE's own live index levels during market hours, NSE's 20-day averages, the NSE holiday list (with names) and the Tuesday-expiry rule. Reports degrade with a labelled gap instead of stopping when an optional source fails, and no longer crash on a dropped connection.
- **"Why did the market move?"** `scripts/market_movers.py` gives direction first (and corrects a wrong premise), weakest and strongest sectors, the stocks that moved the index most, breadth, VIX, FII/DII and dated, filtered headlines. Sensex reports show Sensex drivers in Sensex points (estimated weights, checked against the index), not Nifty points. Index-point contributions are anchored to the NSE Indices factsheet weights (`assets/index_weights.json`, refreshed monthly; ignored after 100 days), and Sensex reports lead with one labelled "Sensex ≈" figure estimated from live Nifty (BSE does not serve scripts), with the last Yahoo quote beside it. Nifty Bank drivers are anchored to the Nifty Bank factsheet too. Intraday daily reports add "Possible reasons (headlines, unverified)" ranked by causal wording (routine filings and multi-day stories filtered out) and a "Cues" line (GIFT Nifty, US markets, gold, crude, rupee).
- **India Daily:** about 60 lines, with records, the broader market (Midcap 150, Smallcap 250, Nifty 500 breadth) and next-session levels.
- **F&O:** "If placed now (estimate)" re-pricing from NSE Clearing's intraday files at the live level; an indicative SPAN + exposure margin from NSE Clearing's SPAN files (SPAN matches a broker calculator on tested baskets; brokers' totals typically within ±10%); return on margin; strike grid with default wing widths per index, arguments checked before any download, live short-leg delta and a near-the-money warning, P&L at ±1/±2 SD, a compact `--summary` and `--row N` for the full payoff; a "Now" line in the chain with the expected move centred on the live level; a "Next session" block in the chain summary (1-SD range from the live level at the current ATM IV, and OI walls near spot); `--held` for positions already open; breakevens in the grid summary; an evidence-based broker range for hedged positions; honest previous-session comparisons (never "on the day" across a gap) and an expiry-day OI caveat; plain wording throughout.
- **Stock analysis:** trailing EPS cross-checked against net profit ÷ current shares, so a recent split or bonus no longer inflates P/E (Trent); level wording follows the trend; up to six cautions in the summary with the rest named; SAST lines say when a stake came from newly allotted shares, and promoter moves without a SAST filing are checked against NSE bulk/block deals; routine headlines ranked below real news; `--compare` for side-by-side answers; shareholding and promoter pledge from NSE filings (`scripts/shareholding.py`), including newly listed companies for sum-of-parts; full analysis of both successors after a demerger (Tata Motors → TMPV and TMCV); year-ago quarters distorted by one-offs are flagged; rupee growth from NSE filings for dollar reporters (Infosys); dollar statements converted at period FX rates; Wilder RSI; curated industry peers; recent ex-dates flagged; news gaps stated; neutral one-line-per-dimension trade-offs in `--compare` (no "leads on N measures" tally); debt/equity and P/B from the latest dated balance sheet; latest-quarter exceptional items explained; P/E history back to FY2019 from NSE filings with a 5-year median; promoter moves above 1 point linked to NSE SAST filings; market caps in lakh crore.
- **Screener:** `--preset dividend` (yield excluding specials, payout, free-cash-flow cover, dividend streak, with data gaps checked against NSE corporate actions), `--columns`, and banks excluded from debt/equity screens with a note.
- **Portfolio review:** demerged shares valued and included; XIRR compared with the Nifty 50; Income-tax Act 2025 wording for transfers from 1 April 2026; sale costs; long-term loss carried forward shown, using the demerger cost split the law applies; any unpriced holding makes totals and tax say "not a full figure"; when a typed buy price falls outside what traded then, P&L, return per year and tax are also shown at the middle of that period's range.
- **Trade plan:** one-command opening-range plan with `--risk`, where price is now relative to the trigger, the ORB history window, minutes left in the entry window, size for a close one tick past the trigger (NSE price-band ticks, `--tick` to override) and `--capital` to check exposure against the account; DP charges no longer double-count GST.
- **After the close:** between 15:30 and NSE's evening files, the option chain reads NSE's own closing option chain (today's OI, PCR and max pain) and options are re-priced at NSE Clearing's closing prices, so "selling now would get about ₹X" replaces yesterday's premium; the expected move is centred on today's close; the opening-range planner gives a review of what would have happened instead of blocking, and names the reason whenever it does block; daily reports keep the headline reasons and add look-ahead cues for the next session; "why did the market move" says "closed" and adds the broader market (Midcap 150, Smallcap 250, Nifty 500 breadth) and a cause for the weakest sector.
- **Data hygiene:** a bad Yahoo price bar can no longer set the Sensex day high or its pivots (Yahoo's own day range is used, isolated spikes are cut back and named in the data notes); one-off gains booked in other income (Reliance's Asian Paints stake sale) are flagged and stripped from EPS growth and the P/E band; profit before exceptional items includes the joint-venture share as filed; options max loss includes exercise STT so it is never smaller than any expiry row.
- **Stocks, more in one run:** curated business peers and listed-holding sum-of-parts by default for conglomerates (`assets/business_peers.json`); latest-quarter revenue growth and EPS growth ex one-offs in `--compare`; an eight-quarter promoter pledge trend and the 2% SAST threshold in shareholding; a sector column, `--max-per-sector` and debt in ₹ crore in the screener.
- **New routes:** `market_movers.py --sector IT|AUTO|METAL|"PSU BANK"|…` lists every stock in an NSE sector index with today's move, breadth, rank and sector-specific headlines; `--period week|month|ytd` / `--since` compares indices over a period (with `--indices` to choose the lead); the movers summary adds a market-mood block (Nifty P/E and P/B against their past-year range, India VIX against its 52-week range, all-NSE and Nifty 500 breadth, NSDL month-to-date FPI flows, one-month change) and corrects a wrong FII premise; `position_size.py --symbol --risk-pct --stop-pct --stop-low N` sizes from a stock name with delivery and intraday side by side; `fno.py` adds an iron-fly grid and relative strikes (`ATM-1`).
- **Weekends and holidays:** a "Market closed today" banner on every report, levels and pivots for the next session, the stocks that moved the index, headline reasons and cues since the last close, and the option chain for the next expiry (with holiday moves labelled).
- **Fundamentals checked against filings:** net profit and equity are cross-checked with NSE consolidated results (bank ROE, P/B and P/E were materially off in Yahoo's data); unrestated bonus years are adjusted and a P/E history that still looks broken is dropped with a reason; market cap uses NSE's latest share count including recent QIPs; 3-year revenue and profit growth; one-off gains in other income and exceptional items stripped from growth and P/E; debt held mostly by a listed lending subsidiary named, with debt/equity without it; low ROE explained from goodwill or cash; demerger-parent P/E flagged; listed subsidiaries in sum-of-parts checked against every Nifty 500 shareholding.
- **Shareholding:** fund houses matched across quarters ("LIC (all schemes)"), government holdings shown apart from DII, older filing formats read correctly, eight-quarter pledge and encumbrance trend, SAST encumbrance creation and release, and the 2% disclosure threshold.
- **F&O margin:** after the close margin uses NSE Clearing's closing file (SPAN matched Zerodha's calculator to the rupee on four positions); the expiry-day extra 2% exposure is shown separately with its SEBI circular; one-session-to-expiry warnings; cash needed for debit spreads; the closing option chain is read even when NSE returns empty rows, and a rejected chain is named instead of silently replaced.
- **Reliability:** previous-day NSE files are retried and cached; missing stock contributions print a rerun warning instead of disappearing; downloads have hard deadlines so a slow server cannot hang a report; the test suite runs with no network access.
- **Speed and reliability:** bounded retries and timeouts on every network source; typical reports run in 3–25 seconds. NSE's published daily index files are cached under the user's cache folder (`%LOCALAPPDATA%\india-day-trader\cache` on Windows, `~/.cache/india-day-trader/cache` elsewhere); set `INDIA_DAY_TRADER_CACHE=off` to disable.

## 1.4.0 — 18 November 2026

Fixes from the second round of user testing.

- **Daily commands restored:** `/india-day-trader:nifty`, `:sensex`, `:banknifty` and `:india-daily` are back (1.3.0 had folded them into `market`). Docs no longer call bare `/nifty` a registered command.
- **Daily reports:** each index shows the last close and the day's change with dates; India Daily opens with a comparison table and India VIX; plain-language, dated headlines; a real `--brief`; after the close the official daily close is used when Yahoo has it; the NSE closing route works on Windows (it failed on the ₹ sign); Sensex is labelled as Yahoo data with a clear note on the closing route; closed days name the last session and the command for its closing report.
- **Stock analysis:** renamed and demerged symbols are explained from a sourced alias list (TATAMOTORS → TMPV and TMCV, ZOMATO → ETERNAL, and others); price history is cut at a demerger so ranges and P/E bands are not polluted; P/E is compared on the same basis as its history band; one 1-year return definition everywhere; bull and bear levels in order; placeholder symbols and REIT units excluded from peers; a weak-quarter note on trailing EPS.
- **Screener:** retries transient failures and separates "failed to load" from "missing data", so repeated screens agree; `nifty100`, `nifty200` and several universes at once; 12-month dividend yield per share with special dividends flagged; any filtered or sorted field is shown as a column.
- **Simple position sizer:** `scripts/position_size.py` answers "how many shares for ₹X risk with this stop" with Indian charges, exposure and margin, with no broker setup. The `trade-plan` skill starts there.
- **F&O:** expiries labelled weekly or monthly from the data, holiday-moved expiries flagged, `--weekly`/`--monthly` selectors, and a next-session one-standard-deviation move for "kal kya lagta hai".
- **Portfolio review:** typed buy prices checked against that period's trading range; a sourced list of 14 bonus issues applied automatically (`--split` to override); demerged shares the user should hold are named in the summary; corporate-action notes list steps in date order.
- **Skills:** guidance for Tata Motors' two successors, shareholding and pledge data via exchange filings, and asking for option strikes rather than choosing them.

## 1.3.0 — 18 November 2026

Easier to use, and the remaining issues from the user-testing review.

- **8 skills instead of 19:** `stock` (analysis, results, disclosures), `fno`, `screener` (fundamental screens and chart-pattern scans), `portfolio`, `market` (Nifty, Sensex, Bank Nifty), `news`, `trade-plan` (entry, stop, replay, size, risk) and `broker`. Old commands such as `/india-day-trader:nifty` and `/india-day-trader:stock-analysis` are replaced by `/india-day-trader:market` and `/india-day-trader:stock`. Always-loaded skill descriptions shrink from about 845 to 335 words.
- **Short core skill:** the main skill file is now research and paper trading only (770 words, down from 1,744). Broker plumbing, accounting, streaming and the live-order and protection tools moved to [advanced tools](references/advanced.md), as an optional module that stays disabled unless the user explicitly asks.
- **Answer templates** for "which looks best", "kal kya lagta hai / where tomorrow", "is it cheap", "should I sell this option", partial purchase dates and bonus shares.
- **Summary modes:** `--summary` on stock analysis, F&O chain and payoff, and the portfolio review gives an answer-first digest.
- **Stock analysis:** `--peers` compares with companies in the same businesses; index peers are labelled as such, and a warning appears when the company is more than three times the size of every index peer. Google News redirect links are no longer dumped into the text report (they stay in JSON).
- **Portfolio review:** demergers from a curated, sourced list (ITC → ITC Hotels, Reliance → Jio Financial, Siemens → Siemens Energy India, Raymond, Aditya Birla Fashion, Tata Motors) are flagged with the new shares due and the published cost split; `--apply-demergers` applies the split to dated lots.
- **F&O:** OI change shown as signed additions and reductions that add up to the net; day-over-day PCR, max pain, ATM IV and OI; India VIX taken from the chain's own date; a 15–20 row payoff table; probability of profit and of finishing above each breakeven implied by option prices; and a today (T+0) value column and chart line.
- **README:** a "See it in action" section with real, dated output and a payoff chart.

## 1.2.1 — 18 November 2026

Trust fixes found by testing the skill as ordinary users would.

- **Stale prices:** when Yahoo's newest daily bar has no close (seen just after midnight IST), the reports no longer silently fall back to the previous day. The session is rebuilt from Yahoo's last-trade quote and labelled, or the gap is stated. Stock analysis, the screener and the portfolio review show the price date; "still trading" appears only during market hours.
- **Bonuses and splits in the portfolio review:** Yahoo's split events are applied to dated holdings and lots (split by default; `--bonus SYMBOL` adds nil-cost bonus lots dated on the ex-date, as Indian tax treats them). Holdings take an optional `bought` date (a month is enough). Holdings that cannot be adjusted get a ⚠ in the table. A pre-bonus HDFC Bank holding now shows about −1% instead of −50%.
- **EPS cross-check:** trailing EPS is compared with the last four quarters. Wrong trailing figures are replaced by the quarterly sum (Adani Enterprises: Yahoo −3,381.91 against +46.55), one-quarter distortions are flagged with a typical-quarter P/E (HPCL), and annual EPS jumps above +200% or below −60% are flagged (JSW Steel). Flags appear in stock analysis and the screener.
- **Tax with missing dates:** the "if you sold everything today" estimate is a best-to-worst range naming the holdings whose dates would narrow it, instead of silently ₹0.
- **Banks:** ROE and P/B carry a warning that Yahoo's book equity can differ a lot from the bank's reported net worth (HDFC Bank: about ₹8.2 lakh crore on Yahoo against about ₹5.5 lakh crore reported).
- 19 regression tests, each checked against a deliberately broken version of the fix.

## 1.2.0 — 18 November 2026

- Add a stock screener (`scripts/screener.py`, skill `stock-screener`) over Nifty 50, Next 50, 500, midcap 150, smallcap 250 and 13 sector indices, with filters and sorting on P/E, P/B, ROE, margins, debt/equity, growth, yields, market cap, 1-year return, distance from high and RSI. Stocks with missing data or failed loads are listed, never counted as matches.
- Add a portfolio review (`scripts/holdings_review.py`, skill `portfolio-review`): holdings from a broker connector, Zerodha CSV or JSON; P&L, weights, sector exposure and concentration; FIFO capital gains from a tradebook with STCG 20% and LTCG 12.5% above ₹1,25,000 as labelled, adjustable defaults; items not modelled are listed.
- Stock analysis now shows sector peers (smallest NSE sectoral index, ranked by market cap, with group medians), a five-year P/E band, and an optional one-year SVG price chart (`--chart`).
- Fix: companies reporting financials in US dollars (for example Infosys) had P/E and P/B overstated about 90 times; money values are now converted at today's USD/INR rate and labelled.
- The news briefing ranks NSE announcements as material, notable or routine using the exchange subject line, hides routine filings (fund NAVs, analyst-meet notices, newspaper copies) unless `--include-routine`, and prints UTF-8 correctly on Windows.
- Answers follow the user's language, including Hindi and Hinglish, while keeping figures and market terms intact.
- Add tests against small recorded excerpts of real Yahoo and NSE responses; fix a template placeholder in the stock-analysis skill.

## 1.1.0 — 17 November 2026

- Add F&O analytics (`scripts/fno.py`, plugin skill `fno-analysis`): option chain around the money with OI, OI change, IV and delta; put-call ratio by OI and volume; max pain; OI support/resistance; long/short build-up; ATM IV, put-call skew and expected move; futures basis; India VIX percentile.
- Implied volatility and Greeks use Black-76 on a put-call-parity forward (NSE options are European); untraded strikes are never given an IV from stale prices.
- Multi-leg strategy payoff for options and futures: net premium, maximum profit and loss (unlimited or price-to-zero cases labelled), breakevens, position Greeks and a shaded SVG chart.
- Data from NSE's free end-of-day F&O bhavcopy, walking back over holidays; live chains from a broker connector through an `fno_chain_v1` input file.
- 15 offline tests, including IV round-trips, parity, max pain, build-up and exact iron-condor arithmetic.

## 1.0.0 — 17 November 2026

- Complete the skill's scope: data the scripts do not bundle (BSE filings and BSE-only stocks, results commentary, concalls, macro and RBI/SEBI events, breaking news) is now fetched at request time through web research with source, URL and date ([web research](references/web-research.md)).
- Support each user's own broker connector (for example Zerodha Kite, Upstox, Dhan, Groww) for reading holdings, positions, funds, quotes and candles; orders stay the user's action ([broker connectors](references/broker-connectors.md)).
- Add web-research and connector steps to the stock-analysis, daily, news, disclosure, results, ticket, risk and sizing skills.
- Replace the open-ended tracker with a [completion status](references/completion-tracker.md) separating skill coverage, request-time research, per-user validation and optional enhancements. The release manifest now records `skill_scope_complete: true`.
- Refresh the README feature matrix for 0.10.1 capabilities.

## 0.10.1-carry-protection-alpha — 17 November 2026

- Add resumable NSE announcement-history windows feeding the existing filing backlog; preserve cursors on source gaps or archive failure.
- Reconcile sourced inventory buy/sell events against every observed fill, alongside settlement and corporate-action events. Accounting does not enable restricted inventory execution.
- Extend mixed cash MIS entries to explicitly enabled shorts and existing signed MIS exposure; use absolute gross exposure and conservatively reserve all outstanding MIS units.
- Add Dhan multi-order basket estimates using the official SDK request spelling, without summing single-leg estimates or claiming authenticated acceptance.
- Add explicit passive-limit queue/latency/partial-fill cost scenarios and account-separated collateral/cash-component stress.
- Add carry-aware bounded protection and sequential group coordination. Preserve delivery/session checks, per-policy human arming, shared same-account execution logs and revocation after failures. One account per broker per process; no services activated.
- Extend end-user read-only probes to supported carry inventory, basket contracts and cash depth. Real order and production acceptance remain unperformed.

See [0.10.1 scope and remaining work](references/development-progress.md).

## 0.10.0-market-workflows-alpha — 16 November 2026

This public alpha packages the sequential development work below. The full specification remains incomplete; account acceptance belongs to each end user. The final local suite passes 936 tests.

- Extend configured one-minute evaluation to VWAP reclaim, Gap and Go, 20-day breakout, bull flag and oversold bounce. Preserve the fifteen-minute opening window, exact candle/history interval binding and point-in-time daily evidence. Verify 25 broker/rule paper combinations; pre-open/regime models and broader special sessions remain incomplete.

- Extend read-only basket-prefix and partial-fill margin research to Upstox, Angel One and FYERS. Validate input/account identity and changes, retain request/response hashes, keep missing initial/final and echoed identity semantics explicit, and separate FYERS proposed-order from account-total requirements. Dhan baskets and broader collateral/market stress remain incomplete.

- Extend cash-equity depth collection to Upstox, Dhan, Angel and FYERS; preserve source timestamp semantics and block last-trade-only books from liquidity sizing. Add quantity-dependent replay fees with brokerage caps and optional explicit latency/linear-impact cost sensitivity, binding changed policies to regenerated replay evidence. Queue/fill simulation and authenticated acceptance remain outstanding.

- Add credential-free dated NSE closing reports through daily commands, with exact index/CSV dates, observed EQ breadth, optional reviewed constituent breadth and separately dated FII/DII/GIFT context. Keep missing Sensex coverage, stale GIFT observations and unknown release/finality explicit; do not invent intraday candles or calendar clearance.

- Add bounded daily NSE announcement backfill with source hashes, explicit partial coverage and JSON/Markdown output. Retain structured issuer identities and description attachment links through matching and the durable filing backlog.
- Add namespace-aware inline-XBRL numeric transforms, including Indian digit grouping, and reject unsupported continuations/exclusions instead of silently concatenating their text. Unstructured numerical fact verification remains outstanding.

- Add reviewed historical membership intervals with publication cutoffs, no fallback across gaps, ISIN-based additions/removals/renames and hash-bound daily-report imports that retain partial verification status.
- Add sourced split/bonus reconstruction for explicitly raw daily equity prices, preserving ISIN chains and synthetic volume factors. Block double-adjustment, unavailable information, unsupported actions and conflicting identities; broader corporate-action/provider coverage remains incomplete.

- Add automatic NSE surveillance-catalogue discovery, dated GSM/ASM/ESM/T2T observations and separate F&O ban collection. Preserve unknown BSE/effective-session status, reject malformed or conflicting reports, retain source hashes and integrate component failures into repeated collection. Verify current NSE calendar/status endpoints and the observed `Close` spelling. Public source smoke tests do not establish comprehensive exchange coverage.

- Add a separate opt-in mixed cash-entry workflow for long MIS orders alongside supported long CNC/MIS equity exposure across the five broker adapters. Reconcile carry accounting and intraday risk reservations, count outstanding buys and carry losses, enforce exact margin and gross caps, and preserve two-stage approval/shared durable recovery. Short/derivative/restricted portfolios, new carry entries and carry-fee evidence integration remain unsupported.

- Add sourced non-trade inventory reconciliation for settled, T1, pledged, MTF and entitlement lots, with explicit settlement/pledge transfers, funded principal repayment, whole-unit split/bonus events and audited corrections. Requires local evidence hashes and exact closing-statement agreement; never authorizes orders or guesses provider conventions.

- Add Angel One/FYERS reducing carry exits, stop-limits and cancellation, preserving native products, exact session/delivery evidence and complete account reconciliation.
- Tighten recovery to match cancellation variety and Angel placement variety.

## 0.9.9-broker-workflows-alpha — 14 November 2026

- Redesign the repository README with original self-contained SVG artwork, a research-workflow illustration, install paths and a current feature matrix. Include the visuals in validated release archives.

- Extend reducing carry execution to Dhan with CNC/MARGIN product mapping, session checks and product-aware recovery; unsupported inventory remains blocked.

- Add Upstox mixed-inventory reducing exits/stops/cancellation with product-aware recovery and explicit placement-session evidence.
- Add bounded repeat public collection with immutable cycle reports and persistent filing backlog.
- Add bounded partial-fill quantity grids for exact Kite basket-margin scenario queries.
- Add a disabled-profile generator for all five brokers; publisher brokerage credentials are not required.

## 0.9.8 — protection operations

- Record terminal runner health after completion, exhaustion or failure; test revocation between cycles and flat observations.
- Add a local acceptance-evidence inventory with account, hash and freshness checks. Missing end-user broker, installed-host and production evidence remains explicit; reports never authorize execution.
- Document the existing opt-in bounded runner, depth collector and partial-fill margin scenarios, including their acceptance limits.
- Extend supported Kite mixed-inventory reducing actions with stop-limits, exact fresh broker quotes and delivery checks. Other-broker carry execution, comprehensive data coverage and real-account acceptance remain unfinished.

## 0.9.8-stock-analysis-alpha

- Add `stock-analysis`: a one-stock report with price vs Nifty 50, trend/RSI/volatility/support/resistance, financial ratios (P/E, P/B, ROE, margins, debt/equity, current ratio, interest cover, FCF and dividend yield, growth), 7-day news from Google News, Yahoo and NSE, and an outlook built from the stock's own historical 1-week/1-month/3-month return ranges with bull/bear levels. Uses the newest EPS and flags later splits/bonuses, stale figures and bank-style statements. 11 offline tests.
- Rewrite the README in plain language with example prompts and install steps; consolidate the skill's repeated caveats into one safety section.
- Rewrite all plugin skill descriptions with the phrases users actually type; add a `python3`/`py` fallback to every skill; release checks now reject `: ` in skill descriptions, which breaks YAML.
- License 1.3: plain copyright line for original contributions; third-party MIT rights unchanged.
- Risk disclosure: add SEBI's retail algo-trading framework and what it requires of API orders.

## 0.9.7-india-expansion-alpha

- Fix daily plugin routes on Windows: call `daily.py nifty` (no leading slash), since Git Bash rewrites `/nifty` into a filesystem path.
- Fix live public intraday briefings blocking: drop Yahoo's trailing still-forming, off-grid bar and record it as `dropped_in_progress_bar`.

- Add a persistent intraday protection controller with stop/target/cutoff decisions, cancel-before-replace sequencing, fresh remaining quantities, and separate preview/human-operated step commands. Unattended submission remains unfinished.

- Harden protection diagnostics against overlapping stop/target exits, pending entries, ambiguous states and product mismatches; count only remaining unfilled units.

- Add a separate opt-in human-operated Kite mixed-inventory reducing-exit/cancellation workflow; preserve CNC/NRML/MIS identity through reconciliation, payloads and recovery. Add delivery-authorization evidence checks, shared-intent ambiguity blocking and regression tests. No authenticated acceptance or unattended execution is claimed.

- Add broker-bound one-minute ORB and one-/five-minute point-in-time custom levels across five candle adapters, replay, watcher and risk checks.
- Add optional visible-depth/participation sizing caps and adverse stop-slippage reserves; exact Kite basket-margin prefix estimates remain research-only.
- Add combined Indian public market/calendar/news/filing collection and a durable bounded filing backlog.
- Add optional dated instrument-session evidence for separate continuous-market and broker cutoffs; document current CAS and API-algo source findings.
- Broad mixed-inventory execution, unattended protection and actual broker/production acceptance remain outstanding.

## 0.9.6-dependent-replay-alpha

- Add optional contiguous same-clock block and whole-session bootstrap replay for broker and public workflows; retain independent sampling as the default.
- Bind risk replay evidence to sampling settings and integer seed. Older reports without sampling metadata must be regenerated.
- Document finite historical evidence and the remaining pre-open, liquidity, slippage and size-specific cost gaps.

## 0.9.5-five-broker-carry-alpha

- Add conservative Angel One/FYERS carry collectors with repeated books, exact identity, fill reconciliation and fresh marks; document unresolved provider multiplier/collateral conventions explicitly.
- Correct FYERS derivative quote keys to use provider NSE/BSE prefixes.
- Add 44 offline collector tests. All five brokers now have limited carry collector code; full-product and authenticated acceptance remain incomplete.

## 0.9.4-dhan-carry-alpha

- Add read-only Dhan carry collection with repeated books, account checks, exact holding identity, actual fill reconciliation and fresh marks.
- Keep availability reservations separate from executed sales; validate delivery/derivative product mapping and overnight position totals.
- Add 26 offline tests covering the supported subset and failure paths. Authenticated acceptance remains pending; live execution stays intraday-only.

## 0.9.3-upstox-carry-alpha

- Add read-only Upstox carry collection with repeated holdings/positions/orders/trades/MTF reads and profile identity checks.
- Map delivery products by instrument kind, reconcile opening inventory and actual fills, preserve reservation-versus-fill distinctions and require fresh last-trade marks.
- Add 30 regression tests for inventory/P&L, derivatives, unsupported exposure, account changes, read-only routes and CLI output. Live execution remains intraday-only; authenticated acceptance is pending.

## 0.9.2-broker-streams-alpha

- Add Dhan and Angel binary quote streams and isolated Upstox/FYERS official SDK workers; enforce TLS certificate/hostname checks, bounded reconnects, quote identity/freshness checks and stop latches.
- Integrate all five brokers into analysis/watch stream selection and render-only Windows/systemd deployment. Preserve independent live-execution evidence checks.
- Add 17 offline regression tests and document separate optional SDK environments. SDK imports were checked; authenticated broker streaming remains end-user acceptance work.

## Unreleased â€” Indian market news briefing

- Add five official NSE results/disclosure feeds, exact issuer-candidate matching and bounded official attachment discovery/retrieval with financial-XBRL analysis or explicit text-review fallback. Add eleven workflow regression tests.

- Support NSE's local timestamp format in the personal news reader with visible assumed-IST provenance; preserve strict default parsing for other consumers. Add four regression tests.

- Add a market-wide news collection and Markdown/JSON display workflow with default NSE feeds and optional personal-use Economic Times markets RSS, publication-time checks, source coverage and persistent deduplication.
- Add eight offline news workflow tests. Earlier v0.9.0 assets remain unchanged.

## 0.9.0-public-workflows-alpha â€” 10 November 2026

- Add Claude plugin/marketplace manifests and fifteen workflow skills: eleven roles plus India daily, Nifty, Sensex and Bank Nifty.
- Add credential-free Yahoo chart and NSE EQ listing collectors, multi-tag daily screening, public daily briefings, one-/five-minute ORB and level entries, bounded watching and public historical replay.
- Add explicit price/percentage/ATR/structural stop and R/price target rules. Broker-bound plans and historical replay bind these policies; policy changes require regenerated evidence.
- Add per-symbol public news, disclosure headline discovery, source-preserving persistent deduplication, supplied XBRL/inline-XBRL facts and comparable YoY/contiguous-quarter TTM, plus explicit filing text/PDF extraction.
- Extend release allowlists and checks to include every plugin wrapper and manifest. Public research stays outside broker execution clearance; authenticated acceptance and comprehensive production data remain pending.

## 0.8.1-public-alpha â€” 8 November 2026

- Prepare the public source-available repository with installation instructions, a feature table, daily command examples and explicit alpha limits.
- Update the personal-use license to 1.2, retaining third-party MIT rights and the applicable GitHub platform permissions.
- Add trading-risk disclosure, security reporting and contribution guidance; broaden ignored local-output directories.
- Fix the standalone public-feed command to parse Nifty 50/Bank Nifty constituent CSVs correctly and record the retrieval time after downloading. Source availability and membership effective dates remain separate acceptance requirements.
- Include 463 offline test methods. Live remains disabled by default; no authenticated broker acceptance or real-trade validation is claimed.

## 0.8.0-portfolio-alpha â€” 8 November 2026

- Add limited read-only Kite carry-account collection against reviewed opening inventory.
- Reconcile holdings/position overlap, matched trades and fresh quotes; reject unsupported inventory or ambiguous evidence.
- Validate all 460 offline tests and the unpacked collector-to-journal workflow.

## 0.7.0-streaming-alpha â€” 7 November 2026

- Add Kite quote streaming, monitoring deployment-file rendering and optional daily constituent/news collection.
- Validate 425 offline tests, loopback WebSocket transport and native Windows task XML parsing.

Earlier local milestones introduced six scanners, five-broker read-only adapters and human-operated execution, the four daily commands, journal/service operations and canonical carry accounting. Historical checkpoint packages do not establish production acceptance.
