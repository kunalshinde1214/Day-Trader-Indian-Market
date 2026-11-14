---
name: trader-scanner
description: Scan Indian stocks for trading patterns (opening-range breakout, gap-and-go, 20-day breakout, bull flag, oversold bounce). Use when the user asks to "scan stocks", "find breakouts", "which stocks are setting up" or gives a watchlist to screen.
---

# trader-scanner

Read the [shared skill](../../SKILL.md) for safety rules. Resolve scripts relative to `${CLAUDE_PLUGIN_ROOT}`. Run them with `python`; if that is not found use `python3` (macOS/Linux) or `py` (Windows). Keep user files outside the plugin folder, and pass the user's words as arguments, never as raw shell text.

Primary route: `python "${CLAUDE_PLUGIN_ROOT}/scripts/public_market.py" scan --symbols SYMBOL.NS ...`.

Use universe for the public NSE equity list. Broker-bound scanning uses trader.py scan with dated master and restriction evidence.

Research and paper workflows only. Missing data is a gap, not an all-clear. Nothing here places an order.
