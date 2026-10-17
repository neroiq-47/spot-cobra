"""Karen agent settings.

LIVE_TRADING is the on/off switch for real orders.
It is OFF by default. Live orders only happen when BOTH:
  1. LIVE_TRADING below is True, AND
  2. the environment variable KAREN_LIVE_TRADING is set to "ON".
Anything else keeps the agent in paper (simulated) mode. In that mode live.py
still sends each order to Kraken with validate=true, so nothing is placed.
"""

import os

# Master switch. Keep False until you have tested in paper mode.
LIVE_TRADING = False

# Second key to turn live trading on, set in the environment, not in this file.
LIVE_ENV_FLAG = os.environ.get("KAREN_LIVE_TRADING", "OFF").upper()

WATCHLIST = ["SOL", "LINK", "GRT", "POPCAT", "WIF"]
QUOTE_CURRENCY = "GBP"

# Hard limits, applied before any order is placed.
MAX_TRADE_GBP = 20.0        # largest single order
MAX_DAILY_LOSS_GBP = 30.0   # stop new buys for the day if losses reach this
MAX_OPEN_POSITIONS = 3      # most coins held at once across the watchlist
MAX_TOTAL_LOSS_GBP = 60.0   # live only: stop ALL new buys for good if realised losses reach this

# Cost assumptions. Source: Kraken fee schedule (Pro, Tier 1: 0.40% maker, 0.80% taker).
# Karen places market orders (takes liquidity), so the taker rate is used: a conservative cost.
# Fees drop with 30-day volume. Check your own tier in your Kraken Pro account.
FEE = 0.008       # 0.80% per side (Kraken Pro Tier 1, taker)
SLIPPAGE = 0.002  # 0.2% worse fill on each trade

# Live loop settings.
POLL_SECONDS = 60           # how often live.py checks prices for the stop loss
STATE_DIR = os.environ.get("KAREN_STATE_DIR", "state")   # where live positions are saved


def live_trading_enabled() -> bool:
    """Return True only if both switches say ON."""
    return LIVE_TRADING and LIVE_ENV_FLAG == "ON"
