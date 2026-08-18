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
import zipfile

from . import config

INSTALL_FOLDER = "FxeaRadar"          # under MQL5/Experts
KEEP_SUFFIXES = (".ex5", ".set")
MAX_ARCHIVE_BYTES = 60 * 1024 * 1024
MAX_MEMBERS = 200
MAX_MEMBER_BYTES = 40 * 1024 * 1024

VPS_SESSION = config.DATA_DIR / "tg_vps.session"


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


def _unpack(archive: pathlib.Path, into: pathlib.Path) -> tuple[list[pathlib.Path], str]:
    """Extract what an archive holds, flat, keeping only EA files.

    .zip is handled here; .rar needs an external tool, and Windows' own bsdtar
    reads rar4 archives, so it is tried before 7-Zip rather than making 7-Zip a
    hard dependency.
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
                    if not name or not name.lower().endswith(KEEP_SUFFIXES):
                        continue
                    with z.open(m) as src, open(into / name, "wb") as dst:
                        shutil.copyfileobj(src, dst, length=1024 * 256)
        except (zipfile.BadZipFile, OSError) as exc:
            return [], f"could not read the zip: {exc}"
        return sorted(into.iterdir()), ""

    tool = _extractor()
    if tool is None:
        return [], (f"{suffix or 'that archive'} needs 7-Zip, which is not installed on this machine."
                    " Install 7-Zip, or extract it yourself and upload the .ex5")
    exe, args = tool
    try:
        subprocess.run([exe, *args, str(archive)], cwd=into, capture_output=True,
                       timeout=180, creationflags=0x08000000)
    except (OSError, subprocess.TimeoutExpired) as exc:
        return [], f"extract failed: {exc}"

    kept = []
    for f in into.rglob("*"):
        if f.is_file() and f.suffix.lower() in KEEP_SUFFIXES and f.stat().st_size <= MAX_MEMBER_BYTES:
            flat = into / f.name
            if f != flat:
                shutil.move(str(f), flat)
            kept.append(flat)
    return sorted(set(kept)), ""


def _extractor() -> tuple[str, list[str]] | None:
    """bsdtar first - it ships with Windows - then 7-Zip where it usually lives."""
    tar = shutil.which("tar")
    if tar:
        return tar, ["-xf"]
    for cand in (r"C:\Program Files\7-Zip\7z.exe", r"C:\Program Files (x86)\7-Zip\7z.exe"):
        if pathlib.Path(cand).exists():
            return cand, ["x", "-y"]
    seven = shutil.which("7z")
    return (seven, ["x", "-y"]) if seven else None


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

        found, why = _unpack(archive, tmpdir / "out")
        if why:
            return {"ok": False, "error": why}
        eas = [f for f in found if f.suffix.lower() == ".ex5"]
        if not eas:
            others = ", ".join(sorted({f.suffix.lstrip('.') for f in found})) or "nothing"
            return {"ok": False,
                    "error": f"no .ex5 inside {archive.name} (found {others})"
                             " - it may be an MT4 EA, a source-only release, or an indicator"}

        target = experts_dir / INSTALL_FOLDER
        target.mkdir(parents=True, exist_ok=True)
        installed, skipped = [], []
        for f in found:
            dst = target / f.name
            if dst.exists() and dst.read_bytes() == f.read_bytes():
                skipped.append(f.name)                     # already installed, unchanged
                continue
            shutil.copy2(f, dst)
            installed.append({"name": dst.stem if dst.suffix.lower() == ".ex5" else dst.name,
                              "file": dst.name,
                              "path": str(pathlib.PurePath("Experts") / INSTALL_FOLDER / dst.name),
                              "kind": "ea" if dst.suffix.lower() == ".ex5" else "set",
                              "size_bytes": dst.stat().st_size})

        return {"ok": True, "archive": archive.name, "installed": installed,
                "unchanged": skipped,
                "experts": [x for x in installed if x["kind"] == "ea"],
                "presets": [x for x in installed if x["kind"] == "set"]}
