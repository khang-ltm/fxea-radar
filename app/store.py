"""Gzipped-JSON store. A few thousand posts, no DB dependency.

Gzip because the text compresses ~13x (4.5 MB -> 0.33 MB), which matters for the
Actions cache and for how long a sync takes to read/write. Plain .json files from
older versions are still read, then replaced by the .gz on the next save.
"""
from __future__ import annotations

import gzip
import json
import os
from pathlib import Path

from . import config


def _gz(path: Path) -> Path:
    return path.with_suffix(path.suffix + ".gz")


def _read(path: Path, fallback):
    for candidate in (_gz(path), path):          # prefer .gz, fall back to legacy .json
        try:
            if candidate.suffix == ".gz":
                with gzip.open(candidate, "rt", encoding="utf-8") as fh:
                    return json.load(fh)
            return json.loads(candidate.read_text(encoding="utf-8"))
        except FileNotFoundError:
            continue
        except (OSError, json.JSONDecodeError, EOFError):
            continue
    return fallback


def _write(path: Path, data) -> None:
    """Atomic: write a temp file, then replace. A killed sync never truncates the store."""
    target = _gz(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_suffix(".tmp")
    payload = json.dumps(data, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    with gzip.open(tmp, "wb", compresslevel=6) as fh:
        fh.write(payload)
    os.replace(tmp, target)
    if path.exists():
        path.unlink()                            # drop the superseded plain file


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
