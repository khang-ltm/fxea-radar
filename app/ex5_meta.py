"""Read the three things a compiled .ex5 still says about itself.

MT5 shows an EA's parameter dialog without its source, so the labels must be in
the .ex5 somewhere - and they are, behind whatever MetaQuotes packs the body
with. Measured on three real files, that body carries 7.99 bits of entropy per
byte, which is not compression falling short of readable, it is encryption:
nothing in it decompresses, and the only zlib-looking header in 88 KB was a
coincidence. Input names and labels are not coming back out, and no amount of
reading harder changes that.

Three fields survive in the clear, because MT5 itself needs them before it will
open the file - it lists them in the Navigator. They sit at fixed offsets as
null-terminated UTF-16LE:

    168  #property copyright  (or description)
    424  #property link
    680  #property version

That is worth having for two reasons. The version compiled into the binary is
the authoritative one - a file named "Quantum Queen X 4.3V.ex5" reports 4.301 -
and the channel names its downloads however it feels that day.

And the copyright is where a cracked build gives itself away. One of the three
files read here has had its copyright replaced with

    "EX4 protection unlock service: @ebfe90"

alongside a Telegram link. An EA whose author field advertises a cracking
service has been modified by someone other than its author, which is worth
knowing before it is given an account to trade.
"""
from __future__ import annotations

import pathlib
import re

MAGIC = b"EX5"

# offset -> field, measured on real files and stable across them
FIELDS = {"author": 168, "link": 424, "version": 680}

MAX_CHARS = 128

# Phrases that only appear when somebody other than the author has been in the
# file. Deliberately narrow: "free" and "fix" are in half the channel's titles
# and mean nothing, while an unlock service names itself because it is
# advertising.
CRACK_SIGNS = re.compile(
    r"unlock|crack|protection\s*(removed|bypass)|no\s*licen[cs]e|"
    r"patched\s*by|nulled|keygen",
    re.I)


def _utf16_at(blob: bytes, offset: int) -> str:
    """Null-terminated UTF-16LE, bounded so a wrong offset cannot run away."""
    out: list[str] = []
    end = min(offset + MAX_CHARS * 2, len(blob))
    for i in range(offset, end - 1, 2):
        ch = blob[i] | (blob[i + 1] << 8)
        if ch == 0:
            break
        # anything unprintable means this is not the field it was meant to be
        if ch < 0x20 and ch not in (0x09,):
            return ""
        out.append(chr(ch))
    return "".join(out).strip()


def read(path) -> dict:
    """What the file says about itself, or ok=False and why.

    Never raises: this runs while listing every EA in a folder, and one odd
    file must not take the listing down with it.
    """
    p = pathlib.Path(path)
    try:
        with p.open("rb") as fh:
            head = fh.read(max(FIELDS.values()) + MAX_CHARS * 2 + 2)
    except OSError as exc:
        return {"ok": False, "error": str(exc)}

    if not head.startswith(MAGIC):
        return {"ok": False, "error": "not an .ex5 file"}

    out = {"ok": True, "build": head[4] if len(head) > 4 else 0}
    for name, off in FIELDS.items():
        out[name] = _utf16_at(head, off) if len(head) > off + 1 else ""

    # The author field is the one a cracker rewrites, but a link to a Telegram
    # handle offering the same service belongs to the same tell.
    marked = " ".join(filter(None, (out.get("author"), out.get("link"))))
    hit = CRACK_SIGNS.search(marked)
    out["tampered"] = bool(hit)
    out["tampered_why"] = (
        f"its author field reads {out['author']!r}" if hit and out.get("author")
        else f"its link reads {out['link']!r}" if hit else "")
    return out


def describe(meta: dict) -> str:
    """One line for a log or a tooltip."""
    if not meta.get("ok"):
        return meta.get("error") or "unreadable"
    bits = []
    if meta.get("version"):
        bits.append("v" + meta["version"])
    if meta.get("author"):
        bits.append("by " + meta["author"])
    if meta.get("tampered"):
        bits.append("MODIFIED - " + meta["tampered_why"])
    return ", ".join(bits) or "no version or author compiled in"
