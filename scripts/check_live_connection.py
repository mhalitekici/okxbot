"""Operator-invoked read-only check; never start the bot, database or Telegram."""

import sys
from importlib import import_module
from pathlib import Path


def main(argv: list[str] | None = None) -> int:
    # Running scripts/foo.py puts scripts/, rather than src/, first on sys.path.
    # Prefer this checkout to a stale installed package or another PYTHONPATH entry.
    source_dir = Path(__file__).resolve().parents[1] / "src"
    sys.path.insert(0, str(source_dir))
    return import_module("okx_bot.live_connection").main(argv)


if __name__ == "__main__":
    raise SystemExit(main())
