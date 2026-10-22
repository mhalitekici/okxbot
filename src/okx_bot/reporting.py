"""Istanbul calendar reports from owned fills, signed fees and verified funding."""

from datetime import UTC, datetime, time, timedelta
from decimal import Decimal
from zoneinfo import ZoneInfo

from sqlalchemy import select

from okx_bot.accounting import aware
from okx_bot.domain import utcnow
from okx_bot.models import AccountBill, BotEvent, CapitalCycle, DailyReport, Execution, TradeOrder

ISTANBUL = ZoneInfo("Europe/Istanbul")
ZERO = Decimal(0)


def day_bounds(day):
    start = datetime.combine(day, time.min, tzinfo=ISTANBUL).astimezone(UTC)
    return start, start + timedelta(days=1)


class Reporter:
    def __init__(self, db, accounting):
        self.db, self.accounting = db, accounting

    async def period(self, begin=None, end=None):
        end = end or utcnow()
        async with self.db.sessions() as session:
            orders = list(
                (
                    await session.scalars(
                        select(TradeOrder).where(TradeOrder.environment == "demo")
                    )
                ).all()
            )
            fills = list(
                (
                    await session.scalars(select(Execution).where(Execution.order_id.is_not(None)))
                ).all()
            )
            bills = list(
                (
                    await session.scalars(
                        select(AccountBill).where(
                            AccountBill.order_id.is_not(None), AccountBill.kind == "8"
                        )
                    )
                ).all()
            )

            def within(stamp):
                return (
                    stamp is not None
                    and (begin is None or aware(stamp) >= begin)
                    and aware(stamp) < end
                )

            entry_times = {
                o.id: min(
                    (aware(f.timestamp) for f in fills if f.order_id == o.id and f.role == "entry"),
                    default=None,
                )
                for o in orders
            }
            opened = [o for o in orders if o.filled > 0 and within(entry_times[o.id])]
            closed = [o for o in orders if o.closed_at and within(o.closed_at)]
            unknown_open_time = any(
                o.filled > 0
                and entry_times[o.id] is None
                and aware(o.created_at) < end
                and (begin is None or not o.closed_at or aware(o.closed_at) >= begin)
                for o in orders
            )
            unknown_close_time = any(
                o.filled > 0
                and o.status == "closed"
                and o.closed_at is None
                and aware(o.created_at) < end
                for o in orders
            )
            fs = [f for f in fills if within(f.timestamp)]
            bs = [b for b in bills if within(b.timestamp)]
            outcomes = [(o, await self.accounting.totals(o.id, session)) for o in closed]
            nets = [t["net_pnl"] for _, t in outcomes if t["net_pnl"] is not None]
            outcomes_complete = len(nets) == len(closed) and not unknown_close_time
            wins, losses = [n for n in nets if n > 0], [n for n in nets if n < 0]
            gross = sum((f.pnl for f in fs), ZERO)
            fees = sum((f.fee for f in fs if f.fee_ccy == "USDT"), ZERO)
            funding = sum((b.amount for b in bs if b.currency == "USDT"), ZERO)
            touched = [
                o
                for o in orders
                if any(f.order_id == o.id for f in fs)
                or (
                    aware(o.created_at) < end
                    and (not o.closed_at or begin is None or aware(o.closed_at) >= begin)
                )
            ]
            complete = (
                all(f.fee_ccy == "USDT" for f in fs)
                and all(b.currency == "USDT" for b in bs)
                and all(o.funding_verified_at is not None for o in touched if o.filled > 0)
                and all(
                    sum(
                        (f.contracts for f in fills if f.order_id == o.id and f.role == "entry"),
                        ZERO,
                    )
                    == o.filled
                    for o in touched
                    if o.filled > 0
                )
            )
            margins = [o.actual_margin for o in opened if o.actual_margin is not None]
            risks = [
                t["net_pnl"] / o.estimated_risk
                for o, t in outcomes
                if t["net_pnl"] is not None and o.estimated_risk and o.estimated_risk > 0
            ]
            cycle = await session.scalar(
                select(CapitalCycle).where(CapitalCycle.active_key == "demo")
            )
            return {
                "begin": begin.isoformat() if begin else None,
                "end": end.isoformat(),
                "opened": None if unknown_open_time else len(opened),
                "closed": None if unknown_close_time else len(closed),
                "wins": len(wins) if outcomes_complete else None,
                "losses": len(losses) if outcomes_complete else None,
                "win_rate": str(Decimal(len(wins)) / len(nets) * 100)
                if nets and outcomes_complete
                else None,
                "average_win": str(sum(wins, ZERO) / len(wins))
                if wins and outcomes_complete
                else None,
                "average_loss": str(sum(losses, ZERO) / len(losses))
                if losses and outcomes_complete
                else None,
                "average_realized_r": str(sum(risks, ZERO) / len(risks))
                if risks and len(risks) == len(closed) and outcomes_complete
                else None,
                "gross_pnl": str(gross),
                "fees": str(fees),
                "funding": str(funding) if complete else None,
                "net_pnl": str(gross + fees + funding) if complete else None,
                "accounting_complete": complete,
                "executions": len(fs),
                "average_margin": str(sum(margins, ZERO) / len(margins))
                if margins and not unknown_open_time
                else None,
                "allocation_base_equity": str(cycle.allocation_base_equity) if cycle else None,
            }

    async def pnl(self, now=None):
        now = now or utcnow()
        today = now.astimezone(ISTANBUL).date()
        start, _ = day_bounds(today)
        return {
            label: await self.period(begin, now)
            for label, begin in (
                ("today", start),
                ("7_days", start - timedelta(days=6)),
                ("30_days", start - timedelta(days=29)),
                ("all_time", None),
            )
        }

    async def daily(self, day=None):
        day = day or utcnow().astimezone(ISTANBUL).date()
        begin, end = day_bounds(day)
        return {
            "day": day.isoformat(),
            "timezone": "Europe/Istanbul",
            **await self.period(begin, end),
        }

    async def account_snapshot(self):
        from dataclasses import asdict

        balance = await self.accounting.exchange.balance()
        positions = await self.accounting.exchange.positions()
        async with self.db.sessions() as session:
            orders = list(
                (
                    await session.scalars(
                        select(TradeOrder).where(
                            TradeOrder.status.in_({"open", "emergency", "closing", "settling"}),
                            TradeOrder.filled > 0,
                        )
                    )
                ).all()
            )
        unrealized = ZERO
        for order in orders:
            matches = [
                p
                for p in positions
                if p["instId"] == order.inst_id and p.get("posSide", "net") == order.pos_side
            ]
            if (
                len(matches) != 1
                or abs(Decimal(matches[0]["pos"])) != order.filled
                or matches[0].get("upl") in {None, ""}
            ):
                unrealized = None
                break
            unrealized += Decimal(matches[0]["upl"])
        return {
            "current_balance": {
                k: str(v) if v is not None else None for k, v in asdict(balance).items()
            },
            "bot_unrealized": str(unrealized) if unrealized is not None else None,
            "snapshot_at": utcnow().isoformat(),
        }

    async def queue_daily(self, now=None):
        now = now or utcnow()
        local = now.astimezone(ISTANBUL)
        due = local.date() - timedelta(days=1 if local.time() >= time(0, 5) else 2)
        async with self.db.sessions() as session:
            last = await session.scalar(
                select(DailyReport.day).order_by(DailyReport.day.desc()).limit(1)
            )
            earliest = await session.scalar(
                select(TradeOrder.created_at).order_by(TradeOrder.created_at).limit(1)
            )
        first = (
            last + timedelta(days=1)
            if last
            else min(aware(earliest).astimezone(ISTANBUL).date(), due)
            if earliest
            else due
        )
        for offset in range(min((due - first).days + 1, 31)):
            day = first + timedelta(days=offset)
            report = await self.daily(day)
            report.update(await self.account_snapshot())
            # Persist the report and its unique notification atomically, once per local day.
            async with self.db.sessions.begin() as session:
                if await session.get(DailyReport, day):
                    continue
                event = BotEvent(kind="daily_report", event_key=f"daily:demo:{day}", details=report)
                session.add(event)
                await session.flush()
                session.add(DailyReport(day=day, event_id=event.id))
