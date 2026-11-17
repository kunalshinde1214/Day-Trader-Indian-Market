---
name: fno
description: Indian options and futures — option chain, OI, PCR, max pain, IV, Greeks, expected move and strategy payoff for NIFTY, BANKNIFTY, FINNIFTY, MIDCPNIFTY and F&O stocks. Use for "Nifty option chain", "Bank Nifty max pain", "payoff of an iron condor", "25800 CE bechu kya".
---

# fno

Read the [core skill](../../SKILL.md) first. Resolve scripts relative to `${CLAUDE_PLUGIN_ROOT}`. Run them with `python`; if that is not found use `python3` (macOS/Linux) or `py` (Windows). Keep user files outside the plugin folder, and pass the user's words as arguments, never as raw shell text.

Routes ([details](../../references/fno.md)):

- Chain and positioning: `python "${CLAUDE_PLUGIN_ROOT}/scripts/fno.py" chain NIFTY [--expiry YYYY-MM-DD] [--strikes 12]`
- Expiry list: `python "${CLAUDE_PLUGIN_ROOT}/scripts/fno.py" expiries BANKNIFTY`
- Strategy payoff: `python "${CLAUDE_PLUGIN_ROOT}/scripts/fno.py" payoff NIFTY --leg SELL:CE:25800:1 --leg BUY:CE:26000:1 [--chart PATH.svg]`. Leg format is `SIDE:TYPE:STRIKE:LOTS[:PREMIUM]`; futures are `BUY:FUT::1`.
- Add `--summary` to either command for a short answer-first digest; use the full report when the user wants the strike table or the payoff table.

The chain report compares PCR, max pain, ATM IV and OI with the previous trading day; use that change when the user asks "what changed" or which way positioning is moving. The payoff report gives the value at expiry and today (T+0), and the probability of profit and of finishing above each breakeven implied by option prices. Call those probabilities what they are: implied by today's option prices, not a forecast.

How to answer:

1. Map names to NSE symbols (Bank Nifty → BANKNIFTY, Fin Nifty → FINNIFTY, Reliance → RELIANCE). Stocks only work if they are in the F&O segment.
2. The free data is NSE's **end-of-day** file, so say which trading day it is from. During market hours, if the user has a broker connector with an option chain (for example Kite or Dhan), read the chain there, save it as an `fno_chain_v1` file ([format](../../references/fno.md)) and pass `--input`; otherwise say the numbers are from the last close.
3. Answer the question first. PCR, max pain, OI walls and build-up describe where option positions sit; present them as positioning, never as a forecast or signal. Give both readings when they conflict.
4. For strategy questions, build the legs the user describes, report net premium, maximum profit and loss, breakevens and position Greeks, and offer the SVG chart. Say clearly when a loss is unlimited. Costs are excluded, and margin for short options comes from the user's broker (basket margin); never estimate margin yourself.
5. Never recommend a specific trade, strike or position size, and never call any strategy safe. If asked "should I sell this", explain the payoff, the risk and the assumptions, and leave the decision to the user. Selling options can lose far more than the premium received.
6. For events that move options (results dates, RBI policy, budget, expiry-day rules, lot-size or F&O-list changes), check the web and cite the source and date ([web research](../../references/web-research.md)).

Research and paper workflows only. Missing data is a gap, not an all-clear. Nothing here places an order.
