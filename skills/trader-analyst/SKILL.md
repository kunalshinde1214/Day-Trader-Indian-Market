---
name: trader-analyst
description: Extract sourced Indian XBRL financial results for comparison.
---

# trader-analyst

Read the [shared skill](../../SKILL.md) and [public workflow guide](../../references/public-workflows.md). Resolve scripts relative to `${CLAUDE_PLUGIN_ROOT}`, not the current directory. Keep user data outside the installed plugin/cache. Parse arguments as data; never interpolate raw user prose into a shell.

Primary route: `python "${CLAUDE_PLUGIN_ROOT}/scripts/research.py" analyst --input PATH --source HTTPS_URL`.

Preserve entity, reporting dimensions, period, units and unsupported facts. Use --url for an official selected attachment. Never invent metrics or treat headlines as financial results.

Source-available personal-use alpha. Follow all shared execution boundaries. Public data is research-only; missing data is not clearance. No hooks, credential prompts or automatic services are installed.
