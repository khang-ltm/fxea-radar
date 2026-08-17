"""Read-only MT5 monitor agent. Runs ON the VPS, beside the running terminal.

SAFETY - this module is deliberately incapable of touching your trading:
  * no order_send, no order_check, no position close, no SL/TP edit anywhere
  * never launches a terminal: MetaTrader5.initialize() is called WITHOUT a path,
    and only after a terminal64 process is confirmed running, so it can never
    start a second instance or take over a fresh one
  * never calls anything that closes or restarts the terminal; mt5.shutdown()
    only drops this process's IPC pipe, and is called just once at exit
  * the IPC connection attaches to the already-logged-in terminal, so no broker
    password is needed or stored

    python -m app.mt5_agent                 # localhost only, no token needed
    MT5_TOKEN=<secret> python -m app.mt5_agent --host 0.0.0.0

With MT5_TOKEN set, every /api/ request must carry `Authorization: Bearer <token>`.
Without a token the agent refuses to bind anything except 127.0.0.1, so it cannot
be exposed by accident.
"""
from __future__ import annotations

import argparse
import json
import os
import time
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import urlparse

from . import config

POLL_CACHE_SECONDS = 2          # collapse rapid refreshes into one IPC read
_cache: dict = {"at": 0.0, "data": None}
_mt5 = None
_init_error: str | None = None


def _terminal_running() -> bool:
    """True if an MT5 terminal process exists. Guard against initialize() starting one."""
    import subprocess

    try:
        out = subprocess.run(
            ["tasklist", "/FI", "IMAGENAME eq terminal64.exe", "/NH"],
            capture_output=True, text=True, timeout=15,
        ).stdout.lower()
        return "terminal64.exe" in out
    except Exception:  # noqa: BLE001 - not on Windows, or tasklist missing
        return False


def _connect():
    """Attach to the running terminal. Returns the module, or None with _init_error set."""
    global _mt5, _init_error
    if _mt5 is not None:
        return _mt5
    try:
        import MetaTrader5 as mt5  # noqa: N813 - vendor's own casing
    except ImportError:
        _init_error = ("MetaTrader5 package not installed. On the VPS: "
                       "pip install MetaTrader5  (Windows only)")
        return None
    if not _terminal_running():
        _init_error = ("No terminal64.exe process found. Start MetaTrader 5 and log in first - "
                       "this agent will not launch one for you.")
        return None
    # No path= argument on purpose: with a path, initialize() may START a terminal.
    if not mt5.initialize():
        _init_error = f"initialize() failed: {mt5.last_error()}"
        return None
    _mt5 = mt5
    _init_error = None
    return _mt5


def _as_dict(obj) -> dict:
    return {k: v for k, v in (obj._asdict().items() if hasattr(obj, "_asdict") else vars(obj).items())}


def _iso(ts) -> str | None:
    if not ts:
        return None
    return datetime.fromtimestamp(int(ts), tz=timezone.utc).isoformat()


POSITION_TYPE = {0: "buy", 1: "sell"}
ORDER_TYPE = {0: "buy", 1: "sell", 2: "buy limit", 3: "sell limit", 4: "buy stop", 5: "sell stop"}


def read_state() -> dict:
    """One read-only snapshot: account, positions, pending orders, per-EA rollup."""
    now = time.time()
    if _cache["data"] is not None and now - _cache["at"] < POLL_CACHE_SECONDS:
        return _cache["data"]

    mt5 = _connect()
    if mt5 is None:
        return {"ok": False, "error": _init_error, "at": datetime.now(timezone.utc).isoformat()}

    acc = mt5.account_info()
    term = mt5.terminal_info()
    positions = mt5.positions_get() or ()
    orders = mt5.orders_get() or ()

    pos = []
    for p in positions:
        d = _as_dict(p)
        pos.append({
            "ticket": d.get("ticket"),
            "symbol": d.get("symbol"),
            "type": POSITION_TYPE.get(d.get("type"), str(d.get("type"))),
            "volume": d.get("volume"),
            "price_open": d.get("price_open"),
            "price_current": d.get("price_current"),
            "sl": d.get("sl"),
            "tp": d.get("tp"),
            "profit": d.get("profit"),
            "swap": d.get("swap"),
            "magic": d.get("magic"),
            "comment": d.get("comment"),
            "opened_at": _iso(d.get("time")),
        })

    pend = []
    for o in orders:
        d = _as_dict(o)
        pend.append({
            "ticket": d.get("ticket"),
            "symbol": d.get("symbol"),
            "type": ORDER_TYPE.get(d.get("type"), str(d.get("type"))),
            "volume": d.get("volume_current"),
            "price_open": d.get("price_open"),
            "sl": d.get("sl"),
            "tp": d.get("tp"),
            "magic": d.get("magic"),
            "comment": d.get("comment"),
            "placed_at": _iso(d.get("time_setup")),
        })

    # magic number is how an EA labels its own trades - the only reliable per-EA key
    by_ea: dict[str, dict] = {}
    for p in pos:
        key = str(p["magic"] or 0)
        e = by_ea.setdefault(key, {
            "magic": p["magic"] or 0, "positions": 0, "volume": 0.0, "profit": 0.0,
            "symbols": [], "comments": [],
        })
        e["positions"] += 1
        e["volume"] += p["volume"] or 0
        e["profit"] += p["profit"] or 0
        if p["symbol"] not in e["symbols"]:
            e["symbols"].append(p["symbol"])
        if p["comment"] and p["comment"] not in e["comments"]:
            e["comments"].append(p["comment"])
    for e in by_ea.values():
        e["volume"] = round(e["volume"], 2)
        e["profit"] = round(e["profit"], 2)

    a = _as_dict(acc) if acc else {}
    t = _as_dict(term) if term else {}
    data = {
        "ok": True,
        "at": datetime.now(timezone.utc).isoformat(),
        "read_only": True,
        "account": {
            "login": a.get("login"),
            "name": a.get("name"),
            "server": a.get("server"),
            "currency": a.get("currency"),
            "leverage": a.get("leverage"),
            "balance": a.get("balance"),
            "equity": a.get("equity"),
            "margin": a.get("margin"),
            "margin_free": a.get("margin_free"),
            "margin_level": a.get("margin_level"),
            "profit": a.get("profit"),
            "trade_allowed": a.get("trade_allowed"),
            "is_demo": (a.get("trade_mode") == 0) if a.get("trade_mode") is not None else None,
        },
        "terminal": {
            "connected": t.get("connected"),
            "trade_allowed": t.get("trade_allowed"),   # the AutoTrading button
            "build": t.get("build"),
            "company": t.get("company"),
            "path": t.get("path"),
        },
        "positions": pos,
        "orders": pend,
        "by_ea": sorted(by_ea.values(), key=lambda e: e["profit"]),
        "totals": {
            "positions": len(pos),
            "orders": len(pend),
            "volume": round(sum(p["volume"] or 0 for p in pos), 2),
            "profit": round(sum(p["profit"] or 0 for p in pos), 2),
        },
    }
    _cache.update(at=now, data=data)
    return data


class Handler(BaseHTTPRequestHandler):
    server_version = "fxea-mt5-agent"
    token = ""

    def log_message(self, fmt, *args):
        if "/api/" in (self.path or ""):
            super().log_message(fmt, *args)

    def _json(self, payload, status=200):
        body = json.dumps(payload, ensure_ascii=False, default=str).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _authorized(self) -> bool:
        if not self.token:
            return True                       # localhost-only mode
        sent = (self.headers.get("Authorization") or "").strip()
        return sent == f"Bearer {self.token}"

    def do_GET(self):  # noqa: N802
        path = urlparse(self.path).path
        if path in ("/api/mt5", "/api/state"):
            if not self._authorized():
                self._json({"ok": False, "error": "unauthorized"}, 401)
                return
            self._json(read_state())
            return
        if path == "/api/health":
            self._json({"ok": True, "agent": "read-only", "terminal_running": _terminal_running()})
            return
        if path in ("/", "/index.html", "/mt5.html"):
            page = config.PUBLIC_DIR / "mt5.html"
            if not page.exists():
                self._json({"ok": False, "error": "mt5.html missing"}, 404)
                return
            body = page.read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        self._json({"ok": False, "error": "not found"}, 404)

    def do_POST(self):  # noqa: N802
        # Phase 1 is a monitor. Nothing here can place, modify or close a trade.
        self._json({"ok": False, "error": "this agent is read-only; no commands are accepted"}, 405)


def main() -> None:
    ap = argparse.ArgumentParser(description="Read-only MT5 monitor")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8788)
    args = ap.parse_args()

    token = (os.environ.get("MT5_TOKEN") or "").strip()
    host = args.host
    if host != "127.0.0.1" and not token:
        raise SystemExit(
            "Refusing to bind %s without MT5_TOKEN set - that would expose your account "
            "data to the network. Set MT5_TOKEN, or leave the default localhost bind "
            "and reach it through a tunnel." % host
        )
    Handler.token = token

    print(f"MT5 monitor (READ-ONLY) on http://{host}:{args.port}")
    print(f"  auth: {'Bearer token required' if token else 'none (localhost only)'}")
    state = read_state()
    if state.get("ok"):
        acc = state["account"]
        print(f"  attached to {acc['login']} @ {acc['server']} | equity {acc['equity']} {acc['currency']}"
              f" | {state['totals']['positions']} positions")
    else:
        print(f"  terminal not readable yet: {state.get('error')}")
        print("  (the agent keeps serving and will attach as soon as the terminal is up)")

    try:
        HTTPServer((host, args.port), Handler).serve_forever()
    except KeyboardInterrupt:
        print("\nstopping agent (the terminal is untouched)")
    finally:
        if _mt5 is not None:
            _mt5.shutdown()   # closes this process's pipe only, not the terminal


if __name__ == "__main__":
    main()
