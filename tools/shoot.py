"""Before/after screenshots of one tab, for a pull request description.

    python tools/shoot.py --tab market --base main --out shots/

Builds the static site twice - from the base ref (in a temporary worktree) and
from the working tree - and screenshots the given tab of each with a headless
browser. A reviewer should not have to check out a branch to see what a page
change looks like.

Uses a system Chrome/Chromium/Edge when one is installed, otherwise Playwright's
own Chromium (`npx playwright install chromium`). Prints which it used, and
exits non-zero rather than writing a blank image when neither is available.

Opened straight onto the tab by URL - which is also how it found that the
Market tab drew "Nothing crawled yet" on a direct link: the first before/after
pair came out identical and empty.
"""
from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

BROWSERS = [
    "google-chrome", "google-chrome-stable", "chromium", "chromium-browser", "chrome", "msedge",
    r"C:\Program Files\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
    "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
]


def find_browser() -> str | None:
    for b in BROWSERS:
        path = shutil.which(b) or (b if os.path.isfile(b) else None)
        if path:
            return path
    return None


def build(tree: Path, out_html: Path) -> None:
    """Run the static export inside `tree` and copy the page out."""
    env = dict(os.environ, PYTHONPATH=str(tree))
    subprocess.run([sys.executable, "-m", "app.export_static"], cwd=tree, env=env,
                   check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    shutil.copy(tree / "site" / "index.html", out_html)


def shoot(html: Path, tab: str, png: Path, width: int, height: int) -> str:
    url = html.resolve().as_uri() + f"?tab={tab}"
    browser = find_browser()
    if browser:
        subprocess.run([browser, "--headless=new", "--disable-gpu", "--hide-scrollbars",
                        "--no-sandbox", f"--window-size={width},{height}",
                        "--virtual-time-budget=4000", f"--screenshot={png}", url],
                       check=False, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        if png.exists() and png.stat().st_size > 0:
            return browser
    npx = shutil.which("npx")
    if npx:
        subprocess.run([npx, "-y", "playwright", "screenshot", f"--viewport-size={width},{height}",
                        "--wait-for-timeout=3000", url, str(png)],
                       check=False, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        if png.exists() and png.stat().st_size > 0:
            return "playwright"
    raise SystemExit("no headless browser could take the screenshot - "
                     "install one, or run `npx playwright install chromium`")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--tab", default="market", help="eas, market, mt5 or myea")
    ap.add_argument("--base", default="main", help="the ref to compare against")
    ap.add_argument("--out", default="shots", help="where before.png and after.png go")
    ap.add_argument("--width", type=int, default=1100)
    ap.add_argument("--height", type=int, default=560)
    args = ap.parse_args()

    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        wt = tmp / "base"
        subprocess.run(["git", "worktree", "add", "-q", "--detach", str(wt), args.base],
                       cwd=ROOT, check=True)
        try:
            # the base build needs the same data the working tree has
            if (ROOT / "data").is_dir():
                shutil.copytree(ROOT / "data", wt / "data", dirs_exist_ok=True,
                                ignore=shutil.ignore_patterns("files", "*.session*"))
            build(wt, tmp / "before.html")
        finally:
            subprocess.run(["git", "worktree", "remove", "--force", str(wt)], cwd=ROOT, check=False)
        build(ROOT, tmp / "after.html")
        used = ""
        for name in ("before", "after"):
            used = shoot(tmp / f"{name}.html", args.tab, out / f"{name}.png", args.width, args.height)
    sizes = {n: (out / f"{n}.png").stat().st_size for n in ("before", "after")}
    print(f"{out / 'before.png'}  {out / 'after.png'}  via {used}  {sizes}")
    if sizes["before"] == sizes["after"]:
        print("WARNING: before and after are the same size - look before using them; "
              "the change may not be visible on this tab, or the tab may not have rendered")


if __name__ == "__main__":
    main()
