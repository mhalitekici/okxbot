from datetime import date, datetime
from decimal import Decimal

from sqlalchemy import (
    JSON,
    BigInteger,
    Boolean,
    Date,
    DateTime,
    ForeignKey,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from okx_bot.decimal_type import PreciseDecimal
from okx_bot.domain import utcnow


class Base(DeclarativeBase):
    pass


class TelegramSignal(Base):
    __tablename__ = "telegram_signals"
    __table_args__ = (UniqueConstraint("channel_id", "message_id"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    channel_id: Mapped[int] = mapped_column(BigInteger)
    message_id: Mapped[int] = mapped_column(BigInteger)
    original_text: Mapped[str] = mapped_column(Text)
    message_date: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    parsed: Mapped[dict | None] = mapped_column(JSON)
    status: Mapped[str] = mapped_column(String(32), default="received", index=True)
    reason: Mapped[str | None] = mapped_column(Text)
    order_id: Mapped[str | None] = mapped_column(String(64))
    edited: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class TradeOrder(Base):
    __tablename__ = "trade_orders"
    id: Mapped[int] = mapped_column(primary_key=True)
    signal_id: Mapped[int] = mapped_column(ForeignKey("telegram_signals.id"), unique=True)
    client_id: Mapped[str] = mapped_column(String(32), unique=True)
    exchange_id: Mapped[str | None] = mapped_column(String(64), unique=True)
    algo_client_id: Mapped[str] = mapped_column(String(32), unique=True)
    algo_id: Mapped[str | None] = mapped_column(String(64))
    inst_id: Mapped[str] = mapped_column(String(64), index=True)
    direction: Mapped[str] = mapped_column(String(8))
    pos_side: Mapped[str] = mapped_column(String(8))
    contracts: Mapped[Decimal] = mapped_column(PreciseDecimal())
    filled: Mapped[Decimal] = mapped_column(PreciseDecimal(), default=Decimal(0))
    entry: Mapped[Decimal] = mapped_column(PreciseDecimal())
    reference_price: Mapped[Decimal] = mapped_column(PreciseDecimal())
    take_profit: Mapped[Decimal] = mapped_column(PreciseDecimal())
    stop_loss: Mapped[Decimal] = mapped_column(PreciseDecimal())
    execution_context: Mapped[dict | None] = mapped_column(JSON)
    environment: Mapped[str] = mapped_column(String(8), default="demo", server_default="demo")
    cycle_id: Mapped[int | None] = mapped_column(ForeignKey("capital_cycles.id"))
    slot_number: Mapped[int | None]
    target_margin: Mapped[Decimal | None] = mapped_column(PreciseDecimal())
    actual_margin: Mapped[Decimal | None] = mapped_column(PreciseDecimal())
    estimated_risk: Mapped[Decimal | None] = mapped_column(PreciseDecimal())
    position_id: Mapped[str | None] = mapped_column(String(64))
    closed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    close_cause: Mapped[str | None] = mapped_column(String(32))
    accounting_complete: Mapped[bool] = mapped_column(
        Boolean, default=False, server_default="false"
    )
    funding_verified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    status: Mapped[str] = mapped_column(String(32), default="prepared", index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class Position(Base):
    __tablename__ = "positions"
    id: Mapped[int] = mapped_column(primary_key=True)
    order_id: Mapped[int] = mapped_column(ForeignKey("trade_orders.id"), unique=True)
    inst_id: Mapped[str] = mapped_column(String(64))
    pos_side: Mapped[str] = mapped_column(String(8))
    contracts: Mapped[Decimal] = mapped_column(PreciseDecimal())
    avg_price: Mapped[Decimal] = mapped_column(PreciseDecimal())
    liquidation_price: Mapped[Decimal | None] = mapped_column(PreciseDecimal())
    status: Mapped[str] = mapped_column(String(32), default="open", index=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class Execution(Base):
    __tablename__ = "executions"
    __table_args__ = (UniqueConstraint("inst_id", "trade_id"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    order_id: Mapped[int | None] = mapped_column(ForeignKey("trade_orders.id"), index=True)
    role: Mapped[str | None] = mapped_column(String(8))
    side: Mapped[str | None] = mapped_column(String(8))
    inst_id: Mapped[str] = mapped_column(String(64))
    trade_id: Mapped[str] = mapped_column(String(64))
    exchange_order_id: Mapped[str] = mapped_column(String(64), index=True)
    price: Mapped[Decimal] = mapped_column(PreciseDecimal())
    contracts: Mapped[Decimal] = mapped_column(PreciseDecimal())
    pnl: Mapped[Decimal] = mapped_column(PreciseDecimal())
    fee: Mapped[Decimal] = mapped_column(PreciseDecimal())
    fee_ccy: Mapped[str] = mapped_column(String(16))
    timestamp: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class BotEvent(Base):
    __tablename__ = "bot_events"
    id: Mapped[int] = mapped_column(primary_key=True)
    event_key: Mapped[str | None] = mapped_column(String(128), unique=True)
    delivery_state: Mapped[str] = mapped_column(
        String(16), default="pending", server_default="pending"
    )
    telegram_message_id: Mapped[int | None] = mapped_column(BigInteger)
    kind: Mapped[str] = mapped_column(String(64))
    details: Mapped[dict] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    delivered_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class RiskEvent(Base):
    __tablename__ = "risk_events"
    id: Mapped[int] = mapped_column(primary_key=True)
    signal_id: Mapped[int | None] = mapped_column(ForeignKey("telegram_signals.id"))
    reason: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class RuntimeState(Base):
    __tablename__ = "runtime_state"
    id: Mapped[int] = mapped_column(primary_key=True, default=1)
    paused: Mapped[bool] = mapped_column(Boolean, default=False)
    emergency: Mapped[bool] = mapped_column(Boolean, default=False)
    reason: Mapped[str | None] = mapped_column(Text)
    channel_id: Mapped[int | None] = mapped_column(BigInteger)
    channel_cursor: Mapped[int | None] = mapped_column(BigInteger)
    channel_started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    bot_update_offset: Mapped[int] = mapped_column(BigInteger, default=0)
    state_revision: Mapped[int] = mapped_column(default=0, server_default="0")
    emergency_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_reconciliation_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_reconciliation_ok: Mapped[bool | None] = mapped_column(Boolean)
    last_reconciliation_error: Mapped[dict | None] = mapped_column(JSON)
    reconciliation_recovered_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_reconciliation_summary: Mapped[dict | None] = mapped_column(JSON)


class DailyRisk(Base):
    __tablename__ = "daily_risk"
    day: Mapped[date] = mapped_column(Date, primary_key=True)
    starting_equity: Mapped[Decimal] = mapped_column(PreciseDecimal())
    loss_latched: Mapped[bool] = mapped_column(Boolean, default=False)


class PersistentSettings(Base):
    __tablename__ = "persistent_settings"
    id: Mapped[int] = mapped_column(primary_key=True, default=1)
    environment: Mapped[str] = mapped_column(String(8), default="demo")
    account_fingerprint: Mapped[str | None] = mapped_column(String(64))
    values: Mapped[dict] = mapped_column(JSON)
    revision: Mapped[int] = mapped_column(default=0)


class CapitalCycle(Base):
    __tablename__ = "capital_cycles"
    id: Mapped[int] = mapped_column(primary_key=True)
    environment: Mapped[str] = mapped_column(String(8), default="demo")
    # NULL for completed cycles; unique non-NULL key permits one active cycle per mode.
    active_key: Mapped[str | None] = mapped_column(String(8), unique=True)
    allocation_base_equity: Mapped[Decimal] = mapped_column(PreciseDecimal())
    slot_count: Mapped[int]
    slot_target_margin: Mapped[Decimal] = mapped_column(PreciseDecimal())
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class CapitalSlot(Base):
    __tablename__ = "capital_slots"
    __table_args__ = (UniqueConstraint("cycle_id", "number"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    cycle_id: Mapped[int] = mapped_column(ForeignKey("capital_cycles.id"))
    number: Mapped[int]
    order_id: Mapped[int | None] = mapped_column(ForeignKey("trade_orders.id"), unique=True)
    reserved_margin: Mapped[Decimal] = mapped_column(PreciseDecimal(), default=Decimal(0))


class OrderLink(Base):
    __tablename__ = "order_links"
    id: Mapped[int] = mapped_column(primary_key=True)
    order_id: Mapped[int] = mapped_column(ForeignKey("trade_orders.id"), index=True)
    exchange_id: Mapped[str] = mapped_column(String(64), unique=True)
    role: Mapped[str] = mapped_column(String(8))
    cause: Mapped[str | None] = mapped_column(String(32))


class AccountBill(Base):
    __tablename__ = "account_bills"
    id: Mapped[int] = mapped_column(primary_key=True)
    bill_id: Mapped[str] = mapped_column(String(64), unique=True)
    order_id: Mapped[int | None] = mapped_column(ForeignKey("trade_orders.id"), index=True)
    kind: Mapped[str] = mapped_column(String(16))
    inst_id: Mapped[str | None] = mapped_column(String(64))
    amount: Mapped[Decimal] = mapped_column(PreciseDecimal())
    currency: Mapped[str] = mapped_column(String(16))
    timestamp: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class DailyReport(Base):
    __tablename__ = "daily_reports"
    day: Mapped[date] = mapped_column(Date, primary_key=True)
    event_id: Mapped[int] = mapped_column(ForeignKey("bot_events.id"), unique=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
