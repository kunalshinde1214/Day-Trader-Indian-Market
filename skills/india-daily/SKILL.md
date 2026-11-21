---
name: india-daily
description: Combined daily report comparing Nifty 50, Sensex and Bank Nifty, with closing flows and breadth. Use for /india-day-trader:india-daily, "market update", "aaj market kaisa hai".
---

# india-daily

Shortcut for a daily briefing on all three indices. Read the [core skill](../../SKILL.md) and the [market skill](../market/SKILL.md), and follow the market skill's rules for sources, dates, gaps and web research. Resolve scripts relative to `${CLAUDE_PLUGIN_ROOT}`; run them with `python` (or `python3` / `py`).

Run `python "${CLAUDE_PLUGIN_ROOT}/scripts/daily.py" india-daily --collect-public` (add `--brief` for a short answer-first version). Pass the name without a leading slash: Git Bash on Windows rewrites `/india-daily` into a file path. For a past session's closing context (FII/DII, breadth, GIFT Nifty) use `--collect-close --session YYYY-MM-DD`.

The Sensex row is Yahoo data about 15 minutes behind; during market hours the report gives ONE Sensex figure, "Sensex ≈ X (y%) at HH:MM IST (estimate from live Nifty 50); last Yahoo quote Z at HH:MM IST" - quote it that way (never as a BSE figure, never with a second Sensex percentage) and follow the market skill's rule for at most one optional web lookup. Stock point contributions are estimated; say so. For the "why", use the "Possible reasons (headlines, unverified)" and "Cues" lines with their dates; "a gap" there means the search failed. Both lines stay after the close; the cues then look ahead to the next session.

Answer with the last close and the day's change with its date first, then the levels and scenarios, then what the script could not cover. Never present index levels as a tradable contract or a forecast.

On a weekend or NSE holiday the report starts "Market closed today (...) — figures are from the last session" and, for each index, gives the last close, "Levels for <next session>" and classic pivots, plus the top point contributors (estimated), "Possible reasons", the look-ahead "Cues", all-NSE breadth, FII/DII (buyers or sellers in words) and GIFT Nifty with the time of its last trade. Lead with the closed-market line and the last session's date.

During and after the session the report adds a "Broader market" line from the same NSE snapshot: Nifty Midcap 150 and Smallcap 250 change and Nifty 500 advances/declines. Use it to say whether the move is broad (midcaps and smallcaps falling with the large caps, most Nifty 500 stocks down) or narrow.

For a period ("midcaps and smallcaps this week vs Nifty") run `python "${CLAUDE_PLUGIN_ROOT}/scripts/market_movers.py" --period week --summary`, and for one sector's stocks `--sector METAL --summary` (see the market skill). If a report prints "⚠ stock contributions not collected — rerun", run it once more before answering.

Research only. Missing data is a gap, not an all-clear. Nothing here places an order.
