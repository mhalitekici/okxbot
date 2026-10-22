"""Live account observation only. No TradingEngine, mutation gateway or order executor."""

import asyncio
import logging
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal

from sqlalchemy import func, select, text

from okx_bot.domain import utcnow
from okx_bot.live_connection import (
    ALGOS,
    BALANCE,
    CONFIG,
    ORDERS,
    POSITIONS,
    LiveCheckError,
    _number,
    _pending_ids,
)
from okx_bot.live_deployment import DeploymentError
from okx_bot.models import (
    BotEvent,
    CapitalCycle,
    CapitalSlot,
    DailyReport,
    PersistentSettings,
    RuntimeState,
    TelegramSignal,
    TradeOrder,
)
from okx_bot.parser import ChannelSignalParser


@dataclass(frozen=True)
class LiveSnapshot:
    equity_usd: Decimal
    equity_usdt: Decimal
    available_usdt: Decimal
    positions: int
    orders: int
    tpsl: int

    @property
    def flat(self):
        return self.positions == self.orders == self.tpsl == 0

    def safe(self):
        return {
            "equity_usd": str(self.equity_usd),
            "equity_usdt": str(self.equity_usdt),
            "available_usdt": str(self.available_usdt),
            "positions": self.positions,
            "pending_orders": self.orders,
            "pending_tpsl": self.tpsl,
            "account_mode": "Futures (2)",
            "position_mode": "net_mode",
            "observed_at": utcnow().isoformat(),
        }


async def read_snapshot(client, identity):
    rows = await client.request("GET", CONFIG)
    if len(rows) != 1 or rows[0].get("acctLv") != "2" or rows[0].get("posMode") != "net_mode":
        raise DeploymentError("IDENTITY_MISMATCH")
    identity.verify(client._config, rows[0])
    balances = await client.request("GET", BALANCE)
    if len(balances) != 1 or not isinstance(balances[0].get("details"), list):
        raise LiveCheckError("RESPONSE", 200)
    details = balances[0]["details"]
    if not all(isinstance(item, dict) for item in details):
        raise LiveCheckError("RESPONSE", 200)
    usdt = [item for item in details if item.get("ccy") == "USDT"]
    if len(usdt) > 1:
        raise LiveCheckError("RESPONSE", 200)
    positions = await client.request("GET", POSITIONS)
    pending = await _pending_ids(client, ORDERS)
    conditional = await _pending_ids(client, ALGOS, "conditional")
    oco = await _pending_ids(client, ALGOS, "oco")
    if conditional & oco:
        raise LiveCheckError("PAGINATION", 200)
    return LiveSnapshot(
        equity_usd=_number(balances[0].get("totalEq")),
        equity_usdt=_number(usdt[0].get("eq")) if usdt else Decimal(0),
        available_usdt=_number(usdt[0].get("availBal")) if usdt else Decimal(0),
        positions=sum(_number(row.get("pos")) != 0 for row in positions),
        orders=len(pending),
        tpsl=len(conditional | oco),
    )


async def require_schema(database):
    # No startup migration: an operator must explicitly migrate the separate Live DB.
    async with database.engine.connect() as connection:
        version = await connection.scalar(text("SELECT version_num FROM alembic_version"))
        if version != "0004":
            raise DeploymentError("SCHEMA_MIGRATION_REQUIRED")


class LiveObserver:
    def __init__(self, deployment, db, client, identity):
        self.deployment, self.db, self.client, self.identity = deployment, db, client, identity
        self.lock = asyncio.Lock()
        self.healthy = False
        self.fatal = False
        self.last_sync = None

    async def state(self):
        async with self.db.sessions() as session:
            return await session.get(RuntimeState, 1)

    async def initialize(self):
        # Must be called under Database.singleton. Bind an empty schema once.
        async with self.db.sessions.begin() as session:
            saved = await session.get(PersistentSettings, 1)
            trades = await session.scalar(select(func.count()).select_from(TradeOrder))
            if trades:
                raise DeploymentError("DATABASE_GUARD")
            if saved is None:
                signals = await session.scalar(select(func.count()).select_from(TelegramSignal))
                cycles = await session.scalar(select(func.count()).select_from(CapitalCycle))
                if signals or cycles:
                    raise DeploymentError("DATABASE_GUARD")
                session.add(
                    PersistentSettings(
                        id=1,
                        environment="live",
                        account_fingerprint=self.identity.fingerprint,
                        values=self.deployment.model_dump(mode="json"),
                    )
                )
            elif (
                saved.environment != "live"
                or saved.account_fingerprint != self.identity.fingerprint
                or saved.values != self.deployment.model_dump(mode="json")
            ):
                raise DeploymentError("DATABASE_GUARD")
            state = await session.get(RuntimeState, 1)
            if state is None:
                state = RuntimeState(id=1)
                session.add(state)
            state.paused = True
            state.last_reconciliation_ok = False
            # Restart never clears emergency, cursor or Telegram update offset.

    async def trip(self, code="CONNECTION_LOST"):
        self.healthy = False
        safe_code = DeploymentError(code).code
        logging.getLogger(__name__).warning(safe_code)
        async with self.db.sessions.begin() as session:
            state = await session.get(RuntimeState, 1, with_for_update=True)
            state.paused = state.emergency = True
            state.reason = safe_code
            state.emergency_at = state.emergency_at or utcnow()
            state.last_reconciliation_ok = False

    async def pause(self, **kwargs):
        async with self.db.sessions.begin() as session:
            state = await session.get(RuntimeState, 1, with_for_update=True)
            state.paused = True

    async def event(self, kind, **details):
        if kind == "telegram_disconnected":
            await self.trip("TELEGRAM_DISCONNECTED")

    async def refresh(self):
        async with self.lock:
            try:
                snapshot = await read_snapshot(self.client, self.identity)
                async with self.db.sessions.begin() as session:
                    state = await session.get(RuntimeState, 1, with_for_update=True)
                    state.paused = True
                    state.last_reconciliation_summary = snapshot.safe()
                    state.last_reconciliation_at = utcnow()
                    state.last_reconciliation_ok = True
                    if not snapshot.flat:
                        state.emergency = True
                        state.reason = "EXTERNAL_EXPOSURE"
                        state.emergency_at = state.emergency_at or utcnow()
                    cycle = await session.scalar(
                        select(CapitalCycle).where(CapitalCycle.active_key == "live")
                    )
                    if (
                        cycle is None
                        and snapshot.flat
                        and not state.emergency
                        and snapshot.equity_usdt > 0
                        and snapshot.available_usdt > 0
                    ):
                        cycle = CapitalCycle(
                            environment="live",
                            active_key="live",
                            slot_count=3,
                            allocation_base_equity=snapshot.equity_usdt,
                            slot_target_margin=snapshot.equity_usdt / 3,
                        )
                        session.add(cycle)
                        await session.flush()
                        for number in range(1, 4):
                            session.add(
                                CapitalSlot(
                                    cycle_id=cycle.id, number=number, reserved_margin=Decimal(0)
                                )
                            )
                self.last_sync, self.healthy = utcnow(), True
                return snapshot
            except Exception:
                await self.trip("CONNECTION_LOST")
                raise DeploymentError("CONNECTION_LOST") from None

    async def ingest(self, message):
        # No sizing, reservation, leverage request or trade intent is created here.
        if (
            message.channel_id != self.deployment.telegram_channel_id
            or message.topic_id != self.deployment.telegram_topic_id
        ):
            return "ignored_topic"
        try:
            parsed = (
                ChannelSignalParser(
                    self.deployment.mztradingbot_channel_id, self.deployment.telegram_topic_id
                )
                .parse(message)
                .model_dump(mode="json")
            )
        except ValueError:
            parsed = None
        async with self.lock, self.db.sessions.begin() as session:
            found = await session.scalar(
                select(TelegramSignal).where(
                    TelegramSignal.channel_id == message.channel_id,
                    TelegramSignal.message_id == message.message_id,
                )
            )
            if found:
                return "duplicate"
            session.add(
                TelegramSignal(
                    channel_id=message.channel_id,
                    message_id=message.message_id,
                    original_text=message.text,
                    message_date=message.date,
                    edited=message.edited,
                    parsed=parsed,
                    status="rejected",
                    reason="LIVE_EXECUTION_DISABLED",
                )
            )
        return "rejected"

    async def status(self):
        state = await self.state()
        async with self.db.sessions() as session:
            cycle = await session.scalar(
                select(CapitalCycle).where(CapitalCycle.active_key == "live")
            )
            signals = await session.scalar(select(func.count()).select_from(TelegramSignal))
        return {
            "mode": "live-observer",
            "paused": True,
            "live_trading_enabled": False,
            "order_execution": "DISABLED",
            "emergency": state.emergency,
            "requires_admin_approval": True,
            "reason": state.reason,
            "exchange_healthy": self.healthy,
            "snapshot": state.last_reconciliation_summary,
            "signals_recorded": signals,
            "allocation": {
                "slots": 3,
                "cycle_id": cycle.id if cycle else None,
                "equity_usdt": str(cycle.allocation_base_equity) if cycle else None,
                "slot_margin_usdt": str(cycle.slot_target_margin) if cycle else None,
                "state": "waiting_for_funded_flat_account" if cycle is None else "observing",
            },
            # There can be no owned Live fills in this observer-only release.
            "bot_pnl": {"realized_usdt": "0", "source": "no_live_executions"},
        }

    async def queue_daily(self):
        day = (datetime.now(UTC) + timedelta(hours=3)).date()
        async with self.db.sessions.begin() as session:
            if await session.get(DailyReport, day):
                return
            event = BotEvent(
                kind="live_observer_daily",
                event_key=f"daily:live:{day}",
                details={"mode": "live-observer", "realized_usdt": "0", "notify": True},
            )
            session.add(event)
            await session.flush()
            session.add(DailyReport(day=day, event_id=event.id))

    async def request_resume(self, **kwargs):
        raise DeploymentError("EXECUTION_DISABLED")

    async def resume(self, **kwargs):
        raise DeploymentError("EXECUTION_DISABLED")
