# Day Trader — Indian Market

**An AI assistant skill for the Indian stock market.** Ask Claude about any NSE/BSE stock, get daily Nifty, Sensex and Bank Nifty reports, read market news, scan for trade setups and plan paper trades with proper risk and cost checks.

Created and maintained by **Kunal Shinde**. Free for personal use.

[![Offline validation](https://github.com/kunalshinde1214/Day-Trader-Indian-Market/actions/workflows/ci.yml/badge.svg)](https://github.com/kunalshinde1214/Day-Trader-Indian-Market/actions/workflows/ci.yml)

> **Important:** this is research software, not investment advice. Research commands do not trade. Separate live tools include human-approved orders and an experimental, explicitly human-armed [bounded protection runner](references/protection-controller.md). Live use is disabled by default and has not passed authenticated production acceptance. Markets can move against any analysis, and trading can lose money. Read the [risk disclosure](RISK_DISCLOSURE.md).

## Install (Claude Code)

```text
/plugin marketplace add kunalshinde1214/Day-Trader-Indian-Market
/plugin install india-day-trader@kunal-india-trading
```

You need **Python 3.10 or newer** on your computer. No broker account, API key or subscription is needed for the research features.

## What you can ask

Just talk to Claude normally. Some examples:

| You say | You get |
| --- | --- |
| "Analyse Reliance" | Full report: price vs Nifty, trend, P/E, ROE, debt, growth, recent news and likely price ranges |
| "Is TCS expensive right now?" | Valuation ratios explained against growth and returns |
| "Where is HDFC Bank heading?" | Trend, support/resistance and the range the stock usually moves in over a week, month and quarter |
| "How is the market today?" | Nifty 50, Sensex and Bank Nifty side by side |
| "Nifty levels for today" | Previous-day high/low/close, opening range and bullish/bearish trigger levels |
| "Latest market news" | NSE announcements and market headlines with timestamps |
| "Scan INFY, TCS and WIPRO for breakouts" | Which stocks match opening-range, gap, breakout, flag or oversold patterns |
| "Any insider or promoter activity in Adani Ports?" | Recent promoter, insider, pledge and bulk/block deal disclosures |
| "How many shares can I buy risking ₹2,000?" | Position size in shares or lots, with brokerage and taxes included |

You can also type a command directly: `/india-day-trader:stock-analysis`, `/india-day-trader:nifty`, `/india-day-trader:india-daily` and so on. There are [16 commands](references/public-workflows.md) in total.

## Stock analysis

The `stock-analysis` command builds a one-page report for any Indian stock:

- **Market:** latest price, 1-month / 3-month / 1-year returns against the Nifty 50, 52-week range
- **Condition:** trend from the 20/50/200-day averages, RSI, volatility, volume, support and resistance
- **Ratios:** market cap, P/E, P/B, ROE, margins, debt/equity, current ratio, interest cover, free-cash-flow and dividend yield, revenue and EPS growth
- **News:** headlines from the last 7 days (Google News, Yahoo and NSE)
- **Outlook:** where the price has typically been after 1 week, 1 month and 3 months, based on the stock's own last two years, plus the levels that would confirm a bullish or bearish move

The outlook is a range of historical outcomes, not a prediction or a price target. Every figure shows its date, and anything missing or out of date is flagged instead of guessed.

## Use it without Claude

All features are plain Python scripts. Clone the repository and run them from the folder:

```bash
git clone https://github.com/kunalshinde1214/Day-Trader-Indian-Market.git india-day-trader
cd india-day-trader
python scripts/stock_analysis.py RELIANCE
python scripts/daily.py india-daily --collect-public
python scripts/india_news.py
python scripts/public_market.py scan --symbols RELIANCE.NS TCS.NS INFY.NS --out reports/scan.json
```

If `python` is not found, use `python3` (macOS/Linux) or `py` (Windows). To try everything with made-up data first, run `python scripts/daily.py nifty --demo` or `python scripts/trader.py demo --broker kite --workdir demo-first`.

## Where the data comes from

Free public sources: Yahoo Finance for prices and company financials, NSE for announcements, and Google News for headlines. Prices can be about 15 minutes delayed, and free feeds sometimes fail or change. When that happens the report says so rather than inventing numbers.

## Trading workflows (advanced)

For active traders there is a full paper-trading pipeline: **scan → entry → plan → replay → size → risk → ticket**. It covers six intraday setups and calculates Indian brokerage, STT, GST and stamp duty. It sizes in whole lots, replays the plan against past sessions, and checks it against your own daily loss limits.

It can also read data from your own **Kite, Upstox, Dhan, Angel One or FYERS** account using your API keys, which stay on your computer. Start with [broker setup](references/user-setup.md).

A separate live-order tool exists but is **switched off by default**. You must enable it yourself and type two confirmations for every order; the AI is never allowed to type them for you. It has not yet been tested against a real broker account, and API trading in India must follow SEBI's retail algo rules (see the [risk disclosure](RISK_DISCLOSURE.md)).

## Documentation

- [Skill instructions](SKILL.md) and [all commands](references/commands.md)
- [Public research workflows](references/public-workflows.md), [daily reports](references/daily.md), [news](references/india-news.md)
- [Setup definitions](references/setups.md), [broker setup](references/brokers.md), [live execution](references/execution.md)
- [What's finished and what isn't](references/release-status.md), [changelog](CHANGELOG.md), [roadmap](references/requirements.md)
- [Security](SECURITY.md), [contributing](CONTRIBUTING.md)

## For developers

```bash
python scripts/release.py --check
```

This runs the full offline test suite and checks syntax, JSON and documentation links. CI runs it on Python 3.10, 3.12 and 3.13. Tests use simulated data only, so passing them does not prove broker connectivity or trading profitability.

## License

**Free for personal use; source-available, not open source.** You can download, run and modify it for your own learning, research and trading. You can't resell it, redistribute it, or run it as a service for other people without permission. See the [license](LICENSE) for the exact terms.

This project builds on Jake Schincariol's MIT-licensed [day-trader-skill](third-party/day-trader-skill.LICENSE); see [third-party notices](THIRD_PARTY_NOTICES.md). Not affiliated with or endorsed by NSE, BSE, SEBI or any broker.
