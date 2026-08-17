"""Non-interactive two-step login, for when stdin is not a terminal.

  Step 1:  python -m app.login_step --phone +84XXXXXXXXX
           -> Telegram sends a 5-digit code to your Telegram app.
  Step 2:  python -m app.login_step --code 12345 [--password YOUR_2FA]
           -> signs in and writes data/tg.session

The pending phone + phone_code_hash live in data/login_pending.json between the
two steps; it is deleted on success. Codes expire in a few minutes - if step 2
says the code is invalid or expired, redo step 1.
"""
from __future__ import annotations

import argparse
import asyncio
import json

from telethon import TelegramClient
from telethon.errors import (
    PhoneCodeExpiredError,
    PhoneCodeInvalidError,
    SessionPasswordNeededError,
)

from . import config

PENDING = config.DATA_DIR / "login_pending.json"


async def send_code(phone: str) -> None:
    client = TelegramClient(str(config.SESSION_FILE), config.API_ID, config.API_HASH)
    await client.connect()
    if await client.is_user_authorized():
        me = await client.get_me()
        print(f"Already logged in as {me.first_name or ''} (id {me.id}) - nothing to do.")
        await client.disconnect()
        return
    sent = await client.send_code_request(phone)
    PENDING.write_text(
        json.dumps({"phone": phone, "hash": sent.phone_code_hash}), encoding="utf-8"
    )
    print(f"Code sent to {phone} (type: {type(sent.type).__name__}).")
    print("Check your Telegram app, then run step 2 with --code <5 digits>")
    await client.disconnect()


async def sign_in(code: str, password: str | None) -> None:
    if not PENDING.exists():
        raise SystemExit("No pending login. Run step 1 with --phone first.")
    pending = json.loads(PENDING.read_text(encoding="utf-8"))

    client = TelegramClient(str(config.SESSION_FILE), config.API_ID, config.API_HASH)
    await client.connect()
    try:
        await client.sign_in(
            phone=pending["phone"], code=code, phone_code_hash=pending["hash"]
        )
    except SessionPasswordNeededError:
        if not password:
            await client.disconnect()
            raise SystemExit(
                "This account has 2FA enabled. Re-run step 2 with --password <your 2FA password>,\n"
                "or run '.\\run.ps1 login' in your own terminal so the password is never stored in a command line."
            )
        await client.sign_in(password=password)
    except PhoneCodeInvalidError:
        await client.disconnect()
        raise SystemExit("That code is wrong. Re-check it, or redo step 1 for a fresh code.")
    except PhoneCodeExpiredError:
        await client.disconnect()
        PENDING.unlink(missing_ok=True)
        raise SystemExit("That code expired. Redo step 1 to get a new one.")

    me = await client.get_me()
    uname = f"@{me.username}" if me.username else ""
    print(f"Logged in as {me.first_name or ''} {uname} (id {me.id})")
    print(f"Session: {config.SESSION_FILE}")

    print("\nResolving channels:")
    for ch in config.CHANNELS:
        try:
            ent = await client.get_entity(ch)
            title = getattr(ent, "title", None) or getattr(ent, "username", ch)
            print(f'  OK  {ch:<24} -> "{title}"  id={ent.id}')
        except Exception as exc:  # noqa: BLE001 - report resolve failures verbatim
            print(f"  !!  {ch:<24} -> cannot resolve ({exc})")
            print("      Join the channel in your Telegram app first, or fix TG_CHANNELS in .env")

    await client.disconnect()
    PENDING.unlink(missing_ok=True)


def main() -> None:
    config.require_creds()
    config.DATA_DIR.mkdir(parents=True, exist_ok=True)

    ap = argparse.ArgumentParser(description="Two-step non-interactive Telegram login")
    ap.add_argument("--phone", help="step 1: phone number with country code, e.g. +84912345678")
    ap.add_argument("--code", help="step 2: the 5-digit code Telegram sent you")
    ap.add_argument("--password", help="step 2: 2FA password, only if your account has one")
    args = ap.parse_args()

    if args.phone:
        asyncio.run(send_code(args.phone))
    elif args.code:
        asyncio.run(sign_in(args.code, args.password))
    else:
        ap.error("give either --phone (step 1) or --code (step 2)")


if __name__ == "__main__":
    main()
