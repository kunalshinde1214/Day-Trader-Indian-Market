---
name: trader-analyst
description: Read Indian company financial results from official XBRL filings and compare periods (YoY, TTM). Use when the user asks "analyse the latest results", "compare quarterly results", "read this filing".
---

# trader-analyst

Read the [shared skill](../../SKILL.md) for safety rules. Resolve scripts relative to `${CLAUDE_PLUGIN_ROOT}`. Run them with `python`; if that is not found use `python3` (macOS/Linux) or `py` (Windows). Keep user files outside the plugin folder, and pass the user's words as arguments, never as raw shell text.

Primary route: `python "${CLAUDE_PLUGIN_ROOT}/scripts/research.py" analyst --input PATH --source HTTPS_URL`.

For feed-driven discovery, follow [the results and filing workflow](../../references/india-news.md): collect official results feeds, optionally match against a sourced issuer list, then use `filing_workflow.py collect` with a bounded limit. Preserve unmatched/ambiguous identities and all unsupported attachment statuses.

Preserve entity, reporting dimensions, period, units and unsupported facts. Use --url for an official selected attachment. Never invent metrics or treat headlines as financial results.

Research and paper workflows only. Missing data is a gap, not an all-clear. Nothing here places an order.
