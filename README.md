# Day Trader — Indian Market

**Daily Indian-market briefings, six intraday scanners, paper trade planning and separately confirmed live execution.**

Created and maintained by **Kunal Shinde**. This repository contains the reusable `india-day-trader` skill and its Python tools for Nifty 50, Sensex, Bank Nifty, Indian cash equities and supported derivatives.

[![Offline validation](https://github.com/kunalshinde1214/Day-Trader-Indian-Market/actions/workflows/ci.yml/badge.svg)](https://github.com/kunalshinde1214/Day-Trader-Indian-Market/actions/workflows/ci.yml)

**Release: 0.9.6-dependent-replay-alpha.** Includes a fifteen-skill Claude plugin, credential-free public-market research and the broker-bound paper workflows. Live execution starts disabled. The full production specification is unfinished, and no authenticated broker acceptance or real-trade validation is claimed. See [release status](references/release-status.md) and [remaining requirements](references/requirements.md).

> **Use this skill at your own risk.** Trading can lose money; derivatives and leverage can cause losses beyond the amount you expected to risk. Outputs are informational tools, not investment advice, a recommendation, a promise of returns or a suitability assessment. Verify every data point, instrument, quantity, price and order yourself. Start with the synthetic demos and paper mode. Read the [risk disclosure](RISK_DISCLOSURE.md) before connecting an account.

## Quick start — no broker account needed

Use Python **3.10 or newer**. Core analysis uses the Python standard library. Clone into a folder named `india-day-trader`:

```bash
git clone https://github.com/kunalshinde1214/Day-Trader-Indian-Market.git india-day-trader
cd india-day-trader
python scripts/daily.py /india-daily --demo
python scripts/trader.py demo --broker kite --workdir demo-kite-first
```

On Windows, use `py` instead of `python` if that is your Python launcher. On systems where Python is named `python3`, use that command. No credentials or subscription are required for these demos. They use **fictional data, example fees and a historical clock**; they are not current-market reports or performance evidence. Choose a new work directory when rerunning a trading demo.

For an assistant that supports skills, install this folder using that assistant's skill installation workflow. Keep [SKILL.md](SKILL.md), `scripts/`, `references/`, `assets/`, `agents/` and the license notices together. The CLI works independently of an assistant. Slash commands below are skill aliases and CLI arguments, not globally installed commands.

## Install the Claude plugin

```text
/plugin marketplace add kunalshinde1214/Day-Trader-Indian-Market
/plugin install india-day-trader@kunal-india-trading
```

The plugin provides the eleven original workflow roles adapted to India, plus four daily commands. Invoke `/india-day-trader:trader-scanner`, `/india-day-trader:india-daily`, `/india-day-trader:nifty`, `/india-day-trader:sensex` or `/india-day-trader:banknifty`. See the [public workflow guide](references/public-workflows.md) for all fifteen commands, capabilities and boundaries. Installation does not start services or enable trading.

## Public market data — no brokerage credentials

Market-wide news now has a [collect-and-display workflow](references/india-news.md): run `python scripts/india_news.py` for NSE feeds, or add `--sources nse-announcements nse-actions et-markets` for an optional personal-use Economic Times briefing. Output includes linked headlines, IST publication times, categories and source coverage.

```bash
python scripts/public_market.py universe --out reports/nse-equities.json
python scripts/public_market.py scan --symbols RELIANCE.NS TCS.NS INFY.NS --out reports/public-scan.json
python scripts/daily.py /india-daily --collect-public
python scripts/research.py news --symbols RELIANCE.NS TCS.NS --state reports/news-seen.sqlite
```

These fetch real public data when available; they do not use the synthetic demo. Public Yahoo charts and NSE feeds can be delayed or unavailable. Daily session metadata is provider-observed, not independently verified exchange-calendar evidence, and missing breadth/events remain visible. Public research cannot authorize an order. Choose new output paths when rerunning. The [guide](references/public-workflows.md) also covers one-minute/custom-level entry research, multi-pattern screening, flexible stops/targets, public replay and XBRL analysis.

## Daily analysis commands

| Command | Report |
| --- | --- |
| `/india-daily` | Nifty 50, Sensex and Bank Nifty comparison |
| `/nifty` | Nifty 50 briefing |
| `/sensex` | Sensex briefing |
| `/banknifty` | Bank Nifty briefing |

Each supports pre-open, intraday and close reports, previous-session levels, completed-candle scenarios, optional constituent breadth and event coverage. Missing or stale data stays visible. Spot-index levels do not imply an executable futures/options contract.

```bash
python scripts/daily.py /nifty --demo --phase preopen
python scripts/daily.py /sensex --demo --phase close
python scripts/daily.py /banknifty --demo --format json
python scripts/daily.py /india-daily --input trader/daily-input.json --out reports/india-daily-new.md
```

The last command needs your sourced input file. For automatic Kite index reads, configure your local credentials and a verified session calendar, then follow [daily report setup](references/daily.md). `--with-context` adds constituent quotes and partial NSE announcement coverage; it does not establish complete market-news or event clearance. Do not use `--demo` as a substitute for unavailable real data.

## Included capabilities

| Area | Implemented scope |
| --- | --- |
| Scanners | Opening-range breakout, VWAP reclaim, gap-and-go, 20-session closing breakout, bull flag and oversold bounce |
| Paper pipeline | Scan → entry → plan → historical replay → size → risk → ticket |
| Replay and sizing | Seeded 10,000-path replay, rupee costs, whole-lot sizing and exact-payload margin proposals; [limitations](references/setups.md) apply |
| Brokers | Read-only adapters and separate human-operated execution transports for Kite, Upstox, Dhan, Angel One and FYERS; authenticated acceptance pending |
| Live controls | Per-action human confirmations, refreshed evidence, persisted intents, no automatic ambiguous retry, recovery and reducing limit/stop-limit workflows |
| Journal | Fill imports, account reconciliation, duplicate/conflict detection, audited fee corrections and CSV export |
| Carry accounting | Reviewed opening inventory, product-specific positions, pooled cost basis and daily P&L; limited collectors for all five brokers; provider conventions and authenticated acceptance remain pending |
| Monitoring | Bounded polling, local alerts, entry halt, opt-in webhook delivery and five-broker WebSocket quote adapters |
| Deployment | Reviewable Windows Task Scheduler/systemd files; rendering does not install or activate a service |
| Research inputs | Sourced news/disclosure processing, comparative reports, event review and expiry/flow diagnostics |
| Public workflows | NSE EQ listing; five multi-tag daily patterns; public index/equity history; one-/five-minute ORB and custom levels; bounded watch; research replay |
| Financial filings | Supported supplied XBRL/inline-XBRL facts, basis-matched YoY, four-quarter revenue/profit TTM and explicit filing/PDF text extraction |

Ambiguous Angel/FYERS field conventions, comprehensive exchange-data coverage, unsupported inventory adjustments and mixed-account live execution remain pending. Stops and exits require their own human actions: this is **not an unattended trading bot**. A running watcher or an entry acknowledgment does not install a broker stop.

## Connect your own broker

Each user supplies and validates their own account. The publisher does not supply a broker account or credentials. Follow the [end-user setup and read-only acceptance guide](references/user-setup.md). Public research needs no account; broker integrations are implemented but not independently account-tested by the publisher.

1. Read [broker setup](references/brokers.md) and choose your broker, market segments and data entitlement.
2. Keep API credentials in your local environment. Never paste tokens into chat, source files, issues or commits.
3. Supply a current instrument master, dated session/restriction/event evidence, your account identity, approved risk limits and broker-specific fee card. Example limits are not personal approvals.
4. Run the documented read-only acceptance checks and paper workflow before considering live actions. Missing evidence must remain a block.
5. Read [execution boundaries](references/execution.md) before enabling the separate human-operated terminal tool. An assistant must never type the human confirmations on your behalf.

Optional Kite/Dhan/Angel streaming uses an additional dependency:

```bash
python -m pip install -r requirements-streaming.txt
```

Upstox/FYERS use a separate SDK environment; follow [streaming setup](references/streaming-deployment.md). Install dependencies in your own virtual environment if needed. Core demos do not require it. This project does not buy subscriptions, log in to a broker for you, provide market-data redistribution rights or issue a license key.

## Documentation

- [Skill entry point](SKILL.md) and [commands/data contracts](references/commands.md)
- [Daily reports](references/daily.md) and [six setup definitions](references/setups.md)
- [Broker setup](references/brokers.md) and [human-operated execution](references/execution.md)
- [Journal and service operations](references/account-service.md)
- [Carry accounting](references/portfolio.md) and [Kite carry collection](references/kite-portfolio.md) and [Upstox carry collection](references/upstox-portfolio.md) and [Dhan carry collection](references/dhan-portfolio.md) and [Angel/FYERS collection](references/angel-fyers-portfolio.md)
- [Streaming/deployment](references/streaming-deployment.md)
- [Risk disclosure](RISK_DISCLOSURE.md), [security reporting](SECURITY.md) and [contributing](CONTRIBUTING.md)
- [Release status](references/release-status.md), [changelog](CHANGELOG.md), [coverage matrix](references/requirements.md) and [target specification](references/product-spec.md)

## Validation and releases

```bash
python scripts/release.py --check
python scripts/release.py --out dist/india-day-trader-0.9.6-dependent-replay-alpha.zip
```

The release command runs the offline suite and checks Python syntax, JSON and packaged documentation links. It builds an allowlisted ZIP with file hashes and refuses to overwrite an existing archive. Research documents, account files, journals, dependencies, logs and generated demos are excluded. GitHub Actions runs the same checks on Python 3.10, 3.12 and 3.13; inspect the linked run for its actual result. Passing synthetic tests does not prove broker connectivity, live reliability or profitability.

## License — public source, personal use

This is **source-available, not open source**. The [India Day Trader Personal Use License](LICENSE) permits individuals to download, inspect, run and privately modify the covered original contributions for their own learning, research, analysis and trading account, without a license fee. Profits from your own trading do not by themselves violate that permission.

It restricts redistribution, resale, sublicensing, trading for clients and hosted services unless separately authorized. GitHub's applicable public-repository permissions remain intact. Public visibility does not transfer ownership, and the license is not technical copy protection. This summary does not replace the license text.

The project adapts **Jake Schincariol's MIT-licensed day-trader-skill**. Its unchanged [MIT notice](third-party/day-trader-skill.LICENSE) and [third-party notices](THIRD_PARTY_NOTICES.md) remain included. The personal-use restrictions apply only to contributions the licensor is authorized to license on those terms; they do not revoke independent MIT rights in inherited material.

The software is supplied **AS IS**, without a trading-performance warranty, subject to applicable law. You remain responsible for your account, decisions, broker/exchange permissions, costs and applicable obligations. No broker, exchange or regulator endorsement is claimed.
