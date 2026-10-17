"""Offline tests for the live path. No network, no real keys.

Run: python -m unittest test_live -v
"""

import os
import tempfile
import unittest
from unittest import mock

import config
import live
from kraken_client import KrakenError, sign

HOUR = 3600
NOW = 1_760_000_000 - (1_760_000_000 % HOUR) + 120   # two minutes after a candle closed


def candles(closes, end_open):
    """Kraken OHLC rows ending with a candle that opened at end_open."""
    start = end_open - (len(closes) - 1) * HOUR
    return [[start + i * HOUR, str(c), str(c * 1.001), str(c * 0.999), str(c), "0", "1", 1]
            for i, c in enumerate(closes)]


def dip_series():
    """Long uptrend then a sharp hourly dip: triggers the buy rule on the last candle."""
    up = [100 + i * 0.05 for i in range(260)]
    return up + [up[-1] * 0.97]


class FakeClient:
    def __init__(self, gbp=100.0, ask=100.0, bid=99.9):
        self.gbp, self.ask, self.bid = gbp, ask, bid
        self.orders = []
        self.order_error = None
        self.coin = 0.0

    def has_keys(self):
        return True

    def asset_pairs(self):
        return {"SOLGBP": {"wsname": "SOL/GBP", "base": "SOL", "quote": "ZGBP",
                           "lot_decimals": 8, "ordermin": "0.02", "costmin": "0.5",
                           "status": "online"}}

    def ticker(self, pair):
        return {"ask": self.ask, "bid": self.bid, "last": self.ask}

    def balance(self):
        return {"ZGBP": self.gbp, "SOL": self.coin}

    def add_market_order(self, pair, side, volume, validate, userref=None):
        if self.order_error:
            raise self.order_error
        self.orders.append((side, volume, validate))
        if validate:
            return {"descr": {"order": f"{side} {volume} {pair} @ market"}}
        return {"txid": [f"TX{len(self.orders)}"]}

    def query_order(self, txid):
        side, volume, _ = self.orders[int(txid[2:]) - 1]
        v = float(volume)
        px = self.ask if side == "buy" else self.bid
        if side == "buy":
            self.coin += v
        else:
            self.coin -= v
        return {"status": "closed", "vol_exec": str(v), "cost": str(v * px),
                "fee": str(v * px * config.FEE), "price": str(px)}


class SignTest(unittest.TestCase):
    def test_matches_kraken_docs_example(self):
        secret = "kQH5HW/8p1uGOVjbgWA7FunAmGO8lsSUXNsu3eow76sz84Q18fWxnyRzBHCd3pd5nE9qa99HAZtuZuj6F1huXg=="
        data = {"nonce": 1616492376594, "ordertype": "limit", "pair": "XBTUSD",
                "price": 37500, "type": "buy", "volume": 1.25}
        self.assertEqual(sign("/0/private/AddOrder", data, secret),
                         "4/dpxb3iT4tp/ZCVEwSnEsLxx0bqyhLpdfOpc6fn7OR8+UClSV5n9E6aSS8MPtnRfp32bAb0nmbRn6H8ndwLUQ==")


class SwitchTest(unittest.TestCase):
    def test_live_off_by_default(self):
        self.assertFalse(config.LIVE_TRADING)
        self.assertFalse(config.live_trading_enabled())

    def test_needs_both_switches(self):
        with mock.patch.object(config, "LIVE_TRADING", True), mock.patch.object(config, "LIVE_ENV_FLAG", "OFF"):
            self.assertFalse(config.live_trading_enabled())
        with mock.patch.object(config, "LIVE_TRADING", True), mock.patch.object(config, "LIVE_ENV_FLAG", "ON"):
            self.assertTrue(config.live_trading_enabled())


class TraderTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.now = NOW
        self.client = FakeClient()
        self.series = dip_series()
        self.feed_end = None               # open time of the newest candle; None = latest closed
        p = mock.patch("live.fetch_kraken.fetch_page",
                       side_effect=lambda pair, since: (candles(self.series, self.feed_end or self.now - 120 - HOUR), 0))
        p.start()
        self.addCleanup(p.stop)

    def trader(self, live_mode):
        t = live.LiveTrader(self.client, live=live_mode, state_dir=self.dir,
                            clock=lambda: self.now, sleep=lambda s: None)
        t.load_pairs()
        return t

    def test_validate_mode_never_places_real_orders(self):
        t = self.trader(False)
        t.step()
        self.assertIn("SOL", t.state["positions"])
        self.assertTrue(self.client.orders)
        self.assertTrue(all(validate for _, _, validate in self.client.orders))

    def test_live_buy_respects_max_trade_and_records_cost(self):
        t = self.trader(True)
        t.step()
        pos = t.state["positions"]["SOL"]
        self.assertLessEqual(pos["cost"], config.MAX_TRADE_GBP + 1e-9)
        self.assertGreater(pos["cost"], config.MAX_TRADE_GBP * 0.97)
        self.assertEqual(self.client.orders[0][2], False)
        # Same candle again: no second buy.
        t.step()
        self.assertEqual(len(self.client.orders), 1)

    def test_state_survives_restart(self):
        self.trader(True).step()
        t2 = self.trader(True)
        self.assertIn("SOL", t2.state["positions"])

    def test_stop_loss_on_live_bid(self):
        t = self.trader(True)
        t.step()
        entry = t.state["positions"]["SOL"]["entry"]
        self.now += 60
        self.client.bid = entry * 0.94
        t.step()
        self.assertNotIn("SOL", t.state["positions"])
        self.assertTrue(t.state["trades"][-1]["action"].startswith("SELL:stop_loss"))
        self.assertLess(t.state["total_pnl"], 0)

    def test_strategy_exit_on_next_candle(self):
        t = self.trader(True)
        t.step()
        self.series = self.series + [self.series[-2] * 1.01]   # price bounces back above the middle band
        self.now += HOUR
        t.step()
        self.assertNotIn("SOL", t.state["positions"])
        self.assertTrue(t.state["trades"][-1]["action"].startswith("SELL:"))
        self.assertNotIn("stop_loss", t.state["trades"][-1]["action"])

    def test_only_sells_what_karen_bought(self):
        t = self.trader(True)
        t.step()
        bought = t.state["positions"]["SOL"]["volume"]
        self.client.coin += 50.0          # the user's own SOL
        self.now += 60
        self.client.bid = self.client.ask * 0.5
        t.step()
        sold = float(self.client.orders[-1][1])
        self.assertAlmostEqual(sold, bought, places=6)

    def test_daily_and_total_loss_block_buys(self):
        t = self.trader(True)
        t.new_day()
        t.state["day_pnl"] = -config.MAX_DAILY_LOSS_GBP
        self.assertFalse(t.can_buy())
        t.state["day_pnl"] = 0
        t.state["total_pnl"] = -config.MAX_TOTAL_LOSS_GBP
        self.assertFalse(t.can_buy())

    def test_no_buy_without_gbp(self):
        self.client.gbp = 0.0
        t = self.trader(True)
        t.step()
        self.assertEqual(self.client.orders, [])

    def test_unclear_order_outcome_halts(self):
        self.client.order_error = TimeoutError("read timed out")
        t = self.trader(True)
        t.step()
        self.assertIsNotNone(t.state["halted"])
        self.client.order_error = None
        self.now += HOUR
        t.step()                           # halted: does nothing
        self.assertEqual(self.client.orders, [])

    def test_service_error_halts_but_order_reject_does_not(self):
        self.client.order_error = KrakenError("EOrder:Insufficient funds")
        t = self.trader(True)
        t.step()
        self.assertIsNone(t.state["halted"])
        self.client.order_error = KrakenError("EService:Unavailable")
        t2 = live.LiveTrader(self.client, live=True, state_dir=tempfile.mkdtemp(),
                             clock=lambda: self.now, sleep=lambda s: None)
        t2.load_pairs()
        t2.step()
        self.assertIsNotNone(t2.state["halted"])

    def test_reconcile_drops_position_sold_by_hand(self):
        t = self.trader(True)
        t.step()
        t.reconcile({"ZGBP": 100.0, "SOL": 0.0})
        self.assertNotIn("SOL", t.state["positions"])

    def test_stale_candles_skipped(self):
        self.feed_end = self.now - 120 - HOUR
        self.now += 5 * HOUR               # feed still ends 5 hours ago
        t = self.trader(True)
        t.step()
        self.assertEqual(self.client.orders, [])


if __name__ == "__main__":
    unittest.main()
