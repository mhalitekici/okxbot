"""Only disposable loopback test clusters; never Docker, Live or Demo databases."""

import asyncio
import os
import subprocess
import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal as D
from pathlib import Path

import pytest
from pydantic import SecretStr
from sqlalchemy import select, text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import create_async_engine

from okx_bot.db import Database
from okx_bot.live_connection import LiveReadOnlyClient, LiveReadOnlyConfig
from okx_bot.live_deployment import AccountIdentity, DeploymentError, LiveDeployment
from okx_bot.live_migration import upgrade_schema
from okx_bot.live_observer import LiveObserver
from okx_bot.models import CapitalCycle, PersistentSettings, RuntimeState
from tests.test_live_deployment import ACCOUNT, account_fingerprint, mock_wire

pytestmark = pytest.mark.postgres


@pytest.fixture
async def live_pg():
    raw = os.environ.get("TEST_POSTGRES_URL")
    if not raw:
        pytest.skip("Use the isolated PostgreSQL validation helper")
    original = make_url(raw)
    assert original.host == "127.0.0.1" and original.database.startswith("okx_test")
    assert os.environ.get("TEST_POSTGRES_DISPOSABLE") == original.database
    name = "okx_test_live_" + uuid.uuid4().hex[:12]
    control = create_async_engine(original.set(database="postgres"))
    async with control.connect() as connection:
        await connection.execution_options(isolation_level="AUTOCOMMIT")
        await connection.execute(text(f'CREATE DATABASE "{name}"'))
    await control.dispose()
    database = Database(original.set(database=name).render_as_string(hide_password=False))
    async with database.engine.begin() as connection:
        await connection.run_sync(upgrade_schema)
    yield database
    await database.engine.dispose()
    # The enclosing disposable cluster is stopped by the helper; retain it for inspection.


async def test_live_schema_starts_paused_and_singleton_refuses_second_worker(live_pg):
    async with live_pg.sessions() as session:
        assert (await session.get(RuntimeState, 1)).paused
    async with live_pg.singleton():
        with pytest.raises(RuntimeError, match="Another trading"):
            async with live_pg.singleton():
                pytest.fail("Two observers acquired the same DB lease")
        with pytest.raises(DeploymentError, match="DATABASE_GUARD"):
            async with live_pg.engine.begin() as connection:
                await connection.run_sync(upgrade_schema)


async def test_live_cycle_is_durable_and_never_uses_demo_equity(live_pg, monkeypatch):
    monkeypatch.setattr("okx_bot.live_connection.REQUEST_INTERVAL", 0)
    config = LiveReadOnlyConfig(
        base_url="https://eea.okx.com",
        api_key=SecretStr("mock"),
        api_secret=SecretStr("mock"),
        api_passphrase=SecretStr("mock"),
    )
    identity = AccountIdentity(
        manually_verified=True,
        base_url=config.base_url,
        fingerprint=account_fingerprint(ACCOUNT),
        verified_at=datetime.now(UTC) - timedelta(seconds=1),
        expires_at=datetime.now(UTC) + timedelta(days=1),
    )
    client = LiveReadOnlyClient(config, mock_wire(funded=True)[0])
    try:
        observer = LiveObserver(LiveDeployment(), live_pg, client, identity)
        async with live_pg.singleton():
            await observer.initialize()
            await observer.refresh()
            await observer.trip()
        restarted = LiveObserver(LiveDeployment(), live_pg, client, identity)
        async with live_pg.singleton():
            await restarted.initialize()
            await restarted.refresh()
        async with live_pg.sessions() as session:
            cycle = await session.scalar(select(CapitalCycle))
            assert cycle.environment == "live" and cycle.slot_target_margin == D(500)
            state = await session.get(RuntimeState, 1)
            assert state.paused and state.emergency
    finally:
        await client.close()


async def test_live_migration_refuses_demo_data(live_pg):
    async with live_pg.sessions.begin() as session:
        session.add(PersistentSettings(id=1, environment="demo", values={}))
    with pytest.raises(DeploymentError, match="DATABASE_GUARD"):
        async with live_pg.engine.begin() as connection:
            await connection.run_sync(upgrade_schema)


async def test_live_backup_restores_paused_schema_to_new_database(live_pg, tmp_path):
    binaries = Path(os.environ["TEST_POSTGRES_BIN"])
    parsed = make_url(live_pg.engine.url)
    target_name = "okx_test_restore_" + uuid.uuid4().hex[:12]
    dump = tmp_path / "live-test.dump"
    base = ["-h", parsed.host, "-p", str(parsed.port), "-U", parsed.username]
    hidden = {"creationflags": subprocess.CREATE_NO_WINDOW} if os.name == "nt" else {}
    for tool, args in (
        ("pg_dump", [*base, "-d", parsed.database, "-Fc", "-f", str(dump)]),
        ("createdb", [*base, target_name]),
        ("pg_restore", [*base, "-d", target_name, "--exit-on-error", "--no-owner", str(dump)]),
    ):
        result = await asyncio.to_thread(
            subprocess.run,
            [str(binaries / (tool + ".exe" if os.name == "nt" else tool)), *args],
            capture_output=True,
            text=True,
            timeout=30,
            **hidden,
        )
        assert result.returncode == 0, "Disposable backup/restore failed"
    restored = Database(parsed.set(database=target_name).render_as_string(hide_password=False))
    try:
        async with restored.sessions() as session:
            assert (await session.get(RuntimeState, 1)).paused
            assert await session.scalar(text("SELECT version_num FROM alembic_version")) == "0004"
    finally:
        await restored.engine.dispose()
