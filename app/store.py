"""JSON-file store. Small dataset (a few thousand posts), no DB dependency."""
from __future__ import annotations

import json
import os
from pathlib import Path

from . import config


def _read(path: Path, fallback):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return fallback


def _write(path: Path, data) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
    os.replace(tmp, path)


def load_posts() -> list[dict]:
    posts = _read(config.POSTS_FILE, [])
    return posts if isinstance(posts, list) else []


def save_posts(posts: list[dict]) -> None:
    posts.sort(key=lambda p: p.get("date", 0), reverse=True)
    _write(config.POSTS_FILE, posts)


def load_state() -> dict:
    state = _read(config.STATE_FILE, {"channels": {}})
    state.setdefault("channels", {})
    return state


def save_state(state: dict) -> None:
    _write(config.STATE_FILE, state)


def merge_posts(existing: list[dict], incoming: list[dict]) -> tuple[list[dict], int, int]:
    """Key on channel+message id. Edited posts get refreshed but keep their first_seen."""
    by_key = {p["key"]: p for p in existing}
    added = updated = 0
    for p in incoming:
        prev = by_key.get(p["key"])
        if prev is None:
            added += 1
            by_key[p["key"]] = p
        elif prev.get("text") != p.get("text") or len(prev.get("files", [])) != len(p.get("files", [])):
            updated += 1
            merged = {**prev, **p, "first_seen": prev.get("first_seen", p.get("first_seen"))}
            by_key[p["key"]] = merged
    return list(by_key.values()), added, updated
