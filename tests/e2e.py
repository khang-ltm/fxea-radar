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
        # the EA catalog and MQL5 Market tabs were shut down on 2026-10-23: the
        # page is the MT5 control panel only and asks for no catalog data
        check("/api/posts" not in html and 'id="grid"' not in html and 'id="syncbox"' not in html,
              "the page no longer loads or shows the catalog")
        check('id="tab-eas"' not in html and 'id="view-eas"' not in html
              and 'id="tab-market"' not in html and 'id="view-market"' not in html,
              "the catalog and Market tabs are gone")
        check("const TABS = ['mt5', 'myea', 'report'];" in html,
              "My MT5, My EA and Report are the only tabs")
        # the Report tab reads; it must have no way to send anything
        rpt = html.rsplit("/* ── Report tab", 1)[-1].split("/* ── tabs ──", 1)[0]   # the script, not the CSS
        check(len(rpt) < len(html) and "loadReport" in rpt
              and not any(w in rpt for w in ("POST", "method:", "managerPost", "managerAction", "/api/manager")),
              "the Report tab only GETs (no POST, no manager call)")
        check('id="sync"' not in html and "'/api/sync'" not in html,
              "no manual catalog sync control")
        check("prefers-reduced-motion" in html, "dashboard respects reduced motion")
        check(".btn[hidden]" in html and ".btn:not([hidden])" in html,
              "buttons respect [hidden] (Undo changes must vanish when nothing changed)")
        for el in ("eadlg", "ea-save", "ea-inputs"):
            check(f'id="{el}"' in html, f"EA settings dialog has #{el}")
        check('id="tab-mt5"' in html and 'id="view-mt5"' in html and "showTab" in html,
              "MT5 is a tab in this page, not a separate page")
        check("dialog.sheet[open]" in html and "\ndialog.sheet {" in html.replace("\r", ""),
              "sheet display is scoped to [open] for the same reason")

        # static export works off the SAME page and bakes in no catalog data
        import os as _os
        from app.export_static import render
        _was = _os.environ.get("MT5_AGENT_URL")
        _os.environ["MT5_AGENT_URL"] = "https://agent.example.test/"
        try:
            static_html = render()
        finally:
            if _was is None:
                _os.environ.pop("MT5_AGENT_URL", None)
            else:
                _os.environ["MT5_AGENT_URL"] = _was
        check("window.__FXEA__" not in static_html and "realFetch" not in static_html,
              "static build bakes in no catalog payload and stubs no fetch")
        check("window.__MT5_URL__ = 'https://agent.example.test';" in static_html,
              "static build bakes in the agent hostname, trailing slash trimmed")
        check('id="view-mt5"' in static_html and "renderMt5" in static_html,
              "static build reuses the real UI (no second dashboard to maintain)")

        # the scheduled Telegram sync and Market crawl are off; the publish is not
        wf = (ROOT / ".github" / "workflows" / "sync.yml").read_text(encoding="utf-8")
        check("schedule:" not in wf and "cron:" not in wf, "the workflow runs on no schedule")
        check("app.sync" not in wf and "app.mql5_market" not in wf,
              "the workflow neither syncs Telegram nor crawls the Market")
        check("push:" in wf and "workflow_dispatch:" in wf and "python -m app.export_static" in wf
              and "pages deploy site" in wf,
              "a push to main still builds and publishes the site")

        # the verdict index must be on the auto-sync cycle, not manual-only
        from app import config as cfg
        from app.server import _auto_sync_loop, do_shots_blocking
        from app.sync_shots import run_shots
        import inspect
        loop_src = inspect.getsource(_auto_sync_loop)
        check(callable(run_shots) and callable(do_shots_blocking), "verdict refresh is callable in-process")
        check("_refresh_shots" in loop_src, "auto-sync cycle refreshes verdicts")
        check(cfg.SHOTS_EVERY_N_SYNCS >= 1, f"verdict refresh cadence set (every {cfg.SHOTS_EVERY_N_SYNCS} cycles)")
        check("dialog.sheet[open]" in html,
              "a sheet only displays when open (a bare display:flex leaves it stuck open)")
        check("body.modal-open" in html and "classList.add('modal-open')" in html,
              "opening the sheet locks page scroll")
        check("addEventListener('close'" in html and "remove('modal-open')" in html,
              "scroll lock is released on close and on Esc")
        check_page_script(html)
        check_agent_names()
        check_agent_imports()
        check_ex5_meta()
        check_version_report()
        check_manager_versions()
        check_bom_tolerant_sha()
        check_no_shadowed_locals()
        check_trade_path()
        check_setsltp()
        check_close_cancel_tickets()
        check_contract_size()
        check_bars()
        check_first_leg()
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


def check_manager_versions() -> None:
    """The manager carries its version twice: MANAGER_VERSION, which it reports
    over the API, and #property version, which MT5 and the .ex5 header show.
    They drifted once (1.32 vs 1.31) and made a stale build look current."""
    import re as _re
    src = open("mql5/FxeaManager.mq5", encoding="utf-8").read()
    a = _re.search(r'#define MANAGER_VERSION "([^"]+)"', src)
    b = _re.search(r'#property version\s+"([^"]+)"', src)
    check(bool(a and b) and a.group(1) == b.group(1),
          f"manager version strings agree ({a and a.group(1)} / {b and b.group(1)})")


def check_bom_tolerant_sha() -> None:
    import pathlib as _p, tempfile
    from app import config as _cfg
    from app import mt5_agent as ag
    old = _cfg.ROOT
    with tempfile.TemporaryDirectory() as d:
        _cfg.ROOT = _p.Path(d); (_p.Path(d) / "data").mkdir()
        try:
            (_p.Path(d) / "data" / ".agent_version").write_text("\ufeff4a301af\r\n", encoding="utf-8")
            check(ag._disk_sha() == "4a301af", "a version marker written by PowerShell (BOM, CRLF) reads cleanly")
        finally:
            _cfg.ROOT = old


def check_version_report() -> None:
    """The System check must not cry wolf about a restart that worked.

    It compares what the process booted as against what is on disk. Pinning the
    boot version before promoting the staged update made every successful
    restart read as a failed one.
    """
    import pathlib as _p
    import tempfile

    from app import config as _cfg
    from app import mt5_agent as ag

    src = _p.Path("app/mt5_agent.py").read_text(encoding="utf-8")
    body = src[src.index("def main() -> None:"):]
    check(body.index("_promote_pending()") < body.index("_BOOTED_SHA = _disk_sha()"),
          "startup promotes the staged update before pinning what it runs")

    old_root = _cfg.ROOT
    with tempfile.TemporaryDirectory() as d:
        _cfg.ROOT = _p.Path(d)
        (_p.Path(d) / "data").mkdir()
        ver, pend = _p.Path(d) / "data" / ".agent_version", _p.Path(d) / "data" / ".agent_pending"
        try:
            # a restart that worked: the new process promotes, and both agree
            ver.write_text("old1234"); pend.write_text("new5678")
            ag._promote_pending()
            check(ag._disk_sha() == "new5678" and ag._newest_on_disk() == "new5678",
                  "after a good restart, running and on-disk agree")
            # a process left behind: update staged, never promoted
            ver.write_text("old1234"); pend.write_text("new5678")
            check(ag._newest_on_disk() == "new5678" and ag._disk_sha() == "old1234",
                  "a stuck process is still caught: the staged update counts as on disk")
        finally:
            _cfg.ROOT = old_root


def check_ex5_meta() -> None:
    """The .ex5 reader, against the real files in the store when they are there.

    The body of an .ex5 is encrypted - 7.99 bits of entropy per byte - so the
    only thing to verify is that the three clear-text fields still come out and
    that nothing else is mistaken for them.
    """
    import pathlib as _p

    from app import ex5_meta

    check(ex5_meta.read("README.md").get("ok") is False, "a non-.ex5 file is refused")
    check(ex5_meta.read("data/files/does-not-exist.ex5").get("ok") is False,
       "a missing file is refused rather than raising")

    files = sorted(_p.Path("data/files").rglob("*.ex5"))
    if not files:
        check(True, "no .ex5 in the store to read (skipped)")
        return
    read = [ex5_meta.read(f) for f in files]
    check(all(m.get("ok") for m in read), f"all {len(read)} stored .ex5 files parse")
    check(any(m.get("version") for m in read), "at least one reports a compiled version")
    for m, f in zip(read, files):
        v = m.get("version") or ""
        check(len(v) <= 24 and all(c.isprintable() for c in v),
           f"{f.name[:28]}: version {v!r} is a plausible string")


def check_agent_imports() -> None:
    """The agent module has to import. That is not a given.

    A default argument is evaluated when the def is read, so a constant defined
    below a function that defaults to it raises NameError at import - and an
    agent that cannot import cannot self-update, which makes it the one failure
    the watchdog cannot restart its way out of. Someone has to go to the VPS.
    Importing here costs a second and catches every shape of it.
    """
    import importlib
    import types

    sys.modules.setdefault("MetaTrader5", types.ModuleType("MetaTrader5"))
    broken = ""
    for name in ("app.mt5_agent", "app.basket_dd", "app.buzz", "app.mq5_inputs",
                 "app.installer", "app.grouping", "app.export_static"):
        try:
            importlib.import_module(name)
        except Exception as exc:                               # noqa: BLE001
            broken = f"{name}: {type(exc).__name__}: {exc}"
            break
    check(not broken, "every agent module imports" + (f" - {broken}" if broken else ""))


def check_no_shadowed_locals() -> None:
    """No function may reuse a name it already bound to something callable.

    read_history defines a helper called window(), and a later edit added a
    variable called window - so "week": window(7) became 'int' object is not
    callable and the whole history endpoint returned 502. Nothing in the syntax
    check, the import check or the AST name check can see it, because every name
    does exist; the bug is that one of them ate the other.
    """
    import ast

    src = (ROOT / "app" / "mt5_agent.py").read_text(encoding="utf-8")
    tree = ast.parse(src)
    clashes = []
    for fn in [n for n in ast.walk(tree)
               if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))]:
        defined = {inner.name: inner.lineno for inner in fn.body
                   if isinstance(inner, (ast.FunctionDef, ast.AsyncFunctionDef))}
        if not defined:
            continue
        for node in ast.walk(fn):
            if not isinstance(node, ast.Assign):
                continue
            for target in node.targets:
                if isinstance(target, ast.Name) and target.id in defined:
                    clashes.append(f"{fn.name}(): {target.id} reassigned at line {node.lineno}"
                                   f", shadowing the helper defined at {defined[target.id]}")
    check(not clashes, "no local helper is shadowed by a later assignment"
          + ("" if not clashes else " - " + "; ".join(clashes)))


_TRADE_CALLS = ("order_send", "order_check")
_TRADE_FORBIDDEN = ("Close", "Buy", "Sell")         # MetaTrader5's own shortcuts
_REQUEST_WRITES_OK = ("type_filling",)              # the one key close may vary


def trade_path_problems(src: str) -> list[str]:
    """Every way `src` can reach order_send/order_check that this test cannot
    read as REMOVE, or DEAL / SLTP naming an existing position. Empty is safe.

    A call must be `mt5.order_send(<request>)` with nothing else on it, and the
    request a dict literal - written there, or bound once to a name in the same
    function, or `dict(name)` of one. Anything else that touches order_send (an
    alias, a getattr string, an import) is a problem, and so is anything that
    could change the request after it was read: a key written, an update(),
    dict(base, **kw), ** unpacking, a second binding, passing it anywhere.
    """
    import ast

    tree = ast.parse(src)
    lines = src.splitlines()
    parent = {}
    for n in ast.walk(tree):
        for c in ast.iter_child_nodes(n):
            parent[c] = n

    def where(node):
        return f"line {node.lineno}: {lines[node.lineno - 1].strip()[:60]}"

    def scope_of(node):
        while node in parent:
            node = parent[node]
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
                return node
        return tree

    def is_mt5(node):
        return isinstance(node, ast.Name) and node.id == "mt5"

    def resolve(scope, name, allowed, chain):
        """The dict literal `name` was bound to in `scope`, or (None, why)."""
        if name in chain:
            return None, f"{name} is built from itself"
        chain.append(name)
        binds = [n for n in ast.walk(scope)
                 if (isinstance(n, ast.Name) and n.id == name
                     and isinstance(n.ctx, (ast.Store, ast.Del)))
                 or (isinstance(n, ast.arg) and n.arg == name)]
        if len(binds) != 1:
            return None, f"{name} is bound {len(binds)} times in its function"
        b = binds[0]
        stmt = parent.get(b)
        if not (isinstance(stmt, ast.Assign) and stmt.targets == [b]):
            return None, f"{name} is not bound by a plain assignment"
        value = stmt.value
        if isinstance(value, ast.Dict):
            return value, ""
        if isinstance(value, ast.Call) and isinstance(value.func, ast.Name) \
                and value.func.id == "dict" and len(value.args) == 1 \
                and not value.keywords and isinstance(value.args[0], ast.Name):
            allowed.add(id(value.args[0]))
            return resolve(scope, value.args[0].id, allowed, chain)
        return None, f"{name} is not a dict literal or dict() of one"

    bad = []
    for node in ast.walk(tree):
        if isinstance(node, ast.alias) and node.name.split(".")[-1] in _TRADE_CALLS:
            bad.append(f"{node.name} is imported by name")
        elif isinstance(node, ast.Name) and node.id in _TRADE_CALLS:
            bad.append(f"{where(node)} - a bare {node.id}")
        elif isinstance(node, ast.Constant) and node.value in _TRADE_CALLS:
            bad.append(f"{where(node)} - {node.value} named as a string")
        elif isinstance(node, ast.Attribute) and node.attr in _TRADE_FORBIDDEN:
            bad.append(f"{where(node)} - {node.attr} is never allowed")
        elif isinstance(node, ast.Attribute) and node.attr in _TRADE_CALLS:
            call = parent.get(node)
            if not (is_mt5(node.value) and isinstance(call, ast.Call) and call.func is node):
                bad.append(f"{where(node)} - {node.attr} used other than as mt5.{node.attr}(...)")
                continue
            if len(call.args) != 1 or call.keywords or isinstance(call.args[0], ast.Starred):
                bad.append(f"{where(node)} - not exactly one request argument")
                continue
            arg, scope = call.args[0], scope_of(call)
            if isinstance(arg, ast.Dict):
                req, why, names = arg, "", []
            elif isinstance(arg, ast.Name):
                allowed, names = {id(arg)}, []
                req, why = resolve(scope, arg.id, allowed, names)
                # every other read of these names could alias or change the request
                for n in ast.walk(scope):
                    if not (isinstance(n, ast.Name) and n.id in names
                            and isinstance(n.ctx, ast.Load)) or id(n) in allowed:
                        continue
                    up = parent.get(n)
                    if isinstance(up, ast.Subscript) and up.value is n \
                            and isinstance(up.ctx, ast.Store) \
                            and isinstance(up.slice, ast.Constant) \
                            and up.slice.value in _REQUEST_WRITES_OK:
                        continue
                    why = why or f"{n.id} is used or changed at line {n.lineno}"
            else:
                req, why = None, "request is not a literal this test can read"
            if why or req is None:
                bad.append(f"{where(node)} - {why or 'request is not a literal'}")
                continue
            keys = [k.value if isinstance(k, ast.Constant) else None for k in req.keys]
            if None in keys:
                bad.append(f"{where(node)} - a key that is not a plain string (** or computed)")
                continue
            if len(set(keys)) != len(keys):
                bad.append(f"{where(node)} - a key given twice")
                continue
            vals = dict(zip(keys, req.values))
            act = vals.get("action")
            action = act.attr if isinstance(act, ast.Attribute) and is_mt5(act.value) else ""
            if action == "TRADE_ACTION_REMOVE":
                continue
            if action in ("TRADE_ACTION_DEAL", "TRADE_ACTION_SLTP"):
                if "position" not in vals:
                    bad.append(f"{where(node)} - {action} without a position")
                elif isinstance(vals["position"], ast.Constant):
                    bad.append(f"{where(node)} - {action} on a literal position"
                               f" {vals['position'].value!r}")
                continue                                   # on what already exists
            bad.append(f"{where(node)} - {action or 'no readable mt5.TRADE_ACTION_*'}")
    return bad


# Each of these must be caught. The first one passed the old version of the check.
_TRADE_MUTANTS = {
    "an alias of order_send": (
        "def f(mt5):\n    s = mt5.order_send\n"
        "    s({'action': mt5.TRADE_ACTION_DEAL, 'symbol': 'X', 'volume': 1.0})\n"),
    "order_send through getattr": (
        "def f(mt5):\n    getattr(mt5, 'order_send')({'action': mt5.TRADE_ACTION_DEAL})\n"),
    "order_send imported by name": "from MetaTrader5 import order_send\n",
    "a request key rewritten": (
        "def f(mt5, t):\n    req = {'action': mt5.TRADE_ACTION_SLTP, 'position': t}\n"
        "    req['action'] = mt5.TRADE_ACTION_PENDING\n    mt5.order_send(req)\n"),
    "a request key deleted": (
        "def f(mt5, t):\n    req = {'action': mt5.TRADE_ACTION_DEAL, 'position': t}\n"
        "    del req['position']\n    mt5.order_send(req)\n"),
    "a request update()": (
        "def f(mt5, t):\n    base = {'action': mt5.TRADE_ACTION_DEAL, 'position': t}\n"
        "    req = dict(base)\n    req.update(position=0)\n    mt5.order_send(req)\n"),
    "dict(base, **override)": (
        "def f(mt5, t):\n    base = {'action': mt5.TRADE_ACTION_DEAL, 'position': t}\n"
        "    req = dict(base, action=mt5.TRADE_ACTION_PENDING)\n    mt5.order_send(req)\n"),
    "** unpacking in the literal": (
        "def f(mt5, t, kw):\n    mt5.order_send({'action': mt5.TRADE_ACTION_REMOVE, **kw})\n"),
    "a literal position 0": (
        "def f(mt5):\n    mt5.order_send({'action': mt5.TRADE_ACTION_DEAL, 'position': 0,"
        " 'symbol': 'X', 'volume': 1.0})\n"),
    "a key given twice": (
        "def f(mt5, t, u):\n    mt5.order_send({'action': mt5.TRADE_ACTION_SLTP,"
        " 'position': t, 'position': u})\n"),
    "a request name bound twice": (
        "def f(mt5, t, other):\n    req = {'action': mt5.TRADE_ACTION_REMOVE, 'order': t}\n"
        "    req = other\n    mt5.order_send(req)\n"),
    "a request built in another function": (
        "def a(mt5, t):\n    req = {'action': mt5.TRADE_ACTION_REMOVE, 'order': t}\n"
        "def b(mt5, req):\n    mt5.order_send(req)\n"),
    "a request aliased and changed": (
        "def f(mt5, t):\n    req = {'action': mt5.TRADE_ACTION_REMOVE, 'order': t}\n"
        "    r2 = req\n    r2['action'] = mt5.TRADE_ACTION_DEAL\n    mt5.order_send(req)\n"),
    "a keyword request": (
        "def f(mt5, t):\n    mt5.order_send(request={'action': mt5.TRADE_ACTION_REMOVE})\n"),
    "a pending order": (
        "def f(mt5):\n    mt5.order_send({'action': mt5.TRADE_ACTION_PENDING, 'symbol': 'X'})\n"),
    "a deal without a position": (
        "def f(mt5):\n    mt5.order_send({'action': mt5.TRADE_ACTION_DEAL, 'symbol': 'X'})\n"),
    "mt5.Buy": "def f(mt5):\n    mt5.Buy('XAUUSD', 0.01)\n",
}

_TRADE_GOOD = (
    "def f(mt5, t):\n"
    "    base = {'action': mt5.TRADE_ACTION_DEAL, 'position': t, 'symbol': 'X'}\n"
    "    for fill in (1, 2):\n"
    "        req = dict(base)\n"
    "        req['type_filling'] = fill\n"
    "        mt5.order_send(req)\n"
    "    mt5.order_send({'action': mt5.TRADE_ACTION_REMOVE, 'order': t})\n"
    "    mt5.order_send({'action': mt5.TRADE_ACTION_SLTP, 'position': t, 'sl': 0.0})\n")


def check_trade_path() -> None:
    """The agent may remove a pending order, close a position, or set SL/TP on
    an open position. Nothing else.

    All three were added on purpose - a stopped EA leaves orders that still
    fill, there has to be a way out of a trade from the page, and a break-even
    TP is the cheapest way out of all. Each acts only on something that exists.
    Nothing can open a trade, add volume, or place or modify an order. With the
    PIN, SL/TP on an open position can be changed, including widening or
    removing a stop - that is the owner's accepted risk (2026-10-22). This
    asserts it in the source rather than trusting a comment: every order_send is
    TRADE_ACTION_REMOVE, or TRADE_ACTION_DEAL / TRADE_ACTION_SLTP naming an
    existing position.
    """
    check(not trade_path_problems(_TRADE_GOOD),
          "trade-path check accepts the allowed shapes"
          f" ({trade_path_problems(_TRADE_GOOD)})")
    for label, snippet in _TRADE_MUTANTS.items():
        check(bool(trade_path_problems(snippet)), f"trade-path check catches {label}")

    bad = trade_path_problems((ROOT / "app" / "mt5_agent.py").read_text(encoding="utf-8"))
    check(not bad,
          "every trade request removes something that exists"
          + ("" if not bad else " - " + "; ".join(bad)))


class _FakeMT5:
    """Just enough of the MetaTrader5 module for the trade paths, with made-up
    positions. Every order_send is recorded and nothing is ever sent anywhere."""

    POSITION_TYPE_BUY, POSITION_TYPE_SELL = 0, 1
    TRADE_ACTION_DEAL, TRADE_ACTION_SLTP, TRADE_ACTION_REMOVE = 1, 6, 8
    TRADE_RETCODE_DONE = 10009
    ORDER_TYPE_BUY, ORDER_TYPE_SELL = 0, 1

    def __init__(self):
        from types import SimpleNamespace as NS
        self.NS = NS
        self.sent, self.info_calls, self.code = [], 0, 10009
        self.positions = {
            101: NS(ticket=101, type=0, symbol="XAUUSD", sl=2600.0, tp=0.0, magic=1, comment="",
                    volume=0.01, price_open=2660.0, price_current=2640.0, profit=-1.0,
                    swap=0.0, time=0),
            102: NS(ticket=102, type=1, symbol="XAUUSD", sl=2700.0, tp=2500.0, magic=1, comment="",
                    volume=0.01, price_open=2620.0, price_current=2640.3, profit=-1.0,
                    swap=0.0, time=0),
            103: NS(ticket=103, type=0, symbol="EURUSD", sl=0.0, tp=0.0, magic=2, comment="",
                    volume=0.01, price_open=1.1, price_current=1.1, profit=0.0, swap=0.0, time=0),
        }
        self.symbols = {
            "XAUUSD": NS(digits=2, point=0.01, trade_stops_level=0, trade_contract_size=100.0),
            "EURUSD": NS(digits=5, point=0.00001, trade_stops_level=30,
                         trade_contract_size=100000.0),
            "ZEROSZ": NS(digits=2, point=0.01, trade_stops_level=0, trade_contract_size=0.0),
        }
        self.ticks = {"XAUUSD": NS(bid=2640.00, ask=2640.30), "EURUSD": NS(bid=1.10000, ask=1.10010)}

    def positions_get(self, ticket=None):
        if ticket is None:
            return tuple(self.positions.values())
        return (self.positions[ticket],) if ticket in self.positions else ()

    def orders_get(self):
        return ()

    def account_info(self):
        return None

    def terminal_info(self):
        return None

    def symbol_info(self, symbol):
        self.info_calls += 1
        return self.symbols.get(symbol)

    def symbol_info_tick(self, symbol):
        return self.ticks.get(symbol)

    def order_send(self, req):
        self.sent.append(dict(req))
        return self.NS(retcode=self.code, comment="done" if self.code == 10009 else "Invalid stops")

    # made-up OHLC bars for the /api/bars checks; copy_rates_range answers
    # nothing, like a terminal that has not selected the symbol
    TIMEFRAME_M1, TIMEFRAME_M5, TIMEFRAME_M15, TIMEFRAME_M30 = 1, 5, 15, 30
    TIMEFRAME_H1, TIMEFRAME_H4, TIMEFRAME_D1 = 16385, 16388, 16408

    def symbol_select(self, symbol, enable):
        self.selected = getattr(self, "selected", []) + [symbol]
        return True

    def _bars(self, n):
        return [{"time": 1700000000 + 900 * i, "open": 2000.0 + 1.5 * i, "high": 2001.5 + 1.5 * i,
                 "low": 1999.0 + 1.5 * i, "close": 2000.5 + 1.5 * i, "tick_volume": 10 + i}
                for i in range(n)]

    def copy_rates_range(self, symbol, tf, start, end):
        return ()

    def copy_rates_from(self, symbol, tf, end, count):
        self.asked = count
        return self._bars(min(count, 6))

    def copy_rates_from_pos(self, symbol, tf, pos, count):
        return self._bars(min(count, 6))


def check_setsltp() -> None:
    """setsltp against a fake terminal: what it sends, and what it refuses to."""
    from app import mt5_agent as ag

    fake = _FakeMT5()
    old = ag._connect
    ag._connect = lambda: fake

    def run(**item):
        fake.sent.clear()
        out = ag.set_sltp([item])
        return out, out["results"][0] if out.get("results") else {}

    try:
        out, row = run(ticket=101, tp=2650.123, sl=None)
        req = fake.sent[0] if len(fake.sent) == 1 else {}
        check(set(req) == {"action", "position", "symbol", "sl", "tp"}
              and req["action"] == fake.TRADE_ACTION_SLTP and req["position"] == 101
              and req["symbol"] == "XAUUSD",
              "setsltp sends one TRADE_ACTION_SLTP naming the position, nothing else")
        check(req.get("tp") == 2650.12, f"setsltp rounds to the symbol's digits (sent tp {req.get('tp')})")
        check(req.get("sl") == 2600.0, f"setsltp: sl null keeps the live SL (sent sl {req.get('sl')})")
        check(row.get("ok") is True and row.get("retcode") == 10009 and row.get("tp") == 2650.12
              and row.get("sl") == 2600.0 and out["changed"] == 1 and out["ok"] is True,
              "setsltp reports ok, retcode and the new sl/tp per ticket")

        for label, item, word in (
                ("buy TP below the bid", dict(ticket=101, tp=2630.0), "above"),
                ("buy SL above the bid", dict(ticket=101, sl=2645.0), "below"),
                ("sell TP above the ask", dict(ticket=102, tp=2645.0), "below"),
                ("sell SL below the ask", dict(ticket=102, sl=2630.0), "above"),
                ("TP inside the stops level", dict(ticket=103, tp=1.10020), "at least 30"),
                ("SL inside the stops level", dict(ticket=103, sl=1.09990), "at least 30"),
                ("a negative TP", dict(ticket=101, tp=-1), "negative"),
                ("a NaN SL", dict(ticket=101, sl=float("nan")), "finite"),
                ("a string TP", dict(ticket=101, tp="2650"), "not a price"),
                ("a boolean SL", dict(ticket=101, sl=True), "not a price"),
                ("neither TP nor SL", dict(ticket=101), "nothing to change"),
                ("the values already set", dict(ticket=102, tp=2500.0, sl=2700.0), "already set")):
            out, row = run(**item)
            check(not fake.sent and row.get("ok") is False and word in (row.get("error") or "")
                  and out["ok"] is False,
                  f"setsltp refuses {label} without sending ({row.get('error')!r})")

        out, row = run(ticket=103, tp=1.10030)
        check(len(fake.sent) == 1 and fake.sent[0]["tp"] == 1.1003 and fake.sent[0]["sl"] == 0.0,
              "setsltp sends a TP exactly at the stops level")
        out, row = run(ticket=102, tp=2600.0)
        check(len(fake.sent) == 1 and fake.sent[0]["tp"] == 2600.0 and fake.sent[0]["sl"] == 2700.0,
              "setsltp sends a sell TP below the ask, keeping its SL")
        out, row = run(ticket=101, sl=0)
        check(len(fake.sent) == 1 and fake.sent[0]["sl"] == 0.0 and fake.sent[0]["tp"] == 0.0,
              "setsltp: 0 removes the SL")
        out, row = run(ticket=999, tp=2650.0)
        check(not fake.sent and out["already_gone"] == [999] and row.get("ok") is False
              and out["ok"] is True and not out["failed"],
              "setsltp reports a closed ticket and sends nothing for it")

        fake.code = 10016
        out, row = run(ticket=101, tp=2650.0)
        check(len(fake.sent) == 1 and row.get("ok") is False and row.get("retcode") == 10016
              and "Invalid stops" in (row.get("comment") or "") and out["ok"] is False,
              "setsltp reports MT5's refusal with its retcode and comment")
        fake.code = 10009

        fake.sent.clear()
        out = ag.set_sltp([{"ticket": 101, "tp": 2650.0}, {"ticket": 102, "tp": 2601.0},
                           {"ticket": 101, "tp": 2660.0}])
        dup = [r for r in out["results"] if r.get("ticket") == 101]
        check([q["position"] for q in fake.sent] == [102] and len(dup) == 2
              and all("more than once" in (r.get("error") or "") and not r["ok"] for r in dup),
              f"setsltp refuses both entries of a ticket listed twice, sends the rest"
              f" (sent {[q['position'] for q in fake.sent]})")

        log = (ag.config.DATA_DIR / "manager.log")

        def last_audit():
            text = log.read_text(encoding="utf-8").strip().splitlines() if log.exists() else []
            return json.loads(text[-1]) if text else {}

        # one bad item in a batch: judged before anything is sent, never a crash
        fake.sent.clear()
        try:
            out = ag.set_sltp([{"ticket": 101, "tp": 2650.0}, {"ticket": 102, "tp": 10 ** 400}])
        except Exception as exc:                               # noqa: BLE001
            out = {"crashed": repr(exc)}
        bad = (out.get("results") or [{}, {}])[1]
        check(len(fake.sent) == 1 and fake.sent[0]["position"] == 101
              and "too large to be a price" in (bad.get("error") or "")
              and [s["ticket"] for s in last_audit().get("fields", {}).get("setsltp", [])] == [101],
              f"setsltp refuses a huge number, sends the rest and audits it ({out.get('crashed')})")
        fake.positions[107] = fake.NS(**{**vars(fake.positions[101]), "ticket": 107, "sl": "x"})
        fake.sent.clear()
        try:
            out = ag.set_sltp([{"ticket": 107, "tp": 2650.0}, {"ticket": 101, "tp": 2651.0}])
        except Exception as exc:                               # noqa: BLE001
            out = {"crashed": repr(exc)}
        bad = (out.get("results") or [{}])[0]
        check(len(fake.sent) == 1 and fake.sent[0]["position"] == 101
              and "could not check" in (bad.get("error") or ""),
              f"setsltp refuses an item that fails to check, and still sends the rest"
              f" ({out.get('crashed') or bad.get('error')})")
        del fake.positions[107]

        class _Stop(BaseException):
            pass

        real_send = fake.order_send

        def send_then_stop(req):
            if fake.sent:
                raise _Stop()
            return real_send(req)

        fake.order_send, stopped = send_then_stop, False
        fake.sent.clear()
        try:
            ag.set_sltp([{"ticket": 101, "tp": 2652.0}, {"ticket": 102, "tp": 2601.0}])
        except _Stop:
            stopped = True
        finally:
            fake.order_send = real_send
        audited = last_audit().get("fields", {}).get("setsltp", [])
        check(stopped and [(s.get("ticket"), s.get("ok")) for s in audited] == [(101, True), (102, False)],
              f"setsltp audits what was sent even when sending is cut off ({audited})")

        # rounding: judged as it will be sent
        for label, item, word in (
                ("a sell SL that rounds to 0", dict(ticket=102, sl=0.004), "rounds to 0"),
                ("a TP that rounds to 0", dict(ticket=101, tp=0.001), "rounds to 0"),
                ("a buy SL that rounds onto the bid", dict(ticket=101, sl=2639.996), "below")):
            out, row = run(**item)
            check(not fake.sent and word in (row.get("error") or ""),
                  f"setsltp refuses {label} without sending ({row.get('error')!r})")

        # tickets: a whole number or digits, nothing int() would bend into one
        for label, raw in (("a float ticket", 101.9), ("a whole float ticket", 101.0),
                           ("a boolean ticket", True), ("a negative ticket", -101),
                           ("ticket 0", 0), ("a non-digit string ticket", "1e2"),
                           ("a huge ticket", 10 ** 400), ("a unicode-digit ticket", "１０１")):
            out, row = run(ticket=raw, tp=2655.0)
            check(not fake.sent and row.get("error") == "not a ticket number",
                  f"setsltp refuses {label} ({row.get('error')!r})")
        out, row = run(ticket=" 101 ", tp=2655.0)
        check(len(fake.sent) == 1 and fake.sent[0]["position"] == 101,
              "setsltp takes a ticket given as a digit string")

        # MT5 must answer with the position that was asked for
        fake.alias = {106: 101}
        real_get = fake.positions_get
        fake.positions_get = lambda ticket=None: (real_get(fake.alias.get(ticket, ticket))
                                                  if ticket is not None else real_get())
        try:
            out, row = run(ticket=106, tp=2650.0)
        finally:
            fake.positions_get = real_get
        check(not fake.sent and "answered with position 101" in (row.get("error") or ""),
              f"setsltp refuses when MT5 returns another position ({row.get('error')!r})")

        # freeze level: no change near a stop that may be filling
        fake.symbols["GBPUSD"] = fake.NS(digits=5, point=0.00001, trade_stops_level=0,
                                         trade_freeze_level=50, trade_contract_size=100000.0)
        fake.ticks["GBPUSD"] = fake.NS(bid=1.30000, ask=1.30010)
        base = {**vars(fake.positions[103]), "symbol": "GBPUSD"}
        fake.positions[104] = fake.NS(**{**base, "ticket": 104})
        fake.positions[105] = fake.NS(**{**base, "ticket": 105, "sl": 1.29960})
        out, row = run(ticket=104, tp=1.30030)
        check(not fake.sent and "freezes changes within 50" in (row.get("error") or ""),
              f"setsltp refuses a TP inside the freeze level ({row.get('error')!r})")
        out, row = run(ticket=105, tp=1.30200)
        check(not fake.sent and "frozen" in (row.get("error") or ""),
              f"setsltp refuses a change while the live SL is inside the freeze level"
              f" ({row.get('error')!r})")
        out, row = run(ticket=104, tp=1.30100)
        check(len(fake.sent) == 1 and fake.sent[0]["tp"] == 1.301,
              "setsltp sends a TP beyond the freeze level")
        for t in (104, 105):
            del fake.positions[t]

        # the owner's call: widening or removing a stop stays allowed, behind the PIN
        out, row = run(ticket=102, sl=2800.0)
        check(len(fake.sent) == 1 and fake.sent[0]["sl"] == 2800.0,
              "setsltp moves a sell SL further away (owner's accepted risk)")

        check(log.exists() and '"setsltp"' in log.read_text(encoding="utf-8"),
              "setsltp writes an audit entry")
        check(ag.needs_pin("setsltp", {}), "setsltp needs the PIN")

        # through the real handler: PIN, confirm, then the same function
        import http.client
        from http.server import ThreadingHTTPServer
        old_pin, old_token = ag.AGENT_PIN, ag.Handler.token
        ag.AGENT_PIN, ag.Handler.token = "246810", ""
        httpd = ThreadingHTTPServer(("127.0.0.1", 0), ag.Handler)
        threading.Thread(target=httpd.serve_forever, daemon=True).start()

        def post(body):
            c = http.client.HTTPConnection("127.0.0.1", httpd.server_address[1], timeout=10)
            c.request("POST", "/api/manager", json.dumps(body),
                      {"Content-Type": "application/json", "Origin": ag.ALLOWED_ORIGINS[0]})
            r = c.getresponse()
            return r.status, json.loads(r.read() or b"{}")

        try:
            fake.sent.clear()
            items = [{"ticket": 101, "tp": 2650.0, "sl": None}]
            st, _ = post({"action": "setsltp", "confirm": True, "items": items})
            check(st == 403 and not fake.sent, f"POST setsltp without the PIN is refused ({st})")
            st, _ = post({"action": "setsltp", "pin": "246810", "items": items})
            check(st == 400 and not fake.sent, f"POST setsltp without confirm is refused ({st})")
            st, body = post({"action": "setsltp", "pin": "246810", "confirm": True, "items": items})
            check(st == 200 and body.get("changed") == 1 and len(fake.sent) == 1,
                  f"POST setsltp with PIN and confirm sets the TP ({st}, {body.get('message')})")
            for action in ("closeposition", "cancelpending"):
                fake.sent.clear()
                st, _ = post({"action": action, "pin": "246810", "confirm": True,
                              "tickets": "101"})
                check(st == 400 and not fake.sent,
                      f"POST {action} with tickets as a string is refused, not read"
                      f" digit by digit ({st})")
        finally:
            httpd.shutdown()
            httpd.server_close()
            ag.AGENT_PIN, ag.Handler.token = old_pin, old_token
            ag._pin_fails.clear()
    finally:
        ag._connect = old


def check_close_cancel_tickets() -> None:
    """closeposition and cancelpending take a ticket as setsltp does: a whole
    number or digits. int(101.9) is 101 - another position - so it is refused."""
    from app import mt5_agent as ag

    fake = _FakeMT5()
    fake.orders_get = lambda: (fake.NS(ticket=201),)
    old = ag._connect
    ag._connect = lambda: fake
    try:
        for fn, live in ((ag.close_positions, 101), (ag.cancel_pendings, 201)):
            name = fn.__name__
            for raw in (live + 0.9, float(live), True, "abc", None, -live):
                fake.sent.clear()
                try:
                    out = fn([raw])
                except Exception as exc:                       # noqa: BLE001
                    out = {"crashed": repr(exc)}
                check(not fake.sent and out.get("ok") is False
                      and out.get("failed") == [{"ticket": raw, "error": "not a ticket number"}],
                      f"{name} refuses ticket {raw!r} without sending"
                      f" ({out.get('crashed') or out.get('message')})")
            fake.sent.clear()
            out = fn([str(live), live + 0.9])
            target = fake.sent[0].get("position", fake.sent[0].get("order")) if fake.sent else None
            check(len(fake.sent) == 1 and target == live and len(out["failed"]) == 1
                  and "1 not a ticket number" in out["message"]
                  and "refused by MT5" not in out["message"],
                  f"{name} acts on the good ticket and refuses the bad one ({out['message']})")
    finally:
        ag._connect = old


def check_contract_size() -> None:
    """Each position carries its symbol's contract size, or null - never a guess."""
    from app import mt5_agent as ag

    fake = _FakeMT5()
    seen: dict = {}
    check(ag._contract_size(fake, "XAUUSD", seen) == 100.0, "contract size is read from symbol_info")
    calls = fake.info_calls
    ag._contract_size(fake, "XAUUSD", seen)
    check(fake.info_calls == calls, "contract size is cached per symbol within one read")
    check(ag._contract_size(fake, "NOSUCH", seen) is None, "an unknown symbol has no contract size")
    check(ag._contract_size(fake, "ZEROSZ", seen) is None, "a zero contract size reads as null")

    old = ag._connect
    ag._connect = lambda: fake
    try:
        state = ag._read_state_locked(time.time())
        sizes = {p["ticket"]: p.get("contract_size", "missing") for p in state.get("positions", [])}
        check(sizes == {101: 100.0, 102: 100.0, 103: 100000.0},
              f"the positions payload carries contract_size ({sizes})")
    finally:
        ag._connect = old
        ag._cache.update(at=0.0, data=None)
        for sym in fake.symbols:
            ag._digits_cache.pop(sym, None)


def check_first_leg() -> None:
    """A position closed in two chunks counts once, whatever order the deals arrive in."""
    from types import SimpleNamespace
    from app import mt5_agent as ag

    def deal(pos, entry, typ, profit, magic, t):
        return SimpleNamespace(**{"position_id": pos, "entry": entry, "type": typ, "profit": profit, "swap": 0.0,
                "commission": 0.0, "fee": 0.0, "magic": magic, "symbol": "TESTSYM", "volume": 0.1,
                "price": 1.0, "comment": "", "time": t})

    base = int(time.time()) - 3600
    deals = [
        deal(9001, 0, 0, 0.0, 777, base),            # open A
        deal(9002, 0, 0, 0.0, 777, base + 1),        # open B
        deal(9001, 1, 1, 10.0, 0, base + 10),        # A closes part 1
        deal(9001, 1, 1, -4.0, 0, base + 20),        # A closes part 2, net +6
        deal(9002, 1, 1, 2.0, 0, base + 30),         # B closes whole, net +2
    ]

    class Fake:
        def history_deals_get(self, *a):
            return deals

    old, old_owners = ag._connect, ag.magic_owners
    ag._connect = lambda: Fake()
    ag.magic_owners = lambda: {}
    ag._hist_cache.update(at=0.0, days=0, data=None)
    try:
        out = ag.read_history(days=7)
    finally:
        ag._connect, ag.magic_owners = old, old_owners
        ag._hist_cache.update(at=0.0, days=0, data=None)
    ea = next((e for e in out.get("by_ea", []) if e.get("magic") == 777), {})
    got = (ea.get("trades"), ea.get("wins"), ea.get("losses"))
    check(got == (2, 2, 0), f"a partly closed position counts once in by_ea (trades, wins, losses = {got})")


def check_bars() -> None:
    """GET /api/bars through the real handler, against a fake terminal with made-up bars."""
    import http.client
    from http.server import ThreadingHTTPServer
    from app import mt5_agent as ag

    fake = _FakeMT5()
    old_conn, old_token = ag._connect, ag.Handler.token
    ag._connect = lambda: fake
    ag.Handler.token = "bars-test-token"
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), ag.Handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()

    def get(query, auth=True):
        c = http.client.HTTPConnection("127.0.0.1", httpd.server_address[1], timeout=10)
        c.request("GET", "/api/bars?" + query,
                  headers={"Authorization": "Bearer bars-test-token"} if auth else {})
        r = c.getresponse()
        return r.status, json.loads(r.read() or b"{}")

    try:
        st, body = get("symbol=XAUUSD&tf=M15&count=4")
        bars = body.get("bars") or [{}]
        check(st == 200 and body.get("symbol") == "XAUUSD" and body.get("tf") == "M15"
              and body.get("digits") == 2 and body.get("count") == 4 == len(body.get("bars", []))
              and body.get("source") == "MT5 copy_rates, broker server time"
              and set(bars[0]) == {"t", "iso", "o", "h", "l", "c", "v"},
              f"/api/bars returns the documented shape ({st})")
        check(bars[0] == {"t": 1700000000, "iso": "2023-11-14T22:13:20Z", "o": 2000.0, "h": 2001.5,
                          "l": 1999.0, "c": 2000.5, "v": 10}
              and bars[1]["o"] == 2001.5 and bars[1]["v"] == 11,
              "/api/bars carries the terminal's own values, rounded to digits")
        check("XAUUSD" in getattr(fake, "selected", []), "/api/bars selects the symbol first")

        st, body = get("symbol=XAUUSD&tf=M2")
        check(st == 400 and body.get("ok") is False, f"/api/bars rejects an unknown timeframe ({st})")
        st, _ = get("tf=M15")
        check(st == 400, f"/api/bars requires a symbol ({st})")
        st, _ = get("symbol=NOSUCH&tf=M15")
        check(st == 400, f"/api/bars rejects an unknown symbol ({st})")
        st, _ = get("symbol=XAUUSD&count=abc")
        check(st == 400, f"/api/bars rejects a non-numeric count ({st})")
        st, _ = get("symbol=XAUUSD&from=yesterday")
        check(st == 400, f"/api/bars rejects an unreadable from ({st})")

        get("symbol=XAUUSD&tf=H1&count=999999")
        check(fake.asked == 5000, f"/api/bars caps count at 5000 (asked MT5 for {fake.asked})")

        st, body = get("symbol=XAUUSD&tf=M15&from=1700000900&to=2023-11-14T22:30:00Z")
        check(st == 200 and body["bars"]
              and all(1700000900 <= b["t"] <= 1700001800 for b in body["bars"]),
              "/api/bars honours from/to given as epoch or ISO, after range returned nothing")

        fake.copy_rates_from = lambda *a: ()
        fake.copy_rates_from_pos = lambda *a: None
        st, body = get("symbol=XAUUSD&tf=M15")
        check(st == 200 and body.get("bars") == [] and body.get("count") == 0 and body.get("note"),
              "/api/bars with no data returns an empty list and a note, not zeros")

        st, _ = get("symbol=XAUUSD&tf=M15", auth=False)
        check(st == 401, f"/api/bars needs the Bearer token ({st})")
    finally:
        httpd.shutdown()
        httpd.server_close()
        ag._connect, ag.Handler.token = old_conn, old_token
        for sym in fake.symbols:
            ag._digits_cache.pop(sym, None)


def check_no_control_chars() -> None:
    """No stray control bytes in source files.

    Writing patches through Python string literals has now put a backspace into a
    regex and a BEL into a Windows path, both of which only showed up when a user
    ran the result. A byte scan costs nothing and catches every repeat.
    """
    for rel in ("public/index.html", "app/mt5_agent.py", "app/installer.py",
                "app/buzz.py", "app/mq5_inputs.py", "app/basket_dd.py",
                "app/mql5_market.py", "app/ex5_meta.py", "install_vps.ps1",
                "restart_agent.ps1", "fix_watchdog.ps1", "office/radar-crew.html",
                "mql5/FxeaManager.mq5"):
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
