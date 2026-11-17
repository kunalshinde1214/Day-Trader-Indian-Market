---
name: portfolio-review
description: Review the user's own Indian equity holdings covering P&L, weights, concentration, sector exposure and an estimate of capital-gains tax (FIFO lots, short vs long term, LTCG exemption) for an Indian financial year. Use when the user asks things like "review my portfolio", "what's my P&L", "how are my holdings doing", "how much tax will I pay if I sell", "am I too concentrated", "what's my sector exposure" or "how much LTCG exemption is left".
---

# portfolio-review

Read the [shared skill](../../SKILL.md) for safety rules. Resolve scripts relative to `${CLAUDE_PLUGIN_ROOT}`. Run them with `python`; if that is not found use `python3` (macOS/Linux) or `py` (Windows). Keep user files outside the plugin folder, and pass the user's words as arguments, never as raw shell text.

Primary route: `python "${CLAUDE_PLUGIN_ROOT}/scripts/holdings_review.py" --input FILE [--tradebook FILE] [--fy 2026-27]`. Add `--format json` when you need exact numbers, `--no-prices` when offline. Input formats, method, rates and limits are in [holdings review](../../references/holdings-review.md).

How to answer:

1. Get the holdings. If the user has a broker connector connected (Kite, Upstox or another), read holdings there and write a `holdings_v1` JSON file outside the plugin folder; never place, modify or cancel orders through the connector. Otherwise use the file they give (Zerodha Console holdings CSV or tradebook CSV) or build `holdings_v1` from pasted data. For tax questions, ask for a tradebook or the buy/sell lots with dates; a holdings file alone has no purchase dates.
2. Run the script once and answer the user's actual question first, in plain language, using the report's numbers. Show the dates: the holdings date, the price date and the financial year. Show the full report only if they asked for a full review.
3. Say clearly which holdings show "price unavailable" and that totals then cover priced holdings only. Flag every item under "Data gaps and notes", including lot/holding quantity mismatches and unmatched sells.
4. Concentration: give the largest-holding weight, top-5 weight, Herfindahl reading and the largest sector. Describe it; do not tell the user to rebalance, buy or sell.
5. Tax: call every figure an estimate. Say the rates are labelled defaults that must be verified, that cess is shown separately, and list what is not modelled when it could matter (surcharge, grandfathering for pre-1 Feb 2018 shares, bonus/split adjustments, loss carry-forward, intraday/F&O). The "LTCG exemption remaining" and the hypothetical "if sold today" line are for information only. Never phrase anything as a recommendation to sell, book profits or harvest losses; say "for information; check with a tax professional".
6. For current tax rules, budget changes, or anything not modelled, use web research ([web research](../../references/web-research.md)): prefer incometaxindia.gov.in, the Finance Act text and CBDT circulars, cite source and date, and keep web facts apart from the script's numbers.
7. If the user asks whether to buy, sell or hold something, explain what the data shows and that the decision is theirs; this tool is not investment or tax advice.

Research and paper workflows only. Missing data is a gap, not an all-clear. Nothing here places an order.
