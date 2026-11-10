# Broker adapters and schema-v2 migration

Development checkpoint: 4 November 2026. Kite, Upstox, Dhan, Angel One and FYERS have read-only adapters and passing synthetic paper workflows. No authenticated broker test or live order has been performed. All five have implemented read-only routes; unknown broker names fail explicitly. The common CLI is `scripts/broker.py`; the original Kite CLI remains available. For Dhan's credentials, commands, canonical symbols and array/time normalization, read [Dhan setup](dhan.md). For Angel One device headers, commands and normalization, read [Angel One setup](angel.md). For FYERS identity, depth-backed quotes and margin semantics, read [FYERS setup](fyers.md).

## Configure locally

Initialize a new directory with `python scripts/trader.py init --broker upstox --workdir trader-upstox` (or `--broker kite`). Enter the user's paper limits and the broker profile's `user_id` in `config.account_id`. `PAPER-DEMO` is only a fictional default. Complete login/2FA through the broker, then set `UPSTOX_ACCESS_TOKEN` or `KITE_API_KEY` plus `KITE_ACCESS_TOKEN` in the Python process environment. Do not put credentials in chat, source files or command arguments. Token exchange, refresh and subscription purchase are not implemented.

Upstox initialization produces an intentionally incomplete charge card. Populate `source`, effective/review dates, brokerage, GST/SEBI and the traded segment's rates from a current broker tariff. The schema matches `assets/charges.example.json`; that file contains Kite-specific assumptions and must not simply be relabeled. Missing fees block analysis. Demo mode uses explicitly fictional test rates for every implemented broker.

Account and margin responses obtain identity from authenticated broker responses: profiles for Kite/Upstox/Angel/FYERS, fund limits for Dhan. They retain the account ID locally; the CLI prints only save status. Account files remain **unreconciled**: raw funds, orders and positions are not a daily risk ledger. No adapter infers cash availability, reserved risk or trade counts from an incomplete account response.

## Commands

```text
python scripts/broker.py capabilities --broker upstox
python scripts/broker.py instruments --broker upstox --out trader-upstox/master.json
python scripts/broker.py account --broker upstox --out trader-upstox/account-raw.json
python scripts/broker.py quotes --broker upstox --symbols "<instrument_key from master>" --out trader-upstox/quotes-raw.json
python scripts/broker.py candles --broker upstox --instrument trader-upstox/instrument.json --intraday --out trader-upstox/current-bars.json
python scripts/broker.py candles --broker upstox --instrument trader-upstox/instrument.json --from 2026-10-01 --to 2026-10-30 --out trader-upstox/history.json
python scripts/broker.py margin --broker upstox --payload trader-upstox/payload.json --out trader-upstox/margin.json
```

Replace illustrative dates with the intended prior history. Select one complete normalized master record into `instrument.json`. The CLI verifies its broker before requesting candles. Upstox history uses inclusive date bounds, newest date first in the API path, and a separate current-day endpoint. Five-minute requests are conservatively limited to a 30-day difference; collect adjacent nonoverlapping ranges as needed for at least 20 complete prior sessions. Preserve identity and provenance when combining bars; duplicates or missing bars block replay. Empty histories are reported, never replaced with invented data.

For Kite, use the same CLI with `--broker kite`; candle bounds must be offset-bearing timestamps, and quotes take `EXCHANGE:SYMBOL`. See [Kite-specific setup](kite.md). New snapshots never overwrite files. Commands are single requests or bounded account reads, without automatic retries, scheduling or streaming.

## Prepare the paper workflow

1. Set `broker` and `account_id` in the configuration. Use the selected broker's normalized master record, with same-day `master_date`, exact key, tick, lot, expiry and underlying classification.
2. Wrap current candle output under `candles.instruments["EXCHANGE:SYMBOL"]`. Build a sourced universe with membership, turnover and restrictions separately. Broker masters do not establish eligibility.
3. Upstox quotes are keyed by `instrument_token`, **not** by the response's display-symbol key. Select the exact master token, add its verified `symbol`, and copy its price/source/`as_of` into `context.quote`. Missing quotes or missing source timestamps block use. Keep actual quote time; `fetched_at` is not quote freshness.
4. Reconcile account orders/positions with the local journal, preserving `broker` and `account_id`, then prepare all context fields in [commands](commands.md). Add verified session, event and surveillance information. Use the [ingestion and journal tools](operations.md); source review and raw broker order/fill normalization still need explicit inputs.
5. Run scan → plan → replay → size. For derivatives, use the selected adapter's margin command on the resulting `payload.json`, insert its output under `context.margin`, then run risk → ticket with fresh account evidence. Margin responses bind the full payload hash and authenticated account identity.

The normalized analysis product remains `MIS`. Kite's preview uses `MIS`, Upstox's uses `I`, and Dhan/Angel/FYERS previews use `INTRADAY`. All are LIMIT/DAY paper previews. `ticket.payload_preview` is the common field; Kite also retains `kite_payload_preview` for existing readers. There is no placement command or permission-granting approval field.

## Normalization and limitations

Upstox JSON master keys remain `SEGMENT|IDENTIFIER`; do not substitute exchange tokens or Kite numeric IDs. Supported segments map NSE_EQ→NSE, BSE_EQ→BSE, NSE_FO→NFO and BSE_FO→BFO. Index quote instruments, commodities and currency contracts are outside this adapter's current scope. Derivative expiry is epoch milliseconds converted to an IST date. Strike and candle/quote prices stay in rupees. The master tick conversion divides raw JSON ticks by 100; the official page's JSON examples show 5 while its legacy CSV example shows 0.05. This unit interpretation must be checked against actual contracts during authenticated acceptance, not inferred from price magnitude. Raw tick and conversion metadata are retained.

The public master download sends no Authorization header. `Last-Modified` supplies its source date; a missing header blocks import and an old date remains old. Download time cannot refresh master validity. JSON bodies, HTTP error bodies and credential-bearing exception text are suppressed on failures. Redirects are refused. Endpoint allowlists reject order mutation and authentication routes before reading credentials.

Upstox margin estimates accept an instrument/quantity/side/product/price subset; the stored hash still binds the entire original preview. Use the greater of returned required/final margins conservatively. A result is an estimate, not a fill or a protective order. Bounded polling, reconciliation, five-broker [raw trade imports](account-service.md) and an opt-in read-only acceptance harness are implemented. The separate [human-operated execution tool](execution.md) includes five mutation transports and intraday order/position normalization. Five read-only WebSocket adapters are implemented; see [streaming setup](streaming-deployment.md). Authenticated acceptance remains pending. Read-only adapters reject mutation endpoints. Automatic daily spot-index collection is currently Kite-only; other sources can supply the normalized [daily input](daily.md).

## Migration

Older saved data lacks broker/account identity and is rejected. Keep it as historical evidence; regenerate a new demo folder or collect fresh real snapshots. Do not add current timestamps or relabel old broker keys to force acceptance. New config has `schema_version: 2` and `account_id`; instruments and histories have `broker`; plans have `broker` and `account_id`; account and margin evidence must match. Broker/account switches invalidate existing plans, replays and ticket hashes. The original `demo-trader/` is preserved as the schema-v1 archive.

## Verified API references

Consulted 1 November 2026; these establish request shapes, not an authenticated acceptance result:

- [Upstox instruments](https://upstox.com/developer/api-documentation/instruments/) — JSON fields, expiry, keys and master downloads.
- [Historical candles V3](https://upstox.com/developer/api-documentation/v3/get-historical-candle-data/) and [intraday candles V3](https://upstox.com/developer/api-documentation/v3/get-intra-day-candle-data/) — paths and intervals.
- [Full quotes](https://upstox.com/developer/api-documentation/get-full-market-quote/) — response identity and timestamp.
- [Profile](https://upstox.com/developer/api-documentation/get-profile/), [funds](https://upstox.com/developer/api-documentation/get-user-fund-margin/), [positions](https://upstox.com/developer/api-documentation/get-positions/) and [orders](https://upstox.com/developer/api-documentation/get-order-book/) — read-only account snapshots.
- [Margin](https://upstox.com/developer/api-documentation/margin/) and [order V3](https://upstox.com/developer/api-documentation/v3/place-order/) — estimate fields and preview encoding only; the order endpoint is not allowlisted.

`tests/test_brokers.py`, `tests/test_dhan.py`, `tests/test_angel.py` and `tests/test_fyers.py` use fictional contract fixtures, including unit/timestamp conversion, endpoint rejection, malformed responses, partial quotes, account binding, exact margins and all five offline demo journeys. No test embeds real credentials or account captures. The separate public Dhan master smoke test is documented in [Dhan setup](dhan.md).
