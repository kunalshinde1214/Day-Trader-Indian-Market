# Broker connectors

Each user brings their own broker. There are two ways to connect one:

1. **The broker's own connector (MCP server)**, added by the user in their assistant's connector settings. Several Indian brokers now publish one, for example Zerodha Kite, Upstox, Dhan and Groww. Their capabilities differ and change: some are read-only and some can place orders. Check the broker's own documentation.
2. **This skill's read-only adapters** for Kite, Upstox, Dhan, Angel One and FYERS, using API keys stored in the user's local environment. See [user setup](user-setup.md) and [brokers](brokers.md).

The publisher does not supply, test or hold any broker account. Each user's own connection is the only acceptance that matters for that user.

## Using a connector

When a broker connector is connected, use it to **read**: holdings, positions, orders, trades, funds and margins, quotes, historical candles, instrument details and fee/charge estimates. That data can stand in for the adapter's read-only collection:

- Name the connector and the time of each read in the answer.
- Check that the account matches the one in the user's config before combining it with plans or risk checks.
- If a connector field's meaning is unclear (units, lot multiplier, product code, collateral), say so and leave it out instead of guessing.

## Orders through a connector

Do not place, modify or cancel orders through a connector on your own initiative, and never as a side effect of analysis, scanning, monitoring or a risk PASS. The skill's output for a trade is a reviewed paper ticket. The user places the order in their broker app, or through the skill's separate human-operated live tool with its two typed confirmations ([execution](execution.md)).

If the user explicitly asks you to place one specific order through their connector, first show the exact account, symbol, exchange, product, side, quantity, order type, price and trigger, plus fees and margin from a fresh read, and get their clear confirmation of that exact order in chat. Ask again for every order. Any order sent through an API must follow SEBI's retail algo framework through the user's broker ([risk disclosure](../RISK_DISCLOSURE.md)).

## Credentials

Connectors handle login through the broker's own OAuth or 2FA flow. Never ask the user to paste API keys, passwords, tokens or TOTP seeds into chat.
