"""What public Telegram says about each EA: live accounts and reports, never files.

The catalog reads one channel. Dozens of public ones discuss the same EAs, and
a few of them are full of the one kind of evidence worth having - a link to a
live account on Myfxbook, FX Blue or an MQL5 signal. In the first sample, one
channel had 41 such links in its last 100 posts while most free-EA channels had
none at all: they post downloads, not results.

So this collects exactly two things per EA:

- **live-account links** posted alongside its name, which can be checked
- **what people say about it** - the same whole-word tone rules the catalog's
  own chat scoring uses, questions counted as mentions only

and deliberately nothing else. No files, no attachments, no download links and
no t.me post links are stored: every URL is stripped from quoted text except a
live-account one. This is a reading of results, not a way to find copies.

Read-only throughout, through the same Telegram login the sync uses. Public
channels are read without joining them.
"""
from __future__ import annotations

import asyncio
import re
import time
from datetime import datetime, timezone

from . import config
from .buzz import STRONG, _run_at, _tone, _words
from .mql5_market import name_key

# Search terms that find channels about EAs. Telegram's own message search only
# covers chats the account is already in, so channels are discovered by name
# first and then read directly.
CHANNEL_QUERIES = ("forex EA", "expert advisor mt5", "EA myfxbook", "gold EA",
                   "forex robot myfxbook", "mt5 robot", "EA live results")

LIVE_RE = re.compile(
    r"https?://(?:www\.)?(?:myfxbook\.com/(?:members|portfolio)/\S+|"
    r"fxblue\.com/users/\S+|mql5\.com/\w\w/signals/\d+)", re.I)
URL_RE = re.compile(r"https?://\S+|t\.me/\S+|www\.\S+", re.I)

# A post selling a copy is not evidence about the EA. The second collection run
# quoted "Quantum Queen X v4.3 EA MT5 - FIX/No DLL PRICE $30 Contact Admin" as a
# positive report: a crack shop's advert, scored as praise. Such posts are
# skipped entirely - no mention, no tone, no quote, no link taken from them.
SALE_RE = re.compile(
    r"\bprice\b|\$\s?\d|\d\s?\$|\busd\b|contact admin|buy (it )?here|\bdm\b|"
    r"\bfix(ed)?\b|\bno ?dll\b|\bdll\b|\bpatch(ed)?\b|\bcrack(ed)?\b|\bunlock(ed)?\b|"
    r"\bnulled\b|\bkeygen\b|unlimited licen|\bfree download\b|download link|\bdecompil", re.I)

MAX_CHANNELS = 25
POSTS_PER_CHANNEL = 200
QUOTES_PER_EA = 3
LIVE_PER_EA = 8


def _clean_quote(text: str) -> str:
    """A report, with every link that is not a live account removed."""
    kept = LIVE_RE.findall(text)
    out = URL_RE.sub(" ", text)
    out = " ".join(out.split())
    return (out[:220] + ("…" if len(out) > 220 else "")) if out else (kept[0] if kept else "")


# Words that name a kind of EA rather than one EA. The first collection run
# credited "Gold Liquidity" with 71 mentions and "Scalping Pro" with 68 - every
# post about any gold liquidity tool or any pro scalper, across eight channels.
# A key made only of these words identifies nothing and is not looked for.
GENERIC = {"gold", "xau", "scalper", "scalping", "pro", "ai", "liquidity", "breakout",
           "trend", "smart", "market", "structure", "robot", "system", "ultimate", "x",
           "hunter", "sniper", "signal", "signals", "trader", "trading", "forex", "fx",
           "max", "plus", "premium", "master", "king", "algo", "edition", "lite", "bot",
           "dynamic", "deep", "support", "resistance", "sweep", "my", "the", "new",
           "grid", "hedge", "news", "price", "action", "zone", "zones", "auto", "gpt",
           "neural", "quant", "scalp", "swing", "power", "fast", "super", "best"}


def heads_from(products: list[dict]) -> list[dict]:
    """One entry per distinct EA, matched only on its whole name.

    The catalog's chat scoring may shorten a long name to its first two words,
    because inside one channel that is how people type it. Across twenty-five
    channels the same shortening matched other products, so here the full key
    must appear and must contain at least one word that is not generic.
    """
    seen, out = set(), []
    for p in products:
        key = name_key(p.get("name") or "")
        if not key or key in seen:
            continue
        seen.add(key)
        words = [w for w in _words(key) if not w.isdigit()]
        distinctive = [w for w in words if w not in GENERIC]
        if not distinctive:
            continue
        if len(words) == 1 and len(words[0]) < 7:
            continue                      # "lizard" alone is a word, not a product
        out.append({"key": key, "runs": [words], "words": set(words)})
    return out


async def discover(client, log=print) -> list[str]:
    from telethon.tl.functions.contacts import SearchRequest

    found: dict[str, int] = {}
    for q in CHANNEL_QUERIES:
        try:
            r = await client(SearchRequest(q=q, limit=30))
        except Exception as exc:                               # noqa: BLE001
            log(f"[tg] search {q!r} failed: {type(exc).__name__}")
            continue
        for ch in r.chats:
            name = getattr(ch, "username", None)
            public = getattr(ch, "broadcast", False) or getattr(ch, "megagroup", False)
            if name and public:
                found[name] = getattr(ch, "participants_count", 0) or 0
        await asyncio.sleep(1.0)
    # the busiest first: a channel nobody reads is rarely where results get posted
    return [n for n, _ in sorted(found.items(), key=lambda kv: -kv[1])][:MAX_CHANNELS]


async def collect(client, products: list[dict], channels: list[str] | None = None,
                  log=print) -> dict:
    heads = heads_from(products)
    channels = channels or await discover(client, log)
    log(f"[tg] {len(heads)} EAs to look for across {len(channels)} public channels")
    now = time.time()
    by_key: dict[str, dict] = {}

    for ch in channels:
        try:
            msgs = await client.get_messages(ch, limit=POSTS_PER_CHANNEL)
        except Exception as exc:                               # noqa: BLE001
            log(f"[tg] {ch}: unreadable ({type(exc).__name__})")
            continue
        hits = 0
        for m in msgs:
            text = getattr(m, "message", "") or ""
            if not text:
                continue
            if SALE_RE.search(text):
                continue
            words = _words(text)
            for h in heads:
                if not any(_run_at(words, run) for run in h["runs"]):
                    continue
                e = by_key.setdefault(h["key"], {"mentions": 0, "good": 0, "bad": 0,
                                                 "channels": set(), "live": [], "quotes": [],
                                                 "last": 0})
                e["mentions"] += 1
                e["channels"].add(ch)
                # A long post is a seller's description or a channel's release
                # note, not somebody reporting how it went. Only short posts get
                # a tone, and only they are quoted.
                short_post = len(text) <= 400
                t = _tone(text, h["words"]) if short_post else 0
                if t > 0:
                    e["good"] += 1
                elif t < 0:
                    e["bad"] += 1
                when = m.date.timestamp() if m.date else 0
                e["last"] = max(e["last"], when)
                for url in LIVE_RE.findall(text):
                    url = url.rstrip(").,]")
                    if url not in {x["url"] for x in e["live"]} and len(e["live"]) < LIVE_PER_EA:
                        e["live"].append({"url": url, "channel": ch,
                                          "date": m.date.strftime("%Y-%m-%d") if m.date else ""})
                # quote reports, not questions and not bare link drops
                low = text.lower()
                reporting = short_post and (t != 0 or any(s in low for s in STRONG))
                if reporting and len(e["quotes"]) < QUOTES_PER_EA:
                    q = _clean_quote(text)
                    if q and len(q) > 25:
                        e["quotes"].append({"text": q, "channel": ch, "tone": t,
                                            "date": m.date.strftime("%Y-%m-%d") if m.date else ""})
                hits += 1
        log(f"[tg] {ch:<28} {len(msgs):>3} posts, {hits:>3} mentions")
        await asyncio.sleep(1.5)

    out = {}
    for key, e in by_key.items():
        out[key] = {"mentions": e["mentions"], "good": e["good"], "bad": e["bad"],
                    "channels": sorted(e["channels"]), "live": e["live"], "quotes": e["quotes"],
                    "last": datetime.fromtimestamp(e["last"], timezone.utc).strftime("%Y-%m-%d")
                    if e["last"] else ""}
    return {"by_key": out, "channels": channels,
            "collected_at": datetime.now(timezone.utc).isoformat(), "age_hours": 0,
            "took_seconds": round(time.time() - now)}


def attach_evidence(items: list[dict], evidence: dict, key_of) -> int:
    """Put an EA's Telegram evidence beside it. Read-only on both sides."""
    by_key = (evidence or {}).get("by_key") or {}
    n = 0
    for it in items:
        e = by_key.get(key_of(it))
        if e:
            it["tg"] = e
            n += 1
    return n


def main() -> None:
    import argparse

    from .client import connect
    from .grouping import group_products
    from .store import load_market, load_posts, load_tg_evidence, save_tg_evidence

    ap = argparse.ArgumentParser(description="Collect public Telegram evidence per EA")
    ap.add_argument("--rediscover", action="store_true", help="search for channels again")
    args = ap.parse_args()

    posts = [p for p in load_posts() if p.get("is_ea") and not p.get("excluded")]
    products = group_products(posts) + (load_market().get("eas") or [])
    previous = load_tg_evidence()

    async def run():
        client = await connect()
        try:
            channels = None if args.rediscover else (previous.get("channels") or None)
            return await collect(client, products, channels)
        finally:
            await client.disconnect()

    data = asyncio.run(run())
    save_tg_evidence(data)
    live = sum(len(e["live"]) for e in data["by_key"].values())
    print(f"[tg] {len(data['by_key'])} EAs discussed, {live} live-account links, "
          f"{len(data['channels'])} channels, {data['took_seconds']}s")


if __name__ == "__main__":
    main()
