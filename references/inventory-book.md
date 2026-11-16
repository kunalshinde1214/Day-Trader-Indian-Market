# Sourced inventory adjustments

Included in 0.10.0. `scripts/inventory_book.py` reconciles reviewed equity lots in settled, T1, pledged, funded (MTF) and pending-entitlement buckets. It applies explicit non-trade events and compares the exact result with a separately supplied closing statement. It does not change the existing broker collectors' eligibility rules or enable orders against restricted inventory.

```text
python scripts/inventory_book.py --input trader/inventory-review.json --db trader/state/inventory.sqlite --out reports/inventory-review-new.json
```

For saved historical evidence only, add `--now OFFSET_TIMESTAMP`. Choose a new output path. This command has no network or trading endpoint. It verifies local source-file hashes, records an immutable event audit and rejects conflicting reused event IDs. Repeating identical evidence is idempotent. A failed statement comparison writes neither an event record nor a report. A storage failure after committing evidence can be retried using the same unchanged input and a new report path.

## Input contract

The root schema is `inventory_reconciliation_v1`, with `broker`, `account_id`, offset-bearing `as_of`, booleans `reviewed: true`, `complete: true`, `trading_activity: false`, and `opening`, `closing`, `events` and `artifacts`. Only no-trade intervals are supported; use the [portfolio journal](portfolio.md) for trade activity. Do not declare no activity when fills occurred.

Each statement contains the same `broker` and `account_id`, `as_of`, `evidence_id` and a `lots` array. The closing timestamp equals root `as_of` and must be fresh within 60 seconds of evaluation. The opening-to-closing window is at most 31 days. Artifact IDs map to `{path, sha256, source}`: a relative file inside the input directory, its SHA-256 and a provenance description. Files are limited to 20 MiB; missing files, changed hashes and escaping paths block. Preserve the original statement/circular in that directory. Hash matching proves file integrity, not authenticity or correct interpretation.

Every lot has exactly these fields:

```json
{
  "lot_id": "statement-lot-1",
  "exchange": "NSE",
  "symbol": "YOUR_EXACT_SYMBOL",
  "token": "YOUR_BROKER_TOKEN",
  "isin": "INE000A01010",
  "bucket": "T1",
  "quantity": 10,
  "cost_basis_inr": "1000",
  "principal_inr": "0"
}
```

This example is fictional. Use original account evidence. Supported exchanges are NSE/BSE cash equities. Lots require positive whole units, an Indian ISIN, and nonnegative total cost basis in INR. `principal_inr` must be positive for MTF and zero for other buckets. It represents sourced outstanding principal, not inferred interest, available margin or collateral value. Multiple lots can share an instrument; their IDs must remain distinct. Economic holdings must not be counted twice merely because collateral and holding fields overlap.

## Events

Every event has `event_id`, matching `broker`/`account_id`, `evidence_id`, `effective_at`, `lot_id`, `expected_lot_hash` and `action`, plus exactly the action's fields below. Events apply in the supplied order with nondecreasing effective timestamps. A current lot hash is `core.fingerprint(inventory_book.lot(row))`; reports expose `lot_hashes` for subsequent reviews. This binds the action to the exact pre-event state.

| Action | Additional fields | Behavior |
| --- | --- | --- |
| `transfer` | `quantity`, `destination_bucket`, `new_lot_id`, `principal_repaid_inr` | T1→SETTLED, SETTLED↔PLEDGED, MTF→SETTLED or ENTITLEMENT→SETTLED. Partial transfers allocate cost/principal proportionally and retain the remainder. Funded release requires the exact allocated principal repayment. |
| `split` | `numerator`, `denominator`, `symbol`, `token`, `isin` | Replaces quantity by the supplied ratio; preserves total cost and principal. Identity must be explicitly supplied even when unchanged. Integer ratios only; fractional entitlements block. |
| `bonus` | `numerator`, `denominator`, `new_lot_id` | Adds the sourced whole-unit entitlement as a separate zero-cost ENTITLEMENT lot. Original cost remains unchanged. A separate credit event is required to classify it as settled. MTF bonus allocation blocks. |
| `adjustment` | `reason`, `replacement_lots` | Replaces one exact lot with the explicitly sourced corrected lots. Empty replacement means an evidenced removal. Preserves the before-state and event hash in the audit; never guesses a correction from a mismatch. |

New IDs cannot reuse an earlier lot ID, including a fully consumed lot. Broker/depository evidence must establish settlement/credit, pledge/release, principal repayment, entitlement eligibility, dates and corporate-action ratios. An ex-date alone does not prove a demat credit. If a provider rounds a proportional allocation differently, use an explicit sourced correction rather than silently changing the ratio or closing statement.

## Output and limits

`RECONCILED` means normalized projected lots exactly match the supplied closing lots. The report includes lots, hashes, cost/principal totals by bucket and event audit records. It always sets `execution_eligible: false`, `submitted: false`, `sellable_quantity: null`. A settled balance alone is not delivery authorization, and pledged or funded stock is not free collateral for another account.

This is the accounting part of broader inventory support. Automatic provider-field mapping, live T1/pledged/MTF orders, trade-inclusive settlement roll-forward, funding interest, tax treatment, dividends/rights/merger formulas and authenticated account acceptance are not implemented by this tool. The existing collectors still block their documented unsupported inventory.

Sources used to define the boundaries: [Kite holdings fields](https://kite.trade/docs/connect/v3/portfolio/), [NSE optional T+0 settlement](https://www.nseindia.com/static/products-services/t0-settlement-cycle), [NSE corporate-action adjustments](https://www.nseindia.com/static/products-services/equity-derivatives-corporate-actions-adjustments). Dates and entitlements are supplied as evidence; no settlement calendar or derivative-adjustment formula is applied automatically.
