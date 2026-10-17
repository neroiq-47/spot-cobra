"""Price data sources for Karen.

Karen reads hourly candles (timestamp, open, high, low, close, volume) for each
watchlist coin. Any source that returns that table can be plugged in here.

- CsvFeed: reads files you export yourself. Works now.
- LiveFeed: the exchange or data connector. Not connected yet, so it raises
  until a connector is set up (Crypto.com market data or CoinDesk).
"""

from abc import ABC, abstractmethod

import pandas as pd

import config


class Feed(ABC):
    @abstractmethod
    def candles(self, symbol: str) -> pd.DataFrame:
        """Return hourly candles for symbol, oldest first."""


class CsvFeed(Feed):
    def __init__(self, folder: str):
        self.folder = folder

    def candles(self, symbol: str) -> pd.DataFrame:
        path = f"{self.folder}/{symbol}.csv"
        df = pd.read_csv(path)
        df.columns = [c.strip().lower() for c in df.columns]
        return df.sort_values("timestamp").reset_index(drop=True)


class LiveFeed(Feed):
    """Placeholder for a real market-data connector. Fails loudly until wired."""

    def candles(self, symbol: str) -> pd.DataFrame:
        raise NotImplementedError(
            f"No live price connector is set up for {symbol}/{config.QUOTE_CURRENCY}. "
            "Connect Crypto.com market data or CoinDesk, then this will be wired in."
        )
