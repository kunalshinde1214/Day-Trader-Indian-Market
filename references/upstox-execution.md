# Upstox mixed-inventory reducing actions

This local development increment adds Upstox to `portfolio_exit.py`: reducing limit exits, stop-limits and cancellation, using the existing [Upstox collector](upstox-portfolio.md). Supported settled/unpledged equity and unit-multiplier overnight derivatives retain canonical CNC/NRML identity. Upstox's wire product is `D` for both; MIS remains `I`. The ordinary intraday entry tool is unchanged. New mixed-inventory entries, MTF/T1/pledged inventory, product conversion and cross-account netting remain unsupported.

## User-owned setup

No publisher account is needed. Each user obtains credentials through Upstox's own login and stores them in the documented local environment; never paste keys or access tokens into chat or setup arguments. Begin with a disabled profile:

```text
python scripts/user_setup.py --broker upstox --account-id YOUR_NONSECRET_ACCOUNT_ID --out trader/live-profile.json
```

The generator refuses to overwrite a file. All execution switches start false and all acceptance statuses are `NOT_TESTED`. Its example caps are starting configuration values, not trading recommendations. Follow [broker setup](brokers.md), [acceptance evidence](acceptance-evidence.md) and [mixed-inventory requirements](mixed-inventory-exits.md). The user supplies a same-day complete master, reviewed opening statement, configured account and instrument, and their own observed acceptance evidence before enabling mixed-inventory reducing actions.

For placements, also supply `--session trader/instrument-session.json`, using the exact dated instrument/session contract from [Indian-market expansion](india-expansion.md). This is required because the Upstox API can infer after-market handling from session time. Cancellations do not require the placement session gate. Delivery sales/stops require the same fresh instrument/account/quantity authorization evidence as the Kite workflow; the tool never performs eDIS/TPIN or broker login for the user.

```text
python scripts/portfolio_exit.py preview --workdir trader --opening trader/opening.json --master trader/master.json --product CNC --quantity 1 --price 100 --session trader/instrument-session.json --delivery-authorization trader/delivery.json
```

Replace illustrative quantities/prices with the user's reviewed values. Use `--kind stop --trigger PRICE` to preview a stop-limit. Human-operated `exit`, `stop` and `cancel` commands repeat collector reads around two exact terminal confirmations. The agent must not enter confirmations. Same-product working orders must reach terminal cancellation before replacement; partial fills change the remaining exit quantity. Shared execution intents, product-aware recovery and ambiguous-request locks remain in force. No automatic retry or market conversion is performed.

Sources checked 14 November 2026: [Upstox placement](https://upstox.com/developer/api-documentation/v3/place-order/) and [order book](https://upstox.com/developer/api-documentation/get-order-book/). Offline tests establish implementation contracts only. End users validate their own broker permissions and account behavior; no real account or trade was used during development.
