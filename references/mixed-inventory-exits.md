# Kite reducing exits from mixed inventory

The 0.9.8 implementation also supports reducing stop-limits. Use `preview --kind stop --trigger PRICE` with the ordinary product, quantity, limit and delivery-evidence arguments, then the human-operated `stop --trigger PRICE` command. Each review fetches a fresh exact broker quote. A sell trigger must be below the quote with a strictly lower limit; a buy trigger must be above the quote with a strictly higher limit. A reached/off-tick trigger, insufficient delivery authorization or any same-product working order blocks. Stop-limits may remain unfilled after triggering and do not create exchange OCO protection. Other brokers and unsupported inventory states remain blocked.

Unreleased development capability. This separate human-operated workflow can reduce a supported Kite CNC, NRML or MIS position while the same account contains other supported holdings or positions. It cannot open new trades, net different accounts, convert products, automate confirmations or guarantee fills. It is disabled without explicit profile opt-in and account-specific acceptance evidence. No authenticated acceptance has been performed by the publisher.

## Evidence and controls

The [Kite portfolio collector](kite-portfolio.md) rereads holdings, net/day positions, orders, trades and quotes against reviewed opening inventory. Its existing supported subset remains unchanged: settled, unpledged delivery inventory and supported unit-multiplier positions. T1, pledged/MTF, discrepant holdings and unexplained inventory changes still block. Every current order and position requires an exact dated master record.

`portfolio_exit.py` recomputes opening/fill/order/inventory reconciliation in an isolated temporary journal; it does not trust a saved report's `inventory_reconciled` flag. Provisional charges remain labelled but do not prevent a strictly reducing exit. The durable accounting journal is not silently rewritten.

Identity includes **exchange, symbol, token and product**. Ten CNC shares and five MIS shares of the same stock are separate positions; a CNC sale cannot use the MIS quantity. Any working order in the selected product must be cancelled and observed terminal before a replacement exit. Orders in another product do not reserve the selected product's units, but unknown order states anywhere in the account block reconciliation. Quantity cannot exceed the reconciled position, must be a whole lot, and must respect the supplied same-day freeze limit. Prices must align to the instrument tick.

The workflow supports reducing LIMIT orders and individual cancellations only. No MARKET order, batch flatten, slicing, reversal or carry-entry transport is enabled. Broker RMS remains authoritative, and local reconciliation cannot prevent an external actor from changing the account after the final read. Use one controlled account work directory and avoid concurrent manual or other-system trading during approval/submission.

## Setup

The work directory supplies `config.json`, `instrument.json`, `live-profile.json` and the shared `state/execution.sqlite`. The selected instrument must match a complete record in the supplied master. Existing live-profile identity, `enabled` and `approval_mode: "human_terminal_two_stage"` checks apply. Add these fields only after the end user's own acceptance process:

```json
{
  "mixed_inventory_reducing_exits": true,
  "mixed_inventory_acceptance": {
    "broker": "kite",
    "account_id": "YOUR_ACCOUNT_ID",
    "status": "PASS",
    "source": "Your actual account-specific acceptance record",
    "as_of": "YYYY-MM-DDTHH:MM:SS+05:30"
  }
}
```

Acceptance must be current within 24 hours. These are contract examples, not evidence of completed acceptance. Do not populate PASS from an offline demo. Reviewed opening sources marked synthetic/fictional/demo are rejected.

For a CNC sale, supply a fresh delivery-authorization record with exact `broker`, `account_id`, `exchange`, `symbol`, `token`, session `date`, `as_of`, `source`, `status: "AUTHORIZED"`, `method` (`DDPI`, `POA` or `CDSL_EDIS`) and `authorized_quantity` covering the proposed sale. This must describe currently usable authorization; do not copy the original daily grant after its units have been used. The local tool does not grant DDPI/POA, generate TPINs, perform OTP steps or bypass broker/depository checks. A cancellation does not require delivery authorization.

The [Kite product documentation](https://kite.trade/docs/connect/v3/orders/) distinguishes CNC, NRML and MIS. Zerodha explains that [selling holdings with MIS can create an intraday position rather than dispose of the holding](https://support.zerodha.com/category/trading-and-markets/general-kite/kite-holdings/articles/sold-holdings-bought-back). Its [CDSL authorization guidance](https://support.zerodha.com/category/trading-and-markets/trading-faqs/general/articles/validity-of-cdsl-tpin-authorisation) describes the daily authorization requirement when applicable. Sources checked on 11 November 2026; verify the user's current broker arrangements.

## Commands

Examples are syntax only; quantities, prices and paths require the user's own evidence. Preview performs current read-only broker collection and submits nothing:

```text
python scripts/portfolio_exit.py preview --workdir trader --opening opening.json --master master.json --product CNC --quantity 4 --price 120 --delivery-authorization delivery.json
```

Replace `preview` with `exit` to run the separate real-order path in the user's interactive terminal. It shows an exact review and requires the same two typed confirmations as the existing live tool. Full inventory is fetched and reconciled again after approval. Changed economics, position, order, authorization or profile evidence requires a new review. A durable intent is committed before HTTP. An acknowledgement is not a fill, and ambiguous submissions are never retried automatically.

```text
python scripts/portfolio_exit.py cancel --workdir trader --opening opening.json --master master.json --product CNC --order-id EXACT_BROKER_ORDER_ID
python scripts/portfolio_exit.py status --workdir trader --opening opening.json --master master.json --product CNC
python scripts/portfolio_exit.py bind --workdir trader --opening opening.json --master master.json --product CNC --intent-id EXACT_INTENT_ID --order-id EXACT_BROKER_ORDER_ID
```

`bind` requires human confirmation and exact observed order matching, including product. It never guesses a timed-out order from a similar price. The workflow shares the existing execution log, preventing another action while that log contains an unresolved intent. Carry exits cannot be imported as intraday entry/exit risk groups; reconcile them through the portfolio accounting workflow. The original `live.py` path remains strict intraday-only.

This increment covers **Kite reducing actions only**. New entries in mixed inventories, other brokers' carry execution, T1/pledged/MTF inventory, settlement/corporate-action automation and unattended protection remain unfinished. This is not authenticated or production-service acceptance.
