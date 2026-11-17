---
name: portfolio
description: Review the user's own holdings — P&L, sectors, concentration, bonuses/splits/demergers and capital-gains tax estimates. Use for "review my portfolio", "how am I doing", "tax if I sell everything", "am I too concentrated".
---

# portfolio

Read the [core skill](../../SKILL.md) first. Resolve scripts relative to `${CLAUDE_PLUGIN_ROOT}`. Run them with `python`; if that is not found use `python3` (macOS/Linux) or `py` (Windows). Keep user files outside the plugin folder, and pass the user's words as arguments, never as raw shell text.

Primary route: `python "${CLAUDE_PLUGIN_ROOT}/scripts/holdings_review.py" --input FILE [--tradebook FILE] [--fy 2026-27]`. Start with `--summary` for a short answer-first digest. Add `--format json` when you need exact numbers, `--no-prices` when offline. Input formats, method, rates and limits are in [holdings review](../../references/holdings-review.md).

How to answer:

1. Get the holdings. If the user has a broker connector connected (Kite, Upstox or another), read holdings there and write a `holdings_v1` JSON file outside the plugin folder; never place, modify or cancel orders through the connector. Otherwise use the file they give (Zerodha Console holdings CSV or tradebook CSV) or build `holdings_v1` from pasted data. When the user mentions when they bought, add `"bought": "2024-03-15"` (or just the month, `"2024-03"`) to that holding; it becomes a tax lot and lets bonuses and splits be applied. For tax questions, ask for a tradebook or purchase dates; a holdings file alone has none.
2. Run the script once and answer the user's actual question first, in plain language, using the report's numbers. Show the dates: the holdings date, the price date and the financial year. Show the full report only if they asked for a full review.
3. Bonuses and splits: the script applies Yahoo's split events to dated holdings and lots, and puts a ⚠ on any holding it could not adjust. Yahoo does not say whether an event was a bonus or a split, and Indian tax treats them differently (bonus shares have nil cost and are held from the allotment date). When a ⚠ or an "adjusted for" note mentions one, check the web (exchange filing or company announcement) and rerun with `--bonus SYMBOL` if it was a bonus. Demergers on the skill's curated list (for example ITC → ITC Hotels, Reliance → Jio Financial) are flagged with ⚠ and the new shares the user should hold; quote those numbers, and rerun with `--apply-demergers` if the user wants the published cost split applied. For a demerger not on the list, check the web.
4. Say clearly which holdings show "price unavailable" and that totals then cover priced holdings only. Flag every item under "Data gaps and notes", including lot/holding quantity mismatches and unmatched sells.
5. Concentration: give the largest-holding weight, top-5 weight, Herfindahl reading and the largest sector. Describe it; do not tell the user to rebalance, buy or sell.
6. Tax: call every figure an estimate. When purchase dates are missing the "if sold today" figure is a range; give both ends, say which dates would narrow it, and ask for them. Say the rates are labelled defaults that must be verified, that cess is shown separately, and list what is not modelled when it could matter (surcharge, grandfathering for pre-1 Feb 2018 shares, demergers, loss carry-forward, intraday/F&O). The "LTCG exemption remaining" and the hypothetical "if sold today" line are for information only. Never phrase anything as a recommendation to sell, book profits or harvest losses; say "for information; check with a tax professional".
7. For current tax rules, budget changes, or anything not modelled, use web research ([web research](../../references/web-research.md)): prefer incometaxindia.gov.in, the Finance Act text and CBDT circulars, cite source and date, and keep web facts apart from the script's numbers.
8. If the user asks whether to buy, sell or hold something, explain what the data shows and that the decision is theirs; this tool is not investment or tax advice.

Research and paper workflows only. Missing data is a gap, not an all-clear. Nothing here places an order.
