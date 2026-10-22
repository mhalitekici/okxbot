"""Read the worker status, Telegram authorization and one message; never ingest a signal."""

import argparse
import asyncio
import json

import httpx

from okx_bot.config import Settings
from okx_bot.logging import configure_logging
from okx_bot.telegram.diagnostics import inspect_telegram


async def main():
    parser = argparse.ArgumentParser(description="Read-only Telegram/forum diagnostics")
    parser.add_argument("--message-ids", nargs="+", type=int, default=None)
    args = parser.parse_args()
    if args.message_ids and (
        len(args.message_ids) > 100 or any(not 0 < value <= 2**31 - 1 for value in args.message_ids)
    ):
        parser.error("Provide between 1 and 100 positive Telegram message IDs")
    settings = Settings()
    configure_logging(settings.log_level)
    async with httpx.AsyncClient(timeout=5, trust_env=False, follow_redirects=False) as api:
        try:
            response = await api.get(
                "http://127.0.0.1:8000/admin/telegram",
                headers={
                    "Authorization": "Bearer " + settings.admin_api_token.get_secret_value(),
                },
            )
            worker = (
                response.json()
                if response.status_code == 200
                else {"available": False, "http_status": response.status_code}
            )
        except (httpx.HTTPError, ValueError):
            worker = {"available": False}
    report = await inspect_telegram(settings, message_ids=args.message_ids)
    report["running_listener"] = worker
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    asyncio.run(main())
