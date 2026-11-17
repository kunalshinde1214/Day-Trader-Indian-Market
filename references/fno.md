# F&O analytics

`scripts/fno.py` analyses NSE index and stock options and futures. It never places orders.

## Commands

```text
python scripts/fno.py chain NIFTY --summary
python scripts/fno.py chain NIFTY
python scripts/fno.py chain NIFTY --no-compare --no-vix
python scripts/fno.py chain BANKNIFTY --expiry 2026-12-29 --strikes 8
python scripts/fno.py chain RELIANCE --format json --out reports/reliance-chain.json
python scripts/fno.py expiries FINNIFTY
python scripts/fno.py payoff NIFTY --leg SELL:CE:25800:1 --leg BUY:CE:26000:1 --leg SELL:PE:25000:1 --leg BUY:PE:24800:1 --chart reports/condor.svg
python scripts/fno.py payoff NIFTY --leg SELL:CE:25800:1 --leg BUY:CE:26000:1 --summary
python scripts/fno.py payoff NIFTY --leg BUY:FUT::1 --leg SELL:CE:25800:1
python scripts/fno.py chain NIFTY --input trader/nifty-chain.json
python scripts/fno.py chain NIFTY --input trader/nifty-chain.json --previous-date 2026-11-17
```

Common options: `--expiry` (default nearest live expiry), `--date` (bhavcopy trading day; default latest published), `--rate` (risk-free assumption, default 6.5%), `--format markdown|json`, `--out NEW_FILE`. Payoff legs are `SIDE:TYPE:STRIKE:LOTS[:PREMIUM]`; the premium defaults to that contract's traded close. Lots are multiplied by the exchange lot size from the data.

`chain` options:

- `--summary`: a short answer-first block (about ten lines): data date, spot, PCR (OI) and max pain with their day-over-day change, ATM IV and its change, expected move and 68% implied range, India VIX, the biggest call and put OI walls, and the day's net OI change. With `--format json` it prints the same fields as `fno_chain_summary_v1`. The full report stays the default.
- `--no-compare`: skip the previous trading day's bhavcopy (one fewer ~1.5 MB download).
- `--previous-date YYYY-MM-DD`: compare with the bhavcopy on or before this date instead of the trading day before the data date. It must be before the data date. With `--input` the comparison runs only when this is given; otherwise the report says it was skipped.
- `--strikes N` (strikes each side of ATM), `--no-vix`.

`payoff` options: `--leg` (repeatable), `--range` (percent either side of spot, default 8), `--chart NEW.svg`, and `--summary` (net premium, maximum profit and loss, breakevens with the probability of finishing above each, probability of profit, 68% range and today's P&L at spot; JSON schema `fno_payoff_summary_v1`).

## Data

By default the script downloads NSE's public F&O bhavcopy (`nsearchives.nseindia.com/content/fo/BhavCopy_NSE_FO_0_0_0_YYYYMMDD_F_0000.csv.zip`, about 1.5 MB). It is published after the close and lists every contract with OHLC, settlement price, open interest, OI change, volume, underlying price and lot size. The script tries today and walks back over weekends and holidays. Reports are therefore **end of day**.

For live analysis, supply a chain with `--input`, for example read from the user's broker connector:

```json
{
  "schema": "fno_chain_v1",
  "underlying": "NIFTY",
  "as_of": "2026-11-18T11:05:00+05:30",
  "source": "Kite connector option chain",
  "spot": 25410.5,
  "lot_size": 65,
  "expiry": "2026-11-23",
  "rows": [
    {"type": "CE", "strike": 25400, "last": 192.3, "prev_close": 189.2, "oi": 4700000, "oi_change": 120000, "volume": 880000},
    {"type": "PE", "strike": 25400, "last": 170.1, "prev_close": 179.95, "oi": 4400000, "oi_change": 95000, "volume": 760000},
    {"type": "FUT", "last": 25438.0, "prev_close": 25415.3, "oi": 1500000, "oi_change": -20000, "volume": 90000}
  ]
}
```

OI and volume are in units (shares), as NSE reports them; reports convert to contracts using the lot size.

## Method

- **Forward:** taken from put-call parity at traded strikes near the money (median of up to six pairs), which absorbs dividends and carry without guessing them. If no pair traded, the spot is carried at the rate assumption, and the report says so.
- **Implied volatility and Greeks:** Black-76 on that forward (NSE options are European). IV uses traded closes only; untraded contracts carry a stale previous close and get no IV. Greeks are per unit; delta is spot delta, theta is per calendar day, and vega is per one volatility point.
- **PCR:** put OI ÷ call OI, and put volume ÷ call volume, for the chosen expiry.
- **Max pain:** the strike at which the total intrinsic value owed to option holders at expiry is smallest.
- **OI walls:** the three strikes with the most call OI at or above spot, and the most put OI at or below spot.
- **Build-up:** price change and OI change per contract: long build-up (price up, OI up), short build-up (price down, OI up), short covering (price up, OI down), long unwinding (price down, OI down). The report shows signed OI changes in contracts: "OI added" rows (long and short build-up) are positive, "OI reduced" rows (short covering and long unwinding) are negative, and "not classified" holds OI change on contracts that did not trade or have no previous close. Added + reduced + not classified = the net OI change in the summary table. JSON `call_oi_breakdown` / `put_oi_breakdown` carry the same signed numbers in units (shares) with `added_total`, `reduced_total`, `unclassified` and `net`.
- **Expected move:** the ATM straddle price, the IV-implied one-standard-deviation move to expiry (spot × ATM IV × √T), and a 68% implied range: ±1 standard deviation of log price around the lognormal median, F·exp(−σ²T/2 ± σ√T).
- **Day over day:** the previous trading day's bhavcopy is fetched (walking back over weekends and holidays, as for the main file) and the same expiry is re-analysed with the same method. Spot, PCR (OI), max pain, ATM IV and call/put OI are shown for both days with the change. If the previous file is unavailable or does not list the expiry, the report says so and continues. JSON key `day_over_day` has `status` (`ok`, `unavailable` or `skipped`), the dates and `metrics.{name}.previous|current|change`.
- **India VIX:** the close on the chain's trading date, taken from the public Yahoo one-year daily history, with its percentile and range over that history up to the date. If that date has no close (holiday or missing bar), the previous close is shown and the report says the exact date was missing. Shown for index underlyings.
- **Payoff:** P&L at expiry across ±8% of spot (wider if strikes require), breakevens, net premium, maximum profit and loss (unlimited when the payoff still slopes at the top of the range; reported at zero when the extreme happens only if the price falls to zero), and current position Greeks. The table has about 15–20 rows: evenly spaced prices across the range (rounded) plus every strike, breakeven and spot, sorted and deduplicated, each with the expiry P&L, today's (T+0) P&L and a note marking strikes, breakevens and spot.
- **Today (T+0):** the strategy's mark-to-market P&L if the underlying moved to each price now: each option leg is valued with Black-76 at its own implied volatility (from the contract's traded close, else from the leg's premium) and the current time to expiry, with the forward moved in proportion to the underlying and IV held constant; futures legs move with the underlying. It is unavailable when an option leg has no IV. The SVG chart shades profit and loss zones at expiry, marks spot and breakevens, and draws T+0 as a dashed second line with a legend.
- **Probabilities (implied by option prices, not a forecast):** a risk-neutral lognormal on the put-call-parity forward at the ATM IV (the legs' average IV if ATM IV is missing) and the time to expiry: P(S_T > K) = N((ln(F/K) − σ²T/2)/(σ√T)). Reported: the probability of finishing above each breakeven, the probability of profit at expiry (the payoff is linear between strikes, so it is integrated exactly segment by segment, including beyond the plotted range), and the 68% range. A single volatility ignores skew; the figures also ignore costs and closing early.

## Limits

End-of-day data is stale during market hours. A `chain` run downloads two bhavcopies (today's and the previous trading day's) unless `--no-compare` is given. Implied probabilities describe what option prices imply under a simple model, not what will happen. IV and Greeks depend on the rate assumption and on closing prices that may come from thin trades. PCR, max pain and OI walls are descriptions of positioning with weak predictive value. Payoffs exclude brokerage, STT, exchange charges, GST, stamp duty and slippage. Margin for short options must come from the user's broker; see [basket margins](basket-margin-providers.md). Nothing here authorizes an order.
