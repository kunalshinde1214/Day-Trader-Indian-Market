# Daily Indian-market reports

The four skill aliases and matching Python commands are implemented:

| Alias | Scope |
| --- | --- |
| `/india-daily` | Combined Nifty 50, Sensex and Bank Nifty report, with comparable percentage changes |
| `/nifty` | Nifty 50 |
| `/sensex` | Sensex |
| `/banknifty` | Bank Nifty |

These are interpreted within this skill; installing a skill does not register arbitrary native slash commands in every host application. Natural language such as “give me today's Bank Nifty briefing” uses the same route. The scripts accept names with or without the leading slash.

## Simple commands

```text
python scripts/daily.py /india-daily --demo
python scripts/daily.py /nifty --demo --phase preopen --brief
python scripts/daily.py /sensex --demo --phase close
python scripts/daily.py /banknifty --input trader/daily.json
python scripts/daily.py /india-daily --input trader/daily.json --format json --out reports/daily.json
```

The demo uses fictional levels and a fixed 6 November 2026 clock. Every report marks it synthetic, including when a fixture source label is present but its top-level flag is missing. It is not a market scan. Real inputs use the current clock; `--now` is an explicit dated-analysis override for local inputs only. New output paths are required; files are not silently overwritten.

For a current report using the user's locally configured Kite data access:

```text
python scripts/daily.py /india-daily --collect-kite --calendar trader/daily-calendar.json
python scripts/daily.py /nifty --collect-kite --calendar trader/daily-calendar.json --brief
```

`--collect-kite` makes only read-only instrument-master and historical-data requests. It does not connect an account ledger or submit orders. History entitlement and valid local Kite credentials are required. It discovers **spot index** tokens from unique `INDICES` master rows for `NSE:NIFTY 50`, `NSE:NIFTY BANK` and `BSE:SENSEX`; it never substitutes an equity, ETF, future or option. Index tokens are not hardcoded. A failed index stays visibly missing in a combined report.

The collector can also save the normalized snapshot for inspection:

```text
python scripts/daily_collect.py --broker kite --calendar trader/daily-calendar.json --out trader/daily-new.json
```

Automatic index collection is currently implemented for **Kite only**. Normalized inputs from other authorized sources can feed the same report engine. The five existing broker trading adapters do not imply five automatic spot-index collectors. Constituent breadth and reviewed event coverage require their own sourced inputs; the basic collector deliberately reports these gaps. `--events` accepts a reviewed event file in collection mode.

### Automatic context collection

```text
python scripts/daily.py /india-daily --collect-kite --calendar trader/daily-calendar.json --with-context
python scripts/daily.py /nifty --collect-kite --calendar trader/daily-calendar.json --with-context --membership trader/daily-membership.json
```

`--with-context` adds intraday constituent quotes and today's available NSE announcement-feed headlines. It also works on `daily_collect.py`. Membership comes from the public Nifty 50/Bank Nifty constituent CSVs or the supplied reviewed file. Public files do not establish an effective date: their membership basis is displayed as unverified and breadth remains partial even when every quote arrives. Public probes on 7 November 2026 did not pass; collection failures remain coverage gaps without substituting demo data. Sensex needs supplied reviewed BSE membership; it is not inferred from an NSE index.

The optional membership file maps index IDs to objects with `index_id`, `source`, `as_of`, current `membership_date`, `membership_verified: true` and unique `members` symbol strings, for example `{"nifty": {...}, "sensex": {...}, "banknifty": {...}}`. Supply evidence only for the indices requested; omitted indices retain a visible collection gap. Names are resolved against exact equity rows in the fetched broker master and verified against each quote's token. Missing or stale member quotes remain missing, not zero-return observations. Kite's previous-day OHLC close is bound to the supplied calendar's preceding trading session.

Automatic breadth collection runs during the session only; pre-open and post-close reports do not relabel live last prices as current-session or final closing breadth. Network completion time is used to validate quote freshness, while the quote's original last-trade timestamp is preserved. Crossing a session boundary blocks that collection.

Announcements are partial feed coverage, not a full economic or earnings calendar. Undated items remain unparsed and never grant event clearance. A supplied reviewed `--events` file is preserved instead of overwritten by RSS. Neither context collection nor the separate [streaming runner](streaming-deployment.md) submits orders.

## Report behavior

The supplied dated session determines pre-open, intraday or close mode. `--phase` can assert a phase, but cannot make a closing report appear before the session closes. Holidays, unknown session status and uncovered exchanges block analysis; weekends need explicit special-session evidence.

- Previous-session high, low and close use the calendar's **actual previous trading session**, not “yesterday.”
- Intraday price defaults to the latest completed five-minute candle. An optional fresh index quote is labelled separately. Its last trade/quote timestamp cannot be replaced by fetch time.
- The first three completed bars define the opening range. Missing opening, intervening or latest completed candles block that index. A forming candle cannot trigger a scenario.
- Conditions compare the last completed candle close with the previous-session/opening-range envelope. Bullish, bearish and range scenarios state their thresholds and invalidation; they are not orders or calibrated probabilities.
- Closing reports require the full verified session's five-minute bars. Their value is the last completed candle close, **not a claim of an official settlement or separately published final closing value**.
- The optional SMA is the mean of the last 20 supplied prior-session closes, ending at the verified previous session. It is a 20-observation measure; full historical calendar completeness is not inferred.
- Breadth reports advancing/declining/unchanged counts and actual missing constituents. Strongest/weakest names are ranked by unweighted observed returns. No exact index-point contribution is invented without divisor/weight evidence.
- Spot-index volume, VWAP, executable quantities, option premiums and lot sizes are never fabricated. A future/options plan needs its own exact contract, market data, costs and risk checks.

`READY` means the implemented report's required and optional context passed its checks; it does not mean a trade is ready. `PARTIAL` means usable index analysis with declared gaps. `BLOCKED` means core evidence failed. Combined reports retain blocked indices and show requested/available/blocked counts.

## Input contract

Start from [the calendar template](../assets/daily-calendar.example.json), replacing its intentionally incomplete fields with verified dated evidence. It declares `source`, `as_of`, `date`, `previous_session`, `is_open_day`, `special_session`, covered `exchanges`, and local IST `open`/`close`. Defaults are not holiday or special-session clearance.

The daily bundle has `session`, `indices` and optional `events`. Keys under `indices` are `nifty`, `sensex`, `banknifty`. Each index needs:

```json
{
  "index_id": "nifty",
  "price_basis": "spot_index_points",
  "source": "YOUR_AUTHORIZED_SOURCE",
  "as_of": "2026-11-06T09:35:00+05:30",
  "previous": {
    "source": "YOUR_AUTHORIZED_SOURCE",
    "as_of": "2026-11-05T16:00:00+05:30",
    "session": "2026-11-05",
    "open": 100, "high": 102, "low": 98, "close": 101
  },
  "candles": {
    "source": "YOUR_AUTHORIZED_SOURCE",
    "as_of": "2026-11-06T09:35:00+05:30",
    "interval": "5minute",
    "rows": []
  }
}
```

Those values are **schema illustrations, not market levels**; empty intraday rows will block once a completed bar is due. Candle rows need offset-bearing start time `at` and `open/high/low/close`; index volume is not required. Index/candle fetch snapshots must be within 120 seconds. Optional `quote` contains source, `as_of` and positive `value`, with a 60-second intraday freshness limit.

Optional `history` contains source/as_of and ordered unique `rows`, each with prior `session`, `close`, `available_at`. Its latest close must agree with `previous.close`; current/future sessions and future availability are rejected.

Optional `breadth` contains source/as_of, same-day `membership_date`, `membership_source`, unique symbol list `members`, and `observations`. Each observation contains `symbol`, `as_of`, `value`, `previous_close`, and `previous_session`. Closing observations also need `final:true`. A stale, duplicate or foreign member invalidates breadth, without inventing missing members' returns.

Optional `events` contains source/as_of, `session`, `coverage` (`partial` or `reviewed_complete`) and `items`. Each item has `headline`, `source`, and nonfuture `published_at`. Missing or stale event evidence is not “no event risk.”

## Data contracts checked

The collector follows [Kite instrument/quote documentation](https://kite.trade/docs/connect/v3/market-quotes/) and [historical candles](https://kite.trade/docs/connect/v3/historical/). Broader index identities can be checked through [Nifty Indices](https://www.niftyindices.com/indices) and [BSE Indices](https://bseindices.com/). These references do not establish authenticated data acceptance or redistribution rights.
