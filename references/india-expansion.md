# Indian-market expansion — development status

This work is released in 0.9.7. It does not certify complete Indian-market coverage, exchange compliance, broker acceptance or production operation.

## One-minute and custom-level broker entries

Set `interval_minutes: 1` for ORB or `level`; the existing six scanners retain their five-minute defaults. The opening range remains fifteen clock minutes, so one-minute ORB requires fifteen opening bars plus a completed confirmation. Real five-minute bars are never interpolated into minute observations. All five read-only adapters and `broker.py candles --interval minute` support minute requests. Use the exact instrument's broker data, not an underlying or Yahoo proxy. Source candle/history metadata must say `minute`; replay and risk bind the interval to the plan. Watch refresh and the read-only acceptance probe support it too.

For `setup: "level"`, supply `config.level_entry`:

```json
{
  "broker": "kite", "exchange": "NSE", "symbol": "YOUR_MASTER_SYMBOL", "token": "YOUR_MASTER_TOKEN",
  "side": "BUY", "price": 100.0, "for_session": "YYYY-MM-DD",
  "available_at": "YYYY-MM-DDT09:15:00+05:30", "source": "Your dated level calculation or reviewed source"
}
```

Use actual tick-aligned prices and exact master identity. The first eligible completed close strictly beyond the level triggers; this is a close-above/below rule, not necessarily a crossover. Only bars starting after the level became available are eligible. The default structural stop is the observed intraday low for BUY or high for SELL, with configured tick buffer. Existing explicit exit policies also work. Changing the level, side, date, source or interval requires regenerated evidence. The scanner excludes instruments not matching this level's identity.

Historical custom-level rule matching requires separately sourced `history.level_entries`, keyed by session date with the same fields. Missing historical levels are skipped and counted separately; today's level is never applied backwards. Bootstrap paths still use the observed entry and prior exact-instrument bars.

```text
python scripts/trader.py demo --broker kite --setup level --workdir demo-level-new
python scripts/acceptance.py --broker kite --authenticated-readonly --instrument instrument.json --account-id YOUR_ID --interval-minutes 1 --out acceptance-new.json
```

The acceptance command reads the user's own locally configured credentials. It submits no orders and does not establish execution acceptance.

## Liquidity and slippage-aware sizing

Opt in using `config.liquidity_model` with `mode: "depth_stress_v1"`, `depth_fraction`, `max_participation_fraction`, `exit_shock_bps` and `max_age_seconds`. Fractions must be in (0,1]; the adverse shock must be nonnegative and below 10,000 basis points. These are user-selected stress assumptions, not calibrated parameters or exchange-prescribed limits.

Supply `context.liquidity` or `context.liquidity_books["EXCHANGE:SYMBOL"]`: exact `broker/exchange/symbol/token`, `source` equal to that broker, `as_of`, `quantity_unit: "units"`, `volume_window_start`, `volume_window_end`, nonnegative `traded_units`, and `bids`/`asks` arrays of `{price, quantity}`. Prices must be distinct, sorted best-first, on tick and uncrossed. The participation window cannot exceed five minutes; its end and the book must be fresh. Do not substitute contract counts for units or invent missing depth.

Sizing caps quantity by haircut visible depth executable at the entry limit, available exit depth and trailing-volume participation. It adds the current exit-depth sweep impact and the configured adverse stop shock to the loss reserve, including any increased estimated stop charges. Risk recomputes this evidence for the actual quantity. A future stop fill cannot be guaranteed by today's book; queue priority, latency, hidden liquidity and future gaps remain unmodelled. There is no automatic depth collector in this increment.

## Exact basket-margin research

`python scripts/portfolio_margin.py --input basket.json --out basket-margin-new.json` queries Kite's read-only basket margin endpoint for each ordered prefix, considering existing positions. Input contains `broker: "kite"`, `account_id` and 1–20 `legs`; each leg contains the exact dated `instrument`, same `account_id`, `product`, `side`, whole-lot `quantity` and tick-aligned limit `price`. CNC applies to equity, NRML to derivatives and MIS to intraday. Account books are read before and after; changes invalidate the estimate. Peak prefix and final requirements are reported separately.

This is not a local SPAN implementation, a guarantee for every partial-fill permutation or an execution permission. Other brokers' basket adapters and scenario-based portfolio margin remain outstanding. Accounts and brokers are never netted against one another.

## Automatic collection and dated sessions

```text
python scripts/india_collect.py --symbols RELIANCE.NS TCS.NS --filing-limit 5 --out india-collection-new.json
```

This bounded workflow collects NSE CM/FO holiday observations, current aggregate NSE cash-market status, public five-minute charts for up to twenty selected Indian symbols, the configured news feeds and up to twenty direct official filing attachments. `--sources` selects feeds; omission selects all existing feeds, including ET. `--universe` enables the existing issuer-candidate matching. Component failures remain visible. No publisher content is republished as full articles. Output is explicitly partial and cannot clear an entry; a missing news item or holiday is not proof that no event or closure exists.

Add `--archive trader/state/filings.sqlite` to retain a durable personal backlog. Later collection runs add unseen URL/publication-time pairs and process pending attachments before repeating completed downloads. Individual results persist transactionally under an OS process lock. Failed attachments require an explicit `filing_archive.py --retry-failed` run. The separate command accepts an `india_news_v1` input, `--database`, `--limit` and `--out`. Each batch is bounded; no scheduling service is activated. Undiscovered historical filings and revisions published without a changed URL/time are not covered.

Complete BSE coverage, historical backfill, effective membership/surveillance, verified issuer/fact extraction and instrument-specific ad-hoc session notices remain outstanding. No paid-data entitlement, redistribution permission or publisher completeness is inferred.

Set `require_instrument_session: true` to require `context.instrument_session` in paper risk checks in addition to the ordinary session snapshot. Every live entry requires it regardless of that optional paper flag. It must contain exact broker/instrument/account identity, `date`, fresh `as_of`, an official exchange `source` URL, `is_open_day: true`, `halted: false`, `continuous_open`, `continuous_close`, `broker_squareoff`, and `auction_included: false`. Weekend operation requires `special_session: true`. Flat-by must precede both the instrument's continuous close and broker square-off. This is supplied reviewed evidence, not an automatic interpretation of exchange circulars.

## Rules checked on 11 November 2026

- [NSE retail-algo framework](https://www.nseindia.com/static/trade/platform-services-non-neat-decision-support-tools-algorithm-trading) and [NSE retail API FAQ](https://nsearchives.nseindia.com/web/sites/default/files/inline-files/FAQ_Retail%20Algo_03112025_NSE.pdf): API orders require the applicable broker/exchange handling. This skill does not grant registration, tagging or static-IP approval. Existing live transports remain human-operated and disabled by default.
- [SEBI implementation timeline](https://www.sebi.gov.in/sebi_data/attachdocs/sep-2025/1759232056254.pdf): the retail-algo framework became applicable to all stock brokers from April 2026; a historical implementation exemption cannot be presumed.
- [NSE CAS](https://www.nseindia.com/market-data/closing-auction-session) and [market timings](https://www.nseindia.com/static/market-data/market-timings): current cash CAS, ordinary cash and derivative windows differ. General pages are not interchangeable with dated, instrument-specific circulars. Do not infer all instruments close at 15:30 or treat an auction as continuous trading. Existing research code still has regular-session assumptions outside the optional risk session gate; special-session/full-close modelling remains incomplete.
- [NSE holiday feed](https://www.nseindia.com/api/holiday-master?type=trading) and [market status](https://www.nseindia.com/api/marketStatus) are observations with explicit coverage limits.
- Minute history contracts: [Kite](https://kite.trade/docs/connect/v3/historical/), [Upstox](https://upstox.com/developer/api-documentation/v3/get-historical-candle-data/), [Dhan](https://dhanhq.co/docs/v2/historical-data/), [Angel](https://smartapi.angelone.in/docs). These document requests, not this user's entitlement or acceptance.
- [Kite basket margin](https://kite.trade/docs/connect/v3/margins/) supplies exact broker estimates. Broker requirements remain authoritative over local research models.

## Still required for the requested complete scope

1. Comprehensive permitted data sources, BSE-specific feeds, surveillance and automatic effective-date/session reconstruction.
2. Deeper execution-cost/queue models, automatic normalized depth collection and other-broker portfolio-margin integration.
3. Broader settlement/T1/pledged/MTF and corporate-action handling, new mixed-inventory entries and other brokers' carry execution. A separate [Kite mixed-inventory reducing-exit workflow](mixed-inventory-exits.md) is now implemented with per-action human review; existing conservative collectors still block unsupported inventory states.
4. A separately authorized, accepted unattended protection/auto-flatten execution design. This increment does not bypass the existing per-action human approvals or install an automatic service.
5. End-user authenticated broker, installed-host invocation and production-service acceptance. The publisher has no broker account; offline tests cannot supply this evidence.
