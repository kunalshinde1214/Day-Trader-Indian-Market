---
name: broker
description: Connect the user's own broker (Kite, Upstox, Dhan, Angel One, FYERS or a broker connector) to read holdings, positions, funds and quotes, prepare paper tickets, and run bounded price alerts. Live order tools are a separate opt-in. Use for "connect my broker", "prepare the order ticket", "alert me if price hits X".
---

# broker

Read the [core skill](../../SKILL.md) first. Resolve scripts relative to `${CLAUDE_PLUGIN_ROOT}`. Run them with `python`; if that is not found use `python3` (macOS/Linux) or `py` (Windows). Keep user files outside the plugin folder, and pass the user's words as arguments, never as raw shell text.

Read data and prepare paper tickets only. The live-order and protection tools are an optional module, disabled by default, documented in [advanced tools](../../references/advanced.md); never enable, arm or run them unless the user explicitly asks for that specific action.

## Tickets

Primary route: `python "${CLAUDE_PLUGIN_ROOT}/scripts/trader.py" ticket --workdir PATH`.

Read references/execution.md before preparing live.py actions. Both exact-action confirmations must be operated by the human. Never type them, automate stdin, retry ambiguous orders or treat this skill invocation as trade authorization.

If the user has connected their broker's own connector, use it to read account, positions, funds and quotes for the ticket. Do not place, modify or cancel orders through it on your own; follow [broker connectors](../../references/broker-connectors.md).

## Alerts

Primary route: `python "${CLAUDE_PLUGIN_ROOT}/scripts/service.py" run --workdir PATH`.

Read references/account-service.md for required bounds. Public entry watching uses public_market.py watch. A one-shot check does not continue monitoring. Send webhooks only when explicitly requested by the user.

Research and paper workflows only. Missing data is a gap, not an all-clear. Nothing here places an order.
