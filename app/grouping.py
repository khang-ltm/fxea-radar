"""Group EA posts into products - one entry per EA, all its versions inside.

Detection uses three signals, strongest first:

1. product_key      - the name with version and packaging noise removed
                      ("Quantum Queen X 4.3V" -> "quantum queen x")
2. attachment stem  - the .rar/.ex5 filename normalised the same way; the channel
                      names files after the product, so a renamed post still matches
3. token containment - one product_key's tokens being a subset of another's
                      ("liquidity sweep hunter" ⊂ "liquidity sweep hunter algo")

Signal 3 is the risky one, so it is fenced in: at least 2 shared tokens, at most
2 extra tokens, and true subset containment - which keeps "Smart Money Concepts
PRO" and "Smart Money Liquidation Exploits" apart (neither contains the other).
"""
from __future__ import annotations

import re

from .parse import EA_FILE_EXT, clean_ea_name, product_key

MIN_SHARED_TOKENS = 2
MIN_MEANINGFUL_TOKENS = 2   # "volume + tradingview" must NOT merge distinct indicators
MAX_EXTRA_TOKENS = 2
# Words too generic to anchor a family on their own.
STOPWORDS = {"gold", "xau", "forex", "fx", "trading", "trade", "system", "pro", "algo",
             "tradingview", "indicator", "signal", "scalper", "master", "smart", "auto",
             "volume", "profile", "zones", "model", "flow", "order", "box", "engine"}


def file_stem_key(post: dict) -> str:
    for f in post.get("files") or []:
        name = f.get("name") or ""
        if EA_FILE_EXT.search(name):
            return product_key(clean_ea_name(EA_FILE_EXT.sub("", name).replace("_", " ")))
    return ""


def _tokens(key: str) -> set[str]:
    return {t for t in re.split(r"\s+", key) if t}


def squash(key: str) -> str:
    """Key with all separators removed: '3x combo ai' and '3xcomboai' collapse.

    Used for EXACT equality only - the channel writes the same product both ways.
    Never for similarity, which would merge different products (see the audit:
    'Ultimate Breakout' vs 'Ultimatum Breakout' score 0.76 and are unrelated).
    """
    return re.sub(r"[^a-z0-9]", "", key.lower())


def _contained(a: set[str], b: set[str]) -> bool:
    """True if one token set sits inside the other, with enough real signal."""
    small, big = (a, b) if len(a) <= len(b) else (b, a)
    if not small or not small <= big:
        return False
    if len(big - small) > MAX_EXTRA_TOKENS:
        return False
    meaningful = small - STOPWORDS
    return len(small) >= MIN_SHARED_TOKENS and len(meaningful) >= MIN_MEANINGFUL_TOKENS


class _Union:
    def __init__(self):
        self.parent: dict[str, str] = {}

    def find(self, x: str) -> str:
        self.parent.setdefault(x, x)
        while self.parent[x] != x:
            self.parent[x] = self.parent[self.parent[x]]
            x = self.parent[x]
        return x

    def union(self, a: str, b: str) -> None:
        ra, rb = self.find(a), self.find(b)
        if ra != rb:
            self.parent[rb] = ra


def build_families(posts: list[dict]) -> dict[str, str]:
    """Map every post key -> canonical family key."""
    uf = _Union()
    keys = []
    for p in posts:
        k = p.get("product_key") or p.get("dedupe_key") or ""
        if not k:
            continue
        uf.find(k)
        keys.append(k)
        stem = file_stem_key(p)
        if stem and stem != k:
            uf.union(k, "file:" + stem)      # same attachment name -> same product

    uniq = sorted(set(keys))

    # same product written with different spacing: "3XComboAI" vs "3X COMBO AI"
    by_squash: dict[str, str] = {}
    for k in uniq:
        s = squash(k)
        if s in by_squash:
            uf.union(by_squash[s], k)
        else:
            by_squash[s] = k

    token_map = {k: _tokens(k) for k in uniq}
    for i, a in enumerate(uniq):
        for b in uniq[i + 1:]:
            if uf.find(a) != uf.find(b) and _contained(token_map[a], token_map[b]):
                uf.union(a, b)

    # canonical label per family: the shortest key (the plainest product name)
    members: dict[str, list[str]] = {}
    for k in uniq:
        members.setdefault(uf.find(k), []).append(k)
    canon: dict[str, str] = {}
    for root, group in members.items():
        label = min(group, key=lambda s: (len(s), s))
        for k in group:
            canon[k] = label
    return canon


def _version_sort(v: str | None) -> tuple:
    if not v:
        return (0,)
    return tuple(int(x) for x in re.findall(r"\d+", v)) or (0,)


def group_products(posts: list[dict]) -> list[dict]:
    """One entry per product, newest post as the headline, versions listed inside.

    `posts` must be newest-first. Posts without a product key pass through as-is.
    """
    canon = build_families(posts)
    out: list[dict] = []
    index: dict[str, dict] = {}

    for p in posts:
        key = p.get("product_key") or p.get("dedupe_key") or ""
        if not key:
            out.append(p)
            continue
        fam = canon.get(key, key)
        head = index.get(fam)

        if head is None:
            head = dict(p)
            head["family"] = fam
            head["versions"] = []
            head["reposts"] = 0
            head["repost_dates"] = []
            index[fam] = head
            out.append(head)

        vkey = p.get("dedupe_key") or key
        existing = next((v for v in head["versions"] if v["key"] == vkey), None)
        if existing is None:
            head["versions"].append({
                "key": vkey,
                "version": p.get("version"),
                "name": p.get("name"),
                "date_iso": p.get("date_iso", "")[:10],
                "date": p.get("date", 0),
                "url": p.get("url"),
                "files": p.get("files") or [],
            })
        else:
            head["reposts"] += 1
            if len(head["repost_dates"]) < 12:
                head["repost_dates"].append(p.get("date_iso", "")[:10])
            if not existing["files"] and p.get("files"):
                existing["files"] = p["files"]

    for head in index.values():
        head["versions"].sort(key=lambda v: (-v["date"], _version_sort(v["version"])))
        head["version_count"] = len(head["versions"])
        # headline should carry a download even if the newest post had none
        if not head.get("files"):
            for v in head["versions"]:
                if v["files"]:
                    head["files"] = v["files"]
                    break
    return out
