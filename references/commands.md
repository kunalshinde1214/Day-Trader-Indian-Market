# Commands and data contracts

Daily briefings now support `/india-daily`, `/nifty`, `/sensex` and `/banknifty` through `scripts/daily.py`. See [daily reports](daily.md) for one-command Kite collection and offline examples. For broker trade imports, provisional-fee reserves, supervised polling and opt-in webhook delivery, see [account/service operations](account-service.md).

Run from the skill directory with Python 3.10+. The analysis CLI remains offline. Broker adapters, explicitly enabled watcher polling, public collectors, acceptance probes and margin proposals can make bounded read-only network requests. All filenames below are relative to `--workdir`. `init` writes example config and a broker-matching charge card/template and refuses to replace either. `demo` requires an empty/new output directory. Analysis output files are atomically replaced on rerun; inputs are preserved. See [broker setup and schema-v2 migration](brokers.md).

```powershell
python scripts/trader.py init --workdir trader
python scripts/trader.py demo --broker kite --workdir demo-kite-v2
python scripts/trader.py demo --broker upstox --workdir demo-upstox-v2
python scripts/trader.py demo --broker dhan --workdir demo-dhan-v2
python scripts/trader.py demo --broker kite --setup vwap_reclaim --workdir demo-vwap-kite
python -m unittest discover -s tests -v
```

The generated demo directory is a complete, inspectable schema example with a fake `DEMO-EQ` instrument. Its dated snapshots cannot pass today's checks. Do not change their timestamps to make them look current. Collect real data instead.

Select `config.setup` (`orb`, `vwap_reclaim`, `gap_and_go`, `breakout_20d`, `bull_flag`, `oversold_bounce`; absent means ORB). `--setup` is available for init/demo only. Regenerate plan/replay/order evidence after changing it. See [exact rules and examples](setups.md).

## Stage commands

```text
python scripts/trader.py scan --workdir trader
python scripts/trader.py entry --workdir trader
python scripts/trader.py plan --workdir trader
python scripts/trader.py replay --workdir trader
python scripts/trader.py size --workdir trader
python scripts/trader.py risk --workdir trader
python scripts/trader.py ticket --workdir trader --index 0
python scripts/trader.py charges --workdir trader --index 0
python scripts/trader.py alerts --workdir trader
```

For an explicitly historical analysis, add an offset-bearing clock such as `--now 2026-10-30T09:35:00+05:30`. This does not authorize live trading or make old data current. Return code 0 means the analysis command completed; code 2 means blocked/invalid. A sizing result is a candidate: derivative margin validation happens during risk and ticket. The ticket recalculates risk, rather than trusting a previously saved PASS.

| File | Producer / shape |
| --- | --- |
| `config.json`, `charges.json` | `init`; examples copied from `assets/` |
| `instrument.json` | Exact normalized instrument selected from broker master |
| `universe.json` | `{source, as_of, scope, instruments:[instrument + average_daily_turnover_inr]}`; deduplicated F&O/Nifty500 selection or explicitly named subset |
| `candles.json` | `{source, as_of, instruments:{"EXCHANGE:SYMBOL":{source, as_of, bars:[...]}}}` |
| `context.json` | Current sourced session, restrictions, event, account and quote snapshots below |
| `watchlist.json` | `scan`; coverage, exclusions, rule matches, trigger/expiry state |
| `entry.json` | `entry`; selected setup state |
| `plan.json` | `plan`; broker/account, setup/rule version, instrument identity, side, entry/stop/target, normalized MIS product, trigger timestamp, flat time |
| `history.json` | Read-only broker candles or normalized import; broker, exact instrument, source/as_of, prior 5-minute candles |
| `replay.json` | `replay`; plan/history hashes, sample, frequencies, historical selected-setup results, limitations |
| `orders.json`, `payload.json` | `size`; one candidate plus selected-broker entry payload for margin estimation |
| `state/risk.json` | `risk`; sequential PASS/BLOCK results |
| `ticket.json` | `ticket`; one paper preview, recomputed risk and context fingerprint |

Instrument fields: `broker` (`kite/upstox/dhan/angel/fyers`), `symbol`, `exchange` (`NSE/BSE/NFO/BFO`), `token` (broker-specific instrument ID string), `kind` (`equity/future/option`), positive `tick_size` in rupees, integer `lot_size`, `master_date` (`YYYY-MM-DD`), and derivative `expiry` plus `underlying_type` (`stock/index`). Kite dumps do not fully classify stock versus index derivatives for this schema; verify it before use. No security token is a broker authentication token. Config includes the observed broker `account_id`; new plans include both broker and account identity. Older plans must be regenerated. Dhan candle requests additionally require `broker_instrument` from the master, and its canonical symbols include security IDs; see [Dhan normalization](dhan.md). Angel uses exchange/token quote keys, explicit device headers and offset-bearing five-minute bounds; see [Angel normalization](angel.md). FYERS takes full NSE/BSE ticker strings for quotes, retains numeric fyToken identity, and uses depth last-trade time; see [FYERS normalization](fyers.md).

Bars: `{at, open, high, low, close, volume}`. `at` is the start, with timezone; candles must start on a 5-minute grid. All prices must be finite and positive, volume nonnegative, OHLC internally consistent. ORB uses the first completed close beyond the high/low of 09:15, 09:20 and 09:25; other setups follow their documented rules. No 09:30 bar is complete until 09:35. Missing intervening bars, duplicates and malformed candles are errors. Turnover from close×volume is labeled an estimate, not exchange-exact turnover.

## Context evidence

Each snapshot has `source` and offset-aware `as_of` indicating when that status was actually checked. Defaults require freshness within 60 seconds and reject future timestamps. A recent fetch timestamp alone must not disguise stale exchange or quote content. Retain actual release/quote timestamps and source links in imported records. Never fill missing status with `false` or mark a partial ledger reconciled.

- `session`: `date`, boolean `is_open_day`, boolean `halted`, `open`, `close`, `broker_squareoff` (HH:MM IST); `special_session: true` only with verified weekend-session evidence. Use the correct exchange segment. A Muhurat exception also needs explicit strategy time-window changes; it is not automatically enabled.
- `restrictions`: `instruments` map keyed `EXCHANGE:SYMBOL`. Each entry must explicitly record booleans `t2t`, `asm`, `gsm`, `ban`, `suspended`, `corporate_action`, `results_due`. Any true/unknown value blocks new entries. Sources are exchange notices and broker restrictions; master membership alone is insufficient.
- `event`: `date`, `risk_multiplier` (`0`, `0.5`, `1`), `events`. `0` blocks, `1` is normal; `0.5` requires `override_confirmed: true` after an explicit user instruction. Preserve the user's words and supporting event sources alongside the record. The CLI cannot prove who edited a local JSON file.
- `account`: `broker`, `account_id`, `date`, `reconciled`, `cash_available`, `margin_available`, `daily_risk_used`, `trades_today`, `orders_today`, `position_values` map and `seen_entries` array of `[exchange,symbol,side]`. Identity must match config and plan. Include pending orders in reservations. `daily_risk_used` conservatively sums each filled/reserved entry's full cost-inclusive planned loss, increased to actual loss when worse; profitable trades never free that day's risk budget. Use the [journal reconciler](operations.md) with a complete canonical snapshot and explicit reservations; raw broker order/fill normalization remains required. Reconcile against broker evidence before setting true.
- `quote`: `symbol`, `exchange`, `price`, `source`, `as_of` (the quote's time). A quote beyond the stop or target expires the plan.
- `margin` (derivatives): `{source:"kite" / "upstox" / "dhan" / "angel" / "fyers", account_id, as_of, payload_hash, required_inr}` from the selected broker's read-only margin command for this exact quantity/payload and account. Do not multiply a one-lot margin guess. A failed margin check requires a newly proposed quantity and full reevaluation, not auto-editing a blocked order.
- `replay`: loaded from `replay.json` by CLI; must match the exact plan, configured path count and minimum history. Multi-plan programmatic calls can supply `replays` keyed by plan hash, `quotes` keyed by instrument, and `margins` keyed by payload hash, with the singular fields as defaults.

The risk command can read an optional `instruments.json` map for batches. It checks entries in order and reserves cash, margin, risk, position value and counts after every PASS; it does not mutate the real account or assume fills. These local snapshots are audit inputs, not a tamper-proof execution authorization system.

## Replay and costs

Replay needs at least 20 complete prior sessions from 09:15 up to the configured flat time for the exact instrument. Same-day/future data cannot enter the pool. Historical matching uses the selected setup, same side, stop buffer and target multiple, but is a rule replay rather than a reconstructed account or full eligibility backtest. Open jumps through a stop use the worse open price; bars touching both levels count stop first.

Set `replay_sampling` in the broker config to `independent` (unchanged default), `block`, or `session`. Block sampling draws consecutive same-clock bars from one historical session, then chooses another session at the next block boundary. `replay_block_bars` defaults to 6 and accepts integers 1–375 only in block mode; broker bars are five minutes. Session sampling retains one historical session for the entire remaining path. Blocks truncate at the horizon without wrapping. Use an integer `replay_seed` for reproducibility. Risk checks bind the sampling settings and seed; regenerate older reports without sampling metadata before sizing.

Public research replay offers the same modes through `python scripts/public_market.py replay --input bundle.json --sampling block --block-bars 6` or `--sampling session`. A block is measured in the input chart's one- or five-minute bars. Public replay remains ineligible for execution and requires at least five complete prior sessions. Independent draws lose serial dependence; blocks retain it only within each block; whole-session paths repeat a finite set of historical tails. More draws do not create additional historical evidence. None of these modes models future regimes, liquidity, slippage or pre-open gaps, or produces calibrated probabilities.

Each simulated path is charged for one reference lot, with an average cost reported. Actual order stop/target costs are recalculated after sizing. This is not a profit forecast, and a reference-lot cost is not the cost of an arbitrary final position. Taxes on income, exercise/assignment, delivery and slippage are excluded. Rate fields are decimal fractions, and output money is rounded to paise.

Cash entries assume no leverage. Derivatives also retain conservative premium/notional caps before exact margin validation; this may yield zero lots for futures under the example budget. Do not lift limits silently to manufacture a tradable order.

## Other helpers

`core.reducing_exit(position, side, quantity)` produces a reduction-only paper description, bypassing entry gates and rejecting reversal/oversizing. It does not execute. `alerts` checks once, returns messages and stops. For bounded polling, persistent local alerts, fee-audited journals, ingestion, expiry/flow diagnostics, exact margin proposals and acceptance probes, read [operations](operations.md). No external notifications or actual exits are sent.
