"""Print a StringSession for CI, derived from the local sqlite session.

    python -m app.export_session

Paste the output into the GitHub repo secret TG_SESSION.

That string IS a logged-in Telegram session for your account: anyone holding it can
read your messages. Put it only in a secret store, never in a file you commit, and
revoke it from Telegram (Settings -> Devices) if it leaks.
"""
from __future__ import annotations

import asyncio

from telethon import TelegramClient
from telethon.sessions import StringSession

from . import config


async def main() -> None:
    config.require_creds()
    if not config.SESSION_FILE.exists():
        raise SystemExit("No local session yet. Run:  .\\run.ps1 login")

    # Load the file session, then re-save it in string form.
    client = TelegramClient(str(config.SESSION_FILE), config.API_ID, config.API_HASH)
    await client.connect()
    if not await client.is_user_authorized():
        await client.disconnect()
        raise SystemExit("Local session is not authorized. Run:  .\\run.ps1 login")

    me = await client.get_me()
    string = StringSession.save(StringSession())  # placeholder, replaced below
    # Telethon can export the live session's auth key directly:
    from telethon.sessions import MemorySession

    mem = MemorySession()
    mem.set_dc(client.session.dc_id, client.session.server_address, client.session.port)
    mem.auth_key = client.session.auth_key
    string = StringSession.save(mem)

    await client.disconnect()

    print(f"\nAccount: {me.first_name or ''} {'@' + me.username if me.username else ''} (id {me.id})")
    print("\nAdd this as the GitHub secret TG_SESSION (single line):\n")
    print(string)
    print("\nAlso add TG_API_ID and TG_API_HASH as secrets. Never commit any of the three.")


if __name__ == "__main__":
    asyncio.run(main())
