# Depth collection and quantity-dependent replay costs

Development after 0.9.9; updated 16 November 2026. Synthetic contract tests pass; authenticated broker acceptance is still performed by each end user with their own credentials.

## Cash-equity depth

```text
python scripts/depth_collect.py --instrument trader/instrument.json --interval 2 --out depth-new.json
```

The instrument selects Kite, Upstox, Dhan, Angel One or FYERS automatically. Use a same-day broker master and the user's configured local credentials. This makes two read-only quote collections; FYERS also reads its depth endpoint for each quote. It does not read portfolio holdings or place orders. Each observation validates exact identity, source time, a maximum five-level book, tick alignment, sorted unique prices and an uncrossed spread. Zero-quantity placeholders are discarded. Derivatives are rejected before collection because contract/volume conventions need separate acceptance.

`traded_units` is the change between two cumulative-volume observations, never the day's cumulative total. Source timestamps must advance 1–300 seconds within one session and volume must not decrease. The command's interval is 1–60 seconds. Rate limits and entitlements remain broker-specific; failed collection does not retry automatically.

| Broker | Source fields retained | Timestamp and use |
| --- | --- | --- |
| Kite | `depth.buy/sell`, `volume` | Feed `timestamp`; fresh trade time also required |
| Upstox | `depth.buy/sell`, `volume` | Quote feed `timestamp` |
| Angel One | `depth.buy/sell`, `tradeVolume` | `exchFeedTime`; fresh `exchTradeTime` also required |
| Dhan | `depth.buy/sell`, `volume` | `last_trade_time`; book-update time unavailable |
| FYERS | `bids/ask`, level `volume`, cumulative `v` | `ltt`; book-update time unavailable |

Kite, Upstox and Angel results can be supplied as `context.liquidity` to the existing optional depth stress model. Dhan/FYERS results explicitly set `depth_timestamp_available: false` and `liquidity_model_eligible: false`: they are research observations and the sizing model rejects them. Last-trade time is not proof of book freshness or the precise cumulative-volume sampling interval. Local fetch time never replaces a missing exchange timestamp. A fresh displayed book is still not a fill guarantee.

Provider contracts checked: [Kite quotes](https://kite.trade/docs/connect/v3/market-quotes/), [Upstox full quotes](https://upstox.com/developer/api-documentation/get-full-market-quote/), [Dhan market quotes](https://dhanhq.co/docs/v2/market-quote/), [Angel FULL market data](https://smartapi.angelone.in/docs/MarketData), [FYERS market depth](https://fyers.in/api-docs/v3/api/market-data/market-depth/). Documented routes are not proof of an account's entitlement or live compatibility.

## Replay cost sensitivity

Add these optional fields to the existing replay configuration. These are fictional assumptions, not calibrated Indian-market parameters:

```json
{
  "replay_cost_quantities": [10, 50, 100],
  "replay_cost_stress": {
    "mode": "linear_impact_v1",
    "latency_bps": 2,
    "impact_bps_at_reference": 5,
    "reference_quantity": 100
  }
}
```

Run the normal `trader.py replay` workflow. Quantities must be 1–10 distinct positive whole-lot amounts, within the instrument's supplied freeze quantity and a one-million-unit computational ceiling. No order splitting is inferred. Omit `replay_cost_stress` to compare fees alone. With neither field present, the original one-lot fee report remains unchanged.

For each terminal price in the same seeded replay paths, the report recalculates the supplied tariff at each quantity, including brokerage caps. Under the optional stress model, adverse basis points per leg equal `latency_bps + impact_bps_at_reference * quantity / reference_quantity`. BUY entry prices rise and exit prices fall; SELL entry prices fall and exit prices rise. Fees are recomputed at those hypothetical prices. Each shift must be below 10,000 bps.

`cost_sensitivity` records average base/stressed fees, adverse-price cost, gross and net mark-to-exit values, policy and rate-card hashes. Open paths use a hypothetical terminal exit, not realised profit. Changing or removing the cost policy requires regenerated replay evidence before sizing. Actual sizing still recomputes fees and its separately configured risk model; these research sensitivities are not automatically adopted as live loss reserves.

Limits: no calibrated latency, queue position, partial-fill prediction, historical tariff reconstruction, hidden liquidity, order splitting or price-path feedback. Target/stop frequencies remain unchanged by this cost-only sensitivity. Multiple orders or cancellations may have different fees. The report is not an execution authorization or maximum-loss guarantee.
