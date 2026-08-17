"""List the channels/groups this account has joined, so you can pick the right
handle for TG_CHANNELS.

  python -m app.list_chats            # all channels and groups
  python -m app.list_chats fx forex   # only those whose title/username matches a word
"""
from __future__ import annotations

import asyncio
import sys

from telethon import TelegramClient
from telethon.tl.types import Channel, Chat

from . import config


async def main(words: list[str]) -> None:
    config.require_creds()
    client = TelegramClient(str(config.SESSION_FILE), config.API_ID, config.API_HASH)
    await client.connect()
    if not await client.is_user_authorized():
        raise SystemExit("Not logged in. Run:  .\\run.ps1 login")

    rows = []
    async for dialog in client.iter_dialogs():
        ent = dialog.entity
        if not isinstance(ent, (Channel, Chat)):
            continue
        title = dialog.name or ""
        uname = getattr(ent, "username", None)
        hay = f"{title} {uname or ''}".lower()
        if words and not any(w.lower() in hay for w in words):
            continue
        kind = "channel" if isinstance(ent, Channel) and getattr(ent, "broadcast", False) else "group"
        last_id = dialog.message.id if dialog.message else 0
        rows.append((last_id, kind, title, uname, ent.id))

    rows.sort(key=lambda r: -r[0])
    print(f"{'msgs':>7}  {'kind':<8} {'handle':<28} title")
    print("-" * 90)
    for last_id, kind, title, uname, ent_id in rows:
        handle = f"@{uname}" if uname else f"id:{ent_id}"
        print(f"{last_id:>7}  {kind:<8} {handle:<28} {title[:44]}")
    print(f"\n{len(rows)} chats listed. Put the handle (without @) in TG_CHANNELS in .env")

    await client.disconnect()


if __name__ == "__main__":
    asyncio.run(main(sys.argv[1:]))
