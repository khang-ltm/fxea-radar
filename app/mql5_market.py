"""Crawl the MQL5 Market for MT5 Experts, and rank them by their live signal.

mql5.com answers 403 to everything that is not a browser on a home connection:
the Market listing, a product page, the Signals section, even the RSS feeds,
whatever user-agent you send. That is Cloudflare, not a header check, so there
is no header to fix. r.jina.ai renders the page server-side and hands back
Markdown, and that is the only way in from a GitHub runner or from the VPS.

What the Market itself tells you is nearly worthless as evidence. A star rating
is a number sellers can and do buy; `5 (4)` means four people clicked five
stars. The one thing on mql5.com that is measured rather than claimed is the
**live signal** an author links from the product description: a real account,
updated by the terminal, with growth, drawdown, trade count and profit factor
that nobody can type in by hand.

So this module crawls in three hops - listing, product page, signal page - and
ranks on the signal. An EA with no signal is not ranked badly; it is marked
unproven, which is a different and more honest thing to say.
"""
from __future__ import annotations

import json
import re
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone

from . import config

# Everything goes through the reader. Only public mql5.com pages are ever sent
# to it - no account data, no token, nothing from the agent.
READER = "https://r.jina.ai/"
BASE = "https://www.mql5.com"
LIST_URL = BASE + "/en/market/mt5/expert"

UA = "fxea-radar/1.0 (+https://github.com/)"

PRODUCT_RE = re.compile(r"\[([^\]]*)\]\(https://www\.mql5\.com/\w\w/market/product/(\d+)")
SIGNAL_RE = re.compile(r"https://www\.mql5\.com/\w\w/signals/(\d+)")
RATING_RE = re.compile(r"^(\d(?:\.\d+)?)\s*\((\d+)\)$")
PRICE_RE = re.compile(r"^([\d\s.,]+)\s*USD$")


def _num(text):
    """A number out of MQL5's formatting: '1 199.99', '_22.58_', '14.02%'."""
    if text is None:
        return None
    cleaned = re.sub(r"[^\d.\-]", "", str(text).replace(" ", " ").replace(" ", ""))
    if cleaned in {"", "-", ".", "-."}:
        return None
    try:
        return float(cleaned)
    except ValueError:
        return None


def fetch(url: str, timeout: int = 60) -> str | None:
    """One page through the reader, or None. Never raises: a crawl must not take
    the whole sync down because one product page timed out."""
    req = urllib.request.Request(READER + url, headers={
        "User-Agent": UA,
        "Accept": "text/plain, text/markdown, */*",
        # ask for the rendered text, not the raw HTML the reader would otherwise
        # have to guess about
        "X-Return-Format": "markdown",
    })
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.read().decode("utf-8", errors="replace")
    except (urllib.error.URLError, urllib.error.HTTPError, OSError, TimeoutError):
        return None


# -- parsing ---------------------------------------------------------------

def parse_list(text: str) -> list[dict]:
    """Every product on a Market listing page.

    The reader flattens each card to: a title link, the author on its own line,
    the rating, the category link, the blurb, then the price linking back to the
    same product. Walking from one title link to its price is therefore enough
    to bound a card without guessing at blank lines.
    """
    lines = text.splitlines()
    titles: list[tuple[int, str, str]] = []
    for i, line in enumerate(lines):
        for name, pid in PRODUCT_RE.findall(line):
            name = name.strip()
            if not name or name.startswith("!["):
                continue
            if PRICE_RE.match(name) or name == "Free":
                continue           # that is the price link closing an earlier card
            titles.append((i, pid, name))
            break                  # one card may repeat its logo; the first wins

    out, seen = [], set()
    for n, (start, pid, name) in enumerate(titles):
        if pid in seen:
            continue
        seen.add(pid)
        stop = titles[n + 1][0] if n + 1 < len(titles) else len(lines)
        entry = {"id": pid, "name": name, "url": f"{BASE}/en/market/product/{pid}",
                 "author": "", "rating": None, "reviews": 0, "price": None,
                 "free": False, "blurb": ""}
        for line in lines[start + 1:stop]:
            line = line.strip()
            if not line:
                continue
            hit = RATING_RE.match(line)
            if hit:
                entry["rating"] = float(hit.group(1))
                entry["reviews"] = int(hit.group(2))
                continue
            for label, lpid in PRODUCT_RE.findall(line):
                if lpid != pid:
                    continue
                label = label.strip()
                if label == "Free":
                    entry["free"] = True
                money = PRICE_RE.match(label)
                if money:
                    entry["price"] = _num(money.group(1))
            if line.startswith(("!", "[", "*", "#", "|", "-")):
                continue
            if not entry["author"]:
                entry["author"] = line[:80]
            elif len(line) > len(entry["blurb"]):
                entry["blurb"] = line[:600]
        out.append(entry)
    return out


def parse_product(text: str, pid: str | None = None) -> dict:
    """Published/updated dates, activations, and - the point of the hop - which
    live signals the author linked."""
    info: dict = {"signals": [], "published": "", "updated": "", "activations": None,
                  "demos": None, "version": ""}
    for line in text.splitlines():
        line = line.strip().strip("*").strip()
        for key, field in (("Published:", "published"), ("Updated:", "updated"),
                           ("Current version:", "version"), ("Version:", "version")):
            if line.startswith(key) and not info[field]:
                info[field] = line[len(key):].strip()
        if line.startswith("Activations:"):
            info["activations"] = _num(line)
        if line.startswith("Demo downloaded:"):
            info["demos"] = _num(line)
    info["signals"] = _own_signals(text, pid)
    return info


def _own_signals(text: str, pid: str | None) -> list[str]:
    """Only the signals this product links, not every signal on the page.

    A product page carries a whole shelf of OTHER sellers' products underneath
    the description, each with its own blurb quoting its own live signal. Taking
    the signal links in document order therefore ranked one EA on another EA's
    account - the first page tried quoted four signals and three belonged to
    strangers. The product's own description is the stretch between its `# Name`
    heading and the first link to a DIFFERENT product.
    """
    lines = text.splitlines()
    start = 0
    for i, line in enumerate(lines):
        if line.startswith("# "):
            start = i
            break
    stop = len(lines)
    for i in range(start + 1, len(lines)):
        others = {found for _label, found in PRODUCT_RE.findall(lines[i])}
        if others - {pid or ""}:
            stop = i
            break
    seen: list[str] = []
    for sid in SIGNAL_RE.findall(chr(10).join(lines[start:stop])):
        if sid not in seen:
            seen.append(sid)
    return seen


# The reader serves a signal page in either of two layouts - one with the full
# site navigation and colon-suffixed labels, one stripped. Labels are matched
# with the colon and the whitespace taken off so both read the same.
_SIGNAL_FIELDS = {
    "growth": ("growth_pct", "num"),
    "profit": ("profit", "num"),
    "equity": ("equity", "num"),
    "balance": ("balance", "num"),
    "initial deposit": ("deposit", "num"),
    "deposits": ("deposits", "num"),
    "withdrawals": ("withdrawals", "num"),
    "weeks": ("weeks", "num"),
    "started": ("started", "text"),
    "subscribers": ("subscribers", "num"),
    "trading days": ("trading_days", "num"),
    "trades": ("trades", "num"),
    "profit factor": ("profit_factor", "num"),
    "sharpe ratio": ("sharpe", "num"),
    "recovery factor": ("recovery", "num"),
    "trades per week": ("per_week", "num"),
    "max deposit load": ("deposit_load", "num"),
    "monthly growth": ("monthly_pct", "num"),
    "annual forecast": ("annual_pct", "num"),
    "algo trading": ("algo_pct", "num"),
    "latest trade": ("latest_trade", "text"),
    "avg holding time": ("avg_hold", "text"),
}


def _label(line: str) -> str:
    """A line reduced to the key it might be. Bullets stay unreduced on purpose:
    the chart tab strip is `*   Growth`, and matching that would read the tab
    name as the growth figure."""
    if line.startswith(("*", "-", "#", "[", "!", "|")):
        return ""
    return line.strip().rstrip(":").strip().lower()


def parse_signal(text: str) -> dict:
    """The measured numbers. These are what ranking is allowed to use."""
    lines = [l.rstrip() for l in text.splitlines()]
    out: dict = {}

    for line in lines[:4]:
        if line.startswith("Title:"):
            out["name"] = line.split(":", 2)[-1].strip()

    # label on one line, value on the next non-empty one. Walk pairs rather than
    # regexing the blob: "Profit", "Profit Trades" and "Gross Profit" all exist
    # and only position tells them apart.
    for i, line in enumerate(lines):
        spec = _SIGNAL_FIELDS.get(_label(line))
        if not spec:
            continue
        field, how = spec
        if out.get(field) is not None:
            continue
        for value in lines[i + 1:i + 4]:
            if not value.strip():
                continue
            # "18 (75.00%)" is one number and one aside; without the cut
            # the two run together and 18 trading days reads as 1875.
            out[field] = _num(value.split("(")[0]) if how == "num" else value.strip()
            break

    # The stripped layout has no "Growth:" row - it puts the same number in a
    # headline sentence instead.
    if out.get("growth_pct") is None:
        for line in lines[:40]:
            hit = re.match(r"growth since (\d{4})\s+([\-\d.,]+)\s*%", line.strip(), re.I)
            if hit:
                out["since"] = int(hit.group(1))
                out["growth_pct"] = _num(hit.group(2))
                break

    # "Profit Trades:" reads "155 (75.60%)" - the share is the useful half
    for i, line in enumerate(lines):
        if _label(line) != "profit trades":
            continue
        for value in lines[i + 1:i + 4]:
            if not value.strip():
                continue
            pct = re.search(r"\(([\d.,]+)%\)", value)
            if pct:
                out["win_pct"] = _num(pct.group(1))
            break
        break

    # "Maximal:  _22.58_ USD (14.02%)" under "Drawdown by balance" - the number
    # that says whether a growth figure was earned or gambled for.
    for i, line in enumerate(lines):
        if "Drawdown by balance" not in line:
            continue
        for j in range(i, min(i + 20, len(lines))):
            if _label(lines[j]) != "maximal":
                continue
            for value in lines[j + 1:j + 4]:
                if not value.strip():
                    continue
                pct = re.search(r"\(([\d.,]+)%\)", value)
                out["dd_usd"] = _num(value.split("(")[0])
                out["dd_pct"] = _num(pct.group(1)) if pct else None
                break
            break
        break

    # An account that has been topped up cannot be read as a track record: its
    # growth percentage is measured against a deposit that kept moving. One of
    # the first eight crawled showed 233%/month on an account that started with
    # $287 and had $7,834 paid into it - real growth, 0.65%.
    # The stripped layout carries no "Weeks:" row, and the reader picks a layout
    # per fetch, so age would otherwise appear and vanish between crawls. Trades
    # over trades-per-week gives the same answer within a week or so, and a
    # scored EA must not change rank because of which page we happened to get.
    if out.get("weeks") is None and out.get("trades") and out.get("per_week"):
        out["weeks"] = round(out["trades"] / out["per_week"])
        out["weeks_estimated"] = True

    deposit = out.get("deposit") or 0
    deposits = out.get("deposits") or 0
    out["top_ratio"] = round(deposits / deposit, 2) if deposit else None
    out["topped_up"] = bool(deposit and deposits > deposit * 0.1)
    out["url"] = ""
    return out


# -- ranking ---------------------------------------------------------------

def score(entry: dict) -> tuple[int, list[str]]:
    """Points and the reason for each one, so the page can show its working.

    Harsh about what empties accounts rather than what looks bad in a brochure.
    Three things get checked before a growth figure is believed at all:

    - **Top-ups.** A percentage is growth against a deposit, so an account that
      keeps being paid into has no meaningful percentage. One crawled signal
      advertised 233%/month having started at $287 with $7,834 paid in; its
      real growth was 0.65%.
    - **Age.** Four weeks of profit is a sample, not a record. Every martingale
      looks perfect right up to the week it does not.
    - **Drawdown.** Growth divided by how deep the account went to get it is the
      only form of the number worth ranking on.
    """
    sig = entry.get("signal") or {}
    reasons: list[str] = []
    points = 0

    def add(n: int, why: str) -> None:
        nonlocal points
        points += n
        reasons.append(("+" if n > 0 else "") + str(n) + " " + why)

    if not sig.get("trades"):
        return 0, ["no live signal - nothing here is measured"]

    trades = sig.get("trades") or 0
    weeks = sig.get("weeks")
    dd = sig.get("dd_pct")
    growth = sig.get("growth_pct")
    monthly = sig.get("monthly_pct")

    # -- is the record readable at all --------------------------------------
    # Derived here rather than read from the signal: a crawl stored by an older
    # version has the deposits but not the ratio, and a missing ratio must not
    # quietly turn the strong verdict into the mild one.
    deposit, paid_in = sig.get("deposit") or 0, sig.get("deposits") or 0
    ratio = (paid_in / deposit) if deposit else None
    if sig.get("topped_up"):
        paid = f"${sig.get('deposits') or 0:,.0f} paid in on a ${sig.get('deposit') or 0:,.0f} start"
        if ratio is not None and ratio >= 1:
            # More money was added than the account began with, so the growth
            # line is not a track record of anything. A signal advertising
            # 233%/month turned out to have started at $287 with $7,834 paid in;
            # its real growth was 0.65%.
            add(-7, f"topped up - {paid} - so its growth % is not a track record")
        else:
            # A small addition flatters the percentage without invalidating it.
            add(-2, f"topped up ({paid}), which flatters the growth %")
    if weeks is not None:
        if weeks >= 52:
            add(3, f"{int(weeks)} weeks live - a real record")
        elif weeks >= 26:
            add(2, f"{int(weeks)} weeks live")
        elif weeks >= 12:
            add(1, f"{int(weeks)} weeks live")
        else:
            add(-5, f"only {int(weeks)} weeks old - too new to have proved anything")

    # -- what it earned, against what it risked ------------------------------
    believable = (ratio is None or ratio < 1) and (weeks is None or weeks >= 8)
    if growth is not None and dd:
        if growth < 0:
            add(-6, f"the account is down {abs(growth):.0f}%")
        elif growth == 0:
            add(-4, "the account has not grown")
        elif not believable:
            add(0, f"up {growth:.0f}% with a {dd:.0f}% drawdown - not yet worth counting")
        elif growth / dd >= 3:
            add(5, f"up {growth:.0f}% with a worst drawdown of only {dd:.0f}%")
        elif growth / dd >= 1:
            add(3, f"up {growth:.0f}% against a {dd:.0f}% drawdown")
        else:
            add(1, f"up {growth:.0f}%, but it went {dd:.0f}% underwater to do it")
    elif monthly is not None and believable:
        add(1, f"{monthly:.1f}%/month so far")

    if dd is not None and dd >= 50:
        add(-6, f"{dd:.0f}% underwater at worst - one bad week from zero")
    elif dd is not None and dd >= 30:
        add(-3, f"drawdown reached {dd:.0f}%")

    # -- how it trades -------------------------------------------------------
    if trades >= 300:
        add(3, f"{int(trades)} trades - enough to mean something")
    elif trades >= 100:
        add(2, f"{int(trades)} trades on the signal")
    elif trades < 30:
        add(-3, f"only {int(trades)} trades - too few to judge")

    pf = sig.get("profit_factor")
    if pf is not None:
        if pf >= 1.5:
            add(2, f"profit factor {pf:.2f}")
        elif pf < 1.1:
            add(-3, f"profit factor {pf:.2f} - barely above break-even")

    load = sig.get("deposit_load")
    if load is not None and load >= 50:
        add(-4, f"{load:.0f}% of the deposit rides in one basket - grid behaviour")
    elif load is not None and load >= 25:
        add(-1, f"{load:.0f}% max deposit load")

    rec = sig.get("recovery")
    if rec is not None and rec >= 5:
        add(1, f"recovery factor {rec:.1f}")

    algo = sig.get("algo_pct")
    if algo is not None and algo < 80:
        add(-2, f"only {algo:.0f}% of the trading is automated - a human is helping")

    # -- what the shop says, worth the least ---------------------------------
    if (entry.get("reviews") or 0) >= 30 and (entry.get("rating") or 0) >= 4.5:
        add(1, f"{entry['rating']} from {entry['reviews']} reviews")
    if (entry.get("reviews") or 0) <= 3:
        add(-1, "almost no reviews yet")

    return points, reasons


# -- crawl -----------------------------------------------------------------

def crawl(limit: int = 60, budget: int = 160, delay: float = 2.5,
          previous: dict | None = None, max_age_hours: int = 72,
          log=print) -> dict:
    """Listing -> product pages -> signal pages, inside a request budget.

    Each hop costs one request through a free, rate-limited reader, so anything
    fetched recently is reused from the last crawl: a refresh then spends its
    budget on products it has never seen instead of re-reading the same signal
    twice a day.
    """
    previous = previous or {}
    cache = {e["id"]: e for e in (previous.get("eas") or []) if e.get("id")}
    now = datetime.now(timezone.utc)
    spent = 0

    def fresh(entry: dict) -> bool:
        stamp = entry.get("checked_at")
        if not stamp:
            return False
        try:
            when = datetime.fromisoformat(stamp)
        except ValueError:
            return False
        if when.tzinfo is None:
            when = when.replace(tzinfo=timezone.utc)
        return (now - when).total_seconds() < max_age_hours * 3600

    log(f"[market] listing {LIST_URL}")
    page = fetch(LIST_URL)
    spent += 1
    if not page:
        # Keep yesterday's answer rather than publishing an empty tab: the
        # reader rate-limits, and a blank Market reads as "there are no good
        # EAs" instead of "the crawl did not run".
        log("[market] listing unreachable - keeping the previous crawl")
        stale = dict(previous)
        stale["stale"] = True
        return stale

    listed = parse_list(page)[:limit]
    log(f"[market] {len(listed)} experts listed")

    out = []
    for entry in listed:
        old = cache.get(entry["id"]) or {}
        if fresh(old):
            merged = {**old, **entry, "signal": old.get("signal"),
                      "product": old.get("product"), "checked_at": old.get("checked_at")}
            out.append(merged)
            continue
        if spent >= budget:
            # Out of requests: carry whatever is known so the entry still shows
            # up, just without fresh numbers.
            out.append({**old, **entry} if old else entry)
            continue

        time.sleep(delay)
        text = fetch(entry["url"])
        spent += 1
        entry["product"] = parse_product(text, entry["id"]) if text else {}
        entry["signal"] = None

        for sid in (entry["product"].get("signals") or [])[:1]:
            if spent >= budget:
                break
            time.sleep(delay)
            stext = fetch(f"{BASE}/en/signals/{sid}")
            spent += 1
            if not stext:
                continue
            sig = parse_signal(stext)
            if sig.get("trades"):
                sig["url"] = f"{BASE}/en/signals/{sid}"
                entry["signal"] = sig
            break

        entry["checked_at"] = now.isoformat()
        out.append(entry)
        log(f"[market] {entry['name'][:40]:<40} "
            f"{'signal' if entry.get('signal') else 'no signal'}  ({spent} requests)")

    for entry in out:
        entry["score"], entry["why"] = score(entry)
        entry["proven"] = bool((entry.get("signal") or {}).get("trades"))

    out.sort(key=lambda e: (e.get("proven", False), e.get("score", 0)), reverse=True)
    return {"eas": out, "crawled_at": now.isoformat(), "requests": spent,
            "proven": sum(1 for e in out if e.get("proven")), "stale": False}


def public_view(data: dict, limit: int = 80) -> dict:
    """What the page needs, and nothing else.

    The crawl keeps the whole product blurb and every signal id it saw, which is
    useful when debugging a parse and dead weight in a static page that gets
    downloaded by every visitor.
    """
    out = []
    for e in (data.get("eas") or [])[:limit]:
        sig = e.get("signal") or {}
        prod = e.get("product") or {}
        out.append({
            "id": e.get("id"), "name": e.get("name"), "url": e.get("url"),
            "author": e.get("author"), "price": e.get("price"), "free": e.get("free"),
            "rating": e.get("rating"), "reviews": e.get("reviews"),
            "blurb": (e.get("blurb") or "")[:260],
            "score": e.get("score", 0), "why": e.get("why") or [],
            "proven": bool(e.get("proven")),
            "updated": prod.get("updated") or "", "published": prod.get("published") or "",
            "signal": {k: sig.get(k) for k in (
                "url", "growth_pct", "dd_pct", "monthly_pct", "trades", "win_pct",
                "profit_factor", "weeks", "weeks_estimated", "deposit", "deposits",
                "topped_up", "deposit_load", "recovery", "algo_pct", "per_week",
                "latest_trade", "subscribers", "started",
            )} if sig else None,
        })
    return {"eas": out, "crawled_at": data.get("crawled_at"),
            "proven": data.get("proven", 0), "stale": bool(data.get("stale"))}


def main() -> None:
    import argparse

    from .store import load_market, save_market

    ap = argparse.ArgumentParser(description="Crawl the MQL5 Market for MT5 Experts")
    ap.add_argument("--limit", type=int, default=60, help="how many listed experts to keep")
    ap.add_argument("--budget", type=int, default=160, help="max requests through the reader")
    ap.add_argument("--delay", type=float, default=2.5, help="seconds between requests")
    ap.add_argument("--max-age", type=int, default=72, help="hours before an entry is re-fetched")
    ap.add_argument("--dry-run", action="store_true", help="print, do not write the store")
    args = ap.parse_args()

    data = crawl(limit=args.limit, budget=args.budget, delay=args.delay,
                 previous=load_market(), max_age_hours=args.max_age)
    if args.dry_run:
        print(json.dumps(data, indent=2)[:4000])
        return
    save_market(data)
    print(f"[market] {len(data.get('eas') or [])} experts, "
          f"{data.get('proven', 0)} with a live signal, "
          f"{data.get('requests', 0)} requests")


if __name__ == "__main__":
    main()
