"""Build data/shots.json - the backtest-screenshot index used by the EA list.

Reads the channels in TG_IMAGE_CHANNELS (default: free_fx_pro_robotest, the
sibling channel that posts per-EA test results *with the EA name in the text*),
downloads each screenshot once, and records which product it belongs to.

  python -m app.sync_shots                # since SINCE_DATE, all image channels
  python -m app.sync_shots --limit 400    # cap messages scanned per channel
"""
from __future__ import annotations

import argparse
import asyncio
import os
import re
from collections import Counter
from datetime import datetime, timezone

from telethon.tl.types import MessageMediaPhoto

from . import config
from .client import connect
from .parse import clean, extract_name, extract_version, product_key
from .shots import load_shots, save_shots
from .sync import safe_name


def image_channels() -> list[str]:
    raw = os.environ.get("TG_IMAGE_CHANNELS", "free_fx_pro_robotest")
    return [c.strip().lstrip("@").split("/")[0] for c in raw.split(",") if c.strip()]


# Posts whose "name" is the channel's own boilerplate, not a product.
JUNK_KEYS = {"week trading history", "test completed", "hello everyone", "grid",
             "attention", "important", "news", "update", "results", "trading history"}

# Caption fields of a ROBOTEST monitoring post - the image alone does not say
# whether a test is still running or how long it has been going.
TEST_UNTIL_RE = re.compile(r"test\s*(?:until|till|end[s]?)\s*[:：]?\s*([^\n]{4,32})", re.I)
TEST_FROM_RE = re.compile(r"test\s*(?:from|start(?:ed|s)?|since)\s*[:：]?\s*([^\n]{4,32})", re.I)
DEPOSIT_RE = re.compile(r"(?:deposit|balance|start(?:ing)?\s+balance)\s*[:：]?\s*([^\n]{2,24})", re.I)
RESULT_RE = re.compile(r"(?:result|profit|pnl|growth|gain)\s*[:：]?\s*([+\-]?\d[\d.,]*\s*%?[^\n]{0,12})", re.I)
COMPLETED_RE = re.compile(r"test\s+completed|✔️", re.I)


HASHTAG_RE = re.compile(r"#([A-Za-z0-9_]{2,24})")
# Live-account links carried by every ROBOTEST post - more useful than the screenshot.
MYFXBOOK_RE = re.compile(r"https?://(?:www\.)?myfxbook\.com/\S+", re.I)
MYSTATEA_RE = re.compile(r"https?://\S*mystatea\S*", re.I)
REPORT_RE = re.compile(r"https?://pro-fx\.cc/\S*postid/\d+/?", re.I)
# The channel states its verdict as a hashtag. Order matters: worst wins.
VERDICT_TAGS = [("scamea", "scam"), ("lossea", "loss"), ("notradeea", "no-trade"), ("bestea", "pass")]
STRATEGY_TAGS = {"sl", "no_sl", "grid", "breakout", "ftmo", "one_shot", "correlation", "arbitrage"}
NOISE_TAGS = {"portfoliosettingsbtn", "post"}


def caption_fields(text: str) -> dict:
    def grab(rx):
        m = rx.search(text)
        return re.sub(r"\s+", " ", m.group(1)).strip(" .,;:-–") if m else None

    tags = [t.lower() for t in HASHTAG_RE.findall(text)]
    verdict = next((v for tag, v in VERDICT_TAGS if tag in tags), None)
    completed = bool(COMPLETED_RE.search(text))

    def first_url(rx):
        m = rx.search(text)
        return m.group(0).rstrip(".,;") if m else None

    return {
        "myfxbook": first_url(MYFXBOOK_RE),
        "mystatea": first_url(MYSTATEA_RE),
        "report": first_url(REPORT_RE),
        "test_until": grab(TEST_UNTIL_RE),
        "test_from": grab(TEST_FROM_RE),
        "test_deposit": grab(DEPOSIT_RE),
        "test_result": grab(RESULT_RE),
        "completed": completed,
        # pass | loss | no-trade | scam | None(=still running / no verdict yet)
        "verdict": verdict,
        "status": "completed" if completed or verdict else "running",
        "hashtags": [t for t in tags if t not in NOISE_TAGS],
        "strategy": [t for t in tags if t in STRATEGY_TAGS],
    }


def is_junk(name: str, key: str) -> bool:
    if not key or key in JUNK_KEYS or len(key) < 4:
        return True
    return bool(re.search(r"test\s+completed|trading\s+history|hello|subscribe|welcome", name, re.I))


async def sync_channel(client, slug: str, limit: int | None, existing: dict, since, with_images: bool) -> list[dict]:
    entity = await client.get_entity(slug)
    title = getattr(entity, "title", None) or slug
    username = getattr(entity, "username", None)
    out_dir = config.FILES_DIR / safe_name(slug) / "shots"
    out_dir.mkdir(parents=True, exist_ok=True)

    print(f"\n[{title}] scanning for test-result screenshots"
          + (f", back to {since.date()}" if since else ", full history"))

    found: list[dict] = []
    scanned = 0
    skipped = 0
    async for msg in client.iter_messages(entity, limit=limit):
        if since and msg.date and msg.date < since:
            print(f"    reached {since.date()} cutoff")
            break
        scanned += 1
        if scanned % 500 == 0:
            print(f"    ...{scanned} scanned, {len(found)} screenshots", flush=True)

        if not isinstance(msg.media, MessageMediaPhoto):
            continue
        text = clean(msg.message or "")
        if not text.strip():
            continue

        name = extract_name(text, [])
        key = product_key(name)
        if is_junk(name, key):
            skipped += 1
            continue

        uid = f"{slug}:{msg.id}"
        prev = existing.get(uid)
        target = out_dir / f"{msg.id}.jpg"
        local = None
        if prev and prev.get("local_path") and (config.FILES_DIR / prev["local_path"]).exists():
            local = prev["local_path"]      # already on disk from an earlier run
        elif with_images:
            try:
                await client.download_media(msg, file=str(target))
                local = target.relative_to(config.FILES_DIR).as_posix()
            except Exception as exc:  # noqa: BLE001 - skip a bad image, keep going
                print(f"    image {msg.id} failed: {exc}")

        found.append({
            "uid": uid,
            "channel": slug,
            "message_id": msg.id,
            "name": name,
            "product_key": key,
            "version": extract_version(name),
            "date_iso": (msg.date or datetime.now(timezone.utc)).astimezone(timezone.utc).isoformat(),
            "url": f"https://t.me/{username}/{msg.id}" if username else None,
            "local_path": local,
            "caption": text[:600],
            **caption_fields(text),
        })

    print(f"  scanned {scanned} messages -> {len(found)} screenshots ({skipped} unnamed/boilerplate skipped)")
    return found


async def run_shots(limit: int | None = None, since="default", with_images: bool = False) -> dict:
    """Refresh the test index. Callable from the server's auto-sync cycle.

    since: "default" -> config.SINCE_DATE, None -> full history, or a datetime.
    Incremental by nature: entries are keyed by channel:message_id, so a rerun only
    adds what is new and refreshes captions (a running test gains its verdict later).
    """
    if since == "default":
        since = config.SINCE_DATE

    client = await connect()
    existing = {s["uid"]: s for s in load_shots() if s.get("uid")}
    collected: dict[str, dict] = dict(existing)
    added = 0
    errors: list[str] = []
    for slug in image_channels():
        try:
            for s in await sync_channel(client, slug, limit, existing, since, with_images):
                if s["uid"] not in existing:
                    added += 1
                collected[s["uid"]] = s
        except Exception as exc:  # noqa: BLE001 - one bad channel must not kill the rest
            print(f"  channel {slug!r} failed: {exc}")
            errors.append(f"{slug}: {exc}")

    await client.disconnect()
    # drop boilerplate entries kept from earlier index builds
    kept = [s for s in collected.values() if not is_junk(s.get("name", ""), s.get("product_key", ""))]
    dropped = len(collected) - len(kept)
    if dropped:
        print(f"  purged {dropped} boilerplate entr{'y' if dropped == 1 else 'ies'} from the index")
    shots = sorted(kept, key=lambda s: s.get("date_iso", ""), reverse=True)
    save_shots(shots)

    verdicts = Counter(s.get("verdict") or "running" for s in shots)
    products = len({s["product_key"] for s in shots})
    print(f"\nIndex: {len(shots)} screenshots covering {products} products (+{added} new)")
    return {
        "total": len(shots),
        "products": products,
        "added": added,
        "verdicts": dict(verdicts),
        "errors": errors,
        "at": datetime.now(timezone.utc).isoformat(),
    }


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=0, help="max messages scanned per channel (0 = until cutoff)")
    ap.add_argument("--since", default=None,
                    help="YYYY-MM-DD override; 'all' scans full history (tests often predate the drop post)")
    ap.add_argument("--reparse", action="store_true",
                    help="recompute verdict/link fields from stored captions, no Telegram call")
    ap.add_argument("--with-images", action="store_true",
                    help="also download the screenshots (the UI links Myfxbook instead)")
    args = ap.parse_args()

    if args.reparse:
        shots = load_shots()
        for s in shots:
            s.update(caption_fields(s.get("caption") or ""))
        kept = [s for s in shots if not is_junk(s.get("name", ""), s.get("product_key", ""))]
        save_shots(kept)
        verdicts = Counter(s.get("verdict") or "running" for s in kept)
        print(f"reparsed {len(kept)} screenshots from stored captions")
        for k, v in verdicts.most_common():
            print(f"  {k:<10} {v}")
        return

    if args.since == "all":
        since = None
    elif args.since:
        since = datetime.strptime(args.since, "%Y-%m-%d").replace(tzinfo=timezone.utc)
    else:
        since = config.SINCE_DATE

    await run_shots(limit=args.limit or None, since=since, with_images=args.with_images)


if __name__ == "__main__":
    asyncio.run(main())
