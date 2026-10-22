"""Fixed cycle budgets and atomic slot reservation, serialized by the durable state row."""

import hashlib
from decimal import Decimal

from sqlalchemy import func, select

from okx_bot.domain import RiskRejected, utcnow
from okx_bot.limits import Limits
from okx_bot.models import (
    BotEvent,
    CapitalCycle,
    CapitalSlot,
    PersistentSettings,
    RuntimeState,
    TradeOrder,
)

ACTIVE = {"prepared", "submitting", "open", "emergency", "closing"}


class Allocator:
    def __init__(self, database, settings):
        self.db, self.settings = database, settings

    async def verify_account(self, config):
        if not config.get("uid"):
            raise RiskRejected("Account identity unavailable", code="ACCOUNT_MISMATCH")
        fingerprint = hashlib.sha256(("demo:" + str(config["uid"])).encode()).hexdigest()
        async with self.db.sessions.begin() as session:
            await session.get(RuntimeState, 1, with_for_update=True)
            saved = await session.get(PersistentSettings, 1)
            if saved.account_fingerprint is None:
                saved.account_fingerprint = fingerprint
            elif saved.account_fingerprint != fingerprint:
                raise RiskRejected(
                    "Dedicated database is bound to another account", code="ACCOUNT_MISMATCH"
                )

    async def limits(self) -> Limits:
        async with self.db.sessions.begin() as session:
            await session.get(RuntimeState, 1, with_for_update=True)
            saved = await session.get(PersistentSettings, 1)
            if saved is None:
                limits = Limits.from_settings(self.settings)
                session.add(
                    PersistentSettings(
                        id=1, environment="demo", values=limits.model_dump(mode="json")
                    )
                )
                session.add(BotEvent(kind="limits_initialized", details={"notify": False}))
            else:
                if saved.environment != "demo":
                    raise RiskRejected("Database environment mismatch", code="ENVIRONMENT_MISMATCH")
                limits = Limits.model_validate(saved.values)
            return limits

    async def cycle(self, balance, *, verified_flat: bool, force=False, slot_count=None):
        limits = await self.limits()
        async with self.db.sessions.begin() as session:
            await session.get(RuntimeState, 1, with_for_update=True)
            cycle = await session.scalar(
                select(CapitalCycle).where(CapitalCycle.active_key == "demo")
            )
            active = await session.scalar(
                select(func.count()).select_from(TradeOrder).where(TradeOrder.status.in_(ACTIVE))
            )
            used = cycle and await session.scalar(
                select(TradeOrder.id).where(TradeOrder.cycle_id == cycle.id).limit(1)
            )
            refresh = force or (limits.auto_rebalance and used and verified_flat and not active)
            if cycle and not refresh:
                return cycle
            if not verified_flat or active:
                raise RiskRejected(
                    "Verified flat account required for allocation", code="ALLOCATION_NOT_FLAT"
                )
            if balance.equity <= 0 or balance.available <= 0:
                raise RiskRejected(
                    "Positive equity and available margin required", code="INSUFFICIENT_BALANCE"
                )
            if cycle:
                cycle.active_key, cycle.completed_at = None, utcnow()
                await session.flush()
            count = slot_count or limits.allocation_slots
            cycle = CapitalCycle(
                environment="demo",
                active_key="demo",
                allocation_base_equity=balance.equity,
                slot_count=count,
                slot_target_margin=balance.equity / count,
            )
            session.add(cycle)
            await session.flush()
            for number in range(1, count + 1):
                session.add(
                    CapitalSlot(cycle_id=cycle.id, number=number, reserved_margin=Decimal(0))
                )
            session.add(
                BotEvent(
                    kind="allocation_created",
                    details={
                        "cycle_id": cycle.id,
                        "equity": str(balance.equity),
                        "slot_target_margin": str(cycle.slot_target_margin),
                        "slots": count,
                        "notify": False,
                    },
                )
            )
            return cycle

    async def reserve(self, order, cycle, required_margin, balance, limits, balance_provider=None):
        # Commit the order intent and reservation together. Unknown submissions retain their slot.
        async with self.db.sessions.begin() as session:
            state = await session.get(RuntimeState, 1, with_for_update=True)
            if balance_provider is not None:
                balance = await balance_provider()
            if state.paused or state.emergency:
                raise RiskRejected("Trading disabled", code="TRADING_DISABLED")
            saved_limits = await session.get(PersistentSettings, 1)
            if Limits.model_validate(saved_limits.values) != limits:
                raise RiskRejected("Limits changed during preflight", code="SETTINGS_CHANGED")
            current = await session.get(CapitalCycle, cycle.id)
            if current.active_key != "demo":
                raise RiskRejected("Capital cycle changed", code="SETTINGS_CHANGED")
            active = list(
                (
                    await session.scalars(select(TradeOrder).where(TradeOrder.status.in_(ACTIVE)))
                ).all()
            )
            if len(active) >= min(limits.max_open_positions, cycle.slot_count):
                raise RiskRejected("Maximum positions reached", code="MAX_OPEN_POSITIONS")
            if any(o.inst_id == order.inst_id for o in active):
                raise RiskRejected("Instrument already reserved", code="EXISTING_EXPOSURE")
            if any(o.estimated_risk is None for o in active):
                raise RiskRejected("Legacy risk is unknown", code="UNKNOWN_OWNERSHIP")
            total_risk = sum((o.estimated_risk for o in active), Decimal(0)) + order.estimated_risk
            if total_risk > balance.equity * limits.max_portfolio_risk_percent / 100:
                raise RiskRejected("Portfolio risk cap exceeded", code="PORTFOLIO_RISK_LIMIT")
            slots = list(
                (
                    await session.scalars(
                        select(CapitalSlot)
                        .where(CapitalSlot.cycle_id == cycle.id)
                        .order_by(CapitalSlot.number)
                    )
                ).all()
            )
            reserved = {s.order_id: s.reserved_margin for s in slots if s.order_id is not None}
            pending_margin = sum(
                (
                    reserved.get(o.id, Decimal(0))
                    for o in active
                    if o.status in {"prepared", "submitting"}
                ),
                Decimal(0),
            )
            if balance.available < required_margin + pending_margin + limits.minimum_free_margin:
                raise RiskRejected(
                    "Available margin including reservations is insufficient",
                    code="INSUFFICIENT_BALANCE",
                )
            slot = next((s for s in slots if s.order_id is None), None)
            if slot is None:
                raise RiskRejected("No unreserved slot", code="MAX_OPEN_POSITIONS")
            order.cycle_id, order.slot_number, order.target_margin = (
                cycle.id,
                slot.number,
                cycle.slot_target_margin,
            )
            session.add(order)
            await session.flush()
            slot.order_id, slot.reserved_margin = order.id, required_margin

    @staticmethod
    async def release(session, order_id):
        slot = await session.scalar(select(CapitalSlot).where(CapitalSlot.order_id == order_id))
        if slot:
            slot.order_id, slot.reserved_margin = None, Decimal(0)

    async def summary(self):
        limits = await self.limits()
        async with self.db.sessions() as session:
            cycle = await session.scalar(
                select(CapitalCycle).where(CapitalCycle.active_key == "demo")
            )
            if cycle is None:
                return {"cycle_id": None, "configured_slots": limits.allocation_slots}
            slots = list(
                (
                    await session.scalars(
                        select(CapitalSlot)
                        .where(CapitalSlot.cycle_id == cycle.id)
                        .order_by(CapitalSlot.number)
                    )
                ).all()
            )
            orders = {
                o.id: o
                for o in (
                    await session.scalars(select(TradeOrder).where(TradeOrder.cycle_id == cycle.id))
                ).all()
            }
            return {
                "cycle_id": cycle.id,
                "allocation_base_equity": str(cycle.allocation_base_equity),
                "slot_target_margin": str(cycle.slot_target_margin),
                "slot_count": cycle.slot_count,
                "auto_rebalance": limits.auto_rebalance,
                "occupied": sum(s.order_id is not None for s in slots),
                "free": sum(s.order_id is None for s in slots),
                "slots": [
                    {
                        "number": s.number,
                        "order_id": s.order_id,
                        "actual_margin": str(orders[s.order_id].actual_margin)
                        if s.order_id and orders[s.order_id].actual_margin is not None
                        else None,
                        "inst_id": orders[s.order_id].inst_id if s.order_id else None,
                    }
                    for s in slots
                ],
            }
