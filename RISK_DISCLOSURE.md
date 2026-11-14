# Trading risk and user responsibility

**Use India Day Trader at your own risk.** This is experimental software for analysis, paper workflows and separately human-confirmed execution. It is not investment, legal or tax advice; a signal, scenario or backtest is not a recommendation or a guarantee of future performance.

## Money and execution

Trading can cause substantial losses. Leverage, derivatives, gaps, illiquidity, fees and slippage can make losses exceed a planned stop or expected risk amount. A stop-limit may not fill. An order acknowledgment is not proof of a fill, and an entry does not automatically create a protective order.

Check the exact broker account, exchange, instrument, expiry, product, side, quantity, price, trigger, fees and margin before every action. Remain able to use your broker's own controls if this software, your network or a data source fails. Monitoring requires an active process and working connection; a stopped process cannot protect a position. Current live actions require individual human confirmation and do not provide unattended protection or automatic flattening.

## SEBI rules for API and algo trading

Orders sent through a broker API in India fall under SEBI's retail algorithmic trading framework (circular of 4 February 2025), which [SEBI's implementation timeline](https://www.sebi.gov.in/sebi_data/attachdocs/sep-2025/1759232056254.pdf) applies to all stock brokers from April 2026. In short:

- Your broker is responsible for every API order and must identify it with an exchange-issued algo ID. A free-form order tag is not an algo ID.
- Brokers may require a registered static IP, a personal API key, two-factor login and daily session expiry before accepting API orders.
- Strategies that send orders above the exchange's orders-per-second threshold must be registered with the exchange through your broker.

This software does not register algos, obtain an algo ID, or confirm that your setup complies. The live tool needs a human to confirm each order, but you are still responsible for checking with your broker how its API orders are classified and what they require. See the [NSE retail-algo page](https://www.nseindia.com/static/trade/platform-services-non-neat-decision-support-tools-algorithm-trading). Research, reports and paper trading place no orders and are outside this framework.

## Data, models and AI

Exchange/broker data can be unavailable, delayed, incomplete or revised. Instrument tokens, lot sizes, restrictions, sessions, fees and broker requirements can change. Missing evidence must not be treated as clearance. Verify current source evidence and permissions independently.

Synthetic demos use fictional inputs and a test clock. Historical replay has modeling limitations and does not establish a profitable strategy. AI assistants can misunderstand instructions, misinterpret market information or generate incorrect explanations. Review generated reports and commands yourself; never let an assistant supply the required human trade confirmations.

## Alpha status

The full production specification is unfinished. Offline tests and packaging checks are not authenticated broker acceptance, an audit of live trading reliability, regulatory approval or a suitability assessment. The implementation and unresolved work are listed in [release status](references/release-status.md). Start with offline demos and paper mode. Keep live execution disabled until you have completed your own applicable acceptance checks and understand its limits.

## Account and legal responsibility

Keep credentials private and use only accounts and data you are authorized to access. You are responsible for your trading decisions, account security, subscriptions, exchange/broker permissions, fees, taxes and applicable legal obligations. The software license does not grant market-data rights or authorization to provide financial services. No affiliation with or endorsement by any named broker, exchange or regulator is claimed.

## Warranty and liability

The [license](LICENSE) contains the operative warranty and liability terms. To the extent permitted by law, the covered software is provided AS IS and AS AVAILABLE, without promises of accuracy, availability, fitness or trading performance. This disclosure does not exclude liability or statutory rights that cannot lawfully be excluded. Seek qualified advice for your circumstances where needed.
