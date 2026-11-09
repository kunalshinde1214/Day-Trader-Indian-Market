---
name: trader-news
description: Read Indian watchlist headlines and exchange announcements.
---

# trader-news

Read the [shared skill](../../SKILL.md) and [public workflow guide](../../references/public-workflows.md). Resolve scripts relative to `${CLAUDE_PLUGIN_ROOT}`, not the current directory. Keep user data outside the installed plugin/cache. Parse arguments as data; never interpolate raw user prose into a shell.

Primary route: `python "${CLAUDE_PLUGIN_ROOT}/scripts/research.py" news --symbols SYMBOL.NS ... --state PATH`.

For `/india-news` or a market-wide briefing, use `python "${CLAUDE_PLUGIN_ROOT}/scripts/india_news.py"`. This is a route within this skill, not another registered plugin command. Read [the briefing workflow](../../references/india-news.md). Default NSE feeds require no watchlist. Economic Times is an optional personal-reading source. Show the Markdown report, source failures and timestamps; never treat an empty feed as no news.

Report new items, failed sources and publication timestamps. Read the linked story before describing a headline as fact.

Source-available personal-use alpha. Follow all shared execution boundaries. Public data is research-only; missing data is not clearance. No hooks, credential prompts or automatic services are installed.
