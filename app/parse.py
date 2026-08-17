"""Turn a raw Telegram message into structured EA metadata.

Forex channels have no schema, so every extractor is best-effort and returns
None / [] when there is no confident match.
"""
from __future__ import annotations

import re

from . import config

ROBOT_EXT = re.compile(r"\.(ex4|ex5|mq4|mq5)$", re.I)
ARCHIVE_EXT = re.compile(r"\.(zip|rar|7z)$", re.I)
SET_EXT = re.compile(r"\.set$", re.I)
EA_FILE_EXT = re.compile(r"\.(ex4|ex5|mq4|mq5|set|zip|rar|7z)$", re.I)

CURRENCIES = "USD|EUR|GBP|JPY|AUD|NZD|CAD|CHF|XAU|XAG"
PAIR_RE = re.compile(rf"\b({CURRENCIES})[/ ]?({CURRENCIES})\b", re.I)
INDEX_RE = re.compile(
    r"\b(GOLD|SILVER|XAU|XAG|US30|US100|US500|NAS100|NASDAQ|SPX500|DAX40|DAX30|GER30|"
    r"BTCUSD|ETHUSD|BTC|ETH)\b",
    re.I,
)
PAIR_ALIASES = {
    "GOLD": "XAUUSD",
    "XAU": "XAUUSD",
    "SILVER": "XAGUSD",
    "NASDAQ": "NAS100",
    "BTC": "BTCUSD",
    "ETH": "ETHUSD",
    "DAX30": "DAX40",
}

TF_RE = re.compile(r"\b(M1|M5|M15|M30|H1|H4|D1|W1|MN1|MN)\b")
TF_MIN_RE = re.compile(r"\b(\d{1,3})\s*(?:min|mins|minute|minutes)\b", re.I)
TF_HOUR_RE = re.compile(r"\b(\d{1,2})\s*(?:h|hr|hrs|hour|hours)\b", re.I)
TF_WORD_RE = re.compile(r"\b(daily|weekly|monthly)\b", re.I)

BULLET_CHARS = r"\-\*•●▪▫◦→➤➡👉✅☑✔❗⚠🔹🔸📌📍🔻▶"
BULLET_RE = re.compile(rf"^\s*(?:[{BULLET_CHARS}️]+|\d{{1,2}}[.)])\s*\S")
BULLET_STRIP_RE = re.compile(rf"^\s*(?:[{BULLET_CHARS}️]+|\d{{1,2}}[.)])\s*")

_RULE_WORDS = (
    r"(?:trading\s+)?(?:rules?|requirements?|conditions?|settings?|setup|set\s?up|"
    r"installation|how\s+to\s+(?:use|install|run)|instructions?|guide|"
    r"recommend(?:ed|ations?)?|notes?|important|warning|risk\s+management|"
    r"money\s+management|inputs?|parameters?)"
)
RULE_HEADING_RE = re.compile(
    rf"^[\s\*_~`>{BULLET_CHARS}️]*{_RULE_WORDS}\b\s*[:：\-–—]?\s*$", re.I
)
_INLINE_WORDS = (
    r"(?:trading\s+)?(?:rules?|requirements?|conditions?|settings?|setup|installation|"
    r"how\s+to\s+(?:use|install|run)|instructions?|recommend(?:ed|ations?)?|"
    r"risk\s+management|money\s+management|broker|account\s+type|"
    r"min(?:imum)?\s+deposit|leverage|time\s?frame|pairs?|symbols?|lot\s?size)"
)
INLINE_RULE_RE = re.compile(
    rf"^[\s\*_~`>{BULLET_CHARS}️]*({_INLINE_WORDS})\b\s*[:：]\s*(.+)$", re.I
)

MD_RE = re.compile(r"[*_`~]{1,3}")
URL_RE = re.compile(r"https?://[^\s)<>\"']+", re.I)
MENTION_RE = re.compile(r"(?:^|[\s(])(@[A-Za-z0-9_]{4,})")

# --- @free_fx_pro drop template -------------------------------------------
# 💥<name> with sets MT5 / 🗂Category : EA / 💱XAUUSD GOLD / ⏱D1 / ▶️Broker /
# 💵Minimum deposit: $200, followed by a fixed promo footer.
TPL_NAME_RE = re.compile(r"^\s*💥\s*(.+?)\s*$", re.M)
TPL_CATEGORY_RE = re.compile(r"🗂\s*Category\s*[:：]\s*([^\n]{1,40})", re.I)
TPL_PAIRS_RE = re.compile(r"💱\s*([^\n]{1,80})")
TPL_TF_RE = re.compile(r"⏱\s*([^\n]{1,40})")
TPL_BROKER_RE = re.compile(r"▶️?\s*([A-Za-z][A-Za-z0-9 .&'\-]{2,30})")
TPL_MARKER_RE = re.compile(r"🗂\s*Category\s*[:：]|^\s*💥", re.I | re.M)

# Promo/footer lines that are identical on every post - they must not become "rules".
BOILERPLATE_RES = [
    re.compile(p, re.I)
    for p in (
        r"we give our \w+ clients .*affiliate commission",
        r"^\s*📈?\s*robotest\s*$",
        r"best vps for forex",
        r"subscribe for success",
        r"signals? for copy trades",
        r"send donation",
        r"beware of scammers",
        r"^\s*👨.*admin\s*@",
        r"^\s*🏦?\s*seller\s*$",
        r"^\s*💰\s*$",
        r"our ib\s*==?>",
        r"^\s*🔝?\s*best forex brokers\s*$",
        r"autorebate|automatically receive a refund",
        r"^\s*[💵🟢▶️]*\s*(roboforex|tickmill|forex4you|govpsfx|zomro|vantagemarkets|icmarkets\w*|exness|vtmarkets)\s*$",
        r"discuss on the robotest forum",
        r"^\s*⤴️?\s*more info\s*$",
        r"download\s+detailed report",
        r"if you are unfamiliar with or unsure of your knowledge",
        r"administration of our free fx team",
        r"^\s*[⚡️💬🔤🔥✅⬇️📣🔭⤴️🗂🏦]+\s*$",
        r"^\s*🗂?\s*category\s*[:：]",       # surfaced as a tag instead
        r"^\s*[💱⏱]",                       # surfaced as pairs / timeframes instead
        r"^\s*💵\s*min(?:imum)?\s+deposit",  # surfaced as the Deposit field instead
        r"^\s*t\.me/\S+\s*$",
    )
]


def strip_boilerplate(text: str) -> str:
    out = []
    for line in clean(text).split("\n"):
        if any(r.search(line) for r in BOILERPLATE_RES):
            continue
        out.append(line)
    # collapse the blank runs the removed lines leave behind
    return re.sub(r"\n{3,}", "\n\n", "\n".join(out)).strip()


# Lines that are neither rules nor prose: bare broker names, promo one-liners.
JUNK_RULE_RE = re.compile(
    r"^(?:vantagemarkets|icmarkets\w*|vtmarkets|exness|roboforex|tickmill|forex4you|govpsfx|zomro"
    r"|signals?(?:\s*\d+)?|seller|category|more info|robotest|http\S*)$",
    re.I,
)


def clean_ea_name(name: str) -> str:
    """Drop channel-handle watermarks and 'with sets MT5' tails from a name."""
    n = re.sub(r"@[A-Za-z0-9_]+", "", name)
    n = re.sub(r"\bwith\s+set(?:s|\s*files?)?\b", "", n, flags=re.I)
    n = re.sub(r"[\s_.\-]+$", "", n.strip())
    return re.sub(r"\s{2,}", " ", n).strip(" -–—_.")


def clean(text: str | None) -> str:
    t = (text or "").replace("\r", "").replace(" ", " ")
    return "\n".join(line.rstrip() for line in t.split("\n"))


def strip_md(s: str) -> str:
    return MD_RE.sub("", s).strip()


# ------------------------------------------------------------------- name

def extract_name(text: str, files: list[dict]) -> str:
    # The channel's own template puts the product name on the 💥 line.
    tpl = TPL_NAME_RE.search(clean(text))
    if tpl:
        name = clean_ea_name(strip_md(tpl.group(1)))
        if len(name) >= 3:
            return name[:90]

    robot = next((f for f in files if ROBOT_EXT.search(f["name"])), None)
    if robot is None:
        robot = next((f for f in files if EA_FILE_EXT.search(f["name"])), None)
    from_file = None
    if robot:
        stem = EA_FILE_EXT.sub("", robot["name"]).replace("_", " ")
        from_file = clean_ea_name(stem)

    ls = [strip_md(l) for l in clean(text).split("\n")]
    ls = [l for l in ls if l]

    for l in ls[:12]:
        m = re.match(
            r"^(?:ea|robot|expert\s*advisor|bot|system|strategy)\s*(?:name)?\s*[:：\-–]\s*(.{2,80})$",
            l,
            re.I,
        )
        if m:
            return m.group(1).strip()

    for l in ls[:8]:
        if len(l) <= 90 and re.search(
            r"\b(ea|robot|bot|expert\s*advisor|scalper|grid|martingale|hedge)\b", l, re.I
        ):
            return re.sub(r"^[^\w$]+", "", l).rstrip(" -–—:")[:90] or l[:90]

    if from_file:
        return from_file[:90]
    if ls:
        first = ls[0]
        return re.sub(r"^[^\w$]+", "", first)[:90] if len(first) <= 90 else first[:70] + "…"
    return "(no title)"


# ------------------------------------------------------------------ pairs

def extract_pairs(text: str) -> list[str]:
    t = clean(text)
    out: list[str] = []
    # Template line wins when present: "💱XAUUSD GOLD" / "💱Any"
    tpl = TPL_PAIRS_RE.search(t)
    if tpl:
        line = tpl.group(1)
        if re.fullmatch(r"\s*(any|all)\s*", line, re.I):
            return ["ALL PAIRS"]
        t = line + "\n" + t
    for a, b in PAIR_RE.findall(t):
        a, b = a.upper(), b.upper()
        if a != b:
            out.append(a + b)
    for raw in INDEX_RE.findall(t):
        raw = raw.upper()
        out.append(PAIR_ALIASES.get(raw, raw))
    if re.search(r"\ball\s+pairs?\b|\bany\s+pairs?\b|\bmulti[\s-]?currency\b", t, re.I):
        out.append("ALL PAIRS")
    return list(dict.fromkeys(out))[:12]


# ------------------------------------------------------------- timeframes

def extract_timeframes(text: str) -> list[str]:
    t = clean(text)
    out: list[str] = []
    # "⏱D1" / "⏱Any"
    tpl = TPL_TF_RE.search(t)
    if tpl:
        line = tpl.group(1)
        if re.fullmatch(r"\s*(any|all)\s*", line, re.I):
            return []
        found = TF_RE.findall(line.upper())
        if found:
            return list(dict.fromkeys("MN1" if f == "MN" else f for f in found))
    for tf in TF_RE.findall(t.upper()):
        out.append("MN1" if tf == "MN" else tf)
    minute_map = {1: "M1", 5: "M5", 15: "M15", 30: "M30", 60: "H1", 240: "H4"}
    for n in TF_MIN_RE.findall(t):
        tf = minute_map.get(int(n))
        if tf:
            out.append(tf)
    hour_map = {1: "H1", 4: "H4"}
    for n in TF_HOUR_RE.findall(t):
        tf = hour_map.get(int(n))
        if tf:
            out.append(tf)
    word_map = {"daily": "D1", "weekly": "W1", "monthly": "MN1"}
    for w in TF_WORD_RE.findall(t):
        out.append(word_map[w.lower()])
    return list(dict.fromkeys(out))


# --------------------------------------------------------- money / broker

def _first(text: str, patterns: list[str]) -> str | None:
    t = clean(text)
    for p in patterns:
        m = re.search(p, t, re.I)
        if m:
            val = (m.group(1) if m.groups() else m.group(0)).strip()
            val = re.sub(r"\s+", " ", val).strip(" .,;:-–")
            if val:
                return val
    return None


def extract_deposit(text: str) -> str | None:
    return _first(
        text,
        [
            r"(?:min(?:imum)?\.?\s*(?:deposit|balance|capital|equity)|recommended\s+(?:deposit|balance|capital))\s*[:：\-–]?\s*(\$?\s?[\d.,]+\s?(?:k|usd|\$|€|eur)?)",
            r"(\$\s?[\d.,]+)\s*(?:min(?:imum)?\s*)?(?:deposit|balance|capital)",
            r"(?:deposit|balance|capital)\s*(?:from|of|>=|≥)\s*(\$?\s?[\d.,]+\s?(?:usd|\$)?)",
        ],
    )


def extract_leverage(text: str) -> str | None:
    return _first(text, [r"\b(1\s?:\s?\d{2,4})\b", r"leverage\s*[:：\-–]?\s*(\d{2,4})\b"])


def extract_lot(text: str) -> str | None:
    return _first(
        text,
        [
            r"(?:lot\s*size|lots?|fixed\s+lot|start(?:ing)?\s+lot)\s*[:：\-–]?\s*(\d+(?:\.\d+)?)",
            r"(\d+\.\d{1,2})\s*lots?\b",
        ],
    )


def extract_risk(text: str) -> str | None:
    return _first(
        text,
        [
            r"(?:risk|drawdown|dd)\s*[:：\-–]?\s*(\d{1,2}(?:\.\d+)?\s*%)",
            r"(\d{1,2}(?:\.\d+)?\s*%)\s*(?:risk|per\s+trade|drawdown)",
        ],
    )


def extract_account_type(text: str) -> str | None:
    return _first(
        text,
        [
            r"account\s*(?:type)?\s*[:：\-–]\s*([A-Za-z0-9 .+/\-]{2,40})",
            r"\b(ecn|raw\s*spread|cent|standard|zero\s*spread|prop\s*firm)\s*account\b",
        ],
    )


def extract_password(text: str) -> str | None:
    return _first(text, [r"(?:password|pass|pwd|unlock\s*code|licen[cs]e\s*key)\s*[:：\-–]\s*(\S{2,60})"])


def extract_expiry(text: str) -> str | None:
    return _first(
        text,
        [r"(?:expires?|expiry|valid\s*(?:till|until)|deadline|offer\s*ends?)\s*[:：\-–]?\s*([^\n]{3,40})"],
    )


# ------------------------------------------------------------------ rules

def extract_rules(text: str) -> list[str]:
    """Lines under a rules/settings heading, plus bullet lines, plus 'Key: value' config lines."""
    rules: list[str] = []
    capturing = False
    blank_run = 0

    for raw in clean(text).split("\n"):
        line = raw.strip()
        if not line:
            if capturing:
                blank_run += 1
                if blank_run >= 2:
                    capturing = False
            continue

        bare = strip_md(line)

        if RULE_HEADING_RE.match(bare):
            capturing = True
            blank_run = 0
            continue

        inline = INLINE_RULE_RE.match(bare)
        if inline:
            label = inline.group(1).strip()
            label = label[:1].upper() + label[1:].lower()
            rules.append(f"{label}: {inline.group(2).strip()}")
            continue

        if line.lstrip().startswith("▶"):
            continue  # broker list, not a rule

        if capturing or BULLET_RE.match(line):
            item = BULLET_STRIP_RE.sub("", bare).strip()
            if len(item) >= 4 and not JUNK_RULE_RE.match(item):
                rules.append(item)
            if len(rules) > 40:
                break

    seen: set[str] = set()
    out = []
    for r in rules:
        k = r.lower()
        if k not in seen:
            seen.add(k)
            out.append(r)
    return out


# ------------------------------------------------------------------ links

INSTALL_HINT_RE = re.compile(
    r"\b(install|copy|paste|attach|drag|drop|extract|unzip|unrar|restart|terminal|"
    r"mql[45]|experts?\s+folder|data\s+folder|allow\s+urls?|algo\s*trading|auto\s*trading|"
    r"enable|import|load\s+set|set\s*file|preset|navigator|smart\s+routing|dll)\b",
    re.I,
)


def extract_install(text: str) -> list[str]:
    """Lines that tell you how to get the EA running, as opposed to trading rules."""
    out = []
    for raw in clean(text).split("\n"):
        if raw.lstrip().startswith("💥"):
            continue  # that is the product title, not an instruction
        line = strip_md(BULLET_STRIP_RE.sub("", raw.strip()))
        if len(line) < 6 or line.lower().startswith("http"):
            continue
        if INSTALL_HINT_RE.search(line) and not JUNK_RULE_RE.match(line):
            out.append(line)
    seen: set[str] = set()
    uniq = []
    for x in out:
        k = x.lower()
        if k not in seen:
            seen.add(k)
            uniq.append(x)
    return uniq[:12]


def extract_links(text: str) -> tuple[list[str], list[str]]:
    t = clean(text)
    links = list(dict.fromkeys(u.rstrip(".,;:") for u in URL_RE.findall(t)))[:15]
    mentions = list(dict.fromkeys(MENTION_RE.findall(t)))[:10]
    return links, mentions


# ------------------------------------------------------------------- tags

def extract_tags(text: str, files: list[dict]) -> list[str]:
    t = clean(text).lower()
    names = " ".join(f["name"].lower() for f in files)
    tags: list[str] = []

    def add(cond, tag):
        if cond:
            tags.append(tag)

    no_mart = re.search(r"\b(?:no|non|without|not?\s+using)[\s-]*martingale\b", t)
    add(re.search(r"\bmartingale\b", t) and not no_mart, "martingale")
    add(re.search(r"\bgrid\b", t), "grid")
    add(re.search(r"\bscalp", t), "scalper")
    add(re.search(r"\bhedg", t), "hedging")
    add(re.search(r"\bnews\b", t), "news")
    add(re.search(r"\bprop\s*firm|\bftmo\b|\bchallenge\b", t), "prop-firm")
    add(no_mart, "no-martingale")
    add(re.search(r"\bsource\s*code\b|\.mq[45]\b", t + " " + names), "source-code")
    add(re.search(r"\bunlimited\b|\bcracked\b|\bno\s*licen[cs]e\b|\bfull\s*version\b", t), "cracked")
    add(re.search(r"\bbacktest", t), "backtest")
    # The channel's own test-result posts: "✔️Test completed" / "🔄week trading history"
    add(re.search(r"test\s+completed|week\s+trading\s+history|trading\s+results?\b", t), "test-result")
    add(re.search(r"\bmt4\b", t) or ".ex4" in names or ".mq4" in names, "mt4")
    add(re.search(r"\bmt5\b", t) or ".ex5" in names or ".mq5" in names, "mt5")
    # The template's "🗂Category : ..." line tells EA from indicator/course.
    cat = TPL_CATEGORY_RE.search(text)
    cat = (cat.group(1).strip().lower() if cat else "")
    add("indicator" in cat or re.search(r"\btradingview\b|\bindicator\b", t), "indicator")
    add("course" in cat or re.search(r"\bcourse\b|\btraining\b", cat), "course")
    add(cat.startswith("ea") or "expert" in cat, "template-ea")
    add(any(ROBOT_EXT.search(f["name"]) for f in files), "has-ea-file")
    add(any(SET_EXT.search(f["name"]) for f in files), "has-set-file")
    add(any(ARCHIVE_EXT.search(f["name"]) for f in files), "has-archive")
    return list(dict.fromkeys(tags))


def dedupe_key(name: str) -> str:
    """Identity of an EA, so reposts of the same product collapse to one entry.

    Version numbers are kept deliberately - v6.2 and v7 are different products -
    but their spelling is normalised first, because the channel writes the same
    version as "v4.3", "4.3V", "4.3 fix" and "v 4.3" on different days.
    """
    n = clean_ea_name(name or "").lower()
    # packaging / repost noise that does not change which product this is
    n = re.sub(
        r"\b(mt4|mt5|metatrader\s*[45]?|ea|expert\s*advisor|robot|bot|free|full|unlimited"
        r"|cracked|new|update[d]?|version|ver|fix(?:ed|es)?|repost|reupload|source\s*code"
        r"|fx\s*pro|work(?:ing)?|build\s*\d+\+?|no\s*dll)\b",
        " ",
        n,
    )
    n = re.sub(r"[^a-z0-9.]+", " ", n)
    n = re.sub(r"\bv\s*(?=\d)", "v", n)                    # "v 4.3" -> "v4.3"
    n = re.sub(r"\b(\d+(?:\.\d+)?)\s*v\b", r"v\1", n)      # "4.3 V" / "4.3V" -> "v4.3"
    n = re.sub(r"(?<![a-z0-9.])(\d+\.\d+)", r"v\1", n)     # bare "4.3" -> "v4.3"
    n = re.sub(r"\bv(\d+)\.0\b", r"v\1", n)                # "v6.0" -> "v6"
    n = re.sub(r"\bv+(\d)", r"v\1", n)                     # "vv4.3" -> "v4.3"
    return re.sub(r"\s{2,}", " ", n).strip()


VERSION_RE = re.compile(r"\bv(\d+(?:\.\d+)*)\b")


def extract_version(name: str) -> str | None:
    """Version as the channel means it, normalised: 'Queen X 4.3V' -> 'v4.3'."""
    m = VERSION_RE.search(dedupe_key(name))
    return f"v{m.group(1)}" if m else None


def product_key(name: str) -> str:
    """Identity of the PRODUCT FAMILY - the same EA across all its versions.

    'Quantum Queen X v4.1', 'Quantum Queen X 4.3V' and 'Quantum Queen X v4.3
    Source code' all collapse to 'quantum queen x'.
    """
    n = VERSION_RE.sub(" ", dedupe_key(name))
    n = re.sub(r"\b\d+(?:\.\d+)*\b", " ", n)   # leftover bare numbers
    return re.sub(r"\s{2,}", " ", n).strip(" .-")


def score_is_ea(text: str, files: list[dict]) -> int:
    """Is this an EA drop, or just chat/signal noise? Score so a file OR strong keywords qualifies."""
    t = clean(text).lower()
    names = " ".join(f["name"].lower() for f in files)
    score = 0
    if any(ROBOT_EXT.search(f["name"]) for f in files):
        score += 5
    if any(ARCHIVE_EXT.search(f["name"]) or SET_EXT.search(f["name"]) for f in files):
        score += 2
    if re.search(r"\bexpert\s*advisor\b|\bea\b|\brobot\b", t):
        score += 2
    if re.search(r"\bmt4\b|\bmt5\b|\bmetatrader\b", t + " " + names):
        score += 2
    if re.search(r"\bfree\b", t):
        score += 1
    if re.search(r"\bdownload\b|mediafire|drive\.google|mega\.nz|dropbox|anonfiles", t):
        score += 2
    if re.search(r"\bscalp|\bmartingale\b|\bgrid\b|\bbacktest", t):
        score += 1
    if not files and re.search(r"\bsignal\b|\bbuy\s+now\b|\bsell\s+now\b|\btp\s?\d\b|\bsl\b", t):
        score -= 2
    if TPL_MARKER_RE.search(clean(text)):
        score += 4  # the channel's own drop template
    return score


# Real file hosts only - t.me links are internal cross-promo on every post.
DL_HOST_RE = re.compile(r"mega\.nz|mediafire|drive\.google|dropbox|anonfiles|gofile|1fichier|pixeldrain|terabox", re.I)


def parse_message(text: str, files: list[dict]) -> dict:
    raw = clean(text or "")
    body = strip_boilerplate(raw)  # promo footer must not become rules or links

    links, mentions = extract_links(body)
    score = score_is_ea(body, files)
    name = extract_name(body, files)

    # A post only counts as a drop if it actually delivers something: an attachment,
    # a download link, or the channel's drop template. Pure chat about EAs does not.
    has_payload = bool(files) or any(DL_HOST_RE.search(l) for l in links) or bool(TPL_MARKER_RE.search(raw))
    is_ea = score >= 3 and has_payload

    tags = extract_tags(raw, files)
    kind = "indicator" if "indicator" in tags else "course" if "course" in tags else "ea"

    # EXCLUDE_KEYWORDS: products the user never wants listed or downloaded
    haystack = f"{name} {raw} {' '.join(f.get('name', '') for f in files)}".lower()
    excluded = next((w for w in config.EXCLUDE_KEYWORDS if w in haystack), None)

    # Structured fields come from the RAW text - the template lines they live on
    # (💱 pairs, ⏱ timeframe, 💵 deposit) are stripped from the display body.
    return {
        "name": name,
        "text_clean": body,  # promo footer removed, for display
        "pairs": extract_pairs(raw),
        "timeframes": extract_timeframes(raw),
        "deposit": extract_deposit(raw),
        "leverage": extract_leverage(raw),
        "lot": extract_lot(raw),
        "risk": extract_risk(raw),
        "account_type": extract_account_type(raw),
        "password": extract_password(raw),
        "expiry": extract_expiry(raw),
        "rules": extract_rules(body),
        "install": extract_install(body),
        "links": links,
        "mentions": mentions,
        "tags": tags,
        "kind": kind,  # ea | indicator | course - indicators are not robots
        "excluded": bool(excluded),
        "excluded_by": excluded,
        "ea_score": score,
        "is_ea": is_ea,
        "dedupe_key": dedupe_key(name) if is_ea else "",
        "product_key": product_key(name) if is_ea else "",
        "version": extract_version(name) if is_ea else None,
    }
