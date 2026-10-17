"""Karen's spot strategy: mean reversion on dips, with a trend filter.

Buy when price drops below the lower Bollinger Band, RSI is oversold,
and the long-term trend (SMA 200) is still up. Sell when price returns to
the middle band, RSI recovers, the stop loss is hit, or the trade times out.
Long only, no leverage.
"""

from dataclasses import dataclass
import pandas as pd


@dataclass
class Params:
    bb_period: int = 20
    bb_std: float = 2.0
    rsi_period: int = 14
    rsi_buy: float = 30.0
    rsi_sell: float = 55.0
    trend_period: int = 200
    stop_loss: float = 0.05      # sell if down 5% from entry
    max_hold_bars: int = 48      # time stop


def add_indicators(df: pd.DataFrame, p: Params) -> pd.DataFrame:
    """df needs a 'close' column. Returns a copy with indicator columns."""
    out = df.copy()
    c = out["close"]
    out["mid"] = c.rolling(p.bb_period).mean()
    sd = c.rolling(p.bb_period).std()
    out["lower"] = out["mid"] - p.bb_std * sd
    delta = c.diff()
    gain = delta.clip(lower=0).rolling(p.rsi_period).mean()
    loss = (-delta.clip(upper=0)).rolling(p.rsi_period).mean()
    rs = gain / loss.replace(0, float("nan"))
    # No losses in the window means RSI is 100 (maximally strong), not undefined.
    out["rsi"] = (100 - 100 / (1 + rs)).where(~((loss == 0) & (gain > 0)), 100.0)
    out["trend"] = c.rolling(p.trend_period).mean()
    return out


def buy_signal(row, p: Params) -> bool:
    if pd.isna(row["lower"]) or pd.isna(row["rsi"]) or pd.isna(row["trend"]):
        return False
    return (
        row["close"] < row["lower"]
        and row["rsi"] < p.rsi_buy
        and row["close"] > row["trend"]
    )


def sell_signal(row, entry_price: float, bars_held: int, p: Params) -> str | None:
    """Return a reason string if the position should close, else None."""
    # Stop uses the candle's LOW, so an intrabar drop triggers it, as a real stop would.
    low = row["low"] if "low" in row and not pd.isna(row["low"]) else row["close"]
    if low <= entry_price * (1 - p.stop_loss):
        return "stop_loss"
    if bars_held >= p.max_hold_bars:
        return "time_stop"
    if not pd.isna(row["mid"]) and row["close"] >= row["mid"]:
        return "mean_target"
    if not pd.isna(row["rsi"]) and row["rsi"] >= p.rsi_sell:
        return "rsi_exit"
    return None
