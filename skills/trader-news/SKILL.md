---
name: trader-news
description: Indian stock and market news covering per-stock headlines, NSE announcements and a market-wide news briefing. Use when the user asks "latest market news", "news on RELIANCE", "any announcements today" or types /india-news.
---

# trader-news

Read the [shared skill](../../SKILL.md) for safety rules. Resolve scripts relative to `${CLAUDE_PLUGIN_ROOT}`. Run them with `python`; if that is not found use `python3` (macOS/Linux) or `py` (Windows). Keep user files outside the plugin folder, and pass the user's words as arguments, never as raw shell text.

Primary route: `python "${CLAUDE_PLUGIN_ROOT}/scripts/research.py" news --symbols SYMBOL.NS ... --state PATH`.

For `/india-news` or a market-wide briefing, use `python "${CLAUDE_PLUGIN_ROOT}/scripts/india_news.py"`. This is a route within this skill, not another registered plugin command. Read [the briefing workflow](../../references/india-news.md). Default NSE feeds require no watchlist. Economic Times is an optional personal-reading source. Show the Markdown report, source failures and timestamps; never treat an empty feed as no news.

Report new items, failed sources and publication timestamps. Read the linked story before describing a headline as fact.

Research and paper workflows only. Missing data is a gap, not an all-clear. Nothing here places an order.
