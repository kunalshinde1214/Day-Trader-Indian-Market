# Stock screener, peers and valuation history

`scripts/screener.py` screens Indian stocks and provides the peer and valuation-history helpers used by `stock_analysis.py`. Research only.

## Commands

```text
python scripts/screener.py --universe nifty50 --where "roe>15" --where "pe<25" --sort roe
python scripts/screener.py --universe it --sort pe --ascending
python scripts/screener.py --universe psubank --where "dividend_yield>3" --format json --out reports/psu-yield.json
python scripts/screener.py --universe nifty100 --where "dividend_yield>2.5" --where "debt_to_equity<0.5" --sort dividend_yield
python scripts/screener.py --universe nifty50 niftynext50 --universe it --sort roe
python scripts/screener.py --symbols RELIANCE TCS INFY HDFCBANK --sort return_1y
```

`--where` filters combine with AND; repeat it for each condition. Operators are `<`, `<=`, `>`, `>=`, `=`. Percent fields (`roe`, margins, growth, yields, `return_1y`, `from_high`) accept `15`, `15%` or `0.15`. `--sort FIELD` sorts descending unless `--ascending`; ties are broken by symbol, so the same data always gives the same order. `--limit` caps the rows shown (default 25). Every field you filter or sort on is added to the table when it is not already a default column.

## Universes

Constituents come from NSE's published index lists (`nsearchives.nseindia.com/content/indices/ind_<index>list.csv`): `nifty50`, `niftynext50`, `nifty100`, `nifty200`, `nifty500`, `midcap150`, `smallcap250`, and sectors `bank`, `psubank`, `it`, `pharma`, `healthcare`, `auto`, `fmcg`, `metal`, `realty`, `energy`, `oilgas`, `media`, `consumerdurables`. Lists are fetched on every run, so they follow index changes. `nifty100` and `nifty200` use NSE's own `ind_nifty100list.csv` and `ind_nifty200list.csv`.

`--universe` takes several names (`--universe nifty50 niftynext50`, or repeat the flag). The lists are merged in order and each stock is screened once. NSE placeholder entries are dropped: symbols starting `DUMMY` with ISINs starting `DUM`, which NSE uses to hold an index slot during a corporate action (seen: `DUMMYHEG` in the Nifty 500 in November 2026). REIT and InvIT units (series `RR`/`IV`) stay in screens but are never used as peers.

## Metrics

For each stock: price and date, 1-year return, distance from the 52-week high, RSI(14) and dividend yield from Yahoo's daily chart; P/E, P/B, ROE, net and operating margin, debt/equity, revenue and EPS growth, free-cash-flow yield, cash dividends paid / market cap, and market cap from Yahoo's fundamentals timeseries (the same calculation as `stock_analysis.py`). Companies that report in another currency (for example Infosys in US dollars) are converted to rupees at today's exchange rate.

**1-year return** has one definition everywhere: the latest close against the close 250 sessions earlier. The screener uses two years of daily bars and stock analysis uses five, through the same function, so both show the same figure for the same stock. A stock with fewer than 251 sessions has no 1-year return.

**Dividend yield** (`dividend_yield`) is the dividends per share with an ex-date in the last 12 months ÷ today's price, from Yahoo's chart dividend events. Their amounts are already on today's share count after splits and bonuses (checked on KOTAKBANK's 2026 split and TRENT's 2026 bonus). It is 0 for a stock with a full year of history and no dividend, and missing for a newer listing.

A payment is flagged as a possible special when, among at least four payments in two years, it is more than three times the median of the others. Two exceptions avoid false flags:

- A payment of similar size (at least two-thirds) came about a year earlier or later, and the company pays fewer than about four times a year. That is a regular large final or interim.
- The flagged payments would be over 80% of two years' dividends.

The flag gives the yield without the special. On the Nifty 200 in November 2026 it flagged TCS (₹57 interim plus special, January 2026; 4.7% trailing, about 2.3% without it) and ABB (₹90, August 2026).

`cash_dividend_yield` keeps the older measure: last financial year's cash dividends paid ÷ today's market cap. It includes specials and can lag a year. Stock analysis shows both, labelled.

**Missing data versus failed to load.** A stock has "missing data" when the feed answered but has no value for a filtered field. It "failed to load" when a request needed for a filter did not succeed (network error, rate limit, cut-off or malformed reply). Requests to the live feed are retried twice with a short backoff (0.5 s, then 1.5 s), and at most four stocks are fetched at once. A screen run twice therefore gives the same result unless the data itself changed. The header counts both groups. The notes list stocks whose prices failed separately from stocks whose financial statements failed. A stock that passes price-only filters but whose statements failed is shown and listed as "shown without financial statements".

**Renamed symbols and demergers.** Symbols in `--symbols` follow `assets/symbol_aliases.json` the same way as stock analysis (see below). For a company that demerged a business (from `assets/demergers.json`), the 1-year return, 52-week high, RSI and dividend yield use only sessions from the record date, and the table notes it.

## Peers in stock analysis

`stock_analysis.py` compares a stock with its peers. The peer group is the smallest NSE sectoral index containing the stock (for example Nifty Bank for a bank). If no sectoral index contains it, the group is the stock's Nifty 500 industry, with companies of comparable size first:

- Size tiers come from NSE's own lists: large = Nifty 100, mid = Midcap 150, otherwise small.
- The adjacent tier is added only when the stock's own tier has fewer than five other companies.
- If the tier lists cannot be fetched, the whole industry is used.

The group is capped at 40. Placeholder entries and REIT/InvIT units are never peers. Every peer that loads is used for the group medians, and the largest six by market cap are listed. Index membership is a rough proxy for business similarity, so the report says so. `--peers SYMBOL ...` replaces the group with companies you choose. Use `--no-peers` to skip.

## Renamed symbols and demergers

`assets/symbol_aliases.json` maps NSE symbols that no longer trade to their successors. Each entry has a one-line reason, the date and a source (for example `ZOMATO` → `ETERNAL`, `MCDOWELL-N` → `UNITDSPR`, `TATAMOTORS` → `TMPV` and `TMCV`). The map is used only when the old symbol fails to load or its history stopped more than 14 days ago, so a symbol NSE later reuses is not redirected.

- **One successor:** the report analyses it and says so at the top.
- **Several successors:** the run stops and lists each with what it is, so you can choose one or analyse each. For example, `TATAMOTORS` became `TMPV` (passenger vehicles and JLR) and `TMCV` (commercial vehicles, now named Tata Motors Ltd). With `--format json` the list is printed as `"status": "SYMBOL_CHANGED"` with `successors`.

Yahoo does not adjust a demerged parent's past prices. Its `TMPV.NS` series includes the pre-demerger Tata Motors prices (₹660.75 on 13 Oct 2025, then ₹395.45 on 14 Oct). Stock analysis handles a parent listed in `assets/demergers.json` (under its current or former symbol) as follows:

- It drops every session before the latest record date inside the history and says "History starts DATE ..." at the top.
- Returns, averages, the 52-week range, support and resistance, the outlook ranges, the dividend yield and the P/E band use only the later sessions.
- Annual EPS for years ending before the record date is left out of the P/E band. The report warns that statements before that date include the demerged business.
- If fewer than 60 sessions remain, it stops and says why.

Separately, any single-session fall of more than 35% with no split recorded is reported as a possible unadjusted corporate action.

## P/E history band

`stock_analysis.py` shows the stock's P/E over up to five years: price on each session divided by the latest annual diluted EPS that had been published by then (each EPS is used from 60 days after its financial year end). It reports the years and sessions actually covered, the low, the 25th–75th percentile range, the median, the high and where today's P/E sits.

Today's value in the band is on the same basis, latest annual EPS. The headline P/E usually uses trailing 12-month EPS, so the summary and the report show the two separately and never compare the trailing figure with the band. For example: "P/E 20.1 (trailing 12 months); on latest annual EPS 22.1 vs its usual 22.1–28.0".

Sessions after a loss year are left out, and an older profitable year is not reused. There is no band when the latest annual EPS is a loss. The band is skipped when financials were currency-converted, because today's exchange rate would distort past values. A P/E below its history can reflect lower expected growth, not only cheapness.

The outlook ranges also state the years and sessions they use: up to two years, less after a demerger or for a recent listing. Bull-case reference levels are listed nearest first going up, bear-case levels nearest first going down, and two levels within 0.5% of each other are merged.

## Data checks

Each stock's trailing EPS is cross-checked against its last four consecutive quarters:

- If Yahoo's trailing figure differs from their sum by more than 25%, the sum is used and flagged.
- If one quarter makes trailing EPS differ from four typical quarters by more than half, the P/E is flagged with the typical-quarter P/E.
- A smaller distortion is mentioned (never corrected) when all of these hold: one quarter is below 40% of the median of the other three, those three agree within 1.6×, and the same quarter a year earlier is in the data and was normal. The last condition keeps seasonal businesses with a weak quarter every year, such as hydro power, from being flagged. On the Nifty 200 in November 2026 this check flagged two stocks (BPCL's loss quarter and Biocon) and none of TCS, Infosys or Reliance.
- An annual EPS change above +200% or below −60% is flagged as a possible one-off.

Flagged stocks get ⚠ in the table and a note under it, as do possible special dividends. When banks or finance companies appear in a screen filtered on ROE or P/B, a note warns that Yahoo's book equity can differ a lot from the net worth in the bank's own results. The header shows the price date; if Yahoo's latest daily close was missing, the last-trade quote is used and counted in the header.

## Limits

Yahoo's financial data can be stale, restated or missing, especially for smaller companies. Ratios are from reported annual figures and the latest trailing EPS. The alias and demerger lists are curated and dated; a change after their `checked` date is not known until an entry is added. Nothing here is a recommendation or authorizes an order.
