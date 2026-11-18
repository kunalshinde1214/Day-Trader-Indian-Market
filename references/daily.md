# Daily Indian-market reports

Four daily reports share one script, `scripts/daily.py`:

| Report | Script command | Scope |
| --- | --- | --- |
| India markets | `daily.py india-daily` | Nifty 50, Sensex and Bank Nifty in one comparison table, plus India VIX |
| Nifty 50 | `daily.py nifty` | Nifty 50, plus India VIX |
| Sensex | `daily.py sensex` | Sensex (Yahoo data; there is no official BSE source), plus India VIX |
| Bank Nifty | `daily.py banknifty` | Bank Nifty, plus India VIX |

Bare `/nifty`, `/sensex`, `/banknifty` and `/india-daily` are **not** registered slash commands. In Claude Code the plugin commands carry the plugin prefix: `/india-day-trader:nifty`, `/india-day-trader:sensex`, `/india-day-trader:banknifty` and `/india-day-trader:india-daily` (these daily commands are being restored in 1.4.0). Otherwise just ask: phrases such as “Nifty today”, “give me today's Bank Nifty briefing” or “aaj market kaisa hai” are recognised by the skill and run the same script. On the command line pass the name without a leading slash (Git Bash on Windows rewrites `/nifty` into a file path); the script still accepts the slash form.

## Simple commands

The everyday, credential-free route is public Yahoo data:

```text
python scripts/daily.py india-daily --collect-public
python scripts/daily.py nifty --collect-public --brief
python scripts/daily.py sensex --collect-public
python scripts/daily.py banknifty --collect-public --format json --out reports/banknifty-new.json
```

For a finished session's published NSE closing data, use [dated closing reports](closing-context.md):

```text
python scripts/daily.py india-daily --collect-close --session 2026-11-17
python scripts/daily.py nifty --collect-close --session 2026-11-17 --brief
```

Offline and supplied-data examples:

```text
python scripts/daily.py india-daily --demo
python scripts/daily.py nifty --demo --phase preopen --brief
python scripts/daily.py sensex --demo --phase close
python scripts/daily.py banknifty --input trader/daily.json
python scripts/daily.py india-daily --input trader/daily.json --format json --out reports/daily.json
```

The demo uses fictional levels and a fixed 6 November 2026 clock. Every report marks it synthetic, including when a fixture source label is present but its top-level flag is missing. It is not a market scan. Real inputs use the current clock; `--now` is an explicit dated-analysis override for local inputs only. New output paths are required; files are not silently overwritten.

### Public route (`--collect-public`)

`--collect-public` reads Yahoo Finance's public chart endpoint for `^NSEI` (Nifty 50), `^BSESN` (Sensex) and `^NSEBANK` (Bank Nifty): about six months of daily bars, plus today's five-minute bars once the session has started. It also reads the daily chart for `^INDIAVIX`. No login or broker account is used and nothing is submitted. The data is unofficial, may be delayed or revised, and is research only. Sensex in particular is Yahoo data, not an official BSE feed, and is labelled that way in every report.

The session date, open/close times and previous trading day come from the provider's reported trading period and observed history, not a verified exchange calendar; the report says so in its data notes. The route needs a provider session dated today and refuses weekends, so on holidays and weekends use `--collect-close` for the last session or a sourced `--input` file. `--now`, `--calendar` and `--events` are refused in this mode. If one index fails, the others are still reported and the failed one is listed; an India VIX failure is a data note, never a block.

### What each report shows

- **Before the open:** the last session's close with its date, change in points and percent against the session before it, and that session's range, e.g. “Last close on 17 Nov: 25,387.40, +96.30 (+0.38%) vs 16 Nov. 17 Nov range: 25,267.30-25,501.35.” The change comes from the supplied daily history; without an earlier close it reads “change n/a”, never a guess.
- **During market hours:** the latest value (last completed five-minute bar, or a fresh index quote) with its IST time, change in points and percent against the previous session's close, the range so far, and whether the index is above, below or inside its reference range.
- **After the close:** the session's last five-minute bar close with the day's change and range. This is not the official published close; use `--collect-close` for that. The watch line becomes the next session's reference high/low.
- **India VIX:** latest daily close, its date, and change against the previous observation. During the session the current day's VIX bar is marked provisional.
- **`india-daily`:** a summary table comparing Nifty 50, Sensex and Bank Nifty (date, close or level, change, change %, range) with an India VIX row, then one section per index.
- Every reference level carries its date (“17 Nov high”, “09:15-09:30 opening-range high”); numbers are formatted with thousands separators and two decimals; the 20-day average is shown with how far the last close sits from it. Coverage caveats are collected once in a closing **Data notes** section instead of being repeated per index.

`--brief` gives a short answer-first version: about six to ten lines per report (title, dated close/level and change, position, watch levels, India VIX, one notes line; `india-daily` uses the comparison table plus one line per index). It works with `--collect-close` too.

Markdown output is printed as UTF-8, so the rupee sign in closing reports does not fail on a legacy Windows console. Only collection and input-validation failures produce the `{"status": "BLOCKED", "reason": ...}` message, and the reason names the failed check; rendering or programming errors are not disguised as missing data.

For a current report using the user's locally configured Kite data access:

```text
python scripts/daily.py india-daily --collect-kite --calendar trader/daily-calendar.json
python scripts/daily.py nifty --collect-kite --calendar trader/daily-calendar.json --brief
```

`--collect-kite` makes only read-only instrument-master and historical-data requests. It does not connect an account ledger or submit orders. History entitlement and valid local Kite credentials are required. It discovers **spot index** tokens from unique `INDICES` master rows for `NSE:NIFTY 50`, `NSE:NIFTY BANK` and `BSE:SENSEX`; it never substitutes an equity, ETF, future or option. Index tokens are not hardcoded. A failed index stays visibly missing in a combined report.

The collector can also save the normalized snapshot for inspection:

```text
python scripts/daily_collect.py --broker kite --calendar trader/daily-calendar.json --out trader/daily-new.json
```

Authenticated automatic index collection is currently implemented for **Kite only**. Public Yahoo and dated NSE closing observations use separate routes. Normalized inputs from other authorized sources can feed the same report engine. The five existing broker trading adapters do not imply five automatic spot-index collectors. Constituent breadth and reviewed event coverage require their own sourced inputs; the basic collector reports these gaps. `--events` accepts a reviewed event file in Kite collection mode.

### Automatic context collection

```text
python scripts/daily.py india-daily --collect-kite --calendar trader/daily-calendar.json --with-context
python scripts/daily.py nifty --collect-kite --calendar trader/daily-calendar.json --with-context --membership trader/daily-membership.json
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

Optional top-level `vix` contains `source`, `as_of` (at most 15 minutes old), `symbol: "^INDIAVIX"` and ordered `rows`, each with `session` and `close` (optionally `open`/`high`/`low`). The last row is shown; the row before it gives the change. An invalid or stale VIX block is reported as unavailable without affecting the index analysis.

Optional `history` rows also supply the last session's change: the second-to-last row is the earlier close it is measured against.

Optional `events` contains source/as_of, `session`, `coverage` (`partial` or `reviewed_complete`) and `items`. Each item has `headline`, `source`, and nonfuture `published_at`. Missing or stale event evidence is not “no event risk.”

## Data contracts checked

The collector follows [Kite instrument/quote documentation](https://kite.trade/docs/connect/v3/market-quotes/) and [historical candles](https://kite.trade/docs/connect/v3/historical/). Broader index identities can be checked through [Nifty Indices](https://www.niftyindices.com/indices) and [BSE Indices](https://bseindices.com/). These references do not establish authenticated data acceptance or redistribution rights.
