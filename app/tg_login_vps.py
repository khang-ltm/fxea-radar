"""One-time Telegram login for the VPS, so the agent can fetch EA files itself.

    python -m app.tg_login_vps

This creates a SECOND authorisation on your account, stored as
data/tg_vps.session. It deliberately does not reuse the session GitHub Actions
holds: one MTProto auth key used from two IPs at once can be killed with
AUTH_KEY_DUPLICATED, which would take the twice-daily catalog sync down with it.

Revoke it any time from Telegram -> Settings -> Devices; the agent then simply
cannot fetch files, and nothing else stops working.
"""
from __future__ import annotations

import asyncio
import sys

from telethon import TelegramClient

from . import config

VPS_SESSION = config.DATA_DIR / "tg_vps.session"


async def main() -> int:
    api_id, api_hash = config.API_ID, config.API_HASH
    if not api_id or not api_hash:
        print("TG_API_ID and TG_API_HASH must be set (they are in .env.mt5 or the environment)")
        return 2

    config.DATA_DIR.mkdir(parents=True, exist_ok=True)
    client = TelegramClient(str(VPS_SESSION), api_id, api_hash)

    # start() asks for the phone number and the code Telegram sends, and for the
    # 2FA password if the account has one. Nothing is written until it succeeds.
    await client.start()
    me = await client.get_me()
    print(f"logged in as {me.first_name} (@{me.username or me.id})")
    print(f"session saved to {VPS_SESSION}")

    joined = []
    for name in (config.CHANNELS or []):
        try:
            entity = await client.get_entity(name)
            joined.append(getattr(entity, "title", name))
        except Exception as exc:                                  # noqa: BLE001
            print(f"  cannot see {name}: {exc}")
    if joined:
        print("can read: " + ", ".join(joined))
    else:
        print("WARNING: this account cannot see the configured channels - join them first")

    await client.disconnect()
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
