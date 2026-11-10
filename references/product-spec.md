# Public product specification

Skill creator and maintainer: Kunal Shinde. Distribution intent: public GitHub, free personal use, restrictive terms against redistribution, resale and service offerings, limited to material he has authority to license on those terms. Creator credit is not a blanket copyright-ownership claim. The upstream MIT license remains applicable to inherited material. Verify licensing authority before publication. This is a target specification, not an implementation-completeness claim.

## Reusable onboarding

Each user selects their supported broker, data entitlement, market segments and paper risk limits. The developer's personal account setup is not a prerequisite for product development or use by others. Keep the fictional offline demo available without an account. Do not purchase subscriptions automatically.

## Broker architecture

| Broker | Status | Target order |
| --- | --- | --- |
| Zerodha Kite | Read-only adapter; mocked tests; no authenticated integration test | First, existing |
| Upstox | Read-only adapter and synthetic paper path; no authenticated integration test | Second, implemented offline |
| Dhan | Read-only adapter, synthetic paper path and public master smoke test; no authenticated integration test | Third, implemented offline |
| Angel One SmartAPI | Read-only adapter and synthetic paper path; public-master and authenticated acceptance pending | Fourth, implemented offline |
| FYERS | Read-only adapter and synthetic paper path; public-master and authenticated acceptance pending | Fifth, implemented offline |

Kite, Upstox, Dhan, Angel One and FYERS pass separate synthetic paper paths and adapter contract tests. Dhan's public master parser has also been exercised against the public CSV. The separate [execution alpha](execution.md) includes five placement/cancellation transports, a human approval tool and durable recovery tests. Authenticated acceptance remains outstanding for every broker; offline contracts do not establish a working account connection.

Shared read interfaces: capabilities, instruments, quotes, candles, funds, positions, orders and estimate_margin. Execution methods remain separate from read-only access. Capabilities distinguish history, streaming, quotes, margin, paper simulation and human-operated live execution; unsupported features fail explicitly.

Use canonical instrument identity (exchange, segment, symbol, expiry, strike, option type) plus broker-specific identifiers. Normalize rupees/paise, quantities, ticks, candle timestamps, product codes and order statuses. Never reuse one provider's token with another. Broker-specific fees, square-off times, freeze limits and rate limits belong in dated configuration.

The Kite coupling in configuration, charges, payloads, margin-source validation and ticket output has been removed for the five implemented brokers. Matching selected-broker rate cards and broker/account-bound plans and account/margin evidence are enforced. Upstox/Dhan/Angel/FYERS initialization leaves rates incomplete rather than inheriting Kite assumptions. Schema-v1 snapshots require regeneration; see [migration](brokers.md). Preserve these checks as broker coverage expands. Never retry a timed-out order with another broker: reconcile the original order first.

Adapter acceptance: documented fixtures, price-unit/timestamp tests, partial/missing-data behavior, secret redaction, redirect controls, exact-order margin matching, explicit unsupported capabilities and an opt-in authenticated read-only smoke test. Mocked tests do not prove a working account connection.

Official discovery links:

- [Kite](https://kite.trade/docs/connect/v3/)
- [Upstox v3 history](https://upstox.com/developer/api-documentation/v3/get-historical-candle-data/), [intraday](https://upstox.com/developer/api-documentation/v3/get-intra-day-candle-data/), [master](https://upstox.com/developer/api-documentation/instruments/)
- [Dhan](https://dhanhq.co/docs/v2/)
- [Angel One SmartAPI](https://smartapi.angelone.in/docs)
- [FYERS](https://myapi.fyers.in/docsv3)

These links do not establish that every provider capability or subscription rule has been verified.

## Original skill parity

The current implementation is not feature-complete. Track all eleven original responsibilities: scanner, news, entry, exit, time machine, budget, risk, broker, alerts, analyst and disclosure tracker. Replace US congressional disclosures with Indian public PIT/SAST, pledge and bulk/block records.

All six setup rules, four daily index reports, structured research processors, named public feeds, universe/reference builders, five-broker intraday trade imports, fee reservations, carry accounting, supervision, webhook delivery and human-approved execution/recovery are implemented. Five-broker streaming, Windows/systemd deployment-file generation, optional constituent/news collection and a limited Kite carry collector requiring reviewed opening evidence are also implemented. Remaining work includes comprehensive exchange ingestion, authenticated acceptance, other-broker carry normalization, inventory adjustments and mixed-account execution, OS activation/runtime acceptance and unattended protection. A normalized processor is not comprehensive collection. [Requirements](requirements.md) records implemented versus pending work.

## India-specific priorities

1. **Eligibility and calendars:** dated segment sessions, special days, holiday-adjusted expiries, daily masters, ASM/GSM/T2T/F&O-ban status, suspensions, price bands and halts. Missing status must not become assumed clearance.
2. **Execution costs:** broker-specific fees, bid/ask spread, depth, slippage, freeze quantities, partial fills, rejection and square-off deadlines. Calculate cost-adjusted reward before presenting a paper candidate.
3. **Derivatives:** explicit contract selection, sourced IV/Greeks/OI, spread liquidity, expiry and settlement exposure, portfolio margin and margin changes. Declare unavailable historical options data; do not substitute underlying returns for premium returns. Keep naked shorts disabled unless explicitly supported and configured.
4. **Indian context:** India VIX, sector-relative movement, breadth/index concentration, timestamped GIFT Nifty, RBI/Budget/election/results events, and correctly dated released FII/DII flows. These are explainable inputs, not guaranteed directional signals.
5. **Data integrity:** corporate actions, symbol changes, survivorship-free historical universes, missing/delayed feeds and adjustment-aware replay. Preserve raw source metadata and disclose incomplete coverage.
6. **Journal and reconciliation:** actual broker fills and contract-note fees, reserved/realized risk and cost-separated CA-oriented exports. An order acknowledgment is not a fill. Tax computations need current-year verification.
7. **Resilience:** disconnect detection, paused new entries on stale data, restart recovery, alert-delivery evidence and tested emergency controls before live work. A stopped process cannot protect positions.

These are proposed engineering requirements, not universal regulatory mandates. Verify current exchange/broker rules during implementation.

## License versus activation keys

The chosen free personal-use model needs no activation key. The license establishes permissions; keys implement access controls. Do not embed a shared secret or master key in a public repository. Broker API credentials are separate.

If restricted distribution is selected later, design per-user signed entitlements with a private issuer and public verification key. Decide expiry, revocation, privacy and offline behavior first. A verifier in public Python/Markdown can be removed; it cannot guarantee copy protection. Keeping valuable functionality on a private server offers stronger access control but changes the standalone skill into a service.

Never disable risk-reducing exits or emergency controls because an entitlement expires. No license server, activation gate, telemetry or user database is implemented or deployed. A later license change does not automatically retract earlier grants; obtain legal review of the release terms and any transition.

## GitHub release boundary

Publish the reviewed `india-day-trader` product folder, not the entire parent workspace. Exclude secrets, account snapshots, journals, generated demo outputs and the local research archive. Include LICENSE, THIRD_PARTY_NOTICES.md, risk/security documentation and the unchanged upstream MIT license. Public GitHub permissions remain acknowledged. The chosen destination is [Day-Trader-Indian-Market](https://github.com/kunalshinde1214/Day-Trader-Indian-Market); inspect its commits, Actions and Releases for publication evidence. A public alpha is not full-product acceptance.
