---
name: news
description: Indian market and company news — NSE announcements ranked by importance, per-stock headlines and filings. Use for "latest market news", "news on RELIANCE", "any announcements today", "why did X move", or /india-news.
---

# news

Read the [core skill](../../SKILL.md) first. Resolve scripts relative to `${CLAUDE_PLUGIN_ROOT}`. Run them with `python`; if that is not found use `python3` (macOS/Linux) or `py` (Windows). Keep user files outside the plugin folder, and pass the user's words as arguments, never as raw shell text.

Market-wide briefing: `python "${CLAUDE_PLUGIN_ROOT}/scripts/india_news.py"` (routine filings such as fund NAVs are hidden; add `--include-routine` to see them).

Primary route: `python "${CLAUDE_PLUGIN_ROOT}/scripts/research.py" news --symbols SYMBOL.NS ... --state PATH`.

For `/india-news` or a market-wide briefing, use `python "${CLAUDE_PLUGIN_ROOT}/scripts/india_news.py"`. This is a route within this skill, not another registered plugin command. Read [the briefing workflow](../../references/india-news.md). Default NSE feeds require no watchlist. Economic Times is an optional personal-reading source. Show the Markdown report, source failures and timestamps; never treat an empty feed as no news.

Report new items, failed sources and publication timestamps. Read the linked story before describing a headline as fact.

The feeds above cover NSE and a few news sources. For BSE announcements, a specific company or event, or anything in the last few hours, search the web and read the story or filing; cite source and date ([web research](../../references/web-research.md)).

Research and paper workflows only. Missing data is a gap, not an all-clear. Nothing here places an order.
