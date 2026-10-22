"""Interactive operator setup. Never print or reuse the active Demo session."""

import argparse
import asyncio
from getpass import getpass
from pathlib import Path

from telethon import TelegramClient
from telethon.sessions import StringSession

from okx_bot.live_deployment import TelegramSecrets


async def create_session(telegram_env_file, output):
    if await asyncio.to_thread(output.exists):
        raise ValueError("Existing session cannot be overwritten")
    settings = TelegramSecrets.from_file(telegram_env_file)
    client = TelegramClient(
        StringSession(), settings.telegram_api_id, settings.telegram_api_hash.get_secret_value()
    )
    try:
        await client.start(password=lambda: getpass("Telegram 2FA password: "))
        value = client.session.save()

        def save():
            with output.open("x", encoding="utf-8") as stream:
                stream.write(value)
            output.chmod(0o600)

        await asyncio.to_thread(save)
    finally:
        await client.disconnect()


def main():
    parser = argparse.ArgumentParser(description="Create a separate private Live Telegram session")
    parser.add_argument("--telegram-env-file", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    try:
        asyncio.run(create_session(args.telegram_env_file, args.output))
        print("Dedicated session saved. No session value printed; execution remains disabled.")
    except Exception:
        print("Session setup failed. Existing session files were not overwritten.")
        raise SystemExit(1) from None


if __name__ == "__main__":
    main()
