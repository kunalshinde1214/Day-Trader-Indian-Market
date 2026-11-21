---
name: sensex
description: Sensex daily briefing — last close and change, previous-day levels and scenarios. Use for /india-day-trader:sensex, "Sensex today", "Sensex aaj kaisa raha".
---

# sensex

Shortcut for a daily briefing on the Sensex (public Yahoo data; there is no official BSE feed in the skill). Read the [core skill](../../SKILL.md) and the [market skill](../market/SKILL.md), and follow the market skill's rules for sources, dates, gaps and web research. Resolve scripts relative to `${CLAUDE_PLUGIN_ROOT}`; run them with `python` (or `python3` / `py`).

Run `python "${CLAUDE_PLUGIN_ROOT}/scripts/daily.py" sensex --collect-public` (add `--brief` for a short answer-first version). Pass the name without a leading slash: Git Bash on Windows rewrites `/sensex` into a file path. For a past session's closing context (FII/DII, breadth, GIFT Nifty) use `--collect-close --session YYYY-MM-DD`.

Answer with the last close and the day's change with its date first, then the levels and scenarios, then what the script could not cover. Never present index levels as a tradable contract or a forecast.

On a weekend or NSE holiday the report starts "Market closed today (...) — figures are from the last session" and gives the last Sensex close (Yahoo daily close, unofficial), "Levels for <next session>", classic pivots, the top point contributors (estimated Sensex points), "Possible reasons" and the look-ahead "Cues". Lead with the closed-market line and the last session's date.

The "What's moving it" line gives estimated **Sensex** points (the 30 members identified from Sensex's own 5-minute moves, because BSE does not serve its list or weights to scripts; their weights are the Nifty 50 weights anchored to the NSE Indices factsheet, rescaled to 100%). Always say the points are estimated. Quote them as Sensex points; never quote Nifty 50 point contributions as Sensex drivers. If the line shows only members' % moves, the point estimate failed its check, so give no point figures. When the report gives one Sensex percentage (the quote) and a Nifty 50 figure at the same moment, use those; do not mix in a different Sensex percentage from elsewhere in the output.

Sensex comes from Yahoo and can be about 15 minutes behind (BSE does not serve scripts). During market hours the report leads with ONE Sensex figure: "Sensex ≈ X (y%) at HH:MM IST (estimate from live Nifty 50); last Yahoo quote Z at HH:MM IST" - the latest Sensex quote (or shared 5-minute bar) moved by the live NSE Nifty 50 change since then, scaled by the Sensex/Nifty ratio fitted on today's bars (the "Estimated Sensex now" data note says how). Lead your answer with that sentence in plain words (for example "Sensex ≈ 81,890, down about 0.8%, estimated from live Nifty 50 at 13:17 IST; the last Yahoo quote was 81,897 at 13:03 IST"), never as a BSE figure; the like-for-like 5-minute-bar comparison sits in the data notes and needs no mention unless asked. For the "why", use the report's "Possible reasons (headlines, unverified)" and "Cues" lines with their dates (both also after the close, when the cues look ahead to the next session). The Sensex day high/low and pivots come from Yahoo's own daily figures; if a data note says a 5-minute bar had a bad tick, it was left out of the levels. A web lookup is optional: at most one search, `Sensex today <date> live` (for example `Sensex today 18 Nov 2026 live`); if the first result does not give a level with a time from today, stop and use the estimate. Do not try site after site (BSE, Google Finance, Moneycontrol, ET and Business Standard block scripted fetches).

If the report prints "⚠ stock contributions not collected — rerun", NSE refused a download this run; run it once more before answering.

Research only. Missing data is a gap, not an all-clear. Nothing here places an order.
