---
name: screener
description: Screen Indian stocks on fundamentals (P/E, ROE, debt, growth, yield) or chart patterns (breakouts, flags, oversold) across Nifty indices and sectors. Use for "Nifty 50 stocks with ROE above 20", "cheapest IT stocks", "scan for breakouts", "achhe fundamentals wale stocks".
---

# screener

Read the [core skill](../../SKILL.md) first. Resolve scripts relative to `${CLAUDE_PLUGIN_ROOT}`. Run them with `python`; if that is not found use `python3` (macOS/Linux) or `py` (Windows). Keep user files outside the plugin folder, and pass the user's words as arguments, never as raw shell text.

Primary route: `python "${CLAUDE_PLUGIN_ROOT}/scripts/screener.py" --universe nifty50 --where "roe>15" --where "pe<25" --sort roe` ([details](../../references/screener.md)).

How to answer:

1. Turn the request into a universe and filters. Universes: `nifty50`, `niftynext50`, `nifty100`, `nifty200`, `nifty500`, `midcap150`, `smallcap250`, and sectors `bank`, `psubank`, `it`, `pharma`, `healthcare`, `auto`, `fmcg`, `metal`, `realty`, `energy`, `oilgas`, `media`, `consumerdurables`; or `--symbols` for the user's own list; `--universe` accepts several (merged). `dividend_yield` is the last 12 months of dividends per share over price, with special dividends flagged. Fields: `pe`, `pb`, `roe`, `net_margin`, `operating_margin`, `debt_to_equity`, `revenue_growth`, `eps_growth`, `dividend_yield`, `fcf_yield`, `market_cap_cr`, `return_1y`, `from_high`, `rsi14`, `price`. Percent fields accept `roe>15` or `roe>15%`.
2. For "dividend stocks" questions use `--preset dividend` (ex-special yield, 5+ years paid in a row, debt/equity ≤ 0.5) and show the dividend-quality columns it adds: recurring yield, payout ratio, free-cash-flow cover, years paid and 5-year dividend growth. Mention a dividend that went ex recently (a new buyer won't receive it). For "find me N" use `--limit N`: with 10 or fewer the preset keeps at most two per sector and names what it left out (say so; `--max-per-sector 0` shows every match). Judge "low debt" on both debt/equity and the debt (₹ cr) column beside it, and quote each stock's sector. Any screen can take `--max-per-sector K`; a ⚠ "Concentrated" line means one sector fills the list.
3. To leave a sector out ("excluding financials", "non-banking"), add `--exclude-sector "Financial Services"` (repeatable, case-insensitive, matched against the sector column; those stocks are not even fetched). If the report says no stock has that sector, fix the spelling to NSE's sector name and rerun. Say which filters you used when the request was vague ("cheap" → for example `pe<15`), so the user can change them.
4. Nifty 500 takes about one and a half to three minutes the first time (about 1,000 requests); a rerun after the day's close (another filter or sort) reads the snapshots saved since the last completed session and takes seconds. The report's last lines give the run time and how many stocks came from saved snapshots; `--no-cache` forces fresh data. Prefer a smaller universe unless the user asks for the whole market.
5. Report how many stocks were evaluated, matched, missing data and failed, and the price date. Missing data is not a pass. Mention every ⚠ earnings check next to the stock, and the bank equity warning when screening banks on ROE or P/B. A "P/E caution" for a demerger parent (Vedanta after its May 2026 demerger) means its trailing EPS still includes businesses it no longer owns, so its P/E looks lower than aggregators show: say so before calling it cheap.
6. Values that would print the same as a filter's threshold get extra decimals (debt/equity 0.0996 under `debt_to_equity<0.1`), so a match never looks like a miss; quote them as printed. "Debt incl. leases" is Yahoo's total debt with lease liabilities (all of TCS's debt is office leases); "borrowings ex leases" leaves them out where the balance sheet gives them separately, so say which one a "debt-free" claim rests on.
7. A match is a filter result, not a recommendation. For banks and finance companies, debt/equity and margins are not meaningful; say so if they are filtered on. Offer to run the `stock` skill on any name the user wants to look at further.

## Chart-pattern scans

Primary route: `python "${CLAUDE_PLUGIN_ROOT}/scripts/public_market.py" scan --symbols SYMBOL.NS ...`.

Use universe for the public NSE equity list. Broker-bound scanning uses trader.py scan with dated master and restriction evidence.

Research and paper workflows only. Missing data is a gap, not an all-clear. Nothing here places an order.
