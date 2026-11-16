# Angel One and FYERS reducing carry orders

Included in 0.10.0, checked 15 November 2026. `scripts/portfolio_exit.py` supports reducing exits, stop-limits and cancellation for the supported [Angel/FYERS collector subsets](angel-fyers-portfolio.md). New carry entries, T1/pledged/MTF orders and unattended carry protection remain unsupported. Live acceptance is user-owned.

CNC maps to Angel `DELIVERY` / FYERS `CNC`; NRML maps to Angel `CARRYFORWARD` / FYERS `MARGIN`. MIS remains `INTRADAY`. Angel stops use `STOPLOSS` variety and `STOPLOSS_LIMIT`; FYERS uses type 4. Cancellation retains the observed order ID and Angel variety. Recovery distinguishes product, instrument, side, quantity, prices and Angel variety.

Follow the [mixed-inventory setup](mixed-inventory-exits.md), selecting `angel` or `fyers`. Each review recollects the complete account using `broker_portfolio.fetch`; after human approval the workflow checks a second unchanged review. Unknown submissions stay locked pending reconciliation.

```text
python scripts/portfolio_exit.py preview --workdir trader --opening trader/opening.json --master trader/universe.json --product CNC --quantity 1 --price 100 --delivery-authorization trader/delivery.json --session trader/instrument-session.json
```

Numbers above are placeholders. Both brokers require fresh account/instrument-specific continuous-session evidence for placements; use the session schema in [Upstox execution](upstox-execution.md). CNC additionally requires fresh delivery authorization covering the requested units. Stop previews add `--kind stop --trigger PRICE` and fetch a current broker quote. The `exit` and `stop` commands require two terminal confirmations. Cancel with `cancel --order-id ID` and the exact product; cancellation does not require placement-session or delivery evidence.

Working orders in the selected product block replacements. Over-reduction, wrong lots, tick/freeze violations, changed accounts, missing authorization, reached triggers and mismatched recovery records block. Stop-limits are DAY orders and may not fill. No real order was submitted during development.

Sources checked: [Angel order constants](https://smartapi.angelone.in/docs), [FYERS placement](https://fyers.in/api-docs/v3/api/trading/order-placement/sync/single-order/), [FYERS cancellation](https://fyers.in/api-docs/v3/api/trading/order-management/sync/cancel-single-order/). Angel's direct documentation request was denied; its indexed official documentation supplied the constants. Inventory-field ambiguities remain blocked as documented by the collector.
