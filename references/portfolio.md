# Carry-position accounting

`scripts/portfolio.py` reconciles sourced opening inventory, journal fills, all current orders and current inventory. It supports canonical `CNC` equity delivery, `NRML` futures/options and `MIS` intraday positions. These labels describe the normalized contract; other brokers' native product codes must be explicitly mapped by the evidence provider.

This module accounts from supplied evidence and makes no broker calls, order submissions, automatic product conversions or position liquidation. A portfolio report is deliberately **not** an account risk ledger and cannot enable the intraday live tool. The five-broker raw trade-import CLI continues to accept intraday accounts only. Separate limited [Kite](kite-portfolio.md) and [Upstox](upstox-portfolio.md) collectors supply carry evidence. Dhan/Angel/FYERS carry collection and authenticated acceptance remain outstanding.

## Workflow

For supported Kite accounts, [the read-only collector](kite-portfolio.md) produces current canonical positions, matched fills and a report from broker books plus reviewed opening evidence. Other brokers and unsupported inventory still require canonical inputs.

Use the existing journal database and a complete same-day instrument master. Keep broker/account identity consistent across every input.

```text
python scripts/portfolio.py opening --db trader/state/journal.sqlite --input trader/opening.json --master trader/universe.json
python scripts/journal.py import --portfolio --db trader/state/journal.sqlite --input trader/portfolio-fills.json
python scripts/portfolio.py reconcile --db trader/state/journal.sqlite --input trader/portfolio-snapshot.json --master trader/universe.json --out reports/portfolio-new.json
```

The opening import is idempotent and survives restarts. Conflicting opening evidence is rejected without overwriting the saved record. Corrections use the explicit audited command below; do not delete the opening to make a mismatch disappear. Each new session requires its own sourced opening statement, including an explicit empty array when genuinely flat. Do not manufacture an opening from the previous report's calculated quantity.

Fill imports default to intraday-only. `--portfolio` explicitly permits canonical delivery and overnight-derivative fills. It does not alter live-order support. Identical fills remain idempotent, conflicting executions block, and contract-note corrections use the existing audited `journal.py fees` workflow. Sharing a journal containing carry fills intentionally blocks the intraday risk-ledger path; mixed-account execution is not enabled by this feature.

Use `--now` only to reproduce a historical, dated accounting check. Report output paths must be new. `RECONCILED` means supplied inventory/order evidence matches and today's fill fees are final. `PARTIAL` means inventory matches but provisional fees leave daily net P&L unknown. `BLOCKED` means evidence failed; quantity/order discrepancies suppress P&L. None of these states is trade authorization.

## Audited opening corrections

When an authoritative source corrects opening inventory or its price basis, prepare a correction object with `schema: "portfolio_opening_correction_v1"`, broker/account/date/source/as_of/complete, a nonempty `reason`, `expected_opening_hash` from the current saved statement/report, and `replacement` containing a complete valid opening object. Correction evidence must be at most 60 seconds old; replacement evidence cannot predate the current saved opening or postdate the correction. This is an accounting correction, not an inventory adjustment or broker action.

```text
python scripts/portfolio.py correct-opening --db trader/state/journal.sqlite --input trader/opening-correction.json --master trader/universe.json
```

The command compares the expected hash under a database write lock and saves the old statement, replacement, source/reason and recording time in `portfolio_opening_audit` atomically. A stale correction cannot overwrite newer evidence. Reimporting the exact correction is idempotent. Existing reports retain their original opening hash and must be regenerated; fills and contract-note records are not rewritten. Actual corporate actions/conversions/settlements still need an authoritative normalization workflow and cannot be hidden as unexplained opening corrections.

## Opening contract

The object requires:

- `schema: "portfolio_opening_v1"`, `broker`, `account_id`, `source`, `date`, offset-bearing `as_of`, and `complete: true`.
- `previous_session`: a sourced trading-session date strictly before the report date, not a calendar-day subtraction.
- `adjustments_reviewed: true` and `inventory_events: []`: evidence establishes that there are no unresolved corporate actions, transfers, physical settlements or product conversions affecting this reconciliation. These fields are not defaults to tick without review.
- `positions`: every nonzero opening position, keyed by string `exchange`, `symbol`, `token`, `product`.

Each opening position needs signed whole-unit `quantity`, positive `average_price`, positive `previous_close`, `price_basis: "per_unit_inr"`, `cost_source`, `mark_source`, and `mark_session` equal to `previous_session`. Average price is the provider's pooled price basis, excluding historical fees; for derivatives, provide the applicable sourced basis rather than guessing a settlement reset. Short delivery inventory and nonzero opening MIS exposure block. Derivative quantities must be actual units matching the master lot size, not counts of lots.

The statement's `as_of` is the time the opening evidence was sourced/reviewed in the current session, not a fictional trade timestamp. Its quantities and prior mark refer to the start of this session and verified preceding session respectively. The statement must be no more than 24 hours old and belong to the report date.

## Snapshot and fill contract

The current snapshot requires `schema: "portfolio_snapshot_v1"`, the same broker/account/date, source, offset-bearing `as_of`, `complete: true`, `inventory_events: []`, `orders` and `positions`. Snapshot freshness is 60 seconds.

Each order needs `order_id`, the four instrument/product identifiers, `side` (`BUY`/`SELL`), `quantity`, `filled_quantity`, and normalized `status` (`OPEN`, `PARTIAL`, `FILLED`, `CANCELLED`, `REJECTED`, `UNKNOWN`). Include all today's orders, including manual and terminal orders. Unknown status prevents a reconciled report; contradictory quantities/statuses block. Working quantities never become inventory without fills.

Each current position needs the identifiers and signed `quantity`. A nonzero position also requires positive `mark_price`, `mark_source`, `mark_as_of` no older than 60 seconds and no later than the snapshot, and `price_basis: "per_unit_inr"`. Zero positions can be omitted. Holdings, unsettled inventory and positions must already be normalized into **one economic quantity per instrument/product**; do not add raw endpoint quantities together without verifying overlap.

Fill objects follow [the journal contract](operations.md), with `product` additionally allowing `CNC`/`NRML` in explicit portfolio import mode. All fills must match the current order's exact identity, product and side, and sum to its filled quantity. If two fills for the same instrument/product have the same timestamp, both require a unique nonnegative integer `sequence` and nonempty `sequence_source` establishing their true order. An alphabetical trade ID is not execution ordering evidence. Final fee records require explicit components, including `{"total": 0}` when a sourced zero is correct; `{}` cannot establish final zero fees.

## Calculations and limits

- Expected current units = opening units + today's signed fills, separately for every exchange/symbol/token/product. MIS and CNC quantities never cancel one another.
- Acquisitions in the same direction update a pooled average price. Reductions realize the price difference on closed units. A derivative reversal closes the old side and starts the new side at the reversal fill price. Delivery selling beyond evidenced inventory blocks.
- Daily gross change = today's signed sale proceeds minus purchase costs + current marked inventory value − opening inventory at its previous-session mark.
- Acquisition-basis realized and unrealized gross amounts are shown separately. They differ from daily P&L when inventory was carried overnight. Historical acquisition fees are excluded.
- Daily net change deducts today's finalized fill fees only. Provisional fees remain explicitly incomplete; the intraday fee estimator is not reused for delivery or carry products.

The report retains opening/snapshot/fill hashes and provenance. Hashes detect changed evidence; they do not prove provider authenticity. Reports do not compute tax lots, cash settlement, delivery authorization, portfolio margin, corporate-action adjustments, expiry settlement or trade protection. Expired/unmapped contracts block rather than being guessed from today's master. Inventory events require authoritative reconciliation outside this supported no-adjustment path.

The separation of holdings and positions follows the concepts documented in [Kite's portfolio API](https://kite.trade/docs/connect/v3/portfolio/). Its native product vocabulary is documented in [Kite orders](https://kite.trade/docs/connect/v3/orders/). Those pages are reference material, not evidence that a connected carry account or another broker's mapping has been accepted.
