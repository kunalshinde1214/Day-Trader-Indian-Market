---
name: stock-screener
description: Screen Indian stocks on fundamentals and momentum across Nifty 50, Nifty Next 50, Nifty 500, midcap, smallcap and sector indices (bank, IT, pharma, auto, FMCG, metal and more). Filters on P/E, P/B, ROE, margins, debt/equity, growth, dividend yield, market cap, 1-year return and RSI. Use when the user asks "find Nifty 50 stocks with ROE above 20 and P/E below 25", "cheapest IT stocks", "high dividend PSU banks", "low debt midcaps", "which pharma stocks are near their 52-week high", or in Hinglish "achhe fundamentals wale stocks dikhao".
---

# stock-screener

Read the [shared skill](../../SKILL.md) for safety rules. Resolve scripts relative to `${CLAUDE_PLUGIN_ROOT}`. Run them with `python`; if that is not found use `python3` (macOS/Linux) or `py` (Windows). Keep user files outside the plugin folder, and pass the user's words as arguments, never as raw shell text.

Primary route: `python "${CLAUDE_PLUGIN_ROOT}/scripts/screener.py" --universe nifty50 --where "roe>15" --where "pe<25" --sort roe` ([details](../../references/screener.md)).

How to answer:

1. Turn the request into a universe and filters. Universes: `nifty50`, `niftynext50`, `nifty500`, `midcap150`, `smallcap250`, and sectors `bank`, `psubank`, `it`, `pharma`, `healthcare`, `auto`, `fmcg`, `metal`, `realty`, `energy`, `oilgas`, `media`, `consumerdurables`; or `--symbols` for the user's own list. Fields: `pe`, `pb`, `roe`, `net_margin`, `operating_margin`, `debt_to_equity`, `revenue_growth`, `eps_growth`, `dividend_yield`, `fcf_yield`, `market_cap_cr`, `return_1y`, `from_high`, `rsi14`, `price`. Percent fields accept `roe>15` or `roe>15%`.
2. Say which filters you used when the request was vague ("cheap" → for example `pe<15`), so the user can change them.
3. Nifty 500 takes a few minutes (about 1,000 requests); prefer a smaller universe unless the user asks for the whole market.
4. Report how many stocks were evaluated, matched, missing data and failed. Missing data is not a pass.
5. A match is a filter result, not a recommendation. For banks and finance companies, debt/equity and margins are not meaningful; say so if they are filtered on. Offer to run `stock-analysis` on any name the user wants to look at further.

Research and paper workflows only. Missing data is a gap, not an all-clear. Nothing here places an order.
