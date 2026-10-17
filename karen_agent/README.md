# Karen spot agent

Mean-reversion spot strategy for SOL, LINK, GRT, POPCAT and WIF on Kraken GBP pairs.
Live trading is **off** by default. The hosted page (`server.py`) is simulation-only;
real orders can only come from `live.py`, and only when both switches are ON.

## Files
- `strategy.py`: the buy/sell rules (Bollinger Bands, RSI, 200-hour trend filter, stop loss).
- `agent.py`: paper broker, backtest, shared watchlist portfolio, live on/off check.
- `config.py`: watchlist, limits, fees, and the live switch (OFF by default).
- `fetch_kraken.py`: downloads public hourly candles from Kraken. No login, no API key.
- `feed.py`: price source interface. The live connector is not connected.
- `server.py`: hosted, login-protected dashboard for Railway. Shows the rolling simulation and, read-only, the live trader's saved state from `KAREN_STATE_DIR`. It never places orders.
- `kraken_client.py`: signed Kraken private API (balance, market orders, order status).
- `live.py`: the live trader. Validate mode (default) or real orders.
- `test_live.py`: offline tests for the live path (`python -m unittest test_live`).

## Local use
```
pip install -r requirements.txt
python fetch_kraken.py              # writes data/SOL.csv, LINK.csv, ...
python agent.py backtest-all data/  # watchlist backtest on one shared pot
```

## Deploy on Railway
1. New project, connect this repo.
2. Service settings: set **Root Directory** to `karen_agent`. The Dockerfile, `railway.json` and `.dockerignore` all live there, and Railway builds with the Dockerfile.
3. Variables: set `KAREN_USER` and `KAREN_PASSWORD` (long random password).
4. Deploy, open the URL, log in.

`/` shows the page and `/api/report` returns JSON. Both need the login.

To run the trader in the same service (one service, one volume, page and trader together):
5. Add a **volume** mounted at `/data`, and set `KAREN_STATE_DIR=/data`. Without it, saved positions are lost on every redeploy. Railway volumes are owned by root: if the state file can't be written, set `RAILWAY_RUN_UID=0`.
6. Set `KRAKEN_API_KEY`, `KRAKEN_API_SECRET` and `KAREN_RUN_TRADER=ON`. Redeploy.
7. The page's Live trader section shows `trader running` in VALIDATE mode. It stays in validate mode (no orders placed) until both live switches are ON.

## Live trading (Kraken)
Two modes, picked by the switches:
- **Validate (default).** Each order goes to Kraken with `validate=true`: Kraken checks it and places nothing. Fills are simulated at the live bid/ask with fees, and saved to `state/positions_validate.json`. This is the forward paper trade.
- **Live.** Real market orders. Needs `LIVE_TRADING = True` in `config.py` (a code change you commit) **and** `KAREN_LIVE_TRADING=ON` in the environment.

Steps:
1. On Kraken, create an API key with only: *Funds: Query*, *Orders and trades: Query open orders & trades*, *Query closed orders & trades*, *Create & modify orders*. **Never Withdraw.** Restrict it to your server's IP if you can.
2. Set `KRAKEN_API_KEY` and `KRAKEN_API_SECRET` as environment variables (Railway Variables, never in the repo).
3. `python live.py check`: checks keys, permissions, pairs, GBP balance and Kraken's minimum order size, and validates one buy per coin. Places nothing. Must say `READY`.
4. `python live.py`: runs in validate mode. Leave it for several weeks and compare with the backtest.
5. Only then, to go live: set `LIVE_TRADING = True` in `config.py`, set `KAREN_LIVE_TRADING=ON`, run `check` again (it should say `LIVE`), then start `live.py`. To stop real orders, set `KAREN_LIVE_TRADING=OFF` and restart.

On Railway, run it inside the dashboard service with `KAREN_RUN_TRADER=ON` (steps 5–7 above), so the page shows it. Run only **one** trader per API key: don't also start `python live.py` elsewhere with the same key. If the trader halts, the page says why; fix it, clear `halted` in the state file, and restart the service.

What `live.py` does each minute: checks open positions against the live bid for the 5% stop loss. After each hourly candle closes, it applies the same buy/sell rules as the backtest.

Safety limits (in `config.py`): £20 per buy, 3 coins at once, no new buys after £30 realised loss in a UTC day or £60 in total. It only sells the amount it bought, never your other holdings of the same coin. On startup in live mode it compares saved positions with your Kraken balances. If an order's outcome is unclear (timeout, Kraken service error, partial fill) it **halts**: check Kraken, fix `positions_live.json` (clear `halted`), then restart.

## Known limits
- **Simulation only.** The hosted service never places orders. It re-runs the strategy on the last 90 days of prices every hour, starting fresh each time. That is a rolling backtest, not a forward paper trade. A true paper trade needs a persistent record of simulated orders, which isn't built yet.
- **Data age.** If the newest candle is over 3 hours old, the run is marked as an error and no new results are shown.
- **Costs.** Fees are set to Kraken Pro Tier 1 taker (0.80% per side), the rate for market orders, from Kraken's fee page. Slippage (0.2%) is an assumption. Fees fall with 30-day volume, so check your tier in your account and update `config.py`.
- **Backtests are not proof.** Results cover 90 days of history and one strategy. Judge the paper run over several weeks before any live decision.
- **No live feed in the hosted service.** `feed.py` is a placeholder; `live.py` reads Kraken directly.
- **Live path is tested offline only.** `test_live.py` uses a fake Kraken and checks the request signing against Kraken's published example. Run `python live.py check` with your key before trusting it.
- **Market orders, hourly rules.** Stops are checked once a minute, not placed on the exchange, so a fast drop can fill well below 5%. If `live.py` is down, nothing protects open positions.
- **Login is basic auth.** Railway serves HTTPS, but use a strong password and rotate it if shared.
