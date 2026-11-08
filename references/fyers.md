# FYERS read-only adapter

Checkpoint: 2 November 2026. FYERS is the fifth offline-tested adapter. It supports NSE/BSE cash equities and stock/index futures/options, account reads, and margin estimates. No authenticated endpoint has been tested. A public-master smoke request failed with a network error; public-file verification remains pending. There is no session creation, order submission or continuous monitoring.

## Setup and commands

Complete login/2FA through the official broker flow and set `FYERS_APP_ID`, `FYERS_ACCESS_TOKEN` and `FYERS_ACCOUNT_ID` locally in the Python process environment. App ID is the application identifier, including its suffix; account ID is the user's `fy_id`, not the app ID. Supply the raw access token without the app-ID prefix. Do not put secrets in chat, source files or command arguments. No SDK is installed or executed by this adapter.

```text
python scripts/trader.py init --broker fyers --workdir trader-fyers
python scripts/broker.py capabilities --broker fyers
python scripts/broker.py instruments --broker fyers --out trader-fyers/master.json
python scripts/broker.py account --broker fyers --out trader-fyers/account-raw.json
python scripts/broker.py quotes --broker fyers --symbols "NSE:<exact master symbol>" --out trader-fyers/quotes-raw.json
python scripts/broker.py candles --broker fyers --instrument trader-fyers/instrument.json --from 2026-10-30T09:15:00+05:30 --to 2026-10-30T09:35:00+05:30 --out trader-fyers/current-bars.json
python scripts/broker.py candles --broker fyers --instrument trader-fyers/instrument.json --from 2026-09-01T09:15:00+05:30 --to 2026-10-30T00:00:00+05:30 --out trader-fyers/history.json
python scripts/broker.py margin --broker fyers --payload trader-fyers/payload.json --out trader-fyers/margin.json
python scripts/trader.py demo --broker fyers --workdir demo-fyers-v2
```

Replace examples with the intended instrument and period. Save one complete normalized master row as `instrument.json`. Set `config.account_id` to the observed profile's account ID. Supply a sourced, effective-dated FYERS fee card: initialization deliberately leaves fees incomplete. The offline demo uses invented data and fees. Snapshots never overwrite existing files. Shared [data contracts](commands.md) and [broker migration](brokers.md) apply.

## Instrument identity and source dates

The adapter downloads four public JSON dictionaries: NSE_CM, BSE_CM, NSE_FO and BSE_FO. Every file must have a valid, nonfuture HTTP `Last-Modified`; each normalized record preserves its segment's source date. Individual `lastUpdate` values are retained separately. Downloads send no credentials. If any segment fails, the combined master is not saved.

The normalized `token` is numeric `fyToken`; `symbol` omits the exchange prefix. Internal derivative exchanges are NFO/BFO, while FYERS request tickers use NSE/BSE. Token exchange/segment, file segment, dictionary key and `symTicker` must agree. Tick and strike values remain in rupees. Expiry uses Unix seconds in IST; lot size comes from the contract. Only active cash-equity and supported stock/index derivative records are retained. Exclusions are counted. Raw surveillance text is preserved without declaring clearance. These master assumptions still require public-file acceptance.

References: [JSON master fields and files](https://fyers.in/api-docs/v3/api/market-information/symbol-master/symbol-master-json/), [token structure](https://fyers.in/api-docs/v3/api/appendix/fytoken/), [instrument codes](https://fyers.in/api-docs/v3/api/appendix/instrument-types/), [exchange codes](https://fyers.in/api-docs/v3/api/appendix/exchanges/) and [segment codes](https://fyers.in/api-docs/v3/api/appendix/segments/).

## Candles and quote evidence

Five-minute bounds require explicit offsets and five-minute alignment, with a maximum 100-day interval. Daily bounds are dates with a 366-day maximum and an exclusive end no later than today. Output is `[start, end)`. Intraday requests end at the last completed bar's start; daily requests end just before the exclusive boundary. Continuous-contract data and OI are disabled. Returned timestamps are Unix seconds converted to IST. Duplicate, out-of-range, incomplete, malformed or inconsistent OHLC rows block use; empty history remains explicit. Entitlement and available history depend on the user's broker plan. See [historical data](https://fyers.in/api-docs/v3/api/market-data/historical-data/).

Quotes accept 1–50 distinct full master tickers. The initial quote response establishes symbol/token identity and per-symbol failures. Because its `tt` field is documented only as “today's time,” the adapter then fetches depth once per successful symbol and takes **both price and last-trade time from that same depth record**. Missing time or conflicting identity blocks output. Old trade times remain old. Failed/absent quote entries appear in `missing`; error messages are suppressed. A batch uses one quote call plus up to one depth call per successful symbol, without retry or polling. Use small batches that fit the current plan's limits. See [quotes](https://fyers.in/api-docs/v3/api/market-data/quotes/) and [depth](https://fyers.in/api-docs/v3/api/market-data/market-depth/).

Select the matching normalized quote for `context.quote`. Check broker, exchange, symbol and token against the selected master. Build current membership, restrictions, event/calendar and completed-bar inputs separately.

## Account and margin evidence

Profile `fy_id` must match `FYERS_ACCOUNT_ID`. Account snapshots retain fund rows, positions and order-book rows with `reconciled: false`. Missing/null arrays block; funds must be nonempty. Conflicting account IDs in rows block. The adapter does not turn raw balances or order acknowledgments into a reconciled ledger. References: [profile](https://fyers.in/api-docs/v3/api/account/profile/), [funds](https://fyers.in/api-docs/v3/api/account/funds/), [positions](https://fyers.in/api-docs/v3/api/trading/positions/get-positions/) and [orders](https://fyers.in/api-docs/v3/api/trading/order-book/get-orders/).

Previews encode INTRADAY, LIMIT (`type: 1`), DAY, share/contract quantity and BUY/SELL as 1/-1. AMO, slicing, trigger price and attached-stop/target fields are disabled. Margin estimation submits one order to the non-executing multi-order calculator after verifying account identity. It uses positive `data.margin_total` for the proposed order; `margin_new_order` includes existing positions and `margin_avail` is not the order's requirement. The full original preview hash is retained even though the calculator accepts a subset. Reference: [margin calculator](https://fyers.in/api-docs/v3/api/calculators/multi-order-margin-calculator/).

## Validation and remaining work

`tests/test_fyers.py` has 29 offline tests covering transport limits/redaction, public-master contracts, source age, identity, units, completed candles, depth-derived quote evidence, partial coverage, account checks, margin binding and a full adapter → 10,000-path replay → sizing → risk → paper-ticket journey. The common suite exercises all five broker demos. None of these results establishes authenticated API compatibility or profitable trading.

The endpoint allowlist rejects authentication, logout and order/position mutation before reading credentials. Public and API downloads have byte caps, socket timeouts and elapsed-deadline checks between chunks. No automatic retries occur. Public-master validation, authenticated acceptance, fee verification, account reconciliation and eventual execution controls remain unfinished.
