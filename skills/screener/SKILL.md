---
name: screener
description: Screen Indian stocks on fundamentals (P/E, ROE, debt, growth, yield) or chart patterns (breakouts, flags, oversold) across Nifty indices and sectors. Use for "Nifty 50 stocks with ROE above 20", "cheapest IT stocks", "scan for breakouts", "achhe fundamentals wale stocks".
---

# screener

Read the [core skill](../../SKILL.md) first. Resolve scripts relative to `${CLAUDE_PLUGIN_ROOT}`. Run them with `python`; if that is not found use `python3` (macOS/Linux) or `py` (Windows). Keep user files outside the plugin folder, and pass the user's words as arguments, never as raw shell text.

Primary route: `python "${CLAUDE_PLUGIN_ROOT}/scripts/screener.py" --universe nifty50 --where "roe>15" --where "pe<25" --sort roe` ([details](../../references/screener.md)).

How to answer:

1. Turn the request into a universe and filters. Universes: `nifty50`, `niftynext50`, `nifty100`, `nifty200`, `nifty500`, `midcap150`, `smallcap250`, and sectors `bank`, `psubank`, `it`, `pharma`, `healthcare`, `auto`, `fmcg`, `metal`, `realty`, `energy`, `oilgas`, `media`, `consumerdurables`; or `--symbols` for the user's own list; `--universe` accepts several (merged). `dividend_yield` is the last 12 months of dividends per share over price, with special dividends flagged. Fields: `pe`, `pb`, `roe`, `net_margin`, `operating_margin`, `debt_to_equity`, `revenue_growth`, `eps_growth`, `dividend_yield`, `fcf_yield`, `market_cap_cr`, `return_1y`, `from_high`, `rsi14`, `price`. Percent fields accept `roe>15` or `roe>15%`.
2. Say which filters you used when the request was vague ("cheap" → for example `pe<15`), so the user can change them.
3. Nifty 500 takes a few minutes (about 1,000 requests); prefer a smaller universe unless the user asks for the whole market.
4. Report how many stocks were evaluated, matched, missing data and failed, and the price date. Missing data is not a pass. Mention every ⚠ earnings check next to the stock, and the bank equity warning when screening banks on ROE or P/B.
5. A match is a filter result, not a recommendation. For banks and finance companies, debt/equity and margins are not meaningful; say so if they are filtered on. Offer to run the `stock` skill on any name the user wants to look at further.

## Chart-pattern scans

Primary route: `python "${CLAUDE_PLUGIN_ROOT}/scripts/public_market.py" scan --symbols SYMBOL.NS ...`.

Use universe for the public NSE equity list. Broker-bound scanning uses trader.py scan with dated master and restriction evidence.

Research and paper workflows only. Missing data is a gap, not an all-clear. Nothing here places an order.
