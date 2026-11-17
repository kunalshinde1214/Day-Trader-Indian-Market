# Stock screener, peers and valuation history

`scripts/screener.py` screens Indian stocks and provides the peer and valuation-history helpers used by `stock_analysis.py`. Research only.

## Commands

```text
python scripts/screener.py --universe nifty50 --where "roe>15" --where "pe<25" --sort roe
python scripts/screener.py --universe it --sort pe --ascending
python scripts/screener.py --universe psubank --where "dividend_yield>3" --format json --out reports/psu-yield.json
python scripts/screener.py --symbols RELIANCE TCS INFY HDFCBANK --sort return_1y
```

`--where` filters combine with AND; repeat it for each condition. Operators are `<`, `<=`, `>`, `>=`, `=`. Percent fields (`roe`, margins, growth, yields, `return_1y`, `from_high`) accept `15`, `15%` or `0.15`. `--sort FIELD` sorts descending unless `--ascending`. `--limit` caps the rows shown (default 25).

## Universes

Constituents come from NSE's published index lists (`nsearchives.nseindia.com/content/indices/ind_<index>list.csv`): `nifty50`, `niftynext50`, `nifty500`, `midcap150`, `smallcap250`, and sectors `bank`, `psubank`, `it`, `pharma`, `healthcare`, `auto`, `fmcg`, `metal`, `realty`, `energy`, `oilgas`, `media`, `consumerdurables`. Lists are fetched on every run, so they follow index changes.

## Metrics

For each stock: price and date, 1-year return, distance from the 52-week high and RSI(14) from Yahoo's daily chart; P/E, P/B, ROE, net and operating margin, debt/equity, revenue and EPS growth, dividend and free-cash-flow yield, and market cap from Yahoo's fundamentals timeseries (the same calculation as `stock_analysis.py`). Companies that report in another currency (for example Infosys in US dollars) are converted to rupees at today's exchange rate.

A stock with no value for a filtered field is listed under "missing data", not counted as a match. A stock whose prices fail to load is listed as failed.

## Peers in stock analysis

`stock_analysis.py` compares a stock with its peers. The peer group is the smallest NSE sectoral index containing the stock (for example Nifty Bank for a bank); if none contains it, it is the stock's Nifty 500 industry, capped at 40. Every peer that loads is used for the group medians, and the largest six by market cap are listed. Index membership is a rough proxy for business similarity, so the report says so. Use `--no-peers` to skip.

## P/E history band

`stock_analysis.py` shows the stock's P/E over up to five years: price on each session divided by the latest annual diluted EPS that had been published by then (each EPS is used from 60 days after its financial year end). It reports the low, the 25th–75th percentile range, the median, the high and where today's P/E sits. It is skipped when financials were currency-converted, because today's exchange rate would distort past values. A P/E below its history can reflect lower expected growth, not only cheapness.

## Data checks

Each stock's trailing EPS is cross-checked against its last four consecutive quarters. If Yahoo's trailing figure differs from their sum by more than 25%, the sum is used and flagged; if one quarter makes trailing EPS differ from four typical quarters by more than half, the P/E is flagged with the typical-quarter P/E; and an annual EPS change above +200% or below −60% is flagged as a possible one-off. Flagged stocks get ⚠ in the table and a note under it. When banks or finance companies appear in a screen filtered on ROE or P/B, a note warns that Yahoo's book equity can differ a lot from the net worth in the bank's own results. The header shows the price date; if Yahoo's latest daily close was missing, the last-trade quote is used and counted in the header.

## Limits

Yahoo's financial data can be stale, restated or missing, especially for smaller companies. Ratios are from reported annual figures and the latest trailing EPS. Nothing here is a recommendation or authorizes an order.
