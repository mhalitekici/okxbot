"""Read-only runtime, PostgreSQL and OKX diagnosis; no reconciliation or unlock."""

import asyncio
import json
from dataclasses import asdict
from datetime import UTC, datetime

from sqlalchemy import func, select, text

from okx_bot.config import Settings
from okx_bot.db import Database
from okx_bot.diagnostics import failure_details
from okx_bot.exchange.client import ReadOnlyOKXClient as ReadOnlyClient
from okx_bot.exchange.gateway import OKXGateway
from okx_bot.models import BotEvent, DailyRisk, Execution, Position, RuntimeState, TradeOrder


async def main():
    settings = Settings()
    report = {
        "checked_at": datetime.now(UTC).isoformat(),
        "mode": "demo",
        "okx_demo": settings.okx_demo,
        "live_enabled": settings.live_trading_enabled,
        "read_only": True,
    }
    db = Database(settings.database_url)
    client = ReadOnlyClient(settings)
    gateway = OKXGateway(client)
    try:
        try:
            async with db.sessions() as session:
                await session.execute(text("SET TRANSACTION READ ONLY"))
                state = await session.get(RuntimeState, 1)
                report["database"] = {
                    "migration": await session.scalar(
                        text("SELECT version_num FROM alembic_version")
                    ),
                    "state": {
                        k: getattr(state, k)
                        for k in (
                            "paused",
                            "emergency",
                            "reason",
                            "state_revision",
                            "emergency_at",
                            "last_reconciliation_at",
                            "last_reconciliation_ok",
                            "last_reconciliation_error",
                            "reconciliation_recovered_at",
                            "last_reconciliation_summary",
                        )
                    },
                    "counts": {
                        model.__tablename__: await session.scalar(
                            select(func.count()).select_from(model)
                        )
                        for model in (TradeOrder, Position, Execution)
                    },
                }
                events = (
                    await session.scalars(
                        select(BotEvent)
                        .where(
                            BotEvent.kind.in_(
                                [
                                    "emergency",
                                    "reconciliation_failed",
                                    "reconciliation_recovered",
                                    "resumed",
                                ]
                            )
                        )
                        .order_by(BotEvent.id.desc())
                        .limit(10)
                    )
                ).all()
                report["database"]["events"] = [
                    {"kind": e.kind, "at": e.created_at, "details": e.details} for e in events
                ]
                daily = await session.get(DailyRisk, datetime.now(UTC).date())
                report["database"]["daily_loss_latched"] = daily.loss_latched if daily else None
        except Exception as exc:
            report["database_error"] = failure_details(exc, "diagnostic_database")
        report["okx"] = {}
        for name, operation in (
            ("account_config", gateway.account_config),
            ("positions", gateway.positions),
            ("pending_orders", gateway.open_orders),
            ("pending_algos", gateway.open_algo_orders),
            ("balance", gateway.balance),
        ):
            try:
                value = await operation()
                if name == "account_config":
                    value = {k: value[k] for k in ("acctLv", "posMode")}
                elif name == "balance":
                    value = asdict(value)
                else:
                    value = {
                        "count": len(value),
                        "instruments": sorted({r["instId"] for r in value}),
                    }
                report["okx"][name] = {"ok": True, "data": value}
            except Exception as exc:
                report["okx"][name] = {"ok": False, **failure_details(exc, name)}
        print(json.dumps(report, ensure_ascii=False, default=str, indent=2))
    finally:
        await client.close()
        await db.engine.dispose()


if __name__ == "__main__":
    asyncio.run(main())
