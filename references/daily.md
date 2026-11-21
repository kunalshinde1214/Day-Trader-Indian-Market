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

One command gives the full briefing. `--collect-public` combines NSE's free public data with Yahoo Finance's public charts:

| Item | Source |
| --- | --- |
| Live Nifty 50, Bank Nifty and India VIX levels (market hours) | NSE allIndices snapshot with NSE's own minute timestamp ([live indices](https://www.nseindia.com/market-data/live-market-indices)); Yahoo `meta.regularMarketPrice` if NSE refuses, labelled |
| Nifty 50 / Bank Nifty constituent advance/decline | the same NSE snapshot |
| Previous close, previous-session change and 20-day average (Nifty 50, Bank Nifty) | NSE's dated `ind_close_all_DDMMYYYY.csv` files (about 21 files, four at a time, once per run). They include special sessions such as the 8 Nov 2026 Muhurat session, which Yahoo's daily history omits |
| Official close after 15:30 (Nifty 50, Bank Nifty) | today's NSE index-close file when published, else NSE's snapshot after 15:30 |
| Five-minute bars (opening range, position) | Yahoo `^NSEI`, `^NSEBANK`, `^BSESN` |
| Sensex | Yahoo only (BSE's APIs return 403 to scripts, and no free real-time Sensex source allows scripted use): quote during the session (about 15 minutes delayed; the age is printed), Yahoo daily close after 15:30, labelled unofficial. Its 5-minute bars are judged up to the quote's own time, so the delay is not reported as missing bars, and the report compares Sensex and Nifty 50 at the same moment next to Nifty's current NSE change. Bars are labelled by their end time. One wording: “compare like for like at the same 5-minute bar, ending 11:45 IST (Yahoo, both indices): Sensex -0.51%, Nifty 50 -0.49%. Nifty 50 is now -0.64% (12:00 IST, NSE).” When the Sensex quote is within two minutes of that bar end the Sensex figure is the quote (one Sensex figure for that moment). During market hours the Sensex and `india-daily` reports lead with ONE Sensex figure, “Sensex ≈ 81,891 (-0.78%) at 13:17 IST (estimate from live Nifty 50); last Yahoo quote 81,897.40 (-0.77%) at 13:03 IST, vs 17 Nov close 82,532.19”, and the like-for-like bar comparison above moves to the data notes (“Like-for-like check: …”). The estimate is the latest Sensex quote (or, when no Nifty 50 bar ends within 2.5 minutes of the quote, Sensex at that shared bar) moved by the live NSE Nifty 50 change since then, times the Sensex/Nifty ratio of today's 5-minute moves (least squares through zero, clamped 0.8-1.2; 1:1 with fewer than six moves). The title then carries the NSE snapshot time the body's “now” lines use (“market hours, NSE data as of 13:17 IST”), not the run time. In `india-daily` the Sensex row is marked “(~15 min behind)”. |
| Drivers | NSE allIndices sector indices; Nifty 50 / Bank Nifty constituent lists and bhavcopy (NSE); Yahoo stock prices and one month of 5-minute charts for the weight estimate |
| 52-week / record context | NSE allIndices `yearHigh`; Yahoo ~10-year daily chart (unofficial) |
| Holidays, next trading day, expiry shifts | NSE holiday master ([holidays](https://www.nseindia.com/resources/exchange-communication-holidays)), read once per run |
| Previous session's context | the [closing report](closing-context.md): FII/DII and NSE EQ breadth; GIFT Nifty only outside 09:15-15:30 |

No login or broker account is used and nothing is submitted; the data is research only. NSE's JSON API answers scripts on `www.nseindia.in` (`www.nseindia.com` sends them a 301 and redirects are refused), so requests go to the exact `.in` twin of an allowlisted `.com` URL; reports cite `www.nseindia.com`. Every step is optional: a source that fails becomes one plain data note.

**Degrade, never block.** Missing five-minute bars no longer block an index. The report still shows the previous close, its change and range from daily history, the latest quote (labelled with source and time), the session range from that quote, and levels from the daily reference; the opening range is omitted if its bars are missing and the data notes say, for example, "intraday 5-minute bars received 40 of 45". Corrupt bars (duplicates, out of order, future) are discarded the same way, and a stale quote is ignored with a note. Only a wrong index identity or an unverifiable previous session leaves an index unavailable, and then the report says so in one plain sentence; internal check names never reach the reader.

**Closed market.** On a weekend or NSE holiday the command answers "**Market closed today (Tue 24 Nov 2026: Prakash Gurpurb Sri Guru Nanak Dev) — figures are from the last session, Mon 23 Nov 2026.** Next trading day: Wed 25 Nov 2026." followed by the after-close block for the next session, as on that session's evening: per index the last close and range (NSE; Sensex from Yahoo's daily close, unofficial), "Levels for Wed 25 Nov (next session)" and classic pivots; the sectors (bank sub-indices for Bank Nifty); the top three point contributors on the side of the move and the two biggest against it (estimated from the session's bhavcopy, weights as in the live reports, with the check against the actual change; charts read back from the session-keyed cache); "Possible reasons (headlines, unverified)" since the close of the session before; "Cues for <next session>" (GIFT Nifty since the last close, US markets, gold, crude and the rupee since that session's open); all NSE shares up/down from the bhavcopy; FII/DII with the side in words ("FII/FPI +₹10,308.42 cr (net buyers); DII −₹4,859.80 cr (net sellers)"); GIFT Nifty with the time of its last trade ("last trade Sat 21 Nov 02:44 IST"), not a staleness warning; and the holiday/expiry calendar. Source and method notes stay in the data notes. If Yahoo has no session on a day NSE lists as a trading day, the report is built from NSE alone. `--now`, `--calendar` and `--events` are refused in this mode.

### What each report shows

- **Before the open:** the last session's close with its date, change in points and percent against the session before it, and that session's range, e.g. “Last close on 17 Nov: 25,387.40, +96.30 (+0.38%) vs 16 Nov. 17 Nov range: 25,267.30-25,501.35.” Without an earlier close it reads “change n/a”, never a guess. Classic floor pivots for today, dated GIFT Nifty futures and the previous session's FII/DII and breadth follow.
- **During market hours:** the latest value with its IST time and source (“At 09:52 IST (NSE live index level): 25,258.40, -129.00 (-0.51%) vs 17 Nov close 25,387.40”), the range so far, and whether the index is above, below or inside its reference range. When completed 5-minute closes are already beyond a watch trigger the report says when: “Bearish condition met at 10:20 IST: every completed 5-minute close since then is below 25,267.30 (17 Nov low), the previous session's low - a sign of weakness”, and the watch line says when it no longer applies (“after a completed 5-minute close back above 25,267.30, the bearish condition no longer applies”) instead of repeating the generic condition; a crossing that has since reversed is not reported as met. If only the latest quote is beyond a trigger, the report says it is not yet confirmed by a completed close. Mid-session it prints “Provisional levels for Thu 19 Nov if the session ended now: high …, low …, last …; final after 15:30 IST.”
- **What is moving it (market hours and after the close):** one line from [market_movers](market-movers.md): the weakest and strongest NSE sectors and the three biggest estimated point drags and lifts, in the report's own index points: Nifty 50 points for Nifty 50 and India markets, **Sensex points** for the Sensex report, or Bank Nifty's five heaviest members with % change and estimated Bank Nifty points plus the PSU/Private Bank sub-indices. Nifty 50 weights are anchored to the NSE Indices Nifty 50 factsheet's top-10 and sector weights and Bank Nifty weights to the Nifty Bank factsheet (`assets/index_weights.json`, see [market_movers](market-movers.md)); the rolled-forward factsheet weights are deterministic (the same past close gives the same figure every run). When the check time (the Yahoo stock-price moment) differs from the NSE headline time, the data note says so. BSE serves neither the Sensex member list nor its weights to scripts, so the Sensex report fits Sensex's own Yahoo 5-minute returns (about a month) on the Nifty 50 stocks, takes the 30 largest fitted weights as the Sensex members, weights them by the anchored Nifty 50 weights rescaled to 100% (a refit on them alone when the factsheet file is unusable), and checks the estimate against Sensex at the same completed 5-minute bar (Sensex's free feed is ~15 minutes behind). When that check fails, only the members' % moves are shown, never points; Nifty 50 points are never presented as Sensex points. The data notes show each estimate-vs-actual check. If the stock data fails, sectors are still shown; if everything fails, one data note says so.
- **Possible reasons and cues (market hours and after the close):** after the drivers line, “Possible reasons (headlines, unverified): TITAN: "Titan, Kalyan Jewellers shares fall up to 4% as gold, silver prices crash" (Moneycontrol.com, 18 Nov 11:59 IST); …” - up to three dated headlines from [market_movers](market-movers.md)' filters (published since the previous close, naming today's movers or a macro driver; housekeeping such as ESOP grants or AGM notices and multi-day stories against today's move dropped; each top mover's causal headline first), for the report's own index (Bank Nifty: its heavyweights and bank sub-indices; `india-daily`: the market). Then “Cues (headlines since the 17 Nov close, unverified): GIFT Nifty - …; US markets - …; Gold - …; Crude - …; Rupee - …” with the newest matching Google News headline for each. All searches run in parallel within about 9 seconds (6 seconds per request; the cue search starts alongside the price work); a failed or slow search prints “… a gap”, never a guess. After the close both lines stay: the reasons still explain the day, and the cues look ahead - “Cues for Thu 19 Nov (look-ahead headlines, unverified; GIFT Nifty since today's 15:30 close, the rest since today's open): GIFT Nifty - none yet; US markets - …; Gold - …; Crude - …; Rupee - …”. GIFT Nifty's morning headlines were about today's open, so only its evening headlines count; a cue with nothing new reads “none yet”.
- **Bad ticks in Yahoo's 5-minute bars:** the day high/low comes from the provider's own day figures (NSE's closing report or live level; for Sensex, Yahoo's daily candle and quote meta), never from the extremes of the 5-minute bars when those exist. A 5-minute bar whose high or low reaches more than 0.3% beyond both its open and close while the reference index's bar at the same time does not (its wick under a quarter as long; Nifty 50 for Sensex and Bank Nifty, Sensex for Nifty 50) is a bad tick: that wick is cut back to the bar's open/close before the opening range, day range (when no published one exists) and pivots are computed, and a data note names the bar (on 18 Nov 2026 Yahoo's 15:20 Sensex bar showed 82,817.57 against a day high of 82,578.87, which had moved every Sensex pivot). After the close, an isolated bar beyond the published day high/low is cut back to it. A wick shared by the reference index, or without a reference bar, is kept: a real fast move is never erased. Single-index reports fetch the reference index's bars alongside the drivers.
- **Broader market (`india-daily`, market hours and after the close):** Nifty Midcap 150 and Nifty Smallcap 250 change and Nifty 500 advances/declines from the same NSE allIndices snapshot as the headline levels.
- **One time for the headline block:** when the headline is NSE's live level, the title reads "market hours, data as of HH:MM IST" and the drivers, India VIX and member-breadth lines at that same minute say "same time" or carry no time; a line from another moment keeps its own time. A short "How to read this" line under the levels table explains the opening range (first 15 minutes) and what bullish/bearish conditions mean.
- **Records:** when an index is within 2% of its 52-week high or set a record, one line says so, e.g. “Records: 17 Nov high 62,442.20 is the 52-week high (NSE), now 0.60% below it; 17 Nov close 62,273.25 was a record close (highest close in Yahoo daily data from 18 Nov 2016, the start of the data used).” 52-week highs come from NSE's allIndices `yearHigh` (Sensex: Yahoo's 52-week high); record closes and highs from Yahoo's ~10-year daily chart (unofficial). Nothing is printed when the index is far from its high.
- **After the close:** the official close (NSE for Nifty 50/Bank Nifty; Yahoo daily close for Sensex, labelled unofficial) and “Levels for Thu 19 Nov (next session): 18 Nov high …, low …, close …”, plus classic floor pivots for that date (arithmetic from those three numbers, not a forecast). Without an official close the last five-minute bar is labelled as not the official close.
- **India VIX:** NSE live level during the session (timestamped, provisional); after the close NSE's official close from the daily index report (`ind_close_all`, labelled "NSE official close"; 15.57 +0.29% on 18 Nov 2026 against 15.56 in the 15:30 snapshot) once it is published, otherwise the snapshot. Yahoo is only the fallback when NSE fails: today's value is Yahoo's quote (`meta.regularMarketPrice`, with its own trade time) against the close of the previous daily candle, because Yahoo's daily candle for today can sit frozen at the open (18 Nov 2026: 15.52, the 17 Nov close, so the change read +0.00 against NSE's 15.57, +0.29%); if no current quote is available and the candle still equals today's open, the line says it may be stale.
- **"⚠ stock contributions not collected — rerun":** printed in the full report and in `--brief` whenever the stock-level drivers are missing this run (the reason follows, usually NSE refusing the previous-day bhavcopy). The bhavcopy is retried with backoff and kept on disk after its first successful download, so a rerun normally fills it. The reason names the stage that failed: the previous-day bhavcopy, NSE's constituent list, or Yahoo returning too few constituent price charts within the time limit (the charts are cached on disk too, so a rerun fetches only what is missing). Background downloads that stall past their time budget become stated gaps, and the script exits as soon as the report is printed.
- **Calendar (NSE):** upcoming holidays and expiry shifts, e.g. “Nifty 50 and Bank Nifty monthly expiry moves from Tue 24 Nov (Prakash Gurpurb Sri Guru Nanak Dev) to Mon 23 Nov.” Expiries follow NSE's Tuesday rule (Nifty 50 weekly and monthly, Bank Nifty monthly), moved to the previous trading day on a holiday; confirm with NSE circulars. Nothing is printed about event calendars unless one was supplied.
- **`india-daily`:** a summary table comparing Nifty 50, Sensex and Bank Nifty (date, level/close, change, change %, range) with an India VIX row, the like-for-like Sensex line, the market and Bank Nifty driver lines and any record line, then **one** levels table with a column per index and one bullet per index (position or met condition, watch levels, 20-day average, NSE member breadth): about 60 lines in full instead of three near-identical sections.
- **Calendar:** Bank Nifty reports omit Nifty 50 weekly expiries (Bank Nifty has monthly expiries only); Sensex reports show NSE holidays but no NSE expiries (Sensex options follow BSE's schedule).
- **Disclaimers:** one short data-notes line, “Research only; no orders.”
- Every reference level carries its date (“17 Nov high”, “09:15-09:30 opening-range high”); numbers use thousands separators and two decimals; the 20-day average states its basis (NSE closes including special sessions, or Yahoo closes for Sensex, and for Nifty 50/Bank Nifty when NSE's files are unavailable, with any missing special session named, e.g. “Yahoo omits the 8 Nov special session, so the window starts one session earlier”). NSE's daily files are read with an 8-second timeout each and a 30-second budget overall; a file that fails is retried once and then skipped with a note (the window then starts that many sessions earlier) instead of abandoning the NSE history. Published files never change, so they are cached on disk outside the plugin (`%LOCALAPPDATA%\india-day-trader\cache` on Windows, `$XDG_CACHE_HOME` or `~/.cache/india-day-trader` elsewhere; set `INDIA_DAY_TRADER_CACHE` to another folder, or `off`). Network failures in any optional context (a dropped connection, `IncompleteRead`, a timeout) become a data note, never a traceback; Yahoo requests retry transient failures with a short backoff. Caveats are collected once in **Data notes**.

For “why is the market up/down?” use [`market_movers.py`](market-movers.md): direction first (correcting a wrong premise), sectors, estimated Nifty 50 point contributors, breadth, VIX and FII/DII.

`--brief` gives a short answer-first version: about six to fourteen lines per report (title, dated close/level and change, position or met condition, what is moving it, records, watch levels, provisional next-session levels, India VIX, previous-session close and FII/DII, holiday/expiry shifts, and one notes line naming the sources, e.g. “NSE live data (nseindia.com, 09:52 IST) + NSE daily closes + Yahoo public data for 5-minute bars”; `india-daily` uses the comparison table plus one line per index). It works with `--collect-close` too.

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
- Intraday price defaults to the latest completed five-minute candle. An optional fresh index quote (`quote`: `source`, `as_of`, `value`, optional `open`/`high`/`low`, `provider`, and `max_age_seconds` up to 1,800 for a delayed public source, default 60) is labelled separately. Its source timestamp cannot be replaced by fetch time; a stale quote is ignored with a note.
- The first three completed bars define the opening range. Missing opening, intervening or latest candles make the index PARTIAL (bars incomplete) instead of blocking it: the opening range is dropped if its bars are missing and position is judged on the latest quote, labelled. A forming candle cannot trigger a scenario.
- Conditions compare the last completed candle close with the previous-session/opening-range envelope. Bullish, bearish and range scenarios state their thresholds and invalidation; they are not orders or calibrated probabilities.
- After the close, a supplied `official_close` (`value`, `session`, `basis`, optional `open`/`high`/`low`) is shown with its basis. Without one the value is the last five-minute bar close, labelled **not the official close**.
- The optional SMA is the mean of the last 20 supplied prior-session closes, ending at the verified previous session. If the history fails its cross-check, the SMA and last-session change are omitted with a note and the index is still reported. A `history.basis` string is shown in the data notes.
- Breadth reports advancing/declining/unchanged counts and actual missing constituents. Strongest/weakest names are ranked by unweighted observed returns. No exact index-point contribution is invented without divisor/weight evidence.
- Spot-index volume, VWAP, executable quantities, option premiums and lot sizes are never fabricated. A future/options plan needs its own exact contract, market data, costs and risk checks.

`READY` means the implemented report's required and optional context passed its checks; it does not mean a trade is ready. `PARTIAL` means usable index analysis with declared gaps (including incomplete intraday bars). `BLOCKED` means the index identity or its previous session could not be verified. Combined reports retain blocked indices and show requested/available/blocked counts.

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
