"""Download attachments for posts already in the store.

Lets you sweep metadata fast (DOWNLOAD_FILES=false), inspect what is there, then
pull only the files worth having - without re-scanning the whole channel.

  python -m app.fetch_files                     # EA-classified posts, robot/set/archive files
  python -m app.fetch_files --limit 50          # stop after 50 files
  python -m app.fetch_files --all-posts         # ignore the is_ea filter
  python -m app.fetch_files --ext ex4,ex5,set   # only these extensions
  python -m app.fetch_files --dry-run           # list what would be downloaded
"""
from __future__ import annotations

import argparse
import asyncio
import re

from . import config
from .client import connect
from .store import load_posts, save_posts
from .sync import safe_name

DEFAULT_EXT = ["ex4", "ex5", "mq4", "mq5", "set", "zip", "rar", "7z"]


def wanted(post: dict, file: dict, exts: list[str], all_posts: bool) -> bool:
    if post.get("excluded"):
        return False  # EXCLUDE_KEYWORDS
    if file.get("local_path"):
        return False
    if not all_posts and not post.get("is_ea"):
        return False
    return bool(re.search(rf"\.({'|'.join(exts)})$", file.get("name", ""), re.I))


async def fetch_photos(args) -> None:
    """Download the image of every stored photo post that has none yet.

    Older posts were synced before photo support existed, so they carry
    has_photo=True but no photos[] entry - those are rebuilt from message ids.
    """
    from .sync import download_photo

    config.require_creds()
    posts = load_posts()
    todo = [
        p
        for p in posts
        if (p.get("has_photo") or p.get("photos"))
        and not any(ph.get("local_path") for ph in p.get("photos") or [])
    ]
    if args.limit:
        todo = todo[: args.limit]

    print(f"{len(todo)} post(s) with an image to fetch")
    if args.dry_run:
        for p in todo[:40]:
            print(f"  {p['date_iso'][:10]}  {(p['text'] or '')[:70]!r}")
        return
    if not todo:
        return

    client = await connect()
    entities: dict[str, object] = {}
    ok = fail = 0
    for i, post in enumerate(todo, 1):
        slug = post["channel"]
        if slug not in entities:
            entities[slug] = await client.get_entity(slug)
        try:
            msgs = await client.get_messages(entities[slug], ids=post["message_ids"])
            saved = []
            for msg in msgs:
                if msg is None or msg.media is None:
                    continue
                path = await download_photo(client, msg, {}, slug)
                if path:
                    saved.append({"size_bytes": 0, "local_path": path, "message_id": msg.id})
            if saved:
                post["photos"] = saved
                ok += 1
            else:
                fail += 1
        except Exception as exc:  # noqa: BLE001 - keep going through the batch
            fail += 1
            print(f"  [{i}/{len(todo)}] FAILED msg {post['message_id']}: {exc}", flush=True)
        if i % 25 == 0:
            save_posts(posts)
            print(f"  [{i}/{len(todo)}] {ok} images saved", flush=True)

    save_posts(posts)
    await client.disconnect()
    print(f"\nImages saved for {ok} posts, {fail} without a usable image.")


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=0, help="max files to download (0 = no cap)")
    ap.add_argument("--all-posts", action="store_true", help="also fetch from non-EA posts")
    ap.add_argument("--ext", default=",".join(DEFAULT_EXT), help="comma-separated extensions")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--photos", action="store_true", help="fetch photos (backtest screenshots) instead of documents")
    args = ap.parse_args()
    if args.photos:
        await fetch_photos(args)
        return
    exts = [e.strip().lstrip(".").lower() for e in args.ext.split(",") if e.strip()]

    config.require_creds()
    posts = load_posts()

    todo = []
    for p in posts:
        for f in p.get("files", []):
            if wanted(p, f, exts, args.all_posts):
                todo.append((p, f))
    todo.sort(key=lambda pf: -pf[0].get("date", 0))  # newest first

    total_mb = sum(f["size_bytes"] for _, f in todo) / (1024 * 1024)
    capped = [pf for pf in todo if pf[1]["size_bytes"] / (1024 * 1024) <= config.MAX_FILE_MB]
    skipped_big = len(todo) - len(capped)
    if args.limit:
        capped = capped[: args.limit]
    plan_mb = sum(f["size_bytes"] for _, f in capped) / (1024 * 1024)

    print(f"{len(todo)} missing attachment(s), {total_mb:.1f} MB total")
    print(f"  {skipped_big} over MAX_FILE_MB={config.MAX_FILE_MB}, will download {len(capped)} ({plan_mb:.1f} MB)")
    if args.dry_run:
        for p, f in capped[:60]:
            print(f"  {p['date_iso'][:10]}  {f['size_bytes']/1024:>8.0f} KB  {f['name'][:60]}")
        if len(capped) > 60:
            print(f"  ... and {len(capped) - 60} more")
        return
    if not capped:
        return

    client = await connect()
    entities: dict[str, object] = {}
    ok = fail = 0

    for i, (post, file) in enumerate(capped, 1):
        slug = post["channel"]
        if slug not in entities:
            entities[slug] = await client.get_entity(slug)
        try:
            msgs = await client.get_messages(entities[slug], ids=post["message_ids"])
            msg = next((m for m in msgs if m is not None and m.media is not None), None)
            if msg is None:
                print(f"  [{i}/{len(capped)}] gone from channel: {file['name']}")
                fail += 1
                continue
            out_dir = config.FILES_DIR / safe_name(slug)
            out_dir.mkdir(parents=True, exist_ok=True)
            target = out_dir / f"{msg.id}_{safe_name(file['name'])}"
            if not (target.exists() and target.stat().st_size > 0):
                await client.download_media(msg, file=str(target))
            file["local_path"] = target.relative_to(config.FILES_DIR).as_posix()
            ok += 1
            print(f"  [{i}/{len(capped)}] {file['name'][:55]}  ({file['size_bytes']/1024:.0f} KB)", flush=True)
        except Exception as exc:  # noqa: BLE001 - one bad file must not stop the batch
            fail += 1
            print(f"  [{i}/{len(capped)}] FAILED {file['name'][:45]}: {exc}", flush=True)
        if i % 20 == 0:
            save_posts(posts)  # checkpoint so a crash does not lose progress

    save_posts(posts)
    await client.disconnect()
    print(f"\nDownloaded {ok}, failed {fail}. Files in {config.FILES_DIR}")


if __name__ == "__main__":
    asyncio.run(main())
