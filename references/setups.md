# Scanner rules and setup selection

Implemented: all six rules: `orb`, `vwap_reclaim`, `gap_and_go`, `breakout_20d`, `bull_flag`, `oversold_bounce`. Unknown setups fail explicitly. These are deterministic pattern rules for paper analysis, not recommendations or validated trading edges.

## Choose one setup

```text
python scripts/trader.py init --broker kite --setup vwap_reclaim --workdir trader-vwap
python scripts/trader.py demo --broker kite --setup vwap_reclaim --workdir demo-vwap-kite
python scripts/trader.py demo --broker fyers --setup vwap_reclaim --workdir demo-vwap-fyers
```

`--setup` is accepted only by `init` and `demo`. Subsequent scan, entry, plan, replay and risk stages use `config.setup`. An absent setting defaults to ORB for existing schema-v2 workspaces. To switch an existing workspace, edit `config.setup` and regenerate the plan, replay, sizing and risk evidence. A plan from the other setup cannot pass replay or sizing. Plans record `rule_version: 1`; legacy ORB plans without it remain readable. All other setups require an explicit supported version. Existing broker/account and exact-margin binding remain mandatory.

One invocation evaluates one selected setup. The first eligible signal of either side is retained for the day; later recrosses do not refresh its time. A SELL match can appear in the scan even when short entries are disabled; risk checks still enforce `allow_short`. `entry` reports `expired` after `plan_max_age_seconds`. Current quotes must also remain inside the planned stop and target before sizing/risk/ticket can pass.

## Shared candle checks

All six rules use completed five-minute bars from the regular 09:15–15:30 IST session. Require every bar from the session open through the latest completed interval at the analysis clock. Missing opening, intervening or trailing bars block evaluation even if the download timestamp is fresh. Duplicate timestamps, invalid grids, nonfinite values and inconsistent OHLC also block. Forming bars cannot trigger a signal. Bars are sorted and prior-day observations do not enter today's indicator.

Signal range, turnover estimate and level comparison use observations through the trigger only. Later bars cannot alter that evidence. The turnover estimate is the sum of bar close times bar volume, not actual exchange turnover. `percent_over_level` is a signed close-versus-reference percentage; it is negative for downward crosses. Special sessions with different opening times need a separate rule implementation; changing risk clock fields does not change the scanner's regular-session anchor.

## Opening-range breakout (`orb`)

The range is the first three bars, 09:15–09:30. The first subsequent completed close strictly above the range high produces BUY; strictly below the low produces SELL. A touch does not trigger. Stop reference is the range midpoint, shifted away from entry by `stop_buffer_ticks` and rounded outward to the instrument tick. Target uses `target_r`. ORB retains its original first-breakout behavior; the risk stage enforces the configured entry window.

## VWAP reclaim (`vwap_reclaim`)

The supplied specification names this setup without numeric trigger rules. Version 1 defines the following reproducible implementation choices:

1. Reset at 09:15. For each completed bar, calculate typical price `(high + low + close) / 3`. Session VWAP is cumulative typical-price × volume divided by cumulative volume. Every contributing bar through the trigger must have positive volume; a zero-volume bar blocks instead of implying a tradable crossover.
2. BUY requires the preceding close strictly below its own cumulative VWAP and the current close strictly above the current cumulative VWAP. SELL uses the mirror condition. Equality on either side does not trigger.
3. The trigger is the current bar's completion time, at or after both 09:30 and `no_entries_before`, and no later than `no_entries_after`. Earlier bars still contribute to VWAP. Keep the first eligible cross of either side.
4. Entry is that close rounded in the trade direction to the exact instrument tick. Stop reference is VWAP at the trigger, with the configured tick buffer applied away from entry and outward rounding. Target is the configured reward/risk multiple. Invalid stop/entry/target geometry blocks the plan.

This is a five-minute HLC3-volume approximation. It is not trade-by-trade exchange VWAP and may differ from broker charts using different source prices or intervals. The formula follows the published [TradingView VWAP calculation](https://www.tradingview.com/support/solutions/43000502018-volume-weighted-average-price-vwap/); the crossing, time and stop rules above are project choices, not a profitability claim or broker requirement.

For example, the synthetic demo closes below session VWAP before crossing above it at 09:35. It deliberately does not break the opening-range high. This checks that the chosen setup actually determines the signal and plan.

## Gap and go (`gap_and_go`)

Version 1 requires an absolute opening gap of at least `gap_min_pct` percent (default 0.7) from a sourced previous-session close. All first three bars must have positive volume. For an upward gap, every opening low stays strictly above that close and the 09:30 close is at or above today's open. Downward gaps use the mirror rule. A touch/fill of the gap disqualifies the setup. Trigger time is fixed at 09:30 and must be inside the configured window; it cannot be delayed by changing the window. The stop is the opening-range low for BUY or high for SELL, plus the outward tick buffer.

The candle record requires `prior_close` with exact `broker/exchange/symbol/token`, `for_session`, `previous_session`, positive `close`, `source`, `calendar_source`, `price_basis:"unadjusted_session_close"` and `corporate_actions_clear:true`. `available_at` must be offset-bearing, no earlier than the previous session's 15:30, and strictly before the target session's 09:15. The supplied prior-session relationship is explicit calendar evidence, not a guessed previous weekday or an independently verified calendar. Never substitute the last replay bar (which may end at 15:05) for an official prior close.

Plan hashes include the reference and gap threshold. Changing `gap_min_pct` invalidates plan/replay/sizing evidence. Historical inputs require `history.prior_closes`, keyed by every complete replay session's ISO date. A missing reference blocks replay rather than selectively removing that day.

## Daily-context rules

These three rules are long-only. They use the first completed intraday close strictly above a prior-session level, at/after 09:30 and inside the configured entry window. Stops use the minimum intraday low through the trigger, plus the configured outward buffer. This converts the upstream daily pattern ideas into explicit intraday triggers; it is not an assertion that unspecified source rules were already precise.

- `breakout_20d`: close above the maximum close of the preceding 20 complete sessions. This is a closing-high rule, not a high-wick rule.
- `bull_flag`: prior daily closes have a ten-session rise greater than 10%, measured from close[-16] to close[-6], followed by five closes whose max-minus-min range is less than 5% of the latest close. Trigger above the maximum daily high of those five flag sessions. Threshold equality does not qualify.
- `oversold_bounce`: prior-session Wilder RSI14 is below 30; trigger above that previous session's high. Wilder averages seed from the first 14 changes in the supplied series; all-up RSI is 100, all-down 0, and flat 50. Different history lengths can affect initialization, so preserve the exact input.

All require a `daily_reference` with exact instrument identity, target/prior sessions, source and calendar source, `price_basis:"unadjusted_session_ohlc"`, corporate-action clearance, and at least 30 ordered daily bars. An explicit `sessions` list must match bar dates without omissions or duplicates. Each daily bar has `date,available_at,open,high,low,close,volume`; its availability must be after its session close and before the target open. Historical replay requires `daily_references` keyed by every replay session, with no future evidence. The full reference is copied into the plan hash. See [reference builders](operations.md).

## Replay and verification

Replay still bootstraps the exact instrument's prior complete sessions at matching time slots. Its separate historical evaluation now calls the selected detector on each prior day, creates that setup's stop/target, and counts same-side first signals within the configured window. It does not silently use ORB firings for VWAP plans.

Output includes `historical_setup`, `historical_rule_version`, `historical_same_side_firings` and `historical_outcomes`. For ORB only, `historical_same_side_orb_firings` remains as a compatibility alias. Plan hashes include setup/version and setup-specific stop/reference evidence. History remains strictly before the plan date; same-day bars cannot increase the historical sample. These counts are rule evaluations, not an account simulation or an eligibility-adjusted backtest. Independent bootstrap draws do not reproduce the VWAP crossing process; they remain conditioned on an already-observed entry.

`tests/test_patterns.py` additionally covers all four newer setups, missing/late references, strict thresholds, history separation, and 20 broker/setup demo combinations. `tests/test_setups.py` covers hand-calculated/unequally weighted VWAP, both sides, equality, session reset, incomplete/zero-volume data, no later-bar influence, tick-aligned stops, version/config mismatches, historical setup separation, expiration, and full 10,000-path paper workflows for all five brokers. Tests use fictional inputs. See [commands](commands.md) for risk/context requirements and [coverage](requirements.md) for the unfinished platform features.
