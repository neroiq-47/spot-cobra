"""Karen live trader for Kraken spot (GBP pairs).

Two modes, picked by the switches in config.py:
  VALIDATE (default)  Every order is sent to Kraken with validate=true, so Kraken
                      checks it but does NOT place it. Fills are simulated at the
                      current bid/ask with fees. This is a forward paper trade
                      on live prices, saved to state/positions_validate.json.
  LIVE                Real market orders. Only when config.LIVE_TRADING is True
                      AND the environment has KAREN_LIVE_TRADING=ON.

Usage:
  python live.py check    # one-off readiness check: keys, permissions, pairs, balances
  python live.py          # run the loop (Ctrl+C to stop; state is saved after every change)

Needs KRAKEN_API_KEY and KRAKEN_API_SECRET in the environment (see kraken_client.py).

Safety rules, all enforced before an order is sent:
  - At most MAX_TRADE_GBP per buy, MAX_OPEN_POSITIONS coins at once.
  - No new buys after MAX_DAILY_LOSS_GBP realised loss in a UTC day, or after
    MAX_TOTAL_LOSS_GBP realised loss in total (exits always run).
  - Only sells what Karen bought, never your other holdings of the same coin.
  - Halts all trading if an order's outcome is unclear. A person must check
    Kraken, fix the state file, and restart.
"""

import json
import math
import os
import sys
import time
import traceback
from datetime import datetime, timezone

import pandas as pd

import config
import fetch_kraken
from kraken_client import KrakenClient, KrakenError
from strategy import Params, add_indicators, buy_signal, sell_signal

BAR_SECONDS = fetch_kraken.INTERVAL_MIN * 60
HISTORY_SECONDS = 14 * 86400    # enough hourly candles for the 200-hour trend filter
FILL_WAIT_SECONDS = 60
# Kraken errors that mean the order was refused outright, so nothing was placed.
REJECTED_PREFIXES = ("EOrder:", "EGeneral:Invalid", "EGeneral:Permission",
                     "EAPI:Invalid key", "EAPI:Invalid signature", "EAPI:Invalid nonce")


def log(msg: str) -> None:
    print(f"{datetime.now(timezone.utc).isoformat(timespec='seconds')} {msg}", flush=True)


def floor_to(x: float, decimals: int) -> float:
    q = 10 ** decimals
    return math.floor(x * q + 1e-9) / q


def fmt_volume(x: float, decimals: int) -> str:
    return f"{x:.{decimals}f}"


def closed_candles(rows: list, now: float) -> pd.DataFrame:
    """Kraken OHLC rows -> DataFrame of fully closed candles, oldest first."""
    seen, clean = set(), []
    for r in sorted(rows, key=lambda r: r[0]):
        if r[0] in seen or int(r[0]) + BAR_SECONDS > now:
            continue
        seen.add(r[0])
        clean.append({"time": int(r[0]), "open": float(r[1]), "high": float(r[2]),
                      "low": float(r[3]), "close": float(r[4]), "volume": float(r[6])})
    return pd.DataFrame(clean)


class LiveTrader:
    def __init__(self, client: KrakenClient, live: bool, state_dir: str = config.STATE_DIR,
                 params: Params | None = None, clock=time.time, sleep=time.sleep):
        self.client = client
        self.live = live
        self.p = params or Params()
        self.clock = clock
        self.sleep = sleep
        os.makedirs(state_dir, exist_ok=True)
        self.state_path = state_path(live, state_dir)
        self.state = self._load()
        self.pairs: dict[str, dict] = {}   # coin -> AssetPairs info plus 'pair' key

    # --- State ---
    def _load(self) -> dict:
        if os.path.exists(self.state_path):
            with open(self.state_path) as f:
                return json.load(f)
        return {"positions": {}, "last_bar": {}, "day": "", "day_pnl": 0.0,
                "total_pnl": 0.0, "halted": None, "trades": []}

    def save(self) -> None:
        tmp = self.state_path + ".tmp"
        with open(tmp, "w") as f:
            json.dump(self.state, f, indent=2)
        os.replace(tmp, self.state_path)   # atomic, so a crash never leaves half a file

    def halt(self, reason: str) -> None:
        self.state["halted"] = reason
        self.save()
        log(f"HALTED: {reason}")

    # --- Setup ---
    def load_pairs(self) -> None:
        info = self.client.asset_pairs()
        by_ws = {v.get("wsname", ""): (k, v) for k, v in info.items()}
        for coin in config.WATCHLIST:
            hit = by_ws.get(f"{coin}/{config.QUOTE_CURRENCY}")
            if not hit:
                log(f"{coin}: no {coin}/{config.QUOTE_CURRENCY} pair on Kraken, skipped")
                continue
            key, v = hit
            if v.get("status", "online") != "online":
                log(f"{coin}: pair status is {v.get('status')}, skipped")
                continue
            self.pairs[coin] = {**v, "pair": key}

    def reconcile(self, balances: dict[str, float]) -> None:
        """Make saved positions agree with what Kraken actually holds (live mode only)."""
        for coin, pos in list(self.state["positions"].items()):
            info = self.pairs.get(coin)
            if not info:
                log(f"{coin}: saved position kept, but its pair is not tradable right now")
                continue
            held = balances.get(info["base"], 0.0)
            if held < pos["volume"] * 0.01:
                log(f"{coin}: saved position {pos['volume']} but Kraken holds {held}. "
                    "Assuming it was sold by hand; dropping it.")
                del self.state["positions"][coin]
            elif held < pos["volume"]:
                log(f"{coin}: saved {pos['volume']}, Kraken holds {held}. Using {held}.")
                pos["volume"] = held
        self.save()

    # --- Limits ---
    def new_day(self) -> None:
        day = datetime.fromtimestamp(self.clock(), timezone.utc).strftime("%Y-%m-%d")
        if day != self.state["day"]:
            self.state["day"] = day
            self.state["day_pnl"] = 0.0

    def can_buy(self) -> bool:
        return (self.state["halted"] is None
                and len(self.state["positions"]) < config.MAX_OPEN_POSITIONS
                and self.state["day_pnl"] > -config.MAX_DAILY_LOSS_GBP
                and self.state["total_pnl"] > -config.MAX_TOTAL_LOSS_GBP)

    # --- Orders ---
    def _wait_fill(self, txid: str) -> dict:
        deadline = self.clock() + FILL_WAIT_SECONDS
        while True:
            o = self.client.query_order(txid)
            if o["status"] in ("closed", "canceled", "expired"):
                return o
            if self.clock() > deadline:
                return o
            self.sleep(2)

    def _order(self, coin: str, side: str, volume: float, quote: dict) -> dict | None:
        """Send one market order. Returns {'volume', 'cost', 'fee', 'price'} or None."""
        info = self.pairs[coin]
        vol_txt = fmt_volume(volume, int(info["lot_decimals"]))
        try:
            res = self.client.add_market_order(info["pair"], side, vol_txt, validate=not self.live)
        except KrakenError as e:
            if self.live:
                if not str(e).startswith(REJECTED_PREFIXES):
                    # e.g. EService:Unavailable or a timeout: the order may or may not exist.
                    self.halt(f"{coin}: {side} {vol_txt} outcome unknown ({e}). Check Kraken.")
                    return None
                log(f"{coin}: {side} {vol_txt} rejected by Kraken: {e}")
                return None
            # In validate mode an empty account gives "Insufficient funds"; the
            # order itself was well formed, so keep the paper trade going.
            log(f"{coin}: validate {side} {vol_txt}: Kraken says {e}")
            if "Insufficient funds" not in str(e):
                return None
            res = {}
        except Exception as e:
            if self.live:
                self.halt(f"{coin}: {side} {vol_txt} outcome unknown ({type(e).__name__}: {e}). Check Kraken.")
                return None
            raise

        if not self.live:
            px = quote["ask"] * (1 + config.SLIPPAGE) if side == "buy" else quote["bid"] * (1 - config.SLIPPAGE)
            cost = volume * px
            log(f"{coin}: VALIDATED {side} {vol_txt} @ ~{px:.6f} (not placed) {res.get('descr', '')}")
            return {"volume": volume, "cost": cost, "fee": cost * config.FEE, "price": px}

        txids = res.get("txid") or []
        if not txids:
            self.halt(f"{coin}: {side} sent but Kraken returned no txid ({res}). Check Kraken.")
            return None
        txid = txids[0]
        log(f"{coin}: LIVE {side} {vol_txt} sent, txid {txid}")
        try:
            o = self._wait_fill(txid)
        except Exception as e:
            self.halt(f"{coin}: {side} {txid} sent but its status could not be read ({e}). Check Kraken.")
            return None
        filled = float(o.get("vol_exec", 0))
        if o["status"] != "closed" or filled <= 0:
            self.halt(f"{coin}: {side} {txid} ended as {o['status']} with {filled} filled. Check Kraken.")
            if filled <= 0:
                return None
        cost, fee = float(o["cost"]), float(o["fee"])
        return {"volume": filled, "cost": cost, "fee": fee, "price": cost / filled, "txid": txid}

    def buy(self, coin: str, quote: dict, cash: float | None) -> None:
        info = self.pairs[coin]
        spend = config.MAX_TRADE_GBP if cash is None else min(config.MAX_TRADE_GBP, cash * 0.99)
        price = quote["ask"] * (1 + config.SLIPPAGE)
        volume = floor_to(spend / (price * (1 + config.FEE)), int(info["lot_decimals"]))
        if volume < float(info.get("ordermin", 0)) or volume * price < float(info.get("costmin", 0) or 0):
            log(f"{coin}: buy skipped, £{spend:.2f} is below Kraken's minimum order")
            return
        fill = self._order(coin, "buy", volume, quote)
        if not fill:
            return
        paid = fill["cost"] + fill["fee"]   # Kraken charges the fee in GBP on top
        self.state["positions"][coin] = {"volume": fill["volume"], "entry": fill["price"],
                                         "cost": paid, "held": 0,
                                         "opened": datetime.now(timezone.utc).isoformat(timespec="seconds")}
        self._record(coin, "BUY", fill["price"], -paid, fill.get("txid"))

    def sell(self, coin: str, quote: dict, reason: str, balances: dict | None) -> None:
        info = self.pairs[coin]
        pos = self.state["positions"][coin]
        volume = pos["volume"]
        if balances is not None:
            volume = min(volume, balances.get(info["base"], 0.0))
        volume = floor_to(volume, int(info["lot_decimals"]))
        if volume <= 0:
            log(f"{coin}: nothing left to sell, dropping position")
            del self.state["positions"][coin]
            self.save()
            return
        fill = self._order(coin, "sell", volume, quote)
        if not fill:
            return
        proceeds = fill["cost"] - fill["fee"]
        sold_share = min(fill["volume"] / pos["volume"], 1.0)
        pnl = proceeds - pos["cost"] * sold_share
        self.state["day_pnl"] += pnl
        self.state["total_pnl"] += pnl
        del self.state["positions"][coin]
        self._record(coin, f"SELL:{reason}", fill["price"], pnl, fill.get("txid"))

    def _record(self, coin, action, price, amount, txid) -> None:
        self.state["trades"].append({"time": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                                     "coin": coin, "action": action, "price": price,
                                     "amount_gbp": round(amount, 4), "txid": txid})
        self.state["trades"] = self.state["trades"][-500:]
        self.save()
        log(f"{coin}: {action} @ {price:.6f}, £{amount:+.2f} | day £{self.state['day_pnl']:+.2f}, "
            f"total £{self.state['total_pnl']:+.2f}")

    # --- One pass of the loop ---
    def balances(self) -> dict | None:
        return self.client.balance() if self.live else None

    def gbp_cash(self, balances: dict | None) -> float | None:
        if balances is None:
            return None
        return balances.get("ZGBP", balances.get("GBP", 0.0))

    def step(self) -> None:
        """Stop-loss check on live prices, then strategy rules on any newly closed candle."""
        self.new_day()
        if self.state["halted"]:
            return
        now = self.clock()
        quotes = {coin: self.client.ticker(info["pair"]) for coin, info in self.pairs.items()}
        balances = self.balances()

        # 1. Stop loss between candles, on the live bid.
        for coin in list(self.state["positions"]):
            pos = self.state["positions"][coin]
            if coin in quotes and quotes[coin]["bid"] <= pos["entry"] * (1 - self.p.stop_loss):
                self.sell(coin, quotes[coin], "stop_loss", balances)

        # 2. Strategy rules, once per newly closed hourly candle.
        rows = {}
        for coin, info in self.pairs.items():
            last = self.state["last_bar"].get(coin, 0)
            if now < last + 2 * BAR_SECONDS:   # last_bar is the open time of the last candle used
                continue
            page, _ = fetch_kraken.fetch_page(info["pair"], int(now - HISTORY_SECONDS))
            df = closed_candles(page, now)
            if df.empty or int(df["time"].iloc[-1]) <= last:
                continue
            if now - int(df["time"].iloc[-1]) > 3 * BAR_SECONDS:
                log(f"{coin}: newest candle is stale, skipped")
                continue
            data = add_indicators(df, self.p)
            rows[coin] = data.iloc[-1]
            self.state["last_bar"][coin] = int(df["time"].iloc[-1])

        for coin, row in rows.items():
            if coin in self.state["positions"]:
                pos = self.state["positions"][coin]
                pos["held"] += 1
                reason = sell_signal(row, pos["entry"], pos["held"], self.p)
                if reason:
                    if self.live:
                        balances = self.client.balance()
                    self.sell(coin, quotes[coin], reason, balances)
        for coin, row in rows.items():
            if coin in self.state["positions"] or not self.can_buy():
                continue
            if buy_signal(row, self.p):
                if self.live:
                    balances = self.client.balance()
                cash = self.gbp_cash(balances)
                if cash is not None and cash < 1:
                    log(f"{coin}: buy signal, but no GBP in the account")
                    continue
                self.buy(coin, quotes[coin], cash)
        self.save()

    def run_forever(self) -> None:
        log(f"Karen live loop started in {'LIVE' if self.live else 'VALIDATE'} mode, "
            f"coins: {', '.join(self.pairs)}")
        while True:
            try:
                self.step()
            except Exception as e:
                # Network blips are normal; keep going. Order problems halt via self.halt().
                log(f"step failed: {type(e).__name__}: {e}")
                traceback.print_exc()
            if self.state["halted"]:
                log(f"Halted: {self.state['halted']}. Fix, edit {self.state_path}, restart.")
                return
            self.sleep(config.POLL_SECONDS)


def check(client: KrakenClient) -> bool:
    """Readiness check. Places nothing."""
    ok = True
    live = config.live_trading_enabled()
    print(f"Mode: {'LIVE (real orders)' if live else 'VALIDATE (orders checked, not placed)'}")
    print(f"  config.LIVE_TRADING={config.LIVE_TRADING}, KAREN_LIVE_TRADING={config.LIVE_ENV_FLAG}")
    if not client.has_keys():
        print("FAIL: KRAKEN_API_KEY / KRAKEN_API_SECRET not set")
        return False
    trader = LiveTrader(client, live=live)
    try:
        trader.load_pairs()
        names = ", ".join(f"{c}={i['pair']}" for c, i in trader.pairs.items())
        print(f"OK: pairs {names}")
    except Exception as e:
        print(f"FAIL: AssetPairs: {e}")
        return False
    try:
        bal = client.balance()
        print(f"OK: Query Funds permission. GBP balance £{bal.get('ZGBP', bal.get('GBP', 0.0)):.2f}")
        if bal.get("ZGBP", bal.get("GBP", 0.0)) < config.MAX_TRADE_GBP:
            print(f"WARN: GBP balance is below one trade (£{config.MAX_TRADE_GBP:.0f})")
    except Exception as e:
        print(f"FAIL: Balance (needs 'Funds: Query'): {e}")
        ok = False
    for coin, info in trader.pairs.items():
        try:
            q = client.ticker(info["pair"])
            vol = floor_to(config.MAX_TRADE_GBP / (q["ask"] * (1 + config.FEE)), int(info["lot_decimals"]))
            ordermin = float(info.get("ordermin", 0))
            if vol < ordermin:
                print(f"FAIL: {coin} £{config.MAX_TRADE_GBP:.0f} buys {vol}, below Kraken minimum {ordermin}")
                ok = False
                continue
            client.add_market_order(info["pair"], "buy", fmt_volume(vol, int(info["lot_decimals"])), validate=True)
            print(f"OK: {coin} validate buy {vol} @ ~{q['ask']} accepted (not placed)")
        except KrakenError as e:
            if "Insufficient funds" in str(e):
                print(f"WARN: {coin} validate buy: insufficient funds (order format is fine)")
            else:
                print(f"FAIL: {coin} validate buy (needs 'Create & modify orders'): {e}")
                ok = False
        except Exception as e:
            print(f"FAIL: {coin}: {e}")
            ok = False
    st = trader.state
    print(f"State file: {trader.state_path} | open positions: {list(st['positions'])} | "
          f"total P&L £{st['total_pnl']:+.2f} | halted: {st['halted']}")
    print("READY" if ok else "NOT READY")
    return ok


def state_path(live: bool, state_dir: str = config.STATE_DIR) -> str:
    return os.path.join(state_dir, "positions_live.json" if live else "positions_validate.json")


def start_trader(client: KrakenClient) -> LiveTrader:
    """Set up a trader in the mode the switches pick. Raises RuntimeError if it can't start."""
    if not client.has_keys():
        raise RuntimeError("Set KRAKEN_API_KEY and KRAKEN_API_SECRET first.")
    live = config.live_trading_enabled()
    trader = LiveTrader(client, live=live)
    trader.load_pairs()
    if not trader.pairs:
        raise RuntimeError("No tradable pairs found.")
    if live:
        trader.reconcile(client.balance())
    return trader


def main() -> None:
    client = KrakenClient()
    if len(sys.argv) > 1 and sys.argv[1] == "check":
        raise SystemExit(0 if check(client) else 1)
    try:
        trader = start_trader(client)
    except RuntimeError as e:
        raise SystemExit(str(e))
    trader.run_forever()


if __name__ == "__main__":
    main()
