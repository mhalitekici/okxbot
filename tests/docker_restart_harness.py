"""Offline broker + disposable PostgreSQL, exercised across a real Docker restart."""

import asyncio
import json
import os
import signal
import sys
from datetime import datetime
from decimal import Decimal
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sqlalchemy import select  # noqa: E402
from sqlalchemy.engine import make_url  # noqa: E402

from okx_bot.config import Settings  # noqa: E402
from okx_bot.db import Database  # noqa: E402
from okx_bot.domain import Message, utcnow  # noqa: E402
from okx_bot.engine import TradingEngine  # noqa: E402
from okx_bot.models import BotEvent, Position, TradeOrder  # noqa: E402
from tests.fakes import DemoExchange, text  # noqa: E402


async def main():
    url = os.environ["TEST_RESTART_DATABASE_URL"]
    parsed = make_url(url)
    assert parsed.host == "127.0.0.1" and parsed.database.startswith("restart_")
    directory = Path("/state")
    saved = directory / "broker.json"
    stop = asyncio.Event()
    asyncio.get_running_loop().add_signal_handler(signal.SIGTERM, stop.set)
    settings = Settings(
        _env_file=None,
        database_url=url,
        telegram_channel_id=-100123,
        telegram_topic_id=23576,
        order_poll_attempts=2,
        order_poll_interval_seconds=0,
        dedicated_trading_account_confirmed=True,
    )
    db = Database(url)
    exchange = DemoExchange()
    exchange.market = Decimal("100.2")
    exchange.instrument_tick = Decimal("0.01")
    try:
        async with db.singleton():
            await db.initialize_state()
            engine = TradingEngine(settings, db, exchange)
            if saved.exists():
                snapshot = json.loads(saved.read_text())
                for key in (
                    "row",
                    "protection",
                    "exposure",
                    "submissions",
                    "amendments",
                    "orders",
                    "algos",
                    "latest_client",
                    "latest_algo_client",
                    "fills_data",
                    "bills_data",
                ):
                    setattr(exchange, key, snapshot[key])
                item = Message(
                    settings.telegram_channel_id,
                    500,
                    text(),
                    datetime.fromisoformat(snapshot["message_date"]),
                    topic_id=23576,
                )
                await engine.reconcile(startup=True)
                assert not (await engine.state()).emergency
                assert await engine.ingest(item) == "duplicate"
                async with db.sessions() as session:
                    position = await session.scalar(select(Position))
                    order = await session.scalar(select(TradeOrder))
                    assert position.status == "open" and position.avg_price == Decimal("100.2")
                    assert order.take_profit == Decimal("106.21")
                    assert order.stop_loss == Decimal("94.19")
                    assert order.execution_context["fill_price"] == "100.2"
                exchange.finish("tp")
                await engine.reconcile()
                async with db.sessions() as session:
                    assert (await session.scalar(select(Position))).status == "closed"
                    closed = await session.scalar(
                        select(BotEvent).where(BotEvent.kind == "position_closed")
                    )
                    assert closed.details["cause"] == "tp"
                assert len(exchange.submissions) == 1
                (directory / "phase2.json").write_text(
                    json.dumps(
                        {
                            "reconciled": True,
                            "duplicate_blocked": True,
                            "entry_count": 1,
                            "fill_price": "100.2",
                            "tp": "106.21",
                            "sl": "94.19",
                            "closed_by": "tp",
                        }
                    )
                )
            else:
                await engine.reconcile(startup=True)
                item = Message(settings.telegram_channel_id, 500, text(), utcnow(), topic_id=23576)
                assert await engine.ingest(item) == "open"
                snapshot = {
                    key: getattr(exchange, key)
                    for key in (
                        "row",
                        "protection",
                        "exposure",
                        "submissions",
                        "amendments",
                        "orders",
                        "algos",
                        "latest_client",
                        "latest_algo_client",
                        "fills_data",
                        "bills_data",
                    )
                }
                snapshot["message_date"] = item.date.isoformat()
                saved.write_text(json.dumps(snapshot, default=str))
                (directory / "phase1.json").write_text('{"open_and_protected": true}')
            await stop.wait()
    finally:
        await db.engine.dispose()


if __name__ == "__main__":
    asyncio.run(main())
