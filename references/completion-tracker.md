# Sequential development checklist

Development status after the published 0.9.9 ZIP. Updated 15 November 2026. Local implementation and offline tests do not establish broker or production acceptance. Items are maintained in the user's requested order; partial items are not counted as complete.

| # | Area | Current status | Remaining acceptance boundary |
| --- | --- | --- | --- |
| 1 | Other-broker carry execution | Implemented for supported Angel/FYERS inventory; [guide](angel-fyers-execution.md) | End users validate their own accounts; ambiguous/restricted inventory still blocks. |
| 2 | Broader inventory | Partial: [sourced lot/event reconciliation](inventory-book.md) | Provider adapters, live T1/pledged/MTF execution, trade-inclusive settlement roll-forward and more corporate-action types. |
| 3 | Mixed-inventory entries | Partial: [long cash-equity MIS entry](mixed-inventory-entries.md) alongside supported long CNC/MIS holdings, with combined risk, margin and shared reservations | New CNC/NRML entries, short/derivative/restricted portfolios, carry-fee evidence integration and end-user acceptance. |
| 4 | Automatic exchange coverage | Partial: [automatic dated NSE surveillance and F&O ban observations](exchange-coverage.md), integrated into repeated collection; public smoke test passed | BSE, instrument-specific sessions/auctions/emergency notices, verified effective dates, complete membership and remaining indicator semantics. |
| 5 | Constituents/history | Partial: [source-backed membership intervals and split/bonus history reconstruction](historical-evidence.md), with information cutoffs, ISIN changes and artifact hashes | Automatic complete historical datasets, independent effective-date/publication verification, broader corporate actions and provider adjustment compatibility. |
| 6 | News/filings | Pending expansion | Historical backfill, attachments, issuer matching, XBRL formats and extracted-number verification. |
| 7 | Daily context | Pending expansion | Closing breadth, released FII/DII, GIFT/macro context and additional index providers. |
| 8 | Liquidity/costs | Pending expansion | Other-broker depth, queue/latency/partial-fill/impact models and quantity-sensitive replay costs. |
| 9 | Portfolio margin | Pending expansion | Other-broker baskets, hedge/collateral stress and scenarios beyond supplied fill grids. |
| 10 | Strategy/replay | Pending expansion | Other one-minute rules, pre-open/regime models and consistent special sessions. |
| 11 | Protection operations | Pending expansion | Multi-instrument/account and carry protection, deployment/runtime acceptance. |
| 12 | Compatibility/charges | Pending expansion | Ambiguous Angel/FYERS fields, master compatibility and account-specific tariffs/exemptions/rounding. |

No publisher trading account is required. Do not change an unsupported field to a convenient value, treat operator declarations as independent verification, or use a research/accounting PASS as permission to submit orders. New code remains separate from the immutable 0.9.9 release archive until explicitly versioned and packaged.
