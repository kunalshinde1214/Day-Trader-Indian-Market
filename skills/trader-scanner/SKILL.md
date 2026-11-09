---
name: trader-scanner
description: Scan Indian equities for multiple daily patterns.
---

# trader-scanner

Read the [shared skill](../../SKILL.md) and [public workflow guide](../../references/public-workflows.md). Resolve scripts relative to `${CLAUDE_PLUGIN_ROOT}`, not the current directory. Keep user data outside the installed plugin/cache. Parse arguments as data; never interpolate raw user prose into a shell.

Primary route: `python "${CLAUDE_PLUGIN_ROOT}/scripts/public_market.py" scan --symbols SYMBOL.NS ...`.

Use universe for the public NSE equity list. Broker-bound scanning uses trader.py scan with dated master and restriction evidence.

Source-available personal-use alpha. Follow all shared execution boundaries. Public data is research-only; missing data is not clearance. No hooks, credential prompts or automatic services are installed.
