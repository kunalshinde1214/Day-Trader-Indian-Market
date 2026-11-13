# Paper and live execution

The analysis pipeline remains paper-only. `scripts/live.py` is a **separate human-operated execution tool**, disabled by default. Its placement/cancellation transports cover Kite, Upstox, Dhan, Angel One and FYERS. Offline tests exercise the contracts; no authenticated execution acceptance has been performed. This is an execution alpha, not unattended trading or a production reliability claim.

## Authorization boundary

Keep analysis `config.json` set to `paper:true`. Copy [the live profile example](../assets/live-profile.example.json) into the user's private work directory as `live-profile.json`. It starts with both `enabled` and `new_entries` false. The profile selects the same broker/account as the analysis configuration and sets additional notional/loss caps. Enabling this file alone never authorizes an order.

Before allowing entries, the human operator must complete account-specific acceptance: authenticated reads, applicable broker permissions/network requirements, matching tariffs and instrument units, a working emergency broker interface, and the local execution/recovery checks. Record the outcome, account, source and timestamp in `acceptance`; entry acceptance expires after 24 hours. Do not generate a fictitious PASS, copy another account's acceptance, or mistake the read-only probe for execution certification. Tokens and login stay in the user's environment and broker login flow.

For every physical order, the terminal displays the exact account, canonical action, wire payload, risk result, account-state hash and review hash. The user types `REVIEW <full hash>` and then `SEND <broker>:<account> <full hash>`. Both are required. A saved boolean, piped input, chat-wide permission or previous ticket cannot substitute for these confirmations. **An assistant must not type these confirmations or drive an interactive shell to simulate the human.** Help the user prepare and inspect the result; the human runs the mutation commands.

This terminal tool implements the second confirmation locally. It is not a broker-hosted approval card, independent identity provider or security boundary against someone who can modify the Python process/database. An organization requiring broker-hosted approvals must integrate its approved gateway before activation. There is no claim that two prompts supply independent authentication.

After confirmation the tool reloads configuration/evidence, refreshes broker quotes/margin/account state and recomputes risk. Economic inputs, policy and broker state must still match the reviewed hash. Source timestamps may advance, but all freshness checks run again. Approval expires after 90 seconds; stale context may block sooner. A changed price or partially filled order requires a new review.

## Files and account preparation

Use the existing pipeline files: `config.json`, `instrument.json`, `charges.json`, `context.json`, `replay.json`, and exactly one sized order in `orders.json`. Include every instrument appearing in today's account in `universe.json`. Instruments need current lot, tick and `freeze_quantity` evidence; missing freeze limits block placements. Broker/account changes require new analysis and acceptance.

```text
python scripts/live.py snapshot --workdir trader
python scripts/live.py reconcile-account --workdir trader
python scripts/live.py preview --kind entry --workdir trader
```

These commands send no orders. Reads use existing read-only adapters. Snapshot normalization rejects unsupported products, unknown/carry exposure, unmatched instruments, cross-account rows and malformed responses. Null arrays are not assumed empty. Orders' filled quantities must reconcile exactly with net positions before acting. The current execution mode supports a completely mapped intraday account, one open trade at a time; new entries require flat positions and terminal prior orders.

`reconcile-account` imports no fabricated fills. It runs journal reconciliation against the observed broker state, then writes the account ledger and `execution_state_hash` into `context.json` only on success. For a genuinely empty account, an empty journal/reservation list suffices. After trading, use the [broker trade importer](account-service.md) for actual executions. An explicit `reservations.json` takes precedence; otherwise known risk groups are reconstructed from the local execution log. Missing mappings block. Final fees are required by default; an explicit sourced provisional-fee policy can reserve conservative estimates without marking them final. See [account/service operations](account-service.md).

All live entries also obtain an exact, fresh broker margin estimate, including equities. The live profile adds caps to the analysis risk limits. Demo/synthetic source labels and the example `PAPER-DEMO` account are rejected for real entries. The tool does not authenticate accounts or buy subscriptions.

The current development version additionally requires fresh, dated `context.instrument_session` evidence for every live entry, even when optional in paper analysis. It binds the exact instrument and account to continuous-market hours and the broker square-off cutoff, excludes auctions and requires an official exchange source. See the [session contract](india-expansion.md). Missing evidence blocks entries; it does not block the separate reducing-exit/cancellation path. This is a supplied evidence check, not automatic exchange-rule certification.

## Human-operated actions

For the separate, limited Kite workflow that reduces supported CNC/NRML/MIS positions in an account with mixed inventory, see [mixed-inventory exits](mixed-inventory-exits.md). It retains exact human confirmations and the shared durable intent log. It does not relax this file's original intraday-only entry workflow.

Each following command requires its own two confirmations. Examples are syntax only, not approved prices or quantities:

```text
python scripts/live.py entry --workdir trader
python scripts/live.py cancel --order-id BROKER_ORDER_ID --workdir trader
python scripts/live.py stop --quantity 10 --trigger 99 --price 98.95 --workdir trader
python scripts/live.py exit --quantity 10 --price 102 --workdir trader
python scripts/live.py status --workdir trader
```

- Entry submits one intraday DAY limit order. It does not submit a stop or target automatically. An acknowledgement is neither a fill nor protection.
- Cancellation uses the observed order's actual variety and ID. Its acknowledgement does not release quantity. Wait for terminal cancellation, rejection, expiry or completion, then refresh positions.
- Exit submits one reducing limit order against a freshly observed position. It cannot reverse the position or exceed lot/freeze limits. Entry halts, daily entry budgets and expired entry acceptance do not prevent a reducing exit; the live tool itself still needs to be enabled.
- Stop submits a separate reducing stop-limit. SELL limit must be below the trigger; BUY limit must be above it. The trigger must not already have been reached. A stop-limit can remain unfilled after a gap.
- Any working order in that instrument blocks a new exit/stop. For partial entry fills, first cancel the unfilled remainder and confirm its terminal state. For a target exit, cancel the existing stop, confirm, refresh and authorize the newly sized exit. No independent stop and target orders reserve the same position twice. No automatic OCO or cancel-and-replace sequence is implied.
- `status` shows observed fills and protection. Only explicit broker trigger-pending status with exact position coverage is reported as `STOP_WAITING`. Generic pending states and acknowledgements remain `UNPROTECTED_OR_UNVERIFIED`. `STOP_WAITING` is not a guarantee of execution.

There are no market orders, modifications, automatic slicing, pyramiding, basket orders, delivery conversion or unattended flattening. Changing an order means cancel, reconcile and review a replacement. Other products and carry accounts remain unsupported for execution. Separate [carry accounting](portfolio.md) accepts canonical inventory evidence; its report cannot enable this intraday execution path.

## Failure recovery and emergency controls

`state/execution.sqlite` uses SQLite WAL with full synchronization. An exact review and `SUBMITTING` event commit **before** the HTTP attempt. A crash, timeout, HTTP error, invalid acknowledgement or interrupted request leaves an unresolved intent. There are no automatic retries or redirects, including retries on another broker. A missing order in a daily book does not establish that the request failed.

```text
python scripts/live.py status --workdir trader
python scripts/live.py bind --intent-id LOCAL_INTENT_ID --order-id VERIFIED_BROKER_ID --workdir trader
python scripts/live.py panic-preview --workdir trader
```

Known IDs reconcile through fresh broker reads. If a placement timed out without returning an ID, the human can explicitly attribute it with `bind`. This requires a same-day broker order whose instrument, side, quantity, limit and trigger match exactly, which did not exist in the pre-submit baseline and is not owned by another intent. The tool displays the evidence and asks twice. It never guesses ownership from a similar price. Binding updates local recovery state and sends no broker order. Absence or ambiguous attribution remains blocked and requires broker support/manual investigation; there is no force-clear switch.

Cancellation IDs are recorded before the request. A timed-out cancel retains its identity and remains locked until a terminal broker observation. Partial fills remain exposure even when the rest is cancelled. Regressing fill counts or contradictory terminal states block recovery.

`panic-preview` immediately writes the local entry halt, lists outstanding orders and current positions, and sends no order. Approve cancellations individually, confirm terminal states, refresh remaining positions, then approve reducing limit exits. If the API is unavailable or an intent is unresolved, use the broker's own controls and reconcile afterwards. Keep the original intent database; deleting it is not recovery. The tool does not promise that a stop, a limit exit or an exchange halt can guarantee flatness at a deadline.

## Protection-status interpretation (unreleased)

`status` evaluates waiting-stop quantities for the exact exchange, symbol, token and product. Remaining quantities exclude fills. `STOP_WAITING` requires exact coverage with no additional working entry or excess exit quantity; it never guarantees execution. Independent stop and target orders that could sell/buy more than the remaining position produce `OVER_EXIT_RISK`. Pending exposure-increasing orders produce `POSITION_CAN_INCREASE`; ambiguous working states produce `ORDER_STATE_UNVERIFIED`. Triggered limits and generic pending orders do not count as verified waiting stops. Reports include uncovered, excess-exit and working-entry quantities. These diagnostics send no orders and do not install unattended protection.

## Official contracts checked during implementation

Checked 5–6 November 2026; these references establish documented interfaces, not acceptance in a user's account.

- [Kite orders](https://kite.trade/docs/connect/v3/orders/): regular placement/cancellation, form encoding, order IDs and order-book state.
- [Upstox v3 placement](https://upstox.com/developer/api-documentation/v3/place-order/), [v3 cancellation](https://upstox.com/developer/api-documentation/v3/cancel-order/), [order book](https://upstox.com/developer/api-documentation/get-order-book/): HFT host and single-order ID array handling.
- [Dhan orders](https://dhanhq.co/docs/v2/orders/), [positions](https://dhanhq.co/docs/v2/portfolio/): v2 routes, filled quantities and static-IP requirements for order mutations.
- [Angel One official SDK routes](https://github.com/angel-one/smartapi-python/blob/main/SmartApi/smartConnect.py): placement/cancellation routes and variety. The code does not import the SDK or perform its IP discovery. The documentation website was inaccessible during this check; full schema/account acceptance remains outstanding.
- [FYERS placement](https://fyers.in/api-docs/v3/api/trading/order-placement/sync/single-order/), [cancellation](https://fyers.in/api-docs/v3/api/trading/order-management/sync/cancel-single-order/), [orders](https://fyers.in/api-docs/v3/api/trading/order-book/get-orders/), [positions](https://fyers.in/api-docs/v3/api/trading/positions/get-positions/), [funds](https://fyers.in/api-docs/v3/api/account/funds/): sync routes, success codes, ambiguous code 201 and named balance rows.

Read-only allowlists include GET trade-book routes and still reject execution mutations. `broker.py capabilities` describes those adapters and reports execution false; only the separate human-operated live tool has mutation routes.
