"""Kraken spot REST client for live trading.

Reads the API key from the environment only:
  KRAKEN_API_KEY      public key
  KRAKEN_API_SECRET   private key (base64, as Kraken shows it)

Create the key with ONLY these permissions:
  Funds: Query
  Orders and trades: Query open orders & trades, Query closed orders & trades,
                     Create & modify orders
Never tick Withdraw. The key is never printed or logged by this module.
"""

import base64
import hashlib
import hmac
import json
import os
import threading
import time
import urllib.parse
import urllib.request

API_URL = "https://api.kraken.com"


def sign(url_path: str, data: dict, secret: str) -> str:
    """API-Sign: HMAC-SHA512 of (path + SHA256(nonce + postdata)) with the decoded secret."""
    postdata = urllib.parse.urlencode(data)
    message = url_path.encode() + hashlib.sha256((str(data["nonce"]) + postdata).encode()).digest()
    mac = hmac.new(base64.b64decode(secret), message, hashlib.sha512)
    return base64.b64encode(mac.digest()).decode()


class KrakenError(RuntimeError):
    pass


class KrakenClient:
    def __init__(self, key: str | None = None, secret: str | None = None, timeout: int = 30):
        self.key = key if key is not None else os.environ.get("KRAKEN_API_KEY", "")
        self.secret = secret if secret is not None else os.environ.get("KRAKEN_API_SECRET", "")
        self.timeout = timeout
        self._nonce_lock = threading.Lock()
        self._last_nonce = 0

    def has_keys(self) -> bool:
        return bool(self.key and self.secret)

    def _nonce(self) -> int:
        # Always increasing, even if two calls land in the same millisecond.
        with self._nonce_lock:
            n = max(int(time.time() * 1000), self._last_nonce + 1)
            self._last_nonce = n
            return n

    def _send(self, req: urllib.request.Request) -> dict:
        with urllib.request.urlopen(req, timeout=self.timeout) as resp:
            body = json.loads(resp.read().decode())
        if body.get("error"):
            raise KrakenError(", ".join(body["error"]))
        return body["result"]

    def public(self, method: str, params: dict | None = None) -> dict:
        url = f"{API_URL}/0/public/{method}"
        if params:
            url += "?" + urllib.parse.urlencode(params)
        return self._send(urllib.request.Request(url))

    def private(self, method: str, params: dict | None = None) -> dict:
        if not self.has_keys():
            raise KrakenError("KRAKEN_API_KEY / KRAKEN_API_SECRET are not set")
        path = f"/0/private/{method}"
        data = {"nonce": self._nonce(), **(params or {})}
        req = urllib.request.Request(
            API_URL + path,
            data=urllib.parse.urlencode(data).encode(),
            headers={
                "API-Key": self.key,
                "API-Sign": sign(path, data, self.secret),
                "Content-Type": "application/x-www-form-urlencoded; charset=utf-8",
            },
        )
        return self._send(req)

    # --- Market data ---
    def asset_pairs(self) -> dict:
        return self.public("AssetPairs")

    def ticker(self, pair: str) -> dict:
        """Returns {'ask': float, 'bid': float, 'last': float} for one pair."""
        result = self.public("Ticker", {"pair": pair})
        t = next(iter(result.values()))
        return {"ask": float(t["a"][0]), "bid": float(t["b"][0]), "last": float(t["c"][0])}

    # --- Account ---
    def balance(self) -> dict[str, float]:
        return {k: float(v) for k, v in self.private("Balance").items()}

    def add_market_order(self, pair: str, side: str, volume: str, validate: bool,
                         userref: int | None = None) -> dict:
        """Market order. With validate=True Kraken checks the order but does not place it."""
        params = {"pair": pair, "type": side, "ordertype": "market", "volume": volume}
        if validate:
            params["validate"] = "true"
        if userref is not None:
            params["userref"] = str(userref)
        return self.private("AddOrder", params)

    def query_order(self, txid: str) -> dict:
        return self.private("QueryOrders", {"txid": txid, "trades": "true"})[txid]
