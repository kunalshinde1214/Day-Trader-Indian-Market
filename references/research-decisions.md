# Research decisions and corrections

Checked 31 October 2026. Sources below support only their cited claims. Refresh operational facts before real-market use; a date printed on a report is not proof of currency.

| Supplied claim / conflict | Decision |
| --- | --- |
| Liquidity prose says ₹10 crore; config says `10000000` | Use `100000000` (₹10 crore), preserving the stated prose threshold. It remains configurable. |
| Every retail strategy requires its own exchange registration and unique ID; 10 OPS defines HFT | Correct this. NSE distinguishes client-developed algos below the threshold using a generic ID from above-threshold registered strategies. Provider/broker algos have separate requirements. Do not label the threshold an HFT classification or fabricate a unique ID. [NSE April 2026 system-audit circular](https://nsearchives.nseindia.com/content/circulars/INSP73850.pdf), [implementation standards](https://nsearchives.nseindia.com/content/circulars/INVG67858.pdf). |
| Cash 09:15–15:30; equity derivatives until 15:40 | NSE's timing page currently lists those hours. Use dated segment/session inputs and recheck special-session circulars; broker square-off is a separate deadline. Default strategy flat-by remains 15:05. [NSE market timings](https://www.nseindia.com/static/market-data/market-timings). |
| “Closing session computes prices” | Avoid asserting a generic closing-price algorithm. The timing page establishes session times; price formation/auction changes require the relevant circular. No closing-price computation is implemented. |
| Fixed lot 65, tick 0.05, Tuesday/Thursday expiries | Examples only. Read the exact contract master; holiday-shifted expiries and lot revisions must not be inferred. [Kite instrument master](https://kite.trade/docs/connect/v3/market-quotes/). |
| Blanket index-option ELM 3%; automatic expiry spread-margin rules | Do not implement a fixed margin percentage. Require the broker's exact-order margin result, with account state, for derivative risk PASS. [Kite margin API](https://kite.trade/docs/connect/v3/margins/). |
| STT 0.05% futures sell, 0.15% option sell premium from April 2026 | Included in dated configuration; confirmed against the [exchange tax circular](https://nsearchives.nseindia.com/content/circulars/FATAX73524.pdf) and [Zerodha charge table](https://zerodha.com/charges/). |
| One 65-unit option round trip at premiums 100 and 120 costs about ₹65.11 | Reproduced as an estimate and regression check. Quantities/prices are a test example, not a live contract assertion. Source: supplied report p. 17, corroborating charge table above. |
| “All-in” cost is exact | Call it an estimate. Two-decimal component display may not sum exactly to unrounded aggregate. Contract-note STT rounding, extra levies, slippage, exercise, assignment, DP, call-and-trade and forced-square-off fees are not modeled. |
| Native brackets unavailable; all stops software-monitored | A preview installs no stop at all. Product availability varies: Kite docs list cover orders and separate GTT APIs. No assumption of OCO, monitoring, or execution is made. [Kite order API](https://kite.trade/docs/connect/v3/orders/). |
| Never print any profit figure versus calculate cost-adjusted expectancy and tax ledger | Scans show no performance promises. Tickets expose planned reward and stop-loss arithmetic for review; replay exposes outcome frequencies and estimated costs without an invented return forecast. Tax ledger work remains separate. |
| Draw overnight gaps for every intraday replay | For an entry after 09:30, the opening gap is already observed. The first implementation bootstraps the remaining intraday path and retains open/previous-close jumps. Pre-open gap models are a separate backlog item, not silently claimed. |
| Report's STCG table puts ₹1.25 lakh exemption on short-term gains; audit turnover claims | Do not encode tax thresholds or return-filing advice from this report. Keep charges separate for later CA-reviewed exports. No tax calculator is shipped. |

Market-size, retail-loss, industry and UPI statistics are retained in the source archive as context, not embedded into executable rules or onboarding claims. The latest-year statistics were not independently verified in this build.

## Rate-card limits

`charges.example.json` is a verified-on snapshot with a conservative review deadline, not a claim that rates cannot change before that date. Decimal fractions, not percentages, are stored. The implementation covers intraday round trips only. BSE instrument-specific fee categories and broker-account-specific charges require separate verification. The rate-card end date is a development review control, not an exchange-announced expiry.

## Preserved upstream ideas

The original repository's architecture is valuable: file outputs make intermediate decisions inspectable; sizing and risk are separate; passing an order is not consent; bootstrap paths have a reproducible seed; risk is reserved across an ordered batch. The implementation retains these ideas while replacing US clocks, symbols, dollars and Alpaca bracket payloads.
