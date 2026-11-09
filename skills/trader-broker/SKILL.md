---
name: trader-broker
description: Prepare Indian paper tickets and individually human-operated live previews.
---

# trader-broker

Read the [shared skill](../../SKILL.md) and [public workflow guide](../../references/public-workflows.md). Resolve scripts relative to `${CLAUDE_PLUGIN_ROOT}`, not the current directory. Keep user data outside the installed plugin/cache. Parse arguments as data; never interpolate raw user prose into a shell.

Primary route: `python "${CLAUDE_PLUGIN_ROOT}/scripts/trader.py" ticket --workdir PATH`.

Read references/execution.md before preparing live.py actions. Both exact-action confirmations must be operated by the human. Never type them, automate stdin, retry ambiguous orders or treat this skill invocation as trade authorization.

Source-available personal-use alpha. Follow all shared execution boundaries. Public data is research-only; missing data is not clearance. No hooks, credential prompts or automatic services are installed.
