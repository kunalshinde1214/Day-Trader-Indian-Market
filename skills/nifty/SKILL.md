---
name: nifty
description: Nifty 50 market briefing with today's levels, previous-day high/low/close, opening range and bullish/bearish scenarios. Use when the user asks "how is Nifty doing", "Nifty levels today", "Nifty pre-open/close report" or types /nifty.
---

# nifty

Read the [shared skill](../../SKILL.md) for safety rules. Resolve scripts relative to `${CLAUDE_PLUGIN_ROOT}`. Run them with `python`; if that is not found use `python3` (macOS/Linux) or `py` (Windows). Keep user files outside the plugin folder, and pass the user's words as arguments, never as raw shell text.

Primary route: `python "${CLAUDE_PLUGIN_ROOT}/scripts/daily.py" nifty --collect-public`.

Use --collect-kite or --input for configured sourced inputs. Show feed age, calendar basis and missing coverage. Never substitute --demo for unavailable real data or infer an executable contract from index levels.

Research and paper workflows only. Missing data is a gap, not an all-clear. Nothing here places an order.
