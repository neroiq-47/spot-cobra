"""Karen hosted service (paper trading only).

What it does:
  - Every hour, downloads the latest Kraken public candles for the watchlist.
  - Runs the strategy on a shared paper portfolio.
  - Serves a password-protected page and JSON with the latest results.

What it never does:
  - Place real orders. This service has no live trading path.
  - Show or log the Kraken API key (it isn't needed: public prices only).

Required environment variables (set in Railway, never in code):
  KAREN_USER       login name for the page
  KAREN_PASSWORD   login password for the page (use a long random one)
Optional:
  PORT             set automatically by Railway
  REFRESH_MINUTES  how often to refresh, default 60
"""

import base64
import hmac
import html
import json
import os
import threading
import time
import traceback
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import config
import fetch_kraken
from agent import load_candles, run_portfolio

DATA_DIR = fetch_kraken.OUT_DIR
REFRESH_SECONDS = int(os.environ.get("REFRESH_MINUTES", "60")) * 60

state = {
    "updated": None,
    "status": "starting",
    "error": None,
    "summary": None,
    "trades": [],
}
state_lock = threading.Lock()


def refresh_once() -> None:
    """Download candles, run the paper portfolio, store the result."""
    try:
        pairs = fetch_kraken.resolve_pairs()
        for coin, pair in pairs.items():
            rows = fetch_kraken.fetch_pair(pair)
            if rows:
                fetch_kraken.write_csv(coin, rows)

        frames = {}
        for coin in pairs:
            path = os.path.join(DATA_DIR, f"{coin}.csv")
            if os.path.exists(path):
                frames[coin] = load_candles(path)
        if not frames:
            raise RuntimeError("No price data downloaded.")

        # Refuse to trade on old data: the newest candle must be under 3 hours old.
        newest = max(f["timestamp"].max() for f in frames.values())
        age_h = (datetime.now(timezone.utc) - datetime.fromisoformat(
            str(newest)).replace(tzinfo=timezone.utc)).total_seconds() / 3600
        if age_h > 3:
            raise RuntimeError(f"Price data is stale ({age_h:.1f} hours old)")

        broker = run_portfolio(frames)
        sells = [t for t in broker.trades if t[1].startswith("SELL")]
        wins = [t for t in sells if t[3] > 0]
        summary = {
            "coins": sorted(frames),
            "closed_trades": len(sells),
            "win_rate_pct": round(len(wins) / len(sells) * 100, 1) if sells else None,
            "return_pct": round((broker.cash - 1000.0) / 1000.0 * 100, 2),
            "ending_cash_gbp": round(broker.cash, 2),
            "mode": "PAPER ONLY",
        }
        recent = [
            {"time": str(t[0]), "action": t[1], "price": round(float(t[2]), 6), "amount": round(float(t[3]), 2)}
            for t in broker.trades[-25:]
        ]
        with state_lock:
            state.update(
                updated=datetime.now(timezone.utc).isoformat(timespec="seconds"),
                status="ok",
                error=None,
                summary=summary,
                trades=recent,
            )
    except Exception as e:
        # Keep the last good result and record the error. Don't crash the service.
        with state_lock:
            state.update(status="error", error=f"{type(e).__name__}: {e}")
        traceback.print_exc()


def refresh_loop() -> None:
    while True:
        refresh_once()
        time.sleep(REFRESH_SECONDS)


def page_html() -> str:
    with state_lock:
        s = dict(state)
    rows = "".join(
        f"<tr><td>{html.escape(t['time'])}</td><td>{html.escape(t['action'])}</td>"
        f"<td>{t['price']}</td><td>{t['amount']}</td></tr>"
        for t in s["trades"]
    ) or "<tr><td colspan=4>No trades yet</td></tr>"
    summ = s["summary"] or {}
    return f"""<!doctype html><html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Karen (paper)</title>
<style>body{{font-family:system-ui;margin:16px;max-width:720px}}
table{{border-collapse:collapse;width:100%}}td,th{{border-bottom:1px solid #ddd;padding:6px;text-align:left;font-size:14px}}
.warn{{background:#fff3cd;padding:8px;border-radius:6px}}</style></head><body>
<h2>Karen agent</h2>
<p class="warn"><b>PAPER TRADING ONLY.</b> No real orders are placed.</p>
<p>Last updated: {html.escape(str(s['updated']))} · Status: {html.escape(str(s['status']))}</p>
{'<p>Error: ' + html.escape(str(s['error'])) + '</p>' if s['error'] else ''}
<ul>
<li>Coins: {html.escape(', '.join(summ.get('coins', [])) or 'n/a')}</li>
<li>Closed trades: {summ.get('closed_trades', 'n/a')}</li>
<li>Win rate: {summ.get('win_rate_pct', 'n/a')}%</li>
<li>Return: {summ.get('return_pct', 'n/a')}% on £1000 (paper)</li>
</ul>
<h3>Recent trades</h3>
<table><tr><th>Time (UTC)</th><th>Action</th><th>Price</th><th>Amount (£)</th></tr>{rows}</table>
</body></html>"""


def authorised(header: str | None) -> bool:
    user = os.environ.get("KAREN_USER", "")
    password = os.environ.get("KAREN_PASSWORD", "")
    if not user or not password or not header or not header.startswith("Basic "):
        return False
    try:
        supplied = base64.b64decode(header[6:]).decode()
    except Exception:
        return False
    u, _, p = supplied.partition(":")
    return hmac.compare_digest(u, user) and hmac.compare_digest(p, password)


class Handler(BaseHTTPRequestHandler):
    def _deny(self) -> None:
        time.sleep(1)  # slow down password guessing
        self.send_response(401)
        self.send_header("WWW-Authenticate", 'Basic realm="Karen"')
        self.end_headers()

    def do_GET(self) -> None:
        if not authorised(self.headers.get("Authorization")):
            return self._deny()
        if self.path == "/api/report":
            with state_lock:
                body = json.dumps(state, default=str).encode()
            ctype = "application/json"
        elif self.path == "/":
            body = page_html().encode()
            ctype = "text/html; charset=utf-8"
        else:
            self.send_response(404)
            self.end_headers()
            return
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, fmt, *args) -> None:  # keep logs free of request details
        return


def main() -> None:
    if not os.environ.get("KAREN_USER") or not os.environ.get("KAREN_PASSWORD"):
        raise SystemExit("Set KAREN_USER and KAREN_PASSWORD before starting.")
    if config.live_trading_enabled():
        # The hosted service is paper-only by design.
        print("Live switch is ON in config, but the hosted service ignores it.")
    os.makedirs(DATA_DIR, exist_ok=True)
    threading.Thread(target=refresh_loop, daemon=True).start()
    port = int(os.environ.get("PORT", "8080"))
    ThreadingHTTPServer(("0.0.0.0", port), Handler).serve_forever()


if __name__ == "__main__":
    main()
