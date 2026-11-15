# Historical constituents and corporate actions

Development after the published 0.9.9 ZIP. These are local source-review workflows. They do not supply a complete licensed history, independently authenticate a document, or certify survivorship-free research.

## Membership at a date and information cutoff

```text
python scripts/membership_history.py --input membership-history.json --session 2026-11-13 --known-at 2026-11-13T10:00:00+05:30 --out membership-new.json
```

The input schema is `index_membership_history_v1`, with `index_id` (`nifty`, `banknifty`, `nifty500` or `sensex`), `reviewed: true`, `artifacts` and `snapshots`. Keep the actual source documents beside the manifest. Each artifact has a relative `path`, exact `sha256`, official index/exchange `source` URL and offset-bearing `published_at`. The reviewer must verify the publication time and effective period against the document; a download timestamp is not a publication timestamp.

Each snapshot has these exact fields:

```json
{
  "snapshot_id": "your-source-interval-id",
  "effective_from": "2026-11-01",
  "effective_through": "2026-11-30",
  "evidence_id": "your-artifact-id",
  "expected_count": 2,
  "members": [
    {"exchange": "NSE", "symbol": "DEMOA", "isin": "INE000000001"},
    {"exchange": "NSE", "symbol": "DEMOB", "isin": "INE000000002"}
  ]
}
```

This is a two-member fictional format illustration, not a Nifty constituent list. Supply the full sourced list and its actual count. NSE is required for the Nifty indices; BSE is required for Sensex. Membership uses ISIN identity, retaining symbol renames separately from additions/removals. An ISIN change requires a different member identity; this workflow does not guess mergers or issuer continuity.

Intervals must be ordered and non-overlapping. A lookup outside supplied coverage or inside a gap blocks. A selected document published after `--known-at` blocks. Changes are compared only between snapshots available by that cutoff, and comparisons across gaps are labelled. A historical session queried using a later information cutoff is explicitly distinguished from a same-session cutoff. No current list is projected backwards.

The output can be supplied to `daily.py --collect-kite --with-context --membership membership-new.json` for the same date, within the normal freshness limit. Combine independently generated index objects into one input if collecting multiple indices. Nifty 500 is supported by the history tool, not the three-index daily report. Daily breadth keeps `membership_verified: false` and the reviewed-source explanation. Changed generated output is detected by its content hash; hashes are integrity checks, not signatures or source authentication.

[NSE Indices' data subscription page](https://www.niftyindices.com/offerings/data-subscription) describes historical/constituent data products. The [reconstitution schedule](https://www.niftyindices.com/resources/index-rebalancing-schedule) supplies scheduling context, not the full membership of a particular historical session. Use authorized source files; no subscription or redistribution entitlement is included here.

## Split and bonus reconstruction

```text
python scripts/corporate_history.py --input equity-actions.json --known-at 2026-11-13T18:00:00+05:30 --out adjusted-history-new.json
```

Input uses `schema: "equity_action_history_v1"`, `reviewed: true`, `actions_complete: true`, `reference_session`, `artifacts`, `history` and `actions`. Artifacts use the same relative-path/source/hash format; action documents also require `published_at` and an official exchange/index host.

`history` contains `exchange`, exact `symbol`, `initial_isin`, `price_basis: "raw_unadjusted"`, `evidence_id` for the raw-price source file and `bars`. Each daily bar has exactly `session`, `isin`, `open`, `high`, `low`, `close`, `volume` and `available_at`. Supply final daily observations with their actual availability times. Rows must be strictly ordered, available by the information cutoff and no later than the reference session.

Each action contains exactly `action_id`, `kind` (`split` or `bonus`), `ex_date`, `evidence_id`, `old_isin`, `new_isin`, `numerator` and `denominator`. Dates must be ordered and distinct, after the first bar and no later than the reference session. Supply a complete reviewed action window. A split ratio means **new shares per old share**; a bonus ratio means **additional shares per existing share**. A 2-for-1 split therefore uses 2/1; a one-for-one bonus uses 1/1. Reverse splits are supported. ISINs must form a consistent chain and each raw bar must match the identity effective on its session.

Earlier OHLC prices are divided by the cumulative share factor; earlier volume is multiplied by it. The output preserves source ISINs and gives all adjusted rows the reference-session ISIN. Adjusted volume is synthetic share-equivalent volume, never evidence of executable liquidity. The result includes source hashes, actions, factors and the complete input hash.

Already-adjusted or unspecified price bases are rejected. Do not relabel a provider's adjusted data as raw. In particular, this command is not automatically applied to Yahoo charts or broker histories; their actual adjustment conventions must first be established. Output is a separate research schema and cannot replace a broker master or execution input.

Dividends/total returns, rights, mergers/demergers, simultaneous actions, fractional entitlements, taxes, automatic corporate-action discovery and independent verification remain unsupported. The [inventory reconciliation tool](inventory-book.md) addresses a separate accounting use case; adjusted research prices do not change holdings or authorize orders.
