---
name: trader-whales
description: Find promoter, insider, bulk/block deal and pledge disclosures for Indian stocks. Use when the user asks "any insider buying in X", "promoter pledges", "bulk deals today", "what are institutions doing".
---

# trader-whales

Read the [shared skill](../../SKILL.md) for safety rules. Resolve scripts relative to `${CLAUDE_PLUGIN_ROOT}`. Run them with `python`; if that is not found use `python3` (macOS/Linux) or `py` (Windows). Keep user files outside the plugin folder, and pass the user's words as arguments, never as raw shell text.

Primary route: `python "${CLAUDE_PLUGIN_ROOT}/scripts/research.py" disclosures --symbols SYMBOL.NS ... --state PATH`.

Announcements are discovery candidates, not parsed trades. Publication date is not transaction date. Normalize reviewed PIT/SAST/pledge/bulk/block facts with data.py whales; never infer insider motives.

For BSE filings, press-reported bulk/block deals and shareholding-pattern changes, search bseindia.com, nseindia.com and the company's filings, then normalize reviewed facts the same way ([web research](../../references/web-research.md)).

Research and paper workflows only. Missing data is a gap, not an all-clear. Nothing here places an order.
