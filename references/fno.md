# F&O analytics

`scripts/fno.py` analyses NSE index and stock options and futures. It never places orders.

## Commands

```text
python scripts/fno.py chain NIFTY
python scripts/fno.py chain BANKNIFTY --expiry 2026-12-29 --strikes 8
python scripts/fno.py chain RELIANCE --format json --out reports/reliance-chain.json
python scripts/fno.py expiries FINNIFTY
python scripts/fno.py payoff NIFTY --leg SELL:CE:25800:1 --leg BUY:CE:26000:1 --leg SELL:PE:25000:1 --leg BUY:PE:24800:1 --chart reports/condor.svg
python scripts/fno.py payoff NIFTY --leg BUY:FUT::1 --leg SELL:CE:25800:1
python scripts/fno.py chain NIFTY --input trader/nifty-chain.json
```

Common options: `--expiry` (default nearest live expiry), `--date` (bhavcopy trading day; default latest published), `--rate` (risk-free assumption, default 6.5%), `--format markdown|json`, `--out NEW_FILE`. Payoff legs are `SIDE:TYPE:STRIKE:LOTS[:PREMIUM]`; the premium defaults to that contract's traded close. Lots are multiplied by the exchange lot size from the data.

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
- **Build-up:** price change and OI change per contract: long build-up (price up, OI up), short build-up (price down, OI up), short covering (price up, OI down), long unwinding (price down, OI down).
- **Expected move:** the ATM straddle price, and the IV-implied one-standard-deviation move to expiry.
- **India VIX:** latest close and its one-year percentile from the public Yahoo chart, shown for index underlyings.
- **Payoff:** P&L at expiry across ±8% of spot (wider if strikes require), breakevens, net premium, maximum profit and loss (unlimited when the payoff still slopes at the top of the range; reported at zero when the extreme happens only if the price falls to zero), and current position Greeks. The SVG chart shades profit and loss zones and marks spot and breakevens.

## Limits

End-of-day data is stale during market hours. IV and Greeks depend on the rate assumption and on closing prices that may come from thin trades. PCR, max pain and OI walls are descriptions of positioning with weak predictive value. Payoffs exclude brokerage, STT, exchange charges, GST, stamp duty and slippage. Margin for short options must come from the user's broker; see [basket margins](basket-margin-providers.md). Nothing here authorizes an order.
