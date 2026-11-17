# 0.10.1 scope — 17 November 2026

These changes are released in 0.10.1-carry-protection-alpha. No real orders, user logins, services or unattended runners were activated during development. Offline results do not establish production acceptance.

| Requested area | Added in this development pass | Still incomplete |
| --- | --- | --- |
| Automatic data coverage | Resumable multi-window NSE announcement history, durable filing backlog integration, explicit failed-date cursor preservation | BSE history/calendar feeds, instrument-specific emergency/special-session notices, complete membership histories and independent filing verification |
| Inventory and mixed entries | Sourced buy/sell events reconciled one-for-one against a complete trade ledger; settlement/corporate-action events can follow those trades. Long or explicitly enabled short cash MIS entries alongside long CNC and signed MIS holdings, without netting gross exposure | Live T1/pledged/MTF execution, new carry/derivative entries, broader provider settlement adapters and corporate actions |
| Execution and margin models | Dhan multi-order basket estimates; account-separated collateral/cash-component stress; explicit passive-limit queue/latency/partial-fill scenarios with fees on filled units | Measured queue position, calibrated impact/latency, independently verified derivative-depth units and exhaustive margin stresses |
| Protection | Carry-aware single-policy runner using reconciled inventory and delivery evidence; sequential multi-instrument/multi-broker group coordination with shared same-account execution journals and group failure revocation | Production/runtime acceptance; multiple accounts at the same broker in one process; automatic renewal of delivery evidence; guaranteed fills or protection |
| User acceptance | Optional carry reconciliation, basket-contract and cash-depth probes on each user's own account | Actual user-account results, live order acceptance and production deployment checks |

## Resumable announcement history

```bash
python scripts/history_service.py --directory history --from-date 2026-11-01 --through-date 2026-11-16 --windows 2 --window-days 3 --filing-limit 5
```

Dates are explicit, nonfuture calendar dates. Each call processes at most ten windows of at most seven days. Repeat the same command to resume the same requested range. Successful windows have immutable reports and a checkpoint; a failed/rejected date or failed archive write does not advance the cursor. A `stop.json` in that directory stops before the next window. Filings remain in `filings.sqlite`; use `scripts/filing_archive.py` with a saved report to drain remaining attachments. A completed requested window means the reported date queries succeeded, not that pagination, revisions, BSE filings or all exchange history are complete.

## Trade-inclusive inventory accounting

The [inventory guide](inventory-book.md) describes the original sourced lot/event format. `inventory_reconciliation_v2` also requires a `trade_ledger` with exact `broker`, `account_id`, `complete: true`, an existing artifact `evidence_id`, and `fills`. Each fill contains only `fill_id`, `exchange`, `symbol`, `token`, `side`, `quantity`, `price`, and timezone-aware `at`. `trading_activity` must agree with whether that ledger contains fills.

Every observed fill must match exactly one `buy` or `sell` event in effective-time order. This bounded version does not allocate one fill across multiple lots. Common event identity/provenance fields still apply. For a buy, `lot_id` is new, `expected_lot_hash` is null, and additional fields are `fill_id`, `new_lot`, `price`, `capitalized_charges_inr`. The new lot must be T1 or MTF; its cost equals filled quantity times price plus the sourced capitalized charges. For a sell, additional fields are `fill_id`, `quantity`, `price`, `charges_inr`, `principal_repaid_inr`. Sale quantity cannot exceed the identified lot and funded principal is allocated proportionally. The projected closing book must equal the sourced closing statement.

Accounting for an observed restricted-lot sale does not permit the tool to submit that sale. Reports retain `execution_eligible: false` and unknown sellability. Evidence hashes protect local integrity; they do not independently authenticate a statement or make a supplied trade ledger complete.

## Risk and acceptance

See [basket margin providers](basket-margin-providers.md) for Dhan's documented-contract ambiguity, [scenario formats](risk-scenarios.md) for the new risk tools, and [portfolio protection](portfolio-protection.md) for operating boundaries.

The existing read-only acceptance command now accepts optional paired `--portfolio-opening opening.json --master master.json`, `--basket basket.json`, and `--depth`. These flags require `--authenticated-readonly`; they cannot be attached to a public-master probe. Optional inputs must belong to the expected account. Reports contain scoped checks and hashes, not raw holdings or credentials. `execution_acceptance` and `production_acceptance` remain `NOT_TESTED`, and no live profile is modified. Example:

```bash
python scripts/acceptance.py --broker dhan --authenticated-readonly --instrument instrument.json --account-id YOUR_ACCOUNT_ID --basket basket.json --out dhan-readonly-new.json
```

Use a new output path and your own configured local login. A successful read or margin response is not evidence that an order, delivery authorisation or unattended deployment works.
