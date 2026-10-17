# Karen spot agent (paper trading)

Mean-reversion spot strategy for SOL, LINK, GRT, POPCAT and WIF on Kraken GBP pairs.
Live trading is **off** and has no code path in the hosted service.

## Files
- `strategy.py`: the buy/sell rules (Bollinger Bands, RSI, 200-hour trend filter, stop loss).
- `agent.py`: paper broker, backtest, shared watchlist portfolio, live on/off check.
- `config.py`: watchlist, limits, fees, and the live switch (OFF by default).
- `fetch_kraken.py`: downloads public hourly candles from Kraken. No login, no API key.
- `feed.py`: price source interface. The live connector is not connected.
- `server.py`: hosted, login-protected paper-trading page for Railway.

## Local use
```
pip install -r requirements.txt
python fetch_kraken.py              # writes data/SOL.csv, LINK.csv, ...
python agent.py backtest-all data/  # watchlist backtest on one shared pot
```

## Deploy on Railway
1. New project, connect this repo.
2. Service settings: set **Root Directory** to `karen_agent` (the Dockerfile lives there).
3. Variables: set `KAREN_USER` and `KAREN_PASSWORD` (long random password). Do not add the Kraken API key.
4. Deploy, open the URL, log in.

`/` shows the page and `/api/report` returns JSON. Both need the login.

## Known limits
- **Paper only.** The service never places orders.
- **Data age.** If the newest candle is over 3 hours old, the run is marked as an error and no new results are shown.
- **Costs are assumptions.** Fees (0.6% per side) and slippage (0.2%) are in `config.py`. Replace them with your Kraken Pro figures.
- **Backtests are not proof.** Results cover 90 days of history and one strategy. Judge the paper run over several weeks before any live decision.
- **No live feed in the hosted service.** `feed.py` is a placeholder.
- **Login is basic auth.** Railway serves HTTPS, but use a strong password and rotate it if shared.
