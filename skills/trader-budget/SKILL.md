---
name: trader-budget
description: Size Indian paper positions with rupee costs and lot sizes.
---

# trader-budget

Read the [shared skill](../../SKILL.md) and [public workflow guide](../../references/public-workflows.md). Resolve scripts relative to `${CLAUDE_PLUGIN_ROOT}`, not the current directory. Keep user data outside the installed plugin/cache. Parse arguments as data; never interpolate raw user prose into a shell.

Primary route: `python "${CLAUDE_PLUGIN_ROOT}/scripts/trader.py" size --workdir PATH`.

Require approved personal limits, sourced fees, exact master and margin evidence. Public quotes cannot establish account funds or margin clearance.

Source-available personal-use alpha. Follow all shared execution boundaries. Public data is research-only; missing data is not clearance. No hooks, credential prompts or automatic services are installed.
