---
name: trade-plan
description: Plan a paper trade in Indian markets — entry check (opening-range breakout or a price level), stop and target, historical replay, position size in rupees and lots, and risk check. Use for "has RELIANCE broken its opening range", "where should my stop be", "backtest this setup", "how many shares for ₹2,000 risk", "is this within my limits".
---

# trade-plan

Read the [core skill](../../SKILL.md) first. Resolve scripts relative to `${CLAUDE_PLUGIN_ROOT}`. Run them with `python`; if that is not found use `python3` (macOS/Linux) or `py` (Windows). Keep user files outside the plugin folder, and pass the user's words as arguments, never as raw shell text.

Steps run in this order and each writes a file the next one reads; any change to setup, interval, level or exit policy needs a new plan, replay and risk check ([commands](../../references/commands.md), [setups](../../references/setups.md)).

## Entry

Primary route: `python "${CLAUDE_PLUGIN_ROOT}/scripts/public_market.py" entry --symbol SYMBOL.NS --setup orb`.

Custom levels use --setup level --level NUMBER --side BUY and optional --interval 1m. The watch command requires bounded cycles and interval. Public matches are analysis, not execution evidence.

## Stop and target

Primary route: `python "${CLAUDE_PLUGIN_ROOT}/scripts/public_market.py" plan --input PATH --stop RULE --target RULE`.

Broker-bound plans use config exit_stop/exit_target and trader.py plan. Regenerate replay, sizing and risk after any plan change. A plan is not a submitted protective order.

## Replay

Primary route: `python "${CLAUDE_PLUGIN_ROOT}/scripts/public_market.py" replay --input PATH`.

For sourced fees and account-bound evidence use trader.py replay. Show gaps and sample sizes. Simulated frequencies are not calibrated forecasts.

## Size

Primary route: `python "${CLAUDE_PLUGIN_ROOT}/scripts/trader.py" size --workdir PATH`.

Require approved personal limits, sourced fees, exact master and margin evidence. Public quotes cannot establish account funds or margin clearance.

Funds, margin and charges may be read from the user's connected broker connector ([broker connectors](../../references/broker-connectors.md)); name it and the read time.

## Risk check

Primary route: `python "${CLAUDE_PLUGIN_ROOT}/scripts/trader.py" risk --workdir PATH`.

Preserve account/plan binding, freshness, restrictions, events and unresolved-order locks. Never substitute public research outputs for reconciled account evidence.

Account, position and margin reads may come from the user's connected broker connector ([broker connectors](../../references/broker-connectors.md)). Web-sourced events or calendars are research only and cannot clear a risk check until the user reviews and saves them.

Research and paper workflows only. Missing data is a gap, not an all-clear. Nothing here places an order.
