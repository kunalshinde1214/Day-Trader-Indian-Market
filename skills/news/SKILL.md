---
name: news
description: Indian market and company news — NSE announcements ranked by importance, per-stock headlines and filings. Use for "latest market news", "news on RELIANCE", "any announcements today", "why did the market or X move", or /india-news.
---

# news

Read the [core skill](../../SKILL.md) first. Resolve scripts relative to `${CLAUDE_PLUGIN_ROOT}`. Run them with `python`; if that is not found use `python3` (macOS/Linux) or `py` (Windows). Keep user files outside the plugin folder, and pass the user's words as arguments, never as raw shell text.

Market-wide briefing: `python "${CLAUDE_PLUGIN_ROOT}/scripts/india_news.py"` (routine filings such as fund NAVs are hidden; add `--include-routine` to see them).

Primary route: `python "${CLAUDE_PLUGIN_ROOT}/scripts/research.py" news --symbols SYMBOL.NS ... --state PATH`.

For `/india-news` or a market-wide briefing, use `python "${CLAUDE_PLUGIN_ROOT}/scripts/india_news.py"`. This is a route within this skill, not another registered plugin command. Read [the briefing workflow](../../references/india-news.md). Default NSE feeds require no watchlist. Economic Times is an optional personal-reading source. Show the Markdown report, source failures and timestamps; never treat an empty feed as no news.

Report new items, failed sources and publication timestamps. Read the linked story before describing a headline as fact.

The feeds above cover NSE and a few news sources. For BSE announcements, a specific company or event, or anything in the last few hours, search the web and read the story or filing; cite source and date ([web research](../../references/web-research.md)).

## "Why did the market move?"

Run `python "${CLAUDE_PLUGIN_ROOT}/scripts/market_movers.py" --summary` (add `--premise up` or `--premise down` with the user's claim, or `--session YYYY-MM-DD` for a past day). It states the actual direction first, so correct a wrong premise before explaining. It gives sector moves, breadth, the stocks that added or took away the most index points (estimated weights, with the check against the actual move), VIX and the previous session's FII/DII. Treat its headlines as possible causes, not verified ones; add dated news from the web for the reasons, and keep the two apart.

Research and paper workflows only. Missing data is a gap, not an all-clear. Nothing here places an order.
