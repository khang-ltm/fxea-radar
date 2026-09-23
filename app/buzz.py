"""What the channel's chat says about each EA.

The catalog lists what was posted; it says nothing about which of those EAs
anyone actually runs. The same channel carries four thousand chat messages
around those drops - asking for a set file, reporting a blown account, saying
one still works after a month - and that is the only opinion available here
that is not the seller's own.

So: count who gets talked about, and roughly how. This is not sentiment analysis
and does not pretend to be. It is a word list and a mention count, deliberately
readable: every score comes with the messages behind it, because the quotes are
the useful part and the number is only a way to sort them.

Matching is strict on purpose. An EA counts as mentioned when the distinctive
run of its name appears - "quantum queen", "boring pips" - never on one generic
word, or "gold" would be the most discussed EA in the channel. A message that
names two related products goes to the more specific one, so "Quantum Queen X"
does not quietly collect everything said about plain Quantum Queen.
"""
from __future__ import annotations

import re
from datetime import datetime, timezone

from .grouping import STOPWORDS
from .parse import clean

# Words that carry no product identity on their own. Grouping keeps a list for
# the same reason; chat is free text, so a few more matter here.
NOISE = STOPWORDS | {
    "ea", "eas", "expert", "advisor", "robot", "bot", "mt4", "mt5", "v", "ver",
    "version", "final", "fix", "fixed", "free", "new", "update", "updated",
    "cracked", "unlimited", "full", "source", "code", "set", "sets", "file",
    "the", "best", "good", "real", "live", "demo", "account", "test", "testing",
}

GOOD = (
    "profit", "profitable", "gain", "gains", "works", "working", "worked", "win",
    "winning", "wins", "stable", "consistent", "recommend", "recommended", "solid",
    "amazing", "excellent", "great", "love", "good result", "best ea", "no loss",
    "in profit", "made money", "still running", "thank", "thanks", "nice",
)
BAD = (
    "loss", "losses", "lost", "lose", "losing", "blown", "blow", "blew", "scam",
    "fake", "crap", "garbage", "useless", "avoid", "dangerous", "martingale",
    "drawdown", "stuck", "stopped out", "burn", "burned", "wiped", "margin call",
    "not working", "doesn't work", "does not work", "waste", "repaint",
)
GOOD_MARKS = ("\U0001f525", "\U0001f680", "\U0001f4b0", "\U0001f44d", "\U0001f4c8")
BAD_MARKS = ("\U0001f4c9", "\U0001f480", "\U0001f622", "\U0001f621")

# Phrases that settle it even inside a question - somebody reporting a result
# rather than asking about one.
STRONG = ("in profit", "made money", "blown", "lost", "scam", "margin call",
          "no loss", "profitable")

QUOTE_LIMIT = 8
QUOTE_CHARS = 240


def _words(text) -> list[str]:
    return [w for w in re.split(r"[^a-z0-9]+", str(text or "").lower()) if w]


def anchors_for(head: dict) -> list[list[str]]:
    """The word runs that identify one EA in free text, longest first.

    The product key is the cleaner source - "quantum queen x" rather than
    "Quantum Queen X 4.3V @free_fx_pro" - and the display name is the fallback.
    Three-word names also get their first two words, because that is how people
    actually type them; the most specific run still wins when both match.

    A name whose distinctive words are all generic gives nothing and is never
    matched. A wrong mention is worse than a missing one here: the whole point
    is to report what people discussed, not to flatter a row with a number.
    """
    source = head.get("product_key") or head.get("name") or ""
    core = [w for w in _words(source) if w not in NOISE and not w.isdigit()]
    runs = []
    if len(core) >= 2:
        runs.append(core[:4])
        if len(core) >= 3:
            runs.append(core[:2])
    elif len(core) == 1 and len(core[0]) >= 7:
        runs.append(core)            # "nostradam7", "hedgefiend" - distinctive alone
    seen, out = set(), []
    for run in runs:
        key = " ".join(run)
        if key not in seen:
            seen.add(key)
            out.append(run)
    return out


def _run_at(words: list[str], run: list[str]) -> bool:
    n = len(run)
    return any(words[i:i + n] == run for i in range(len(words) - n + 1))


def _count(terms, low: str) -> int:
    """Whole words only. "Algowin" contains "win" and "how it works" contains
    "works": counted as substrings, the channel's two most discussed EAs both
    read as universally loved."""
    n = 0
    for term in terms:
        if " " in term:
            n += low.count(term)                  # a phrase is specific enough
        elif re.search(r"\b" + re.escape(term) + r"\b", low):
            n += 1
    return n


def _tone(text: str, name_words: set) -> int:
    """Rough temperature of one message, with the EA's own name taken out first
    - an EA called ALGOWIN or GoldTrap must not praise or damn itself."""
    words = [w for w in _words(text) if w not in name_words]
    low = " ".join(words)
    good = _count(GOOD, low) + sum(1 for m in GOOD_MARKS if m in str(text))
    bad = _count(BAD, low) + sum(1 for m in BAD_MARKS if m in str(text))
    # "Does anyone know how it works?" is somebody asking, not somebody
    # reporting. A question counts as a mention and nothing more, unless it
    # says outright that money was made or lost.
    if "?" in str(text) and not any(p in low for p in STRONG):
        return 0
    if good and not bad:
        return 1
    if bad and not good:
        return -1
    return 0


def _weight(date_epoch, now: float) -> float:
    """Talk from last month means more than talk from six months ago - telling
    those apart is most of what this is for."""
    try:
        age = max(0.0, (now - float(date_epoch or 0)) / 86400)
    except (TypeError, ValueError):
        return 0.3
    return 1.0 if age <= 30 else 0.6 if age <= 90 else 0.3


def attach_buzz(grouped: list[dict], posts: list[dict]) -> list[dict]:
    """Add a `buzz` block to every EA in `grouped`.

    `posts` is the whole store; the chat messages are the part that matters,
    which is everything the parser did not call an EA drop.
    """
    products = [(head, anchors_for(head)) for head in grouped if head.get("is_ea")]
    for head, runs in products:
        head["buzz"] = {"mentions": 0, "good": 0, "bad": 0, "score": 0,
                        "last_iso": "", "quotes": [],
                        **({"unmatchable": True} if not runs else {})}

    chat = [p for p in posts if not p.get("is_ea") and (p.get("text") or "").strip()]
    now = datetime.now(timezone.utc).timestamp()

    for post in chat:
        words = _words(post.get("text_clean") or post.get("text") or "")
        if not words:
            continue
        # Every product that this message names, and how specifically. Only the
        # most specific ones are credited: a message about Quantum Queen X is
        # not also a message about Quantum Queen.
        best, hits = 0, []
        for head, runs in products:
            longest = max((len(r) for r in runs if _run_at(words, r)), default=0)
            if not longest:
                continue
            if longest > best:
                best, hits = longest, [(head, runs)]
            elif longest == best:
                hits.append((head, runs))
        if not hits:
            continue

        body = clean(post.get("text_clean") or post.get("text") or "")
        for head, runs in hits:
            # the tone of a message is judged per EA, because the words to
            # ignore are that EA's own name
            tone = _tone(post.get("text") or "", {w for run in runs for w in run})
            b = head["buzz"]
            b["mentions"] += 1
            b["score"] = round(b["score"] + _weight(post.get("date"), now), 1)
            if tone > 0:
                b["good"] += 1
            elif tone < 0:
                b["bad"] += 1
            date_iso = (post.get("date_iso") or "")[:10]
            if date_iso > b["last_iso"]:
                b["last_iso"] = date_iso
            if len(b["quotes"]) < QUOTE_LIMIT:
                b["quotes"].append({
                    "text": body[:QUOTE_CHARS] + ("…" if len(body) > QUOTE_CHARS else ""),
                    "date_iso": date_iso,
                    "url": post.get("url"),
                    "tone": tone,
                })

    for head, _ in products:
        b = head["buzz"]
        b["score"] = round(b["score"] + 2 * b["good"] - 2 * b["bad"], 1)
        b["quotes"].sort(key=lambda q: q["date_iso"], reverse=True)
    return grouped
