# Requirements and implementation status

Development checkpoint: **8 November 2026**, version **0.8.1-public-alpha**. Includes public installation/risk/license documentation, a constituent-feed CLI fix and limited read-only Kite carry collection against reviewed opening inventory. Streaming, monitoring deployment files, daily index reports, paper analysis and human-operated execution are included. Each user supplies their broker/data entitlement, approved limits and sourced evidence. Example capital and loss limits are not approvals.

The full platform is not complete. Read [release status and remaining requirements](release-status.md) before making publication claims. All six requested scanners and five read-only adapters have offline coverage. A separate human-operated execution tool covers all five brokers and remains disabled by default; see [execution](execution.md).

## Source inventory

All three originals are preserved without changes in `../research/`; SHA-256 checksums and file sizes are in `../research/manifest.json`. `report-pdf.txt` preserves the PDF's 22-page boundaries; `report-docx.txt` includes paragraph and table-cell text in document order. Both reports describe the same blueprint. Their charts concern contextual statistics, not missing strategy inputs. The standalone Markdown provides the detailed proposed config and stage rules.

| Source | Material used |
| --- | --- |
| Research PDF, physical pages 4–7 | Eleven-stage architecture, provenance, original safeguards and portability |
| PDF pp. 8–15 | India market mechanics, calendars, instruments, restrictions, regulation and event risk |
| PDF pp. 16–17 | Cost components, worked option example, ledger/tax requirements |
| PDF pp. 18–21 | Stage mapping, broker interface, defaults and phased roadmap |
| PDF p. 22 | Research source leads; verified separately where used operationally |
| Research DOCX | Cross-check of tables and corresponding narrative; full text retained |
| Master conversion prompt | Entry/flat times, risk defaults, commands, files and future execution gates |
| Existing local MIT project | File pipeline, same-slot bootstrap, stop-first ambiguity rule, four-limit sizing, sequential reservation and ticket fingerprints |

Original code references: `trader-scanner/scan.py` (`orb_hit` / completed-bar approach), `trader-timemachine/timemachine.py` (`build_pool`, `simulate`), `trader-budget/size.py`, `trader-risk/check.py`, `trader-broker/ticket.py`. The India code adapts these mechanisms, rather than copying US clocks, APIs, tax rules or order payloads. The upstream MIT notice is retained in `../third-party/day-trader-skill.LICENSE`. Kunal Shinde is credited as skill creator and maintainer; `../LICENSE` provides restrictive free personal-use terms only for material he is legally authorized to license under those terms, without a blanket copyright-ownership claim.

## Module coverage

| Module | Implemented | Remaining full-product work |
| --- | --- | --- |
| Daily briefings | Four aliases; three session phases; completed-bar scenarios; Markdown/JSON; Kite index history and optional constituent quotes; partial NSE announcement collection; public or reviewed membership | Authenticated feed acceptance; public membership transport/effective-date acceptance; complete macro/calendar/closing breadth feeds; additional index providers |
| Universe / scan | Six rules; exact instrument/context checks; sourced membership/master/20-session turnover builder; Nifty 500 collector code | Live feed acceptance; automatic F&O membership/restrictions and historical membership |
| News / analyst / disclosures | Named RSS collector, deduplication, source/release metadata, structured comparative reports and PIT/SAST/pledge/bulk/block processing | Comprehensive exchange coverage, symbol resolution and attachment parsing |
| Entry / exit plan | Selected rule, completion/expiration, structural stop/target; bounded polling and optional Kite stream quotes; separate reducing limit/stop-limit workflow | Other-broker streaming; authenticated stream and protective-order acceptance |
| Replay | Seeded 10,000 paths; strict prior history; selected-rule historical matching; mandatory point-in-time references | Dependence-aware/pre-open models, slippage/liquidity and size-specific path costs |
| Size / margin | Four conservative caps, stop charges, daily reservation, exact whole-lot margin proposal search | Portfolio/hedged margin models; authenticated margin acceptance |
| Risk / ticket | Broker/account/plan binding; freshness, events, restrictions, expiry, exact margins, monitor heartbeat; paper previews; two human confirmations and pre-submit rechecks | Production policy/account acceptance; integration where broker-hosted approvals are required |
| Alerts / halt | Local outbox; opt-in HTTPS delivery; locked supervisor; bounded Kite stream; Windows/systemd user-deployment files with operator commands; panic previews | Real receiver and OS activation/runtime acceptance; other-broker streaming; authenticated emergency-control acceptance |
| Journal | Five-broker raw intraday trade import; idempotent fills; canonical reconciliation; audited final fees; explicit conservative provisional-fee policy; risk groups rebuilt from execution log; separate sourced carry opening/inventory/P&L accounting; limited Kite carry collector with reviewed opening evidence | Other-broker carry normalization; T1/pledged/MTF inventory and corporate-action/settlement adjustments; mixed-account risk/execution integration; account-specific tariff/timestamp acceptance |
| Live execution | Five transports; exact per-action human review; committed intents before HTTP; no retries; partial-fill/cancel reconciliation, explicit timeout binding, stop-limit/exit quantity protection | Authenticated acceptance, unattended protection/service operation, broker-hosted approval gateway where required |
| Expiry / context | Actual-contract expiry/settlement checks; sourced spread/OI/Greeks diagnostics; dated FII/DII heuristic | Live context collectors and settlement/volatility models |
| Charges | Effective-dated matching-broker cards; per-segment brokerage/minimum/fixed overrides; IPFT/clearing/GST support | Current per-user tariffs and instrument-specific exemptions/rounding; other-broker init cards intentionally incomplete |
| Brokers | Five read-only data/account/margin adapters; explicit acceptance harness; quote/candle bridge; separate Kite streaming client | Authenticated acceptance; public source acceptance gaps; other-broker streaming |
| Release | Packaging allowlist, deterministic hashes, GitHub CI definition, public README, risk/security/contribution guidance, retained licenses and attribution | Verify the target commit's GitHub CI and release status; full-product acceptance remains outstanding |

## Acceptance

The offline suite exercises every setup/broker combination, including 10,000-path replay and recomputed risk before paper tickets. See [release status](release-status.md) for test count and acceptance limits. [Setup rules](setups.md) documents exact definitions; [operations](operations.md) documents ingestion, journal, monitoring and proposal schemas.

Dhan's earlier public-master smoke test passed; current Angel/FYERS public-master and Nifty 500 collector attempts did not pass. No authenticated account test has been performed. Public downloads and synthetic fixtures cannot validate credentials, subscriptions, holdings, live execution or strategy performance.

The preserved research archive and upstream checkout remain unchanged. They are excluded from the public release archive. No real trade, brokerage-account mutation, message delivery or global install was performed. Publication status and CI results are visible in the [public repository](https://github.com/kunalshinde1214/Day-Trader-Indian-Market).
