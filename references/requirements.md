# Requirements and implementation status

**Historical page.** For the current release (1.5.1, 12 plugin skills, 1,883 offline tests) read [release status](release-status.md) and the [completion status](completion-tracker.md). The requirements sources below still apply. The development checkpoint and module counts further down describe version 0.10.0 and are kept unchanged as a record; their counts (sixteen skills) are not current.

Development checkpoint: **16 November 2026**, version **0.10.0-market-workflows-alpha**. Includes sixteen Claude plugin skills, credential-free charts and screening, one-minute/custom-level research, explicit exit policies, news and supplied XBRL analysis. Limited five-broker carry collection/execution, streaming, monitoring deployment files, daily reports and paper analysis are included. Each user supplies their broker/data entitlement, approved limits and sourced evidence for broker-bound workflows. Example capital and loss limits are not approvals.

Implemented development includes all six broker-bound scanners/custom levels at one/five minutes, depth stress and replay cost sensitivity, four-broker basket estimates, partial public collection and a persistent filing backlog; see the [completion tracker](completion-tracker.md) for current scope. The full platform is not complete. Read [published release status](release-status.md) before making publication claims. All six requested scanners and five read-only adapters have offline coverage. A separate human-operated execution tool covers all five brokers and remains disabled by default; see [execution](execution.md).

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

- Sixteen plugin commands, four daily aliases, one-stock public analysis and six scanners are implemented. All six broker-bound scanners plus custom levels support one/five-minute evaluation; regular-session assumptions still apply.
- Five broker data/account/streaming adapters and limited carry collectors are implemented. Reducing execution supports their supported carry inventory; separate mixed cash MIS entries support existing long CNC/MIS exposure. Restricted inventory and broader entry combinations remain blocked; see the [completion tracker](completion-tracker.md).
- Public/repeated collection, NSE announcement backfill, dated surveillance/ban observations, reviewed membership/action histories and a persistent filing backlog are implemented with partial coverage. Comprehensive calendars, BSE feeds and independent historical/issuer/fact verification remain pending.
- Replay supports independent/block/session sampling, five-broker cash-depth observations, quantity-dependent fees and explicit cost sensitivity, plus Kite/Upstox/Angel/FYERS basket prefixes/scenarios/grids. Dhan/FYERS books remain research-only. Dhan baskets, broader collateral stress, queue/fill simulation, calibrated impact and regime models remain pending.
- Human-approved execution and a separately human-armed bounded intraday protection runner are implemented, disabled by default. Multi-instrument/carry protection coordination, production operation and broker-required approval integration remain outside verified scope.
- Disabled user profiles, read-only probes and acceptance inventories are implemented. Users supply credentials, permissions, entitlements, tariffs, host/service validation and observed account evidence. No publisher brokerage account is required.

## Acceptance

Account ownership and acceptance are end-user responsibilities: see [bring-your-own-account setup](user-setup.md). No developer brokerage account is required to publish the documented public/paper skill. This changes release prerequisites, not the evidence: publisher authenticated acceptance remains unperformed. Unimplemented engineering capabilities remain roadmap work.

The offline suite exercises every setup/broker combination, including 10,000-path replay and recomputed risk before paper tickets. See [release status](release-status.md) for test count and acceptance limits. [Setup rules](setups.md) documents exact definitions; [operations](operations.md) documents ingestion, journal, monitoring and proposal schemas.

Dhan's earlier public-master smoke test passed; current Angel/FYERS public-master and Nifty 500 collector attempts did not pass. No authenticated account test has been performed. Public downloads and synthetic fixtures cannot validate credentials, subscriptions, holdings, live execution or strategy performance.

The preserved research archive and upstream checkout remain unchanged. They are excluded from the public release archive. No real trade, brokerage-account mutation, message delivery or global install was performed. Publication status and CI results are visible in the [public repository](https://github.com/kunalshinde1214/Day-Trader-Indian-Market).
