---
name: trader-risk
description: Check Indian account-bound trading risk and evidence.
---

# trader-risk

Read the [shared skill](../../SKILL.md) and [public workflow guide](../../references/public-workflows.md). Resolve scripts relative to `${CLAUDE_PLUGIN_ROOT}`, not the current directory. Keep user data outside the installed plugin/cache. Parse arguments as data; never interpolate raw user prose into a shell.

Primary route: `python "${CLAUDE_PLUGIN_ROOT}/scripts/trader.py" risk --workdir PATH`.

Preserve account/plan binding, freshness, restrictions, events and unresolved-order locks. Never substitute public research outputs for reconciled account evidence.

Source-available personal-use alpha. Follow all shared execution boundaries. Public data is research-only; missing data is not clearance. No hooks, credential prompts or automatic services are installed.
