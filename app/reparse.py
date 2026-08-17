"""Re-run the parser over posts already in the store.

Use after editing app/parse.py - no Telegram connection, no re-download.
Message text, dates and downloaded file paths are preserved; every derived
field (name, pairs, rules, tags, is_ea, dedupe_key, ...) is recomputed.

  python -m app.reparse            # rewrite data/posts.json in place
  python -m app.reparse --dry-run  # only report what would change
"""
from __future__ import annotations

import argparse

from .parse import parse_message
from .store import load_posts, save_posts


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    posts = load_posts()
    if not posts:
        raise SystemExit("Store is empty - nothing to reparse.")

    before_ea = sum(1 for p in posts if p.get("is_ea"))
    changed = flipped_in = flipped_out = 0

    for p in posts:
        old_ea = bool(p.get("is_ea"))
        old_name = p.get("name")
        fresh = parse_message(p.get("text", ""), p.get("files", []))
        if fresh["is_ea"] != old_ea or fresh["name"] != old_name:
            changed += 1
            flipped_in += int(fresh["is_ea"] and not old_ea)
            flipped_out += int(old_ea and not fresh["is_ea"])
        p.update(fresh)

    after_ea = sum(1 for p in posts if p.get("is_ea"))
    uniq = len({p["dedupe_key"] for p in posts if p.get("dedupe_key")})

    print(f"posts: {len(posts)}")
    print(f"is_ea: {before_ea} -> {after_ea}  (+{flipped_in} promoted, -{flipped_out} demoted)")
    print(f"unique EAs after dedupe: {uniq}")
    print(f"records with changed name/classification: {changed}")

    if args.dry_run:
        print("\n--dry-run: store not written")
        return
    save_posts(posts)
    print(f"\nRewrote {len(posts)} posts.")


if __name__ == "__main__":
    main()
