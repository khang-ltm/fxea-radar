"""Pull messages from the tracked channel(s), parse them, store to data/posts.json.

Incremental: remembers the highest message id per channel and only asks for newer
ones next run. First run walks back FIRST_SYNC_LIMIT messages to build history.
"""
from __future__ import annotations

import asyncio
import re
from datetime import datetime, timezone

from telethon.tl.types import (
    DocumentAttributeFilename,
    MessageMediaDocument,
    MessageMediaPhoto,
)

from . import config
from .client import connect
from .parse import parse_message
from .store import load_posts, load_state, merge_posts, save_posts, save_state

_UNSAFE = re.compile(r"[^\w.\-() ]+")


def safe_name(name: str) -> str:
    return _UNSAFE.sub("_", name or "file")[:120]


def _doc_filename(doc) -> str:
    for attr in getattr(doc, "attributes", []) or []:
        if isinstance(attr, DocumentAttributeFilename):
            return attr.file_name
    ext = (getattr(doc, "mime_type", "") or "application/bin").split("/")[-1]
    return f"document.{ext}"


def photo_size_bytes(photo) -> int:
    """Largest known size of a photo, so the UI can show it before downloading."""
    best = 0
    for s in getattr(photo, "sizes", []) or []:
        size = getattr(s, "size", None)
        if size:
            best = max(best, int(size))
        for chunk in getattr(s, "sizes", []) or []:  # PhotoSizeProgressive
            if isinstance(chunk, int):
                best = max(best, chunk)
    return best


def describe_media(msg) -> tuple[list[dict], bool]:
    media = msg.media
    if media is None:
        return [], False
    if isinstance(media, MessageMediaPhoto):
        return [], True  # photos are tracked separately - see describe_photo
    if isinstance(media, MessageMediaDocument) and getattr(media, "document", None):
        doc = media.document
        return (
            [
                {
                    "name": _doc_filename(doc),
                    "size_bytes": int(getattr(doc, "size", 0) or 0),
                    "mime": getattr(doc, "mime_type", "") or "",
                    "local_path": None,
                }
            ],
            False,
        )
    return [], False


def describe_photo(msg) -> list[dict]:
    m = msg.media
    if not isinstance(m, MessageMediaPhoto) or not getattr(m, "photo", None):
        return []
    return [{"size_bytes": photo_size_bytes(m.photo), "local_path": None, "message_id": msg.id}]


async def download_photo(client, msg, photo: dict, slug: str) -> str | None:
    out_dir = config.FILES_DIR / safe_name(slug) / "img"
    out_dir.mkdir(parents=True, exist_ok=True)
    target = out_dir / f"{msg.id}.jpg"
    if target.exists() and target.stat().st_size > 0:
        return target.relative_to(config.FILES_DIR).as_posix()
    try:
        await client.download_media(msg, file=str(target))
        return target.relative_to(config.FILES_DIR).as_posix()
    except Exception as exc:  # noqa: BLE001 - a bad image must not stop the sync
        print(f"    image download failed for msg {msg.id}: {exc}")
        return None


async def maybe_download(client, msg, file: dict, slug: str) -> str | None:
    if not config.DOWNLOAD_FILES:
        return None
    mb = file["size_bytes"] / (1024 * 1024)
    if mb > config.MAX_FILE_MB:
        print(f"    skip download ({mb:.1f} MB > MAX_FILE_MB={config.MAX_FILE_MB}): {file['name']}")
        return None
    out_dir = config.FILES_DIR / safe_name(slug)
    out_dir.mkdir(parents=True, exist_ok=True)
    target = out_dir / f"{msg.id}_{safe_name(file['name'])}"
    if target.exists() and target.stat().st_size > 0:
        return target.relative_to(config.FILES_DIR).as_posix()
    try:
        await client.download_media(msg, file=str(target))
        return target.relative_to(config.FILES_DIR).as_posix()
    except Exception as exc:  # noqa: BLE001 - a bad file must not kill the sync
        print(f"    download failed for {file['name']}: {exc}")
        return None


async def sync_channel(client, slug: str, state: dict, now_iso: str) -> list[dict]:
    entity = await client.get_entity(slug)
    title = getattr(entity, "title", None) or getattr(entity, "username", slug)
    username = getattr(entity, "username", None)
    ch_state = state["channels"].get(slug, {})
    last_id = int(ch_state.get("last_id", 0) or 0)
    first = last_id == 0

    if first:
        if config.SINCE_DATE:
            depth = f"back to {config.SINCE_DATE.date()}"
        elif config.FIRST_SYNC_LIMIT > 0:
            depth = f"up to {config.FIRST_SYNC_LIMIT} messages"
        else:
            depth = "whole history"
        print(f"\n[{title}] first sync, {depth}")
        kwargs = {"limit": config.FIRST_SYNC_LIMIT if config.FIRST_SYNC_LIMIT > 0 else None}
    else:
        # Everything since the previous sync, nothing older.
        print(f"\n[{title}] incremental sync from message id {last_id}")
        kwargs = {"limit": None, "min_id": last_id}

    by_group: dict[str, dict] = {}
    posts: list[dict] = []
    max_id = last_id
    scanned = 0

    async for msg in client.iter_messages(entity, **kwargs):
        # Messages arrive newest-first, so the first one older than the cutoff ends the walk.
        if first and config.SINCE_DATE and msg.date and msg.date < config.SINCE_DATE:
            print(f"    reached {config.SINCE_DATE.date()} cutoff at message {msg.id}")
            break

        scanned += 1
        if msg.id > max_id:
            max_id = msg.id
        if scanned % 1000 == 0:
            print(f"    ...{scanned} scanned, {len(posts)} kept", flush=True)

        text = msg.message or ""
        files, has_photo = describe_media(msg)
        photos = describe_photo(msg)
        if not text.strip() and not files and not photos:
            continue

        # EXCLUDE_KEYWORDS: never pull bytes for a product the user opted out of
        low = f"{text} {' '.join(f['name'] for f in files)}".lower()
        if any(w in low for w in config.EXCLUDE_KEYWORDS):
            files = [{**f, "local_path": None} for f in files]
            photos = []
        else:
            for f in files:
                f["local_path"] = await maybe_download(client, msg, f, slug)
        if photos and config.DOWNLOAD_PHOTOS:
            for ph in photos:
                ph["local_path"] = await download_photo(client, msg, ph, slug)

        date = msg.date or datetime.now(timezone.utc)
        group_key = f"{slug}:g{msg.grouped_id}" if msg.grouped_id else None

        # Albums: caption sits on one message, the file on another.
        if group_key and group_key in by_group:
            g = by_group[group_key]
            if len(text.strip()) > len(g["text"]):
                g["text"] = text
            g["files"].extend(files)
            g["photos"].extend(photos)
            g["has_photo"] = g["has_photo"] or has_photo
            g["message_ids"].append(msg.id)
            continue

        rec = {
            "key": group_key or f"{slug}:{msg.id}",
            "channel": slug,
            "channel_title": title,
            "message_id": msg.id,
            "message_ids": [msg.id],
            "date": int(date.timestamp()),
            "date_iso": date.astimezone(timezone.utc).isoformat(),
            "url": f"https://t.me/{username}/{msg.id}" if username else None,
            "text": text,
            "files": files,
            "photos": photos,
            "has_photo": has_photo,
            "views": int(getattr(msg, "views", 0) or 0),
            "forwards": int(getattr(msg, "forwards", 0) or 0),
            "first_seen": now_iso,
        }
        if group_key:
            by_group[group_key] = rec
        posts.append(rec)

    print(f"  scanned {scanned} messages -> {len(posts)} candidate posts")

    parsed = [{**p, **parse_message(p["text"], p["files"])} for p in posts]
    state["channels"][slug] = {"last_id": max_id, "last_sync": now_iso, "title": title}
    return parsed


async def run_sync() -> dict:
    config.require_creds()
    now_iso = datetime.now(timezone.utc).isoformat()
    client = await connect()

    state = load_state()
    incoming: list[dict] = []
    errors: list[str] = []

    for slug in config.CHANNELS:
        try:
            incoming += await sync_channel(client, slug, state, now_iso)
        except Exception as exc:  # noqa: BLE001 - one bad channel must not kill the rest
            print(f"  channel \"{slug}\" failed: {exc}")
            errors.append(f"{slug}: {exc}")

    await client.disconnect()

    posts, added, updated = merge_posts(load_posts(), incoming)
    save_posts(posts)
    ea_count = sum(1 for p in posts if p.get("is_ea"))
    state["last_sync"] = now_iso
    state["total"] = len(posts)
    state["ea_count"] = ea_count
    state["errors"] = errors
    save_state(state)

    print(f"\nDone. +{added} new, {updated} updated. Store: {len(posts)} posts ({ea_count} look like EA drops).")
    return {
        "added": added,
        "updated": updated,
        "total": len(posts),
        "ea_count": ea_count,
        "errors": errors,
        "at": now_iso,
    }


if __name__ == "__main__":
    asyncio.run(run_sync())
