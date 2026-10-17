"""Download hourly candles from Kraken's PUBLIC price data (no login, no API key).

Run this on your own computer:
  python fetch_kraken.py

It writes one CSV per coin into ./data/ (SOL.csv, LINK.csv, ...), in the format
agent.py expects: timestamp, open, high, low, close, volume.

This only reads public market prices. It cannot place orders or see your account.
"""

import csv
import json
import os
import time
import urllib.parse
import urllib.request

API = "https://api.kraken.com/0/public/OHLC"
ASSET_PAIRS_API = "https://api.kraken.com/0/public/AssetPairs"

# Coins to download, matched against Kraken's own list of pairs (e.g. "SOL/GBP").
COINS = ["SOL", "LINK", "GRT", "POPCAT", "WIF"]
INTERVAL_MIN = 60          # 1-hour candles
START_DAYS = 90            # history to download, counted back from now
MAX_PAGES = 40             # about 720 candles per page; hard cap on requests
OUT_DIR = "data"


def resolve_pairs() -> dict[str, str]:
    """Ask Kraken for its pair list and return {coin: Kraken pair name} for GBP pairs.

    Matches the readable name "SOL/GBP" (field 'wsname'), so no pair name is guessed.
    Coins with no GBP pair are left out and reported.
    """
    with urllib.request.urlopen(ASSET_PAIRS_API, timeout=30) as resp:
        body = json.loads(resp.read().decode())
    if body.get("error"):
        raise RuntimeError(f"AssetPairs: {body['error']}")
    by_name = {}
    for key, info in body["result"].items():
        ws = info.get("wsname", "")
        if ws.endswith("/GBP"):
            by_name[ws.split("/")[0]] = key
    found = {}
    for coin in COINS:
        if coin in by_name:
            found[coin] = by_name[coin]
        else:
            print(f"{coin}: no {coin}/GBP pair on Kraken")
    return found


def fetch_page(pair: str, since: int | None) -> tuple[list, int | None]:
    params = {"pair": pair, "interval": INTERVAL_MIN}
    if since is not None:
        params["since"] = since
    url = f"{API}?{urllib.parse.urlencode(params)}"
    with urllib.request.urlopen(url, timeout=30) as resp:
        body = json.loads(resp.read().decode())
    if body.get("error"):
        raise RuntimeError(f"{pair}: {body['error']}")
    result = body["result"]
    key = next(k for k in result if k != "last")
    return result[key], int(result["last"])


def fetch_pair(pair: str) -> list:
    """Download START_DAYS of hourly candles, oldest first, with no duplicates.

    Starts from a fixed point in time so history doesn't depend on Kraken's default.
    Leaves out the candle that is still forming, since its close is not final.
    """
    now = int(time.time())
    rows = []
    since = now - START_DAYS * 86400
    for _ in range(MAX_PAGES):
        page, last = fetch_page(pair, since)
        if not page:
            break
        rows.extend(page)
        if last == since:
            break
        since = last
        time.sleep(1.1)  # stay within Kraken's public rate limit

    seen, clean = set(), []
    for r in sorted(rows, key=lambda r: r[0]):
        if r[0] in seen:
            continue
        seen.add(r[0])
        # Keep only candles that have fully closed.
        if int(r[0]) + INTERVAL_MIN * 60 <= now:
            clean.append(r)
    return clean


def write_csv(coin: str, rows: list) -> str:
    os.makedirs(OUT_DIR, exist_ok=True)
    path = os.path.join(OUT_DIR, f"{coin}.csv")
    with open(path, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["timestamp", "open", "high", "low", "close", "volume"])
        for r in rows:
            ts = time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime(int(r[0])))
            w.writerow([ts, r[1], r[2], r[3], r[4], r[6]])
    return path


def main() -> None:
    pairs = resolve_pairs()
    print("Pairs found:", pairs)
    for coin, pair in pairs.items():
        try:
            rows = fetch_pair(pair)
        except Exception as e:  # keep going so one missing coin doesn't stop the rest
            print(f"{coin}: skipped ({e})")
            continue
        if not rows:
            print(f"{coin}: no data returned for {pair}")
            continue
        path = write_csv(coin, rows)
        print(f"{coin}: {len(rows)} hourly candles -> {path}")


if __name__ == "__main__":
    main()
