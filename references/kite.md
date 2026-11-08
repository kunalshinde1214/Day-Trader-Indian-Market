# First broker: Zerodha Kite

This adapter is read-only. It allows instrument, quote, historical, account, position and order-book reads, plus the non-executing margin-estimate POST. Order placement/modification/cancellation and token-generation endpoints are rejected before credentials are accessed. Redirects are refused so authorization headers cannot be forwarded. Failures are bounded; no retry loop or background service is started.

The user selected Kite on 31 October 2026. No live authentication has been performed or tested in this project.

## Check data access first

The user is unsure whether data access is enabled. Sign in to the [Kite developer console](https://developers.kite.trade/) and check the application's type and subscription status. As checked on 31 October 2026, the free Personal API excludes live and historical market data; paid Kite Connect includes both and is listed at ₹500/month per API key. See [Zerodha's plan explanation](https://support.zerodha.com/category/trading-and-markets/general-kite/kite-api/articles/historical-data-and-live-market-data-payment-plan). Having the normal Kite trading app does not establish a paid API subscription.

Record only the plan name and active/inactive status here or in chat; never copy credentials. If no developer app or active data plan exists, keep using offline fixtures while the user decides on setup. Do not purchase, renew or upgrade a subscription automatically.

## Local session setup

Use your own Kite application and official login/2FA flow. Configure `KITE_API_KEY` and `KITE_ACCESS_TOKEN` in the local environment of the Python process. Do not put values in chat, checked-in files, command arguments or test fixtures. The adapter needs no API secret or TOTP seed. It does not generate or renew access tokens; an expired session must be renewed locally. Do not dump environment variables to diagnose a failure.

Kite documents its [login/session flow](https://kite.trade/docs/connect/v3/user/). Check your market-data and historical entitlement in the broker account; trading access alone is not evidence of data access. The adapter performs no purchase or subscription change.

## Read snapshots

From the skill directory, with a configured local session:

```powershell
python scripts/kite_readonly.py instruments --out trader/instruments-kite.json
python scripts/kite_readonly.py quotes --symbols NSE:INFY NSE:RELIANCE --out trader/quotes-kite.json
python scripts/kite_readonly.py account --out trader/account-kite.json
```

These symbols merely illustrate API syntax. No watchlist or investment recommendation is implied. Each command requires a new output filename. Do not send these account snapshots to third-party services.

Select the exact master record and save it as `instrument.json`. For historical bars, supply that record's token, symbol, exchange and offset-bearing dates:

```text
python scripts/kite_readonly.py candles --token <master-token> --symbol <master-symbol> --exchange NSE --from <start-ISO-with-offset> --to <end-ISO-with-offset> --out trader/history.json
```

The default interval is `5minute`; `--interval day` is available for collecting daily inputs. A daily file is not a valid intraday replay input. Requests are intentionally single-range and bounded; split ranges according to the current broker limits rather than blind retries. Preserve source timestamps and instrument identity. Validate token-symbol correspondence using the master before fetching; endpoint candle data itself does not return a trustworthy symbol mapping.

Kite provides the [instrument/quote formats](https://kite.trade/docs/connect/v3/market-quotes/) and [historical candle API](https://kite.trade/docs/connect/v3/historical/). The adapter normalizes master/candle rows; quote and account files remain raw source snapshots. A separate ingestion/reconciliation step is still needed to populate `context.json`, `universe.json` and `candles.json` as specified in [commands](commands.md). The adapter does not assume an empty account, invent holiday status, or clear surveillance lists.

For a derivative candidate emitted by `size`:

```powershell
python scripts/kite_readonly.py margin --payload trader/payload.json --out trader/margin-kite.json
```

Insert that dated record under `context.margin` and rerun risk promptly with current account evidence. Account and margin commands also read the profile to bind the snapshot to its `user_id`; configure the same ID in `config.account_id`. Normalized masters/histories carry `broker: "kite"`. See [schema-v2 migration](brokers.md) for older snapshots. The request is a [margin calculation](https://kite.trade/docs/connect/v3/margins/), not an order. Margin may depend on existing positions/open orders and must be refreshed if they change. No fixed SPAN/ELM percentage or scaled one-lot estimate substitutes for the exact quote.

## Before future live development

The present executable intentionally has no placement method. Further work must establish the actual broker/exchange classification, static-IP/session requirements, generic versus registered algo tagging, approval mechanism, safe protective-order lifecycle, partial fills and reconnection behavior. Kite's user `tag` field is not a substitute for an exchange algo ID. A simulated stop is not an installed broker stop; GTT/cover-order availability must be checked per product. Read current [order semantics](https://kite.trade/docs/connect/v3/orders/) rather than assuming Alpaca bracket behavior.
