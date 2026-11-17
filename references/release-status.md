# Release status — 17 November 2026

**0.10.1, 17 November 2026:** adds the [history, inventory, risk and protection work](development-progress.md) on top of 0.10.0. Release validation passed **984 offline tests**. The 0.10.0 feature description below still applies.

Version: **0.10.1-carry-protection-alpha** (builds on 0.10.0-market-workflows-alpha). A public alpha with expanded Indian-market research and broker workflows. **The full specification is not complete.** See the [feature matrix](../README.md#what-is-implemented) and [ordered completion tracker](completion-tracker.md).

## Included in this release

- Sixteen Claude plugin skills, a one-line terminal installer, four daily aliases and one-stock public research. The README includes original SVG artwork and a workflow illustration.
- Six scanners plus custom levels at one/five-minute intervals, interval-bound replay, INR sizing and risk checks. Gap and Go retains its full fifteen-minute opening window.
- Dated public NSE Nifty/Bank Nifty closes, observed EQ breadth, separately dated FII/DII and GIFT context, surveillance indicators and F&O-ban observations. Missing or stale components remain explicit; none establishes trade clearance.
- Bounded NSE announcement backfill, official attachment discovery, symbol-plus-ISIN matching, supported Indian inline-XBRL numeric formats and a durable filing backlog.
- Reviewed historical membership intervals and split/bonus reconstruction, with information cutoffs and source hashes. Inventory accounting covers sourced settled, T1, pledged, funded and entitlement lots without granting trading eligibility.
- Supported reducing carry exits, stop-limits and cancellation across Kite, Upstox, Dhan, Angel One and FYERS. Separate long MIS cash entries can coexist with supported long CNC/MIS exposure. These are disabled-by-default, human-operated workflows.
- Five-broker cash-depth observations, quantity-dependent replay fees and explicit cost sensitivity, plus Kite/Upstox/Angel/FYERS basket prefixes and supplied fill scenarios. Dhan/FYERS depth is research-only where book timestamps are unavailable; margin providers retain their different response semantics.
- Bounded repeated public collection; user-owned setup and acceptance workflows; the existing experimental single-instrument intraday protection runner. No service is installed automatically.

## Validation and its limits

The final development suite passes **936 offline tests**. Packaging reruns the full suite and checks Python syntax, JSON, local links, plugin manifests and self-contained SVGs. ZIP contents have a SHA-256 manifest; private outputs, credentials, local state and dependencies are excluded by an allowlist.

Release verification checks the archive manifest, source-byte agreement, a clean unpack, synthetic daily commands, disabled-profile generation, new CLI entrypoints and portfolio/journal workflows. GitHub CI targets Python **3.10, 3.12 and 3.13**. Use the exact published commit's CI result; a generic badge does not prove account acceptance.

Public NSE source smoke tests succeeded during development, but do not establish future source availability or comprehensive coverage. Simulated broker tests do not establish authenticated broker behavior, installed-host invocation, production reliability or profitability.

No publisher trading account was connected, trade submitted or production service activated. Each user supplies their own credentials and acceptance evidence. The publisher does not need a brokerage account to distribute the skill.

## Remaining engineering

1. Provider-specific T1/pledged/MTF execution, broader settlement/corporate actions and more mixed-entry combinations.
2. Comprehensive BSE/calendar/surveillance/filing coverage, complete licensed histories, independent effective-date/issuer/fact verification, BSE closing data and macro feeds.
3. Derivative depth units, calibrated queue/latency/partial-fill/impact models, Dhan baskets, collateral/market stress and unenumerated margin scenarios.
4. Pre-open/regime models and consistent special-session handling across research and replay.
5. Multi-instrument/account and carry-aware unattended protection, runtime validation, remaining broker inventory/master ambiguities and account-specific tariffs/exemptions/rounding.

The [completion tracker](completion-tracker.md) keeps all twelve requested areas visible. Partial implementations are not counted as complete.

## User-owned acceptance

Users supply broker login, API permissions and entitlements, delivery authorization, current tariffs, approved limits and observed broker/host/service checks. See [user setup](user-setup.md), [acceptance evidence](acceptance-evidence.md), [carry execution](mixed-inventory-exits.md) and [protection operations](protection-controller.md). Operator-supplied declarations are not independent broker certification.

## Distribution

Build with `python scripts/release.py --out dist/india-day-trader-0.10.1-carry-protection-alpha.zip`. The command refuses overwrite and does not skip tests. The manifest retains `full_specification_complete: false`, `authenticated_broker_acceptance: false` and `live_enabled_by_default: false`.

The [0.10.1 release page](https://github.com/kunalshinde1214/Day-Trader-Indian-Market/releases/tag/v0.10.1-carry-protection-alpha) provides the ZIP, checksum and scope notes. Original contributions remain source-available for personal use; third-party MIT rights remain intact.
