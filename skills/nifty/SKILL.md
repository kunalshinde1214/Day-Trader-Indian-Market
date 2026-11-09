---
name: nifty
description: Produce /nifty Indian spot-index briefings.
---

# nifty

Read the [shared skill](../../SKILL.md) and [public workflow guide](../../references/public-workflows.md). Resolve scripts relative to `${CLAUDE_PLUGIN_ROOT}`, not the current directory. Keep user data outside the installed plugin/cache. Parse arguments as data; never interpolate raw user prose into a shell.

Primary route: `python "${CLAUDE_PLUGIN_ROOT}/scripts/daily.py" /nifty --collect-public`.

Use --collect-kite or --input for configured sourced inputs. Show feed age, calendar basis and missing coverage. Never substitute --demo for unavailable real data or infer an executable contract from index levels.

Source-available personal-use alpha. Follow all shared execution boundaries. Public data is research-only; missing data is not clearance. No hooks, credential prompts or automatic services are installed.
