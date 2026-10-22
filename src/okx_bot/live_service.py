"""Deployment entry point for the permanently paused Live observer."""

import argparse
import asyncio
import hmac
import logging
import os
from contextlib import asynccontextmanager
from logging.handlers import RotatingFileHandler
from pathlib import Path
from types import SimpleNamespace

from fastapi import Depends, FastAPI, HTTPException
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import SecretStr
from sqlalchemy import select, text

from okx_bot.db import Database
from okx_bot.domain import utcnow
from okx_bot.live_connection import LiveReadOnlyClient, load_live_config
from okx_bot.live_deployment import (
    AccountIdentity,
    DeploymentError,
    LiveDeployment,
    TelegramSecrets,
    enroll_identity,
    live_database_url,
)
from okx_bot.live_observer import LiveObserver, read_snapshot, require_schema
from okx_bot.models import BotEvent
from okx_bot.telegram.admin import AdminBot
from okx_bot.telegram.listener import TelegramListener

LIVE_CONFIG = Path("/app/config/live.toml")
LIVE_ENV = Path("/run/secrets/okx_live_env")
IDENTITY = Path("/app/live-identity/account.json")
TELEGRAM_ENV = Path("/run/secrets/telegram_live_env")
SESSION = Path("/app/live-data/telegram.session-string")


def report_text(status):
    snapshot = status.get("snapshot") or {}
    allocation = status["allocation"]
    return "\n".join(
        [
            "LIVE GÖZLEMCİ — Emir gönderme: DISABLED; paused=true",
            f"Bağlantı güncel: {status['exchange_healthy']}; acil kilit: {status['emergency']}",
            f"Özkaynak: {snapshot.get('equity_usdt', 'N/A')} USDT",
            f"Kullanılabilir: {snapshot.get('available_usdt', 'N/A')} USDT",
            f"Pozisyon: {snapshot.get('positions', 'N/A')}",
            f"Bekleyen emir: {snapshot.get('pending_orders', 'N/A')}",
            f"Bekleyen conditional/OCO: {snapshot.get('pending_tpsl', 'N/A')}",
            f"3 slot; slot hedefi: {allocation['slot_margin_usdt'] or 'N/A'} USDT",
            f"Kaydedilen sinyal: {status['signals_recorded']}",
            "Gerçekleşmiş bot PnL: 0 USDT (bu sürüm Live işlem üretmez).",
            "Hesaptaki bot dışı pozisyon ve emirler yönetilmez.",
        ]
    )


class LiveAdmin(AdminBot):
    async def call(self, method, payload):
        try:
            return await super().call(method, payload)
        except Exception:
            await self.engine.trip("TELEGRAM_DISCONNECTED")
            raise RuntimeError("Telegram read/report connection unavailable") from None

    async def command(self, update):
        message = update.get("message", {})
        sender, chat = message.get("from", {}), message.get("chat", {})
        if (
            sender.get("id") != self.settings.telegram_admin_id
            or sender.get("is_bot")
            or chat.get("id") != self.settings.telegram_admin_id
            or chat.get("type") != "private"
        ):
            return None
        parts = message.get("text", "").split()
        if not parts:
            return None
        cmd = parts[0].split("@")[0]
        if cmd in {"/pause", "/emergency"}:
            if not -5 <= utcnow().timestamp() - message.get("date", 0) <= 60:
                return "Komut süresi doldu."
            if cmd == "/emergency":
                await self.engine.trip()
            else:
                await self.engine.pause()
        elif cmd not in {
            "/start",
            "/status",
            "/balance",
            "/allocation",
            "/positions",
            "/pnl",
            "/daily",
            "/trades",
            "/history",
            "/limits",
        }:
            return "EXECUTION_DISABLED: Live gözlemcide /resume, /confirm, /close ve ayar açma yok."
        return report_text(await self.engine.status())

    async def deliver_pending(self):
        async with self.engine.db.sessions() as session:
            events = list(
                (
                    await session.scalars(
                        select(BotEvent)
                        .where(
                            BotEvent.kind == "live_observer_daily",
                            BotEvent.delivery_state == "pending",
                        )
                        .order_by(BotEvent.id)
                        .limit(5)
                    )
                ).all()
            )
        for event in events:
            async with self.engine.db.sessions.begin() as session:
                saved = await session.get(BotEvent, event.id, with_for_update=True)
                saved.delivery_state = "sending"
            try:
                await self.send(report_text(await self.engine.status()))
            except BaseException:
                async with self.engine.db.sessions.begin() as session:
                    saved = await session.get(BotEvent, event.id)
                    saved.delivery_state = "uncertain"
                raise
            async with self.engine.db.sessions.begin() as session:
                saved = await session.get(BotEvent, event.id)
                saved.delivery_state = "delivered"
                saved.delivered_at = utcnow()

    async def notifications(self):
        while True:
            try:
                if self.engine.healthy:
                    await self.engine.queue_daily()
                    await self.deliver_pending()
            except Exception:
                await self.engine.trip("TELEGRAM_DISCONNECTED")
            await asyncio.sleep(60)


def create_live_app(deployment, credentials, config, identity, database, *, transport=None):
    @asynccontextmanager
    async def lifespan(app):
        client = LiveReadOnlyClient(config, transport)
        try:
            # Stop before initializing a DB or Telegram session if identity is unverified.
            await read_snapshot(client, identity)
            await require_schema(database)
            async with database.singleton() as lease:
                observer = LiveObserver(deployment, database, client, identity)
                await observer.initialize()
                await observer.refresh()
                runtime = SimpleNamespace(
                    **deployment.model_dump(),
                    **credentials.model_dump(),
                    telegram_session_file=SESSION,
                    telegram_session_string=SecretStr(""),
                    telegram_scan_interval_seconds=10,
                    max_signal_age_seconds=60,
                )
                listener = TelegramListener(runtime, observer)
                admin = LiveAdmin(runtime, observer, listener)
                app.state.observer, app.state.listener = observer, listener

                async def readings():
                    while True:
                        await asyncio.sleep(deployment.poll_seconds)
                        try:
                            await observer.refresh()
                        except DeploymentError:
                            pass  # emergency remains latched after reconnection

                async def listen():
                    while True:
                        try:
                            await listener.run()
                        except Exception:
                            await observer.trip("TELEGRAM_DISCONNECTED")
                        await asyncio.sleep(5)

                async def watchdog():
                    while True:
                        await asyncio.sleep(5)
                        if lease.invalidated:
                            os._exit(1)
                        try:
                            await lease.execute(text("SELECT 1"))
                        except Exception:
                            os._exit(1)  # no silent reacquisition of an advisory lock
                        if not listener.ready:
                            await observer.trip("TELEGRAM_DISCONNECTED")

                async def supervised(operation):
                    try:
                        await operation
                    except asyncio.CancelledError:
                        raise
                    except Exception:
                        observer.fatal, observer.healthy = True, False
                        await observer.trip()

                tasks = [
                    asyncio.create_task(supervised(operation))
                    for operation in (
                        readings(),
                        listen(),
                        admin.poll(),
                        admin.notifications(),
                        watchdog(),
                    )
                ]
                try:
                    yield
                finally:
                    observer.healthy = False
                    for task in tasks:
                        task.cancel()
                    await asyncio.gather(*tasks, return_exceptions=True)
        except DeploymentError as error:
            raise RuntimeError(
                f"Live observer startup blocked: {error.code}; execution DISABLED"
            ) from None
        except Exception:
            raise RuntimeError("Live observer startup blocked; execution stays disabled") from None
        finally:
            await client.close()
            await database.engine.dispose()

    app = FastAPI(title="OKX Live observer", lifespan=lifespan, docs_url=None, redoc_url=None)
    bearer = HTTPBearer(auto_error=False)
    bearer_dependency = Depends(bearer)

    async def auth(value: HTTPAuthorizationCredentials | None = bearer_dependency):
        if value is None or not hmac.compare_digest(
            value.credentials.encode(), credentials.admin_api_token.get_secret_value().encode()
        ):
            raise HTTPException(401, "Unauthorized")

    @app.get("/health/live")
    async def alive():
        observer = getattr(app.state, "observer", None)
        if observer and observer.fatal:
            raise HTTPException(503, "Observer failed")
        return {"mode": "live-observer", "paused": True, "order_execution": "DISABLED"}

    @app.get("/health/ready")
    async def ready():
        observer = getattr(app.state, "observer", None)
        if (
            observer is None
            or not observer.healthy
            or observer.fatal
            or not app.state.listener.ready
            or observer.last_sync is None
            or (utcnow() - observer.last_sync).total_seconds() > 60
        ):
            raise HTTPException(503, "Observation stale or dependencies unavailable")
        try:
            async with database.sessions() as session:
                await session.execute(text("SELECT 1"))
        except Exception:
            raise HTTPException(503, "Database unavailable") from None
        return {"mode": "live-observer", "paused": True, "order_execution": "DISABLED"}

    @app.get("/admin/status", dependencies=[Depends(auth)])
    @app.get("/admin/balance", dependencies=[Depends(auth)])
    @app.get("/admin/allocation", dependencies=[Depends(auth)])
    @app.get("/admin/pnl", dependencies=[Depends(auth)])
    async def status():
        try:
            return await app.state.observer.status()
        except Exception:
            raise HTTPException(503, "Observation state unavailable") from None

    @app.api_route(
        "/admin/{action:path}",
        methods=["POST", "PUT", "PATCH", "DELETE"],
        dependencies=[Depends(auth)],
    )
    async def blocked(action: str):
        raise HTTPException(409, "EXECUTION_DISABLED")

    return app


def configure_live_logs():
    logger = logging.getLogger("okx_bot.live_observer")
    logger.setLevel(logging.INFO)
    logger.propagate = False
    # This logger receives only fixed startup/health codes, never exceptions or config objects.
    handler = RotatingFileHandler("/app/live-logs/observer.log", maxBytes=2_000_000, backupCount=3)
    handler.setFormatter(logging.Formatter("%(asctime)s %(message)s"))
    logger.addHandler(handler)
    for name in ("httpx", "httpcore", "telethon"):
        logging.getLogger(name).setLevel(logging.CRITICAL + 1)
    logger.info("LIVE_OBSERVER_PAUSED_EXECUTION_DISABLED")


def main(argv=None):
    parser = argparse.ArgumentParser(description="Paused Live observer; no OKX order executor")
    parser.add_argument("action", choices=["serve", "enroll", "migrate", "identity-help"])
    parser.add_argument("--manual-subaccount", default="")
    parser.add_argument("--confirm-live-database", action="store_true")
    args = parser.parse_args(argv)
    try:
        if args.action == "identity-help":
            print("Verify TelegramBot1 in OKX UI, then enroll --manual-subaccount TelegramBot1.")
        elif args.action == "enroll":
            if args.manual_subaccount != "TelegramBot1":
                raise DeploymentError("MANUAL_SUBACCOUNT_CONFIRMATION_REQUIRED")
            asyncio.run(
                enroll_identity(load_live_config(LIVE_ENV), IDENTITY, args.manual_subaccount)
            )
            print("Live subaccount pin saved; manual name attestation; execution DISABLED.")
        elif args.action == "migrate":
            from okx_bot.live_migration import migrate_live_database

            if not args.confirm_live_database:
                raise DeploymentError("DATABASE_GUARD")
            asyncio.run(migrate_live_database(live_database_url()))
            print("Separate Live schema ready; execution DISABLED.")
        else:
            import uvicorn

            deployment = LiveDeployment.from_file(LIVE_CONFIG)
            credentials = TelegramSecrets.from_file(TELEGRAM_ENV)
            config = load_live_config(LIVE_ENV)
            identity = AccountIdentity.from_file(IDENTITY)
            if not SESSION.is_file():
                raise DeploymentError()
            configure_live_logs()
            app = create_live_app(
                deployment, credentials, config, identity, Database(live_database_url())
            )
            uvicorn.run(
                app, host="0.0.0.0", port=8000, workers=1, access_log=False, log_level="warning"
            )
        return 0
    except DeploymentError as error:
        print(f"LIVE preparation FAILED: {error.code}; execution DISABLED.")
        if error.code in {"IDENTITY_REQUIRED", "MANUAL_SUBACCOUNT_CONFIRMATION_REQUIRED"}:
            print("Stop: verify TelegramBot1 in OKX UI, then perform manual identity enrollment.")
        return 1
    except Exception:
        print("LIVE preparation FAILED: check config, identity and schema; execution DISABLED.")
        return 1
