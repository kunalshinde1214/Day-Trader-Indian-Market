---
name: trader-broker
description: Prepare paper trade tickets and, only with explicit opt-in, human-operated live order previews for Kite, Upstox, Dhan, Angel One or FYERS. Use when the user asks to "make a ticket", "prepare the order" or "connect my broker".
---

# trader-broker

Read the [shared skill](../../SKILL.md) for safety rules. Resolve scripts relative to `${CLAUDE_PLUGIN_ROOT}`. Run them with `python`; if that is not found use `python3` (macOS/Linux) or `py` (Windows). Keep user files outside the plugin folder, and pass the user's words as arguments, never as raw shell text.

Primary route: `python "${CLAUDE_PLUGIN_ROOT}/scripts/trader.py" ticket --workdir PATH`.

Read references/execution.md before preparing live.py actions. Both exact-action confirmations must be operated by the human. Never type them, automate stdin, retry ambiguous orders or treat this skill invocation as trade authorization.

Research and paper workflows only. Missing data is a gap, not an all-clear. Nothing here places an order.
