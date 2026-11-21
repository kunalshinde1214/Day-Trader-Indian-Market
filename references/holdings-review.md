# Holdings review

`scripts/holdings_review.py` reviews a personal Indian equity portfolio: P&L, weights, concentration, sector exposure and, when buy/sell lots are supplied, an estimate of capital-gains tax for one Indian tax year (with the sale costs of selling everything), returns per year against the Nifty 50 over the same dates, and the value of demerged shares the file does not list. Research only: it places no orders and never recommends buying, selling or harvesting gains or losses. Output reports `execution_eligible: false` and `submitted: false`.

## Commands

```bash
python scripts/holdings_review.py --input holdings.json
python scripts/holdings_review.py --input holdings.csv --tradebook tradebook.csv --fy 2026-27
python scripts/holdings_review.py --input tradebook.csv                     # holdings derived from open FIFO lots
python scripts/holdings_review.py --input holdings.json --no-prices          # offline, values at average cost
python scripts/holdings_review.py --input holdings.json --format json --out reports/review.json
python scripts/holdings_review.py --input holdings.json --stcg-rate 0.20 --ltcg-rate 0.125 --ltcg-exemption 125000 --cess-rate 0.04
python scripts/holdings_review.py --input holdings.json --summary            # short answer-first block
python scripts/holdings_review.py --input holdings.json --apply-demergers    # apply published demerger cost splits
python scripts/holdings_review.py --input holdings.json --split RELIANCE     # treat a listed bonus as a split instead
python scripts/holdings_review.py --input holdings.json --bonus SYMBOL       # treat an unlisted Yahoo split event as a bonus
```

`--out` writes a new file and refuses to overwrite one. Errors print `BLOCKED - ...` on stderr and exit 2.

`--summary` prints about a dozen lines instead of the full report: value and P&L (₹ and %) with the best and worst holding on the same line (⚠ beside a holding whose typed price is outside what traded then), then — when such a ⚠ price exists — one what-if line with the combined P&L and return per year with **every** ⚠ price at the middle of its period's range ("⚠ ITC and SBIN typed prices are outside what traded then; at ITC ~₹428 in Jun 2023 (Yahoo understates it before a demerger) and SBIN ~₹961 in Oct 2026: P&L −₹15,469 (−7.1%), return per year −1.7%"; JSON `totals.at_range_midpoint`; holdings whose quantity the lots do not explain are left out; until round 6 it swapped only the prices that drive the tax, so ITC's was missing), day change since the last close, return per year (XIRR) on the dated holdings against the Nifty 50 on the same dates, largest-holding weight with the concentration in plain words ("like 3.3 equal-sized holdings"; the top-5 weight only when there are more than five holdings) and the top sector on the same line, the "if you sold everything today" tax (one figure, or a range naming the undated holdings) with the sale costs and the unrealised gain on open lots by term today ("· all ₹22,404 long-term", or "₹X long-term, ₹Y short-term", naming undated holdings as term unknown; JSON `tax.unrealised`) on one line, then short sub-bullets: the tax what-if, which swaps only the ⚠ prices that change the tax on their own and says so ("⚠ At SBIN ~₹961 in Oct 2026 (the only ⚠ price that changes the tax): tax ₹230 to ₹1,451"; JSON `hypothetical_sell_all.at_range_midpoint`); when the published demerger cost splits change the tax, the split figure leads (the cost the law uses: s.49(2C)/(2D) of the Income-tax Act 1961, carried into the Income-tax Act 2025) with a sub-bullet giving the full-typed-cost figure and the assumption that the typed price is the pre-demerger purchase price (the typed-price midpoint is then on the split basis too), and the net long-term loss left over (JSON `hypothetical_sell_all.unused_long_term_loss_low`/`unused_long_term_loss` on the typed cost, `with_demerger_cost_split.unused_long_term_loss_low`/`unused_long_term_loss` with the split; a range when undated holdings could be either term) — on the split basis first when the split changes it, with the typed-cost range in brackets ("₹0 on the demerger cost split the law applies to ITC and RELIANCE (₹8,588 to ₹14,399 on the full typed cost, which the law does not allow)") as information — it cannot reduce short-term gains and can be carried forward against long-term gains for up to 8 years if the return is filed on time (not modelled). A "Demerged shares" line (with what selling them too would do to the tax and sale costs) when a demerger applies (whether the resulting company's shares, such as JIOFIN or ITCHOTELS, are in the holdings; if not, how many to expect, what they are worth now, "if you still hold them: +₹X → total ₹Y, P&L Z" and a prompt to check the demat account), a "Bonus applied" ("Split applied", "Bonus/split applied") line for every holding whose shares and average cost were adjusted from its purchase date ("HDFCBANK 100 → 200 (1:1, Aug 2025)"; JSON `holdings[].corporate_action_applied`) — information, not a ⚠ — and a count of ⚠ items grouped as buy/sell price, bonus/split, demerger, stale price and missing price, where every flagged typed price counts ("SBIN ×2") and a fully sold symbol with a flagged price is listed too (JSON `sold_price_flags`). Round 7 added the bonus and term items within the line budget (at most one line more). Round 8: a ₹0 sell-everything estimate says why in brackets ("₹17,144 short-term loss set off against long-term gains; ₹93,356 net long-term within the ₹1.25 lakh exemption", or "no net gain to tax: losses only"), and a listed bonus or split dated before a holding's first purchase is named as not applied — the typed quantity and price are already after it — on the bonus line, or on its own "Bonus/split not applied" line when nothing was applied ("not applied: WIPRO 1:1 bonus Dec 2024 predates purchase (Dec 2025)"; JSON `holdings[].corporate_action_before_purchase`), still within that budget. With `--format json` it returns `{"schema": "holdings_summary_v1", "summary": ..., "flags": {...}}`. Lead with the summary when the user asks "how is my portfolio doing"; offer the full report for the details behind each ⚠.

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

- **Prices**: Yahoo Finance public chart for `SYMBOL.NS` (or the `.BO` symbol given). While the NSE session is open (09:15–15:30 IST on a weekday) and Yahoo's newest daily bar is today's, the price is labelled "live price HH:MM IST (session in progress); last close DATE" with Yahoo's quote time; otherwise "closing price DATE". Day change is the latest price against the previous close. A symbol whose fetch fails is tried once more after the others (a second failure says "failed twice"; demerged shares likewise) and is otherwise listed as "price unavailable" and never estimated; totals then say "priced holdings only", and the summary's P&L, return (XIRR) and tax lines each say "excludes SBIN (price unavailable), not a full figure" (JSON `totals.unpriced_symbols`, `returns.unpriced`, `hypothetical_sell_all.unpriced_symbols`), so a ₹0 tax on the priced holdings is never shown as complete. Prices older than seven days and splits/bonuses recorded in the last five years are flagged, so an average cost from before a bonus or split is not mistaken for a loss.
- **Per holding**: quantity, average cost, last price, invested, current value, unrealised P&L (₹ and %), weight and sector.
- **Weights and concentration**: weights use current value; a holding without a price is weighted at average cost and marked. Reported: number of holdings, largest-holding weight, top-5 weight (only above five holdings), a plain reading (below 0.10 diversified, 0.10–0.18 moderately concentrated, above 0.18 highly concentrated, from the Herfindahl index, the sum of squared weights, shown in the Concentration section) and the equivalent number of equal-sized holdings.
- **Returns vs Nifty 50**: for symbols whose dated lots (or `bought` date) explain the whole holding, the money-weighted annual return (XIRR) on the buy/sell dates and amounts ending at today's value, per holding and for all dated holdings together, shown only when the first buy is at least a year old (otherwise the simple return and the days held). The Nifty 50 (`^NSEI`) figures put the same rupees into the index on the same dates (first close on or after each date). Both are price returns without dividends. Undated holdings are listed as excluded; month-only dates make the figures approximate. When demerged shares are valued (below), the parent's return including them is shown too.
- **Sectors**: the `Industry` column of the NSE Nifty 500 list, `https://nsearchives.nseindia.com/content/indices/ind_nifty500list.csv`. Symbols not in the list are grouped as "Other / not in Nifty 500".
- **Tax lots**: FIFO per symbol, ordered by trade date and execution time (buys before sells on the same date when no time is given). A sell with no earlier buy in the file is reported as unmatched and its gain is not computed. Same-day buy and sell of one symbol is flagged as possibly intraday.
- **Holding period**: listed equity is long-term when the sale date is **more than 12 calendar months** after the purchase date. Bought 10 Jan 2025: sold 10 Jan 2026 (365 days, exactly 12 months) is short-term; sold 11 Jan 2026 is long-term. Across a leap day, 366 days can still be exactly 12 months (1 Jun 2023 to 1 Jun 2024), which stays short-term. A purchase on 29 Feb counts its anniversary as 28 Feb.
- **Tax year**: April to March, chosen with `--fy 2026-27`; the default is the one containing today in IST. Realised gains are sales dated 1 April to 31 March inclusive. From 1 April 2026 the Income-tax Act 2025 calls it the "tax year" (labelled "tax year 2026-27"); earlier years keep "FY 2025-26" and the 1961 Act's sections.
- **Set-off within this report**: short-term losses offset short-term gains first, then long-term gains; long-term losses offset only long-term gains. Losses left over are shown but not carried forward.
- **Unrealised**: open lots valued at the last price, split short/long-term as of today, with the date each lot becomes long-term. When the selected year is the current one, a hypothetical line shows the estimated tax if every priced open lot were sold at its last price. This is arithmetic for information, not a suggestion to sell. It is followed by:
  - **Sale costs** of selling every priced holding as delivery: STT 0.1% of the sale value and a DP charge per scrip plus GST (`position_size.DELIVERY`), with exchange, SEBI and GST from `assets/charges.example.json`, and the cash left after tax and costs. The tax itself does not deduct these costs (STT is not deductible; DP and exchange charges are, which the estimate ignores).
  - **Demerger cost splits**: the figure with the published splits applied (as `--apply-demergers` would), printed only when it differs; otherwise one line says the splits do not change it. When the splits change the long-term loss left over, both amounts follow (JSON `with_demerger_cost_split.changes_loss`). The full report keeps the typed-cost figure as its headline (the user may already have adjusted the cost); the summary leads with the split figure.
  - **Demerged shares included**: the figure if the unlisted demerged shares are still held, as tax lots at the apportioned cost and the parent's original purchase dates (JSON `hypothetical_sell_all.with_demerged_shares`).
  - **Typed-price midpoint**: when a ⚠ buy/sell price outside its period's traded range moves the figure on its own, the figure with that price replaced by the middle of the range (JSON `at_range_midpoint`), noting when the range is understated because of a later demerger. The value/P&L, return per year and per-holding P&L at the midpoint swap every ⚠ price, not only the ones that move the tax (JSON `totals.at_range_midpoint`; a row in the full report's summary table).

## Rates (defaults — verify current rates)

| Item | Default | Option |
| --- | --- | --- |
| Short-term capital gains on listed equity with STT: s.196 of the Income-tax Act 2025 (s.111A of the 1961 Act before 1 Apr 2026) | 20% | `--stcg-rate` |
| Long-term capital gains: s.198 (s.112A before 1 Apr 2026) | 12.5% above the exemption | `--ltcg-rate` |
| Annual LTCG exemption | ₹1,25,000 per tax year | `--ltcg-exemption` |
| Health and education cess | 4% of the tax, shown as its own line | `--cess-rate` |

These rates apply to transfers on or after 23 July 2024 (Finance (No. 2) Act 2024) and were carried into the Income-tax Act 2025, in force from 1 April 2026, unchanged by the Finance Act 2026 (checked 18 Nov 2026 against the incometaxindia.gov.in section pages for s.196 and s.198 and the CBDT commencement notice; verify before relying on them). For a tax year from 2026-27 the report cites the 2025 Act; for earlier years the 1961 Act. Sales before 23 July 2024 in the selected year trigger a warning (15% STCG; 10% LTCG above ₹1,00,000 applied then). The report also shows "LTCG exemption remaining this tax year" as information; the exemption is shared with other s.198 (s.112A) gains outside the file, such as equity mutual funds.

## Bonuses, splits and missing purchase dates

A holding may carry `"bought": "YYYY-MM-DD"` or just `"YYYY-MM"` (the 1st of the month is assumed and flagged). A dated holding with no lots becomes one tax lot.

Yahoo's split events from the last five years are applied inside FIFO on the ex-date, before that day's trades. Yahoo records a 1:1 bonus as a 2-for-1 split and does not say which it was, so each event is classified:

- **Listed bonus → bonus, automatically.** `assets/bonuses.json` is a curated list of well-known NSE bonus issues since 2022, each with ratio (Indian terms: new shares for every N held), record date, ex-date, allotment date when the company published it, source URLs (company or exchange filings) and the date checked. A Yahoo event for the same symbol within 4 days of the ex-date or record date, with the matching share multiplier ((new + held) ÷ held, e.g. 2 for 1:1, 4/3 for 1:3), is treated as that bonus and the note says so.
- **Bonus treatment:** a new lot of (multiplier − 1) × the open quantity at nil cost, dated from the **allotment date** (from which Indian tax counts the holding period of bonus shares); when the list has no allotment date, from the record date with a note that the long-term date may be a few days early.
- **Split plus bonus on one record date** (Bajaj Finance Jun 2025: 1:2 split then 4:1 bonus; Bajaj Finserv Sep 2022: 1:5 split then 1:1 bonus): the entry's `split_before_bonus` makes it a split (quantity up, cost per share down, date kept) followed by the bonus lot. Yahoo has recorded only the split part for both while adjusting its prices by the full factor; either form matches.
- **Anything else → split** (open lots multiplied by the ratio, cost divided, purchase date kept), with a note naming `--bonus SYMBOL`.
- `--split SYMBOL` forces split treatment even for a listed bonus; `--bonus SYMBOL` forces bonus treatment (dated from the listed allotment date if any, else the ex-date). Giving one symbol to both blocks.

Values and P&L are the same either way; only tax lots differ. If the list cannot be read, events fall back to splits and `errors.bonuses` says so. JSON `bonus_reference` gives the entry count, check date and forced symbols. To add an entry, cite the company's record-date or allotment letter (and the NSE F&O circular for the ex-date); never estimate dates; `tests/test_bonuses.py` refuses entries without https sources.

If a holding's quantity equals its lots before the event, it is converted to today's units and a note says so. When `--apply-demergers` also changes the same holding, one note lists the events in date order with the shares and total cost after each (for RELIANCE 30 at ₹2,400 bought Jan 2023: the Jul 2023 JIOFIN cost split takes ₹72,000 to ₹68,630, then the Oct 2024 1:1 bonus takes 30 shares to 60, giving ₹1,143.84 a share). A holding with no date that had an event gets a ⚠ in the table and an explanation under it (broker exports are normally already adjusted). Mergers and rights issues are not applied.

## Buy-price check

Every dated buy or sell (lots, tradebook rows, or a holding's `bought` date) is compared with what the stock traded at then: the Yahoo daily low–high on that day (the days within 3 of it when the date had no trading), or over the whole month for a month-only date. Yahoo's bars are in today's share units, so each bar is scaled back by every later split or bonus to the prices of the time. A price more than 3% outside that range gets a ⚠ ("SBIN: ₹600 (bought) is outside the ₹937–984 range traded in Oct 2026 — check the price or date") in the holdings table, under "Check before trusting these numbers", in the summary's ⚠ count and tax line, and in JSON `price_warning` / `price_checks` (with `price_checks_ok` and `price_checks_not_checked` counts). The user's numbers are never changed.

- If the price fits the range in today's units instead, the warning adds that the quantity and price may already be adjusted, in which case the review has adjusted them twice.
- Yahoo also scales down prices from before some demergers (RELIANCE before Jul 2023, ITC before Jan 2025, SIEMENS before Apr 2025; not TMPV), with no event to undo it. For a period before a listed demerger, only a price **below** the range is flagged, and the warning says the real low was at or above the figure shown.
- Dates before the five-year price history, or with no bars nearby, are not checked (counted in `price_checks_not_checked`).

When some priced holdings have no purchase date, the "if you sold everything today" tax is a range: the low end treats their gains as long-term and losses as short-term, the high end the reverse.

## Demergers

Yahoo reports no demerger events, so `assets/demergers.json` ships a short curated list of NSE demergers from 2023 onwards: parent symbol (plus old symbols, such as `TATAMOTORS` for `TMPV`), resulting company and NSE symbol, record date, entitlement (new shares per parent shares) and, only where the company published it, the split of the cost of acquisition. Every entry carries its source URLs (company or exchange letters where available) and the date it was checked. Unverified fields are `null`, never estimated.

| Parent | Resulting company (symbol) | Record date | Entitlement | Cost split parent / new |
| --- | --- | --- | --- | --- |
| RELIANCE | Jio Financial Services (JIOFIN) | 2023-07-20 | 1 for 1 | 95.32% / 4.68% |
| RAYMOND | Raymond Lifestyle (RAYMONDLSL) | 2024-07-11 | 4 for 5 | not verified |
| ITC | ITC Hotels (ITCHOTELS) | 2025-01-06 | 1 for 10 | 86.49% / 13.51% |
| SIEMENS | Siemens Energy India (ENRIN) | 2025-04-07 | 1 for 1 | 76.18% / 23.82% |
| RAYMOND | Raymond Realty (RAYMONDREL) | 2025-05-14 | 1 for 1 | 60.92% / 39.08% |
| ABFRL | Aditya Birla Lifestyle Brands (ABLBL) | 2025-05-22 | 1 for 1 | 75.68% / 24.32% |
| TMPV (was TATAMOTORS) | Tata Motors, the CV business (TMCV) | 2025-10-14 | 1 for 1 | 68.85% / 31.15% |

For a holding whose symbol is a parent:

- **Dated before the record date** (lots, a tradebook or `bought`): only shares held at the end of the day before the record date count, since a purchase on the record date is not entitled. The holding gets a ⚠ saying how many new shares the user should also hold (fractions are paid in cash) and whether the new symbol is already in the file; if not, it asks the user to check their demat account, since value and P&L exclude those shares (the summary's "Demerged shares" line says the same; JSON `demergers[].resulting_in_holdings`). With a known split it spells out the cost: for 1,000 ITC at ₹400, ₹3,45,960 (₹345.96 a share) stays with ITC and ₹54,040 (₹540.40 each) goes to the 100 ITC Hotels shares. Without one it says the company publishes the split and the cost shown may be overstated. A month-only `bought` in the record month is caveated.
- **Undated**: the same ⚠, worded "if you bought before DATE".
- **Dated on or after the record date**: nothing.
- **Realised sales** of shares bought before a record date and sold after it get a note that the gain uses the full original cost.

**Valuing shares the file does not list.** When the new company's shares are not in the file, the review fetches their price and shows them under "Demerged shares not in your list": shares expected (adjusted for any later Yahoo split of the new company), price with its time, value and apportioned cost, then "If you still hold them: +₹X → total ₹Y, P&L Z". They are never added to the main totals, since the user may have sold them. For the tax estimate they become lots at the apportioned cost with the parent's purchase dates (only for dated holdings with a known split).

Nothing is changed by default, because the user may already hold the new shares separately or the broker may already have adjusted the cost. `--apply-demergers` applies a published split as a FIFO event on the record date: open lots keep their quantity and purchase date and their cost is multiplied by the parent's percentage, so realised gains and the tax estimate use the apportioned cost. It applies only to dated lots, only where the split is known, and to a holdings-file average cost only when the lots match the holding quantity (otherwise a note says it was not applied). The new shares are not added; the user adds them, at the cost shown, if they hold them. Successive demergers of one parent (Raymond) compound.

To add an entry, take the record date, ratio and cost split from the company's exchange letter ("apportionment of cost of acquisition"), cite it, and set `checked`; `tests/test_demergers.py` refuses entries without a source.

## Not modelled

- Surcharge.
- Grandfathering for shares bought before 1 Feb 2018 (flagged when such lots appear).
- Merger and rights-issue cost adjustments. Demergers outside `assets/demergers.json` are not detected.
- Set-off of capital losses against other income, and carry-forward of losses.
- Intraday and F&O trades (business income).
- Brokerage, exchange and DP charges as transfer expenses deducted from the gain (STT is not deductible); sale costs are shown separately.
- Basic exemption limit, section 87A rebate, non-resident treatment and TDS.

## Limits

- Public prices may be delayed or revised; they are research data, not broker valuations.
- Holdings files carry no purchase dates, so the tax section needs lots or a tradebook. A tradebook covers only the period it was exported for; buys before it, transfers in, IPO allotments, bonus, split and demerger shares are missing unless added as lots. Quantity mismatches between lots and holdings are listed.
- Tax figures are estimates for information. Check current rules and your own position with a tax professional; use [web research](web-research.md) for rate changes and anything listed as not modelled.
