---
name: nifty
description: Nifty 50 daily briefing — last close and change, previous-day levels, scenarios, India VIX. Use for /india-day-trader:nifty, "Nifty today", "Nifty levels", "Nifty aaj kaisa hai".
---

# nifty

Shortcut for a daily briefing on the Nifty 50. Read the [core skill](../../SKILL.md) and the [market skill](../market/SKILL.md), and follow the market skill's rules for sources, dates, gaps and web research. Resolve scripts relative to `${CLAUDE_PLUGIN_ROOT}`; run them with `python` (or `python3` / `py`).

Run `python "${CLAUDE_PLUGIN_ROOT}/scripts/daily.py" nifty --collect-public` (add `--brief` for a short answer-first version). Pass the name without a leading slash: Git Bash on Windows rewrites `/nifty` into a file path. For a past session's closing context (FII/DII, breadth, GIFT Nifty) use `--collect-close --session YYYY-MM-DD`.

Answer with the last close and the day's change with its date first, then the levels and scenarios, then what the script could not cover. Never present index levels as a tradable contract or a forecast.

Research only. Missing data is a gap, not an all-clear. Nothing here places an order.
