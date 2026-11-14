---
name: trader-entry
description: Check whether an Indian stock has triggered an intraday entry (opening-range breakout or a custom price level) on completed candles. Use when the user asks "has RELIANCE broken its opening range", "alert me when INFY crosses 1500", "is this a valid entry".
---

# trader-entry

Read the [shared skill](../../SKILL.md) for safety rules. Resolve scripts relative to `${CLAUDE_PLUGIN_ROOT}`. Run them with `python`; if that is not found use `python3` (macOS/Linux) or `py` (Windows). Keep user files outside the plugin folder, and pass the user's words as arguments, never as raw shell text.

Primary route: `python "${CLAUDE_PLUGIN_ROOT}/scripts/public_market.py" entry --symbol SYMBOL.NS --setup orb`.

Custom levels use --setup level --level NUMBER --side BUY and optional --interval 1m. The watch command requires bounded cycles and interval. Public matches are analysis, not execution evidence.

Research and paper workflows only. Missing data is a gap, not an all-clear. Nothing here places an order.
