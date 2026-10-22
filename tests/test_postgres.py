import asyncio
import importlib
import os
import subprocess
import sys

import httpx
import pytest
from sqlalchemy import select
from sqlalchemy.engine import make_url

from okx_bot.db import Database
from okx_bot.domain import ExchangeError, Message, utcnow
from okx_bot.engine import TradingEngine
from okx_bot.models import Position
from tests.fakes import text

pytestmark = pytest.mark.postgres


@pytest.fixture
async def postgres():
    url = os.environ.get("TEST_POSTGRES_URL")
    if not url:
        pytest.skip("Run scripts/check_postgres.py for an isolated, migrated Docker database")
    parsed = make_url(url)
    if (
        parsed.host != "127.0.0.1"
        or not parsed.database.startswith("okx_test")
        or os.environ.get("TEST_POSTGRES_DISPOSABLE") != parsed.database
    ):
        pytest.fail("PostgreSQL tests require an explicitly disposable, local test database")
    database = Database(url)
    await database.initialize_state()
    yield database
    await database.engine.dispose()


async def test_postgres_singleton_prevents_two_workers(postgres):
    async with postgres.singleton():
        with pytest.raises(RuntimeError, match="Another trading"):
            async with postgres.singleton():
                pytest.fail("Two workers cannot own the same database")


async def test_decimal_position_survives_restart(postgres, settings, exchange):
    engine = TradingEngine(settings, postgres, exchange)
    await engine.reconcile(startup=True)
    item = Message(
        settings.telegram_channel_id, 10, text(), utcnow(), topic_id=settings.telegram_topic_id
    )
    assert await engine.ingest(item) == "open"
    restarted = TradingEngine(settings, postgres, exchange)
    await restarted.reconcile(startup=True)
    assert await restarted.ingest(item) == "duplicate"
    async with postgres.sessions() as session:
        position = await session.scalar(select(Position))
        assert position.status == "open"
        assert position.avg_price == 100
    await restarted.close("BTC-USDT-SWAP")


async def test_fastapi_lifespan_with_postgres_and_mock_external_services(
    postgres, settings, exchange, monkeypatch
):
    module = importlib.import_module("okx_bot.app")
    settings.database_url = os.environ["TEST_POSTGRES_URL"]
    settings.telegram_api_id = 12345
    settings.telegram_api_hash = "offline-test"
    settings.telegram_session_string = "offline-test"
    settings.telegram_bot_token = "offline-test"
    settings.okx_api_key = "offline-demo-test"
    settings.okx_api_secret = "offline-demo-test"
    settings.okx_api_passphrase = "offline-demo-test"

    class Listener:
        def __init__(self, *_):
            self.ready = False

        async def run(self):
            self.ready = True
            await asyncio.Event().wait()

    class Bot:
        def __init__(self, *_):
            pass

        async def poll(self):
            await asyncio.Event().wait()

        async def notifications(self):
            await asyncio.Event().wait()

    monkeypatch.setattr(module, "OKXGateway", lambda _: exchange)
    monkeypatch.setattr(module, "TelegramListener", Listener)
    monkeypatch.setattr(module, "AdminBot", Bot)
    app = module.create_app(settings)
    async with app.router.lifespan_context(app):
        await asyncio.sleep(0)
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            assert (await client.get("/health/ready")).status_code == 200
            approval = await app.state.engine.request_resume(actor="test_admin")
            await app.state.engine.resume(confirmation=approval["confirmation"], actor="test_admin")
            content = Message(
                settings.telegram_channel_id,
                99,
                text(),
                utcnow(),
                topic_id=settings.telegram_topic_id,
            )
            assert await app.state.engine.ingest(content) == "open"
            response = await client.post(
                "/admin/positions/BTC-USDT-SWAP/close",
                headers={"Authorization": "Bearer " + "x" * 40},
            )
            assert response.status_code == 200
            assert not exchange.exposure


@pytest.mark.parametrize(
    "startup_error", [RuntimeError("SECRET_PROVIDER_EXCEPTION"), ExchangeError("HTTP_401")]
)
async def test_failed_startup_keeps_admin_available_and_preserves_lock(
    postgres, settings, exchange, monkeypatch, startup_error
):
    module = importlib.import_module("okx_bot.app")
    settings.database_url = os.environ["TEST_POSTGRES_URL"]
    settings.telegram_api_id = 12345
    settings.telegram_api_hash = "offline-test"
    settings.telegram_session_string = "offline-test"
    settings.telegram_bot_token = "offline-test"
    settings.okx_api_key = "offline-demo-test"
    settings.okx_api_secret = "offline-demo-test"
    settings.okx_api_passphrase = "offline-demo-test"
    original = exchange.account_config

    async def broken():
        raise startup_error

    exchange.account_config = broken
    monkeypatch.setattr(module, "OKXGateway", lambda _: exchange)

    class Listener:
        def __init__(self, *_):
            self.ready = True

        async def run(self):
            await asyncio.Event().wait()

    class Bot:
        def __init__(self, *_):
            pass

        async def poll(self):
            await asyncio.Event().wait()

        async def notifications(self):
            await asyncio.Event().wait()

    monkeypatch.setattr(module, "TelegramListener", Listener)
    monkeypatch.setattr(module, "AdminBot", Bot)
    app = module.create_app(settings)
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            assert (await client.get("/health/live")).status_code == 200
            assert (await client.get("/health/ready")).status_code == 503
            response = await client.get(
                "/admin/status", headers={"Authorization": "Bearer " + "x" * 40}
            )
            assert response.status_code == 200 and response.json()["emergency"]
            assert "SECRET" not in response.text
    exchange.account_config = original
    restarted = TradingEngine(settings, postgres, exchange)
    await restarted.reconcile(startup=True)
    state = await restarted.state()
    assert state.emergency and state.paused
    assert state.last_reconciliation_error["stage"] == "account_config"
    assert "SECRET" not in str(state.last_reconciliation_error)
    approval = await restarted.request_resume(actor="startup_test_admin")
    await restarted.resume(confirmation=approval["confirmation"], actor="startup_test_admin")


async def test_migration_preserves_legacy_emergency_and_existing_records(
    postgres, settings, exchange
):
    from sqlalchemy import text as sql

    from okx_bot.models import RuntimeState

    # Only the disposable TEST_POSTGRES_URL used by check_postgres.py is touched.
    env = {**os.environ, "DATABASE_URL": os.environ["TEST_POSTGRES_URL"]}
    await postgres.engine.dispose()
    await asyncio.to_thread(
        subprocess.run, [sys.executable, "-m", "alembic", "downgrade", "0001"], env=env, check=True
    )
    async with postgres.engine.begin() as connection:
        await connection.execute(
            sql(
                "UPDATE runtime_state SET paused=true, emergency=true, "
                "reason='Reconciliation failed; new orders disabled' WHERE id=1"
            )
        )
        before = await connection.scalar(sql("SELECT count(*) FROM trade_orders"))
    await postgres.engine.dispose()
    await asyncio.to_thread(
        subprocess.run, [sys.executable, "-m", "alembic", "upgrade", "head"], env=env, check=True
    )
    await postgres.engine.dispose()
    async with postgres.sessions() as session:
        state = await session.get(RuntimeState, 1)
        assert state.emergency and state.paused
        assert state.reason == "Reconciliation failed; new orders disabled"
        assert state.state_revision == 0
        assert state.last_reconciliation_error is None
        assert await session.scalar(sql("SELECT count(*) FROM trade_orders")) == before
    restarted = TradingEngine(settings, postgres, exchange)
    await restarted.reconcile(startup=True)
    assert (await restarted.state()).emergency
    approval = await restarted.request_resume(actor="migration_test_admin")
    await restarted.resume(confirmation=approval["confirmation"], actor="migration_test_admin")
    assert not (await restarted.state()).emergency


async def test_postgres_atomic_slot_reservations_and_persistence(postgres, settings):
    from decimal import Decimal as D

    from okx_bot.allocation import Allocator
    from okx_bot.models import CapitalSlot, TelegramSignal, TradeOrder
    from okx_bot.risk import Balance

    allocator = Allocator(postgres, settings)
    limits = await allocator.limits()
    cycle = await allocator.cycle(Balance(D(1500), D(1500), D(0)), verified_flat=True, force=True)
    assert cycle.slot_target_margin == 500
    orders = []
    async with postgres.sessions.begin() as session:
        for number in range(4):
            signal = TelegramSignal(
                channel_id=-10098765,
                message_id=number + 1,
                original_text="test",
                message_date=utcnow(),
            )
            session.add(signal)
            await session.flush()
            orders.append(
                TradeOrder(
                    signal_id=signal.id,
                    client_id=f"concurrent{number}",
                    algo_client_id=f"protect{number}",
                    inst_id=f"TEST{number}-USDT-SWAP",
                    direction="long",
                    pos_side="net",
                    contracts=D(1),
                    entry=D(100),
                    reference_price=D(100),
                    take_profit=D(103),
                    stop_loss=D(97),
                    estimated_risk=D(3),
                )
            )
    results = await asyncio.gather(
        *(
            allocator.reserve(o, cycle, D(100), Balance(D(1500), D(1500), D(0)), limits)
            for o in orders
        ),
        return_exceptions=True,
    )
    assert sum(r is None for r in results) == 3
    async with postgres.sessions.begin() as session:
        slots = list(
            (
                await session.scalars(select(CapitalSlot).where(CapitalSlot.cycle_id == cycle.id))
            ).all()
        )
        assert len({s.order_id for s in slots}) == 3
        for slot in slots:
            order = await session.get(TradeOrder, slot.order_id)
            order.status = "canceled"
            await Allocator.release(session, order.id)
    restarted = Allocator(postgres, settings)
    assert (await restarted.summary())["slot_target_margin"] == "500.000000000000000000"
