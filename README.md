# Binance leverage bot

Trades Binance USD-M futures with three trend strategies chosen by a two-year test against random entries
(`python -m tradebot.research`). Every signal goes through a full check list before an order is sent: signal,
six confirmations, market (spread, price run, funding), risk (budget, open positions, daily loss, drawdown) and
size (margin, exchange minimum, stop distance, liquidation distance, costs). Each position gets a stop order on the
exchange at once; trend trades trail it after every bar.

Money rules (all in `.env`): the bot uses `BUDGET_USD` (100) in total, at most `MARGIN_PER_TRADE_USD` (10) margin
and `RISK_PER_TRADE_USD` (1) loss per trade, isolated margin, at most `MAX_LEVERAGE` (5x).

- `TRADE_MODE=demo` (default) trades Binance's demo account with fake money. `TRADE_MODE=live` also needs
  `LIVE_ARMED=YES`.
- Keys: `BINANCE_TESTNET_API_KEY` + `BINANCE_TESTNET_KEY_FILE` (Ed25519 private key in PEM; Binance holds only the
  public half). Live: `BINANCE_API_KEY` + `BINANCE_KEY_FILE`.
- Panel: `PORT` (8080) on 127.0.0.1, served through nginx.
- Run: `python main.py`. Tests: `python -m pytest`.
