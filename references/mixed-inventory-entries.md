# Entries alongside supported carry inventory

Development after 0.9.9. `scripts/mixed_entry.py` provides a separate, disabled-by-default human-operated path for **BUY MIS cash-equity entries alongside long CNC/MIS cash-equity exposure**. It uses all five broker collectors and existing single-order margin/transport adapters. It does not open new CNC/NRML positions or support short/derivative/MTF/pledged/T1 portfolios. These limits are enforced in code.

The regular `live.py entry` workflow still requires a flat account. Do not edit a snapshot to appear flat or hide positions to use it.

## Setup and preview

Prepare the ordinary selected instrument, dated master, plan, replay, sized order, charges, restrictions, session and event evidence in the work directory. Supply reviewed opening carry inventory and a complete broker master. Read [user setup](user-setup.md), [execution controls](execution.md), [carry accounting](portfolio.md) and the selected broker's collector limits first.

The generated profile keeps `mixed_inventory_entries: false`, `mixed_entry_acceptance.status: "NOT_TESTED"` and `max_portfolio_gross_inr: 0`. The end user must choose a positive whole-portfolio gross-exposure cap and explicitly opt in after their own account-specific acceptance. Ordinary `enabled`, `new_entries`, `acceptance`, `max_entry_notional_inr`, `max_entry_loss_inr` and two-stage human approval requirements also apply. The new acceptance object has the same broker/account/status/source/as_of fields as ordinary acceptance; it must be current within 24 hours. No default cap or completed acceptance is inferred.

```text
python scripts/mixed_entry.py preview --workdir trader --opening trader/opening.json --master trader/universe.json
```

The preview performs read-only account, quote and margin requests. It gathers complete inventory before and after funds/margin collection and refuses changed order/position state. Exact instrument-session evidence remains mandatory. Local halt, stale evidence, restrictions, replay, cost, quantity, tick/freeze and ordinary budget limits remain active.

## Combined risk and reservations

- The carry journal recomputes opening, fills, orders and positions. The separate intraday journal reconciles every MIS fill and its planned-risk reservation. Neither report substitutes for the other.
- Intraday reservations come from the shared execution database, or the user's explicit `reservations.json` mapping. Its existing schema is `{broker, account_id, date, entries}`, with each entry binding `entry_order_id`, `exit_order_ids` and `planned_loss_inr`. Unmapped manual orders block. Carry actions are omitted from the intraday reservation partition only after the full carry portfolio has independently reconciled.
- Current position values and the unfilled portions of working MIS BUY orders count toward gross exposure and exchange/symbol position caps. Ten CNC shares and two MIS shares are not netted. A pending carry sale does not free exposure before a fill. Working CNC buys block; working carry sells cannot exceed the remaining delivery position.
- Intraday planned/realized loss reservations persist through the day. Negative daily carry P&L and final carry-fill charges additionally consume the daily risk budget; positive carry P&L does not replenish it. Current marks must be fresh within 30 seconds.
- Provisional MIS charges require the existing explicit [fee-reserve policy](account-service.md). Provisional carry-fill charges block further entries. Automatic collectors do not produce final contract-note fees, so the CLI currently blocks after such same-day carry executions; end-user fee evidence integration remains outstanding.
- The exact new payload needs a fresh account-bound broker margin estimate. The new order also must fit the per-entry caps and the whole-portfolio gross cap. No collateral, cross-account netting or basket hedge benefit is inferred.

Gross exposure is a price-based cap, not a worst-case-loss guarantee. Risk remains sensitive to gaps, costs and external account changes. This first implementation excludes short and derivative inventory rather than assigning it an invented finite loss bound.

## Human-operated execution and recovery

Changing `preview` to `entry` prompts the operator for two exact confirmations. The workflow repeats collection, margin and risk checks after approval. Changed economic evidence invalidates the review. The shared `state/execution.sqlite` records SUBMITTING before transport, serializes competing local actions, and retains ambiguous requests across restarts. There is no automatic resubmission or protective-order installation.

Use one work directory/execution database for each broker account. Different directories or external broker activity cannot be coordinated by this local database. Use `portfolio_exit.py status` / `bind` for mixed-account order reconciliation and its reducing exit/stop/cancel commands as appropriate. Do not invoke the regular flat-account reconciliation path on carry inventory.

Offline tests include each broker's product/funds contract, mixed cash exposure, partial-fill reservations, unmapped orders, carry losses and provisional charges, gross/margin limits, freshness/identity errors, changed review inputs and a simulated CLI journey. No authenticated order or service was activated. The [ordered checklist](completion-tracker.md) retains broader mixed-entry variants as unfinished.
