# Changelog

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
