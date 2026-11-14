---
name: trader-timemachine
description: Replay a trade idea against past price history to see how similar entries played out. Use when the user asks "backtest this setup", "how did this work historically", "replay this trade".
---

# trader-timemachine

Read the [shared skill](../../SKILL.md) for safety rules. Resolve scripts relative to `${CLAUDE_PLUGIN_ROOT}`. Run them with `python`; if that is not found use `python3` (macOS/Linux) or `py` (Windows). Keep user files outside the plugin folder, and pass the user's words as arguments, never as raw shell text.

Primary route: `python "${CLAUDE_PLUGIN_ROOT}/scripts/public_market.py" replay --input PATH`.

For sourced fees and account-bound evidence use trader.py replay. Show gaps and sample sizes. Simulated frequencies are not calibrated forecasts.

Research and paper workflows only. Missing data is a gap, not an all-clear. Nothing here places an order.
