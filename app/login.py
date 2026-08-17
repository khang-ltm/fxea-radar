"""One-time interactive login.

Telethon stores the authorized session in data/tg.session, so every later
`sync` / `serve` run is unattended.
"""
from __future__ import annotations

import asyncio

from telethon import TelegramClient

from . import config


async def main() -> None:
    config.require_creds()
    config.DATA_DIR.mkdir(parents=True, exist_ok=True)

    client = TelegramClient(str(config.SESSION_FILE), config.API_ID, config.API_HASH)
    await client.start()  # prompts for phone / code / 2FA password as needed

    me = await client.get_me()
    uname = f"@{me.username}" if me.username else ""
    print(f"\nLogged in as {me.first_name or ''} {uname} (id {me.id})")
    print(f"Session file: {config.SESSION_FILE}  -- treat it like a password.")

    print("\nResolving channels:")
    for ch in config.CHANNELS:
        try:
            ent = await client.get_entity(ch)
            title = getattr(ent, "title", None) or getattr(ent, "username", ch)
            print(f"  OK  {ch:<24} -> \"{title}\"  id={ent.id}")
        except Exception as exc:  # noqa: BLE001 - report any resolve failure verbatim
            print(f"  !!  {ch:<24} -> cannot resolve ({exc}).")
            print("      Join the channel in your Telegram app first, or fix TG_CHANNELS in .env")

    await client.disconnect()


if __name__ == "__main__":
    asyncio.run(main())
