"""Read an EA's real input labels out of its MQL5 source.

MT5 builds its parameter dialog from the .ex5, where the labels and enum names
have been compressed beyond reach - so this page shows "LotCalcMode = 0" where
the terminal shows "Lot calculation mode: BASE_FIXED_LOTS". When an EA ships
its source, though, all of it is right there in plain text:

    input ENUM_LOT_MODE LotCalcMode = BASE_FIXED_LOTS; // Lot calculation mode

and the enum it refers to names its own members. That is not a guess or a
convention: it is the same text the compiler turned into the dialog, so a label
read this way is exactly what MT5 shows.

Nothing here runs the source or trusts it. It is read as text, and only two
things are taken from it: what an input is called, and what its enum members
are. Everything else in the file is ignored.
"""
from __future__ import annotations

import re

# input int Foo = 3;            // Some label
# sinput group "Money"
# extern double Bar = 0.1;      // Another label
INPUT_RE = re.compile(
    r"^\s*(?:extern|s?input)\s+"                 # the declaration keyword
    r"(?:const\s+)?"
    r"([A-Za-z_][A-Za-z0-9_]*)\s+"               # type
    r"([A-Za-z_][A-Za-z0-9_]*)\s*"               # name
    r"(?:=\s*([^;]*?))?\s*;"                     # optional default
    r"(?:\s*//\s*(.*))?$",                       # optional label comment
    re.MULTILINE)

GROUP_RE = re.compile(r"^\s*(?:s?input)\s+group\s+\"([^\"]*)\"", re.MULTILINE)

ENUM_RE = re.compile(r"\benum\s+([A-Za-z_][A-Za-z0-9_]*)\s*\{([^}]*)\}", re.S)

# One member per line is how MQL5 enums are usually written, and a comment on
# that line is the member's label. Splitting the whole body on commas instead
# cut through "// Lot per equity step" and turned the member into the tail of
# somebody's sentence - so lines first, then commas inside the code part.
MEMBER_RE = re.compile(r"^([A-Za-z_][A-Za-z0-9_]*)\s*(?:=\s*(-?\d+))?$")


LABEL_MAX = 120
NOTE_MAX = 300


def _strip_comments(text: str) -> str:
    """Block comments only. Line comments carry the labels and must survive."""
    return re.sub(r"/\*.*?\*/", " ", text, flags=re.S)


def parse_enums(text: str) -> dict[str, list[tuple[int, str, str]]]:
    """enum name -> [(value, member, label)], numbering the way C does."""
    out: dict[str, list[tuple[int, str, str]]] = {}
    for name, body in ENUM_RE.findall(text):
        members: list[tuple[int, str, str]] = []
        nxt = 0
        for line in body.splitlines():
            code, _, label = line.partition("//")
            label = label.strip()
            parts = [x.strip() for x in code.split(",") if x.strip()]
            for at, part in enumerate(parts):
                m = MEMBER_RE.match(part)
                if not m:
                    continue
                member, explicit = m.group(1), m.group(2)
                value = int(explicit) if explicit is not None else nxt
                nxt = value + 1
                # a trailing comment belongs to the last member on its line
                members.append((value, member, label if at == len(parts) - 1 else ""))
        if members:
            out[name] = members
    return out


def parse_inputs(source: str) -> dict[str, dict]:
    """input name -> {label, note}, where note spells out an enum's choices.

    The note is written in the form the page already understands for building a
    dropdown - "0 = BASE_FIXED_LOTS (fixed lots), 1 = ..." - so a source-read
    enum becomes the same named dropdown MT5 shows.
    """
    text = _strip_comments(source)
    enums = parse_enums(text)
    out: dict[str, dict] = {}

    for kind, name, _default, comment in INPUT_RE.findall(text):
        if kind == "group" or name == "group":
            continue
        label = (comment or "").strip().strip("/").strip()
        members = enums.get(kind) or []
        note = ""
        if members:
            note = ", ".join(
                f"{value} = {member}" + (f" ({label_})" if label_ else "")
                for value, member, label_ in members[:12])
        row = {"label": label[:LABEL_MAX], "note": note[:NOTE_MAX]}
        if row["label"] or row["note"]:
            out[name] = row
    return out


def verify_against(parsed: dict, keys) -> tuple[bool, str]:
    """Is this source actually the EA whose settings we are labelling?

    A name can match two different robots, and labelling one EA with another's
    dialog would be worse than showing raw keys - it would be confidently wrong
    about what a number does. The check is the input names themselves: most of
    what the EA reports has to appear in the source, or the file is rejected.
    """
    have = {str(k) for k in keys if str(k).strip()}
    if not have:
        return False, "no settings to check the source against"
    if not parsed:
        return False, "no inputs found in that source file"
    hit = len(have & set(parsed)) / len(have)
    if hit < 0.6:
        return False, (f"that source declares different settings"
                       f" ({int(hit * 100)}% of this EA's names appear in it)")
    return True, f"{int(hit * 100)}% of this EA's settings appear in the source"
