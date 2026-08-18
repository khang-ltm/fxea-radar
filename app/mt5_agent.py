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
import hashlib
import json
import os
import pathlib
import re
import threading
import time
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

from . import config

# The MetaTrader5 package is NOT thread-safe: two threads calling into the
# terminal at once can stall for a minute or more. Every IPC read goes through
# this lock, which is also why the cache below matters - it keeps the lock free.
_ipc_lock = threading.Lock()

POLL_CACHE_SECONDS = 1          # collapse rapid refreshes into one IPC read
STREAM_TICK_SECONDS = 1         # how often the stream re-reads the terminal
STREAM_HEARTBEAT_SECONDS = 20   # keeps proxies from dropping an idle connection
STREAM_MAX_SECONDS = 3600       # close after an hour; the browser reconnects
_cache: dict = {"at": 0.0, "data": None}
HISTORY_CACHE_SECONDS = 30      # closed trades change rarely; keep the IPC lock free
_hist_cache: dict = {"at": 0.0, "days": 0, "data": None}
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


# Origins allowed to read with credentials. "*" cannot be used once cookies are
# involved, so the site origin is named explicitly.
ALLOWED_ORIGINS = [
    o.strip().rstrip("/")
    for o in (os.environ.get("MT5_ALLOWED_ORIGINS")
              or "https://fxea-radar.linkpc.net,http://127.0.0.1:8787,http://localhost:8787").split(",")
    if o.strip()
]
COOKIE_NAME = "mt5auth"
COOKIE_MAX_AGE = 60 * 60 * 24 * 365      # a year: log in once per device


def _cookie_value(token: str) -> str:
    """Opaque, derived from the token: rotating the token invalidates every cookie."""
    import hmac
    return hmac.new(token.encode(), b"fxea-mt5-cookie-v1", hashlib.sha256).hexdigest()


_digits_cache: dict[str, int] = {}


def _digits(mt5, symbol: str) -> int:
    """Price precision for a symbol, cached. Raw floats arrive as 1.6069200000000001,
    so values are rounded to the instrument's own digits before leaving the agent."""
    if symbol not in _digits_cache:
        try:
            info = mt5.symbol_info(symbol)
            _digits_cache[symbol] = int(getattr(info, "digits", 5)) if info else 5
        except Exception:  # noqa: BLE001 - unknown symbol must not break the read
            _digits_cache[symbol] = 5
    return _digits_cache[symbol]


def _px(value, digits: int):
    return None if value is None else round(float(value), digits)


POSITION_TYPE = {0: "buy", 1: "sell"}
ORDER_TYPE = {0: "buy", 1: "sell", 2: "buy limit", 3: "sell limit", 4: "buy stop", 5: "sell stop"}


def read_state() -> dict:
    """One read-only snapshot: account, positions, pending orders, per-EA rollup.

    Serialised on _ipc_lock: concurrent calls into the terminal (SSE thread plus a
    fallback poll) used to hang the bridge, which showed up in the UI as
    "no data for 84s" right after placing an order.
    """
    now = time.time()
    if _cache["data"] is not None and now - _cache["at"] < POLL_CACHE_SECONDS:
        return _cache["data"]

    with _ipc_lock:
        # another thread may have refreshed while we waited for the lock
        now = time.time()
        if _cache["data"] is not None and now - _cache["at"] < POLL_CACHE_SECONDS:
            return _cache["data"]
        try:
            return _read_state_locked(now)
        except Exception as exc:  # noqa: BLE001 - a broken pipe must not kill the agent
            global _mt5, _init_error
            _mt5 = None                                  # force a clean re-attach
            _init_error = f"terminal read failed: {exc}"
            return {"ok": False, "error": _init_error, "at": datetime.now(timezone.utc).isoformat()}


def _read_state_locked(now: float) -> dict:
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
        dg = _digits(mt5, d.get("symbol", ""))
        pos.append({
            "ticket": d.get("ticket"),
            "symbol": d.get("symbol"),
            "digits": dg,
            "type": POSITION_TYPE.get(d.get("type"), str(d.get("type"))),
            "volume": round(float(d.get("volume") or 0), 2),
            "price_open": _px(d.get("price_open"), dg),
            "price_current": _px(d.get("price_current"), dg),
            "sl": _px(d.get("sl"), dg),
            "tp": _px(d.get("tp"), dg),
            "profit": _px(d.get("profit"), 2),
            "swap": _px(d.get("swap"), 2),
            "magic": d.get("magic"),
            "comment": d.get("comment"),
            "opened_at": _iso(d.get("time")),
        })

    pend = []
    for o in orders:
        d = _as_dict(o)
        dg = _digits(mt5, d.get("symbol", ""))
        pend.append({
            "ticket": d.get("ticket"),
            "symbol": d.get("symbol"),
            "digits": dg,
            "type": ORDER_TYPE.get(d.get("type"), str(d.get("type"))),
            "volume": round(float(d.get("volume_current") or 0), 2),
            "price_open": _px(d.get("price_open"), dg),
            "sl": _px(d.get("sl"), dg),
            "tp": _px(d.get("tp"), dg),
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


DEAL_ENTRY_OUT = (1, 3)          # OUT and OUT_BY: the leg that realises a result


def read_history(days: int = 30, tz_minutes: int = 0) -> dict:
    """Realised results from closed deals, grouped per EA.

    Read-only like everything else: history_deals_get only reports what already
    happened. Times are UTC; a broker on another server time can shift day
    boundaries slightly, which matters for "today" but not for the totals.
    """
    now = time.time()
    key = (days, tz_minutes)
    if (_hist_cache["data"] is not None and _hist_cache["days"] == key
            and now - _hist_cache["at"] < HISTORY_CACHE_SECONDS):
        return _hist_cache["data"]

    with _ipc_lock:
        mt5 = _connect()
        if mt5 is None:
            return {"ok": False, "error": _init_error, "at": datetime.now(timezone.utc).isoformat()}
        try:
            until = datetime.now(timezone.utc) + timedelta(days=1)
            since = datetime.now(timezone.utc) - timedelta(days=days)
            deals = mt5.history_deals_get(since, until) or ()
        except Exception as exc:  # noqa: BLE001 - never kill the agent over history
            return {"ok": False, "error": f"history read failed: {exc}",
                    "at": datetime.now(timezone.utc).isoformat()}

    by_ea: dict[int, dict] = {}
    by_day: dict[str, float] = {}
    closed: list[dict] = []
    # day boundaries follow the viewer's clock: with a UTC day, "today" was wrong
    # for the first seven hours of every morning in UTC+7
    local = timezone(timedelta(minutes=tz_minutes))
    today = datetime.now(local).date()
    seen_positions: dict[int, dict] = {}

    # An EA stamps its magic on the deal that OPENS a position; the closing deal
    # frequently carries 0, which made real EA trades look manual. Build the
    # position -> magic map first, then attribute each close to its opener.
    magic_of_position: dict[int, int] = {}
    comment_of_position: dict[int, str] = {}
    for dl in deals:
        d = _as_dict(dl)
        pid = int(d.get("position_id") or 0)
        if not pid:
            continue
        mg = int(d.get("magic") or 0)
        if mg and pid not in magic_of_position:
            magic_of_position[pid] = mg
        # The EA's own comment sits on the opening deal; the closing deal carries
        # the broker's close reason instead, e.g. "[sl 4402.25]", which is what
        # MetaTrader shows in a separate column - not as the trade comment.
        if int(d.get("entry") or 0) == 0:
            cm = (d.get("comment") or "").strip()
            if cm and pid not in comment_of_position:
                comment_of_position[pid] = cm

    for dl in deals:
        d = _as_dict(dl)
        # types 0/1 are buy/sell; 2+ are balance, credit, bonus and similar
        # non-trades that would otherwise pollute every total
        if int(d.get("type") or 0) > 1:
            continue
        if d.get("entry") not in DEAL_ENTRY_OUT:
            continue                                    # skip the opening leg
        # a trade's true result is profit plus its costs
        net = round(float(d.get("profit") or 0) + float(d.get("swap") or 0)
                    + float(d.get("commission") or 0) + float(d.get("fee") or 0), 2)
        when = datetime.fromtimestamp(int(d.get("time") or 0), tz=timezone.utc)
        magic = int(d.get("magic") or 0) or magic_of_position.get(int(d.get("position_id") or 0), 0)

        e = by_ea.setdefault(magic, {"magic": magic, "trades": 0, "wins": 0, "losses": 0,
                                     "profit": 0.0, "symbols": [], "comments": []})
        # a position closed in chunks yields several OUT deals; count the position
        # once and accumulate its parts, or trade counts and win rate drift from MT5
        first_leg = pid not in seen_positions
        if first_leg:
            seen_positions[pid] = {"net": 0.0, "ea": e}
            e["trades"] += 1
        seen_positions[pid]["net"] = round(seen_positions[pid]["net"] + net, 2)
        e["profit"] = round(e["profit"] + net, 2)
        sym = d.get("symbol")
        if sym and sym not in e["symbols"]:
            e["symbols"].append(sym)
        pid = int(d.get("position_id") or 0)
        cm = comment_of_position.get(pid) or (d.get("comment") or "").strip()
        close_reason = (d.get("comment") or "").strip()
        if cm and cm not in e["comments"]:
            e["comments"].append(cm)

        day = when.astimezone(local).date().isoformat()
        by_day[day] = round(by_day.get(day, 0.0) + net, 2)
        closed.append({
            "ticket": d.get("position_id") or d.get("ticket"),
            "symbol": sym,
            "type": POSITION_TYPE.get(d.get("type"), str(d.get("type"))),
            "volume": round(float(d.get("volume") or 0), 2),
            "price": d.get("price"),
            "profit": net,
            "magic": magic,
            "comment": cm,
            "close_reason": close_reason if close_reason != cm else "",
            "closed_at": when.isoformat(),
        })

    for agg in seen_positions.values():
        agg["ea"]["wins" if agg["net"] > 0 else "losses"] += 1

    closed.sort(key=lambda c: c["closed_at"], reverse=True)
    wins = sum(e["wins"] for e in by_ea.values())
    trades = sum(e["trades"] for e in by_ea.values())

    def window(n: int) -> float:
        cutoff = today - timedelta(days=n - 1)
        return round(sum(v for k, v in by_day.items()
                         if datetime.fromisoformat(k).date() >= cutoff), 2)

    data = {
        "ok": True,
        "at": datetime.now(timezone.utc).isoformat(),
        "days": days,
        "totals": {
            "trades": trades,
            "wins": wins,
            "losses": trades - wins,
            "win_rate": round(100 * wins / trades, 1) if trades else None,
            "profit": round(sum(e["profit"] for e in by_ea.values()), 2),
            "today": by_day.get(today.isoformat(), 0.0),
            "week": window(7),
            "month": window(30),
        },
        "by_ea": sorted(by_ea.values(), key=lambda e: e["profit"]),
        "by_day": [{"date": k, "profit": v} for k, v in sorted(by_day.items(), reverse=True)][:60],
        "closed": closed[:2000],
    }
    _hist_cache.update(at=now, days=key, data=data)
    return data


# --- manager EA channel -----------------------------------------------------
# FxeaManager.mq5 sits on a spare chart and exchanges files with us inside the
# terminal's MQL5\Files sandbox. Same machine, no network, no token in MQL5.
MANAGER_CMD = "fxea_cmd.txt"
MANAGER_RESULT = "fxea_result.txt"
MANAGER_STATUS = "fxea_status.json"
MANAGER_TIMEOUT = 8            # seconds to wait for the EA to answer
_manager_seq = [0]


def _mql5_files_dir():
    """The terminal's own Files folder; the EA cannot read anywhere else."""
    mt5 = _connect()
    if mt5 is None:
        return None
    term = mt5.terminal_info()
    data_path = getattr(term, "data_path", "") if term else ""
    if not data_path:
        return None
    d = pathlib.Path(data_path) / "MQL5" / "Files"
    return d if d.is_dir() else None


def read_charts() -> dict:
    """Whatever FxeaManager last wrote. Absent file means it is not attached."""
    d = _mql5_files_dir()
    if d is None:
        return {"ok": False, "error": _init_error or "terminal not readable"}
    f = d / MANAGER_STATUS
    if not f.exists():
        return {"ok": False, "attached": False,
                "error": "FxeaManager is not attached to a chart (no status file yet)"}
    try:
        data = json.loads(f.read_text(encoding="utf-8", errors="replace"))
    except (OSError, json.JSONDecodeError) as exc:
        return {"ok": False, "attached": False, "error": f"unreadable status file: {exc}"}
    age = time.time() - f.stat().st_mtime
    data["ok"] = True
    data["attached"] = age < 60          # the EA rewrites it every few seconds
    data["age_seconds"] = round(age, 1)
    return data


MANAGER_INPUTS = "fxea_inputs.txt"
_INPUT_KEY = re.compile(r"^[A-Za-z_][A-Za-z0-9_.]{0,63}$")


def _stage_inputs(pairs) -> object:
    """Write the settings the manager should apply. Returns a message on refusal.

    Values go into the EA's own template, so anything that could break the file
    - newlines, over-long strings, odd keys - is rejected here rather than
    corrupting a template that a live EA is about to reload.
    """
    if not isinstance(pairs, dict) or not pairs:
        return "no settings given"
    if len(pairs) > 100:
        return "too many settings in one request"

    lines = []
    for key, value in pairs.items():
        key = str(key)
        text = "" if value is None else str(value)
        if not _INPUT_KEY.match(key):
            return f"bad setting name: {key}"
        if len(text) > 500 or chr(10) in text or chr(13) in text:
            return f"bad value for {key}"
        lines.append(f"{key}={text}")

    d = _mql5_files_dir()
    if d is None:
        return _init_error or "terminal not readable"
    (d / MANAGER_INPUTS).write_text(chr(10).join(lines) + chr(10),
                                    encoding="ascii", errors="replace")
    return len(lines)


def manager_command(action: str, **fields) -> dict:
    """Drop a command file, wait for the EA to answer, return its verdict."""
    d = _mql5_files_dir()
    if d is None:
        return {"ok": False, "error": _init_error or "terminal not readable"}

    _manager_seq[0] += 1
    cmd_id = f"{int(time.time())}-{_manager_seq[0]}"
    lines_out = [f"id={cmd_id}", f"action={action}"]
    lines_out += [f"{k}={v}" for k, v in fields.items() if v not in (None, "")]

    result = d / MANAGER_RESULT
    if result.exists():
        result.unlink(missing_ok=True)          # ignore a stale answer
    (d / MANAGER_CMD).write_text(chr(10).join(lines_out) + chr(10),
                                 encoding="ascii", errors="replace")

    deadline = time.time() + MANAGER_TIMEOUT
    while time.time() < deadline:
        if result.exists():
            try:
                body = result.read_text(encoding="utf-8", errors="replace")
            except OSError:
                time.sleep(0.2)
                continue
            got = dict(
                line.split("=", 1) for line in body.splitlines() if "=" in line
            )
            if got.get("id") == cmd_id:
                out = {"ok": got.get("ok") == "1", "message": got.get("message", ""),
                       "id": cmd_id, "action": action}
                _audit(out, fields)
                return out
        time.sleep(0.2)

    out = {"ok": False, "error": "FxeaManager did not answer in time - is it attached?",
           "id": cmd_id, "action": action}
    _audit(out, fields)
    return out


def _audit(result: dict, fields: dict) -> None:
    """Every command is recorded: this is the only path that changes anything."""
    try:
        line = json.dumps({"at": datetime.now(timezone.utc).isoformat(),
                           **result, "fields": fields}, ensure_ascii=False)
        with open(config.DATA_DIR / "manager.log", "a", encoding="utf-8") as fh:
            fh.write(line + chr(10))
    except OSError:
        pass


class Handler(BaseHTTPRequestHandler):
    server_version = "fxea-mt5-agent"
    token = ""

    def log_message(self, fmt, *args):
        if "/api/" in (self.path or ""):
            super().log_message(fmt, *args)

    def _cors(self):
        # The dashboard is served from another origin (GitHub Pages), so it needs
        # CORS to read this. With cookies in play the wildcard is not allowed, so
        # only a known origin is reflected - which is also tighter than before.
        origin = (self.headers.get("Origin") or "").rstrip("/")
        if origin in ALLOWED_ORIGINS:
            self.send_header("Access-Control-Allow-Origin", origin)
            self.send_header("Access-Control-Allow-Credentials", "true")
            self.send_header("Vary", "Origin")
        else:
            self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Headers", "Authorization, Content-Type")
        self.send_header("Access-Control-Allow-Methods", "GET, OPTIONS")
        self.send_header("Access-Control-Max-Age", "600")

    def _json(self, payload, status=200):
        body = json.dumps(payload, ensure_ascii=False, default=str).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self._cors()
        self._maybe_set_cookie()
        self.end_headers()
        self.wfile.write(body)

    def _maybe_set_cookie(self):
        """After a successful token auth, hand the browser a year-long cookie so the
        token never has to be entered again on that device."""
        if not getattr(self, "_set_cookie", False) or not self.token:
            return
        self.send_header(
            "Set-Cookie",
            f"{COOKIE_NAME}={_cookie_value(self.token)}; Max-Age={COOKIE_MAX_AGE}; "
            "Path=/; Secure; HttpOnly; SameSite=None",
        )

    def do_OPTIONS(self):  # noqa: N802 - preflight for the Authorization header
        self.send_response(204)
        self._cors()
        self.send_header("Content-Length", "0")
        self.end_headers()

    def _authorized(self) -> bool:
        if not self.token:
            return True                       # localhost-only mode
        if (self.headers.get("Authorization") or "").strip() == f"Bearer {self.token}":
            self._set_cookie = True           # remember this device
            return True
        qs = parse_qs(urlparse(self.path).query)
        if qs.get("token", [""])[0] == self.token:
            self._set_cookie = True
            return True
        cookie = (self.headers.get("Cookie") or "")
        want = f"{COOKIE_NAME}={_cookie_value(self.token)}"
        return any(c.strip() == want for c in cookie.split(";"))

    def _stream(self):
        """Server-Sent Events: push a snapshot only when something changed.

        Cheaper than polling and closer to real time - one connection instead of a
        request every few seconds, and silence while nothing moves.
        """
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-cache, no-transform")
        self.send_header("Connection", "keep-alive")
        self.send_header("X-Accel-Buffering", "no")     # ask proxies not to buffer
        self._cors()
        self.end_headers()

        started = time.time()
        last_sig = None
        last_sent = 0.0
        try:
            while time.time() - started < STREAM_MAX_SECONDS:
                state = read_state()
                # signature over the fields worth waking the UI for
                sig = hashlib.sha1(json.dumps({
                    "a": state.get("account"),
                    "p": state.get("positions"),
                    "o": state.get("orders"),
                    "t": state.get("terminal"),
                    "e": state.get("error"),
                }, sort_keys=True, default=str).encode()).hexdigest()

                now = time.time()
                if sig != last_sig:
                    payload = json.dumps(state, ensure_ascii=False, default=str)
                    self.wfile.write(f"event: state\ndata: {payload}\n\n".encode("utf-8"))
                    self.wfile.flush()
                    last_sig, last_sent = sig, now
                elif now - last_sent >= STREAM_HEARTBEAT_SECONDS:
                    self.wfile.write(b": ping\n\n")     # comment frame, ignored by clients
                    self.wfile.flush()
                    last_sent = now
                time.sleep(STREAM_TICK_SECONDS)
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
            pass                                        # client closed the tab; normal

    def do_GET(self):  # noqa: N802
        path = urlparse(self.path).path
        if path == "/api/stream":
            # EventSource cannot set headers, so a token in the query is accepted
            # here as well as the Authorization header.
            qs = parse_qs(urlparse(self.path).query)
            if self.token and not (self._authorized() or qs.get("token", [""])[0] == self.token):
                self._json({"ok": False, "error": "unauthorized"}, 401)
                return
            self._stream()
            return
        if path == "/api/charts":
            if not self._authorized():
                self._json({"ok": False, "error": "unauthorized"}, 401)
                return
            self._json(read_charts())
            return
        if path == "/api/history":
            if not self._authorized():
                self._json({"ok": False, "error": "unauthorized"}, 401)
                return
            qs = parse_qs(urlparse(self.path).query)
            try:
                days = max(1, min(365, int(qs.get("days", ["30"])[0])))
            except ValueError:
                days = 30
            try:
                tzmin = max(-840, min(840, int(qs.get("tz", ["0"])[0])))
            except ValueError:
                tzmin = 0
            self._json(read_history(days, tzmin))
            return
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
        """Manager commands only. There is still no path here that can place,
        modify or close a TRADE - unloading an EA leaves its positions open."""
        if urlparse(self.path).path != "/api/manager":
            self._json({"ok": False, "error": "no trade endpoints exist on this agent"}, 405)
            return
        if not self._authorized():
            self._json({"ok": False, "error": "unauthorized"}, 401)
            return
        try:
            length = int(self.headers.get("Content-Length") or 0)
            body = json.loads(self.rfile.read(length) or b"{}")
        except (ValueError, json.JSONDecodeError):
            self._json({"ok": False, "error": "bad JSON body"}, 400)
            return

        action = str(body.get("action") or "")
        if action not in ("status", "pause", "run", "resume", "unload", "setinputs"):
            self._json({"ok": False, "error": f"unsupported action: {action}"}, 400)
            return
        if action in ("pause", "unload", "setinputs") and not body.get("confirm"):
            self._json({"ok": False, "error": f"{action} requires confirm: true"}, 400)
            return

        if action == "setinputs":
            written = _stage_inputs(body.get("inputs"))
            if isinstance(written, str):
                self._json({"ok": False, "error": written}, 400)
                return

        self._json(manager_command(
            action,
            chart=body.get("chart"),
            symbol=body.get("symbol"),
            expert=body.get("expert"),
            key=body.get("key"),
            magic=body.get("magic"),
            force=1 if (action == "setinputs" and body.get("force")) else None,
        ))


UPDATE_URL = os.environ.get(
    "MT5_UPDATE_URL", "https://api.github.com/repos/khang-ltm/fxea-radar/commits/main")
ZIP_URL = os.environ.get(
    "MT5_ZIP_URL", "https://codeload.github.com/khang-ltm/fxea-radar/zip/refs/heads/main")
UPDATE_EVERY_MINUTES = int(os.environ.get("MT5_UPDATE_MINUTES", "20") or 20)


def _current_sha() -> str:
    f = config.ROOT / "data" / ".agent_version"
    try:
        return f.read_text(encoding="utf-8").strip()
    except OSError:
        return ""


def _remote_sha() -> str:
    import urllib.request

    req = urllib.request.Request(UPDATE_URL, headers={"User-Agent": "fxea-agent"})
    with urllib.request.urlopen(req, timeout=20) as r:
        return json.load(r).get("sha", "")[:40]


def _apply_update(sha: str) -> bool:
    """Download the repo and refresh app/ and public/ in place. Data is untouched."""
    import io
    import shutil
    import urllib.request
    import zipfile

    req = urllib.request.Request(ZIP_URL, headers={"User-Agent": "fxea-agent"})
    with urllib.request.urlopen(req, timeout=120) as r:
        blob = r.read()
    with zipfile.ZipFile(io.BytesIO(blob)) as z:
        root = z.namelist()[0].split("/")[0]
        staged = config.ROOT / "data" / "_update"
        if staged.exists():
            shutil.rmtree(staged, ignore_errors=True)
        z.extractall(staged)
        src = staged / root
        for folder in ("app", "public"):
            if (src / folder).is_dir():
                shutil.copytree(src / folder, config.ROOT / folder, dirs_exist_ok=True)
        shutil.rmtree(staged, ignore_errors=True)
    (config.ROOT / "data" / ".agent_version").write_text(sha, encoding="utf-8")
    return True


def _self_update_loop() -> None:
    """Pull new code and re-exec, so shipping a fix never needs a manual restart.

    Re-exec replaces this process with a fresh interpreter running the same
    command, which keeps the scheduled task's supervision intact. Any failure is
    swallowed: a broken update check must never take the monitor down.
    """
    if UPDATE_EVERY_MINUTES <= 0:
        return
    import subprocess
    import sys

    while True:
        time.sleep(UPDATE_EVERY_MINUTES * 60)
        try:
            remote = _remote_sha()
            if not remote or remote == _current_sha():
                continue
            print(f"[self-update] new version {remote[:7]} - updating", flush=True)
            _apply_update(remote)
            print("[self-update] restarting agent", flush=True)
            if _mt5 is not None:
                _mt5.shutdown()          # drop our IPC pipe; the terminal keeps running
            os.execv(sys.executable, [sys.executable, "-m", "app.mt5_agent", *sys.argv[1:]])
        except Exception as exc:  # noqa: BLE001 - never let updating kill the monitor
            print(f"[self-update] skipped: {exc}", flush=True)


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

    threading.Thread(target=_self_update_loop, daemon=True).start()
    print(f"  self-update: every {UPDATE_EVERY_MINUTES} min from GitHub"
          if UPDATE_EVERY_MINUTES > 0 else "  self-update: off")

    try:
        # Threading matters: one SSE connection would otherwise block every other
        # request on a single-threaded server.
        ThreadingHTTPServer((host, args.port), Handler).serve_forever()
    except KeyboardInterrupt:
        print("\nstopping agent (the terminal is untouched)")
    finally:
        if _mt5 is not None:
            _mt5.shutdown()   # closes this process's pipe only, not the terminal


if __name__ == "__main__":
    main()
