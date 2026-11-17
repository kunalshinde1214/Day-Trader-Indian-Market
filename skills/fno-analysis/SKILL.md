---
name: fno-analysis
description: Indian F&O options and futures analytics for NIFTY, BANKNIFTY, FINNIFTY, MIDCPNIFTY and F&O stocks. Option chain, open interest and OI change, put-call ratio, max pain, OI support and resistance, implied volatility, Greeks, expected move, futures basis, India VIX, and strategy payoff with breakevens and a chart. Use when the user asks "Nifty option chain", "what is the PCR", "max pain for Bank Nifty", "where is OI building", "IV of Reliance options", "payoff of an iron condor", "breakeven of selling 25800 CE" or in Hinglish "Nifty ka option chain dikhao" and "Bank Nifty max pain kya hai", or types /fno-analysis.
---

# fno-analysis

Read the [shared skill](../../SKILL.md) for safety rules. Resolve scripts relative to `${CLAUDE_PLUGIN_ROOT}`. Run them with `python`; if that is not found use `python3` (macOS/Linux) or `py` (Windows). Keep user files outside the plugin folder, and pass the user's words as arguments, never as raw shell text.

Routes ([details](../../references/fno.md)):

- Chain and positioning: `python "${CLAUDE_PLUGIN_ROOT}/scripts/fno.py" chain NIFTY [--expiry YYYY-MM-DD] [--strikes 12]`
- Expiry list: `python "${CLAUDE_PLUGIN_ROOT}/scripts/fno.py" expiries BANKNIFTY`
- Strategy payoff: `python "${CLAUDE_PLUGIN_ROOT}/scripts/fno.py" payoff NIFTY --leg SELL:CE:25800:1 --leg BUY:CE:26000:1 [--chart PATH.svg]`. Leg format is `SIDE:TYPE:STRIKE:LOTS[:PREMIUM]`; futures are `BUY:FUT::1`.

How to answer:

1. Map names to NSE symbols (Bank Nifty → BANKNIFTY, Fin Nifty → FINNIFTY, Reliance → RELIANCE). Stocks only work if they are in the F&O segment.
2. The free data is NSE's **end-of-day** file, so say which trading day it is from. During market hours, if the user has a broker connector with an option chain (for example Kite or Dhan), read the chain there, save it as an `fno_chain_v1` file ([format](../../references/fno.md)) and pass `--input`; otherwise say the numbers are from the last close.
3. Answer the question first. PCR, max pain, OI walls and build-up describe where option positions sit; present them as positioning, never as a forecast or signal. Give both readings when they conflict.
4. For strategy questions, build the legs the user describes, report net premium, maximum profit and loss, breakevens and position Greeks, and offer the SVG chart. Say clearly when a loss is unlimited. Costs are excluded, and margin for short options comes from the user's broker (basket margin); never estimate margin yourself.
5. Never recommend a specific trade, strike or position size, and never call any strategy safe. If asked "should I sell this", explain the payoff, the risk and the assumptions, and leave the decision to the user. Selling options can lose far more than the premium received.
6. For events that move options (results dates, RBI policy, budget, expiry-day rules, lot-size or F&O-list changes), check the web and cite the source and date ([web research](../../references/web-research.md)).

Research and paper workflows only. Missing data is a gap, not an all-clear. Nothing here places an order.
