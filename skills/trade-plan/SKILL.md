---
name: trade-plan
description: Plan a paper trade in Indian markets — entry check (opening-range breakout or a price level), stop and target, historical replay, position size in rupees and lots, and risk check. Use for "has RELIANCE broken its opening range", "where should my stop be", "backtest this setup", "how many shares for ₹2,000 risk", "is this within my limits".
---

# trade-plan

Read the [core skill](../../SKILL.md) first. Resolve scripts relative to `${CLAUDE_PLUGIN_ROOT}`. Run them with `python`; if that is not found use `python3` (macOS/Linux) or `py` (Windows). Keep user files outside the plugin folder, and pass the user's words as arguments, never as raw shell text.

## Quick answer: "how many shares?"

For an opening-range breakout question ("buy X if it breaks today's opening range, risk ₹2,000"), run `python "${CLAUDE_PLUGIN_ROOT}/scripts/public_market.py" entry --symbol SYMBOL.NS --setup orb --risk RUPEES` once: it reports the range, the trigger, the state, sizes for the midpoint and range-low stops with charges, the worst entry that still fits the risk, and a small-sample history of the setup on that stock. Quote the bold size: it is for a close one tick past the trigger (a confirming close cannot fill exactly at it; the at-trigger size is a reference column). Say plainly that a close further past the trigger raises the per-share risk, give the entry-window line ("entry window closes 14:30 IST (N min left)", or closed for today), and quote the exposure line; add `--capital RUPEES` when the user gives their account size, so the exposure and margin are checked against it. The history is a small sample, not a forecast.

After the close (15:30 IST) the same command prints a review headed "ENTRY WINDOW CLOSED": say plainly that the entry window has closed and nothing can be acted on today, then present it as what would have been — triggered at HH:MM (or not), the midpoint-stop size, how each stop or 2R target played out by the close, the setup history and the entry-window line. Yahoo often drops the last bars after the close; the review says where its data ends. To replay an earlier session, add `--now YYYY-MM-DDTHH:MM+05:30` with a date before today (network reads refuse `--now` for today).

If the entry command still prints `BLOCKED`, quote its `reason` to the user (for example which bars are missing), then fall back: `python "${CLAUDE_PLUGIN_ROOT}/scripts/public_market.py" chart --symbol SYMBOL.NS --interval 5m` for today's 09:15–09:30 bars (opening-range high and low, midpoint), then `position_size.py --entry RANGE_HIGH_PLUS_ONE_TICK --stop MIDPOINT --risk RUPEES` for the size. Label that as a manual reading of the public chart.

For any other sizing question, start here; it needs no broker setup: `python "${CLAUDE_PLUGIN_ROOT}/scripts/position_size.py" --entry PRICE --stop PRICE --risk RUPEES [--product BOTH|MIS|CNC] [--capital RUPEES] [--lot N] [--target PRICE]`. It returns the quantity whose loss at the stop, including Indian charges, fits the risk budget, plus exposure, capital or intraday margin needed, the result at the target and the price that covers charges. By default (`--product BOTH`) a buy is sized for delivery (CNC) and intraday (MIS) side by side in one run; a sell, or an F&O lot, is intraday only.

When the user names a stock, a percent stop or a percent risk, map the words straight onto flags and run once — do not look the price up separately:

- "Mere paas ₹3 lakh hai, 0.5% risk, Infosys pe 1.5% stop" → `position_size.py --symbol INFY --capital 300000 --risk-pct 0.5 --stop-pct 1.5`
- `--symbol` takes the entry from the stock's last close (the live price during market hours), labelled with its date; `--entry PRICE` overrides it.
- `--stop-pct N` puts the stop N% below the entry for a buy (above for a sell), on the ₹0.05 tick; `--risk-pct N` is N% of `--capital`.
- A stop at a recent low or high ("stop at the 20-day low", "swing low of the last 10 sessions") is `--stop-low N` for a buy or `--stop-high N` with `--side SELL`, with `--symbol`: e.g. `position_size.py --symbol INFY --capital 200000 --risk 1500 --stop-low 20`. It reads the daily bars, puts the stop at the N-session low (completed sessions only) and names its date. Repeat any ⚠ it prints: a low that is a one-bar wick (far below that day's open and close, measured in ATR — a single intraday spike, not where the stock has traded since) or a low that drops out of the window next session; each comes with the alternative stop and its size, so give both sizes and let the user choose. Never work the low out by hand.

Without `--target` the target is 2R and the output labels it "2R default; pass --target": quote it as a default, not as a recommendation, or leave it out when the user did not ask for one. Ask for the stop and risk if missing, and for capital when the exposure could exceed it. Say that intraday margin is an assumption the broker decides.

## Full paper pipeline

Use the pipeline below only when the user wants the full workflow with replay and account-bound risk checks. Steps run in this order and each writes a file the next one reads; any change to setup, interval, level or exit policy needs a new plan, replay and risk check ([commands](../../references/commands.md), [setups](../../references/setups.md)).

## Entry

Primary route: `python "${CLAUDE_PLUGIN_ROOT}/scripts/public_market.py" entry --symbol SYMBOL.NS --setup orb`.

Custom levels use --setup level --level NUMBER --side BUY and optional --interval 1m. The watch command requires bounded cycles and interval. Public matches are analysis, not execution evidence.

## Stop and target

Primary route: `python "${CLAUDE_PLUGIN_ROOT}/scripts/public_market.py" plan --input PATH --stop RULE --target RULE`.

Broker-bound plans use config exit_stop/exit_target and trader.py plan. Regenerate replay, sizing and risk after any plan change. A plan is not a submitted protective order.

## Replay

Primary route: `python "${CLAUDE_PLUGIN_ROOT}/scripts/public_market.py" replay --input PATH`.

For sourced fees and account-bound evidence use trader.py replay. Show gaps and sample sizes. Simulated frequencies are not calibrated forecasts.

## Size

Primary route: `python "${CLAUDE_PLUGIN_ROOT}/scripts/trader.py" size --workdir PATH`.

Require approved personal limits, sourced fees, exact master and margin evidence. Public quotes cannot establish account funds or margin clearance.

Funds, margin and charges may be read from the user's connected broker connector ([broker connectors](../../references/broker-connectors.md)); name it and the read time.

## Risk check

Primary route: `python "${CLAUDE_PLUGIN_ROOT}/scripts/trader.py" risk --workdir PATH`.

Preserve account/plan binding, freshness, restrictions, events and unresolved-order locks. Never substitute public research outputs for reconciled account evidence.

Account, position and margin reads may come from the user's connected broker connector ([broker connectors](../../references/broker-connectors.md)). Web-sourced events or calendars are research only and cannot clear a risk check until the user reviews and saves them.

Research and paper workflows only. Missing data is a gap, not an all-clear. Nothing here places an order.
