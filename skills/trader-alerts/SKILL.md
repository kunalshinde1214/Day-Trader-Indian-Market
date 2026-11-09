---
name: trader-alerts
description: Monitor Indian price and trade-plan alerts with bounded polling.
---

# trader-alerts

Read the [shared skill](../../SKILL.md) and [public workflow guide](../../references/public-workflows.md). Resolve scripts relative to `${CLAUDE_PLUGIN_ROOT}`, not the current directory. Keep user data outside the installed plugin/cache. Parse arguments as data; never interpolate raw user prose into a shell.

Primary route: `python "${CLAUDE_PLUGIN_ROOT}/scripts/service.py" run --workdir PATH`.

Read references/account-service.md for required bounds. Public entry watching uses public_market.py watch. A one-shot check does not continue monitoring. Send webhooks only when explicitly requested by the user.

Source-available personal-use alpha. Follow all shared execution boundaries. Public data is research-only; missing data is not clearance. No hooks, credential prompts or automatic services are installed.
