from contextlib import asynccontextmanager

from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from okx_bot.models import RuntimeState


class Database:
    def __init__(self, url: str):
        self.engine = create_async_engine(url, pool_pre_ping=True)
        self.sessions = async_sessionmaker(self.engine, expire_on_commit=False)

    async def initialize_state(self):
        async with self.sessions.begin() as session:
            if await session.get(RuntimeState, 1) is None:
                session.add(RuntimeState(id=1))

    @asynccontextmanager
    async def singleton(self):
        # A dedicated connection holds the advisory lock for the whole process.
        # Losing it stops the service supervisor; multiple replicas are unsupported.
        async with self.engine.connect() as connection:
            locked = await connection.scalar(text("SELECT pg_try_advisory_lock(721046382)"))
            if not locked:
                raise RuntimeError("Another trading service owns this database")
            try:
                yield connection
            finally:
                await connection.execute(text("SELECT pg_advisory_unlock(721046382)"))
