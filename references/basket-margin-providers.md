# Basket margin provider coverage

Included in 0.10.0, checked 16 November 2026. Read-only research for Kite, Upstox, Angel One and FYERS. End users configure their own local credentials; offline tests do not establish authenticated acceptance.

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
| Dhan | No basket adapter implemented | Single-order calculators are not summed to pretend that hedge benefits are known |

`peak_estimate_inr` is the largest reported requirement among the requested prefixes/scenarios, not a maximum across all possible fills or market states. `final_required_inr` is the requirement for the final ordered basket. `final_estimate_inr` is only populated where the provider reports a distinct final figure; it is `null` for Angel/FYERS. FYERS additionally reports `peak_account_total_inr`; do not add this to its proposed-order figure, as that would double-count exposure.

Each estimate retains the request and response hashes and its interpretation. Upstox/Angel/FYERS set `response_identity_verified: false` because their responses do not independently echo the exact submitted legs. Request hashes establish what this program sent, not what the provider computed. All reports retain `execution_eligible: false`; they do not authorize basket orders.

Still incomplete: Dhan baskets, independently accepted account semantics, collateral/haircut and cash-component scenarios, volatility/price/expiry stresses, dynamic margin changes and exhaustive fill permutations. No local SPAN replica or funding guarantee is claimed.

Primary contracts: [Kite margins](https://kite.trade/docs/connect/v3/margins/), [Upstox margin details](https://upstox.com/developer/api-documentation/margin/), [Angel SmartAPI margin calculator](https://smartapi.angelone.in/docs), [FYERS multi-order margin calculator](https://fyers.in/api-docs/v3/api/calculators/multi-order-margin-calculator/).
