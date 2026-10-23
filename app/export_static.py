"""Build the static site into site/: the MT5 control panel.

    python -m app.export_static

The page is the SAME public/index.html - no second UI to maintain. The only
thing baked in is the agent's hostname, so a new device asks for the token alone.

The EA catalog and MQL5 Market tabs were shut down on 2026-10-23, so their data
is no longer baked into the page. build_payload() is kept, unused, so they can
be revived: it is what used to be inlined as window.__FXEA__.
"""
from __future__ import annotations

from datetime import datetime, timezone

from . import config
from .buzz import attach_buzz
from .grouping import group_products
from .server import summarize
from .shots import attach_shots
from .mql5_market import attach_market, public_view as market_view
from .store import load_market, load_posts, load_state

SITE_DIR = config.ROOT / "site"


def build_payload(limit: int = 3000) -> dict:
    """Dormant since 2026-10-23: the catalog + Market payload the page used to carry."""
    posts = [p for p in load_posts() if not p.get("excluded")]
    summary = summarize(posts)

    selected = [p for p in posts if p.get("is_ea") and (p.get("kind") or "ea") == "ea"]
    before = len(selected)
    grouped = attach_buzz(group_products(selected), posts)
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
    market = market_view(load_market())
    attach_market(grouped, market)
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
        "market": market,
        "sync": {
            "running": False,
            "static": True,
            "next_at": None,
            "shots_at": state.get("last_sync"),
            "generated_at": datetime.now(timezone.utc).isoformat(),
        },
    }


def render() -> str:
    import os

    agent_url = (os.environ.get("MT5_AGENT_URL") or "").strip().rstrip("/")
    html = (config.PUBLIC_DIR / "index.html").read_text(encoding="utf-8")

    shim = f"""<script>
// agent hostname baked in at build time so only the token is ever asked for;
// the token is NEVER built in - this file is public
window.__MT5_URL__ = {agent_url!r};
</script>
"""
    if "</head>" not in html:
        raise SystemExit("public/index.html has no </head> - cannot inject the agent URL")
    return html.replace("</head>", shim + "</head>", 1)


def main() -> None:
    import os

    SITE_DIR.mkdir(parents=True, exist_ok=True)
    out = SITE_DIR / "index.html"
    out.write_text(render(), encoding="utf-8")
    (SITE_DIR / ".nojekyll").write_text("", encoding="utf-8")   # GitHub Pages: serve as-is

    # PAGES_DOMAIN -> a CNAME file in the artifact, which is what tells Pages the
    # custom hostname. Left unset, Pages keeps serving the github.io URL.
    domain = (os.environ.get("PAGES_DOMAIN") or "").strip()
    if domain:
        (SITE_DIR / "CNAME").write_text(domain + "\n", encoding="utf-8")
        print(f"  CNAME -> {domain}")

    kb = out.stat().st_size / 1024
    print(f"site/index.html  {kb:,.0f} KB  (MT5 control panel; no catalog data baked in)")


if __name__ == "__main__":
    main()
