# Why did the market move? (`market_movers.py`)

One command answers "why is the market up/down today?" and "what moved the market on <date>?" from free public data.
It explains price moves after the fact; it never predicts the next move or gives buy/sell advice.

```text
python scripts/market_movers.py --summary                  # today, live (or the latest session before the open)
python scripts/market_movers.py --summary --premise up     # corrects the user's premise if the market is actually down
python scripts/market_movers.py --session 2026-11-17       # a finished session from NSE's official files
python scripts/market_movers.py --summary --flow-premise selling   # checks "FIIs are selling" against NSE and NSDL flows
python scripts/market_movers.py --sector IT --summary      # one sector: every constituent, breadth, rank, cause headlines
python scripts/market_movers.py --period week --summary    # this week / month / ytd, or --since YYYY-MM-DD
python scripts/market_movers.py --period month --summary --indices midcaps smallcaps   # the named indices lead
python scripts/market_movers.py --format json --out reports/movers-new.json
```

## What it reports, in order

On a weekend or NSE holiday every report (the movers summary, `--sector`, `--period`) starts with daily.py's banner:
"**Market closed today (Sat 21 Nov 2026: weekend) — figures are from the last session, Fri 20 Nov 2026.** Next trading
day: Mon 23 Nov 2026." (NSE holiday list; without it only a weekend is recognised).

1. **Direction first.** "Nifty 50 is down -129.00 (-0.51%) at 25,258.40 at 09:52 IST (NSE live)." With `--premise up|down`
   the first line becomes "Correction: the market is actually down, not up." whenever the user's assumption is wrong,
   so the answer never explains a move that did not happen. The next line gives the last session's direction from NSE's
   dated index file, for a user who meant yesterday: "If you meant the last session: Nifty 50 closed **up** +96.30
   (+0.38%) at 25,387.40 on Tue 17 Nov 2026."
2. **Breadth.** Nifty 50 advances/declines (NSE live snapshot, or the session's bhavcopy) and the Nifty 500's advances,
   declines and unchanged. (The snapshot's top-level advances/declines add up every index's counts - about 2,300 up and
   7,200 down on 18 Nov 2026 - so they are not a count of securities and are not printed; the JSON keeps them as
   `index_breadth_totals` with that label.)
3. **Sectors.** Seventeen NSE sector indices (Auto, Bank, Financial Services, FMCG, IT, Media, Metal, Pharma, PSU Bank,
   Private Bank, Realty, Healthcare, Consumer Durables, Oil & Gas, Energy, Cement, Chemicals) sorted by % change.
4. **Index-point contributors (estimated).** The five biggest drags and lifts in Nifty 50 points, each with its % change
   and estimated weight, plus the sum over all 50 stocks next to the actual index change. Both times are labelled: the
   check compares the estimates with Yahoo's `^NSEI` at the time of the stock prices (one Yahoo snapshot) and names the
   NSE time of the lead figure, e.g. "Check: these estimates add up to -156.7 points vs Nifty's -155.7 on Yahoo at
   10:51 IST, the time of the stock prices (the -154.05 above is NSE at 10:49 IST)".
5. **Top Nifty 50 gainers and losers** by % change.
6. **India VIX** level and change - after the close NSE's official close from the daily index report once it is
   published (18 Nov 2026: 15.57, +0.29%, against the 15:30 snapshot's 15.56); **Sensex** (Yahoo, unofficial) when Yahoo
   has today's value, its change measured against the close of Yahoo's previous daily candle, which must be the same
   session as NSE's previous close (Yahoo's own `chartPreviousClose` pointed two sessions back on 18 Nov 2026 and turned
   a 500-point fall into -72.99).
7. **FII/DII** net values from NSE's provisional endpoint, dated (today's flows appear only after the close). NSE serves
   only the latest day, so `--session` for an older day says the flows for that session are not available instead of
   printing the latest day's. **FPI month to date** from NSDL's daily FPI report
   (`https://www.fpi.nsdl.co.in/web/Reports/Monthly.aspx`, equity via the stock exchanges): the month's net, the last
   five reports and how many were net buying. Each NSDL reporting date covers trades of the previous trading day, so it
   lags and differs from NSE's provisional figure; both are named and dated. With `--flow-premise selling|buying` a line
   checks the user's claim: "Correction: FIIs were net **buyers**, not sellers - FII/FPI net +₹4,195.38 cr on 17 Nov
   (NSE provisional); FPI equity +₹1,00,365.32 cr month to date (NSDL, to the 18 Nov report)."
   When the stock-level contributions cannot be collected (NSE refusing a download), the report says so:
   "⚠ stock contributions not collected — rerun (NSE's previous-day bhavcopy ... could not be downloaded after retries)".
8. **Headlines to check** (`--summary` prints the top three; the full report up to five with links; `--no-news` skips):
   dated market headlines from the ET Markets RSS feed merged with Google News search RSS (`Sensex Nifty when:1d`,
   India edition), Indian-market headlines first; within them, headlines that give a cause ("falls as gold crashes",
   "slips after downgrade", "why is the market falling today?") rank before newer ones that only report the move, then
   newest first. Pre-market "outlook/prediction" pieces are dropped, and so are multi-day stories that run against
   today's move ("Nifty rallies 13% as FIIs pour Rs 88,300 cr" on a falling day: a big index % or wording such as "this
   month", "in a week" marks a retrospective story). Google News is also the fallback when ET Markets is down. For the
   two biggest movers in the direction of the move, up to two Google News headlines naming the company are added, causal
   wording first (the second only when it also gives a cause and is a different story), for example `TITAN: "Titan,
   Kalyan Jewellers shares fall up to 4% as gold, silver prices crash" (Moneycontrol.com, 18 Nov 11:59 IST)`; corporate
   housekeeping (ESOP/stock-option grants, AGM/EGM notices, board-meeting intimations, trading-window closures) is
   dropped. All are labelled **unverified possible causes**, never confirmed reasons. Google offers
   this feed for personal feed readers; only titles, publishers, times and links are shown, never article text.
   On a weekend or holiday (no session has opened since the latest one) the searches reach back to the close of the
   session before it (`when:2d`/`when:3d`..., at most a week), as on that session's evening; the wider window would bring
   in the earlier session's evening wrap-ups ("Nifty Auto surges 4.4% as Maruti jumps", published the evening before),
   so headlines published before the session opened that report an Indian index, sector or stock move are dropped
   (overnight global and commodity moves and news without a move stay), and so are index stories running against the
   session's close ("Sensex falls 300 pts" at 09:22 on a day that closed up). When no headline could be collected the
   summary says "Headlines not collected (reason)"; when the search ran but nothing qualified it says so as a gap.
9. **Mood context** (every summary; the full report has it as a section): Nifty 50 P/E, P/B and dividend yield from
   NSE's `ind_close_all` file of the session (the latest published one during market hours), against their range over the
   past year from NSE's file of each month's last session (kept on disk, so only the first run downloads them); the
   Nifty 50 change over one month (against the close one calendar month earlier); India VIX against its 52-week range
   (NSE's allIndices yearHigh/yearLow, else Yahoo's one-year daily closes, labelled). Plus **all NSE shares** advancing,
   declining and unchanged from the session's bhavcopy (EQ series) and, for a finished session, NSE's own Nifty 500
   count from the snapshot (or its members in the bhavcopy). Context, not a forecast: "P/E 21.02 vs its past-year range
   19.30 (Oct 2026) to 22.81 (Nov 2025)". The NSDL month-to-date line is printed in every summary (or "not collected").

## Sources

| Item | Live mode | Session mode (`--session`) |
| --- | --- | --- |
| Index, sector and VIX levels | NSE allIndices snapshot (`www.nseindia.com/api/allIndices`, minute timestamp) | NSE `ind_close_all_DDMMYYYY.csv` |
| Nifty 50 breadth | NSE snapshot's NIFTY 50 advances/declines | NSE bhavcopy for the session |
| Constituent prices | Yahoo `meta.regularMarketPrice` vs NSE bhavcopy previous close (official) | NSE bhavcopy close vs previous close |
| Constituent list | NSE `ind_nifty50list.csv` (current list; older membership not verified) | same |
| Weights | estimated, anchored to the NSE Indices factsheet (`assets/index_weights.json`), see below | same (factsheet anchors apply only to sessions on or after the factsheet date) |
| FII/DII | NSE `fiidiiTradeReact` (latest published date) | only when that date is the session; otherwise stated as unavailable |
| FPI month to date | NSDL `Monthly.aspx` (daily FPI equity by reporting date, current month) | the report carrying that session's trades |
| Headlines | ET Markets RSS + Google News search RSS (`news.google.com/rss/search?q=...&hl=en-IN&gl=IN&ceid=IN:en`, one exact URL shape) | same |

NSE's JSON API answers scripts on `www.nseindia.in` (the `.com` host sends a 301 there and redirects are refused), so
requests go to the exact `.in` twin of an allowlisted `.com` URL; reports cite the `www.nseindia.com` address. Every
request uses a fixed allowlist, no redirects, a size limit and a timeout. Yahoo charts are fetched at most four at a time.

Previous-day bhavcopy: NSE's archive host sometimes refuses a download that succeeds a second later. The bhavcopy is
retried twice (after 1 and 2.5 seconds), and once a past day's file has been read it is kept in the cache folder
(`nse_bhavcopy/`, beside `nse_ind_close_all/`; `INDIA_DAY_TRADER_CACHE=off` disables it) because a published bhavcopy
never changes, so later runs need no download at all. NSE's evening snapshot also relabels `previousDay` as today while
`previousClose` is still the previous session's close (seen from about 17:30 IST on 18 Nov 2026); such a date is
ignored and the previous session comes from NSE's holiday calendar, so the reports never compare today with itself.

Constituent charts: the five-minute charts used for the weights are kept per session once that session is over
(`yahoo_5m/`), so a run on a weekend or holiday about the last session reads them back instead of downloading 51 charts
again (on Sat 21 Nov 2026 that took 3 s instead of timing out). Downloads that stall are cut off (a whole NSE or news
download may take at most three per-read timeouts; a main-thread Yahoo call at most 25 s) and the scripts exit as soon
as the report is printed, so a slow source is a stated gap, never a hung command.

## One sector (`--sector`)

`--sector IT` (or `METAL`, `"PSU BANK"`, `"IT stocks"`, `"metal stocks"`, `"real estate"` ...) lists every constituent of
that NSE sector index with its % change, sorted, plus advances/declines (computed and NSE's own count), the sector's
change and its rank among the seventeen NSE sector indices, and the strongest/weakest sector for context.

| Item | Source |
| --- | --- |
| Constituents | NSE `ind_<index>list.csv` (the files the screener uses, e.g. `ind_niftyitlist.csv`, `ind_niftymetallist.csv`, `ind_nifty_privatebanklist.csv`, `ind_niftyfinancelist.csv`), kept in the cache folder (`nse_index_lists/`) for up to 7 days; an older copy is used, and labelled, if the download fails |
| Stock moves after the evening bhavcopy, or `--session` | NSE bhavcopy for that day: official close vs previous close |
| Stock moves during the session and after the close until the bhavcopy | Yahoo `meta.regularMarketPrice` (unofficial) vs NSE's previous-day bhavcopy close; the line says so |
| Sector level and rank | NSE allIndices snapshot (or `ind_close_all` for a past session) |
| Headlines | Google News RSS, since the previous close: `<sector> stocks when:1d`, `Nifty <sector> when:1d`, the commodity behind the sector where one exists (`copper price` for metals, `crude oil price` for oil & gas and energy), and the two biggest movers' own news. On a weekend or holiday about the latest session: since the close of the session before it (`when:Nd`), the earlier session's evening wrap-ups dropped. For an older session: "Headlines not collected (... a past session ...)" |

Headlines are kept when they name the sector, one of its stocks or a sector driver (copper/aluminium/zinc/steel/China
for metals, the rupee/Accenture/US tech spending for IT, the USFDA/tariffs for pharma, the RBI/deposits/NIMs for banks,
...), report a move or give a cause on the side of the sector's move, and were published since the last close; a
single other company's stock ("Why is Russel Metals stock sliding today?") and another country's market are dropped. The
sector counts as named only as stocks or an index ("metal stocks", "Nifty Metal", "IT shares", "PSU banks"), not as a
bare commodity word ("metal prices", "red metal"); a driver-only headline that is really another company's results
("KGHM beats profit estimates on high metal prices"), another group of stocks ("AC stocks jump as copper prices hit a
7-month low") or a foreign-listed company ("(NYSE:AA) ... Alcoa") is dropped, and so is a mover's story that runs
against its move today and quotes a bigger move ("yet down nearly 19%" on a +1.5% day). Across all headline lines,
forecasts ("copper will hit $15,500/t in 2027"), trading tips ("SL 800 Tgt 828") and posts on social networks (LinkedIn,
Facebook, X, YouTube) are never offered as causes. An
explanation of the sector's move comes first, then the best driver headline, then the movers' causes. Cement and
Chemicals have no public constituent file, so they are not offered.

## Over a period (`--period`, `--since`)

`--period week|month|ytd` or `--since YYYY-MM-DD` gives Nifty 50, Bank Nifty, Next 50, Midcap 150, Smallcap 250,
Nifty 500, Midcap 100, Smallcap 100 and every sector index over the period, each with its gap to Nifty 50 in percentage
points, plus Sensex from Yahoo's daily closes (unofficial). The base is the last session before the period: "week" =
the previous week's last close (normally Friday's; the session before it after a holiday), "month" = the previous
month's last close, "ytd" = the previous year's last close, `--since D` = D's own close (or the session before D if D
was not one). Base and end closes come from NSE's official `ind_close_all` files (cached on disk); when today's file is
not out yet the end is NSE's live or 15:30 closing level, and the output says which. allIndices' `oneWeekAgo` field is a
calendar week (Wednesday to Wednesday), not "this week", so it is not used. Check on 18 Nov 2026 (Fri 13 Nov close to
Wed 18 Nov close): Nifty 50 +0.33%, Midcap 150 -1.28%, Smallcap 250 -1.52%.

The answer leads with Nifty 50, and every other index and sector in the summary carries its gap to Nifty 50 ("Nifty Bank
+18.19% (+6.27 pp)"). With `--indices` (natural names: `midcaps`, `smallcap 250`, `next 50`, `nifty 500`, `bank`, `IT`,
`sensex`, ...) the named indices lead instead, each with its gap, and Nifty 50 follows "for comparison": "**This month**
(Fri 30 Oct close to Fri 20 Nov close): Nifty Midcap 150 +8.09% (-3.83 pp vs Nifty 50); Nifty Smallcap 250 +6.09%
(-5.83 pp vs Nifty 50). For comparison, Nifty 50 +11.92% (23,053.10 to 25,800.60)."

## How contributions are estimated (method and accuracy)

Point contribution of a stock = weight at the previous close x its % change x the previous Nifty 50 close.

NSE no longer serves constituent free-float market caps to scripts (`www.nseindia.in/api/equity-stockIndices?index=NIFTY%2050`
and its `.com` twin both return 404, checked 18 Nov 2026), and the public constituent list has no weights. The official
figures that are public are the monthly NSE Indices factsheet's **top-10 constituent weights** and **sector weights**
(`https://www.niftyindices.com/Factsheet/ind_nifty50.pdf`; niftyindices.com answers scripts with an HTML page, so the
factsheet is not fetched at run time). They ship, dated and sourced, in `assets/index_weights.json` together with NSE's
closes on the factsheet date (bhavcopy and `ind_close_all`). The script **estimates** the weights from them:

1. Fixed anchors: each top-10 stock's factsheet weight, rolled from the factsheet date to the previous close by its price
   change relative to the index (a free-float weight moves with price). Example, 30 Oct -> 17 Nov 2026: Infosys 3.92% ->
   3.40% (it lagged a 10% Nifty rally), Bajaj Finance 2.40% -> 2.61%, HDFC Bank 9.95% -> 10.64%. A ratio outside 0.6-1.6
   (a split or bonus) drops that anchor. These rolled-forward weights are kept exactly as computed (factsheet weight x
   price ratio), so the same past close always gives the same figure; only the fitted remainder is scaled.
2. Download about one month of 5-minute Yahoo charts for the 50 constituents and `^NSEI` (51 requests, about 5-15 s) and
   fit the index move left after the fixed stocks to the other 40 stocks with non-negative least squares (about 1,400
   bars; overnight gaps excluded).
3. Sector budgets: each factsheet sector weight minus its top-10 stocks is split among the sector's other stocks in the
   fitted proportions, then rolled to the previous close. Example: Consumer Durables 2.70% = Titan + Asian Paints, so the
   fit can no longer give Titan more than the whole sector (before anchoring it fitted Titan at 2.1%; anchored 1.9%).
   Sector budgets apply only when the current `ind_nifty50list.csv` membership equals the file's `members` list.
4. Scale the fitted remainder so all weights add to 100% (the fixed anchors are not rescaled). Without a usable file (missing, malformed, more than 100 days old, or a session before its date) the
   script falls back to the pure fit of all 50 stocks, and the data notes say which method ran.

The data notes print the method with the factsheet date and each top-10 weight as "factsheet % -> estimated %".
Refresh `assets/index_weights.json` after each month-end factsheet (the file's `note` lists the steps).

Accuracy: on live data (18 Nov 2026, 13:17 IST) the anchored weights had R² 0.995 on the month of 5-minute bars, and the
50 estimated contributions summed to -196.2 points against Yahoo's -194.8 at the same moment (pure fit: -238.7 vs -235.9
earlier in the session). The report always prints the estimated total next to the actual change so the error is visible.
The split inside a sector is still a fit: treat a single stock's weight as accurate to a few tenths of a percentage point.
Weights for an old session use the current month's fit and current membership, so treat them as approximate. If fewer
than 90% of constituent charts load, contributions are omitted and only % gainers/losers are shown.

## Drivers in the daily briefings

`daily.py --collect-public` reuses this machinery during and after the session (`market_movers.live_drivers`): one
line names the weakest/strongest NSE sectors and the three biggest estimated Nifty 50 point drags and lifts; Bank Nifty
reports list its five heaviest members with % change and estimated Bank Nifty points, plus the PSU/Private Bank
sub-indices. Bank Nifty weights are anchored the same way to the NSE Indices **Nifty Bank** factsheet of 30 Oct 2026
(the `NIFTY BANK` block of `assets/index_weights.json`: top-10 weights HDFCBANK 18.18%, ICICIBANK 14.99%, KOTAKBANK
11.28%, AXISBANK 9.97%, SBIN 9.94%, ...; one sector, Financial Services 100%; members from `ind_niftybanklist.csv`), so
only the four smallest members' split is fitted; the data notes print "Nifty Bank factsheet weights (30 Oct 2026) ->
estimated at the 17 Nov close". During and after the session the briefings also add **Possible reasons (headlines,
unverified)** (up to three dated headlines from the same filters, each top mover's causal headline first) and **Cues**
(the newest dated headline since the last close on GIFT Nifty, US markets/futures, gold, crude and the rupee), searched
in parallel within about 9 seconds; a search that fails or runs out of time is printed as a gap. After the close the cues
look ahead to the next session (GIFT Nifty since today's close, the rest since today's open).

After 15:30 IST the movers summary says the index **closed** up/down (NSE closing level), not that it "is" moving
live, and adds a broader-market line from the same snapshot: "Broader market (NSE, 18 Nov): Nifty Midcap 150 -1.14%,
Nifty Smallcap 250 -0.49%; Nifty 500 stocks 125 up, 374 down, 2 unchanged." For the two sectors leading the move
(weakest on a falling day), when no headline shown already names the sector, one Google News search (`metal stocks
when:1d`) keeps at most one headline that names the sector, gives a cause or explanation, runs with the sector's move
and was published since the last close, e.g. `Nifty Metal: "Why Did Metal Stocks Fall by Up to 7% in Today's Trading
Session?"`.

Yahoo 5-minute charts: the first run of a day downloads the month and saves the completed past days (never today's
forming bars) in the cache folder (`yahoo_5m/`); later runs that day download only today's bars and join them on. After 16:00 IST today's chart is final (Yahoo's last
price is the close), so it is kept whole and later post-close runs that day read every constituent chart without a download.
Each chart gets an 8-second timeout with at most one quick retry, and all charts together a 20-second budget. Sensex reports use the factsheet-anchored Nifty 50 weights of the
30 Sensex members identified from Sensex's own 5-minute moves, rescaled to 100%. Point
figures are shown only when the fit has R² ≥ 0.9 and the estimates explain the actual move within 25% (or 10 points);
otherwise only the sectors and % changes are shown.

## Limits

- Yahoo constituent prices can lag NSE by seconds to minutes; Sensex on Yahoo lags by about 15 minutes during the session.
- FII/DII values are provisional and dated; they are not today's flows during market hours. NSDL's FPI figures are by
  reporting date (previous day's trades) and differ from NSE's provisional ones.
- Cue headlines are filtered to India's markets: the Indian rupee (a "rupee" rate far above USD/INR, or a publisher based
  in Pakistan, Sri Lanka, Nepal or Bangladesh, marks another country's rupee), COMEX/MCX/Indian gold (not Thai baht or
  Gulf retail prices) and Brent/WTI crude (not Oman or Dubai crude); among the rest, Indian business media and the major
  wires are preferred over a newer headline from elsewhere.
- Headlines are not verified causes. The tool does not attribute the move to news, and never predicts.
