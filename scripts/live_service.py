"""Dedicated container entry point. Never invoke scripts/start.py for Live."""

import sys
from importlib import import_module
from pathlib import Path


def main():
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
    return import_module("okx_bot.live_service").main()


if __name__ == "__main__":
    raise SystemExit(main())
