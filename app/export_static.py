"""Build a static, serverless copy of the dashboard into site/.

    python -m app.export_static

The page is the SAME public/index.html - no second UI to maintain. The generator
injects the API payload as a constant and stubs window.fetch so the existing
load() call resolves against it. Attachments are not hosted (a static host has no
/files route), so each file links to its Telegram post instead.
"""
from __future__ import annotations

import json
import re
from datetime import datetime, timezone

from . import config
from .grouping import group_products
from .server import summarize
from .shots import attach_shots
from .store import load_posts, load_state

SITE_DIR = config.ROOT / "site"


def build_payload(limit: int = 3000) -> dict:
    posts = [p for p in load_posts() if not p.get("excluded")]
    summary = summarize(posts)

    selected = [p for p in posts if p.get("is_ea") and (p.get("kind") or "ea") == "ea"]
    before = len(selected)
    grouped = group_products(selected)
    attach_shots(grouped)
    collapsed = before - len(grouped)

    # No /files route on a static host: point each attachment at its Telegram post.
    for g in grouped:
        for f in g.get("files") or []:
            f["local_path"] = None
            f["tg_url"] = g.get("url")
        for v in g.get("versions") or []:
            for f in v.get("files") or []:
                f["local_path"] = None
                f["tg_url"] = v.get("url")
        if g.get("shot"):
            g["shot"]["local_path"] = None

    state = load_state()
    return {
        "posts": grouped[:limit],
        "matched": len(grouped),
        "collapsed": collapsed,
        "truncated": len(grouped) > limit,
        "ea_only": True,
        "grouped": True,
        "kind": "ea",
        "tag": "",
        "summary": summary,
        "state": state,
        "sync": {
            "running": False,
            "static": True,
            "next_at": None,
            "shots_at": state.get("last_sync"),
            "generated_at": datetime.now(timezone.utc).isoformat(),
        },
    }


def render(payload: dict) -> str:
    html = (config.PUBLIC_DIR / "index.html").read_text(encoding="utf-8")
    data = json.dumps(payload, ensure_ascii=False).replace("</", "<\\/")

    shim = f"""<script>
/* static build: the page keeps its own logic, fetch is answered from this constant */
window.__FXEA__ = {data};
window.fetch = (url) => Promise.resolve({{
  ok: true, status: 200,
  json: () => Promise.resolve(window.__FXEA__),
}});
</script>
"""
    if "</head>" not in html:
        raise SystemExit("public/index.html has no </head> - cannot inject the static payload")
    html = html.replace("</head>", shim + "</head>", 1)
    # the auto-refresh timer is pointless without a server
    html = re.sub(r"setInterval\(load, \d+\);", "/* static: no polling */", html)
    return html


def main() -> None:
    import os

    payload = build_payload()
    SITE_DIR.mkdir(parents=True, exist_ok=True)
    out = SITE_DIR / "index.html"
    out.write_text(render(payload), encoding="utf-8")
    (SITE_DIR / ".nojekyll").write_text("", encoding="utf-8")   # GitHub Pages: serve as-is

    # PAGES_DOMAIN -> a CNAME file in the artifact, which is what tells Pages the
    # custom hostname. Left unset, Pages keeps serving the github.io URL.
    domain = (os.environ.get("PAGES_DOMAIN") or "").strip()
    if domain:
        (SITE_DIR / "CNAME").write_text(domain + "\n", encoding="utf-8")
        print(f"  CNAME -> {domain}")

    with_test = sum(1 for p in payload["posts"] if p.get("shot"))
    kb = out.stat().st_size / 1024
    print(f"site/index.html  {kb:,.0f} KB")
    print(f"  {payload['matched']} EAs, {payload['collapsed']} versions/reposts grouped, {with_test} with a test verdict")
    print("  attachments link to Telegram (a static host cannot serve /files)")


if __name__ == "__main__":
    main()
