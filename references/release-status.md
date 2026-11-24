# Release status

Current version: **1.5.1**, published 24 November 2026: the 1.5.0 features with corrected documentation and release manifest. This is the one place for what the current release contains, how it was checked and what that checking does not establish. Per-version detail is in the [changelog](../CHANGELOG.md).

## What 1.5.1 contains

**12 plugin skills:** the daily commands `nifty`, `sensex`, `banknifty` and `india-daily`, plus `market`, `stock`, `screener`, `fno`, `portfolio`, `trade-plan`, `news` and `broker`. See the [command reference](commands.md).

| Area | What is implemented | Guide |
| --- | --- | --- |
| Market moves | Direction, sectors, index drivers in estimated points (checked against the official change), breadth, VIX, FII/DII and NSDL flows, dated headlines and cues; one sector's stocks; indices over a period; a mood view; weekend and holiday reports | [market movers](market-movers.md) |
| Daily briefings | Nifty, Sensex, Bank Nifty, India Daily: live NSE levels in market hours, close and closed-day modes, next-session levels | [daily reports](daily.md) |
| Stock research | Ratios cross-checked with NSE consolidated filings, P/E history adjusted for bonuses and splits, one-offs stripped, 3-year growth, business peers, sum of parts, side-by-side compare, news | [public workflows](public-workflows.md) |
| Shareholding | Pattern, promoter pledge and encumbrance trend, SAST filings, fund houses matched across quarters | [shareholding](shareholding.md) |
| Screener | Index universes, fundamental and momentum filters, dividend preset, sector exclusion | [screener](screener.md) |
| Portfolio review | P&L, XIRR, concentration, bonuses, splits and demergers, capital-gains estimates under the Income-tax Act 2025 | [holdings review](holdings-review.md) |
| F&O analytics | Chain, OI, PCR, max pain, IV and Greeks, payoffs, strike grids, indicative SPAN + exposure margin | [F&O](fno.md) |
| Paper planning | Scanners, opening-range plans, replay, position sizing with Indian charges, risk checks | [setups](setups.md) |
| Broker workflows | Broker connectors and five built-in adapters; human-operated execution and protection, disabled by default | [broker connectors](broker-connectors.md), [advanced tools](advanced.md) |

## How it was checked

- **Automated tests:** 1,883 offline tests pass on Python 3.10, 3.12 and 3.13 in GitHub CI, and on 3.11, 3.12 and 3.13 locally with network access blocked. They use recorded and simulated exchange and broker responses, including failure cases. Mutation checks confirmed the new logic in 1.5.0 is covered by tests; 1.5.1 changes documentation and the release manifest only.
- **Packaging:** `python scripts/release.py --check` reruns the suite and checks Python syntax, JSON, local links, plugin manifests and self-contained SVGs. The ZIP is built from an explicit allowlist and carries a SHA-256 manifest of every file; private outputs, credentials, local state and dependencies are excluded.
- **Answer scoring:** separate AI reviewer agents answered everyday questions by following the skill, scored each answer out of 10 against a fixed rubric (correctness, completeness, clarity, honesty, friction, comparison with the best free alternative), and spot-checked figures against NSE files, company filings, Screener.in and a broker margin calculator.

  | Round | Questions | Average before that round's fixes |
  | --- | --- | --- |
  | 1–6 | The same 15 questions, used during development | 6.5 at the start; about 9 by round 6 |
  | 7 | 15 questions not used during development | 6.8 market · 7.2 stocks · 7.6 portfolio and F&O |
  | 8 | 15 more unseen questions | 8.4 market · 7.8 stocks · 7.6 portfolio and F&O |

  Fixes followed each round. The round-8 fixes were checked by the agents that made them, not re-scored independently, so the last independent figure is **about 8 out of 10 on unseen questions**. The reviewers were AI agents, not traders, and the rubric is this project's own.

## What this does not establish

- **Exchange coverage is partial.** Scripts read specific NSE public files and APIs. BSE notices and filings, special sessions, emergency circulars, concalls and macro events come from [web research](web-research.md) at request time: a cited search, not an automatic or comprehensive feed, and a search can miss things. BSE blocks scripts, so Sensex figures are estimates or Yahoo quotes.
- **Sources can change.** Live smoke runs against NSE, Yahoo and Google News succeeded during development; they do not establish that these sources will stay available or keep their formats.
- **Broker acceptance is not tested.** Broker adapters and execution paths are tested against recorded and simulated responses only. No publisher trading account was connected, no real order was submitted and no production service was activated. Passing tests does not establish that any broker accepts these workflows; each user supplies their own credentials and acceptance evidence ([user setup](user-setup.md), [acceptance evidence](acceptance-evidence.md)).
- **No performance claim.** Nothing here measures profitability. Research output is not investment advice.

## Known limits and upkeep

- Prices and some fundamentals come from Yahoo (unofficial, about 15 minutes delayed). Fundamentals are cross-checked with NSE filings where the skill can; it flags the disagreements it detects, not every error.
- Index-point drivers use estimated weights anchored to the monthly NSE Indices factsheet (`assets/index_weights.json`, ignored after 100 days).
- Indicative margin follows NSE Clearing's files and published exposure rules; broker figures differ, and the broker's basket margin is final.
- Kept up to date by hand: index weights (monthly), the NSE holiday list (yearly), tax and STT rates (each Budget), the bonus and split list, and curated peer, subsidiary and holding-vehicle lists.

Remaining engineering and optional enhancements are listed in the [completion status](completion-tracker.md).

## Distribution

Build with `python scripts/release.py --out dist/india-day-trader-1.5.1.zip`. The command refuses to overwrite and does not skip tests. The [1.5.1 release page](https://github.com/kunalshinde1214/Day-Trader-Indian-Market/releases/tag/v1.5.1) provides the ZIP and its `.sha256` checksum; see the [README](../README.md#python-cli) for how to verify it.

Original contributions are source-available, free for personal use; third-party MIT rights remain intact.

## Release history

| Version | Date | Summary |
| --- | --- | --- |
| 1.5.1 | 24 Nov 2026 | Documentation and release manifest brought up to date with 1.5.0; completion and coverage claims made precise |
| 1.5.0 | 21 Nov 2026 | Market moves, sectors, periods and mood; stock figures cross-checked with NSE filings; shareholding; after-close F&O and SPAN margin; weekend reports; 1,883 tests |
| 1.4.0 | 18 Nov 2026 | Daily commands restored, repeatable screens, renamed symbols, simple position sizer, weekly/monthly expiry labels |
| 1.3.0 | 18 Nov 2026 | 19 skills reduced to 8, answer-first `--summary` modes, broker material moved to advanced tools |
| 1.2.1 | 18 Nov 2026 | Trust fixes: fresh prices, bonuses and splits in the portfolio review, EPS cross-check |
| 1.2.0 | 18 Nov 2026 | Screener, portfolio review with capital gains, peers, P/E band, Hinglish answers |
| 1.1.0 | 17 Nov 2026 | F&O analytics |
| 1.0.0 | 17 Nov 2026 | Web research and broker connectors; 984 tests |
| 0.10.x and earlier | to 17 Nov 2026 | Scanners, replay, broker adapters, carry accounting, protection runners; see [0.10.1 scope](development-progress.md) and the [changelog](../CHANGELOG.md) |
