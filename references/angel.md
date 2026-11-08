# Angel One SmartAPI read-only adapter

Development checkpoint: 2 November 2026. Instruments, candles, quotes, profile, funds, positions, order-book reads and single-order margin estimates are implemented and tested with fictional fixtures. No authenticated API test or successful public-master smoke test has been completed. No order placement, session creation, streaming or continuous monitoring is implemented.

## Local setup

Complete login and 2FA using your own official broker flow. Set `ANGEL_API_KEY`, `ANGEL_ACCESS_TOKEN`, `ANGEL_CLIENT_CODE`, `ANGEL_LOCAL_IP`, `ANGEL_PUBLIC_IP` and `ANGEL_MAC_ADDRESS` in the Python process environment. IP settings must be valid addresses and the MAC must use six colon-separated hexadecimal octets. Supply the device/network values required by the broker; the adapter neither invents them nor calls an external IP-discovery service. Never paste secrets into chat, commands, source files or fixtures.

```text
python scripts/trader.py init --broker angel --workdir trader-angel
python scripts/broker.py capabilities --broker angel
python scripts/broker.py instruments --broker angel --out trader-angel/master.json
python scripts/broker.py account --broker angel --out trader-angel/account-raw.json
python scripts/broker.py quotes --broker angel --symbols "NSE:<token>" --out trader-angel/quotes-raw.json
python scripts/broker.py candles --broker angel --instrument trader-angel/instrument.json --from 2026-10-30T09:15:00+05:30 --to 2026-10-30T09:35:00+05:30 --out trader-angel/current-bars.json
python scripts/broker.py candles --broker angel --instrument trader-angel/instrument.json --from 2026-09-01T09:15:00+05:30 --to 2026-10-30T00:00:00+05:30 --out trader-angel/history.json
python scripts/broker.py margin --broker angel --payload trader-angel/payload.json --out trader-angel/margin.json
python scripts/trader.py demo --broker angel --workdir demo-angel-v2
```

Replace example IDs and dates with the selected master and intended analysis period. Save one complete normalized master record as `instrument.json`. Set `config.account_id` to the observed profile client code. Initialization leaves fees incomplete; supply a sourced, effective-dated Angel tariff before analysis. The demo uses fictional rates, a historical clock and no network. All output paths must be new. See the shared [data contracts](commands.md).

## Normalization

The public JSON master carries no account/device headers. Its HTTP `Last-Modified` supplies the source date; missing or future source time blocks import, while an old date remains old. Downloads are capped at 64 MiB, use a 30-second socket timeout and check a 60-second elapsed deadline between reads. A pending read can run until the socket timeout. Redirects are rejected and failures are not retried.

Supported exchanges are NSE/BSE cash and NFO/BFO stock/index futures/options. Identity is the exchange plus numeric token; the trading symbol comes from the master. Index-only records, commodities and currencies are excluded. Tick and option-strike master values are divided by 100 to rupees; raw values and conversion metadata are retained. Futures' strike sentinel becomes null. Expiry is parsed from the dated `DDMMMYYYY` field and lot size comes from each contract. These unit and enum assumptions still need comparison with the current public master and broker-visible contracts. Master membership is not evidence of intraday eligibility or surveillance clearance.

Five-minute requests require exact-minute, offset-bearing bounds and are limited to 100 days. Daily requests use date-only bounds and a 2,000-day limit. All requests and source timestamps normalize to IST. Returned bars use `[start, end)`; an exact end-boundary row is excluded and counted. Daily end must be today or earlier, so the current daily candle is excluded. Rows must have six fields, consistent positive OHLC, nonnegative volume, unique timestamps and a valid five-minute grid. Null history blocks; an empty array remains explicitly empty. Completed-bar and prior-session replay requirements still apply.

FULL quotes accept 1–50 distinct `EXCHANGE:TOKEN` identifiers and disclose missing coverage, including `unfetched` records. The source price time is `exchTradeTime`, parsed as `DD-Mon-YYYY HH:MM:SS` in IST. `exchFeedTime` is preserved separately. Neither feed time nor download time refreshes an old trade. Select the matching quote for `context.quote`; its symbol must agree with the master. Authenticated acceptance must confirm these timestamp conventions.

## Account and margin binding

Profile `clientcode` must match the locally configured client code before account/margin reads proceed. Position/order rows with a conflicting client code block use. Funds must be an object and positions/orders must be arrays; null does not establish an empty account. Raw account snapshots remain `reconciled: false`. Convert and reconcile them into the paper ledger before risk evaluation.

Previews use NORMAL, INTRADAY, LIMIT and DAY with the exact master symbol/token, side, price and quantity. Only the non-executing margin calculator receives a single-position estimate request. Positive `totalMarginRequired` is bound to the observed account and a hash of the entire original preview, including fields not accepted by the calculator. A margin estimate does not establish a fill or a protective order. The endpoint allowlist excludes login, refresh, order placement/modification/cancellation and every other mutation route.

## Evidence and remaining acceptance

`tests/test_angel.py` covers headers, route restrictions, redirect refusal, response limits, error redaction, units, timestamps, partial data, account identity, exact margin binding and the adapter → 10,000-path replay → size → paper-ticket journey. Fixtures contain invented identities and prices. The common suite also runs the Angel demo.

The public-master request did not complete successfully. A subsequent bounded check was not executed because automatic approval review reached an account usage limit. Public-file parsing, authenticated data entitlement, available history, profile/account shapes, quote timestamps, margin comparisons and sourced charges remain acceptance work. Do not describe this adapter as account-tested or the complete skill as finished.

Official request-shape references: [SmartAPI documentation](https://smartapi.angelone.in/docs), [market data](https://smartapi.angelone.in/docs/MarketData), [response and historical data](https://smartapi.angelone.in/docs/ResponseStructure), [official SDK route definitions](https://github.com/angel-one/smartapi-python/blob/main/SmartApi/smartConnect.py), [official SDK examples](https://github.com/angel-one/smartapi-python/blob/main/test/api_test.py) and [public instrument master](https://margincalculator.angelone.in/OpenAPI_File/files/OpenAPIScripMaster.json). Documentation and SDK code were inspected; the SDK is not imported or executed by this adapter.
