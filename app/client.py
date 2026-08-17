"""One place that builds the Telegram client.

Locally it uses the sqlite session file in data/. In CI there is no such file, so
TG_SESSION (a Telethon StringSession) is used instead - that is the only way this
can run on GitHub Actions, where the filesystem is thrown away after every job.
"""
from __future__ import annotations

import os

from telethon import TelegramClient
from telethon.sessions import StringSession

from . import config


def build_client() -> TelegramClient:
    config.require_creds()
    session_str = (os.environ.get("TG_SESSION") or "").strip()
    session = StringSession(session_str) if session_str else str(config.SESSION_FILE)
    return TelegramClient(
        session,
        config.API_ID,
        config.API_HASH,
        flood_sleep_threshold=config.FLOOD_SLEEP_THRESHOLD,
    )


async def connect() -> TelegramClient:
    client = build_client()
    await client.connect()
    if not await client.is_user_authorized():
        await client.disconnect()
        raise SystemExit(
            "Not authorized. Locally: .\\run.ps1 login  |  In CI: set the TG_SESSION secret "
            "(generate it with: python -m app.export_session)"
        )
    return client
