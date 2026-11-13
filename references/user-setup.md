# Bring your own broker account

The publisher supplies the skill, not a brokerage account, credentials, data subscription or approved risk budget. Public research works without a brokerage account. Each user configures and tests their own broker locally. Publisher-side authenticated acceptance is not a release prerequisite, and must never be represented as already passed.

## Start without an account

```bash
python scripts/india_news.py --sources nse-announcements nse-actions et-markets
python scripts/public_market.py scan --symbols RELIANCE.NS TCS.NS
python scripts/daily.py india-daily --collect-public
```

Public daily collection can block outside a supported current session or when data is stale/incomplete. Synthetic demos remain explicitly labelled. Read [news and filings](india-news.md) for automatic filing collection, and [public workflows](public-workflows.md) for research limitations.

## Configure your own broker

1. Choose Kite, Upstox, Dhan, Angel One or FYERS and read [broker setup](brokers.md). Use the broker's own login/2FA process and required data entitlements.
2. Initialize a new work directory using `python scripts/trader.py init --broker BROKER --workdir trader-BROKER`. Replace BROKER with the supported lowercase broker name. Set your non-secret account ID and your chosen capital/risk limits. Defaults are examples, not approvals.
3. Configure the documented environment variables locally. Never paste credentials into chat, commits, issues or reports.
4. Collect a same-day instrument master and select the exact instrument. Supply current fees and dated calendar/restriction/event evidence. Public prices and news cannot fill missing account or eligibility evidence.
5. Run read-only acceptance locally with your instrument record and expected account ID:

```bash
python scripts/acceptance.py --broker kite --authenticated-readonly --instrument trader-kite/instrument.json --account-id YOUR_ACCOUNT_ID --out reports/kite-readonly-acceptance.json
```

Use the selected broker name and work directory; choose a new output file each time. The harness checks observed account identity and selected-instrument candle/quote responses. Optional `--payload` tests exact proposed margin evidence; it does not place an order. A missing/expired credential, unsupported instrument or unavailable market session can block. A PASS covers only the tested reads, not every instrument or live execution.

6. Run the [paper pipeline](commands.md), reconcile fills and review costs. Keep the live profile disabled until you independently review the [execution workflow](execution.md). Any real action remains a separate exact-order human decision; an assistant cannot type confirmations. No unattended trading, automatic protection or account-wide certification is supplied.

## Reporting problems

Share the script name, version, redacted error category and whether the failure was during public collection or authenticated reading. Never share tokens, cookies, full raw account responses or sensitive order/position details. Source failures remain visible; the skill must not silently substitute invented data.

Remaining roadmap work includes additional broker streaming/carry support, unsupported inventory adjustments, broader exchange-context coverage and advanced execution/replay models. These are limitations of the product, not tasks users are expected to repair to use its documented public and paper workflows.
