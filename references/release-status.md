# Release status — 14 November 2026

Version: **0.9.9-broker-workflows-alpha**. This is a public alpha, not the completed production specification. See the [README feature matrix](../README.md#what-is-implemented) for the maintained capability summary.

## Included in this release

- Redesigned README with original self-contained SVG cover and illustrative research workflow, accessible descriptions and clear install paths.
- Sixteen Claude plugin commands, including stock analysis and four daily aliases; six scanners, paper planning, replay, sizing and risk checks.
- Five broker data/account/streaming adapters and limited carry collectors. Human-operated mixed-inventory reducing exits, stop-limits and cancellation for Kite, Upstox and Dhan.
- Bounded repeated public collection, immutable cycle reports, stop latch, health records and a shared filing backlog. Source coverage stays explicitly partial.
- Visible-depth stress, Kite cash-equity depth sampling and exact Kite basket prefixes, explicit fill scenarios and bounded quantity grids.
- Disabled profile generator, read-only probes and end-user acceptance inventory. Publisher brokerage credentials are not required.
- Separately human-armed, expiring, action-limited intraday protection runner. It is experimental, single-instrument and disabled by default; limits and stops do not guarantee flatness.

## Validation

The prior complete development check passed **781 offline tests**, Python/JSON checks and all packaged local links. Release packaging reruns the full suite on final files, validates SVG XML and excludes active/external SVG content. GitHub CI targets Python 3.10, 3.12 and 3.13; use the exact run for the published commit rather than assuming a badge establishes acceptance.

The release process verifies manifest hashes, unpacked synthetic daily and portfolio/journal CLI workflows, both Claude plugin manifests, GitHub file hashes and uploaded ZIP/checksum assets. Visual inspection covers the SVG artwork and repository presentation. These checks do not establish authenticated host invocation, live broker behavior or profitability.

No publisher broker account was connected, real trade submitted, webhook delivered or production service activated. End users own their account/host acceptance. A complete local evidence inventory verifies supplied hashes and declarations, not the truth of an operator claim or permission to trade.

## Remaining engineering

1. Angel One/FYERS carry execution, ambiguous provider inventory conventions, T1/pledged/MTF support, settlement and corporate-action adjustments, and new mixed-inventory entries.
2. Comprehensive BSE/calendar/surveillance/filing coverage, historical membership and corporate actions, reliable issuer/fact resolution, complete closing breadth and macro/context collection.
3. Other-broker depth and basket-margin adapters, queue/latency/market-impact models, size-specific replay costs, unenumerated margin scenarios and pre-open/regime modelling.
4. One-minute variants of the other scanner rules and consistent special-session modelling across research/replay.
5. Broader multi-instrument/carry protection coordination, production deployment/runtime evidence and any broker-required hosted approval integration.

## User-owned acceptance

Each user supplies broker login, API permissions/entitlements, delivery authorization, current tariffs, account limits, and observed broker/host/service checks. See [user setup](user-setup.md), [acceptance evidence](acceptance-evidence.md), [Upstox execution](upstox-execution.md), [Dhan execution](dhan-execution.md) and [protection operations](protection-controller.md). These are not requirements for the publisher to supply an account.

## Distribution

Build with `python scripts/release.py --out dist/india-day-trader-0.9.9-broker-workflows-alpha.zip`. The command refuses overwrite and does not skip tests. The release manifest retains `full_specification_complete: false` and `authenticated_broker_acceptance: false`.

The [0.9.9 release page](https://github.com/kunalshinde1214/Day-Trader-Indian-Market/releases/tag/v0.9.9-broker-workflows-alpha) supplies the archive, checksum and scope notes. Original contributions retain the source-available personal-use license; third-party MIT rights remain intact.
