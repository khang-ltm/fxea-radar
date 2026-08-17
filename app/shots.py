"""Attach a backtest screenshot to each grouped EA.

Why an index instead of the EA post itself: the drop posts in FREE FX CHAT carry
no image, and the channel's "✔️Test completed" photo posts name no EA in their
text and are not replies - so there is nothing in that channel to join on.

The test-result screenshots with names live in the ROBOTEST channel, so
`app.sync_shots` builds data/shots.json from it:

    {"product_key": "quantum queen x", "name": ..., "date_iso": ...,
     "local_path": "...", "url": "https://t.me/...", "version": "v4.3"}

Matching reuses the same family logic as grouping: exact product key first, then
token containment. No index -> every EA simply has no shot, and the UI says so.
"""
from __future__ import annotations

import json

from . import config
from .grouping import _contained, _tokens, squash

SHOTS_FILE = config.DATA_DIR / "shots.json"


def load_shots() -> list[dict]:
    from .store import _read

    data = _read(SHOTS_FILE, [])
    return data if isinstance(data, list) else []


def save_shots(shots: list[dict]) -> None:
    from .store import _write

    _write(SHOTS_FILE, shots)


def _own_photo(group: dict) -> dict | None:
    for ph in group.get("photos") or []:
        if ph.get("local_path"):
            return {"local_path": ph["local_path"], "source": "post", "date_iso": group.get("date_iso", "")[:10]}
    return None


def attach_shots(groups: list[dict], shots: list[dict] | None = None) -> None:
    """Set group['shot'] in place. Newest matching screenshot wins."""
    shots = load_shots() if shots is None else shots
    by_key: dict[str, list[dict]] = {}
    for s in shots:
        # a test counts even with no downloaded image - the verdict and the
        # Myfxbook link are what the UI shows
        if s.get("product_key"):
            by_key.setdefault(s["product_key"], []).append(s)
    # A finished test with a verdict beats a run that is still in progress;
    # otherwise the newest one wins.
    for v in by_key.values():
        v.sort(key=lambda s: (bool(s.get("verdict")), s.get("date_iso", "")), reverse=True)
    token_cache = {k: _tokens(k) for k in by_key}

    for g in groups:
        own = _own_photo(g)
        if own:
            g["shot"] = own
            continue
        fam = g.get("family") or g.get("product_key") or ""
        if not fam:
            continue
        hit = by_key.get(fam)
        if not hit:
            # spacing variant of the same product name
            fam_sq = squash(fam)
            hit = next((v for k, v in by_key.items() if squash(k) == fam_sq), None)
        if not hit:
            ftok = _tokens(fam)
            best = None
            for k, toks in token_cache.items():
                if _contained(ftok, toks):
                    cand = by_key[k][0]
                    if best is None or cand.get("date_iso", "") > best.get("date_iso", ""):
                        best = cand
            hit = [best] if best else None
        if hit:
            s = hit[0]
            g["shot"] = {
                "local_path": s.get("local_path"),
                "myfxbook": s.get("myfxbook"),
                "mystatea": s.get("mystatea"),
                "report": s.get("report"),
                "source": "robotest",
                "source_name": s.get("name"),
                "source_version": s.get("version"),
                # the test may be of a different version than the newest drop
                "same_version": bool(s.get("version")) and s.get("version") == g.get("version"),
                "url": s.get("url"),
                "date_iso": s.get("date_iso", "")[:10],
                # caption fields: the image alone does not say if the test still runs
                "test_until": s.get("test_until"),
                "test_from": s.get("test_from"),
                "test_deposit": s.get("test_deposit"),
                "test_result": s.get("test_result"),
                "completed": bool(s.get("completed")),
                "verdict": s.get("verdict"),          # pass | loss | no-trade | scam | None
                "status": s.get("status") or "running",
                "strategy": s.get("strategy") or [],
            }
