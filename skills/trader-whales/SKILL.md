---
name: trader-whales
description: Discover Indian public promoter and institutional disclosures.
---

# trader-whales

Read the [shared skill](../../SKILL.md) and [public workflow guide](../../references/public-workflows.md). Resolve scripts relative to `${CLAUDE_PLUGIN_ROOT}`, not the current directory. Keep user data outside the installed plugin/cache. Parse arguments as data; never interpolate raw user prose into a shell.

Primary route: `python "${CLAUDE_PLUGIN_ROOT}/scripts/research.py" disclosures --symbols SYMBOL.NS ... --state PATH`.

Announcements are discovery candidates, not parsed trades. Publication date is not transaction date. Normalize reviewed PIT/SAST/pledge/bulk/block facts with data.py whales; never infer insider motives.

Source-available personal-use alpha. Follow all shared execution boundaries. Public data is research-only; missing data is not clearance. No hooks, credential prompts or automatic services are installed.
