# Dhan read-only adapter

Implemented checkpoint: 1 November 2026. Supports NSE/BSE cash and index/stock futures/options through instruments, candles, quotes, funds, positions, order-book reads and single-order margin estimates. There is no order submission, token exchange, streaming, automatic reconciliation or protective-order monitoring.

## Setup and commands

Use your own official Dhan login/session flow and confirm data entitlement locally. Set `DHAN_CLIENT_ID` and `DHAN_ACCESS_TOKEN` in the Python process environment. Never place the token in chat, source files, command arguments or fixtures. No purchase or renewal is performed.

```text
python scripts/trader.py init --broker dhan --workdir trader-dhan
python scripts/broker.py capabilities --broker dhan
python scripts/broker.py instruments --broker dhan --out trader-dhan/master.json
python scripts/broker.py account --broker dhan --out trader-dhan/account-raw.json
python scripts/broker.py quotes --broker dhan --symbols "NSE_EQ:<securityId>" --out trader-dhan/quotes-raw.json
python scripts/broker.py candles --broker dhan --instrument trader-dhan/instrument.json --from 2026-10-30T09:15:00+05:30 --to 2026-10-30T09:35:00+05:30 --out trader-dhan/current-bars.json
python scripts/broker.py candles --broker dhan --instrument trader-dhan/instrument.json --from 2026-09-01T09:15:00+05:30 --to 2026-10-30T00:00:00+05:30 --out trader-dhan/history.json
python scripts/broker.py margin --broker dhan --payload trader-dhan/payload.json --out trader-dhan/margin.json
```

Replace sample IDs and dates with the selected master and intended analysis period. Copy one complete normalized master record into `instrument.json`. For daily candles add `--interval day` and use `YYYY-MM-DD` bounds. For five-minute candles supply offset-bearing timestamps; the adapter converts them to the API's IST clock. Dhan uses these explicit bounds for both current-day and historical bars; `--intraday` is reserved for Upstox's separate endpoint.

Set `config.account_id` to the observed Dhan client ID and supply a sourced, effective-dated Dhan fee card. Initialization leaves fees incomplete and blocks analysis until supplied. The synthetic demo uses fictional fees and needs no account:

```text
python scripts/trader.py demo --broker dhan --workdir demo-dhan-v2
```

All output paths must be new. The common CLI prints save status, not account contents. Single-request failures are not retried; quote polling must respect the provider's current limits.

## Identity, units and source time

The adapter reads the public **detailed** CSV, checks required columns and preserves its HTTP `Last-Modified` time. Missing/future source time blocks import; an old source date remains old. Public downloads carry neither access-token nor client-id headers. The full public-file parser was exercised on 1 November 2026: 143,498 supported rows normalized from a file dated 31 October 2026. This was a public data read, not an authenticated API test or trading scan.

Instrument identity uses both exchange segment and security ID. The same numeric ID on two exchanges is distinct. Exchange mappings are NSE_EQ→NSE, BSE_EQ→BSE, NSE_FNO→NFO and BSE_FNO→BFO. Commodities, currencies and index-only instruments are excluded. Each normalized row retains `broker_instrument` from the master, which is required for candle requests; the adapter never guesses derivative type from a symbol.

Canonical `symbol` values include `@securityId`. Cash uses the underlying symbol; derivatives additionally use actual expiry, strike and option type from the row. This avoids collisions between equity series or similarly named weekly contracts. Original `broker_symbol`, `display_name` and `underlying` remain available for research matching. Use the full canonical symbol consistently in universe, restrictions, quotes, plans, position-value maps and block lists; a bare underlying name is not an exact canonical block-list entry.

Detailed-master `TICK_SIZE` is divided by 100 to rupees; raw tick and conversion metadata are retained. Public records checked included raw option/cash tick 5 and futures tick 20, normalized to 0.05 and 0.20. Strike and market prices remain in rupees. A future's negative strike sentinel becomes `null`, not a negative option strike. Expiry is read from the dated contract field; lot size is read per instrument. Archived expired contracts can remain in the master and fail the core expiry check if selected. Raw ASM/GSM flags are preserved as evidence, but do not create complete surveillance clearance.

Quotes take `SEGMENT:SECURITY_ID`, group requests by segment and report missing instruments. They retain `last_trade_time`, interpreted as `DD/MM/YYYY HH:MM:SS` in IST, as the price timestamp. Missing or malformed time blocks normalization. The documented 1980 sentinel remains stale and cannot become a current price merely because it was fetched recently. This local-time convention still needs confirmation during authenticated acceptance. Select the matching master record and add its canonical `symbol` when populating `context.quote`.

Candle responses are parallel arrays. Unequal lengths, nonfinite prices, inconsistent OHLC, duplicate timestamps or invalid grids block use. Unix-second timestamps become explicit IST starts; no 1980 epoch offset or millisecond scaling is guessed. Intraday ranges are bounded to 90 days. Output uses `[start, end)`; any exact end-boundary candle is excluded and counted. Daily end dates are exclusive. Empty arrays remain explicitly empty. Follow the existing completed-bar and prior-session replay checks in [commands](commands.md).

## Account and margin evidence

The fund-limit response supplies observed `dhanClientId`, which must match the local client ID. Account position/order rows must carry that same identity. The raw snapshot remains `reconciled: false`: balances, open orders and fills still need manual reconciliation into the paper risk ledger.

Dhan previews use `INTRADAY`, `LIMIT`, `DAY`, exact segment/security ID, quantity and account ID. Margin checks first verify the account using fund limits, then call only the non-executing single-order calculator. The returned `totalMargin` binds to a hash of the entire preview, including fields the calculator does not accept. Margin, account, plan and config identities must match. Acknowledgments, available balances and estimates are never interpreted as fills or installed stops.

## Evidence and acceptance still needed

`tests/test_dhan.py` uses fictional fixtures for normal and failure responses, array/unit/time conversion, source-age preservation, missing quotes, redirects, error redaction, identity checks and the full adapter → replay → sizing → paper-ticket path. The shared suite exercises all five demos. No authenticated account or data endpoint was called during development.

Before describing this as account-tested, perform an explicitly requested read-only smoke test with the user's local session, compare normalized master values and quote times with broker-visible values, verify available histories/entitlements, and reconcile margin estimates and charges. Full account/order-status normalization, fee-card release verification and execution remain pending.

Official sources consulted on 1 November 2026: [instrument schema](https://dhanhq.co/docs/v2/instruments/), [detailed public master](https://images.dhan.co/api-data/api-scrip-master-detailed.csv), [historical candles](https://dhanhq.co/docs/v2/historical-data/), [quotes](https://dhanhq.co/docs/v2/market-quote/), [funds and margin](https://dhanhq.co/docs/v2/funds/), [orders](https://dhanhq.co/docs/v2/orders/), [positions](https://dhanhq.co/docs/v2/portfolio/) and [segment/instrument enums](https://dhanhq.co/docs/v2/annexure/). Order documentation informs preview encoding; mutation routes remain rejected by the allowlist.
