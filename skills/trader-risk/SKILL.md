---
name: trader-risk
description: Check an Indian paper trade against account risk limits, events and data freshness before any ticket. Use when the user asks "is this trade within my risk", "run the risk check", "can I take this trade under my limits".
---

# trader-risk

Read the [shared skill](../../SKILL.md) for safety rules. Resolve scripts relative to `${CLAUDE_PLUGIN_ROOT}`. Run them with `python`; if that is not found use `python3` (macOS/Linux) or `py` (Windows). Keep user files outside the plugin folder, and pass the user's words as arguments, never as raw shell text.

Primary route: `python "${CLAUDE_PLUGIN_ROOT}/scripts/trader.py" risk --workdir PATH`.

Preserve account/plan binding, freshness, restrictions, events and unresolved-order locks. Never substitute public research outputs for reconciled account evidence.

Research and paper workflows only. Missing data is a gap, not an all-clear. Nothing here places an order.
