"""Config loading: plain .env parsing, no external deps."""
from __future__ import annotations

import os
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
ENV_FILE = ROOT / ".env"
DATA_DIR = ROOT / "data"
FILES_DIR = DATA_DIR / "files"
POSTS_FILE = DATA_DIR / "posts.json"
STATE_FILE = DATA_DIR / "state.json"
PUBLIC_DIR = ROOT / "public"
SESSION_FILE = DATA_DIR / "tg.session"  # Telethon sqlite session


def _load_env() -> None:
    if not ENV_FILE.exists():
        return
    for line in ENV_FILE.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, val = line.partition("=")
        key, val = key.strip(), val.strip()
        if len(val) >= 2 and val[0] == val[-1] and val[0] in "\"'":
            val = val[1:-1]
        os.environ.setdefault(key, val)


_load_env()


def _bool(name: str, default: bool) -> bool:
    v = os.environ.get(name, "")
    if v == "":
        return default
    return v.strip().lower() in {"1", "true", "yes", "y", "on"}


def _int(name: str, default: int) -> int:
    try:
        return int(str(os.environ.get(name, "")).strip())
    except (TypeError, ValueError):
        return default


def _channels() -> list[str]:
    raw = os.environ.get("TG_CHANNELS", "free_fx_chat")
    out = []
    for part in raw.split(","):
        c = part.strip()
        if not c:
            continue
        c = c.replace("https://t.me/", "").replace("http://t.me/", "").lstrip("@")
        c = c.split("/")[0]
        out.append(c)
    return out


API_ID = _int("TG_API_ID", 0)
API_HASH = os.environ.get("TG_API_HASH", "").strip()
CHANNELS = _channels()
def _exclude() -> list[str]:
    """EXCLUDE_KEYWORDS - products to skip entirely: never listed, never downloaded."""
    raw = os.environ.get("EXCLUDE_KEYWORDS", "")
    return [w.strip().lower() for w in raw.split(",") if w.strip()]


EXCLUDE_KEYWORDS = _exclude()

FIRST_SYNC_LIMIT = _int("FIRST_SYNC_LIMIT", 800)  # 0 = whole channel history
FLOOD_SLEEP_THRESHOLD = _int("FLOOD_SLEEP_THRESHOLD", 600)


def _since_date():
    """SINCE_DATE=YYYY-MM-DD - stop walking back past this date. Empty = no cutoff."""
    raw = (os.environ.get("SINCE_DATE", "") or "").strip()
    if not raw:
        return None
    from datetime import datetime, timezone

    for fmt in ("%Y-%m-%d", "%Y/%m/%d", "%d-%m-%Y"):
        try:
            return datetime.strptime(raw, fmt).replace(tzinfo=timezone.utc)
        except ValueError:
            continue
    raise SystemExit(f"SINCE_DATE={raw!r} is not a date. Use YYYY-MM-DD, e.g. 2026-07-01")


SINCE_DATE = _since_date()
DOWNLOAD_FILES = _bool("DOWNLOAD_FILES", True)
DOWNLOAD_PHOTOS = _bool("DOWNLOAD_PHOTOS", True)  # backtest screenshots
MAX_FILE_MB = _int("MAX_FILE_MB", 25)
PORT = _int("PORT", 8787)
SYNC_INTERVAL_MIN = _int("SYNC_INTERVAL_MIN", 15)
# Refresh the ROBOTEST verdict index every Nth sync cycle (0 = never)
SHOTS_EVERY_N_SYNCS = _int("SHOTS_EVERY_N_SYNCS", 4)

FILES_DIR.mkdir(parents=True, exist_ok=True)


def require_creds() -> None:
    if not API_ID or not API_HASH:
        raise SystemExit(
            "TG_API_ID / TG_API_HASH missing in .env\n"
            "Get them at https://my.telegram.org -> API development tools, "
            f"then fill {ENV_FILE}"
        )
