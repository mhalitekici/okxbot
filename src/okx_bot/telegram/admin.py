import asyncio
import logging
import time
from dataclasses import asdict

import httpx
from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError

from okx_bot.diagnostics import failure_details
from okx_bot.domain import ExchangeError, RiskRejected, utcnow
from okx_bot.models import BotEvent, RuntimeState
from okx_bot.telegram.formatting import format_command, format_notification


class AdminBot:
    def __init__(self, settings, engine, listener=None):
        self.settings, self.engine = settings, engine
        self.listener = listener
        self._next_report_check = 0.0
        self._memory_offset = 0
        self.http = httpx.AsyncClient(
            base_url=f"https://api.telegram.org/bot{settings.telegram_bot_token.get_secret_value()}/",
            timeout=40,
            follow_redirects=False,
            trust_env=False,
        )

    async def call(self, method, payload):
        response = await self.http.post(method, json=payload)
        if response.status_code != 200:
            raise RuntimeError("Telegram Bot API unavailable")
        data = response.json()
        if not data.get("ok"):
            raise RuntimeError("Telegram Bot API rejected request")
        return data.get("result")

    async def send(self, text: str):
        return await self.call(
            "sendMessage", {"chat_id": self.settings.telegram_admin_id, "text": text[:4000]}
        )

    async def command(self, update: dict) -> str | None:
        message = update.get("message", {})
        sender, chat = message.get("from", {}), message.get("chat", {})
        if (
            sender.get("id") != self.settings.telegram_admin_id
            or sender.get("is_bot")
            or chat.get("type") != "private"
            or chat.get("id") != self.settings.telegram_admin_id
        ):
            return None
        parts = message.get("text", "").split()
        if not parts:
            return None
        # Delayed mutating commands must not unexpectedly act after a restart.
        cmd = parts[0].split("@")[0]
        if cmd in {
            "/pause",
            "/resume",
            "/close",
            "/emergency",
            "/rebalance",
            "/confirm",
        } or cmd.startswith("/set"):
            age = utcnow().timestamp() - message.get("date", 0)
            if not -5 <= age <= 60:
                return format_command("expired")
        try:
            match cmd:
                case "/start":
                    return format_command(cmd)
                case "/status":
                    try:
                        value = await self.engine.status()
                        value["database_healthy"] = True
                    except SQLAlchemyError:
                        value = {
                            "mode": "demo",
                            "live_enabled": False,
                            "paused": True,
                            "emergency": True,
                            "trading_enabled": False,
                            "exchange_healthy": False,
                            "database_healthy": False,
                            "requires_admin_approval": True,
                            "reconciliation": {"last_error": self.engine.last_error},
                        }
                    if self.listener is not None:
                        value["telegram"] = self.listener.status()
                case "/balance":
                    value = asdict(await self.engine.exchange.balance())
                case "/positions":
                    value = await self.engine.owned_positions()
                case "/allocation":
                    value = await self.engine.allocator.summary()
                case "/limits":
                    value = (await self.engine.allocator.limits()).model_dump(mode="json")
                case "/pnl":
                    value = await self.engine.reporter.pnl()
                case "/daily":
                    value = await self.engine.reporter.daily()
                    try:
                        value.update(await self.engine.reporter.account_snapshot())
                    except (ExchangeError, RiskRejected):
                        value["current_balance"] = None
                case "/history":
                    value = await self.engine.history()
                case (
                    "/setslots"
                    | "/setdailycap"
                    | "/setriskcap"
                    | "/setreserve"
                    | "/setmaxpositions"
                    | "/setspread"
                    | "/setdeviation"
                    | "/setslippage"
                    | "/setstopwarning"
                    | "/setliqbuffer"
                    | "/setautorebalance"
                    | "/rebalance"
                ):
                    fields = {
                        "/setslots": "allocation_slots",
                        "/setdailycap": "max_daily_loss_percent",
                        "/setriskcap": "max_portfolio_risk_percent",
                        "/setreserve": "minimum_free_margin",
                        "/setmaxpositions": "max_open_positions",
                        "/setspread": "max_bid_ask_spread_percent",
                        "/setdeviation": "max_entry_price_deviation_percent",
                        "/setslippage": "max_order_slippage_percent",
                        "/setstopwarning": "unusual_stop_distance_percent",
                        "/setliqbuffer": "liquidation_buffer_percent",
                        "/setautorebalance": "auto_rebalance",
                        "/rebalance": "rebalance",
                    }
                    if len(parts) != (1 if cmd == "/rebalance" else 2):
                        return "Kullanım: " + cmd + ("" if cmd == "/rebalance" else " <değer>")
                    value = await self.engine.request_change(
                        fields[cmd],
                        parts[1] if len(parts) == 2 else None,
                        actor=f"telegram:{self.settings.telegram_admin_id}",
                    )
                    return format_command("change_preview", value)
                case "/confirm":
                    if len(parts) != 2:
                        return "Kullanım: /confirm <onay-kodu>"
                    await self.engine.confirm_change(
                        parts[1], actor=f"telegram:{self.settings.telegram_admin_id}"
                    )
                    return "Ayar değişikliği onaylandı ve kalıcı olarak kaydedildi."
                case "/emergency":
                    async with self.engine.lock:
                        await self.engine.trip(
                            "Administrator emergency lock; no position intervention"
                        )
                    return (
                        "Acil durum kilidi etkin. Yeni işlemler kapalı; "
                        "pozisyon müdahalesi için ayrı /close komutu gerekir."
                    )
                case "/trades":
                    value = await self.engine.trades()
                case "/stats":
                    value = await self.engine.stats()
                case "/pause":
                    await self.engine.pause(notify=False)
                    return format_command("pause")
                case "/resume":
                    actor = f"telegram:{self.settings.telegram_admin_id}"
                    if len(parts) == 1:
                        result = await self.engine.request_resume(actor=actor)
                        if result["status"] == "already_running":
                            return format_command("already_running")
                        return format_command("resume_preview", result)
                    if len(parts) != 3 or parts[1] != "CONFIRM":
                        return format_command("resume_usage")
                    await self.engine.resume(confirmation=parts[2], actor=actor)
                    return format_command("resumed")
                case "/close":
                    if len(parts) != 2:
                        return format_command("close_usage")
                    await self.engine.close(parts[1].upper())
                    return format_command("closed")
                case _:
                    return format_command("unknown")
            return format_command(cmd, value)
        except (RiskRejected, ExchangeError) as exc:
            return format_command("rejected", exc)
        except Exception as exc:
            logging.getLogger(__name__).error(
                "admin_command_failed",
                extra={
                    "context": failure_details(exc, "telegram_admin"),
                },
            )
            return format_command("error")

    async def poll(self):
        try:
            while True:
                try:
                    try:
                        state = await self.engine.state()
                        self._memory_offset = max(self._memory_offset, state.bot_update_offset)
                    except SQLAlchemyError:
                        self.engine.healthy = False
                    updates = await self.call(
                        "getUpdates",
                        {
                            "offset": self._memory_offset,
                            "timeout": 25,
                            "allowed_updates": ["message"],
                        },
                    )
                    for update in updates:
                        reply = await self.command(update)
                        if reply:
                            await self.send(reply)
                        self._memory_offset = update["update_id"] + 1
                        async with self.engine.db.sessions.begin() as session:
                            saved = await session.get(RuntimeState, 1)
                            saved.bot_update_offset = update["update_id"] + 1
                except (httpx.HTTPError, RuntimeError, ValueError, SQLAlchemyError):
                    await asyncio.sleep(5)
        finally:
            await self.http.aclose()

    async def deliver_pending(self):
        async with self.engine.db.sessions() as session:
            events = (
                await session.scalars(
                    select(BotEvent)
                    .where(BotEvent.delivered_at.is_(None), BotEvent.delivery_state == "pending")
                    .order_by(BotEvent.id)
                    .limit(20)
                )
            ).all()
        for event in events:
            # Receipt and analysis produce one user summary. Protection is included
            # in order_opened; confirmation previews already have command responses.
            visible = event.details.get("notify", True) and event.kind not in {
                "signal_received",
                "resume_requested",
            }
            if visible:
                async with self.engine.db.sessions.begin() as session:
                    saved = await session.get(BotEvent, event.id, with_for_update=True)
                    if saved.delivery_state != "pending" or saved.delivered_at is not None:
                        continue
                    saved.delivery_state = "sending"
                try:
                    result = await self.send(format_notification(event.kind, event.details))
                except (httpx.ConnectError, httpx.ConnectTimeout, httpx.PoolTimeout):
                    async with self.engine.db.sessions.begin() as session:
                        saved = await session.get(BotEvent, event.id)
                        saved.delivery_state = "pending"
                    raise
                except BaseException:
                    # Bot API has no idempotency key. A lost ACK must never cause
                    # blind duplicate delivery after a restart. Expose it in /status.
                    async with self.engine.db.sessions.begin() as session:
                        saved = await session.get(BotEvent, event.id)
                        saved.delivery_state = "uncertain"
                    raise
            async with self.engine.db.sessions.begin() as session:
                saved = await session.get(BotEvent, event.id)
                saved.delivered_at = utcnow()
                saved.delivery_state = "delivered"
                if visible and isinstance(result, dict):
                    saved.telegram_message_id = result.get("message_id")
            if visible:
                await asyncio.sleep(1)

    async def notifications(self):
        while True:
            try:
                if time.monotonic() >= self._next_report_check and self.engine.healthy:
                    await self.engine.reporter.queue_daily()
                    self._next_report_check = time.monotonic() + 60
                await self.deliver_pending()
            except Exception as exc:
                # Sending or storing a notification cannot mutate an order state.
                logging.getLogger(__name__).error(
                    "notification_delivery_failed",
                    extra={"context": failure_details(exc, "telegram_notification")},
                )
                await asyncio.sleep(5)
            await asyncio.sleep(1)
