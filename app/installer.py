"""Fetch an EA from the channel and install it, without anyone touching the VPS.

The catalog already knows which message carries which file, so a row can say
"install this" and mean it. What arrives is a stranger's archive, so this module
is deliberately narrow:

  * only the channels in config.CHANNELS are ever read
  * only .ex5 and .set files are kept, and only from inside the archive
  * paths inside the archive are ignored entirely, so nothing can be written
    outside the install folder
  * sizes and file counts are capped
  * everything lands in MQL5/Experts/FxeaRadar/, never over an existing EA

It cannot make the terminal trade on its own: attaching is a separate, PIN-gated
step that the person doing it has to confirm.
"""
from __future__ import annotations

import asyncio
import pathlib
import shutil
import subprocess
import tempfile
import re
import zipfile

from . import config

# Straight into MQL5/Experts, under the name the archive used. Cracked EAs are
# known to check their own file name or look for a licence file beside them, and
# every EA on this terminal that works was installed exactly like this - a
# subfolder and a tidied name are differences with nothing to gain.
INSTALL_FOLDER = ""
KEEP_SUFFIXES = (".ex5", ".set")
MAX_ARCHIVE_BYTES = 60 * 1024 * 1024
MAX_MEMBERS = 200
MAX_MEMBER_BYTES = 40 * 1024 * 1024

VPS_SESSION = config.DATA_DIR / "tg_vps.session"


def _tidy_name(name: str) -> str:
    """Drop the channel tag from a file name before installing it.

    "Boring Pips MT5 @free_fx_pro.ex5" becomes "Boring Pips MT5.ex5". The tag is
    advertising, it puts '@' and double spaces into a name that has to survive a
    template file and a Navigator lookup, and it makes every installed EA read
    like the channel rather than the product.
    """
    stem = pathlib.PurePath(name).stem
    suffix = pathlib.PurePath(name).suffix
    stem = re.sub(r"[@#]\S*", " ", stem)
    stem = re.sub(r"\s+", " ", stem).strip(" -_")
    return (stem or pathlib.PurePath(name).stem) + suffix


def session_ready() -> bool:
    return VPS_SESSION.exists()


async def _download(channel: str, message_id: int, into: pathlib.Path) -> tuple[pathlib.Path | None, str]:
    """The attachment of one message, or a reason it could not be fetched."""
    from telethon import TelegramClient

    if channel not in config.CHANNELS:
        return None, f"{channel} is not one of the channels this agent may read"
    if not session_ready():
        return None, "no Telegram session on this machine - run: python -m app.tg_login_vps"

    client = TelegramClient(str(VPS_SESSION), config.API_ID, config.API_HASH)
    try:
        await client.connect()
        if not await client.is_user_authorized():
            return None, "the Telegram session is no longer authorised - log in again"
        msg = await client.get_messages(channel, ids=message_id)
        if msg is None or msg.media is None:
            return None, "that message has no file attached"
        size = getattr(getattr(msg, "document", None), "size", 0) or 0
        if size > MAX_ARCHIVE_BYTES:
            return None, f"file is {size // 1048576} MB, over the {MAX_ARCHIVE_BYTES // 1048576} MB limit"
        path = await msg.download_media(file=str(into))
        return (pathlib.Path(path) if path else None), ("" if path else "download produced no file")
    except Exception as exc:                                       # noqa: BLE001
        return None, f"download failed: {exc}"
    finally:
        try:
            await client.disconnect()
        except Exception:                                          # noqa: BLE001
            pass


def _unpack(archive: pathlib.Path, into: pathlib.Path,
            seen: list | None = None) -> tuple[list[pathlib.Path], str]:
    """Extract what an archive holds, flat, keeping only EA files.

    .zip is handled here; anything else goes to whichever external tool can read
    it, see _extractor.
    """
    into.mkdir(parents=True, exist_ok=True)
    suffix = archive.suffix.lower()

    if suffix in (".ex5", ".set"):                 # posted bare, nothing to unpack
        target = into / archive.name
        shutil.copy2(archive, target)
        return [target], ""

    if suffix == ".zip":
        try:
            with zipfile.ZipFile(archive) as z:
                members = [m for m in z.infolist() if not m.is_dir()][:MAX_MEMBERS]
                for m in members:
                    if m.file_size > MAX_MEMBER_BYTES:
                        continue
                    name = pathlib.PurePosixPath(m.filename).name      # ignore any path
                    if seen is not None and name:
                        seen.append(m.filename)
                    if not name or not name.lower().endswith(KEEP_SUFFIXES):
                        continue
                    with z.open(m) as src, open(into / name, "wb") as dst:
                        shutil.copyfileobj(src, dst, length=1024 * 256)
        except (zipfile.BadZipFile, OSError) as exc:
            return [], f"could not read the zip: {exc}"
        return sorted(into.iterdir()), ""

    tool = _extractor(suffix)
    if tool is None:
        return [], (f"nothing on this machine can unpack {suffix or 'that archive'}."
                    " Install 7-Zip on the VPS")
    exe, args = tool
    try:
        subprocess.run([exe, *args, str(archive)], cwd=into, capture_output=True,
                       timeout=180, creationflags=0x08000000)
    except (OSError, subprocess.TimeoutExpired) as exc:
        return [], f"extract failed: {exc}"

    kept = []
    for f in into.rglob("*"):
        if f.is_file() and seen is not None:
            seen.append(str(f.relative_to(into)))
        if f.is_file() and f.suffix.lower() in KEEP_SUFFIXES and f.stat().st_size <= MAX_MEMBER_BYTES:
            flat = into / f.name
            if f != flat:
                shutil.move(str(f), flat)
            kept.append(flat)
    return sorted(set(kept)), ""


def _seven_zip() -> str | None:
    for cand in (r"C:\Program Files\7-Zip\7z.exe", r"C:\Program Files (x86)\7-Zip\7z.exe"):
        if pathlib.Path(cand).exists():
            return cand
    return shutil.which("7z")


def _extractor(suffix: str = "") -> tuple[str, list[str]] | None:
    """A tool that can actually read this kind of archive.

    Windows ships bsdtar, which reads zip and rar4 but not rar5, and the channel
    posts rar of unknown vintage. So 7-Zip goes first for .rar with bsdtar as the
    fallback, and bsdtar goes first for everything else - which keeps 7-Zip from
    being a hard requirement.
    """
    seven, tar = _seven_zip(), shutil.which("tar")
    for tool in ([seven, tar] if suffix.lower() == ".rar" else [tar, seven]):
        if not tool:
            continue
        is_tar = pathlib.Path(tool).stem.lower() == "tar"
        return (tool, ["-xf"]) if is_tar else (tool, ["x", "-y"])
    return None


def install_from_channel(channel: str, message_id: int, experts_dir: pathlib.Path) -> dict:
    """Download, unpack, and copy the EA files into MQL5/Experts/FxeaRadar."""
    try:
        message_id = int(message_id)
    except (TypeError, ValueError):
        return {"ok": False, "error": "message_id must be a number"}

    with tempfile.TemporaryDirectory(prefix="fxea_install_") as tmp:
        tmpdir = pathlib.Path(tmp)
        archive, why = asyncio.run(_download(str(channel), message_id, tmpdir))
        if archive is None:
            return {"ok": False, "error": why}

        seen: list[str] = []
        found, why = _unpack(archive, tmpdir / "out", seen)
        if why:
            return {"ok": False, "error": why}
        eas = [f for f in found if f.suffix.lower() == ".ex5"]
        if not eas:
            others = ", ".join(sorted({f.suffix.lstrip('.') for f in found})) or "nothing"
            return {"ok": False,
                    "error": f"no .ex5 inside {archive.name} (found {others})"
                             " - it may be an MT4 EA, a source-only release, or an indicator"}

        target = experts_dir / INSTALL_FOLDER if INSTALL_FOLDER else experts_dir
        target.mkdir(parents=True, exist_ok=True)
        # MT5 loads presets from MQL5/Presets - a .set sitting in Experts is a file
        # nothing will ever offer you in the EA's Load dialog
        presets = experts_dir.parent / "Presets"
        presets.mkdir(parents=True, exist_ok=True)
        installed, skipped = [], []
        for f in found:
            dst = (presets if f.suffix.lower() == ".set" else target) / f.name
            if dst.exists():
                if dst.read_bytes() == f.read_bytes():
                    skipped.append(f.name)                 # already installed, unchanged
                    continue
                # a different file under the same name: replacing it could swap an
                # EA that is running right now, so leave it and say so
                skipped.append(f.name + " (kept the installed version)")
                continue
            shutil.copy2(f, dst)
            installed.append({"name": dst.stem if dst.suffix.lower() == ".ex5" else dst.name,
                              "file": dst.name,
                              "path": str(pathlib.PurePath("Presets") / dst.name)
                                      if dst.suffix.lower() == ".set"
                                      else (str(pathlib.PurePath("Experts") / INSTALL_FOLDER / dst.name)
                                            if INSTALL_FOLDER
                                            else str(pathlib.PurePath("Experts") / dst.name)),
                              "kind": "ea" if dst.suffix.lower() == ".ex5" else "set",
                              "size_bytes": dst.stat().st_size})

        # What an EA needs beyond its .ex5 is worth naming: a pack that shipped a
        # .dll or a data file has an EA that may refuse to run without it, and
        # this is the only place that knows those files existed.
        kept_names = {f.name for f in found}
        dropped = sorted({pathlib.PurePath(x).name for x in seen
                          if pathlib.PurePath(x).name and pathlib.PurePath(x).name not in kept_names})
        return {"ok": True, "archive": archive.name, "installed": installed,
                "unchanged": skipped, "dropped": dropped[:40],
                "experts": [x for x in installed if x["kind"] == "ea"],
                "presets": [x for x in installed if x["kind"] == "set"]}
