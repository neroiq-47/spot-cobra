"""Karen: spot trading agent (paper mode by default).

Usage:
  python agent.py backtest data/SOL_GBP_1h.csv        # test on history
  python agent.py backtest-all data/                  # every CSV in a folder (watchlist)
  python agent.py paper    data/SOL_GBP_1h.csv        # simulate on candles
  python agent.py live     data/SOL_GBP_1h.csv        # only if switches are ON

CSV columns: timestamp, open, high, low, close, volume (oldest first).
"""

import sys
import glob
import os
from dataclasses import dataclass, field

import pandas as pd

import config
from strategy import Params, add_indicators, buy_signal, sell_signal

FEE = config.FEE
SLIPPAGE = config.SLIPPAGE
REQUIRED_COLS = {"timestamp", "close"}


@dataclass
class PaperBroker:
    cash: float = 1000.0
    coin: float = 0.0
    entry_price: float = 0.0
    cost_basis: float = 0.0   # GBP actually spent on the open position, fee included
    bars_held: int = 0
    day: str = ""             # date the daily P&L is counting for
    day_pnl: float = 0.0
    trades: list = field(default_factory=list)

    def in_position(self) -> bool:
        return self.coin > 0

    def new_day(self, ts) -> None:
        """Reset the daily loss counter when the calendar day changes."""
        day = str(ts)[:10]
        if day != self.day:
            self.day = day
            self.day_pnl = 0.0

    def can_buy(self) -> bool:
        return self.day_pnl > -config.MAX_DAILY_LOSS_GBP

    def buy(self, price: float, ts) -> None:
        spend = min(config.MAX_TRADE_GBP, self.cash)
        if spend <= 0:
            return
        fill = price * (1 + SLIPPAGE)
        self.coin = spend * (1 - FEE) / fill
        self.cash -= spend
        self.cost_basis = spend
        self.entry_price = fill
        self.bars_held = 0
        self.trades.append((ts, "BUY", fill, spend))

    def sell(self, price: float, ts, reason: str) -> None:
        fill = price * (1 - SLIPPAGE)
        proceeds = self.coin * fill * (1 - FEE)
        pnl = proceeds - self.cost_basis   # compares against what was actually paid
        self.cash += proceeds
        self.day_pnl += pnl
        self.trades.append((ts, f"SELL:{reason}", fill, pnl))
        self.coin = 0.0
        self.entry_price = 0.0
        self.cost_basis = 0.0
        self.bars_held = 0

    def equity(self, price: float) -> float:
        return self.cash + self.coin * price


def load_candles(path: str) -> pd.DataFrame:
    df = pd.read_csv(path)
    df.columns = [c.strip().lower() for c in df.columns]
    missing = REQUIRED_COLS - set(df.columns)
    if missing:
        raise SystemExit(f"{path} is missing columns: {sorted(missing)}")
    return df.sort_values("timestamp").reset_index(drop=True)


def run(df: pd.DataFrame, params: Params | None = None) -> PaperBroker:
    """Walk through candles and apply the rules. Returns the broker record."""
    p = params or Params()
    data = add_indicators(df, p)
    broker = PaperBroker()
    for _, row in data.iterrows():
        price, ts = row["close"], row["timestamp"]
        broker.new_day(ts)
        if broker.in_position():
            broker.bars_held += 1
            reason = sell_signal(row, broker.entry_price, broker.bars_held, p)
            if reason:
                broker.sell(price, ts, reason)
        # Daily loss limit only blocks NEW buys for that day; exits always run.
        elif broker.can_buy() and buy_signal(row, p):
            broker.buy(price, ts)
    if broker.in_position():
        broker.sell(data.iloc[-1]["close"], data.iloc[-1]["timestamp"], "end_of_data")
    return broker


def run_portfolio(frames: dict[str, pd.DataFrame], params: Params | None = None,
                  start_cash: float = 1000.0) -> PaperBroker:
    """Run the whole watchlist on one shared cash pot.

    Each coin can hold one position at a time. At most MAX_OPEN_POSITIONS
    coins are held at once. Walks forward through timestamps shared by all coins.
    """
    p = params or Params()
    data = {sym: add_indicators(df.set_index("timestamp"), p) for sym, df in frames.items()}
    times = sorted(set().union(*[set(d.index) for d in data.values()]))
    broker = PaperBroker(cash=start_cash)
    positions: dict[str, dict] = {}   # symbol -> {entry, cost, held}

    for ts in times:
        broker.new_day(ts)
        for sym, d in data.items():
            if ts not in d.index:
                continue
            row = d.loc[ts]
            if sym in positions:
                pos = positions[sym]
                pos["held"] += 1
                reason = sell_signal(row, pos["entry"], pos["held"], p)
                if reason:
                    _sell(broker, sym, row["close"], ts, reason, pos)
                    del positions[sym]
        for sym, d in data.items():
            if ts not in d.index or sym in positions:
                continue
            if len(positions) >= config.MAX_OPEN_POSITIONS or not broker.can_buy():
                break
            row = d.loc[ts]
            if buy_signal(row, p):
                spend = min(config.MAX_TRADE_GBP, broker.cash)
                if spend <= 0:
                    break
                fill = row["close"] * (1 + SLIPPAGE)
                coin_amt = spend * (1 - FEE) / fill
                broker.cash -= spend
                positions[sym] = {"entry": fill, "cost": spend, "held": 0, "coin": coin_amt}
                broker.trades.append((ts, f"BUY:{sym}", fill, spend))

    for sym, pos in list(positions.items()):
        last = data[sym].iloc[-1]
        _sell(broker, sym, last["close"], last.name, "end_of_data", pos)
    return broker


def _sell(broker, sym, price, ts, reason, pos) -> None:
    fill = price * (1 - SLIPPAGE)
    proceeds = pos["coin"] * fill * (1 - FEE)
    pnl = proceeds - pos["cost"]
    broker.cash += proceeds
    broker.day_pnl += pnl
    broker.trades.append((ts, f"SELL:{sym}:{reason}", fill, pnl))


def report(name: str, broker: PaperBroker, start_cash: float = 1000.0) -> None:
    sells = [t for t in broker.trades if t[1].startswith("SELL")]
    wins = [t for t in sells if t[3] > 0]
    total = (broker.cash - start_cash) / start_cash * 100
    print(f"\n== {name} ==")
    print(f"Trades closed : {len(sells)}")
    print(f"Win rate      : {len(wins) / len(sells) * 100:.0f}%" if sells else "Win rate      : n/a")
    print(f"Return        : {total:+.2f}% on £{start_cash:.0f}")
    print(f"Ending cash   : £{broker.cash:.2f}")


def main() -> None:
    if len(sys.argv) < 3:
        print(__doc__)
        return
    mode, path = sys.argv[1], sys.argv[2]

    if mode == "backtest-all":
        # Run the watchlist together on one shared pot, one CSV per coin.
        files = sorted(glob.glob(os.path.join(path, "*.csv")))
        if not files:
            raise SystemExit(f"No CSV files found in {path}")
        frames = {os.path.splitext(os.path.basename(f))[0]: load_candles(f) for f in files}
        broker = run_portfolio(frames)
        report(f"Watchlist portfolio: {', '.join(frames)}", broker)
        return

    if mode in ("backtest", "paper"):
        candles = load_candles(path)
        broker = run(candles)
        label = "Backtest" if mode == "backtest" else "Paper trading"
        report(f"{label}: {path}", broker)
        for ts, action, price, amt in broker.trades[-10:]:
            print(f"  {ts}  {action:<22} £{price:.4f}  {amt:+.2f}")
        return

    if mode == "paper-live":
        # Simulated trading on live prices. Needs a LiveFeed connector.
        from feed import LiveFeed
        feed = LiveFeed()
        frames = {sym: feed.candles(sym) for sym in config.WATCHLIST}
        report("Paper trading (live prices)", run_portfolio(frames))
        return

    if mode == "live":
        if not config.live_trading_enabled():
            print("Live trading is OFF. Running in paper mode instead.")
            broker = run(load_candles(path))
            report("Paper trading (live is off)", broker)
            return
        raise SystemExit("Live trading runs from live.py (Kraken). Use: python live.py check, then python live.py")

    print(__doc__)


if __name__ == "__main__":
    main()
