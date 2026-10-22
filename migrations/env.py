import asyncio
import os
from logging.config import fileConfig

from alembic import context
from sqlalchemy import pool
from sqlalchemy.ext.asyncio import async_engine_from_config

from okx_bot.models import Base

config = context.config
external_connection = config.attributes.get("connection")
if external_connection is None:
    # Explicit DB URLs (including disposable tests) must not discover a Demo .env.
    database_url = os.environ.get("DATABASE_URL")
    if database_url is None:
        from okx_bot.config import Settings

        database_url = Settings().database_url
    config.set_main_option("sqlalchemy.url", database_url.replace("%", "%%"))
if config.get_section("loggers"):
    fileConfig(config.config_file_name)
target_metadata = Base.metadata


def run(connection):
    context.configure(connection=connection, target_metadata=target_metadata, compare_type=True)
    with context.begin_transaction():
        context.run_migrations()


async def online():
    engine = async_engine_from_config(
        config.get_section(config.config_ini_section),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )
    async with engine.connect() as connection:
        await connection.run_sync(run)
    await engine.dispose()


if external_connection is not None:
    run(external_connection)
elif context.is_offline_mode():
    context.configure(
        url=config.get_main_option("sqlalchemy.url"),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )
    with context.begin_transaction():
        context.run_migrations()
else:
    asyncio.run(online())
