import asyncio
import os
from pathlib import Path

from telethon import TelegramClient
from telethon.sessions import StringSession

from okx_bot.config import Settings


def save_session(path: Path, value: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as file:
        file.write(value)
    path.chmod(0o600)


async def setup() -> None:
    settings = Settings()
    if not settings.telegram_api_id or not settings.telegram_api_hash.get_secret_value():
        raise ValueError("TELEGRAM_API_ID and TELEGRAM_API_HASH are required")
    async with TelegramClient(
        StringSession(), settings.telegram_api_id, settings.telegram_api_hash.get_secret_value()
    ) as client:
        save_session(settings.telegram_session_file, client.session.save())
    print(f"Session saved to {settings.telegram_session_file}; keep this file secret.")


def main() -> None:
    asyncio.run(setup())
