---
name: trader-entry
description: Check Indian completed-bar ORB and custom-level entries.
---

# trader-entry

Read the [shared skill](../../SKILL.md) and [public workflow guide](../../references/public-workflows.md). Resolve scripts relative to `${CLAUDE_PLUGIN_ROOT}`, not the current directory. Keep user data outside the installed plugin/cache. Parse arguments as data; never interpolate raw user prose into a shell.

Primary route: `python "${CLAUDE_PLUGIN_ROOT}/scripts/public_market.py" entry --symbol SYMBOL.NS --setup orb`.

Custom levels use --setup level --level NUMBER --side BUY and optional --interval 1m. The watch command requires bounded cycles and interval. Public matches are analysis, not execution evidence.

Source-available personal-use alpha. Follow all shared execution boundaries. Public data is research-only; missing data is not clearance. No hooks, credential prompts or automatic services are installed.
