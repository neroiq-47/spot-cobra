"""Karen hosted service: dashboard, plus the live trader when KAREN_RUN_TRADER=ON.

What it does:
  - Every hour, downloads the latest Kraken public candles for the watchlist.
  - Re-runs the strategy over the last 90 days of those prices on a shared
    simulated portfolio. This is a rolling simulation, not a forward test:
    nothing is carried over between runs.
  - Shows the live trader's saved state (live.py), read-only, if its state
    file is in KAREN_STATE_DIR: mode, halt status, open positions and P&L.
  - Serves a password-protected HTML dashboard and JSON with the latest results.
  - If KAREN_RUN_TRADER=ON, also runs the live.py trading loop in this process,
    so one Railway service and one volume hold both the page and the trader.
    The trader is in validate mode (no orders placed) unless both live switches are ON.

What it never does:
  - Place orders from the web page itself. Pages and /api/report are read-only.
  - Show or log the Kraken API key (it isn't needed: public prices only).

Required environment variables (set in Railway, never in code):
  KAREN_USER       login name for the page
  KAREN_PASSWORD   login password for the page (use a long random one)
Optional:
  PORT             set automatically by Railway
  REFRESH_MINUTES  how often to refresh, default 60
  KAREN_STATE_DIR  folder holding live.py's state files, default "state"
  KAREN_RUN_TRADER ON to run the trader here (needs KRAKEN_API_KEY / KRAKEN_API_SECRET)
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
RUN_TRADER = os.environ.get("KAREN_RUN_TRADER", "OFF").upper() == "ON"
TRADER_RETRY_SECONDS = 60

# What the in-process trader is doing, for the dashboard.
trader_status = {"state": "off" if not RUN_TRADER else "starting", "detail": None}

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


def trader_loop() -> None:
    """Run the live.py loop in this process. Retries start-up (e.g. Kraken unreachable at boot)."""
    import live
    from kraken_client import KrakenClient

    while True:
        try:
            client = KrakenClient()
            if not client.has_keys():
                trader_status.update(state="error", detail="KRAKEN_API_KEY / KRAKEN_API_SECRET are not set")
                return
            trader = live.LiveTrader(client, live=config.live_trading_enabled())
            trader.load_pairs()
            if not trader.pairs:
                raise RuntimeError("No tradable pairs found")
            if trader.live:
                trader.reconcile(client.balance())
            trader_status.update(state="running", detail="LIVE" if trader.live else "VALIDATE")
            trader.run_forever()   # returns only when halted
            trader_status.update(state="halted", detail=trader.state["halted"])
            return
        except Exception as e:
            trader_status.update(state="error", detail=f"start-up failed, retrying: {type(e).__name__}: {e}")
            traceback.print_exc()
            time.sleep(TRADER_RETRY_SECONDS)


def refresh_loop() -> None:
    while True:
        refresh_once()
        time.sleep(REFRESH_SECONDS)


def load_trader() -> list[dict]:
    """Read live.py's state files, if any. Read-only: the dashboard never trades."""
    found = []
    for mode, name in (("LIVE", "positions_live.json"), ("VALIDATE", "positions_validate.json")):
        path = os.path.join(config.STATE_DIR, name)
        if not os.path.exists(path):
            continue
        try:
            with open(path) as f:
                st = json.load(f)
        except (OSError, ValueError) as e:
            found.append({"mode": mode, "error": f"{type(e).__name__}: {e}"})
            continue
        found.append({"mode": mode, "updated": datetime.fromtimestamp(
            os.path.getmtime(path), timezone.utc).isoformat(timespec="seconds"), **st})
    return found


def _money(v: float) -> str:
    cls = "pos" if v >= 0 else "neg"
    return f'<span class="{cls}">£{v:+.2f}</span>'


def _bar_chart(per_coin: dict) -> str:
    """Inline SVG bar chart of P&L per coin. Green = profit, red = loss."""
    if not per_coin:
        return "<p class='muted'>No closed trades yet.</p>"
    items = sorted(per_coin.items())
    values = [v["pnl_gbp"] for _, v in items]
    biggest = max(max(abs(v) for v in values), 1.0)
    width, row_h, label_w = 560, 34, 80
    zero_x = label_w + (width - label_w) / 2
    half = (width - label_w) / 2 - 64   # leave room for the £ label at the bar end
    height = row_h * len(items) + 10
    bars = []
    for i, (sym, v) in enumerate(items):
        y = 5 + i * row_h
        pnl = v["pnl_gbp"]
        length = abs(pnl) / biggest * half
        x = zero_x - length if pnl < 0 else zero_x
        cls = "bar-pos" if pnl >= 0 else "bar-neg"
        bars.append(
            f'<text x="4" y="{y + 20}" font-size="14">{html.escape(sym)}</text>'
            f'<rect class="{cls}" x="{x:.1f}" y="{y + 6}" width="{max(length, 1):.1f}" height="20" rx="3"/>'
            f'<text x="{(zero_x + length + 6) if pnl >= 0 else (zero_x - length - 6):.1f}" y="{y + 21}" '
            f'font-size="13" text-anchor="{"start" if pnl >= 0 else "end"}">£{pnl:+.2f}</text>'
        )
    return (
        f'<svg viewBox="0 0 {width} {height}" width="100%" role="img" aria-label="P&L per coin">'
        f'<line class="axis" x1="{zero_x}" y1="0" x2="{zero_x}" y2="{height}"/>'
        + "".join(bars) + "</svg>"
    )


def card(label: str, value: str, sub: str = "") -> str:
    return (f'<div class="card"><div class="label">{html.escape(label)}</div>'
            f'<div class="value">{value}</div><div class="sub">{html.escape(sub)}</div></div>')


def trader_status_html() -> str:
    st, detail = trader_status["state"], trader_status["detail"]
    text = {
        "off": "Not running in this service (KAREN_RUN_TRADER is off).",
        "starting": "Starting…",
        "running": f"Running here in {detail} mode.",
        "halted": f"Stopped: {detail}",
        "error": f"Not running: {detail}",
    }[st]
    cls = {"running": "ok", "off": "starting", "starting": "starting"}.get(st, "error")
    return f'<p class="muted"><span class="pill {cls}">trader {html.escape(st)}</span> {html.escape(text)}</p>'


def trader_html(traders: list[dict]) -> str:
    """The live trader section: one block per state file found."""
    if not traders:
        return (f'<div class="note">No trader state yet in <code>{html.escape(config.STATE_DIR)}</code>. '
                f'Set <code>KAREN_RUN_TRADER=ON</code> and the Kraken key on this service to run it here.</div>')
    out = []
    for t in traders:
        mode = t["mode"]
        badge = ('<span class="pill live">LIVE: real orders</span>' if mode == "LIVE"
                 else '<span class="pill validate">VALIDATE: orders checked, none placed</span>')
        if "error" in t:
            out.append(f'<h3>{badge}</h3><div class="error">State file unreadable: {html.escape(t["error"])}</div>')
            continue
        halted = (f'<div class="error"><b>HALTED.</b> {html.escape(str(t["halted"]))} '
                  f'Check Kraken, clear <code>halted</code> in the state file, restart the trader.</div>'
                  if t.get("halted") else "")
        positions = t.get("positions", {})
        cards = "".join([
            card("Realised P&L (total)", _money(t.get("total_pnl", 0.0)),
                 f"stops new buys at -£{config.MAX_TOTAL_LOSS_GBP:.0f}"),
            card("Realised P&L (today)", _money(t.get("day_pnl", 0.0)),
                 f"{t.get('day') or 'n/a'} · limit -£{config.MAX_DAILY_LOSS_GBP:.0f}"),
            card("Open positions", f"{len(positions)} / {config.MAX_OPEN_POSITIONS}",
                 f"£{config.MAX_TRADE_GBP:.0f} max per buy"),
            card("Trades logged", str(len(t.get("trades", []))), f"state saved {t['updated']}"),
        ])
        pos_rows = "".join(
            f"<tr><td>{html.escape(c)}</td><td>{p['volume']:.6g}</td><td>£{p['entry']:.6g}</td>"
            f"<td>£{p['cost']:.2f}</td><td>£{p['entry'] * 0.95:.6g}</td><td>{p['held']}h</td></tr>"
            for c, p in sorted(positions.items())
        ) or "<tr><td colspan=6>No open positions</td></tr>"
        trade_rows = "".join(
            f"<tr><td>{html.escape(str(x['time']))}</td><td>{html.escape(x['coin'])}</td>"
            f"<td>{html.escape(x['action'])}</td><td>£{x['price']:.6g}</td><td>{_money(x['amount_gbp'])}</td></tr>"
            for x in reversed(t.get("trades", [])[-25:])
        ) or "<tr><td colspan=5>No trades yet</td></tr>"
        out.append(f"""<h3>{badge}</h3>{halted}
<div class="cards">{cards}</div>
<h4>Open positions</h4>
<div class="scroll"><table><tr><th>Coin</th><th>Volume</th><th>Entry</th><th>Cost</th><th>Stop (−5%)</th><th>Held</th></tr>{pos_rows}</table></div>
<h4>Recent trades (newest first)</h4>
<div class="scroll"><table><tr><th>Time (UTC)</th><th>Coin</th><th>Action</th><th>Price</th><th>Cash change</th></tr>{trade_rows}</table></div>""")
    return "".join(out)


def page_html() -> str:
    with state_lock:
        s = dict(state)
    summ = s["summary"] or {}
    per_coin = s["per_coin"] or {}
    status = s["status"]
    traders = load_trader()

    ret = summ.get("return_pct")
    ret_txt = f"{ret:+.2f}%" if ret is not None else "n/a"
    cards = "".join([
        card("Simulated return", f'<span class="{"pos" if (ret or 0) >= 0 else "neg"}">{ret_txt}</span>',
             f"on £{START_CASH:,.0f} start"),
        card("Ending cash", f"£{summ.get('ending_cash_gbp', 'n/a')}"),
        card("Closed trades", str(summ.get("closed_trades", "n/a"))),
        card("Win rate", f"{summ.get('win_rate_pct', 'n/a')}%"),
    ])

    coin_rows = "".join(
        f"<tr><td>{html.escape(sym)}</td><td>{v['trades']}</td><td>{v['win_rate_pct']}%</td>"
        f"<td>{_money(v['pnl_gbp'])}</td></tr>"
        for sym, v in sorted(per_coin.items())
    ) or "<tr><td colspan=4>No closed trades yet</td></tr>"

    trade_rows = "".join(
        f"<tr><td>{html.escape(t['time'])}</td><td>{html.escape(t['action'])}</td>"
        f"<td>{t['price']}</td><td>{t['amount']:+.2f}</td></tr>"
        for t in reversed(s["trades"])
    ) or "<tr><td colspan=4>No trades yet</td></tr>"

    error_html = (f'<div class="error">Last refresh failed: {html.escape(str(s["error"]))}</div>'
                  if s["error"] else "")

    live_on = config.live_trading_enabled()
    switches = (f"Live switches: config.LIVE_TRADING={config.LIVE_TRADING}, "
                f"KAREN_LIVE_TRADING={config.LIVE_ENV_FLAG} (as seen by this service)")
    if any(t["mode"] == "LIVE" for t in traders):
        banner = ('<div class="banner live-banner"><b>LIVE TRADING DATA PRESENT.</b> The trader has placed '
                  'or may place real orders on Kraken. This page only reads its state.</div>')
    else:
        banner = ('<div class="banner"><b>NO REAL ORDERS.</b> Real orders come only from the trader, '
                  'and only when both live switches are ON. Otherwise it validates orders and places none.</div>')

    return f"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<meta http-equiv="refresh" content="300">
<title>Karen dashboard</title>
<style>
  :root {{
    color-scheme: light;
    --bg: #f6f2fc; --surface: #ffffff; --ink: #24133d; --muted: #6e5d86;
    --line: #e2d8f1; --accent: #6a35b8; --header: #5b2ca6; --accent-soft: #ede4fa;
    --btn: #cfe8ff; --btn-hover: #b5dbff; --btn-ink: #0d3557; --btn-line: #9ccbf5;
    --pos: #1f7a3a; --neg: #b3261e;
    --warn-bg: #efe6fb; --warn-ink: #3d1f6b; --err-bg: #fdecea; --err-ink: #611a15;
  }}
  @media (prefers-color-scheme: dark) {{
    :root:not([data-theme="light"]) {{
      color-scheme: dark;
      --bg: #170f24; --surface: #21173a; --ink: #eee6fb; --muted: #b3a3cc;
      --line: #3a2b57; --accent: #b896f0; --header: #3b2170; --accent-soft: #2e2150;
      --btn: #a9d4fb; --btn-hover: #c4e2ff; --btn-ink: #082a47; --btn-line: #7fb9ec;
      --pos: #6fd18f; --neg: #ff8a80;
      --warn-bg: #2e2150; --warn-ink: #e3d4fb; --err-bg: #4a1d1a; --err-ink: #ffd8d3;
    }}
  }}
  * {{ box-sizing: border-box; }}
  body {{ font-family: system-ui, sans-serif; margin: 0; background: var(--bg); color: var(--ink); }}
  .wrap {{ padding: 16px; max-width: 820px; margin-inline: auto; }}
  header {{ background: var(--header); color: #fff; }}
  header .wrap {{ display: flex; flex-wrap: wrap; align-items: center; justify-content: space-between; gap: 10px; }}
  h1 {{ font-size: 22px; margin: 0; }}
  h2 {{ font-size: 18px; margin: 28px 0 4px; color: var(--accent); }}
  h3 {{ font-size: 15px; margin: 14px 0 6px; }}
  h4 {{ font-size: 14px; margin: 18px 0 6px; }}
  .muted {{ color: var(--muted); font-size: 13px; }}
  header .muted {{ color: #e6dbf7; }}
  .actions {{ display: flex; flex-wrap: wrap; gap: 8px; margin: 14px 0 4px; }}
  .btn {{ display: inline-block; background: var(--btn); color: var(--btn-ink); border: 1px solid var(--btn-line);
          padding: 7px 14px; border-radius: 8px; font-size: 14px; font-weight: 600; text-decoration: none; cursor: pointer; }}
  .btn:hover {{ background: var(--btn-hover); }}
  .btn:focus-visible {{ outline: 2px solid var(--accent); outline-offset: 2px; }}
  .banner {{ background: var(--warn-bg); color: var(--warn-ink); border-left: 4px solid var(--accent);
             padding: 10px 12px; border-radius: 8px; font-size: 14px; margin-top: 12px; }}
  .live-banner {{ border-left-color: var(--neg); }}
  .note {{ background: var(--surface); border: 1px dashed var(--line); padding: 10px 12px; border-radius: 8px; font-size: 14px; }}
  .error {{ background: var(--err-bg); color: var(--err-ink); padding: 10px; border-radius: 8px; font-size: 14px; margin-top: 8px; }}
  .pill {{ display: inline-block; padding: 2px 10px; border-radius: 999px; font-size: 12px; font-weight: 600; }}
  .pill.ok {{ background: #d7f5df; color: #14532d; }}
  .pill.error {{ background: #fdecea; color: #611a15; }}
  .pill.starting {{ background: #ede4fa; color: #3d1f6b; }}
  .pill.validate {{ background: var(--accent-soft); color: var(--accent); }}
  .pill.live {{ background: var(--neg); color: #fff; }}
  .cards {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(160px, 1fr)); gap: 10px; margin-top: 10px; }}
  .card {{ background: var(--surface); border: 1px solid var(--line); border-radius: 10px; padding: 12px; }}
  .label {{ font-size: 12px; color: var(--muted); }}
  .value {{ font-size: 22px; font-weight: 600; margin-top: 4px; }}
  .sub {{ font-size: 12px; color: var(--muted); }}
  .pos {{ color: var(--pos); }} .neg {{ color: var(--neg); }}
  .scroll {{ overflow-x: auto; }}
  table {{ border-collapse: collapse; width: 100%; font-size: 14px; background: var(--surface); border-radius: 10px; }}
  td, th {{ border-bottom: 1px solid var(--line); padding: 6px 8px; text-align: left; white-space: nowrap; }}
  th {{ font-size: 12px; color: var(--muted); font-weight: 500; }}
  .chart {{ background: var(--surface); border: 1px solid var(--line); border-radius: 10px; padding: 10px; }}
  .chart text {{ fill: var(--ink); }}
  .axis {{ stroke: var(--muted); }}
  .bar-pos {{ fill: var(--pos); }} .bar-neg {{ fill: var(--neg); }}
  code {{ background: var(--accent-soft); padding: 1px 5px; border-radius: 4px; overflow-wrap: anywhere; }}
</style></head><body>
<header><div class="wrap">
  <h1>Karen agent</h1>
  <span class="muted">Updated {html.escape(str(s['updated'] or 'never'))} UTC · auto-refresh 5 min</span>
</div></header>
<main class="wrap">
<div class="actions">
  <a class="btn" href="/">Refresh</a>
  <a class="btn" href="#trader">Live trader</a>
  <a class="btn" href="#simulation">Simulation</a>
  <a class="btn" href="/api/report">JSON report</a>
</div>
{banner}
<p class="muted">{html.escape(switches)}{' · LIVE mode' if live_on else ' · validate mode'}</p>

<h2 id="trader">Live trader (live.py)</h2>
<p class="muted">Forward trading on live Kraken prices. Read from <code>{html.escape(config.STATE_DIR)}</code>.</p>
{trader_status_html()}
{trader_html(traders)}

<h2 id="simulation">Simulation (rolling 90-day backtest)
  <span class="pill {html.escape(str(status))}">{html.escape(str(status))}</span></h2>
<p class="muted">Re-runs the strategy on the last 90 days of prices every hour. Nothing carries over between runs.</p>
{error_html}
<div class="cards">{cards}</div>
<h4>Profit or loss by coin</h4>
<div class="chart">{_bar_chart(per_coin)}</div>
<h4>Per coin</h4>
<div class="scroll"><table><tr><th>Coin</th><th>Closed trades</th><th>Win rate</th><th>P&amp;L</th></tr>{coin_rows}</table></div>
<h4>Recent simulated trades (newest first)</h4>
<div class="scroll"><table><tr><th>Time (UTC)</th><th>Action</th><th>Price</th><th>Cash change (£)</th></tr>{trade_rows}</table></div>
<p class="muted">Coins: {html.escape(', '.join(summ.get('coins', [])) or 'n/a')} · Costs assumed: fees {config.FEE*100:.2f}% per side, slippage {config.SLIPPAGE*100:.2f}%</p>
</main>
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
                report = dict(state)
            report["trader"] = load_trader()
            report["trader_status"] = dict(trader_status)
            body = json.dumps(report, default=str).encode()
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
    os.makedirs(DATA_DIR, exist_ok=True)
    threading.Thread(target=refresh_loop, daemon=True).start()
    if RUN_TRADER:
        mode = "LIVE (real orders)" if config.live_trading_enabled() else "VALIDATE (no orders placed)"
        print(f"Starting the trader in this service, {mode}.", flush=True)
        threading.Thread(target=trader_loop, daemon=True).start()
    port = int(os.environ.get("PORT", "8080"))
    ThreadingHTTPServer(("0.0.0.0", port), Handler).serve_forever()


if __name__ == "__main__":
    main()
