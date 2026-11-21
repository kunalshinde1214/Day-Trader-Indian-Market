---
name: banknifty
description: Bank Nifty daily briefing — last close and change, previous-day levels and scenarios. Use for /india-day-trader:banknifty, "Bank Nifty today", "Bank Nifty levels".
---

# banknifty

Shortcut for a daily briefing on Bank Nifty. Read the [core skill](../../SKILL.md) and the [market skill](../market/SKILL.md), and follow the market skill's rules for sources, dates, gaps and web research. Resolve scripts relative to `${CLAUDE_PLUGIN_ROOT}`; run them with `python` (or `python3` / `py`).

Run `python "${CLAUDE_PLUGIN_ROOT}/scripts/daily.py" banknifty --collect-public` (add `--brief` for a short answer-first version). Pass the name without a leading slash: Git Bash on Windows rewrites `/banknifty` into a file path. For a past session's closing context (FII/DII, breadth, GIFT Nifty) use `--collect-close --session YYYY-MM-DD`.

Answer with the last close and the day's change with its date first, then the levels and scenarios, then what the script could not cover. For the "why", quote the report's "Possible reasons (headlines, unverified)" and "Cues" lines with their dates (both during and after the session; after the close the cues look ahead to the next session), as possible causes, never confirmed ones. Never present index levels as a tradable contract or a forecast.

For "kal ke liye levels" (levels for tomorrow / the next session), run both:

1. `python "${CLAUDE_PLUGIN_ROOT}/scripts/daily.py" banknifty --collect-public` for the price levels (during the session these are today's provisional high, low and last; after 15:30 IST they are final, with classic pivots).
2. `python "${CLAUDE_PLUGIN_ROOT}/scripts/fno.py" chain BANKNIFTY --summary` for the options view: the implied range and the call/put OI walls.

From the chain summary, quote the 1-sigma range dated for the next session (the line naming the next trading day, e.g. "1-sigma for the Thu 19 Nov session" or "to the Thu 19 Nov close"), not a line for today's session, and give the "OI walls near spot" (the largest call and put OI inside the 1-SD range to expiry) as the next-session levels where open interest is concentrated (often read as resistance and support, not guaranteed levels); mention the biggest-overall walls and max pain only with their distance from spot, since they can sit several percent away. Say the range is a one-standard-deviation band from at-the-money implied volatility (roughly two days in three stay inside it), not a forecast. During market hours, say the price levels are provisional and offer to re-run after 15:30 IST.

On a weekend or NSE holiday the daily report starts "Market closed today (...) — figures are from the last session" and already carries the next-session block: the last close, "Levels for <next session>", classic pivots, the top point contributors (estimated Bank Nifty points), "Possible reasons" and the look-ahead "Cues". Also run step 2 above by default, `python "${CLAUDE_PLUGIN_ROOT}/scripts/fno.py" chain BANKNIFTY --summary`, even when the user only asked "how did Bank Nifty do": the market reopens on the next trading day, and the chain gives its 1-sigma range and OI walls from the last close. Say if the next expiry has moved for a holiday (the chain summary and the report's calendar line state it, e.g. "moved from Tuesday ..., NSE trading holiday"), and that the chain data are from the last session's close (end of day), not live.

For the bank stocks themselves ("which PSU banks fell?") use `python "${CLAUDE_PLUGIN_ROOT}/scripts/market_movers.py" --sector "PSU BANK" --summary` (or `BANK`, `PRIVATE BANK`). If the report prints "⚠ stock contributions not collected — rerun", run it once more.

Research only. Missing data is a gap, not an all-clear. Nothing here places an order.
