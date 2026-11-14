---
name: trader-alerts
description: Bounded price and trade-plan monitoring with local alerts for Indian stocks. Use when the user asks to "watch this stock", "monitor my plan", "alert me if price hits X".
---

# trader-alerts

Read the [shared skill](../../SKILL.md) for safety rules. Resolve scripts relative to `${CLAUDE_PLUGIN_ROOT}`. Run them with `python`; if that is not found use `python3` (macOS/Linux) or `py` (Windows). Keep user files outside the plugin folder, and pass the user's words as arguments, never as raw shell text.

Primary route: `python "${CLAUDE_PLUGIN_ROOT}/scripts/service.py" run --workdir PATH`.

Read references/account-service.md for required bounds. Public entry watching uses public_market.py watch. A one-shot check does not continue monitoring. Send webhooks only when explicitly requested by the user.

Research and paper workflows only. Missing data is a gap, not an all-clear. Nothing here places an order.
