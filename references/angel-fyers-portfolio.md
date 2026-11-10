# Angel One and FYERS carry-account collection

`scripts/broker_portfolio.py` provides read-only collectors for the two brokers. Both require a complete same-day master and a reviewed [opening statement](portfolio.md). They produce accounting evidence, not execution authorization. Authenticated acceptance has not been performed.

```text
python scripts/broker_portfolio.py --broker angel --fetch --account-id YOUR_ACCOUNT --opening trader/opening.json --master trader/universe.json --out reports/angel-portfolio-new.json
python scripts/broker_portfolio.py --broker fyers --fetch --account-id YOUR_ACCOUNT --opening trader/opening.json --master trader/universe.json --out reports/fyers-portfolio-new.json
```

Use the existing local [Angel credentials](angel.md) or [FYERS credentials](fyers.md). The collector checks profile identity before/after and reads holdings, positions, orders and trades twice within one 60-second session window. Book changes, malformed/nullable arrays, account mismatches and unsupported exposure block. These reads are not atomic broker snapshots. Quote collection uses the existing read-only adapters; it does not enable trading endpoints.

## Supported subsets and unresolved conventions

Angel `DELIVERY` maps to equity CNC and `CARRYFORWARD` to derivative NRML; FYERS uses CNC and MARGIN respectively. INTRADAY maps to MIS. Products and instruments must agree. Funded products, cover/bracket orders and unmapped contracts are excluded. The normal live-order path remains intraday-only.

Both collectors require reported starting holdings to agree with reviewed opening units, then apply matched day fills exactly once. Missing or reduced/available-only opening holdings block. Authorizations and reservations never become executions. FYERS holding reductions must have matching sales; current delivery inventory still uses opening units plus all day fills.

Angel's supported position convention treats buy/sell quantities as day activity and requires carry plus day activity to equal net units. Price/unit multipliers must explicitly be 1. Collateral and T1 quantities must explicitly be zero; null is unknown. **The documented `multiplier: -1` and null-collateral example is deliberately unsupported pending authoritative interpretation.** Do not edit raw values to force acceptance.

FYERS requires unit multipliers, matching exchange/segment/token, consistent carry/day/total quantities and position direction. Derivative quotes use NSE/BSE provider prefixes even when the canonical exchange is NFO/BFO. Only HLD holdings with zero T1, collateral and remaining-pledge quantities are accepted. **FYERS documentation describes remaining pledge units but also shows a positive value beside zero collateral; that combination blocks pending clarification.** Mutual funds and other holding types are not silently dropped.

These are conservative offline-tested adapters, not complete coverage of every account response. T1/pledged/MTF inventory, corporate actions, settlement adjustments, mixed-account execution and ambiguous same-time fill ordering remain unsupported. Current marks must have fresh provider last-trade times; download time cannot refresh stale quotes. Final fees require sourced contract-note reconciliation, so reports with collected trades remain PARTIAL with daily net P&L unset.

## Saved fixtures and output

Use `--input raw.json --now OFFSET_TIMESTAMP` instead of `--fetch` for reproducible saved evidence. Raw schema is `angel_portfolio_read_v1` or `fyers_portfolio_read_v1`, with broker/account/source/date/as_of/complete, `started_at`, four arrays (`holdings`, `positions`, `orders`, `trades`), a `verification` object containing the second arrays, and the adapter's normalized quote wrapper. FYERS outer API envelopes are unwrapped into those arrays during collection. Preserve real provider fields and timestamps; synthetic tests are not authenticated evidence.

The output includes opening, snapshot, fills, report and evidence hashes, with `execution_eligible: false`. An in-memory journal validates the bundle before a new file is written. Persistent journals and broker state are untouched. Use the [canonical accounting workflow](portfolio.md) to persist reviewed objects and final fees.

Source contracts checked 10 November 2026: [Angel documentation](https://smartapi.angelone.in/docs), [Angel holdings announcement](https://smartapi.angelone.in/smartapi/forum/topic/4006/new-fields-added-to-getholding-endpoint-and-introduction-of-getallholding-endpoint/4), [FYERS holdings](https://fyers.in/api-docs/v3/api/account/holdings/), [FYERS positions](https://fyers.in/api-docs/v3/api/trading/positions/get-positions/). Angel's direct documentation page denied access during this check; indexed official documentation supplied the field examples. The explicit ambiguities above remain unresolved.
