---
name: banknifty
description: Bank Nifty daily briefing — last close and change, previous-day levels and scenarios. Use for /india-day-trader:banknifty, "Bank Nifty today", "Bank Nifty levels".
---

# banknifty

Shortcut for a daily briefing on Bank Nifty. Read the [core skill](../../SKILL.md) and the [market skill](../market/SKILL.md), and follow the market skill's rules for sources, dates, gaps and web research. Resolve scripts relative to `${CLAUDE_PLUGIN_ROOT}`; run them with `python` (or `python3` / `py`).

Run `python "${CLAUDE_PLUGIN_ROOT}/scripts/daily.py" banknifty --collect-public` (add `--brief` for a short answer-first version). Pass the name without a leading slash: Git Bash on Windows rewrites `/banknifty` into a file path. For a past session's closing context (FII/DII, breadth, GIFT Nifty) use `--collect-close --session YYYY-MM-DD`.

Answer with the last close and the day's change with its date first, then the levels and scenarios, then what the script could not cover. Never present index levels as a tradable contract or a forecast.

Research only. Missing data is a gap, not an all-clear. Nothing here places an order.
