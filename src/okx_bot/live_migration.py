"""Explicit schema upgrade for the isolated Live observer; no application startup."""

from pathlib import Path

from alembic import command
from alembic.config import Config
from sqlalchemy import insert, inspect, text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import create_async_engine

from okx_bot.live_deployment import DeploymentError
from okx_bot.models import RuntimeState


def validate_live_database_url(url):
    try:
        parsed = make_url(url)
        if (
            parsed.drivername != "postgresql+asyncpg"
            or parsed.host != "postgres-live"
            or parsed.database != "okx_live"
            or parsed.username != "okx_live"
            or parsed.port != 5432
            or not parsed.password
            or parsed.query
        ):
            raise DeploymentError("DATABASE_GUARD")
    except Exception:
        raise DeploymentError("DATABASE_GUARD") from None


def upgrade_schema(connection):
    # Shared with the observer's lifetime session lock: migration refuses an active worker.
    if not connection.scalar(text("SELECT pg_try_advisory_xact_lock(721046382)")):
        raise DeploymentError("DATABASE_GUARD")
    tables = inspect(connection).get_table_names()
    if "persistent_settings" in tables:
        mode = connection.scalar(text("SELECT environment FROM persistent_settings WHERE id=1"))
        if mode not in {None, "live"}:
            raise DeploymentError("DATABASE_GUARD")
    if "trade_orders" in tables and connection.scalar(text("SELECT count(*) FROM trade_orders")):
        raise DeploymentError("DATABASE_GUARD")
    # Only a fresh schema or one previously bound as Live may be upgraded.
    if "telegram_signals" in tables and connection.scalar(
        text("SELECT count(*) FROM telegram_signals")
    ):
        if "persistent_settings" not in tables or mode != "live":
            raise DeploymentError("DATABASE_GUARD")
    root = Path(__file__).resolve().parents[2]
    config = Config(str(root / "alembic.ini"))
    config.set_main_option("script_location", str(root / "migrations"))
    config.set_main_option("path_separator", "os")
    config.attributes["connection"] = connection
    command.upgrade(config, "head")
    if connection.scalar(text("SELECT id FROM runtime_state WHERE id=1")) is None:
        connection.execute(insert(RuntimeState).values(id=1, paused=True, emergency=False))
    connection.execute(text("UPDATE runtime_state SET paused=true WHERE id=1"))


async def migrate_live_database(url):
    validate_live_database_url(url)
    engine = create_async_engine(url)
    try:
        async with engine.begin() as connection:
            await connection.run_sync(upgrade_schema)
    except Exception:
        raise DeploymentError("DATABASE_GUARD") from None
    finally:
        await engine.dispose()
