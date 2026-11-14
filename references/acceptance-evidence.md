# End-user acceptance evidence

Generate a disabled local profile with `python scripts/user_setup.py --broker BROKER --account-id NONSECRET_ACCOUNT_ID --out trader/live-profile.json`. This writes no credentials, refuses overwrite, and leaves all execution/automation flags false with acceptance `NOT_TESTED`. Users complete their own broker login and observed checks before enabling any live workflow.

The publisher does not need a broker account to distribute the package. Each user must test their own account, entitlements, host and operating environment. Never publish their evidence files or credentials in the release archive.

Use `scripts/acceptance.py --broker BROKER --authenticated-readonly --instrument trader/instrument.json --account-id ACCOUNT --out private/broker-readonly.json` for the supported account and quote/history probe. This reads data and does not place an acceptance trade. Public-master probes cannot substitute for authenticated acceptance.

Record installed-host invocation, production-service observations, and broker-approved automation operating checks separately. Required categories are `broker_readonly`, `installed_host`, `production_service`, and `automation_operating`. The latter three evidence documents contain the exact `broker`, `account_id`, current `as_of`, `category`, `status`, real evidence `source`, `observed: true`, and nonempty `checks` mapping named observations to boolean results. Do not mark an unperformed test true. Include host/plugin version and observed invocation in the source artifact, and exercise service stop/restart, disconnect, stale-feed, revocation and ambiguous-request recovery in an authorized non-trading test environment before real use. A configured flag alone is not evidence of broker approval.

Create an inventory JSON with `schema: acceptance_inventory_v1`, exact `broker`, `account_id`, and `evidence` mapping category names to `{ "path": "relative-file.json", "sha256": "actual-file-sha256" }`. Keep evidence under the inventory directory. Then run:

```text
python scripts/readiness.py --input private/inventory.json --out private/readiness-new.json
```

The report checks file hashes, identity, declared test outcomes and a 24-hour freshness window. Missing categories remain `MISSING`; malformed, changed or stale evidence blocks. A successful inventory is labelled `EVIDENCE_RECORDED`, never independently certified or execution-authorized. This report does not enable a live profile or replace the runner's human arming. No broker permission, host invocation or production-service operation has been performed merely by generating this report.
