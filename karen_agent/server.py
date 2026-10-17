"""Karen hosted service (simulation only).

What it does:
  - Every hour, downloads the latest Kraken public candles for the watchlist.
  - Re-runs the strategy over the last 90 days of those prices on a shared
    simulated portfolio. This is a rolling simulation, not a forward test:
    nothing is carried over between runs.
  - Serves a password-protected HTML dashboard and JSON with the latest results.

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
START_CASH = 1000.0


def _refresh_minutes() -> int:
    """Read REFRESH_MINUTES, falling back to 60 if it's missing or not a positive number."""
    try:
        minutes = int(os.environ.get("REFRESH_MINUTES", "60"))
        return minutes if minutes > 0 else 60
    except ValueError:
        return 60


REFRESH_SECONDS = _refresh_minutes() * 60

state = {
    "updated": None,
    "status": "starting",
    "error": None,
    "summary": None,
    "per_coin": {},
    "trades": [],
}
state_lock = threading.Lock()


def per_coin_stats(trades: list) -> dict:
    """Closed-trade count, wins and P&L for each coin, from the broker's trade log."""
    stats: dict[str, dict] = {}
    for t in trades:
        if not t[1].startswith("SELL:"):
            continue
        sym = t[1].split(":")[1]
        d = stats.setdefault(sym, {"trades": 0, "wins": 0, "pnl": 0.0})
        d["trades"] += 1
        d["wins"] += 1 if t[3] > 0 else 0
        d["pnl"] += t[3]
    return {
        sym: {
            "trades": d["trades"],
            "win_rate_pct": round(d["wins"] / d["trades"] * 100, 1),
            "pnl_gbp": round(d["pnl"], 2),
        }
        for sym, d in stats.items()
    }


def refresh_once() -> None:
    """Download candles, run the simulation, store the result."""
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

        # Refuse to show results on old data: the newest candle must be under 3 hours old.
        newest = max(f["timestamp"].max() for f in frames.values())
        age_h = (datetime.now(timezone.utc) - datetime.fromisoformat(
            str(newest)).replace(tzinfo=timezone.utc)).total_seconds() / 3600
        if age_h > 3:
            raise RuntimeError(f"Price data is stale ({age_h:.1f} hours old)")

        broker = run_portfolio(frames, start_cash=START_CASH)
        sells = [t for t in broker.trades if t[1].startswith("SELL")]
        wins = [t for t in sells if t[3] > 0]
        summary = {
            "coins": sorted(frames),
            "closed_trades": len(sells),
            "win_rate_pct": round(len(wins) / len(sells) * 100, 1) if sells else None,
            "return_pct": round((broker.cash - START_CASH) / START_CASH * 100, 2),
            "ending_cash_gbp": round(broker.cash, 2),
            "start_cash_gbp": START_CASH,
            "mode": "SIMULATION ONLY (last 90 days, re-run hourly)",
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
                per_coin=per_coin_stats(broker.trades),
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


def _bar_chart(per_coin: dict) -> str:
    """Inline SVG bar chart of P&L per coin. Green = profit, red = loss."""
    if not per_coin:
        return "<p class='muted'>No closed trades yet.</p>"
    items = sorted(per_coin.items())
    values = [v["pnl_gbp"] for _, v in items]
    biggest = max(max(abs(v) for v in values), 1.0)
    width, row_h, label_w = 560, 34, 80
    zero_x = label_w + (width - label_w) / 2
    half = (width - label_w) / 2 - 8
    height = row_h * len(items) + 10
    bars = []
    for i, (sym, v) in enumerate(items):
        y = 5 + i * row_h
        pnl = v["pnl_gbp"]
        length = abs(pnl) / biggest * half
        x = zero_x - length if pnl < 0 else zero_x
        colour = "#2e7d32" if pnl >= 0 else "#c62828"
        bars.append(
            f'<text x="4" y="{y + 20}" font-size="14">{html.escape(sym)}</text>'
            f'<rect x="{x:.1f}" y="{y + 6}" width="{max(length, 1):.1f}" height="20" fill="{colour}" rx="3"/>'
            f'<text x="{(zero_x + length + 6) if pnl >= 0 else (zero_x - length - 6):.1f}" y="{y + 21}" '
            f'font-size="13" text-anchor="{"start" if pnl >= 0 else "end"}">£{pnl:+.2f}</text>'
        )
    return (
        f'<svg viewBox="0 0 {width} {height}" width="100%" role="img" aria-label="P&L per coin">'
        f'<line x1="{zero_x}" y1="0" x2="{zero_x}" y2="{height}" stroke="#999"/>'
        + "".join(bars) + "</svg>"
    )


def page_html() -> str:
    with state_lock:
        s = dict(state)
    summ = s["summary"] or {}
    per_coin = s["per_coin"] or {}
    status = s["status"]
    status_colour = {"ok": "#2e7d32", "error": "#c62828"}.get(status, "#757575")

    def card(label: str, value: str, sub: str = "") -> str:
        return (f'<div class="card"><div class="label">{html.escape(label)}</div>'
                f'<div class="value">{value}</div><div class="sub">{html.escape(sub)}</div></div>')

    ret = summ.get("return_pct")
    ret_txt = f"{ret:+.2f}%" if ret is not None else "n/a"
    ret_colour = "#2e7d32" if (ret or 0) >= 0 else "#c62828"
    cards = "".join([
        card("Simulated return", f'<span style="color:{ret_colour}">{ret_txt}</span>',
             f"on £{START_CASH:,.0f} start"),
        card("Ending cash", f"£{summ.get('ending_cash_gbp', 'n/a')}"),
        card("Closed trades", str(summ.get("closed_trades", "n/a"))),
        card("Win rate", f"{summ.get('win_rate_pct', 'n/a')}%"),
    ])

    coin_rows = "".join(
        f"<tr><td>{html.escape(sym)}</td><td>{v['trades']}</td><td>{v['win_rate_pct']}%</td>"
        f"<td style='color:{'#2e7d32' if v['pnl_gbp'] >= 0 else '#c62828'}'>£{v['pnl_gbp']:+.2f}</td></tr>"
        for sym, v in sorted(per_coin.items())
    ) or "<tr><td colspan=4>No closed trades yet</td></tr>"

    trade_rows = "".join(
        f"<tr><td>{html.escape(t['time'])}</td><td>{html.escape(t['action'])}</td>"
        f"<td>{t['price']}</td><td>{t['amount']:+.2f}</td></tr>"
        for t in reversed(s["trades"])
    ) or "<tr><td colspan=4>No trades yet</td></tr>"

    error_html = (f'<div class="error">Last refresh failed: {html.escape(str(s["error"]))}</div>'
                  if s["error"] else "")

    return f"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<meta http-equiv="refresh" content="300">
<title>Karen dashboard</title>
<style>
  :root {{ color-scheme: light dark; }}
  body {{ font-family: system-ui, sans-serif; margin: 0; padding: 16px; max-width: 760px; margin-inline: auto; }}
  h1 {{ font-size: 22px; margin: 0 0 4px; }}
  h2 {{ font-size: 16px; margin: 24px 0 8px; }}
  .muted {{ color: #757575; font-size: 13px; }}
  .banner {{ background: #fff3cd; color: #5d4500; padding: 10px; border-radius: 8px; font-size: 14px; }}
  .error {{ background: #fdecea; color: #611a15; padding: 10px; border-radius: 8px; font-size: 14px; margin-top: 8px; }}
  .pill {{ display: inline-block; padding: 2px 10px; border-radius: 999px; color: #fff; font-size: 12px; }}
  .cards {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(150px, 1fr)); gap: 10px; margin-top: 12px; }}
  .card {{ border: 1px solid #ddd; border-radius: 10px; padding: 12px; }}
  .label {{ font-size: 12px; color: #757575; }}
  .value {{ font-size: 22px; font-weight: 600; margin-top: 4px; }}
  .sub {{ font-size: 12px; color: #757575; }}
  table {{ border-collapse: collapse; width: 100%; font-size: 14px; }}
  td, th {{ border-bottom: 1px solid #ddd; padding: 6px; text-align: left; }}
  th {{ font-size: 12px; color: #757575; font-weight: 500; }}
  .chart {{ border: 1px solid #ddd; border-radius: 10px; padding: 10px; }}
</style></head><body>
<h1>Karen agent</h1>
<div class="muted">
  <span class="pill" style="background:{status_colour}">{html.escape(str(status))}</span>
  Last updated: {html.escape(str(s['updated'] or 'never'))} (UTC) · Auto-refreshes every 5 min
</div>
<div class="banner"><b>SIMULATION ONLY.</b> Re-runs the strategy on the last 90 days of prices every hour.
No orders are placed, and nothing carries over between runs.</div>
{error_html}
<div class="cards">{cards}</div>
<h2>Profit or loss by coin</h2>
<div class="chart">{_bar_chart(per_coin)}</div>
<h2>Per coin</h2>
<table><tr><th>Coin</th><th>Closed trades</th><th>Win rate</th><th>P&amp;L</th></tr>{coin_rows}</table>
<h2>Recent trades (newest first)</h2>
<table><tr><th>Time (UTC)</th><th>Action</th><th>Price</th><th>Cash change (£)</th></tr>{trade_rows}</table>
<p class="muted">Coins: {html.escape(', '.join(summ.get('coins', [])) or 'n/a')} · Costs assumed: fees {config.FEE*100:.2f}% per side, slippage {config.SLIPPAGE*100:.2f}%</p>
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
