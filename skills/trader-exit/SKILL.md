---
name: trader-exit
description: Plan Indian trade stops and targets using structural, ATR or explicit rules.
---

# trader-exit

Read the [shared skill](../../SKILL.md) and [public workflow guide](../../references/public-workflows.md). Resolve scripts relative to `${CLAUDE_PLUGIN_ROOT}`, not the current directory. Keep user data outside the installed plugin/cache. Parse arguments as data; never interpolate raw user prose into a shell.

Primary route: `python "${CLAUDE_PLUGIN_ROOT}/scripts/public_market.py" plan --input PATH --stop RULE --target RULE`.

Broker-bound plans use config exit_stop/exit_target and trader.py plan. Regenerate replay, sizing and risk after any plan change. A plan is not a submitted protective order.

Source-available personal-use alpha. Follow all shared execution boundaries. Public data is research-only; missing data is not clearance. No hooks, credential prompts or automatic services are installed.
