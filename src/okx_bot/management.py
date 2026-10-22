"""Administrator previews, actor-bound confirmations and persisted audited changes."""

import hashlib
import hmac
import secrets
from datetime import timedelta
from decimal import Decimal

from pydantic import ValidationError
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
from okx_bot.risk import decimal


class Management:
    async def _allocation_snapshot(self):
        if (
            not self.healthy
            or self.fatal
            or not self.last_sync
            or (utcnow() - self.last_sync).total_seconds() > 120
        ):
            raise RiskRejected("Recent healthy reconciliation required", code="TRADING_DISABLED")
        await self.allocator.verify_account(await self.exchange.account_config())
        positions = await self.exchange.positions()
        pending = await self.exchange.open_orders()
        algos = await self.exchange.open_algo_orders()
        async with self.db.sessions() as session:
            active = await session.scalar(
                select(func.count())
                .select_from(TradeOrder)
                .where(
                    TradeOrder.status.in_(
                        {"prepared", "submitting", "open", "emergency", "closing", "settling"}
                    )
                )
            )
            occupied = await session.scalar(
                select(func.count())
                .select_from(CapitalSlot)
                .where(CapitalSlot.order_id.is_not(None))
            )
        if positions or pending or algos or active or occupied:
            raise RiskRejected(
                "Rebalance requires verified flat inventory", code="ALLOCATION_NOT_FLAT"
            )
        balance = await self.exchange.balance()
        await self._daily(balance)
        return {"equity": str(balance.equity), "available": str(balance.available)}

    async def request_change(self, field, value, *, actor):
        async with self.lock:
            current = await self.allocator.limits()
            if field != "rebalance" and field not in Limits.model_fields:
                raise RiskRejected("Unknown setting", code="INVALID_SETTING")
            try:
                proposed = (
                    current
                    if field == "rebalance"
                    else Limits.model_validate({**current.model_dump(), field: value})
                )
            except ValidationError as exc:
                raise RiskRejected("Setting out of safe bounds", code="INVALID_SETTING") from exc
            if (
                not self.healthy
                or self.fatal
                or not self.last_sync
                or (utcnow() - self.last_sync).total_seconds() > 120
            ):
                raise RiskRejected("OKX unavailable", code="TRADING_DISABLED")
            await self.allocator.verify_account(await self.exchange.account_config())
            snapshot = (
                await self._allocation_snapshot()
                if field in {"rebalance", "allocation_slots"}
                else None
            )
            state = await self.state()
            token = secrets.token_urlsafe(24)
            self._change_approvals[actor] = {
                "hash": hashlib.sha256(token.encode()).hexdigest(),
                "expires": utcnow() + timedelta(seconds=120),
                "revision": state.state_revision,
                "current": current.model_dump(mode="json"),
                "field": field,
                "proposed": proposed.model_dump(mode="json"),
                "snapshot": snapshot,
            }
            await self.event("setting_change_requested", actor=actor, field=field, notify=False)
            return {
                "confirmation": token,
                "field": field,
                "old": current.model_dump(mode="json").get(field),
                "new": proposed.model_dump(mode="json").get(field),
                "snapshot": snapshot,
            }

    async def confirm_change(self, token, *, actor):
        async with self.lock:
            challenge = self._change_approvals.get(actor)
            if not challenge or not hmac.compare_digest(
                challenge["hash"], hashlib.sha256(token.encode()).hexdigest()
            ):
                raise RiskRejected("Confirmation required", code="CONFIRMATION_REQUIRED")
            del self._change_approvals[actor]
            if utcnow() > challenge["expires"]:
                raise RiskRejected("Confirmation expired", code="CONFIRMATION_REQUIRED")
            current = await self.allocator.limits()
            if current.model_dump(mode="json") != challenge["current"]:
                raise RiskRejected("Settings changed", code="SETTINGS_CHANGED")
            if (
                not self.healthy
                or self.fatal
                or not self.last_sync
                or (utcnow() - self.last_sync).total_seconds() > 120
            ):
                raise RiskRejected("OKX unavailable", code="TRADING_DISABLED")
            await self.allocator.verify_account(await self.exchange.account_config())
            field = challenge["field"]
            snapshot = (
                await self._allocation_snapshot()
                if field in {"rebalance", "allocation_slots"}
                else None
            )
            if snapshot != challenge["snapshot"]:
                raise RiskRejected("Account changed since preview", code="SETTINGS_CHANGED")
            proposed = Limits.model_validate(challenge["proposed"])
            async with self.db.sessions.begin() as session:
                state = await session.get(RuntimeState, 1, with_for_update=True)
                saved = await session.get(PersistentSettings, 1, with_for_update=True)
                if (
                    state.state_revision != challenge["revision"]
                    or utcnow() > challenge["expires"]
                    or saved.values != challenge["current"]
                ):
                    raise RiskRejected("Safety state changed", code="SETTINGS_CHANGED")
                if snapshot:
                    active = await session.scalar(
                        select(func.count())
                        .select_from(CapitalSlot)
                        .where(CapitalSlot.order_id.is_not(None))
                    )
                    if active:
                        raise RiskRejected("Slots changed", code="ALLOCATION_NOT_FLAT")
                    previous = await session.scalar(
                        select(CapitalCycle).where(CapitalCycle.active_key == "demo")
                    )
                    if previous:
                        previous.active_key, previous.completed_at = None, utcnow()
                        await session.flush()
                    equity = decimal(snapshot["equity"])
                    cycle = CapitalCycle(
                        environment="demo",
                        active_key="demo",
                        allocation_base_equity=equity,
                        slot_count=proposed.allocation_slots,
                        slot_target_margin=equity / proposed.allocation_slots,
                    )
                    session.add(cycle)
                    await session.flush()
                    for number in range(1, proposed.allocation_slots + 1):
                        session.add(
                            CapitalSlot(
                                cycle_id=cycle.id, number=number, reserved_margin=Decimal(0)
                            )
                        )
                saved.values, saved.revision = proposed.model_dump(mode="json"), saved.revision + 1
                state.state_revision += 1
                session.add(
                    BotEvent(
                        kind="settings_changed",
                        details={
                            "actor": actor,
                            "field": field,
                            "old": challenge["current"].get(field),
                            "new": challenge["proposed"].get(field),
                            "notify": False,
                        },
                    )
                )
            self._resume_approvals.clear()
            return await self.allocator.summary() if snapshot else proposed.model_dump(mode="json")

    async def owned_positions(self):
        rows = await self.exchange.positions()
        balance = await self.exchange.balance()
        async with self.db.sessions() as session:
            orders = list(
                (
                    await session.scalars(
                        select(TradeOrder).where(
                            TradeOrder.status.in_({"open", "emergency", "closing", "settling"})
                        )
                    )
                ).all()
            )
        result = []
        total_risk = sum((o.estimated_risk or Decimal(0) for o in orders), Decimal(0))
        for order in orders:
            position = self._matching_position(order, rows)
            verified = (
                position
                and abs(decimal(position["pos"])) == order.filled
                and (not order.position_id or position.get("posId") == order.position_id)
            )
            risk, margin = order.estimated_risk, order.actual_margin
            result.append(
                {
                    "order_id": order.id,
                    "instId": order.inst_id,
                    "posSide": order.direction,
                    "pos": str(order.filled),
                    "avgPx": (order.execution_context or {}).get("fill_price"),
                    "markPx": (position or {}).get("markPx"),
                    "upl": position.get("upl") if verified else None,
                    "ownership_verified": bool(verified),
                    "liqPx": (position or {}).get("liqPx"),
                    "take_profit": str(order.take_profit),
                    "stop_loss": str(order.stop_loss),
                    "actual_margin": str(margin) if margin is not None else None,
                    "risk_margin_percent": str(risk / margin * 100)
                    if risk is not None and margin
                    else None,
                    "risk_equity_percent": str(risk / balance.equity * 100)
                    if risk is not None and balance.equity > 0
                    else None,
                    "portfolio_risk_percent": str(total_risk / balance.equity * 100)
                    if balance.equity > 0 and all(o.estimated_risk is not None for o in orders)
                    else None,
                }
            )
        return result

    async def history(self):
        async with self.db.sessions() as session:
            orders = list(
                (
                    await session.scalars(
                        select(TradeOrder)
                        .where(TradeOrder.closed_at.is_not(None))
                        .order_by(TradeOrder.closed_at.desc())
                        .limit(20)
                    )
                ).all()
            )
        return [
            {
                "id": o.id,
                "inst_id": o.inst_id,
                "direction": o.direction,
                "cause": o.close_cause,
                "closed_at": self._aware(o.closed_at).isoformat(),
                **{
                    k: str(v) if v is not None else None
                    for k, v in (await self.accounting.totals(o.id)).items()
                    if k in {"gross_pnl", "fees", "funding", "net_pnl"}
                },
            }
            for o in orders
        ]
