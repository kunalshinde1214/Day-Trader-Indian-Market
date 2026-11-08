# Kite carry-account collection

`scripts/portfolio_collect.py` reads Kite holdings, net/day positions, all today's orders/trades and fresh quotes. It produces an accounting bundle and a reconciled gross-P&L report against an explicitly reviewed [opening statement](portfolio.md). It does not create opening cost evidence, update a broker, submit orders or enable mixed-account execution. Authenticated broker acceptance is still pending.

## Run

Use a same-day Kite master containing every account instrument and a reviewed current-session `portfolio_opening_v1` statement. Opening quantities refer to the session start; costs and previous-session marks must come from authoritative evidence. Review unresolved inventory changes through the collection time; the collector cannot discover every corporate action or transfer.

```text
python scripts/portfolio_collect.py --fetch --account-id YOUR_KITE_ID --opening trader/opening.json --master trader/universe.json --out reports/kite-portfolio-new.json
```

Credentials stay in local `KITE_API_KEY` and `KITE_ACCESS_TOKEN` environment variables. `--fetch` uses the actual clock and checks the profile identity before and after collection. Only GET calls are made. Orders, trades, holdings and positions are read twice; changed order/trade records or inventory quantities block. The entire collection must be within 60 seconds, on one IST date. This detects many concurrent changes but does not make the separate broker APIs an atomic snapshot. Refresh after a block; there is no automatic retry.

For a saved raw fixture, use `--input raw.json --now OFFSET_TIMESTAMP` instead of `--fetch`. The raw `kite_portfolio_read_v1` object requires broker/account/source/date/as_of/complete, `started_at`, `orders`, `trades`, `holdings`, `positions` with explicit `net` and `day` arrays, `quotes` keyed by exchange:symbol, and `verification` containing the second orders/trades/holdings/positions read. Preserve the actual response fields and collection times; do not relabel synthetic evidence as a broker read.

## Supported accounting and overlap checks

- Settled, unpledged CNC delivery holdings on the mapped exchange; MIS intraday positions; NRML futures/options with unit multiplier 1. Products stay separate.
- A holding must have zero T1, short and collateral quantities, no price discrepancy, and explicit zero MTF quantity/used quantity. `quantity` must equal `realised_quantity`; `quantity + used_quantity` must equal both `opening_quantity` and the reviewed opening units. Unsupported field combinations block instead of guessing settlement semantics.
- Today's CNC position quantity must equal its matched buy units minus sell units, with zero overnight quantity. Economic CNC inventory equals verified opening holdings plus that net change. For 10 opening shares and a sale of 4, holdings quantity 6/used quantity 4 and a CNC position of -4 produce **6 shares**, not 2.
- NRML net quantity must equal reviewed overnight units plus today's matched buy/sell activity. The day array must independently match today's fills. Position multiplier, lot size, exact product/instrument and all order filled quantities are checked.
- A nonzero position needs a matching token and a same-session quote whose last trade is no more than 60 seconds old. Quote download time does not refresh an old price. Fully closed positions need no price.

The collector blocks T1 holdings, pledged or discrepant inventory, MTF/CO/BO products, unsupported order varieties, ambiguous order states, duplicate holdings/ISINs, missing opening holdings, conversions, quantity mismatches, non-unit multipliers and unmapped/expired instruments. Holdings that disappear after a full sale require separate canonical evidence; absence is not silently treated as a valid zero. Cross-exchange holding identity changes are not resolved automatically. Broker books without required explicit fields also block.

No execution sequence is inferred from a trade ID or response order. Multiple fills for the same instrument/product at the same timestamp require sourced sequencing through the canonical accounting workflow; this collector blocks them. Previous-session marks, settlement resets, historic fees and opening acquisition prices are never inferred from the current positions average.

## Bundle and journal use

The new output file contains `opening`, `snapshot`, `fills`, `report`, source/opening hashes, `execution_eligible: false` and `submitted: false`. The report is validated in an isolated in-memory journal before the file is written. The collector does not change a persistent journal. Hashes identify the supplied evidence; they do not establish authenticity.

Today's collected fills have unknown fees, so a report with trades is `PARTIAL` and daily net P&L remains unknown. An account with no trades may reconcile without today's fees. Never apply the intraday fee reserve to CNC/NRML accounting.

For durable accounting, save the bundle's `opening`, `fills` and `snapshot` objects as separate JSON files and use the three commands in [portfolio workflow](portfolio.md). Preserve existing opening evidence; conflicting records require its audited correction workflow. Import final contract-note fees separately and regenerate the report. A carry journal must never be substituted for the intraday execution ledger.

## Source contracts

Field mappings were checked on 8 November 2026 against [Kite portfolio documentation](https://kite.trade/docs/connect/v3/portfolio/), [the holdings-sale overlap explanation](https://kite.trade/forum/discussion/comment/52974/) and [quote timestamps](https://kite.trade/docs/connect/v3/market-quotes/). The adapter deliberately supports a narrower settled-inventory subset and makes no assumption about today's settlement timetable. Offline fixtures verify the mappings; actual account response acceptance remains outstanding.
