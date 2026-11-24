# Completion status

Updated 24 November 2026 for version 1.5.1.

**Implemented:** Indian-market research, daily reports, market-move explanations, single-stock analysis with peers and valuation history, shareholding, a [stock screener](screener.md), a [portfolio review with capital-gains estimates](holdings-review.md), F&O analytics ([guide](fno.md)), news and filings, pattern scans, and paper trade planning with Indian costs and risk checks. "Implemented" means code and offline tests exist; see [release status](release-status.md) for how it was checked.

**Not claimed:**

- *Comprehensive exchange coverage.* Scripts read specific NSE public files and APIs. Anything else (BSE notices and filings, special sessions, emergency circulars, concalls, macro events) is looked up by [web research](web-research.md) when a user asks. That is a cited search at request time, not an automatic feed, and it can miss things.
- *Broker or production acceptance.* Broker adapters and execution paths pass tests against recorded and simulated responses only. Each user connects and validates their own account through a [broker connector](broker-connectors.md) or the built-in adapters. No publisher trading account is needed, and none was used.

## How each original workstream is covered

| # | Area | Implemented in scripts | Looked up by web research when asked (not automatic) | Left to each user |
| --- | --- | --- | --- | --- |
| 1 | Other-broker carry execution | Angel One/FYERS reducing exits, stop-limits and cancellation ([guide](angel-fyers-execution.md)) | — | Validate their own account before enabling live actions |
| 2 | Broader inventory | Settled, T1, pledged, MTF and corporate-action lot reconciliation, including trades ([inventory book](inventory-book.md)) | — | Supply their own statements; unsupported inventory stays blocked |
| 3 | Mixed-inventory entries | MIS cash entries (opt-in shorts) alongside long CNC and signed MIS ([guide](mixed-inventory-entries.md)) | — | Opt in explicitly; confirm each order |
| 4 | Exchange coverage | Dated NSE surveillance and F&O ban collection ([guide](exchange-coverage.md)) | BSE notices, special sessions, emergency circulars | Review web facts before saving them as evidence |
| 5 | Constituents and history | Sourced membership intervals and split/bonus reconstruction ([guide](historical-evidence.md)) | Current index changes and corporate actions | — |
| 6 | News and filings | NSE feeds, backfill, attachments and inline XBRL ([guide](announcement-history.md)); Google News per stock | BSE filings, results commentary, concalls, breaking news | — |
| 7 | Daily context | NSE closes, breadth, FII/DII and GIFT observations ([guide](closing-context.md)) | BSE closes, macro and RBI/SEBI events, global cues | — |
| 8 | Liquidity and costs | Five-broker cash depth, quantity-dependent costs, passive-fill scenarios ([depth](depth-and-replay-costs.md), [scenarios](risk-scenarios.md)) | — | Supply their own fee card and assumptions |
| 9 | Portfolio margin | Five-broker basket margins and collateral stress ([guide](basket-margin-providers.md)) | — | Read margins from their own account |
| 10 | Strategy and replay | Six scanners and custom levels at one and five minutes, interval-bound replay ([setups](setups.md)) | — | — |
| 11 | Protection | Intraday and carry-aware bounded runners with group coordination ([guide](portfolio-protection.md)) | — | Arm it themselves; no fill is guaranteed |
| 12 | Compatibility and charges | Read-only probes for carry, basket and depth contracts ([progress](development-progress.md)) | — | Run the probes on their own account; use their broker's tariff |

## Optional future enhancements

These would extend the skill. The research and paper workflows work without them:

- calibrated market-impact, queue and latency models fitted to real fills;
- pre-open and market-regime models for replay;
- new carry (CNC/NRML) and derivative entries alongside existing inventory;
- running more than one account per broker in a single protection process.

## Boundaries that stay in place

Web facts are cited research and never satisfy a risk check or authorize an order. A connector is used to read account data; orders stay the user's action ([broker connectors](broker-connectors.md)). Do not replace an unsupported field with a convenient value, treat an operator declaration as independent verification, or use a research or accounting PASS as permission to submit orders.
