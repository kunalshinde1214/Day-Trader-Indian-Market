# Explicit execution and portfolio scenarios

Added in 0.10.1, 17 November 2026. These tools evaluate supplied scenarios. They neither estimate a calibrated probability nor authorize execution.

## Passive cash-equity fills

```bash
python scripts/fill_scenarios.py --input fill-scenarios.json --out fill-report.json
```

Input schema `passive_fill_scenarios_v1` requires a current `instrument`, `side` (`BUY`/`SELL`), on-tick `limit_price` and `hypothetical_exit_price`, whole-unit `quantity` within the freeze limit, the existing `charges` rate card, and 1–40 `scenarios`. Cash equity with one-unit lots only is supported. An explicit `--now` may replay saved evidence.

Each scenario has exactly `name`, `ahead_units_at_arrival`, `latency_ms`, `cancel_after_ms`, and `eligible_flow`. Flow contains up to 10,000 strictly ordered objects with `elapsed_ms` and `units`. Supply only assumed contra-side volume eligible to execute against that passive limit; ordinary candle volume cannot establish it. Time is measured from the order decision. Queue size refers to the assumed queue when the order arrives.

```json
{"name":"slower arrival","ahead_units_at_arrival":100,"latency_ms":200,"cancel_after_ms":30000,"eligible_flow":[{"elapsed_ms":100,"units":500},{"elapsed_ms":500,"units":120},{"elapsed_ms":1000,"units":30}]}
```

Pre-arrival and post-cancellation volume is excluded. Eligible volume first consumes the queue ahead, then produces bounded partial fills. Filled units belong to one order; fees are recomputed on those units and one hypothetical exit, preserving brokerage caps. Unfilled units have no trade fees. The model does not assume queue cancellations, hidden liquidity, price improvement, actual cancel acknowledgement or a fill probability. It remains separate from replay target/stop frequencies.

## Portfolio collateral and cash-component stress

```bash
python scripts/portfolio_stress.py --input portfolio-stress.json --out stress-report.json
```

Input schema `portfolio_stress_v1` contains 1–20 `accounts` and 1–40 `scenarios`. Each account has a unique local `id`, exact `broker`/`account_id`, evidence `source` and fresh `as_of`, `cash_balance_inr`, `reserved_cash_inr`, `account_margin_required_inr`, `cash_component_required_inr`, `positions`, and `collateral`. Cash means gross balance; do not substitute available margin or add balances that already include collateral. The cash component must come from current account evidence, not a hard-coded regulatory percentage.

Every asset has a unique `id` within its collection, exact Indian `exchange` and `token`, nonzero unit `quantity`, positive `mark_price`, and `price_basis: per_unit_inr`. Positions may have signed quantities; collateral must have positive quantities and a `haircut` fraction from 0 to 1. Include pledged holdings in positions when measuring their economic P&L, and in collateral only for funding value; the collateral calculation never adds a second P&L.

Each scenario has a unique `name` and an `accounts` object keyed by every account ID. Each account scenario contains exactly `position_prices`, `collateral_prices`, `collateral_haircuts`, `margin_multiplier`, `cash_component_multiplier`, and `cash_debit_inr`. Every asset needs an explicit price; no automatic option repricing or omitted-asset default exists. Multipliers are positive assumptions; debits are nonnegative. Output shows mark-to-market loss/gain, stressed cash, collateral after haircut, total-margin and cash-component shortfalls, and additional cash needed. Positive unrealised P&L does not replenish available cash. Another account's surplus never pays a deficit.

User scenarios, supplied account accounting and current tariffs must be independently reviewed. These calculations are not a SPAN replica, collateral eligibility engine, future broker quote, tax calculation or maximum-loss guarantee.
