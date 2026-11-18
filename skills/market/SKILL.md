---
name: market
description: Indian market today — Nifty 50, Sensex and Bank Nifty levels, previous-day ranges, scenarios and closing context. Use for "how is the market today", "Nifty levels", "Bank Nifty today", "aaj market kaisa hai". The shortcut commands nifty, sensex, banknifty and india-daily open this skill.
---

# market

Read the [core skill](../../SKILL.md) first. Resolve scripts relative to `${CLAUDE_PLUGIN_ROOT}`. Run them with `python`; if that is not found use `python3` (macOS/Linux) or `py` (Windows). Keep user files outside the plugin folder, and pass the user's words as arguments, never as raw shell text.

Routes: `python "${CLAUDE_PLUGIN_ROOT}/scripts/daily.py" india-daily --collect-public` for all three indices, or `nifty`, `sensex`, `banknifty` for one. Pass the name without a leading slash ([daily reports](../../references/daily.md)).

For a specified closing-report date, use `--collect-close --session YYYY-MM-DD` instead. Read [closing context](../../references/closing-context.md) and keep source dates and stale context explicit.

Use --collect-kite or --input for configured sourced inputs. Show feed age, calendar basis and missing coverage. Never substitute --demo for unavailable real data or infer an executable contract from index levels.

Then search the web for what the script does not collect: overnight global cues, GIFT Nifty, FII/DII flows, RBI/SEBI or macro events today, and the main reasons the index moved. Cite each source with its date and keep it apart from the script's numbers ([web research](../../references/web-research.md)).

Research and paper workflows only. Missing data is a gap, not an all-clear. Nothing here places an order.
