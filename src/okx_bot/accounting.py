"""Exchange-evidenced ownership and realized accounting. Account changes are not bot PnL."""

from datetime import UTC, datetime, timedelta
from decimal import Decimal

from sqlalchemy import select

from okx_bot.domain import RiskRejected, utcnow
from okx_bot.models import AccountBill, BotEvent, Execution, OrderLink, TradeOrder
from okx_bot.risk import decimal

ZERO = Decimal(0)


def aware(value):
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


class Accounting:
    def __init__(self, db, exchange, settings):
        self.db, self.exchange, self.settings = db, exchange, settings

    async def link(self, order, exchange_id, role, cause=None):
        if not exchange_id:
            raise RiskRejected("Missing exchange order identity", code="UNKNOWN_OWNERSHIP")
        async with self.db.sessions.begin() as session:
            link = await session.scalar(
                select(OrderLink).where(OrderLink.exchange_id == str(exchange_id))
            )
            if link and (link.order_id != order.id or link.role != role):
                raise RiskRejected(
                    "Exchange order has conflicting owners", code="UNKNOWN_OWNERSHIP"
                )
            if link is None:
                session.add(
                    OrderLink(
                        order_id=order.id, exchange_id=str(exchange_id), role=role, cause=cause
                    )
                )

    async def link_algo(self, order, algo):
        if not algo:
            return
        if (
            algo.get("instId") != order.inst_id
            or algo.get("algoClOrdId") != order.algo_client_id
            or (order.algo_id and algo.get("algoId") != order.algo_id)
        ):
            raise RiskRejected("Algo identity does not match bot", code="UNKNOWN_OWNERSHIP")
        children = list(algo.get("ordIdList") or [])
        if algo.get("ordId") and algo["ordId"] not in children:
            children.append(algo["ordId"])
        cause = algo.get("actualSide") if algo.get("state") == "effective" else None
        cause = cause if cause in {"tp", "sl"} else "unknown"
        for child in children:
            detail = await self.exchange.order_by_id(order.inst_id, child)
            self.validate_exit(order, detail)
            await self.link(order, child, "exit", cause)

    @staticmethod
    def validate_exit(order, detail):
        if (
            not detail
            or detail.get("instId") != order.inst_id
            or detail.get("side") != ("sell" if order.direction == "long" else "buy")
            or detail.get("posSide", "net") != order.pos_side
        ):
            raise RiskRejected("Exit identity or direction mismatch", code="UNKNOWN_OWNERSHIP")

    async def sync(self):
        async with self.db.sessions() as session:
            orders = list((await session.scalars(select(TradeOrder))).all())
        relevant = [
            o
            for o in orders
            if o.exchange_id
            and o.filled > 0
            and (
                not o.accounting_complete
                or not o.closed_at
                or aware(o.closed_at) >= utcnow() - timedelta(days=2)
            )
        ]
        if not relevant:
            return
        earliest = min(aware(o.created_at) for o in relevant)
        begin = max(earliest - timedelta(minutes=1), utcnow() - timedelta(days=89))
        for order in relevant:
            await self.link(order, order.exchange_id, "entry")
            await self.link_algo(order, await self.exchange.algo(order.algo_client_id))
        rows = await self.exchange.fills(begin)
        # External exits can only be attributed in an explicitly dedicated account,
        # within one unambiguous instrument ownership interval. Their cause stays unknown
        # unless exchange order category proves liquidation.
        async with self.db.sessions() as session:
            known = {r.exchange_id for r in (await session.scalars(select(OrderLink))).all()}
        for row in rows:
            if row["ordId"] in known or not self.settings.dedicated_trading_account_confirmed:
                continue
            stamp = datetime.fromtimestamp(int(row["ts"]) / 1000, UTC)
            candidates = [
                o
                for o in relevant
                if o.inst_id == row["instId"]
                and aware(o.created_at) <= stamp
                and (not o.closed_at or stamp <= aware(o.closed_at))
                and row.get("side") == ("sell" if o.direction == "long" else "buy")
            ]
            if len(candidates) == 1:
                order = candidates[0]
                detail = await self.exchange.order_by_id(order.inst_id, row["ordId"])
                self.validate_exit(order, detail)
                category = detail.get("category")
                cause = (
                    "liquidation"
                    if category in {"partial_liquidation", "full_liquidation", "adl"}
                    else "unknown"
                )
                if detail.get("clOrdId") and detail.get("clOrdId") == (
                    order.execution_context or {}
                ).get("close_client_id"):
                    cause = "manual"
                await self.link(order, row["ordId"], "exit", cause)
                known.add(row["ordId"])
        async with self.db.sessions.begin() as session:
            links = {r.exchange_id: r for r in (await session.scalars(select(OrderLink))).all()}
            for row in rows:
                if decimal(row["fillPx"]) <= 0 or decimal(row["fillSz"]) <= 0:
                    raise RiskRejected("Invalid fill price/quantity", code="INVALID_EXCHANGE_DATA")
                link = links.get(row["ordId"])
                order = next((o for o in orders if link and o.id == link.order_id), None)
                if order and order.inst_id != row["instId"]:
                    raise RiskRejected("Fill instrument mismatch", code="UNKNOWN_OWNERSHIP")
                saved = await session.scalar(
                    select(Execution).where(
                        Execution.inst_id == row["instId"], Execution.trade_id == row["tradeId"]
                    )
                )
                if saved is None:
                    saved = Execution(
                        inst_id=row["instId"],
                        trade_id=row["tradeId"],
                        exchange_order_id=row["ordId"],
                        price=decimal(row["fillPx"]),
                        contracts=decimal(row["fillSz"]),
                        pnl=decimal(row["fillPnl"]),
                        fee=decimal(row["fee"]),
                        fee_ccy=row["feeCcy"],
                        timestamp=datetime.fromtimestamp(int(row["ts"]) / 1000, UTC),
                        side=row.get("side"),
                    )
                    session.add(saved)
                if link:
                    if saved.order_id is not None and saved.order_id != link.order_id:
                        raise RiskRejected("Fill owner changed", code="UNKNOWN_OWNERSHIP")
                    saved.order_id, saved.role = link.order_id, link.role
        bills = await self.exchange.bills(begin)
        async with self.db.sessions.begin() as session:
            for row in bills:
                saved_bill = await session.scalar(
                    select(AccountBill).where(AccountBill.bill_id == row["billId"])
                )
                stamp = datetime.fromtimestamp(int(row["ts"]) / 1000, UTC)
                owner = None
                if row.get("type") == "8" and row.get("ccy") == "USDT":
                    candidates = [
                        o
                        for o in relevant
                        if o.inst_id == row.get("instId")
                        and aware(o.created_at) <= stamp
                        and (not o.closed_at or stamp <= aware(o.closed_at))
                    ]
                    if len(candidates) == 1:
                        candidate = candidates[0]
                        matches_position = bool(
                            row.get("posId") and row["posId"] == candidate.position_id
                        )
                        exclusive_without_position_id = (
                            not row.get("posId")
                            and self.settings.dedicated_trading_account_confirmed
                        )
                        if matches_position or exclusive_without_position_id:
                            owner = candidate.id
                if saved_bill is not None:
                    if owner is not None:
                        if saved_bill.order_id is not None and saved_bill.order_id != owner:
                            raise RiskRejected("Funding owner changed", code="UNKNOWN_OWNERSHIP")
                        saved_bill.order_id = owner
                    continue
                session.add(
                    AccountBill(
                        bill_id=row["billId"],
                        order_id=owner,
                        kind=row.get("type", "unknown"),
                        inst_id=row.get("instId"),
                        amount=decimal(row["balChg"]),
                        currency=row["ccy"],
                        timestamp=stamp,
                    )
                )
            await session.flush()
            for order in relevant:
                query = select(AccountBill.id).where(
                    AccountBill.kind == "8",
                    AccountBill.inst_id == order.inst_id,
                    AccountBill.order_id.is_(None),
                    AccountBill.timestamp >= aware(order.created_at),
                )
                if order.closed_at:
                    query = query.where(AccountBill.timestamp <= aware(order.closed_at))
                unassigned = await session.scalar(query.limit(1))
                if aware(order.created_at) >= utcnow() - timedelta(days=89) and not unassigned:
                    saved = await session.get(TradeOrder, order.id)
                    saved.funding_verified_at = utcnow()
                elif unassigned:
                    saved = await session.get(TradeOrder, order.id)
                    saved.funding_verified_at = None

    async def totals(self, order_id, session=None):
        if session is None:
            async with self.db.sessions() as owned_session:
                return await self.totals(order_id, owned_session)
        order = await session.get(TradeOrder, order_id)
        fills = list(
            (await session.scalars(select(Execution).where(Execution.order_id == order_id))).all()
        )
        funding = list(
            (
                await session.scalars(
                    select(AccountBill).where(
                        AccountBill.order_id == order_id, AccountBill.kind == "8"
                    )
                )
            ).all()
        )
        entry = [f for f in fills if f.role == "entry"]
        exits = [f for f in fills if f.role == "exit"]
        in_qty, out_qty = (sum((f.contracts for f in fs), ZERO) for fs in (entry, exits))
        complete = bool(
            in_qty > 0
            and in_qty == order.filled == out_qty
            and all(f.fee_ccy == "USDT" for f in fills)
        )
        gross, fees = (
            sum((f.pnl for f in fills), ZERO),
            sum((f.fee for f in fills if f.fee_ccy == "USDT"), ZERO),
        )
        funding_known = order.funding_verified_at is not None and all(
            f.currency == "USDT" for f in funding
        )
        funding_total = sum((f.amount for f in funding), ZERO) if funding_known else None
        links = list(
            (
                await session.scalars(
                    select(OrderLink).where(
                        OrderLink.order_id == order_id, OrderLink.role == "exit"
                    )
                )
            ).all()
        )
        causes = {
            link.cause
            for link in links
            if any(f.exchange_order_id == link.exchange_id for f in exits)
        }
        cause = next(iter(causes)) if len(causes) == 1 else "unknown"
        return {
            "complete": complete,
            "funding_complete": funding_known,
            "entry_quantity": in_qty,
            "exit_quantity": out_qty,
            "entry_price": sum((f.price * f.contracts for f in entry), ZERO) / in_qty
            if in_qty
            else None,
            "exit_price": sum((f.price * f.contracts for f in exits), ZERO) / out_qty
            if out_qty
            else None,
            "gross_pnl": gross if complete else None,
            "fees": fees if complete else None,
            "funding": funding_total,
            "net_pnl": gross + fees + funding_total
            if complete and funding_total is not None
            else None,
            "closed_at": max((aware(f.timestamp) for f in exits), default=None),
            "cause": cause or "unknown",
        }

    async def daily_realized_loss(self, begin, end):
        async with self.db.sessions() as session:
            fills = list(
                (
                    await session.scalars(
                        select(Execution).where(
                            Execution.order_id.is_not(None),
                            Execution.timestamp >= begin,
                            Execution.timestamp < end,
                        )
                    )
                ).all()
            )
            bills = list(
                (
                    await session.scalars(
                        select(AccountBill).where(
                            AccountBill.order_id.is_not(None),
                            AccountBill.kind == "8",
                            AccountBill.timestamp >= begin,
                            AccountBill.timestamp < end,
                        )
                    )
                ).all()
            )
            ambiguous = await session.scalar(
                select(AccountBill.id)
                .join(TradeOrder, AccountBill.inst_id == TradeOrder.inst_id)
                .where(
                    AccountBill.kind == "8",
                    AccountBill.order_id.is_(None),
                    AccountBill.timestamp >= begin,
                    AccountBill.timestamp < end,
                    TradeOrder.created_at <= AccountBill.timestamp,
                    (
                        TradeOrder.closed_at.is_(None)
                        | (TradeOrder.closed_at >= AccountBill.timestamp)
                    ),
                )
                .limit(1)
            )
        if ambiguous:
            raise RiskRejected("Funding ownership is uncertain", code="ACCOUNTING_INCOMPLETE")
        if any(f.fee_ccy != "USDT" for f in fills) or any(b.currency != "USDT" for b in bills):
            raise RiskRejected("Unconvertible fee currency", code="ACCOUNTING_INCOMPLETE")
        return max(
            -sum((f.pnl + f.fee for f in fills), ZERO) - sum((b.amount for b in bills), ZERO), ZERO
        )

    async def close_event(self, order, totals, equity, daily_net):
        # The caller commits closure, slot release and this unique event in one transaction.
        return BotEvent(
            kind="position_closed",
            event_key=f"close:demo:{order.id}",
            details={
                "order_id": order.id,
                "inst_id": order.inst_id,
                "direction": order.direction,
                "cause": totals["cause"],
                "entry_price": str(totals["entry_price"]),
                "exit_price": str(totals["exit_price"]),
                "actual_margin": str(order.actual_margin)
                if order.actual_margin is not None
                else None,
                "notional": str(order.actual_margin * 10)
                if order.actual_margin is not None
                else None,
                **{
                    k: str(totals[k]) if totals[k] is not None else None
                    for k in ("gross_pnl", "fees", "funding", "net_pnl")
                },
                "equity": str(equity),
                "daily_net": str(daily_net) if daily_net is not None else None,
            },
        )
