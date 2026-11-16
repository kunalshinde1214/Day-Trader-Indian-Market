# Public closing reports and daily context

Included in 0.10.0. These commands need no broker account:

```text
python scripts/daily.py india-daily --collect-close --session 2026-11-13
python scripts/daily.py nifty --collect-close --session 2026-11-13 --format json --out nifty-close-new.json
python scripts/daily.py banknifty --collect-close --session 2026-11-13
python scripts/closing_context.py --session 2026-11-13 --out closing-context-new.md
```

Choose the actual report date. The date is never guessed from weekdays or silently moved to an earlier successful download. A dated closing report can be read on a weekend without claiming that the market is open. The report is a separate `india_closing_context_v1` observation schema; it does not fabricate five-minute candles, intraday scenarios or an open-session calendar. Current-session Yahoo and configured Kite routes remain separate.

## Collected observations

| Component | Source and interpretation |
| --- | --- |
| Nifty 50 and Bank Nifty closing values | NSE's dated `ind_close_all_DDMMYYYY.csv`; exact names, dates, OHLC and change arithmetic checked. Source URL, retrieval time and hash retained. |
| Cash-market breadth | Dated NSE full bhavcopy/security-delivery report; compare EQ closing values with the report's previous closes. Other series are excluded and unavailable EQ values are counted. This is observed report breadth, not all Indian-market breadth. |
| FII/FPI and DII | NSE's public institutional-flow endpoint. Both categories must have one date and consistent buy/sell/net arithmetic. Source units are crore rupees, with a separate INR conversion. |
| GIFT Nifty | The separately timestamped NIFTY futures observation in NSE's market-status response. It retains expiry, timestamp, age and a stale marker. It is never substituted for the domestic spot index or a broker contract. |

The new source does not supply Sensex. Combined reports explicitly mark it unavailable; requesting only Sensex through this route returns a nonzero status. Use the existing public/Kite/sourced-input Sensex routes where their own evidence is available. No Nifty value is relabelled as Sensex.

For constituent breadth, supply the [historical membership workflow's](historical-evidence.md) generated exact-date JSON with `--membership`. Missing symbols remain visible. Content hashes detect changed inputs, but source authentication and membership verification remain false. Broader EQ breadth does not become constituent breadth without this input.

## Dates, release times and completeness

Institutional flows retain their own trading date, which may differ from the requested closing session. The report calls out that difference. Neither retrieval time nor the trading date is treated as an independently verified release timestamp or finality confirmation. These observations cannot establish what was available to a historical strategy at a particular time.

GIFT's source timestamp currently omits an offset; the report explicitly labels the IST assumption. Observations older than 180 seconds are marked STALE. Recent observations still carry the unverified-timezone label. Contract expiry is retained, and the JSON also flags an expired contract. No gap-opening prediction or executable price is inferred.

All output remains partial, with `execution_eligible: false`, `full_market_coverage: false` and `submitted: false`. Each component fails independently. Missing/stale files, membership, macro events and provider finality remain gaps. The report does not infer special-session timings, auction inclusion or trading clearance from a CSV file name.

## Verification

A public read-only smoke test on 15 November 2026 obtained the 13 November Nifty/Bank Nifty report and 2,744 valid NSE EQ observations, institutional-flow observations and an explicitly stale GIFT observation. No brokerage account or order endpoint was used. Source samples and generated reports remain in excluded local test output.

Primary references: [NSE daily reports](https://www.nseindia.in/all-reports), [FII/DII reports and crore units](https://www.nseindia.in/reports/fii-dii), [market-status observation](https://www.nseindia.in/api/marketStatus). These establish the source context, not guaranteed future endpoint availability.

Remaining work includes a BSE closing provider, complete macro-event feeds, independently verified release/finality and membership evidence, and broader provider compatibility. See the [ordered checklist](completion-tracker.md).
