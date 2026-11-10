# Dhan carry-account collection

`scripts/dhan_portfolio.py` adds read-only accounting collection against a reviewed [opening statement](portfolio.md). It supports settled/unpledged equity holdings, intraday positions and unit-multiplier equity/index futures/options carry. The adapter has offline coverage; authenticated account acceptance remains with end users.

```text
python scripts/dhan_portfolio.py --fetch --account-id YOUR_DHAN_CLIENT_ID --opening trader/opening.json --master trader/universe.json --out reports/dhan-portfolio-new.json
```

Keep `DHAN_CLIENT_ID` and `DHAN_ACCESS_TOKEN` in the local environment. Supply a complete same-day Dhan master and sourced session-start quantities, cost basis and previous-session marks. Review adjustments and transfers explicitly. The collector does not infer them from current prices or quantity differences.

## Supported mapping

- A holding labelled `ALL` must match exactly one equity by security ID and symbol across the supplied master. Explicit NSE/BSE rows also require that exchange. Ambiguity, duplicate holdings/ISINs and missing opening inventory block.
- `INTRADAY`, `CNC` and `MARGIN` map to canonical `MIS`, equity `CNC` and derivative `NRML`. Funded, cover/bracket and unsupported products block. Linked order legs are excluded. Live execution and the default trade-import CLI remain intraday-only.
- The supported holdings convention requires `totalQty = dpQty = reviewed opening units`, with zero T1/collateral quantities. `availableQty` must be within total units but is not treated as an executed sale. Reduced/available-only holdings responses block rather than selecting an overlap formula by guesswork.
- Economic delivery inventory is opening holdings plus matched day buys minus sells. A sale of 4 from 10 opening shares leaves 6, irrespective of availability reservations. Derivative carry quantities must match the opening, while total buy/sell counts, net units, day activity and position direction must agree.
- Nonzero positions require a fresh exact-instrument Dhan quote, using the adapter's last-trade clock. Missing/old marks block. Fully closed positions need no price.

Orders, trades, holdings and positions are read twice. Account identity is checked through the existing funds endpoint before and after collection. Changing books block; moving mark prices do not. The full window must be within 60 seconds and one IST date. Separate API calls are not an atomic snapshot. Quote retrieval uses Dhan's read-only market-feed POST endpoint; no execution endpoint is enabled.

MTF/pledged/T1 inventory, foreign-currency or non-unit positions, conversions, settlement/corporate-action changes, unknown order states and ambiguous same-time execution ordering are unsupported. Do not advertise this conservative subset as complete Dhan account coverage. Provider response conventions must be checked in the user's account, including whether holdings retain their opening total and positions retain carry quantities during reductions.

## Evidence and output

For an offline reproduction, use `--input raw.json --now OFFSET_TIMESTAMP` instead of `--fetch`. The raw `dhan_portfolio_read_v1` object requires broker/account/source/date/as_of/complete, `started_at`, `orders`, `trades`, `holdings`, `positions`, `verification` containing the second four arrays, and the normalized Dhan quote wrapper (`source`, `missing`, `quotes` keyed by `SEGMENT:SECURITY_ID`). Retain actual provider fields and clocks. Fixtures are synthetic, not broker acceptance evidence.

The output bundle includes `opening`, `snapshot`, `fills`, `report`, evidence hashes and `execution_eligible: false`. It is validated in an in-memory journal before writing a new file. No persistent journal, opening evidence or broker state is changed. Today's collected fees remain provisional, so reports with trades leave daily net P&L unknown. Persist the objects and reconcile sourced final fees through the [canonical accounting workflow](portfolio.md).

Contracts checked 10 November 2026: [portfolio fields](https://dhanhq.co/docs/v2/portfolio/), [product definitions](https://dhanhq.co/docs/v2/annexure/), [orders and trade books](https://dhanhq.co/docs/v2/orders/). Offline tests establish implementation behavior, not a working authenticated account connection.
