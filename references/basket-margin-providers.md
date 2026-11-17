# Basket margin provider coverage

Four providers were included in 0.10.0. Version 0.10.1 (checked 17 November 2026) adds Dhan. End users configure their own local credentials; offline tests do not establish authenticated acceptance.

```text
python scripts/portfolio_margin.py --input basket.json --out basket-margin-new.json
```

Use the existing [basket input format](india-expansion.md#exact-basket-margin-research), selecting `broker` and that user's observed `account_id`. Each of 1–20 ordered legs contains a same-day exact `instrument`, matching `account_id`, canonical `product` (`MIS`, equity `CNC`, derivative `NRML`), `side`, whole-lot `quantity` and tick-aligned limit `price`. Freeze limits apply when supplied. All legs must belong to one broker/account. Upstox additionally forbids repeated instrument keys across different products. No MTF or cross-account netting is inferred.

Every ordered prefix is estimated. Optional `fill_scenarios` or `fill_grid` also estimates up to 40 explicitly enumerated nonempty partial baskets. Broker calls stop on error; no failed request is retried automatically. The three new provider routes require fresh account snapshots and a collection window of at most 60 seconds. Changed positions, orders or margins invalidate the whole result. Large grids may exceed an account's rate limits or this timing bound; use a smaller explicitly chosen set instead of treating a partial run as complete.

| Provider | API and result semantics | Limits |
| --- | --- | --- |
| Kite | `/margins/basket`, `consider_positions=true`, initial/final totals | Existing response exchange/symbol checks; not every fill permutation |
| Upstox | `/v2/charges/margin`, `instruments`; required/final totals | Requires one margin row per requested leg; no echoed leg IDs; existing-position inclusion unverified |
| Angel One | `/rest/secure/angelbroking/margin/v1/batch`, `positions`; `totalMarginRequired` | No invented initial/final split or echoed identities; existing-position inclusion unverified |
| FYERS | `/api/v3/multiorder/margin`, ordered `data`; `margin_total` | Proposed-order margin kept separate from `margin_new_order`, which includes existing positions; no echoed identities |
| Dhan | `/margincalculator/multi`, SDK spelling `scripList`, `includePosition`, `includeOrder`; documented INR `total_margin` response | Account-inclusive requested estimate, not incremental margin; no initial/final split or echoed leg identity. Documentation contains conflicting request examples; user contract acceptance is required |

`peak_estimate_inr` is the largest reported requirement among the requested prefixes/scenarios, not a maximum across all possible fills or market states. `final_required_inr` is the requirement for the final ordered basket. `final_estimate_inr` is only populated where the provider reports a distinct final figure; it is `null` for Angel/FYERS/Dhan. FYERS and Dhan additionally report `peak_account_total_inr`; do not add this to another requirement figure and double-count exposure. Dhan's total is requested with existing positions/orders included and must not be interpreted as additional cash needed.

Each estimate retains the request and response hashes and its interpretation. Upstox/Angel/FYERS/Dhan set `response_identity_verified: false` because their responses do not independently echo the exact submitted legs. Request hashes establish what this program sent, not what the provider computed. Dhan uses the official SDK request spelling and requires the documented snake-case INR response. A different response blocks; no alternative contract or single-leg fallback is retried. All reports retain `execution_eligible: false`; they do not authorize basket orders.

Separate [explicit collateral and cash-component scenarios](risk-scenarios.md) are now available. Independently accepted account semantics, calibrated volatility/price/expiry stresses, dynamic margin changes and exhaustive fill permutations remain incomplete. No local SPAN replica or funding guarantee is claimed.

Dhan contracts: [funds and margin documentation](https://dhanhq.co/docs/v2/funds/), [official SDK implementation](https://github.com/dhan-oss/DhanHQ-py/blob/main/src/dhanhq/_funds.py) and [SDK HTTP account binding](https://github.com/dhan-oss/DhanHQ-py/blob/main/src/dhanhq/dhan_http.py). The SDK and one documentation example use `scripList`/`includeOrder`; another documentation example uses `scripts`/`includeOrders`. The SDK transport also adds `dhanClientId`, which this implementation binds to the observed expected account. The implementation chooses the SDK contract explicitly and records user-account acceptance as unverified.

Primary contracts: [Kite margins](https://kite.trade/docs/connect/v3/margins/), [Upstox margin details](https://upstox.com/developer/api-documentation/margin/), [Angel SmartAPI margin calculator](https://smartapi.angelone.in/docs), [FYERS multi-order margin calculator](https://fyers.in/api-docs/v3/api/calculators/multi-order-margin-calculator/).
