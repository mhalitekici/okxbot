import asyncio
import hashlib
import hmac
import json
import logging
import secrets
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError

from okx_bot.accounting import Accounting
from okx_bot.allocation import Allocator
from okx_bot.config import Settings
from okx_bot.db import Database
from okx_bot.diagnostics import failure_details
from okx_bot.domain import (
    DailyLossLimitReached,
    DemoInstrumentMissing,
    ExchangeError,
    Message,
    ReviewRequired,
    RiskRejected,
    Signal,
    utcnow,
)
from okx_bot.management import Management
from okx_bot.models import (
    BotEvent,
    DailyRisk,
    Execution,
    Position,
    RiskEvent,
    RuntimeState,
    TelegramSignal,
    TradeOrder,
)
from okx_bot.parser import ChannelSignalParser, SignalParser, symbol_candidates
from okx_bot.reporting import ISTANBUL, Reporter, day_bounds
from okx_bot.risk import RiskManager, decimal, fill_levels, safe_stop

ACTIVE = {"prepared", "submitting", "open", "emergency", "closing", "settling"}
TERMINAL_EXCHANGE = {"filled", "canceled", "mmp_canceled"}
logger = logging.getLogger(__name__)


class TradingEngine(Management):
    def __init__(
        self, settings: Settings, database: Database, exchange, parser: SignalParser | None = None
    ):
        self.settings, self.db, self.exchange = settings, database, exchange
        self.parser = parser or ChannelSignalParser(
            settings.mztradingbot_channel_id, settings.telegram_topic_id
        )
        self.risk = RiskManager(settings)
        self.allocator = Allocator(database, settings)
        self.accounting = Accounting(database, exchange, settings)
        self.reporter = Reporter(database, self.accounting)
        self.lock = asyncio.Lock()
        self.healthy = False
        self.fatal = False
        self.last_sync: datetime | None = None
        self.last_error: dict | None = None
        self._unpersisted_failure: dict | None = None
        self._resume_approvals: dict[str, dict] = {}
        self._change_approvals: dict[str, dict] = {}

    async def event(self, kind: str, *, event_key: str | None = None, **details):
        async with self.db.sessions.begin() as session:
            if event_key and await session.scalar(
                select(BotEvent.id).where(BotEvent.event_key == event_key)
            ):
                return
            session.add(BotEvent(kind=kind, event_key=event_key, details=details))
        logger.info(
            kind,
            extra={
                "context": {
                    k: v
                    for k, v in details.items()
                    if k in {"signal_id", "inst_id", "order_id", "code", "cause"}
                }
            },
        )

    async def state(self) -> RuntimeState:
        async with self.db.sessions() as session:
            state = await session.get(RuntimeState, 1)
            if state is None:
                raise RuntimeError("Runtime state not initialized")
            return state

    async def trip(self, reason: str):
        self._change_approvals.clear()
        self._resume_approvals.clear()
        async with self.db.sessions.begin() as session:
            state = await session.get(RuntimeState, 1, with_for_update=True)
            if not state.emergency:
                state.reason = reason
                state.emergency_at = utcnow()
            stamp = self._aware(state.emergency_at).isoformat() if state.emergency_at else "legacy"
            key = "emergency:" + stamp + ":" + hashlib.sha256(reason.encode()).hexdigest()[:24]
            changed = not await session.scalar(select(BotEvent.id).where(BotEvent.event_key == key))
            state.paused = state.emergency = True
            state.state_revision += 1
            if changed:
                session.add(BotEvent(kind="emergency", event_key=key, details={"reason": reason}))
                session.add(RiskEvent(reason=reason))

    async def _persist_reconciliation_failure(self, details: dict):
        async with self.db.sessions.begin() as session:
            state = await session.get(RuntimeState, 1, with_for_update=True)
            previous = state.last_reconciliation_error or {}
            changed = any(
                previous.get(k) != details.get(k)
                for k in ("stage", "code", "exception_type", "sqlstate")
            )
            reason = (
                f"Reconciliation failed at {details['stage']} ({details['code']}); "
                "new orders disabled"
            )
            if not state.emergency:
                state.reason, state.emergency_at = reason, utcnow()
            state.paused = state.emergency = True
            state.state_revision += 1
            state.last_reconciliation_at = utcnow()
            state.last_reconciliation_ok = False
            state.last_reconciliation_error = details
            state.reconciliation_recovered_at = None
            if changed:
                session.add(BotEvent(kind="reconciliation_failed", details=details))
                session.add(RiskEvent(reason=reason))

    async def _record_reconciliation_failure(self, exc: Exception, stage: str):
        self.healthy = False
        self._resume_approvals.clear()
        details = {**failure_details(exc, stage), "at": utcnow().isoformat()}
        self.last_error, self._unpersisted_failure = details, details
        logger.error("reconciliation_failed", extra={"context": details})
        try:
            await self._persist_reconciliation_failure(details)
            self._unpersisted_failure = None
        except Exception as persist_error:
            logger.error(
                "reconciliation_state_persist_failed",
                extra={
                    "context": failure_details(persist_error, "persist_failure"),
                },
            )

    @staticmethod
    def _message_context(message: Message) -> dict:
        candidates = symbol_candidates(message.text)
        return {
            "channel_id": message.channel_id,
            "message_id": message.message_id,
            "topic_id": message.topic_id,
            "edited": message.edited,
            "coin": candidates[0] if len(candidates) == 1 else None,
            "coin_candidates": candidates,
        }

    @staticmethod
    def _saved_signal_context(row: TelegramSignal) -> dict:
        parsed = row.parsed or {}
        candidates = symbol_candidates(row.original_text)
        return {
            **{
                k: parsed[k]
                for k in (
                    "symbol",
                    "entry",
                    "range_percent",
                    "take_profit",
                    "stop_loss",
                    "interpretation_rule",
                    "parser_name",
                    "level_coefficient",
                    "topic_id",
                    "timestamp",
                    "signal_kind",
                    "automation_policy",
                )
                if k in parsed
            },
            "signal_id": row.id,
            "channel_id": row.channel_id,
            "message_id": row.message_id,
            "coin": parsed.get("symbol") or (candidates[0] if len(candidates) == 1 else None),
            "coin_candidates": candidates,
            "direction": parsed.get("direction"),
            "analysis_result": "needs_review"
            if row.status == "needs_review"
            else "parsed"
            if row.parsed
            else "not_attempted",
            "trade_status": row.status,
        }

    async def _signal_status(
        self,
        signal_id: int,
        status: str,
        reason: str | None = None,
        *,
        reason_code: str | None = None,
        risk_context: dict | None = None,
    ):
        async with self.db.sessions.begin() as session:
            row = await session.get(TelegramSignal, signal_id)
            row.status, row.reason = status, reason
            context = self._saved_signal_context(row)
            context["reason_code"] = reason_code
            context.update(
                {
                    k: v
                    for k, v in (risk_context or {}).items()
                    if k
                    in {
                        "reference_price",
                        "market_price",
                        "mark_price",
                        "price_deviation_percent",
                        "max_deviation_percent",
                        "spread_percent",
                        "max_spread_percent",
                    }
                }
            )
            if reason:
                session.add(BotEvent(kind=status, details={**context, "reason": reason}))
                if status == "rejected":
                    session.add(RiskEvent(signal_id=signal_id, reason=reason))
        logger.info("Signal processed", extra={"context": context})
        if status == "rejected":
            logger.info("risk_rejected", extra={"context": context})

    async def _order_status(self, order_id: int, status: str, **values):
        async with self.db.sessions.begin() as session:
            order = await session.get(TradeOrder, order_id)
            changed = order.status != status
            order.status = status
            if status in {"canceled", "rejected"}:
                if order.filled > 0:
                    raise RiskRejected("Cannot release filled exposure", code="UNKNOWN_OWNERSHIP")
                await self.allocator.release(session, order_id)
            for key, value in values.items():
                setattr(order, key, value)
            signal = await session.get(TelegramSignal, order.signal_id)
            signal.status = status
            if values.get("exchange_id"):
                signal.order_id = values["exchange_id"]
            context = self._saved_signal_context(signal)
            context["inst_id"] = order.inst_id
        if changed:
            logger.info("Signal trade status changed", extra={"context": context})

    async def ingest(self, message: Message) -> str:
        async with self.lock:
            if (
                type(message.message_id) is not int
                or message.message_id <= 0
                or message.date.tzinfo is None
            ):
                return "invalid_message"
            state = await self.state()
            if message.channel_id != self.settings.telegram_channel_id:
                logger.debug(
                    "Telegram message ignored",
                    extra={
                        "context": {
                            **self._message_context(message),
                            "trade_status": "ignored_channel",
                        }
                    },
                )
                return "ignored_channel"
            if (
                not self.settings.telegram_topic_id
                or type(message.topic_id) is not int
                or message.topic_id != self.settings.telegram_topic_id
            ):
                # Never persist/parse/notify/submit messages without an allowed topic.
                return "ignored_topic"
            if state.channel_started_at and message.date.astimezone(UTC) < self._aware(
                state.channel_started_at
            ):
                logger.debug(
                    "Telegram message ignored",
                    extra={
                        "context": {
                            **self._message_context(message),
                            "trade_status": "ignored_history",
                        }
                    },
                )
                return "ignored_history"
            async with self.db.sessions.begin() as session:
                existing = await session.scalar(
                    select(TelegramSignal).where(
                        TelegramSignal.channel_id == message.channel_id,
                        TelegramSignal.message_id == message.message_id,
                    )
                )
                if existing:
                    message = replace(
                        message,
                        date=min(message.date.astimezone(UTC), self._aware(existing.message_date)),
                    )
                    if (
                        message.edited
                        and existing.original_text != message.text
                        and existing.status in {"open", "submitting", "closing", "emergency"}
                    ):
                        edit_hash = hashlib.sha256(message.text.encode()).hexdigest()
                        recorded = await session.scalar(
                            select(BotEvent.id).where(
                                BotEvent.kind == "signal_edit_review",
                                BotEvent.details["signal_id"].as_integer() == existing.id,
                                BotEvent.details["edit_hash"].as_string() == edit_hash,
                            )
                        )
                        if recorded is None:
                            session.add(
                                BotEvent(
                                    kind="signal_edit_review",
                                    details={
                                        **self._saved_signal_context(existing),
                                        "edit_hash": edit_hash,
                                    },
                                )
                            )
                    if not message.edited or existing.status not in {
                        "received",
                        "needs_review",
                        "rejected",
                        "manual_required",
                    }:
                        logger.debug(
                            "Duplicate Telegram message ignored",
                            extra={
                                "context": {
                                    **self._message_context(message),
                                    "trade_status": "duplicate",
                                }
                            },
                        )
                        return "duplicate"
                    if existing.original_text == message.text:
                        return "duplicate"
                    attempted = await session.scalar(
                        select(TradeOrder.id).where(TradeOrder.signal_id == existing.id)
                    )
                    if attempted is not None:
                        return "duplicate"
                    existing.original_text = message.text
                    existing.edited = True
                    existing.status = "received"
                    existing.parsed = None
                    existing.reason = None
                    signal_id = existing.id
                else:
                    row = TelegramSignal(
                        channel_id=message.channel_id,
                        message_id=message.message_id,
                        original_text=message.text,
                        message_date=message.date,
                        edited=message.edited,
                    )
                    session.add(row)
                    try:
                        await session.flush()
                    except IntegrityError:
                        await session.rollback()
                        return "duplicate"
                    signal_id = row.id
                session.add(
                    BotEvent(
                        kind="signal_received",
                        details={
                            "signal_id": signal_id,
                            "coin": self._message_context(message)["coin"],
                            "notify": False,
                        },
                    )
                )
            context = {**self._message_context(message), "signal_id": signal_id}
            logger.info(
                "Signal received",
                extra={
                    "context": {
                        **context,
                        "event": "signal_received",
                        "analysis_result": "pending",
                        "trade_status": "received",
                    }
                },
            )
            try:
                logger.info("Signal parser started", extra={"context": context})
                signal = self.parser.parse(message)
            except (ReviewRequired, ValueError) as exc:
                await self._signal_status(
                    signal_id,
                    "needs_review",
                    str(exc),
                    reason_code=exc.code if isinstance(exc, ReviewRequired) else "INVALID_SIGNAL",
                )
                return "needs_review"
            logger.info(
                "Signal parsed",
                extra={
                    "context": {
                        **context,
                        "event": "signal_parsed",
                        "coin": signal.symbol,
                        "direction": signal.direction.value,
                        "analysis_result": "parsed",
                        "trade_status": "pending_risk_checks",
                    }
                },
            )
            async with self.db.sessions.begin() as session:
                row = await session.get(TelegramSignal, signal_id)
                row.parsed = {**signal.model_dump(mode="json"), "topic_id": message.topic_id}
                session.add(BotEvent(kind="signal_parsed", details=self._saved_signal_context(row)))
            age = (utcnow() - signal.timestamp).total_seconds()
            if not -5 <= age <= self.settings.max_signal_age_seconds:
                await self._signal_status(
                    signal_id,
                    "rejected",
                    "Signal expired or future-dated",
                    reason_code="STALE_OR_FUTURE_SIGNAL",
                )
                return "rejected"
            if (
                state.paused
                or state.emergency
                or not self.healthy
                or self.fatal
                or not self.last_sync
                or (utcnow() - self.last_sync).total_seconds() > 120
                or (state.last_reconciliation_summary or {}).get("blockers")
            ):
                await self._signal_status(
                    signal_id,
                    "rejected",
                    "Trading paused or reconciliation unavailable",
                    reason_code="TRADING_DISABLED",
                )
                return "rejected"
            try:
                return await self._execute(signal_id, signal)
            except DemoInstrumentMissing as exc:
                await self._signal_status(
                    signal_id, "manual_required", str(exc), reason_code=exc.code
                )
                return "manual_required"
            except RiskRejected as exc:
                await self._signal_status(
                    signal_id, "rejected", str(exc), reason_code=exc.code, risk_context=exc.context
                )
                return "rejected"
            except ExchangeError as exc:
                self.healthy = False
                await self._signal_status(signal_id, "failed", str(exc), reason_code=exc.code)
                await self.trip(f"Preflight exchange failure: {exc.code}")
                return "failed"

    @staticmethod
    def _aware(value: datetime) -> datetime:
        return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)

    async def _daily(self, balance):
        limits = await self.allocator.limits()
        today = utcnow().astimezone(ISTANBUL).date()
        midnight, end = day_bounds(today)
        loss = await self.accounting.daily_realized_loss(midnight, end)
        async with self.db.sessions.begin() as session:
            daily = await session.get(DailyRisk, today)
            if daily is None:
                daily = DailyRisk(day=today, starting_equity=balance.equity, loss_latched=False)
                session.add(daily)
            if loss >= daily.starting_equity * limits.max_daily_loss_percent / 100:
                daily.loss_latched = True
            blocked = daily.loss_latched
            start = daily.starting_equity
        if blocked:
            raise DailyLossLimitReached(
                "Daily loss limit reached (latched until next Istanbul day)",
                code="DAILY_LOSS_LIMIT",
            )
        return start, loss

    async def _ownership_audit(self, orders):
        async with self.db.sessions() as session:
            for order in orders:
                if order.status != "open":
                    continue
                foreign = await session.scalar(
                    select(Execution.id)
                    .where(
                        Execution.inst_id == order.inst_id,
                        Execution.timestamp >= self._aware(order.created_at),
                        Execution.order_id.is_(None),
                    )
                    .limit(1)
                )
                if foreign:
                    raise RiskRejected(
                        "Foreign fills on bot-owned instrument", code="UNKNOWN_OWNERSHIP"
                    )

    async def _execute(self, signal_id: int, signal: Signal) -> str:
        limits = await self.allocator.limits()
        instrument = await self.exchange.instrument(signal.inst_id)
        config = await self.exchange.account_config()
        await self.allocator.verify_account(config)
        positions = await self.exchange.positions()
        pending = await self.exchange.open_orders()
        algos = await self.exchange.open_algo_orders()
        async with self.db.sessions() as session:
            active_orders = list(
                (
                    await session.scalars(select(TradeOrder).where(TradeOrder.status.in_(ACTIVE)))
                ).all()
            )
        if (
            any(
                (p["instId"], p.get("posSide", "net"))
                not in {(o.inst_id, o.pos_side) for o in active_orders}
                for p in positions
            )
            or any(p.get("clOrdId") not in {o.client_id for o in active_orders} for p in pending)
            or any(not self._owned_algo(a, active_orders) for a in algos)
        ):
            await self.trip("Unmanaged exchange inventory during preflight")
            raise RiskRejected("Unknown inventory ownership", code="UNKNOWN_OWNERSHIP")
        if any(p["instId"] == signal.inst_id for p in positions + pending):
            raise RiskRejected(
                "An existing position/order already uses this instrument", code="EXISTING_EXPOSURE"
            )
        balance = await self.exchange.balance()
        await self.accounting.sync()
        try:
            await self._ownership_audit(active_orders)
            for existing in active_orders:
                if existing.status != "open":
                    raise RiskRejected("Unresolved trade lifecycle", code="UNKNOWN_OWNERSHIP")
                self._validate_position(existing, self._matching_position(existing, positions))
        except RiskRejected:
            await self.trip("Uncertain ownership during entry preflight")
            raise
        start, loss = await self._daily(balance)
        cycle = await self.allocator.cycle(
            balance, verified_flat=not positions and not pending and not algos
        )
        quote = await self.exchange.quote(signal.inst_id)
        market, mark = quote.market(signal.direction), quote.mark
        if quote.spread_percent > limits.max_bid_ask_spread_percent:
            raise RiskRejected(
                "Bid/ask spread exceeds limit",
                code="SPREAD_LIMIT",
                context={
                    "spread_percent": str(quote.spread_percent),
                    "max_spread_percent": str(limits.max_bid_ask_spread_percent),
                },
            )
        fee, tiers = await self.exchange.risk_metadata(instrument)
        if any(o.estimated_risk is None for o in active_orders):
            raise RiskRejected("Legacy order risk requires review", code="UNKNOWN_OWNERSHIP")
        open_risk = sum((o.estimated_risk for o in active_orders), Decimal(0))
        plan = self.risk.plan(
            signal,
            instrument,
            balance,
            market,
            mark,
            fee,
            tiers,
            len(active_orders),
            start,
            loss,
            target_margin=cycle.slot_target_margin,
            open_risk=open_risk,
            limits=limits,
        )
        pos_side = "net" if config["posMode"] == "net_mode" else signal.direction.value
        # Recheck age immediately before the durable order intent.
        if (utcnow() - signal.timestamp).total_seconds() > self.settings.max_signal_age_seconds:
            raise RiskRejected("Signal expired during preflight", code="STALE_OR_FUTURE_SIGNAL")
        identifier = hashlib.sha256(
            f"{self.settings.telegram_channel_id}:{signal_id}".encode()
        ).hexdigest()[:28]
        order = TradeOrder(
            signal_id=signal_id,
            client_id=f"e{identifier}",
            algo_client_id=f"a{identifier}",
            inst_id=signal.inst_id,
            direction=signal.direction.value,
            pos_side=pos_side,
            contracts=plan.contracts,
            entry=plan.limit_price,
            reference_price=signal.entry,
            take_profit=plan.take_profit,
            stop_loss=plan.stop_loss,
            estimated_risk=plan.risk_amount,
            status="prepared",
            execution_context={
                "bid": str(quote.bid),
                "ask": str(quote.ask),
                "mark": str(mark),
                "spread_percent": str(quote.spread_percent),
                "range_percent": str(signal.range_percent)
                if signal.range_percent is not None
                else None,
                "coefficient": "0.6",
                "take_profit_source": signal.take_profit_source,
                "stop_loss_source": signal.stop_loss_source,
                "signal_take_profit": str(signal.take_profit),
                "signal_stop_loss": str(signal.stop_loss),
                "initial_take_profit": str(plan.take_profit),
                "initial_stop_loss": str(plan.stop_loss),
                "tick": str(instrument.tick),
                "fee": str(fee),
                "contract_value": str(instrument.contract_value),
                "risk_budget": str(
                    balance.equity * limits.max_portfolio_risk_percent / 100 - open_risk
                ),
                "account_equity": str(balance.equity),
                "open_risk": str(open_risk),
                "max_deviation_percent": str(limits.max_entry_price_deviation_percent),
                "liquidation_buffer_percent": str(limits.liquidation_buffer_percent),
                "max_slippage_percent": str(limits.max_order_slippage_percent),
                "entry_market": str(market),
                "unusual_stop_distance_percent": str(limits.unusual_stop_distance_percent),
                "signal_timestamp": signal.timestamp.isoformat(),
                "protection_policy": "range_from_fill_preserve_explicit_v1",
            },
        )
        await self.allocator.reserve(
            order,
            cycle,
            plan.required_margin,
            balance,
            limits,
            balance_provider=self.exchange.balance,
        )
        payload = {
            "instId": order.inst_id,
            "tdMode": "isolated",
            "posSide": order.pos_side,
            "clOrdId": order.client_id,
            "side": "buy" if order.direction == "long" else "sell",
            "ordType": "ioc",
            "px": str(plan.limit_price),
            "sz": str(plan.contracts),
            "attachAlgoOrds": [
                {
                    "attachAlgoClOrdId": order.algo_client_id,
                    "tpTriggerPx": str(plan.take_profit),
                    "tpOrdPx": "-1",
                    "tpTriggerPxType": "mark",
                    "slTriggerPx": str(plan.stop_loss),
                    "slOrdPx": "-1",
                    "slTriggerPxType": "mark",
                }
            ],
        }
        try:
            await self.exchange.set_leverage(signal.inst_id, pos_side)
            # Do not submit if any safety event, stale signal or balance change occurred.
            state = await self.state()
            fresh_balance = await self.exchange.balance()
            if state.paused or state.emergency or not self.healthy or self.fatal:
                await self._order_status(order.id, "canceled")
                return "canceled"
            if (
                (utcnow() - signal.timestamp).total_seconds() > self.settings.max_signal_age_seconds
                or fresh_balance.available < plan.required_margin + limits.minimum_free_margin
            ):
                await self._order_status(order.id, "canceled")
                raise RiskRejected("Pre-submit checks changed", code="INSUFFICIENT_BALANCE")
            await self._order_status(order.id, "submitting")
            ack = await self.exchange.place_entry(payload)
            order.exchange_id = ack["ordId"]
            await self._order_status(order.id, "submitting", exchange_id=ack["ordId"])
            await self.accounting.link(order, ack["ordId"], "entry")
            await self.event(
                "order_submitted",
                order_id=order.id,
                signal_id=signal_id,
                inst_id=order.inst_id,
                direction=order.direction,
            )
            result = await self._settle_entry(order)
            await self._order_status(
                order.id,
                "submitting",
                filled=decimal(result.get("accFillSz")),
                exchange_id=result["ordId"],
            )
            if decimal(result.get("accFillSz")) == 0:
                await self._order_status(order.id, "canceled")
                await self.event("entry_canceled", inst_id=order.inst_id, signal_id=signal_id)
                return "canceled"
            # Partial fills are canceled/flattened, never promoted into an open trade.
            if result["state"] != "filled" or decimal(result["accFillSz"]) != order.contracts:
                await self.event(
                    "entry_partial",
                    order_id=order.id,
                    signal_id=signal_id,
                    inst_id=order.inst_id,
                    contracts=str(decimal(result["accFillSz"])),
                )
                raise RiskRejected("Partial fill: flattening confirmed exposure")
            await self.event(
                "entry_filled",
                order_id=order.id,
                signal_id=signal_id,
                inst_id=order.inst_id,
                fill_price=str(decimal(result["avgPx"])),
                contracts=str(decimal(result["accFillSz"])),
                notify=False,
            )
            await self._verify_open(order, result)
            return "open"
        except ExchangeError as exc:
            async with self.db.sessions() as session:
                durable = await session.get(TradeOrder, order.id)
            if durable.status == "prepared":
                # Leverage setup failed before the entry's submitting commit.
                # No entry POST can have occurred, so its slot can be released safely.
                await self._order_status(order.id, "canceled")
                self.healthy = False
                await self.trip(f"Exchange setup failure before submission: {exc.code}")
                return "failed"
            if not exc.ambiguous and exc.code not in {"TRANSPORT_OR_RESPONSE", "RETRIES_EXHAUSTED"}:
                # A rejection is terminal only if no position/order could have been sent.
                # An ACK may already exist, so recover by inspecting the exchange.
                if "ack" not in locals():
                    await self._order_status(order.id, "rejected")
                    await self.event(
                        "order_rejected", inst_id=order.inst_id, signal_id=signal_id, code=exc.code
                    )
                    return "rejected"
            await self._recover_failure(order, f"Order/protection uncertainty: {exc.code}")
            return "emergency"
        except (RiskRejected, KeyError, ValueError, ArithmeticError) as exc:
            async with self.db.sessions() as session:
                saved = await session.get(TradeOrder, order.id)
            if saved.status in {"prepared", "canceled"}:
                await self._order_status(order.id, "canceled")
                raise
            await self._recover_failure(order, f"Protection/entry validation: {exc}")
            return "emergency"
        except Exception:
            # DB/implementation failure after send must still latch the kill switch.
            await self._recover_failure(order, "Unexpected failure after durable order intent")
            raise

    async def _settle_entry(self, order: TradeOrder) -> dict:
        for _ in range(self.settings.order_poll_attempts):
            row = await self.exchange.order(order.inst_id, order.client_id)
            if row and (
                row.get("ordId") != order.exchange_id
                or row.get("instId") != order.inst_id
                or row.get("clOrdId") != order.client_id
            ):
                raise RiskRejected("Entry order identity mismatch", code="UNKNOWN_OWNERSHIP")
            if row and row.get("state") in TERMINAL_EXCHANGE:
                return row
            await asyncio.sleep(self.settings.order_poll_interval_seconds)
        await self.exchange.cancel_order(order.inst_id, order.client_id)
        row = await self.exchange.order(order.inst_id, order.client_id)
        if not row or row.get("state") not in TERMINAL_EXCHANGE:
            raise ExchangeError("ENTRY_STATE_UNCERTAIN", ambiguous=True)
        return row

    def _matching_position(self, order, rows):
        return next(
            (
                p
                for p in rows
                if p["instId"] == order.inst_id and p.get("posSide", "net") == order.pos_side
            ),
            None,
        )

    def _validate_position(self, order, position):
        if (
            not position
            or position.get("mgnMode") != "isolated"
            or decimal(position.get("lever")) != 10
        ):
            raise RiskRejected("Missing position or incorrect 10x isolated settings")
        quantity = decimal(position.get("pos"))
        if order.position_id and position.get("posId") != order.position_id:
            raise RiskRejected("Position identity changed", code="UNKNOWN_OWNERSHIP")
        if order.pos_side == "net" and (quantity > 0) != (order.direction == "long"):
            raise RiskRejected("Exchange position direction mismatch")
        if abs(quantity) != order.contracts:
            raise RiskRejected("Position size differs from confirmed bot entry")
        liquidation = decimal(position.get("liqPx"))
        safe_stop(
            order.direction,
            order.stop_loss,
            liquidation,
            decimal((order.execution_context or {}).get("liquidation_buffer_percent", "0")),
        )
        average = decimal(position.get("avgPx"))
        if (
            abs(average - order.reference_price) / order.reference_price
            > decimal(
                (order.execution_context or {}).get(
                    "max_deviation_percent", str(self.settings.max_entry_price_deviation_percent)
                )
            )
            / 100
        ):
            raise RiskRejected("Actual fill exceeds allowed entry deviation")
        if not (
            order.stop_loss < average < order.take_profit
            if order.direction == "long"
            else order.take_profit < average < order.stop_loss
        ):
            raise RiskRejected("Actual fill no longer lies between stop and target")
        return quantity, liquidation, average

    def _validate_algo(self, order, algo):
        if not algo or algo.get("state") != "live" or algo.get("ordType") != "oco":
            raise RiskRejected("Live OCO protection not confirmed")
        if (
            algo.get("instId") != order.inst_id
            or algo.get("posSide", "net") != order.pos_side
            or algo.get("tdMode") != "isolated"
            or algo.get("algoClOrdId") != order.algo_client_id
            or algo.get("side") != ("sell" if order.direction == "long" else "buy")
            or decimal(algo.get("sz")) != order.contracts
            or decimal(algo.get("tpTriggerPx")) != order.take_profit
            or decimal(algo.get("slTriggerPx")) != order.stop_loss
            or algo.get("tpOrdPx") != "-1"
            or algo.get("slOrdPx") != "-1"
            or algo.get("slTriggerPxType") != "mark"
            or algo.get("tpTriggerPxType") != "mark"
            or (order.pos_side == "net" and str(algo.get("reduceOnly")).lower() != "true")
        ):
            raise RiskRejected("Protective OCO parameters do not match intended position")

    async def _verify_open(self, order, result):
        algo = None
        for _ in range(self.settings.order_poll_attempts):
            algo = await self.exchange.algo(order.algo_client_id)
            if algo and algo.get("state") == "live":
                break
            await asyncio.sleep(self.settings.order_poll_interval_seconds)
        self._validate_algo(order, algo)
        position = self._matching_position(order, await self.exchange.positions())
        quantity, liquidation, average = self._validate_position(order, position)
        fill_price = decimal(result.get("avgPx"))
        tick = decimal((order.execution_context or {}).get("tick", "0"))
        if fill_price <= 0 or abs(fill_price - average) > tick:
            raise RiskRejected(
                "Order fill and position average disagree", code="FILL_PRICE_MISMATCH"
            )
        tp, sl = fill_levels(order, fill_price, liquidation)
        execution = order.execution_context or {}
        if execution.get("entry_market") and abs(
            fill_price - decimal(execution["entry_market"])
        ) / decimal(execution["entry_market"]) * 100 > decimal(execution["max_slippage_percent"]):
            raise RiskRejected("Actual fill slippage exceeds limit", code="SLIPPAGE_LIMIT")
        changed = tp != order.take_profit or sl != order.stop_loss
        context = {
            **(order.execution_context or {}),
            "fill_price": str(fill_price),
            "position_average": str(average),
            "final_take_profit": str(tp),
            "final_stop_loss": str(sl),
        }
        # The durable desired levels remain in submitting until GET verifies protection.
        async with self.db.sessions.begin() as session:
            tracked = await session.get(TradeOrder, order.id)
            tracked.take_profit, tracked.stop_loss, tracked.execution_context = tp, sl, context
        order.take_profit, order.stop_loss, order.execution_context = tp, sl, context
        if changed:
            await self.exchange.amend_protection(order.inst_id, algo["algoId"], tp, sl)
            for _ in range(self.settings.order_poll_attempts):
                algo = await self.exchange.algo(order.algo_client_id)
                try:
                    self._validate_algo(order, algo)
                    break
                except RiskRejected:
                    await asyncio.sleep(self.settings.order_poll_interval_seconds)
            else:
                raise RiskRejected(
                    "Fill-based protection not confirmed", code="PROTECTION_UNCONFIRMED"
                )
            position = self._matching_position(order, await self.exchange.positions())
            quantity, liquidation, average = self._validate_position(order, position)
        details = {
            "order_id": order.id,
            "signal_id": order.signal_id,
            "inst_id": order.inst_id,
            "direction": order.direction,
            "leverage": "10",
            "margin_mode": "isolated",
            "reference_price": str(order.reference_price),
            "fill_price": str(fill_price),
            "price_deviation_percent": str(
                abs(fill_price - order.reference_price) / order.reference_price * 100
            ),
            "trade_time": utcnow().isoformat(),
            "contracts": str(abs(quantity)),
            "take_profit": str(order.take_profit),
            "stop_loss": str(order.stop_loss),
            "algo_id": algo["algoId"],
        }
        if order.execution_context:
            ct_value = decimal(order.execution_context["contract_value"])
            margin = abs(quantity) * ct_value * fill_price / 10
            risk = (
                abs(quantity)
                * ct_value
                * (
                    abs(fill_price - order.stop_loss)
                    + decimal(order.execution_context["fee"]) * (fill_price + order.stop_loss)
                )
            )
            equity = decimal(order.execution_context["account_equity"])
            details.update(
                {
                    "actual_margin": str(margin),
                    "target_margin": str(order.target_margin),
                    "notional": str(margin * 10),
                    "estimated_sl_loss": str(risk),
                    "risk_margin_percent": str(risk / margin * 100),
                    "risk_equity_percent": str(risk / equity * 100),
                    "portfolio_risk_percent": str(
                        (risk + decimal(order.execution_context["open_risk"])) / equity * 100
                    ),
                    "unusual_stop": abs(fill_price - order.stop_loss) / fill_price * 100
                    > decimal(order.execution_context["unusual_stop_distance_percent"]),
                }
            )
        else:
            margin, risk = None, None
        async with self.db.sessions.begin() as session:
            tracked = await session.get(TradeOrder, order.id, with_for_update=True)
            was_open = tracked.status == "open"
            saved = await session.scalar(select(Position).where(Position.order_id == order.id))
            if saved is None:
                session.add(
                    Position(
                        order_id=order.id,
                        inst_id=order.inst_id,
                        pos_side=order.pos_side,
                        contracts=abs(quantity),
                        avg_price=average,
                        liquidation_price=liquidation,
                    )
                )
            tracked.status, tracked.algo_id = "open", algo["algoId"]
            tracked.filled = decimal(result["accFillSz"])
            tracked.actual_margin, tracked.estimated_risk = margin, risk
            tracked.position_id = position.get("posId")
            signal = await session.get(TelegramSignal, tracked.signal_id)
            signal.status = "open"
            if not was_open:
                session.add(BotEvent(kind="order_opened", details=details))
                # Included in the opened notification; retain the separate audit event.
                session.add(BotEvent(kind="tpsl_placed", details={**details, "notify": False}))
        if not was_open:
            logger.info("order_opened", extra={"context": details})
            logger.info("tpsl_placed", extra={"context": details})

    async def _assert_close_ownership(self, order, row, position):
        if not self.settings.dedicated_trading_account_confirmed:
            raise RiskRejected(
                "Dedicated trading account confirmation required for automated exits",
                code="UNKNOWN_OWNERSHIP",
            )
        if not row or not row.get("ordId") or row.get("state") not in TERMINAL_EXCHANGE:
            raise RiskRejected("Entry evidence unavailable", code="UNKNOWN_OWNERSHIP")
        if (
            row.get("instId") != order.inst_id
            or row.get("clOrdId") != order.client_id
            or (order.exchange_id and row["ordId"] != order.exchange_id)
        ):
            raise RiskRejected("Entry identity does not prove ownership", code="UNKNOWN_OWNERSHIP")
        filled = decimal(row.get("accFillSz"))
        if order.position_id and position and position.get("posId") != order.position_id:
            raise RiskRejected("Position identity changed", code="UNKNOWN_OWNERSHIP")
        if position:
            quantity = decimal(position.get("pos"))
            if (
                position.get("mgnMode") != "isolated"
                or abs(quantity) > filled
                or filled <= 0
                or (order.pos_side == "net" and (quantity > 0) != (order.direction == "long"))
            ):
                raise RiskRejected("Position ownership/size is ambiguous", code="UNKNOWN_OWNERSHIP")
        pending = await self.exchange.open_orders()
        if any(
            p.get("instId") == order.inst_id and p.get("clOrdId") != order.client_id
            for p in pending
        ):
            raise RiskRejected("Foreign pending order on bot instrument", code="UNKNOWN_OWNERSHIP")
        algos = await self.exchange.open_algo_orders()
        if any(
            a.get("instId") == order.inst_id and not self._owned_algo(a, [order]) for a in algos
        ):
            raise RiskRejected("Foreign algo on bot instrument", code="UNKNOWN_OWNERSHIP")
        await self.accounting.link(order, row["ordId"], "entry")
        algo = await self.exchange.algo(order.algo_client_id)
        await self.accounting.link_algo(order, algo)
        await self.accounting.sync()
        async with self.db.sessions() as session:
            foreign = list(
                (
                    await session.scalars(
                        select(Execution).where(
                            Execution.inst_id == order.inst_id,
                            Execution.timestamp >= self._aware(order.created_at),
                            Execution.order_id.is_(None),
                        )
                    )
                ).all()
            )
        if foreign or (position and decimal(row.get("accFillSz")) < abs(decimal(position["pos"]))):
            raise RiskRejected(
                "Foreign fills invalidate position ownership", code="UNKNOWN_OWNERSHIP"
            )
        return filled

    async def _flatten(self, order):
        row = await self.exchange.order(order.inst_id, order.client_id)
        if row and (
            row.get("instId") != order.inst_id
            or row.get("clOrdId") != order.client_id
            or (order.exchange_id and row["ordId"] != order.exchange_id)
        ):
            raise RiskRejected("Entry identity does not prove ownership", code="UNKNOWN_OWNERSHIP")
        if row and row.get("state") not in TERMINAL_EXCHANGE:
            await self.exchange.cancel_order(order.inst_id, order.client_id)
            row = await self.exchange.order(order.inst_id, order.client_id)
            if not row or row.get("state") not in TERMINAL_EXCHANGE:
                raise ExchangeError("CANCEL_NOT_CONFIRMED", ambiguous=True)
        if row is None:
            raise ExchangeError("SUBMISSION_NOT_RESOLVED", ambiguous=True)
        position = self._matching_position(order, await self.exchange.positions())
        if decimal(row.get("accFillSz")) == 0 and position is None:
            await self._order_status(order.id, "canceled", exchange_id=row["ordId"])
            return
        await self._assert_close_ownership(order, row, position)
        await self._order_status(
            order.id, "closing", exchange_id=row["ordId"], filled=decimal(row.get("accFillSz"))
        )
        if position:
            quantity = abs(decimal(position["pos"]))
            client_id = "x" + order.client_id[1:]
            async with self.db.sessions.begin() as session:
                saved = await session.get(TradeOrder, order.id, with_for_update=True)
                context = saved.execution_context or {}
                started = context.get("close_submission_started", False)
                saved.execution_context = {
                    **context,
                    "close_submission_started": True,
                    "close_client_id": client_id,
                }
            if started:
                close = await self.exchange.order(order.inst_id, client_id)
                if not close:
                    raise ExchangeError("CLOSE_SUBMISSION_UNRESOLVED", ambiguous=True)
            else:
                close = await self.exchange.close_owned(order, quantity, client_id)
            await self.accounting.link(order, close["ordId"], "exit", "manual")
        for _ in range(self.settings.order_poll_attempts):
            if self._matching_position(order, await self.exchange.positions()) is None:
                break
            await asyncio.sleep(self.settings.order_poll_interval_seconds)
        else:
            raise ExchangeError("CLOSE_NOT_CONFIRMED", ambiguous=True)
        algo = await self.exchange.algo(order.algo_client_id)
        if algo and algo.get("state") == "live":
            if not self._owned_algo(algo, [order]):
                raise RiskRejected("Unowned protective order", code="UNKNOWN_OWNERSHIP")
            await self.exchange.cancel_algo(order.inst_id, algo["algoId"])
            after = await self.exchange.algo(order.algo_client_id)
            if after and after.get("state") == "live":
                raise ExchangeError("ALGO_CANCEL_NOT_CONFIRMED", ambiguous=True)
        await self.accounting.sync()
        if not await self._mark_closed(order, "unknown"):
            raise RiskRejected("Exit fills are not yet complete", code="CLOSURE_UNCONFIRMED")

    async def _recover_failure(self, order, reason):
        await self._order_status(order.id, "emergency")
        await self.trip(reason)
        try:
            await self._flatten(order)
        except (ExchangeError, RiskRejected):
            await self._order_status(order.id, "emergency")
            await self.event(
                "manual_intervention_required",
                inst_id=order.inst_id,
                reason=reason,
                event_key=f"manual:demo:{order.id}:"
                + hashlib.sha256(reason.encode()).hexdigest()[:24],
            )

    async def _mark_closed(self, order, cause):
        # A vanished position or a trigger-price crossing alone is never closure evidence.
        totals = await self.accounting.totals(order.id)
        if not totals["complete"]:
            await self._order_status(order.id, "settling")
            return False
        balance = await self.exchange.balance()
        daily = (await self.reporter.pnl())["today"]["net_pnl"]
        async with self.db.sessions.begin() as session:
            tracked = await session.get(TradeOrder, order.id, with_for_update=True)
            if tracked.closed_at:
                return True
            if tracked.actual_margin is None and (tracked.execution_context or {}).get(
                "contract_value"
            ):
                tracked.actual_margin = (
                    tracked.filled
                    * decimal(tracked.execution_context["contract_value"])
                    * totals["entry_price"]
                    / 10
                )
            position = await session.scalar(select(Position).where(Position.order_id == order.id))
            if position:
                position.status, position.contracts, position.updated_at = (
                    "closed",
                    Decimal(0),
                    utcnow(),
                )
            tracked.status, tracked.closed_at = "closed", totals["closed_at"]
            tracked.close_cause, tracked.accounting_complete = (
                totals["cause"],
                totals["funding_complete"],
            )
            await self.allocator.release(session, order.id)
            signal = await session.get(TelegramSignal, tracked.signal_id)
            signal.status = "closed"
            session.add(await self.accounting.close_event(tracked, totals, balance.equity, daily))
        return True

    async def _store_fills(self):
        await self.accounting.sync()

    async def reconcile(self, *, startup=False):
        async with self.lock:
            self.healthy = False
            stage = "persist_previous_failure"
            try:
                stage = "initialize_limits"
                await self.allocator.limits()
                if self._unpersisted_failure:
                    await self._persist_reconciliation_failure(self._unpersisted_failure)
                    self._unpersisted_failure = None
                stage = "account_config"
                config = await self.exchange.account_config()  # Also verifies demo credentials.
                await self.allocator.verify_account(config)
                stage = "positions"
                positions = await self.exchange.positions()
                stage = "pending_orders"
                pending = await self.exchange.open_orders()
                stage = "pending_algos"
                algos = await self.exchange.open_algo_orders()
                stage = "database_orders"
                async with self.db.sessions() as session:
                    orders = list(
                        (
                            await session.scalars(
                                select(TradeOrder).where(TradeOrder.status.in_(ACTIVE))
                            )
                        ).all()
                    )
                stage = "inventory_validation"
                known = {(o.inst_id, o.pos_side) for o in orders}
                blockers = []
                if any((p["instId"], p.get("posSide", "net")) not in known for p in positions):
                    blockers.append("unmanaged_positions")
                    await self.trip(
                        "Unmanaged exchange position found; dedicated demo account required"
                    )
                known_clients = {o.client_id for o in orders}
                if any(p.get("clOrdId") not in known_clients for p in pending):
                    blockers.append("unmanaged_orders")
                    await self.trip("Unmanaged pending order found")
                if any(not self._owned_algo(algo, orders) for algo in algos):
                    blockers.append("unmanaged_algos")
                    await self.trip("Unmanaged pending algo order found")
                for order in orders:
                    stage = "order_recovery"
                    if order.status == "prepared":
                        # No request can have been sent before 'submitting' was committed.
                        await self._order_status(order.id, "canceled")
                        continue
                    if order.status in {"submitting", "emergency", "closing"}:
                        await self._recover_failure(order, "Recovering interrupted order lifecycle")
                        continue
                    stage = "position_protection"
                    position = self._matching_position(order, positions)
                    algo = await self.exchange.algo(order.algo_client_id)
                    if position is None:
                        stage = "closure_evidence"
                        await self.accounting.sync()
                        if algo and algo.get("state") == "live":
                            if not self._owned_algo(algo, [order]):
                                raise RiskRejected(
                                    "Unowned protective order", code="UNKNOWN_OWNERSHIP"
                                )
                            await self.exchange.cancel_algo(order.inst_id, algo["algoId"])
                            after = await self.exchange.algo(order.algo_client_id)
                            if after and after.get("state") == "live":
                                raise ExchangeError(
                                    "ORPHAN_ALGO_CANCEL_UNCONFIRMED", ambiguous=True
                                )
                        closed = await self._mark_closed(order, "unknown")
                        if not closed:
                            blockers.append("closure_evidence_pending")
                            await self.trip("Closed exposure awaits verified exit fills")
                    else:
                        try:
                            quantity, liquidation, average = self._validate_position(
                                order, position
                            )
                            self._validate_algo(order, algo)
                            async with self.db.sessions.begin() as session:
                                saved = await session.scalar(
                                    select(Position).where(Position.order_id == order.id)
                                )
                                if saved:
                                    saved.contracts = abs(quantity)
                                    saved.avg_price = average
                                    saved.liquidation_price = liquidation
                                    saved.updated_at = utcnow()
                        except RiskRejected as exc:
                            # Quantity changes can be manual additions or partial exits.
                            # Preserve protection and never flatten ambiguous merged exposure.
                            await self.trip(str(exc))
                            blockers.append("position_validation_failed")
                            if abs(decimal(position.get("pos"))) == order.contracts:
                                await self._recover_failure(order, str(exc))
                stage = "executions"
                await self._store_fills()
                async with self.db.sessions() as session:
                    current = list(
                        (
                            await session.scalars(
                                select(TradeOrder).where(TradeOrder.status == "open")
                            )
                        ).all()
                    )
                try:
                    await self._ownership_audit(current)
                except RiskRejected:
                    blockers.append("ownership_ambiguous")
                    await self.trip("Foreign fills invalidate position ownership; no intervention")
                stage = "balance"
                balance = await self.exchange.balance()
                if not positions and not pending and not algos and not blockers:
                    async with self.db.sessions() as session:
                        active_count = await session.scalar(
                            select(func.count())
                            .select_from(TradeOrder)
                            .where(TradeOrder.status.in_(ACTIVE))
                        )
                    if not active_count:
                        await self.allocator.cycle(balance, verified_flat=True)
                stage = "daily_risk"
                try:
                    await self._daily(balance)
                except DailyLossLimitReached as exc:
                    blockers.append("daily_loss_limit")
                    if startup:
                        await self.event("daily_risk_block", reason=str(exc))
                stage = "persist_success"
                async with self.db.sessions.begin() as session:
                    state = await session.get(RuntimeState, 1, with_for_update=True)
                    recovered = state.last_reconciliation_ok is False
                    state.last_reconciliation_at = utcnow()
                    state.last_reconciliation_ok = True
                    state.last_reconciliation_summary = {
                        "account_level": config["acctLv"],
                        "position_mode": config["posMode"],
                        "exchange_positions": len(positions),
                        "pending_orders": len(pending),
                        "pending_algos": len(algos),
                        "blockers": blockers,
                    }
                    if recovered:
                        state.reconciliation_recovered_at = utcnow()
                        session.add(
                            BotEvent(
                                kind="reconciliation_recovered",
                                details={
                                    "approval_required": state.paused or state.emergency,
                                },
                            )
                        )
                self.last_sync = utcnow()
                self.healthy = not self.fatal
                if recovered:
                    logger.info("reconciliation_recovered; administrator approval required")
            except Exception as exc:
                await self._record_reconciliation_failure(exc, stage)
                raise

    async def pause(self, *, notify: bool = True):
        self._resume_approvals.clear()
        async with self.lock, self.db.sessions.begin() as session:
            state = await session.get(RuntimeState, 1, with_for_update=True)
            state.paused = True
            state.state_revision += 1
            if not state.emergency:
                state.reason = "Administrator pause"
        await self.event("paused", notify=notify)

    @staticmethod
    def _owned_algo(algo, orders):
        return any(
            algo.get("instId") == order.inst_id
            and (
                algo.get("algoClOrdId") == order.algo_client_id
                or (order.algo_id and algo.get("algoId") == order.algo_id)
            )
            for order in orders
        )

    async def _resume_snapshot(self) -> dict:
        """Fresh validation only: never cancels, closes, places orders, or changes leverage."""
        if self.fatal:
            raise RiskRejected("Required background task stopped; restart the service")
        if not self.settings.okx_demo or self.settings.live_trading_enabled:
            raise RiskRejected("LIVE_TRADING_DISABLED")
        if (
            not self.healthy
            or not self.last_sync
            or (utcnow() - self.last_sync).total_seconds() > 120
        ):
            raise RiskRejected("A successful recent reconciliation is required before resuming")
        config = await self.exchange.account_config()
        await self.allocator.verify_account(config)
        positions = await self.exchange.positions()
        pending = await self.exchange.open_orders()
        algos = await self.exchange.open_algo_orders()
        async with self.db.sessions() as session:
            orders = list(
                (
                    await session.scalars(select(TradeOrder).where(TradeOrder.status.in_(ACTIVE)))
                ).all()
            )
            saved_positions = list(
                (await session.scalars(select(Position).where(Position.status == "open"))).all()
            )
        if any(o.status != "open" for o in orders):
            raise RiskRejected("An unresolved order requires manual intervention")
        keys = {(o.inst_id, o.pos_side) for o in orders}
        if len(keys) != len(orders):
            raise RiskRejected("Multiple tracked orders share one exchange position")
        if any((p["instId"], p.get("posSide", "net")) not in keys for p in positions):
            raise RiskRejected("Unmanaged exchange exposure remains")
        if pending:
            raise RiskRejected("Pending exchange orders remain")
        if any(not self._owned_algo(algo, orders) for algo in algos):
            raise RiskRejected("Unmanaged pending algo orders remain")
        if len(saved_positions) != len(orders) or any(
            p.order_id not in {o.id for o in orders} for p in saved_positions
        ):
            raise RiskRejected("Database position state does not match active orders")
        checked = []
        for order in orders:
            row = await self.exchange.order(order.inst_id, order.client_id)
            if (
                not row
                or row.get("state") != "filled"
                or decimal(row.get("accFillSz")) != order.contracts
            ):
                raise RiskRejected("Entry order is not fully confirmed")
            position = self._matching_position(order, positions)
            self._validate_position(order, position)
            saved = next(p for p in saved_positions if p.order_id == order.id)
            if (
                saved.contracts != order.contracts
                or saved.inst_id != order.inst_id
                or saved.pos_side != order.pos_side
            ):
                raise RiskRejected("Database position quantity or side mismatch")
            algo = await self.exchange.algo(order.algo_client_id)
            self._validate_algo(order, algo)
            if not any(a.get("algoId") == algo["algoId"] for a in algos):
                raise RiskRejected("Protective algo missing from pending order snapshot")
            checked.append(
                {
                    "order_id": order.id,
                    "inst_id": order.inst_id,
                    "pos_side": order.pos_side,
                    "contracts": str(order.contracts),
                    "algo_id": algo["algoId"],
                    "tp": str(order.take_profit),
                    "sl": str(order.stop_loss),
                }
            )
        balance = await self.exchange.balance()
        if balance.equity <= 0 or balance.available <= 0:
            raise RiskRejected("Positive USDT equity and available balance are required")
        start, loss = await self._daily(balance)
        return {
            "account_level": config["acctLv"],
            "position_mode": config["posMode"],
            "positions": checked,
            "pending_orders": len(pending),
            "pending_algos": len(algos),
            "risk_day": utcnow().astimezone(ISTANBUL).date().isoformat(),
            "starting_equity": str(start),
            "current_equity": str(balance.equity),
            "available_margin": str(balance.available),
            "gross_daily_loss": str(loss),
        }

    @staticmethod
    def _snapshot_hash(snapshot):
        return hashlib.sha256(json.dumps(snapshot, sort_keys=True).encode()).hexdigest()

    async def _checked_resume_snapshot(self):
        try:
            return await self._resume_snapshot()
        except RiskRejected as exc:
            await self.trip(f"Resume validation rejected: {exc}")
            raise
        except Exception as exc:
            await self._record_reconciliation_failure(exc, "resume_validation")
            raise

    async def request_resume(self, *, actor: str) -> dict:
        async with self.lock:
            before = await self.state()
            snapshot = await self._checked_resume_snapshot()
            state = await self.state()
            if state.state_revision != before.state_revision or self.fatal:
                raise RiskRejected("Safety state changed during validation; request /resume again")
            if not state.paused and not state.emergency:
                return {"status": "already_running", "mode": "demo"}
            token = secrets.token_urlsafe(24)
            expires = utcnow() + timedelta(seconds=120)
            self._resume_approvals[actor] = {
                "token_hash": hashlib.sha256(token.encode()).hexdigest(),
                "expires": expires,
                "revision": state.state_revision,
                "snapshot_hash": self._snapshot_hash(snapshot),
            }
            await self.event("resume_requested", actor=actor, state_revision=state.state_revision)
            return {
                "status": "confirmation_required",
                "mode": "demo",
                "confirmation": token,
                "expires_at": expires.isoformat(),
                "summary": snapshot,
            }

    async def resume(self, *, confirmation: str, actor: str):
        async with self.lock:
            challenge = self._resume_approvals.get(actor)
            if not challenge or not hmac.compare_digest(
                challenge["token_hash"], hashlib.sha256(confirmation.encode()).hexdigest()
            ):
                raise RiskRejected(
                    "Explicit administrator confirmation is required; request /resume first"
                )
            del self._resume_approvals[actor]
            state = await self.state()
            if utcnow() > challenge["expires"] or state.state_revision != challenge["revision"]:
                raise RiskRejected(
                    "Confirmation expired or safety state changed; request /resume again"
                )
            snapshot = await self._checked_resume_snapshot()
            if self._snapshot_hash(snapshot) != challenge["snapshot_hash"]:
                raise RiskRejected("Account snapshot changed; request /resume again")
            async with self.db.sessions.begin() as session:
                state = await session.get(RuntimeState, 1, with_for_update=True)
                if (
                    state.state_revision != challenge["revision"]
                    or self.fatal
                    or utcnow() > challenge["expires"]
                ):
                    raise RiskRejected(
                        "Safety state changed or confirmation expired during validation; "
                        "request /resume again"
                    )
                previous_reason = state.reason
                state.paused = state.emergency = False
                state.reason = None
                state.emergency_at = None
                state.state_revision += 1
                session.add(
                    BotEvent(
                        kind="resumed",
                        details={
                            "actor": actor,
                            "explicit_confirmation": True,
                            "previous_reason": previous_reason,
                            "notify": not actor.startswith("telegram:"),
                        },
                    )
                )
            self._resume_approvals.clear()

    async def close(self, inst_id: str):
        async with self.lock:
            async with self.db.sessions() as session:
                orders = list(
                    (
                        await session.scalars(
                            select(TradeOrder).where(
                                TradeOrder.inst_id == inst_id, TradeOrder.status.in_(ACTIVE)
                            )
                        )
                    ).all()
                )
            if not orders:
                raise RiskRejected("No active bot-owned order for this instrument")
            for order in orders:
                await self._order_status(order.id, "closing")
                try:
                    await self._flatten(order)
                except (ExchangeError, RiskRejected):
                    await self._order_status(order.id, "emergency")
                    await self.trip("Administrator close could not be confirmed")
                    raise

    async def status(self) -> dict:
        state = await self.state()
        fresh = bool(
            self.healthy and self.last_sync and (utcnow() - self.last_sync).total_seconds() <= 120
        )
        summary = state.last_reconciliation_summary or {}
        async with self.db.sessions() as session:
            open_positions = await session.scalar(
                select(func.count(Position.id)).where(Position.status == "open")
            )
            uncertain_notifications = await session.scalar(
                select(func.count())
                .select_from(BotEvent)
                .where(
                    BotEvent.delivered_at.is_(None),
                    BotEvent.delivery_state.in_({"sending", "uncertain"}),
                )
            )
        return {
            "mode": "demo",
            "live_enabled": False,
            "open_positions": open_positions,
            "uncertain_notifications": uncertain_notifications,
            "paused": state.paused,
            "emergency": state.emergency,
            "reason": state.reason,
            "exchange_healthy": fresh,
            "last_sync": self.last_sync.isoformat() if self.last_sync else None,
            "trading_enabled": bool(
                fresh
                and not self.fatal
                and not state.paused
                and not state.emergency
                and not summary.get("blockers")
            ),
            "requires_admin_approval": state.paused or state.emergency,
            "emergency_at": self._aware(state.emergency_at).isoformat()
            if state.emergency_at
            else None,
            "state_revision": state.state_revision,
            "reconciliation": {
                "ok": state.last_reconciliation_ok,
                "last_attempt_at": self._aware(state.last_reconciliation_at).isoformat()
                if state.last_reconciliation_at
                else None,
                "last_error": state.last_reconciliation_error or self.last_error,
                "recovered_at": self._aware(state.reconciliation_recovered_at).isoformat()
                if state.reconciliation_recovered_at
                else None,
                "summary": summary,
            },
        }

    async def trades(self) -> list[dict]:
        async with self.db.sessions() as session:
            rows = (
                await session.scalars(select(TradeOrder).order_by(TradeOrder.id.desc()).limit(20))
            ).all()
            return [
                {
                    "id": r.id,
                    "inst_id": r.inst_id,
                    "direction": r.direction,
                    "contracts": str(r.contracts),
                    "filled": str(r.filled),
                    "status": r.status,
                }
                for r in rows
            ]

    async def stats(self) -> dict:
        report = await self.reporter.period()
        return {
            "executions": report["executions"],
            "realized_pnl": report["gross_pnl"],
            "usdt_fees": report["fees"],
            "net_pnl": report["net_pnl"],
        }
