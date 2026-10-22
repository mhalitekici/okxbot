import pytest

from okx_bot.config import Settings
from okx_bot.db import Database
from okx_bot.engine import TradingEngine
from okx_bot.models import Base
from tests.fakes import DemoExchange


@pytest.fixture
def settings():
    return Settings(
        _env_file=None,
        telegram_channel_id=-100123,
        telegram_topic_id=23576,
        telegram_admin_id=42,
        order_poll_attempts=2,
        order_poll_interval_seconds=0,
        admin_api_token="x" * 40,
        dedicated_trading_account_confirmed=True,  # The offline broker is exclusive to this bot.
    )


@pytest.fixture
async def database(tmp_path):
    db = Database(f"sqlite+aiosqlite:///{tmp_path / 'test.db'}")
    async with db.engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    await db.initialize_state()
    yield db
    await db.engine.dispose()


@pytest.fixture
def exchange():
    return DemoExchange()


@pytest.fixture
async def engine(settings, database, exchange):
    engine = TradingEngine(settings, database, exchange)
    await engine.reconcile(startup=True)
    return engine
