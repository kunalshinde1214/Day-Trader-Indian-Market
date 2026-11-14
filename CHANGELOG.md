# Changelog

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
