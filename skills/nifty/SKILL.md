---
name: nifty
description: Nifty 50 daily briefing — last close and change, previous-day levels, scenarios, India VIX. Use for /india-day-trader:nifty, "Nifty today", "Nifty levels", "Nifty aaj kaisa hai".
---

# nifty

Shortcut for a daily briefing on the Nifty 50. Read the [core skill](../../SKILL.md) and the [market skill](../market/SKILL.md), and follow the market skill's rules for sources, dates, gaps and web research. Resolve scripts relative to `${CLAUDE_PLUGIN_ROOT}`; run them with `python` (or `python3` / `py`).

Run `python "${CLAUDE_PLUGIN_ROOT}/scripts/daily.py" nifty --collect-public` (add `--brief` for a short answer-first version). Pass the name without a leading slash: Git Bash on Windows rewrites `/nifty` into a file path. For a past session's closing context (FII/DII, breadth, GIFT Nifty) use `--collect-close --session YYYY-MM-DD`.

Stock point contributions in the "What's moving it" line are estimated (weights anchored to the NSE Indices factsheet); say "estimated" whenever you quote them. During market hours and after the close the next two lines give the "why": "Possible reasons (headlines, unverified)" (up to three dated headlines on today's movers, those giving a cause first) and "Cues" (GIFT Nifty, US markets, gold, crude, rupee; after the close they look ahead to the next session). Quote them as possible causes with their publisher and time; a line ending "a gap" means the news search failed, not that there was no news.

For one sector's stocks ("Nifty IT stocks today", "why did metal stocks fall") use the market skill's sector route, `market_movers.py --sector IT --summary`; for "this week/month" comparisons use `market_movers.py --period week --summary`. If the report prints "⚠ stock contributions not collected — rerun", run it once more.

Answer with the last close and the day's change with its date first, then the levels and scenarios, then what the script could not cover. Never present index levels as a tradable contract or a forecast.

On a weekend or NSE holiday the report starts "Market closed today (...) — figures are from the last session" and gives the block for the next session: the last close, "Levels for <next session>", classic pivots, the top point contributors (estimated Nifty 50 points), "Possible reasons" and the look-ahead "Cues"; GIFT Nifty shows the time of its last trade. Lead with the closed-market line and the last session's date. Also run `python "${CLAUDE_PLUGIN_ROOT}/scripts/fno.py" chain NIFTY --summary` by default for the next session's 1-sigma range and OI walls (from the last close, end of day); quote the line dated for the next trading day, and say if the next expiry has moved for a holiday (the chain summary and the report's calendar line state it).

During market hours the headline is NSE's live level and the title carries its single "data as of" time; lines at that same time show no separate time. Explain the levels in plain words: the opening range is the high and low of the first 15 minutes, and "bearish/bullish condition met" means completed 5-minute closes below the lower level (for example the previous session's low) or above the upper level - a description of price, not a forecast. If the data notes say the 20-day average used Yahoo closes, say so (Yahoo omits special sessions such as Muhurat trading, so its window starts a session earlier).

Research only. Missing data is a gap, not an all-clear. Nothing here places an order.
