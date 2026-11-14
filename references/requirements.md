# Requirements and implementation status

Development checkpoint: **14 November 2026**, version **0.9.9-broker-workflows-alpha**. Adds sixteen Claude plugin skills, credential-free charts and screening, one-minute/custom-level research, explicit exit policies, news and supplied XBRL analysis. Limited read-only five-broker carry collection, streaming, monitoring deployment files, daily reports, paper analysis and human-operated execution are included. Each user supplies their broker/data entitlement, approved limits and sourced evidence for broker-bound workflows. Example capital and loss limits are not approvals.

Implemented development includes broker-bound minute ORB/custom levels, depth stress in sizing, Kite basket-margin estimates, a combined public collector and persistent filing backlog; see [Indian-market expansion](india-expansion.md) for scope and remaining work. The full platform is not complete. Read [release status and remaining requirements](release-status.md) before making publication claims. All six requested scanners and five read-only adapters have offline coverage. A separate human-operated execution tool covers all five brokers and remains disabled by default; see [execution](execution.md).

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

The maintained [README feature matrix](../README.md#what-is-implemented) is the release's capability summary. The [release status](release-status.md) records validation and remaining engineering. Keep these distinct from each user's account acceptance.

- Sixteen plugin commands, four daily aliases, one-stock public analysis and six scanners are implemented. One-minute broker entries cover ORB/custom levels; other rules remain five-minute.
- Five broker data/account/streaming adapters and limited carry collectors are implemented. Mixed-inventory reducing execution supports Kite, Upstox and Dhan; Angel/FYERS carry execution remains pending. New mixed-inventory entries and unsupported inventory remain blocked.
- Public collection, bounded repeated runs and persistent filing backlog are implemented with partial source coverage. Comprehensive calendars/surveillance, BSE filings, historical membership/corporate actions and verified issuer/fact mapping remain pending.
- Replay supports independent/block/session sampling. Optional depth stress, Kite cash-equity depth collection and exact Kite basket prefixes/scenarios/quantity grids are implemented. Other-broker portfolio margins, queue/market-impact, size-dependent replay and regime models remain pending.
- Human-approved execution and a separately human-armed bounded intraday protection runner are implemented, disabled by default. Multi-instrument/carry protection coordination, production operation and broker-required approval integration remain outside verified scope.
- Disabled user profiles, read-only probes and acceptance inventories are implemented. Users supply credentials, permissions, entitlements, tariffs, host/service validation and observed account evidence. No publisher brokerage account is required.

## Acceptance

Account ownership and acceptance are end-user responsibilities: see [bring-your-own-account setup](user-setup.md). No developer brokerage account is required to publish the documented public/paper skill. This changes release prerequisites, not the evidence: publisher authenticated acceptance remains unperformed. Unimplemented engineering capabilities remain roadmap work.

The offline suite exercises every setup/broker combination, including 10,000-path replay and recomputed risk before paper tickets. See [release status](release-status.md) for test count and acceptance limits. [Setup rules](setups.md) documents exact definitions; [operations](operations.md) documents ingestion, journal, monitoring and proposal schemas.

Dhan's earlier public-master smoke test passed; current Angel/FYERS public-master and Nifty 500 collector attempts did not pass. No authenticated account test has been performed. Public downloads and synthetic fixtures cannot validate credentials, subscriptions, holdings, live execution or strategy performance.

The preserved research archive and upstream checkout remain unchanged. They are excluded from the public release archive. No real trade, brokerage-account mutation, message delivery or global install was performed. Publication status and CI results are visible in the [public repository](https://github.com/kunalshinde1214/Day-Trader-Indian-Market).
