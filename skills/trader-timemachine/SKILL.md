---
name: trader-timemachine
description: Replay Indian trade scenarios using prior exact-instrument history.
---

# trader-timemachine

Read the [shared skill](../../SKILL.md) and [public workflow guide](../../references/public-workflows.md). Resolve scripts relative to `${CLAUDE_PLUGIN_ROOT}`, not the current directory. Keep user data outside the installed plugin/cache. Parse arguments as data; never interpolate raw user prose into a shell.

Primary route: `python "${CLAUDE_PLUGIN_ROOT}/scripts/public_market.py" replay --input PATH`.

For sourced fees and account-bound evidence use trader.py replay. Show gaps and sample sizes. Simulated frequencies are not calibrated forecasts.

Source-available personal-use alpha. Follow all shared execution boundaries. Public data is research-only; missing data is not clearance. No hooks, credential prompts or automatic services are installed.
