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
2. Service settings: set **Root Directory** to `karen_agent`. The Dockerfile, `railway.json` and `.dockerignore` all live there, and Railway builds with the Dockerfile.
3. Variables: set `KAREN_USER` and `KAREN_PASSWORD` (long random password). Do not add the Kraken API key.
4. Deploy, open the URL, log in.

`/` shows the page and `/api/report` returns JSON. Both need the login.

## Known limits
- **Simulation only.** The hosted service never places orders. It re-runs the strategy on the last 90 days of prices every hour, starting fresh each time. That is a rolling backtest, not a forward paper trade. A true paper trade needs a persistent record of simulated orders, which isn't built yet.
- **Data age.** If the newest candle is over 3 hours old, the run is marked as an error and no new results are shown.
- **Costs.** Fees are set to Kraken Pro Tier 1 taker (0.80% per side), the rate for market orders, from Kraken's fee page. Slippage (0.2%) is an assumption. Fees fall with 30-day volume, so check your tier in your account and update `config.py`.
- **Backtests are not proof.** Results cover 90 days of history and one strategy. Judge the paper run over several weeks before any live decision.
- **No live feed in the hosted service.** `feed.py` is a placeholder.
- **Login is basic auth.** Railway serves HTTPS, but use a strong password and rotate it if shared.
