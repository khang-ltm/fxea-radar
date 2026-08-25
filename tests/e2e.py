"""End-to-end check: parse -> store -> merge -> HTTP API -> file download.

Runs against a throwaway data dir (tests/_tmp) so the real store is untouched.
No Telegram connection needed; messages are synthetic but shaped like real posts.

  .venv\\Scripts\\python.exe -m tests.e2e            # one pass
  .venv\\Scripts\\python.exe -m tests.e2e --loop 5   # repeat, must pass every time
"""
from __future__ import annotations

import argparse
import json
import shutil
import sys
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TMP = ROOT / "tests" / "_tmp"

# Point config at the throwaway dir before importing anything that reads it.
sys.path.insert(0, str(ROOT))

failures: list[str] = []


def check(cond: bool, label: str) -> None:
    print(("  PASS  " if cond else "  FAIL  ") + label)
    if not cond:
        failures.append(label)


SAMPLES = [
    (
        1001,
        """🔥 FREE EA — GOLD SCALPER PRO v3.2 🔥
Expert Advisor for MT4

RULES:
1. Only trade XAUUSD on M15
2. Min deposit: $500
3. Leverage 1:500 required
4. Use ECN account
Broker: Exness
Download: https://mega.nz/file/abc123""",
        [{"name": "GoldScalperPro.ex4", "size_bytes": 1234, "mime": "", "local_path": None}],
    ),
    (1002, "XAUUSD BUY NOW 2650 TP 2660 SL 2645", []),
    (
        1003,
        """NEW ROBOT: TrendMaster EA
How to use:
- Attach to EURUSD H1 chart
- Recommended balance 1000 USD
- Risk 2% per trade
MT5 only.""",
        [{"name": "TrendMaster_EA.ex5", "size_bytes": 4321, "mime": "", "local_path": None}],
    ),
    # The real @free_fx_pro drop template, promo footer included.
    (
        1004,
        """💥Ultimate Breakout System v6.2 with sets MT5

🏦Seller

 🗂Category : EA

💱XAUUSD GOLD

⏱D1

 ▶️VantageMarkets

💵Minimum deposit: $200

 ⚡️WE GIVE OUR ROBOFOREX CLIENTS 85% OF OUR AFFILIATE COMMISSION USING OUR IB ==> puqz

📈 ROBOTEST

🔥Best VPS for Forex

✅Subscribe for success

⚠️Beware of scammers

👨‍💻 Admin @Feedback_fx""",
        [{"name": "Ultimate Breakout System v6.2 @free_fx_pro.rar", "size_bytes": 2048, "mime": "", "local_path": None}],
    ),
    # Same product reposted a day later -> must collapse into 1004.
    (
        1005,
        """💥Ultimate Breakout System v6.2 MT5

 🗂Category : EA

💱XAUUSD

⏱D1

📈 ROBOTEST""",
        [{"name": "Ultimate Breakout System v6.2 @free_fx_pro.rar", "size_bytes": 2048, "mime": "", "local_path": None}],
    ),
    # Human chat that mentions EAs but delivers nothing -> must NOT be an EA drop.
    (1006, "Any suggestions for scalping ea ? my mt5 robot keeps losing on gold", []),
]


def build_posts(store, parse_message, now_iso: str) -> list[dict]:
    posts = []
    for mid, text, files in SAMPLES:
        rec = {
            "key": f"testchan:{mid}",
            "channel": "testchan",
            "channel_title": "TEST CHAN",
            "message_id": mid,
            "message_ids": [mid],
            "date": 1_700_000_000 + mid,
            "date_iso": "2023-11-14T22:13:20+00:00",
            "url": f"https://t.me/testchan/{mid}",
            "text": text,
            "files": files,
            "has_photo": False,
            "views": mid,
            "forwards": 0,
            "first_seen": now_iso,
        }
        posts.append({**rec, **parse_message(text, files)})
    return posts


def check_page_script(html: str) -> None:
    """Parse the page's inline JS with node. A syntax error blanks the whole list,
    but still serves HTTP 200 - nothing else in this suite would notice."""
    import re
    import shutil
    import subprocess

    node = shutil.which("node")
    m = re.search(r"<script>(.*?)</script>", html, re.S)
    if not m:
        check(False, "page has an inline script")
        return
    if not node:
        print("  SKIP  page script syntax (node not on PATH)")
        return
    tmp = TMP / "page.mjs"
    tmp.write_text(m.group(1), encoding="utf-8")
    proc = subprocess.run([node, "--check", str(tmp)], capture_output=True, text=True)
    err = (proc.stderr or "").strip().splitlines()
    check(proc.returncode == 0, f"page script parses ({err[-1] if proc.returncode else 'ok'})")


def http_json(url: str):
    with urllib.request.urlopen(url, timeout=10) as r:
        return r.status, json.loads(r.read().decode("utf-8"))


def run_once(run_no: int, port: int) -> None:
    print(f"\n=== e2e pass {run_no} (port {port}) ===")

    # 1. isolated data dir
    if TMP.exists():
        shutil.rmtree(TMP)
    TMP.mkdir(parents=True)

    from app import config

    config.DATA_DIR = TMP
    config.POSTS_FILE = TMP / "posts.json"
    config.STATE_FILE = TMP / "state.json"
    config.FILES_DIR = TMP / "files"
    config.FILES_DIR.mkdir(parents=True, exist_ok=True)
    config.PORT = port

    from app import store
    from app.parse import parse_message

    # 2. parse
    posts = build_posts(store, parse_message, "2026-01-01T00:00:00+00:00")
    gold = posts[0]
    check(gold["is_ea"] is True, "gold EA post classified as EA")
    check("XAUUSD" in gold["pairs"], "XAUUSD pair extracted")
    check("M15" in gold["timeframes"], "M15 timeframe extracted")
    check(gold["deposit"] == "$500", f"deposit extracted (got {gold['deposit']!r})")
    check(gold["leverage"] == "1:500", f"leverage extracted (got {gold['leverage']!r})")
    check(len(gold["rules"]) >= 4, f"rules extracted (got {len(gold['rules'])})")
    check(any("mega.nz" in l for l in gold["links"]), "download link extracted")
    check("has-ea-file" in gold["tags"], "has-ea-file tag set")
    check(posts[1]["is_ea"] is False, "pure signal post rejected as non-EA")
    check("EURUSD" in posts[2]["pairs"] and "H1" in posts[2]["timeframes"], "EURUSD/H1 from second EA")

    # channel drop template
    tpl = posts[3]
    check(tpl["name"] == "Ultimate Breakout System v6.2 MT5", f"template name cleaned (got {tpl['name']!r})")
    check(tpl["is_ea"] is True, "template post classified as EA")
    check(tpl["pairs"] == ["XAUUSD"], f"template pair line parsed (got {tpl['pairs']})")
    check(tpl["timeframes"] == ["D1"], f"template timeframe parsed (got {tpl['timeframes']})")
    check(tpl["deposit"] == "$200", f"template deposit parsed (got {tpl['deposit']!r})")
    check("template-ea" in tpl["tags"], "template category recognised as EA")
    promo = " ".join(tpl["rules"]).lower()
    check("affiliate" not in promo and "vps" not in promo and "scammer" not in promo,
          f"promo footer stripped from rules (got {tpl['rules']})")
    clean_lower = tpl["text_clean"].lower()
    for junk in ("affiliate commission", "best vps", "subscribe for success",
                 "beware of scammers", "admin @", "robotest", "category :"):
        check(junk not in clean_lower, f"promo line removed from displayed message: {junk!r}")
    check("ultimate breakout system" in clean_lower, "product line survives promo stripping")
    check("vantagemarkets" not in " ".join(tpl["rules"]).lower(), "broker list is not shown as a rule")
    check(all("t.me" not in l for l in tpl["links"]), "cross-promo t.me links not treated as downloads")

    # chat noise about EAs delivers nothing -> rejected
    chat = posts[5]
    check(chat["is_ea"] is False, f"EA chatter without payload rejected (score {chat['ea_score']})")

    # dedupe identity
    from app.parse import dedupe_key
    check(posts[3]["dedupe_key"] == posts[4]["dedupe_key"] != "", "repost shares the dedupe key")
    check(dedupe_key("EA Avengers v3.7 MT5") == dedupe_key("Avengers v3.7 @free_fx_pro"),
          "same product, different watermark -> same key")
    check(dedupe_key("Vision v2 MT5") != dedupe_key("Vision v3 MT5"), "different versions stay distinct")

    # spacing variants are the same product; look-alikes are not
    from app.grouping import _contained, _tokens, squash
    check(squash("3x combo ai") == squash("3xcomboai"), "spacing variants squash to the same key")
    check(squash("ultimate breakout system") != squash("ultimatum breakout"),
          "look-alike names do NOT squash together")
    check(not _contained(_tokens("ultimate breakout system"), _tokens("ultimatum breakout")),
          "containment guard rejects unrelated look-alikes")

    from app.server import dedupe as dedupe_posts
    newest_first = sorted(posts, key=lambda p: -p["date"])
    uniq = dedupe_posts([p for p in newest_first if p["is_ea"]])
    check(len(uniq) == 3, f"dedupe collapses the repost (got {len(uniq)} unique)")
    kept = next(p for p in uniq if p["dedupe_key"] == posts[3]["dedupe_key"])
    check(kept["message_id"] == 1005, f"newest copy kept (got {kept['message_id']})")
    check(kept["reposts"] == 1, f"repost counted (got {kept['reposts']})")

    # 3. store round-trip
    merged, added, updated = store.merge_posts([], posts)
    check(added == len(posts) and updated == 0, f"first merge adds all ({added} added)")
    store.save_posts(merged)
    reloaded = store.load_posts()
    check(len(reloaded) == len(posts), "posts survive save/load")
    check(reloaded[0]["date"] >= reloaded[-1]["date"], "store sorted newest-first")

    # 4. idempotency: same posts again -> nothing new
    merged2, added2, updated2 = store.merge_posts(reloaded, posts)
    check(added2 == 0 and updated2 == 0, f"re-merge is idempotent ({added2} added, {updated2} updated)")

    # 5. edit detection
    edited = [{**posts[0], "text": posts[0]["text"] + "\nUPDATE: now works on M30 too"}]
    _, added3, updated3 = store.merge_posts(reloaded, edited)
    check(added3 == 0 and updated3 == 1, f"edited post detected as update ({updated3})")

    # 6. a real downloaded file, for the /files/ route
    chan_dir = config.FILES_DIR / "testchan"
    chan_dir.mkdir(parents=True, exist_ok=True)
    (chan_dir / "1001_GoldScalperPro.ex4").write_bytes(b"MZ fake ea binary")
    posts[0]["files"][0]["local_path"] = "testchan/1001_GoldScalperPro.ex4"
    store.save_posts(store.merge_posts([], posts)[0])
    store.save_state({"channels": {"testchan": {"last_id": 1003}}, "last_sync": "2026-01-01T00:00:00+00:00"})

    # 7. HTTP layer
    import importlib

    from app import server as server_mod

    importlib.reload(server_mod)  # rebind module-level config references
    server_mod.config = config
    from http.server import HTTPServer

    httpd = HTTPServer(("127.0.0.1", port), server_mod.Handler)
    t = threading.Thread(target=httpd.serve_forever, daemon=True)
    t.start()
    time.sleep(0.4)
    try:
        status, data = http_json(f"http://127.0.0.1:{port}/api/posts")
        check(status == 200, "GET /api/posts -> 200")
        check(data["ea_only"] is True and data["grouped"] is True and data["kind"] == "ea",
              "EA-only + grouped + kind=ea are the API defaults")
        check(all(p["is_ea"] for p in data["posts"]), "no non-EA post leaks into default view")
        check(all(p.get("kind", "ea") == "ea" for p in data["posts"]), "indicators excluded from the EA view")
        # 3 EA robots in SAMPLES; the v6.2 repost groups into its product
        check(len(data["posts"]) == 3, f"default returns one entry per product ({len(data['posts'])})")
        check(data["collapsed"] == 1, f"API reports 1 grouped post (got {data['collapsed']})")
        check(data["truncated"] is False, "nothing truncated at this size")

        ubs = next((p for p in data["posts"] if "Ultimate Breakout" in p["name"]), None)
        check(ubs is not None and ubs.get("version_count") == 1,
              f"the two v6.2 posts group as one version (got {ubs and ubs.get('version_count')})")
        check(ubs is not None and ubs.get("reposts") == 1, f"repost counted on the product (got {ubs and ubs.get('reposts')})")

        status, ungrouped = http_json(f"http://127.0.0.1:{port}/api/posts?group=0")
        check(len(ungrouped["posts"]) == 4, f"group=0 shows every EA post ({len(ungrouped['posts'])})")

        status, all_resp = http_json(f"http://127.0.0.1:{port}/api/posts?ea=0&group=0&kind=all")
        check(len(all_resp["posts"]) == len(SAMPLES), f"ea=0&kind=all returns everything ({len(all_resp['posts'])})")

        status, one = http_json(f"http://127.0.0.1:{port}/api/posts?ea=0&group=0&kind=all&limit=1")
        check(len(one["posts"]) == 1, "limit honoured")
        check(one["matched"] == len(SAMPLES) and one["truncated"] is True, "truncation reported honestly")
        check(one["posts"][0]["date"] == max(p["date"] for p in posts), "limit keeps the newest posts")

        check(data["summary"]["ea_count"] == 4, f"summary ea_count == 4 (got {data['summary']['ea_count']})")
        check(data["summary"]["total"] == len(SAMPLES), "summary counts the whole store, not the page")
        check("XAUUSD" in data["summary"]["pairs"], "summary lists XAUUSD facet")
        check("M15" in data["summary"]["timeframes"], "summary lists M15 facet")

        status, sdata = http_json(f"http://127.0.0.1:{port}/api/state")
        check(status == 200 and "state" in sdata, "GET /api/state -> 200")

        with urllib.request.urlopen(f"http://127.0.0.1:{port}/", timeout=10) as r:
            html = r.read().decode("utf-8")
        check(r.status == 200 and "FX EA Radar" in html, "GET / serves dashboard")
        check("/api/posts" in html and 'id="grid"' in html, "dashboard wired to API")
        check('id="syncbox"' in html, "dashboard shows sync status")
        # the ban is on a manual CATALOG sync trigger, not on POST in general -
        # unloading an EA legitimately posts to the agent
        check('id="sync"' not in html and "'/api/sync'" not in html,
              "no manual catalog sync control (auto-sync only)")
        check("prefers-reduced-motion" in html, "dashboard respects reduced motion")
        check("verdicts refreshed" in html or "verdicts not refreshed" in html,
              "dashboard reports verdict-index freshness")
        check("renderStaleness" in html and "STALE_WARN_H" in html,
              "dashboard warns when the data goes stale")
        check(".stale[hidden]" in html and ".stale:not([hidden])" in html,
              "stale banner respects [hidden] (a bare display:flex leaves an empty bar)")
        check(".toolbar[hidden]" in html and ".toolbar:not([hidden])" in html,
              "EA filter bar respects [hidden] so it disappears on the MT5 tab")
        check(".btn[hidden]" in html and ".btn:not([hidden])" in html,
              "buttons respect [hidden] (Undo changes must vanish when nothing changed)")
        for el in ("eadlg", "ea-save", "ea-inputs"):
            check(f'id="{el}"' in html, f"EA settings dialog has #{el}")
        check('id="tab-mt5"' in html and 'id="view-mt5"' in html and "showTab" in html,
              "MT5 is a tab in this page, not a separate page")
        check("dialog.sheet[open]" in html and "\ndialog.sheet {" in html.replace("\r", ""),
              "sheet display is scoped to [open] for the same reason")
        check("VERDICT_RANK" in html and 'value="verdict"' in html,
              "dashboard can sort by test verdict")

        # static export must work off the SAME page, with data inlined and no polling
        from app.export_static import render
        static_html = render({"posts": [], "matched": 0, "summary": {}, "state": {}, "sync": {}})
        check("window.__FXEA__" in static_html, "static build inlines the payload")
        check("realFetch" in static_html and "/api/posts" in static_html,
              "static build answers only the catalog fetch, passing others through")
        check("setInterval(load," not in static_html, "static build drops the polling timer")
        check("id=\"grid\"" in static_html and "detailHtml" in static_html,
              "static build reuses the real UI (no second dashboard to maintain)")

        # the verdict index must be on the auto-sync cycle, not manual-only
        from app import config as cfg
        from app.server import _auto_sync_loop, do_shots_blocking
        from app.sync_shots import run_shots
        import inspect
        loop_src = inspect.getsource(_auto_sync_loop)
        check(callable(run_shots) and callable(do_shots_blocking), "verdict refresh is callable in-process")
        check("_refresh_shots" in loop_src, "auto-sync cycle refreshes verdicts")
        check(cfg.SHOTS_EVERY_N_SYNCS >= 1, f"verdict refresh cadence set (every {cfg.SHOTS_EVERY_N_SYNCS} cycles)")
        check('id="detail"' in html and "detailHtml" in html, "dashboard has the detail sheet")
        check("dialog.sheet[open]" in html,
              "detail sheet only displays when open (a bare display:flex leaves it stuck open)")
        check("body.modal-open" in html and "classList.add('modal-open')" in html,
              "opening the sheet locks page scroll")
        check("addEventListener('close'" in html and "remove('modal-open')" in html,
              "scroll lock is released on close and on Esc")
        check_page_script(html)
        check_agent_names()
        check_trade_path()
        check_no_control_chars()

        status, tagged = http_json(f"http://127.0.0.1:{port}/api/posts?ea=0&group=0&kind=all&tag=has-ea-file")
        check(status == 200, "GET /api/posts?tag=… -> 200")
        check(tagged["tag"] == "has-ea-file", "tag echoed back")
        check(len(tagged["posts"]) >= 1 and all("has-ea-file" in p["tags"] for p in tagged["posts"]),
              f"tag filter returns only tagged posts ({len(tagged['posts'])})")

        status, none_tag = http_json(f"http://127.0.0.1:{port}/api/posts?ea=0&group=0&kind=all&tag=does-not-exist")
        check(none_tag["posts"] == [] and none_tag["matched"] == 0, "unknown tag yields an empty, honest result")

        with urllib.request.urlopen(
            f"http://127.0.0.1:{port}/files/testchan/1001_GoldScalperPro.ex4", timeout=10
        ) as r:
            body = r.read()
        check(r.status == 200 and body == b"MZ fake ea binary", "GET /files/<x> serves attachment")

        # path traversal must be refused
        code = None
        try:
            urllib.request.urlopen(f"http://127.0.0.1:{port}/files/%2e%2e%2f%2e%2e%2f.env", timeout=10)
        except urllib.error.HTTPError as e:
            code = e.code
        check(code == 403, f"path traversal refused (got {code})")

        code = None
        try:
            urllib.request.urlopen(f"http://127.0.0.1:{port}/files/missing.ex4", timeout=10)
        except urllib.error.HTTPError as e:
            code = e.code
        check(code == 404, f"missing file -> 404 (got {code})")
    finally:
        httpd.shutdown()
        httpd.server_close()



def check_agent_names() -> None:
    """Every plain function call in the agent must resolve to something real.

    A hand-written `_ensure()` that never existed shipped and turned /api/symbols
    into a 502 - the endpoint was only reachable on the VPS, so nothing here
    caught it. This is cheap and would have.
    """
    import ast
    import builtins

    src = (ROOT / "app" / "mt5_agent.py").read_text(encoding="utf-8")
    tree = ast.parse(src)

    known = {n.name for n in ast.walk(tree) if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))}
    known |= {n.name for n in ast.walk(tree) if isinstance(n, ast.ClassDef)}
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            known |= {(a.asname or a.name.split(".")[0]) for a in node.names}
        elif isinstance(node, ast.ImportFrom):
            known |= {(a.asname or a.name) for a in node.names}
        elif isinstance(node, ast.Assign):
            known |= {t.id for t in node.targets if isinstance(t, ast.Name)}

    called = {n.func.id for n in ast.walk(tree)
              if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)}
    unknown = sorted(n for n in called - known if not hasattr(builtins, n))
    check(not unknown, f"agent calls only functions that exist{'' if not unknown else ': missing ' + ', '.join(unknown)}")


def check_trade_path() -> None:
    """The agent may cancel a pending order and do nothing else to a trade.

    Cancelling pendings is the one trade request this agent can send, and it was
    added on purpose: a stopped EA leaves orders that still fill. Everything else
    - opening, closing, modifying a position - must stay impossible, so that a
    stolen token can never move money. This asserts it in the source rather than
    trusting a comment: every order_send must be TRADE_ACTION_REMOVE.
    """
    import ast

    src = (ROOT / "app" / "mt5_agent.py").read_text(encoding="utf-8")
    tree = ast.parse(src)

    sends, actions = 0, set()
    for node in ast.walk(tree):
        if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)):
            continue
        if node.func.attr not in ("order_send", "order_check", "Close", "Buy", "Sell"):
            continue
        sends += 1
        for arg in node.args:
            if not isinstance(arg, ast.Dict):
                actions.add("not a literal request")
                continue
            for k, v in zip(arg.keys, arg.values):
                if isinstance(k, ast.Constant) and k.value == "action":
                    actions.add(ast.unparse(v).split(".")[-1])

    check(sends <= 1, f"the agent sends at most one kind of trade request (found {sends})")
    check(actions <= {"TRADE_ACTION_REMOVE"},
          "the only trade request is TRADE_ACTION_REMOVE"
          + ("" if actions <= {"TRADE_ACTION_REMOVE"} else f" - found {sorted(actions)}"))


def check_no_control_chars() -> None:
    """No stray control bytes in source files.

    Writing patches through Python string literals has now put a backspace into a
    regex and a BEL into a Windows path, both of which only showed up when a user
    ran the result. A byte scan costs nothing and catches every repeat.
    """
    for rel in ("public/index.html", "app/mt5_agent.py", "app/installer.py",
                "install_vps.ps1", "mql5/FxeaManager.mq5"):
        f = ROOT / rel
        if not f.exists():
            continue
        text = f.read_text(encoding="utf-8", errors="replace")
        bad = sorted({hex(ord(c)) for c in text if ord(c) < 32 and c not in "\n\r\t"})
        check(not bad, f"{rel} has no stray control bytes{'' if not bad else ' (found ' + ', '.join(bad) + ')'}")

def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--loop", type=int, default=1, help="how many times to run the whole suite")
    args = ap.parse_args()

    for i in range(1, args.loop + 1):
        run_once(i, 8900 + i)

    print("\n" + "=" * 60)
    if failures:
        print(f"FAILED: {len(failures)} check(s)")
        for f in dict.fromkeys(failures):
            print("  -", f)
        return 1
    print(f"ALL CHECKS PASSED across {args.loop} pass(es)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
