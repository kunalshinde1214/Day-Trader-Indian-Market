# Upstox carry-account collection

`scripts/upstox_portfolio.py` collects read-only accounting evidence against a reviewed [opening statement](portfolio.md). It supports settled, unpledged cash holdings, intraday positions and unit-multiplier overnight equity/index derivatives. This is an offline-tested adapter; authenticated account acceptance remains with the end user.

```text
python scripts/upstox_portfolio.py --fetch --account-id YOUR_UPSTOX_ID --opening trader/opening.json --master trader/universe.json --out reports/upstox-portfolio-new.json
```

Use local `UPSTOX_ACCESS_TOKEN`, a complete same-day Upstox instrument master and a sourced `portfolio_opening_v1` statement. The opening must contain actual session-start inventory, acquisition costs and previous-session marks, with unresolved adjustments reviewed. Download time cannot establish those facts. No account login, order submission or persistent-journal update occurs.

The collector verifies profile identity before and after collection. It reads all orders, trades, holdings, positions and the separate MTF position endpoint twice. Changed inventory/activity blocks; changing last prices do not. Reads must complete within 60 seconds and one IST session date. This detects changes but cannot make separate endpoints an atomic snapshot. MTF endpoint failure blocks rather than implying no funded positions.

## Product and inventory rules

- Upstox `I` becomes canonical `MIS`. `D` becomes `CNC` for a mapped equity and `NRML` for a mapped future/option. Other products block. The live-order normalizer and default trade importer remain intraday-only.
- The supported holdings convention requires reported total `quantity` to equal reviewed opening units. `cnc_used_quantity` includes open-order reservations; it never becomes a fill or a second inventory deduction. Used units cannot exceed holdings or matched sales plus remaining working sell quantities. A provider response with reduced/available-only holdings blocks instead of guessing its basis.
- Current cash inventory equals verified opening holdings plus today's matched delivery buys minus sells. For 10 opening shares and a sale of 4, the result is 6. Missing opening holding rows, duplicate ISINs, T1/collateral quantities and MTF exposure block.
- Position day-buy/day-sell quantities must match actual trade-book fills. Derivative overnight units must match reviewed opening inventory and explicit overnight buy/sell quantities. Unsupported multipliers, product conversions, missing activity and contradictory quantities block.
- Nonzero inventory requires an exact-instrument quote with a fresh last-trade epoch in milliseconds, consistent with the quote clock. A recent quote update does not make an old trade fresh. Fully closed inventory requires no price.

All orders must map to supported states and actual filled units. Same-timestamp fills for one instrument/product cannot be sorted by arbitrary trade IDs; they require the separately sourced canonical sequencing workflow. The collector blocks ambiguous sequencing. Fee totals are unknown until contract-note reconciliation, so collected trades yield a `PARTIAL` accounting report with daily net P&L unset.

## Saved evidence and output

For reproducible offline checks, replace `--fetch` with `--input raw.json --now OFFSET_TIMESTAMP`. Raw `upstox_portfolio_read_v1` evidence requires broker/account/source/date/as_of/complete, `started_at`, five arrays (`orders`, `trades`, `holdings`, `positions`, `mtf_positions`), `verification` with those five second-read arrays, and the normalized Upstox quote wrapper (`source`, `missing`, `quotes` keyed by instrument token). Preserve actual collection/source clocks and fields; never label synthetic fixtures as account evidence.

The new output file contains `opening`, `snapshot`, `fills`, `report` and source hashes, with `execution_eligible: false`. An in-memory journal validates the entire bundle before writing. Use the [canonical journal workflow](portfolio.md) to persist the separate objects and final fees. The collector does not change opening evidence or enable mixed-account execution. T1/pledged/MTF inventory, settlement and corporate-action adjustments remain unsupported.

Source contracts checked 10 November 2026: [holdings and reserved units](https://upstox.com/developer/api-documentation/get-holdings/), [positions and overnight quantities](https://upstox.com/developer/api-documentation/get-positions/), [separate MTF positions](https://upstox.com/developer/api-documentation/get-mtf-positions/), [order book](https://upstox.com/developer/api-documentation/get-order-book/), [quote clocks](https://upstox.com/developer/api-documentation/get-full-market-quote/). The conservative supported holdings convention still requires confirmation against each user's actual account responses.
