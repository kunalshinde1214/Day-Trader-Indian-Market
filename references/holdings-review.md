# Holdings review

`scripts/holdings_review.py` reviews a personal Indian equity portfolio: P&L, weights, concentration, sector exposure and, when buy/sell lots are supplied, an estimate of capital-gains tax for one Indian financial year. Research only: it places no orders and never recommends buying, selling or harvesting gains or losses. Output reports `execution_eligible: false` and `submitted: false`.

## Commands

```bash
python scripts/holdings_review.py --input holdings.json
python scripts/holdings_review.py --input holdings.csv --tradebook tradebook.csv --fy 2026-27
python scripts/holdings_review.py --input tradebook.csv                     # holdings derived from open FIFO lots
python scripts/holdings_review.py --input holdings.json --no-prices          # offline, values at average cost
python scripts/holdings_review.py --input holdings.json --format json --out reports/review.json
python scripts/holdings_review.py --input holdings.json --stcg-rate 0.20 --ltcg-rate 0.125 --ltcg-exemption 125000 --cess-rate 0.04
```

`--out` writes a new file and refuses to overwrite one. Errors print `BLOCKED - ...` on stderr and exit 2.

## Input formats

The format is detected from the content: JSON with `"schema": "holdings_v1"`, or CSV headers. Header matching ignores case, surrounding spaces, a UTF-8 BOM and extra columns. Blank rows are skipped; zero-quantity holdings are skipped with a note; negative quantities or prices block.

**holdings_v1 JSON** (build this from a broker connector such as Kite or Upstox holdings, or from pasted data):

```json
{"schema": "holdings_v1", "as_of": "2026-11-17T09:00:00+05:30", "source": "Kite holdings via connector",
 "holdings": [{"symbol": "RELIANCE", "quantity": 10, "average_price": 1200.5},
              {"symbol": "TCS", "quantity": 5, "average_price": 3400}],
 "lots": [{"symbol": "TCS", "quantity": 5, "price": 3400, "date": "2025-01-10", "side": "BUY"}]}
```

`lots` is optional. `side` is `BUY` or `SELL` (default `BUY`). Symbols may be `RELIANCE`, `RELIANCE.NS` or `RELIANCE.BO`; duplicates are merged at a quantity-weighted average cost.

**Zerodha Console holdings CSV** (needs `Instrument`, `Qty.`, `Avg. cost`; other columns such as `LTP` are ignored):

```csv
Instrument,Qty.,Avg. cost,LTP,Cur. val,P&L,Net chg.,Day chg.
RELIANCE,10,1200.00,1500.00,15000.00,3000.00,25.00,1.00
```

**Zerodha tradebook CSV** (needs `symbol`, `trade_date`, `segment`, `trade_type`, `quantity`, `price`; only `segment` = `EQ` rows count, others are counted and ignored):

```csv
symbol,isin,trade_date,exchange,segment,series,trade_type,auction,quantity,price,trade_id,order_id,order_execution_time
INFY,INE009A01021,2024-11-01,NSE,EQ,EQ,buy,false,10,1000,1,1,2024-11-01T10:00:00
```

Pass a tradebook either as `--input` (holdings are derived from open lots) or as `--tradebook` next to a holdings file (lots are used for tax and reconciled against holdings quantities).

## Method

- **Prices**: Yahoo Finance public daily chart for `SYMBOL.NS` (or the `.BO` symbol given). The latest daily close and its date are shown; during market hours that bar is still forming. Day change is the latest bar against the previous close. A symbol whose fetch fails is listed as "price unavailable" and is never estimated; totals then say "priced holdings only". Prices older than seven days and splits/bonuses recorded in the last five years are flagged, so an average cost from before a bonus or split is not mistaken for a loss.
- **Per holding**: quantity, average cost, last price, invested, current value, unrealised P&L (₹ and %), weight and sector.
- **Weights and concentration**: weights use current value; a holding without a price is weighted at average cost and marked. Reported: number of holdings, largest-holding weight, top-5 weight, Herfindahl index (sum of squared weights) with a plain reading (below 0.10 diversified, 0.10–0.18 moderately concentrated, above 0.18 highly concentrated) and the equivalent number of equal-sized holdings.
- **Sectors**: the `Industry` column of the NSE Nifty 500 list, `https://nsearchives.nseindia.com/content/indices/ind_nifty500list.csv`. Symbols not in the list are grouped as "Other / not in Nifty 500".
- **Tax lots**: FIFO per symbol, ordered by trade date and execution time (buys before sells on the same date when no time is given). A sell with no earlier buy in the file is reported as unmatched and its gain is not computed. Same-day buy and sell of one symbol is flagged as possibly intraday.
- **Holding period**: listed equity is long-term when the sale date is **more than 12 calendar months** after the purchase date. Bought 10 Jan 2025: sold 10 Jan 2026 (365 days, exactly 12 months) is short-term; sold 11 Jan 2026 is long-term. Across a leap day, 366 days can still be exactly 12 months (1 Jun 2023 to 1 Jun 2024), which stays short-term. A purchase on 29 Feb counts its anniversary as 28 Feb.
- **Financial year**: April to March, chosen with `--fy 2026-27`; the default is the FY containing today in IST. Realised gains are sales dated 1 April to 31 March inclusive.
- **Set-off within this report**: short-term losses offset short-term gains first, then long-term gains; long-term losses offset only long-term gains. Losses left over are shown but not carried forward.
- **Unrealised**: open lots valued at the last price, split short/long-term as of today, with the date each lot becomes long-term. When the selected FY is the current one, a hypothetical line shows the estimated FY tax if every priced open lot were sold at its last price. This is arithmetic for information, not a suggestion to sell.

## Rates (defaults — verify current rates)

| Item | Default | Option |
| --- | --- | --- |
| Short-term capital gains, s.111A | 20% | `--stcg-rate` |
| Long-term capital gains, s.112A | 12.5% above the exemption | `--ltcg-rate` |
| Annual LTCG exemption | ₹1,25,000 per FY | `--ltcg-exemption` |
| Health and education cess | 4% of the tax, shown as its own line | `--cess-rate` |

These defaults apply to transfers on or after 23 July 2024 under the Finance (No. 2) Act 2024. Sales before that date in the selected FY trigger a warning (15% STCG; 10% LTCG above ₹1,00,000 applied then). The report also shows "LTCG exemption remaining this FY" as information; the exemption is shared with other s.112A gains outside the file, such as equity mutual funds.

## Not modelled

- Surcharge.
- Grandfathering for shares bought before 1 Feb 2018 (flagged when such lots appear).
- Bonus, split, merger and demerger cost adjustments.
- Set-off of capital losses against other income, and carry-forward of losses.
- Intraday and F&O trades (business income).
- Brokerage and exchange charges as transfer expenses (STT is not deductible).
- Basic exemption limit, section 87A rebate, non-resident treatment and TDS.

## Limits

- Public prices may be delayed or revised; they are research data, not broker valuations.
- Holdings files carry no purchase dates, so the tax section needs lots or a tradebook. A tradebook covers only the period it was exported for; buys before it, transfers in, IPO allotments, bonus and split shares are missing unless added as lots. Quantity mismatches between lots and holdings are listed.
- Tax figures are estimates for information. Check current rules and your own position with a tax professional; use [web research](web-research.md) for rate changes and anything listed as not modelled.
