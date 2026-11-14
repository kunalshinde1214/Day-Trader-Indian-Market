---
name: trader-budget
description: Size an Indian paper trade in rupees and lots from capital, risk limits and fees. Use when the user asks "how many shares should I buy", "position size for ₹X risk", "lot size and cost for this trade".
---

# trader-budget

Read the [shared skill](../../SKILL.md) for safety rules. Resolve scripts relative to `${CLAUDE_PLUGIN_ROOT}`. Run them with `python`; if that is not found use `python3` (macOS/Linux) or `py` (Windows). Keep user files outside the plugin folder, and pass the user's words as arguments, never as raw shell text.

Primary route: `python "${CLAUDE_PLUGIN_ROOT}/scripts/trader.py" size --workdir PATH`.

Require approved personal limits, sourced fees, exact master and margin evidence. Public quotes cannot establish account funds or margin clearance.

Research and paper workflows only. Missing data is a gap, not an all-clear. Nothing here places an order.
