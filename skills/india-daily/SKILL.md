---
name: india-daily
description: Combined daily Indian market report comparing Nifty 50, Sensex and Bank Nifty. Use when the user asks "how is the Indian market today", "market update", "daily market report", "what did the market do" or types /india-daily.
---

# india-daily

Read the [shared skill](../../SKILL.md) for safety rules. Resolve scripts relative to `${CLAUDE_PLUGIN_ROOT}`. Run them with `python`; if that is not found use `python3` (macOS/Linux) or `py` (Windows). Keep user files outside the plugin folder, and pass the user's words as arguments, never as raw shell text.

Primary route: `python "${CLAUDE_PLUGIN_ROOT}/scripts/daily.py" india-daily --collect-public`.

Use --collect-kite or --input for configured sourced inputs. Show feed age, calendar basis and missing coverage. Never substitute --demo for unavailable real data or infer an executable contract from index levels.

Research and paper workflows only. Missing data is a gap, not an all-clear. Nothing here places an order.
