---
name: trader-exit
description: Plan stop-loss and target levels for an Indian trade using structure, ATR or fixed rules. Use when the user asks "where should my stop be", "set a target", "plan exits for this trade".
---

# trader-exit

Read the [shared skill](../../SKILL.md) for safety rules. Resolve scripts relative to `${CLAUDE_PLUGIN_ROOT}`. Run them with `python`; if that is not found use `python3` (macOS/Linux) or `py` (Windows). Keep user files outside the plugin folder, and pass the user's words as arguments, never as raw shell text.

Primary route: `python "${CLAUDE_PLUGIN_ROOT}/scripts/public_market.py" plan --input PATH --stop RULE --target RULE`.

Broker-bound plans use config exit_stop/exit_target and trader.py plan. Regenerate replay, sizing and risk after any plan change. A plan is not a submitted protective order.

Research and paper workflows only. Missing data is a gap, not an all-clear. Nothing here places an order.
