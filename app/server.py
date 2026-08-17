"""Local web server. Stdlib only - no Flask/FastAPI dependency.

Routes
  GET  /                 dashboard (public/index.html)
  GET  /api/posts        all parsed posts + summary
  GET  /api/state        sync state only
  POST /api/sync         trigger a sync now
  GET  /files/<path>     downloaded EA attachment
"""
from __future__ import annotations

import asyncio
import json
import mimetypes
import threading
import time
from http.server import HTTPServer, SimpleHTTPRequestHandler
from urllib.parse import parse_qs, unquote, urlparse

from . import config
from .grouping import group_products
from .shots import attach_shots
from .store import load_posts, load_state

_sync_lock = threading.Lock()
_sync_status = {
    "running": False,
    "last_result": None,
    "last_error": None,
    "started_at": None,
    "finished_at": None,
    "next_at": None,
    "interval_min": config.SYNC_INTERVAL_MIN,
    "shots_at": None,        # last test-index refresh
    "shots_result": None,
    "shots_error": None,
}


def _now_iso() -> str:
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).isoformat()


def _schedule_next() -> None:
    from datetime import datetime, timedelta, timezone

    if config.SYNC_INTERVAL_MIN > 0:
        nxt = datetime.now(timezone.utc) + timedelta(minutes=config.SYNC_INTERVAL_MIN)
        _sync_status["next_at"] = nxt.isoformat()


def do_sync_blocking() -> dict:
    """Run one sync in this thread's own event loop. Skips if another sync holds the lock."""
    if not _sync_lock.acquire(blocking=False):
        return {"skipped": "a sync is already running"}
    _sync_status["running"] = True
    _sync_status["started_at"] = _now_iso()
    try:
        from .sync import run_sync

        result = asyncio.run(run_sync())
        _sync_status["last_result"] = result
        _sync_status["last_error"] = None
        return result
    except BaseException as exc:  # noqa: BLE001 - surface SystemExit text to the UI too
        _sync_status["last_error"] = str(exc)
        return {"error": str(exc)}
    finally:
        _sync_status["running"] = False
        _sync_status["finished_at"] = _now_iso()
        _schedule_next()
        _sync_lock.release()


def do_shots_blocking() -> dict:
    """Refresh the ROBOTEST verdict/Myfxbook index. Shares the sync lock so the two
    never touch data/ at the same time."""
    if not _sync_lock.acquire(blocking=False):
        return {"skipped": "a sync is already running"}
    try:
        from .sync_shots import run_shots

        result = asyncio.run(run_shots())
        _sync_status["shots_result"] = result
        _sync_status["shots_error"] = None
        _sync_status["shots_at"] = _now_iso()
        return result
    except BaseException as exc:  # noqa: BLE001 - surface SystemExit text too
        _sync_status["shots_error"] = str(exc)
        return {"error": str(exc)}
    finally:
        _sync_lock.release()


def _shots_stale_minutes() -> float:
    """Minutes since the newest test post in the index (not when we last looked)."""
    from datetime import datetime, timezone

    at = _sync_status.get("shots_at")
    if not at:
        return float("inf")
    try:
        then = datetime.fromisoformat(at)
    except ValueError:
        return float("inf")
    return (datetime.now(timezone.utc) - then).total_seconds() / 60


def _stale_minutes() -> float:
    """Minutes since the last successful sync, per data/state.json."""
    from datetime import datetime, timezone

    last = (load_state() or {}).get("last_sync")
    if not last:
        return float("inf")
    try:
        then = datetime.fromisoformat(last)
    except ValueError:
        return float("inf")
    if then.tzinfo is None:
        then = then.replace(tzinfo=timezone.utc)
    return (datetime.now(timezone.utc) - then).total_seconds() / 60


def _auto_sync_loop() -> None:
    """Sync on start when the store is stale, then on the configured interval.

    There is no manual trigger in the UI, so this loop is the only thing keeping
    the store current - it must catch up after the machine was off.
    """
    minutes = config.SYNC_INTERVAL_MIN
    if minutes <= 0:
        return
    stale = _stale_minutes()
    if stale >= minutes:
        age = "never synced" if stale == float("inf") else f"{stale / 60:.1f}h old"
        print(f"[auto-sync] store is {age} - catching up now")
        do_sync_blocking()
        _refresh_shots("startup")
    else:
        _schedule_next()

    # Tests run for weeks, so the verdict index needs refreshing far less often
    # than the drop list - every Nth cycle is enough.
    cycle = 0
    while True:
        time.sleep(minutes * 60)
        cycle += 1
        print(f"[auto-sync] {minutes} min elapsed - starting", flush=True)
        do_sync_blocking()
        if config.SHOTS_EVERY_N_SYNCS > 0 and cycle % config.SHOTS_EVERY_N_SYNCS == 0:
            _refresh_shots(f"cycle {cycle}")


def _refresh_shots(why: str) -> None:
    print(f"[auto-sync] refreshing test verdicts ({why})", flush=True)
    res = do_shots_blocking()
    if res.get("error"):
        print(f"[auto-sync] verdict refresh failed: {res['error']}", flush=True)
    elif res.get("skipped"):
        print(f"[auto-sync] verdict refresh skipped: {res['skipped']}", flush=True)
    else:
        print(f"[auto-sync] verdicts: {res.get('total')} tests, +{res.get('added')} new", flush=True)


def dedupe(posts: list[dict]) -> list[dict]:
    """One entry per unique EA. Newest post wins; older reposts are counted on it.

    Posts without a dedupe key (non-EA, or no usable name) are always kept as-is.
    Input must be newest-first.
    """
    out: list[dict] = []
    seen: dict[str, dict] = {}
    for p in posts:
        key = p.get("dedupe_key") or ""
        if not key:
            out.append(p)
            continue
        first = seen.get(key)
        if first is None:
            copy = dict(p)
            copy["reposts"] = 0
            copy["repost_dates"] = []
            seen[key] = copy
            out.append(copy)
            continue
        first["reposts"] += 1
        if len(first["repost_dates"]) < 12:
            first["repost_dates"].append(p.get("date_iso", "")[:10])
        # A repost may carry the attachment the first one lacked.
        if not first.get("files") and p.get("files"):
            first["files"] = p["files"]
    return out


def summarize(posts: list[dict]) -> dict:
    pairs: dict[str, int] = {}
    tfs: dict[str, int] = {}
    tags: dict[str, int] = {}
    for p in posts:
        for x in p.get("pairs", []):
            pairs[x] = pairs.get(x, 0) + 1
        for x in p.get("timeframes", []):
            tfs[x] = tfs.get(x, 0) + 1
        for x in p.get("tags", []):
            tags[x] = tags.get(x, 0) + 1
    top = lambda d: [k for k, _ in sorted(d.items(), key=lambda kv: -kv[1])]  # noqa: E731
    return {
        "total": len(posts),
        "ea_count": sum(1 for p in posts if p.get("is_ea")),
        "with_files": sum(1 for p in posts if p.get("files")),
        "pairs": top(pairs),
        "timeframes": top(tfs),
        "tags": top(tags),
        "channels": sorted({p.get("channel_title") or p.get("channel", "") for p in posts}),
    }


class Handler(SimpleHTTPRequestHandler):
    def __init__(self, *a, **kw):
        super().__init__(*a, directory=str(config.PUBLIC_DIR), **kw)

    def log_message(self, fmt, *args):  # quieter console
        if "/api/" in (self.path or ""):
            super().log_message(fmt, *args)

    # -- helpers ----------------------------------------------------------
    def _json(self, payload, status: int = 200) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _serve_file(self, rel: str) -> None:
        target = (config.FILES_DIR / unquote(rel)).resolve()
        try:
            target.relative_to(config.FILES_DIR.resolve())  # block path traversal
        except ValueError:
            self.send_error(403, "forbidden")
            return
        if not target.is_file():
            self.send_error(404, "not found")
            return
        ctype = mimetypes.guess_type(target.name)[0] or "application/octet-stream"
        data = target.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Content-Disposition", f'attachment; filename="{target.name}"')
        self.end_headers()
        self.wfile.write(data)

    # -- routes -----------------------------------------------------------
    def do_GET(self):  # noqa: N802
        path = urlparse(self.path).path

        if path == "/api/posts":
            qs = parse_qs(urlparse(self.path).query)
            ea_only = qs.get("ea", ["1"])[0] not in {"0", "false", "no"}
            grouped = qs.get("group", ["1"])[0] not in {"0", "false", "no"}
            tag = (qs.get("tag", [""])[0] or "").strip()
            try:
                limit = max(0, int(qs.get("limit", ["3000"])[0]))
            except ValueError:
                limit = 3000

            # kind=ea (default) keeps robots only - indicators and courses are not EAs
            kind = (qs.get("kind", ["ea"])[0] or "ea").strip()

            posts = [p for p in load_posts() if not p.get("excluded")]
            summary = summarize(posts)  # facets always reflect the full store
            selected = [p for p in posts if p.get("is_ea")] if ea_only else posts
            if kind != "all":
                selected = [p for p in selected if (p.get("kind") or "ea") == kind]
            if tag:
                selected = [p for p in selected if tag in (p.get("tags") or [])]

            collapsed = 0
            if grouped:
                before = len(selected)
                selected = group_products(selected)
                attach_shots(selected)
                collapsed = before - len(selected)
            matched = len(selected)
            if limit:
                selected = selected[:limit]  # store is already newest-first

            self._json(
                {
                    "posts": selected,
                    "matched": matched,
                    "collapsed": collapsed,
                    "truncated": matched > len(selected),
                    "ea_only": ea_only,
                    "grouped": grouped,
                    "kind": kind,
                    "tag": tag,
                    "summary": summary,
                    "state": load_state(),
                    "sync": _sync_status,
                }
            )
            return
        if path == "/api/state":
            self._json({"state": load_state(), "sync": _sync_status})
            return
        if path.startswith("/files/"):
            self._serve_file(path[len("/files/") :])
            return
        if path == "/":
            self.path = "/index.html"
        super().do_GET()

    def do_POST(self):  # noqa: N802
        if urlparse(self.path).path != "/api/sync":
            self.send_error(404, "not found")
            return
        result = do_sync_blocking()
        posts = load_posts()
        self._json({"result": result, "summary": summarize(posts), "state": load_state()})


def main() -> None:
    threading.Thread(target=_auto_sync_loop, daemon=True).start()
    server = HTTPServer(("127.0.0.1", config.PORT), Handler)
    posts = load_posts()
    print(f"fxea-radar on http://127.0.0.1:{config.PORT}")
    print(f"  {len(posts)} posts in store, {sum(1 for p in posts if p.get('is_ea'))} tagged as EA drops")
    if config.SYNC_INTERVAL_MIN > 0:
        print(f"  auto-sync every {config.SYNC_INTERVAL_MIN} min (SYNC_INTERVAL_MIN=0 to disable)")
    print("  Ctrl+C to stop")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nbye")


if __name__ == "__main__":
    main()
