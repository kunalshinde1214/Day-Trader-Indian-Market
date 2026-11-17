# Portfolio protection and group coordination

Added in 0.10.1, 17 November 2026. Experimental code tested with simulated broker responses. Users must validate their own account and runtime. Nothing starts automatically.

## Carry-aware policy

Use the ordinary [runner arming workflow](protection-controller.md), with a separate `portfolio_protection_policy_v1` policy. All ordinary policy fields remain required; `product` selects exactly one `MIS`, `CNC` or `NRML` position. Carry flattening is an explicit owner policy with a same-day cutoff, not a default assumption that all investments should be sold.

Add the reviewed `opening.json` and full `universe.json` to the work directory. These files become part of the arming scope; changing them requires new human review and arming. Enable `portfolio_unattended_protection` separately in the live profile, as well as the existing unattended and mixed-inventory opt-ins and account-specific acceptance evidence. All settings start disabled.

Each observation recollects broker inventory, fills and detailed orders, reconciles them to the opening book, and rechecks exact product quantities. Stops and exits use the existing reducing-only portfolio execution path. CNC placements require fresh `delivery-authorization.json` with exact account, instrument, quantity and supported authorisation method. This file is rechecked at action time; the runner does not manufacture or renew it. Existing instrument-session evidence remains mandatory. Unsupported restricted holdings still block.

The controller distinguishes carry and intraday positions in the same security. It cancels conflicting orders for the selected product, waits for authoritative cancellation, then recomputes remaining units. An exit trigger stays latched after a price reversal. No target/stop pair is simultaneously submitted as independent exits.

## Group coordination

Create a `protection_group_v1` settings file containing `workdirs` (one to twenty absolute paths, or paths relative to the group directory) and `max_actions` (one to one hundred submissions for the invocation). Each member has its own dated policy, work directory and independently human-armed mandate. Instruments at the same broker/account must configure the identical absolute `execution_database` path ending in `.sqlite` in each `config.json`. Configure sharing before arming. All live, mixed-entry, reducing-exit and runner commands use this setting.

The default remains `state/execution.sqlite`. Switching to a shared path is refused if a different local execution database already exists. Preserve and reconcile all existing history; never delete it to bypass the check. Migration of existing journals is not automated.

Example settings (replace the paths and arm each member yourself):

```json
{"schema":"protection_group_v1","workdirs":["../nifty-position","../bank-position"],"max_actions":8}
```

```bash
python scripts/protection_group.py --settings group-settings.json --directory protection-group --cycles 12 --interval 5
```

The coordinator locks all member directories before starting and runs them sequentially, using each member's full-account reconciliation and shared intent history. It never offsets risk or funds between accounts. Each process supports one account per broker, allowing different brokers together; credentials are never switched automatically. For a carry account, use the portfolio policy for its intraday members too, so full-account carry inventory is reconciled.

The group checks all mandates and scope hashes before each member step. A failed or ambiguous member action or interruption revokes the whole group and every member. Reconcile the broker books and durable logs before explicitly clearing latches and rearming. A group revocation or file change cannot retract a request already in flight. Existing independent software can still alter an account, so each action recollects and rechecks broker state.

Health reports distinguish a cycle/action limit from observing all selected scopes flat. Neither observation means every unselected account position is flat. The group creates no service and cannot guarantee fills, connectivity, broker permission or production reliability. Fresh delivery and session evidence can expire and block action; do not lengthen freshness merely to keep it running.
