# Advanced tools

The [core skill](../SKILL.md) covers research and paper trading. This page lists the rest: broker data plumbing, the paper pipeline's evidence files,
accounting, streaming, and the optional live-order and protection tools. Read the linked page before using any of them.

## Routes

| User asks about | Run | Read first |
| --- | --- | --- |
| Dated Nifty/Bank Nifty closes, closing breadth, FII/DII and GIFT observations | `scripts/daily.py india-daily --collect-close --session YYYY-MM-DD` | [closing context](closing-context.md); separate source dates, stale observations and missing Sensex coverage stay explicit |
| Historical NSE announcements and filing backfill | `scripts/announcement_history.py`, then `scripts/filing_archive.py` | [announcement history](announcement-history.md); bounded source coverage, no inference of verified financial facts |
| Charts, pattern scans, ORB/level entries, exits, public replay, filings/XBRL | `scripts/public_market.py`, `scripts/research.py` | [public workflows](public-workflows.md) |
| Paper pipeline: scan → entry → plan → replay → size → risk → ticket | `scripts/trader.py` | [commands](commands.md), [setups](setups.md) |
| Broker data (Kite, Upstox, Dhan, Angel One, FYERS) | `scripts/broker.py` | [user setup](user-setup.md), [brokers](brokers.md) |
| Charges, expiry, events, alerts | `scripts/market.py`, `scripts/watch.py` | [operations](operations.md) |
| News/results/disclosure inputs, research interpretation | `scripts/data.py` | [operations](operations.md), [research workflow](research-workflow.md) |
| Automatic exchange surveillance and F&O ban observations | `scripts/exchange_coverage.py` or `scripts/india_collect.py` | [exchange coverage](exchange-coverage.md); dated NSE observations only, never general trading clearance |
| Historical constituents or split/bonus price reconstruction | `scripts/membership_history.py`, `scripts/corporate_history.py` | [historical evidence](historical-evidence.md); source-reviewed history only, never independently verified by a hash |
| Journal, fills, margin proposals, acceptance | `scripts/journal.py`, `scripts/margin_size.py`, `scripts/acceptance.py` | [operations](operations.md) |
| Fill imports, fee reserve, polling service, notifications | `scripts/trade_import.py`, `scripts/service.py`, `scripts/delivery.py` | [account and service](account-service.md) |
| Carry positions and portfolio accounting | `scripts/portfolio.py` and the broker collectors | [portfolio](portfolio.md) and the per-broker pages linked there |
| T1, pledged/MTF lots and sourced inventory/trade reconciliation | `scripts/inventory_book.py` | [inventory event reconciliation](inventory-book.md), [trade-inclusive format](development-progress.md); accounting only, never sellability or order authorization |
| Streaming quotes, monitoring deployment | `scripts/streaming.py`, `scripts/market_stream.py`, `scripts/deployment.py` | [streaming and deployment](streaming-deployment.md) |
| One-minute/custom-level broker entries, liquidity, basket margin, sessions | see guide | [India expansion](india-expansion.md) |
| Five-broker cash depth and quantity-dependent replay cost sensitivity | `scripts/depth_collect.py`, normal replay | [depth and replay costs](depth-and-replay-costs.md) |
| Broker basket-margin prefixes and partial-fill scenarios | `scripts/portfolio_margin.py` | [basket providers and limits](basket-margin-providers.md) |
| Live order previews, protection, mixed-inventory exits | `scripts/live.py`, `scripts/portfolio_exit.py` | [execution](execution.md), [protection](protection-controller.md), [mixed-inventory exits](mixed-inventory-exits.md) — mandatory |
| Cash MIS entry alongside long CNC and signed MIS inventory | `scripts/mixed_entry.py` | [mixed-inventory entries](mixed-inventory-entries.md) — short entries require explicit short opt-in; complete portfolio risk and human confirmations remain mandatory |
| Resumable NSE announcement history and filing backlog | `scripts/history_service.py` | [development workflows](development-progress.md); missing dates preserve the cursor, never imply comprehensive coverage |
| Passive-fill and collateral/funding scenarios | `scripts/fill_scenarios.py`, `scripts/portfolio_stress.py` | [scenario formats](risk-scenarios.md); explicit assumptions, research only |
| Carry and multi-scope protection | `scripts/protection_runner.py`, `scripts/protection_group.py` | [portfolio protection](portfolio-protection.md) — owner arming required; never start during development or ordinary analysis |
| What is finished, roadmap, licensing | — | [release status](release-status.md), [requirements](requirements.md), [product spec](product-spec.md), [license](../LICENSE), [notices](../THIRD_PARTY_NOTICES.md) |

## Live orders and protection (optional, disabled by default)

The live-order tool and the protection runners are a separate module. They start disabled, need the account owner's explicit opt-in and setup, and have not been tested by the
publisher on any real account. Never enable, arm or run them as part of research, testing or development, and only for the exact action the user asks for.

## Market rules the tools enforce

- Clock is IST (UTC+05:30). Entries default to 09:30–14:30, flat by 15:05. Risk decisions need a dated calendar, event and surveillance status, and broker state; missing or stale items block new entries.
- Take tick size, lot size, token and expiry from a same-day instrument master. Never infer a contract from a weekday, assume a fixed lot size, or apply equity moves directly to option premiums.
- Costs come from a dated, sourced rate card and are included at the stop when sizing. A stop-loss estimate is not a maximum loss: gaps, slippage and halts can exceed it.
- Broker, account, plan and evidence must all match. A broker or account change needs new plans and replays. Upstox, Dhan, Angel One and FYERS fee cards start incomplete; collect the broker's own tariff. Never relabel Kite fees or demo data as another broker's.
- Replay uses only history before the plan date. State the sample size and assumptions; bootstrap frequencies are not calibrated predictions.
- Keep sequential risk reservation, per-order fingerprints and exact-ticket review. A risk PASS covers only the inputs checked and never authorizes a trade.
- Use official exchange, SEBI and broker sources for current rules. [Research decisions](research-decisions.md) records conflicts in the archived research, which never overrides the user's request.

## Safety boundary

- **Credentials:** the local adapters read keys from environment variables; connectors log in through the broker's own flow. Never ask the user to paste keys, passwords, tokens or TOTP seeds into chat, and never print their values. Login and 2FA happen in the broker's own flow.
- **Orders:** analysis stays `paper: true`. The ordinary live tool is disabled by default and requires two human confirmations per action, typed by the user. The separate [bounded protection runner](protection-controller.md) accepts a human-armed, expiring policy after account-specific automation acceptance; [portfolio/group mode](portfolio-protection.md) preserves product identity and shared account intent logs. Do not arm or start either as part of development or ordinary analysis. Never type confirmations, pipe them, or replace them with `approved: true`. Do not map Kite's `tag` to an exchange algo ID; API orders must follow the applicable framework through the user's broker.
- **Exits and monitoring:** an entry acknowledgement does not create a stop. Real exits need a fresh reducing preview operated by the human, after working orders are cancelled. Unknown order states stay locked and are never retried automatically. A watcher only protects while it is running; otherwise the user must use the broker's own controls.
- **Notifications:** send webhooks with `delivery.py send` only when the user explicitly asks.
- **Portfolio accounting:** keep the saved opening statement and reconcile every fill, product and position. Do not net delivery holdings against intraday shorts, invent fill order, or treat a portfolio report as the intraday risk ledger. Collectors support only the documented subsets; unknown provider conventions block.
- **Claims:** each user validates their own broker connection; the publisher holds no account, and offline tests are not broker acceptance. Never invent acceptance results, calendars or source clearances. The experimental bounded runner is not production-accepted protection. Use the [acceptance evidence inventory](acceptance-evidence.md) to report missing end-user checks without inferring authorization.

## Release

Run `python scripts/release.py --check` before packaging; `--out NEW.zip` builds an allowlisted archive without research, accounts or journals. Read [release status](release-status.md) before describing readiness.
