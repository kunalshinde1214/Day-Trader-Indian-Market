# Account imports, estimated fees and supervised notifications

These are standard-library tools. Development tests use fake broker/HTTP responses. No real account, message destination or background service has been activated.

## Broker trade imports

`trade_import.py` normalizes actual trade-book rows for Kite, Upstox, Dhan, Angel One and FYERS into the existing journal contract. The adapters add only allowlisted GET trade-book endpoints; mutation routes remain excluded.

```text
python scripts/trade_import.py --broker kite --account-id YOUR_ACCOUNT --master trader/universe.json --fetch --out trader/fills-new.json --journal trader/state/journal.sqlite
```

This explicitly opts into authenticated reads. The tool checks the observed account, fetches the trade book, fetches a fresh account/order snapshot, resolves instruments, then requires fill totals to match every observed order. A race or missing trade blocks the entire batch. It never creates executions from an order's filled quantity alone.

For saved, attributed evidence:

```text
python scripts/trade_import.py --broker kite --account-id YOUR_ACCOUNT --master trader/universe.json --input trader/raw-trades.json --snapshot trader/state/live-snapshot.json --now 2026-11-06T10:00:00+05:30 --out trader/fills-import.json
```

Raw input is an envelope containing broker/account_id/source, offset `as_of`, `date`, and a `trades` array of that broker's unchanged trade rows. The snapshot uses the canonical `live_state.py` contract. Saved evidence must be fresh relative to the explicitly selected clock. Live fetch mode rejects historical clock overrides.

Fill IDs include date, exchange, order ID and exchange trade ID, preventing collisions across sessions. Identical reimports are idempotent; conflicting prices/quantities block. Time-only Angel trade-book fields are bound to the verified current-day response; other ambiguous timestamps fail. FYERS uses its documented trade time. Dhan retains its trade-book `exchangeTime`; account acceptance must verify the provider's exact timestamp semantics. Unsupported products and carry accounts still block this raw importer. The separate [carry accounting workflow](portfolio.md) accepts sourced canonical opening inventory and explicitly imported carry fills; it does not relax broker execution. No raw broker error or credential is printed.

Imported fees start as `{}` with `fees_final:false`. The import does not pretend a broker execution response contains final charges.

## Optional intraday fee reservation

Strict reconciliation still requires final contract-note fees by default. An operator can explicitly place a completed [fee-policy object](../assets/fee-policy.example.json) under `fee_policy` in their private `live-profile.json` to permit provisional intraday reserves. It requires source, matching broker/account, a timestamp within 24 hours, a multiplier from 1 to 10, and a nonnegative per-fill buffer.

The reservation recomputes a full round-trip charge at each fill's actual price and quantity using the supplied broker-specific rate card, applies the multiplier and buffer, and retains any larger recorded fee amount. Charging a round trip for every split fill intentionally over-reserves under that card. This is **not a guarantee of an upper bound on actual charges**; tariff correctness, exemptions and final rounding still require acceptance.

`live.py reconcile-account` uses this policy only when present. It exposes the provisional-fill count, policy hash, rate-card hash and estimate basis. The journal's original fills remain provisional and unchanged. Later audited contract-note corrections remain the final source of truth. Neither an estimate nor a reconciled ledger authorizes an order.

If `reservations.json` exists, reconciliation respects that explicit mapping. Otherwise it reconstructs entry/exit risk groups from the observed local execution log. Unresolved submissions, untracked exits and foreign/manual orders block or remain unmapped; they require explicit journal review. Existing entry approvals and timeout locks remain in force.

## Foreground monitoring supervisor

```text
python scripts/service.py run --workdir trader --cycles 120 --interval 5 --max-failures 5
python scripts/service.py run --workdir trader --broker-poll --cycles 120 --interval 5
python scripts/service.py status --workdir trader
python scripts/service.py stop --workdir trader
python scripts/service.py clear-stop --workdir trader
```

Runs are bounded and foreground-only. `--broker-poll` opts into the existing read-only refresh bridge. The service shares an OS-owned work-directory lock with `watch.py run`, preventing simultaneous monitors. Locks release on process exit; no stale PID is treated as proof of liveness.

Transient read failures pause monitor eligibility and back off, capped at 60 seconds. Successful cycles reset the failure streak. Repeated failures stop the run, write the local entry halt and queue a notice. Shutdown always writes `state:stopped, entry_allowed:false`. A hard process kill may leave an old heartbeat; status reports its age and does not falsely certify process liveness. Keep `monitor_required:true` when live entries depend on this monitor so the core rejects stale heartbeats.

`stop` writes both a stop request and the entry halt. `clear-stop` clears only the stop request while holding the monitor lock. Resume entries separately with the existing explicit `watch.py resume` command after reviewing the failure; clearing a stop does not resume trading. Service control never cancels, retries or places broker orders.

OS boot registration, an external process manager, WebSocket feeds and unattended protective-order operation are **not** implemented by this foreground supervisor.

## Explicit webhook delivery

Configure `DAY_TRADER_WEBHOOK_URL` locally as an HTTPS receiver and optional `DAY_TRADER_WEBHOOK_TOKEN` as a bearer token. Do not put secrets in commands, reports or chat. The script sends only notice ID, timestamp and message; it excludes local work paths/account metadata stored elsewhere in the notice.

```text
python scripts/delivery.py status --journal trader/state/journal.sqlite
python scripts/delivery.py send --journal trader/state/journal.sqlite --limit 20
```

Run `send` only when the user has explicitly requested notifications to that destination. It is never called by demos, daily analysis or the supervisor automatically. HTTP acceptance is recorded transactionally; it is not proof that a person read the alert. Redirects and URL-embedded credentials are refused.

A delivery attempt is persisted before sending. Timeouts, interruptions and ambiguous outcomes remain pending, with **no automatic retry**. The sender uses a stable `Idempotency-Key`; the receiver must implement deduplication for reliable retries. After inspecting the receiver and confirming no other sender is active, an explicit `--retry-unknown` permits another attempt. An OS lock prevents parallel senders for the same journal. A changed destination cannot silently replay old ambiguous notices. The receiver may still see duplicates if it ignores idempotency.

## Primary contracts

- [Kite trade book](https://kite.trade/docs/connect/v3/orders/)
- [Upstox current-day trades](https://upstox.com/developer/api-documentation/get-trade-history/)
- [Dhan trade book](https://dhanhq.co/docs/v2/orders/)
- [Angel One official SDK trade-book route](https://github.com/angel-one/smartapi-python/blob/main/SmartApi/smartConnect.py), [official order schema](https://smartapi.angelbroking.com/docs/Orders) (website access was intermittent; account acceptance remains required)
- [FYERS trades and timestamp semantics](https://fyers.in/api-docs/v3/api/trading/trade-book/get-trades/)
