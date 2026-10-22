import asyncio
import hmac
import logging
import os
from contextlib import asynccontextmanager
from dataclasses import asdict

from fastapi import Depends, FastAPI, HTTPException
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError

from okx_bot.config import Settings
from okx_bot.db import Database
from okx_bot.diagnostics import failure_details
from okx_bot.domain import ExchangeError, RiskRejected, utcnow
from okx_bot.engine import TradingEngine
from okx_bot.exchange.client import OKXClient
from okx_bot.exchange.gateway import OKXGateway
from okx_bot.logging import configure_logging
from okx_bot.telegram.admin import AdminBot
from okx_bot.telegram.listener import TelegramListener

logger = logging.getLogger(__name__)


class ResumeRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    confirmation: str | None = Field(default=None, min_length=16, max_length=128)


def create_app(settings: Settings | None = None):
    # Importing the ASGI module must not read credentials or a developer's .env.
    configure_logging(settings.log_level if settings else "INFO")

    @asynccontextmanager
    async def lifespan(app):
        nonlocal settings
        settings = settings or Settings()
        configure_logging(settings.log_level)
        settings.validate_runtime()
        logger.info(
            "Trading bot starting",
            extra={
                "context": {
                    "mode": "demo",
                    "live_enabled": False,
                    "telegram_channel_id": settings.telegram_channel_id,
                }
            },
        )
        database = Database(settings.database_url)
        client = OKXClient(settings)
        engine = TradingEngine(settings, database, OKXGateway(client))
        app.state.engine, app.state.database = engine, database
        app.state.fatal = False
        try:
            async with database.singleton() as lease:
                await database.initialize_state()
                # A process cannot prove what happened during its downtime. Require
                # an explicit resume after startup, while still reconciling protection.
                await engine.pause(notify=False)
                try:
                    await engine.reconcile(startup=True)
                except Exception:
                    # Reconciliation already latched and sanitized the failure. Keep
                    # administration available while the reconciler retries.
                    logger.warning("Starting with trading locked; exchange reconciliation pending")
                listener = TelegramListener(settings, engine)
                admin = AdminBot(settings, engine, listener)
                app.state.listener = listener

                async def reconciliation_loop():
                    while True:
                        await asyncio.sleep(settings.reconciliation_interval_seconds)
                        try:
                            await engine.reconcile()
                        except Exception:
                            # reconcile() already persisted/logged a sanitized, staged failure.
                            # Keep monitoring after DB, API, and malformed-snapshot errors.
                            continue

                async def lease_watchdog():
                    while True:
                        await asyncio.sleep(5)
                        # SQLAlchemy must never silently reacquire a session-level lock.
                        if lease.invalidated:
                            raise RuntimeError("Database singleton connection lost")
                        for attempt in range(3):
                            try:
                                await lease.execute(text("SELECT 1"))
                                break
                            except SQLAlchemyError:
                                engine.healthy = False
                                if lease.invalidated or attempt == 2:
                                    raise
                                await asyncio.sleep(1)

                async def listener_loop():
                    while True:
                        try:
                            await listener.run()
                        except Exception as exc:
                            logger.error(
                                "telegram_listener_retry",
                                extra={"context": failure_details(exc, "telegram_listener")},
                            )
                            try:
                                await engine.trip("Telegram disconnected; admin approval required")
                            except SQLAlchemyError:
                                engine.healthy = False
                        await asyncio.sleep(5)

                async def guarded(name, operation):
                    try:
                        logger.info("Background task started", extra={"context": {"task": name}})
                        await operation
                    except asyncio.CancelledError:
                        raise
                    except Exception as exc:
                        app.state.fatal = True
                        engine.fatal = True
                        engine.healthy = False
                        logger.error(
                            "Background task stopped",
                            extra={
                                "context": {
                                    "task": name,
                                    **failure_details(exc, "background_task"),
                                }
                            },
                        )
                        if name == "singleton_lease":
                            # Prevent further exchange requests if database ownership is lost.
                            os._exit(1)
                        await engine.trip(f"Required background task stopped: {name}")

                jobs = [
                    ("telegram_listener", listener_loop()),
                    ("admin_bot", admin.poll()),
                    ("notifications", admin.notifications()),
                    ("reconciler", reconciliation_loop()),
                    ("singleton_lease", lease_watchdog()),
                ]
                tasks = [
                    asyncio.create_task(guarded(name, operation), name=name)
                    for name, operation in jobs
                ]
                try:
                    yield
                finally:
                    engine.healthy = False
                    for task in tasks:
                        task.cancel()
                    await asyncio.gather(*tasks, return_exceptions=True)
        except Exception as exc:
            logger.error(
                "service_lifecycle_failed",
                extra={
                    "context": failure_details(exc, "service_lifecycle"),
                },
            )
            # Uvicorn may format startup exceptions itself. Forward a safe summary.
            raise RuntimeError("Service lifecycle failed; inspect structured diagnostics") from None
        finally:
            await client.close()
            await database.engine.dispose()

    app = FastAPI(title="Telegram → OKX Demo", lifespan=lifespan, docs_url=None, redoc_url=None)
    bearer = HTTPBearer(auto_error=False)

    credentials_dependency = Depends(bearer)

    async def authorize(credentials: HTTPAuthorizationCredentials | None = credentials_dependency):
        token = settings.admin_api_token.get_secret_value() if settings else ""
        if (
            not token
            or credentials is None
            or not hmac.compare_digest(credentials.credentials.encode(), token.encode())
        ):
            raise HTTPException(401, "Unauthorized")

    @app.get("/health/live")
    async def live():
        if getattr(app.state, "fatal", False):
            raise HTTPException(503, "Required background task failed")
        return {"status": "alive", "mode": "demo"}

    @app.get("/health/ready")
    async def ready():
        engine = getattr(app.state, "engine", None)
        listener = getattr(app.state, "listener", None)
        if (
            getattr(app.state, "fatal", False)
            or engine is None
            or not engine.healthy
            or listener is None
            or not listener.ready
            or engine.last_sync is None
            or (utcnow() - engine.last_sync).total_seconds() > 120
        ):
            raise HTTPException(503, "Dependencies unavailable or synchronization stale")
        try:
            async with app.state.database.sessions() as session:
                await session.execute(text("SELECT 1"))
            state = await engine.state()
        except SQLAlchemyError as exc:
            raise HTTPException(503, "Database unavailable") from exc
        return {
            "status": "ready",
            "mode": "demo",
            "trading_enabled": not state.paused
            and not state.emergency
            and not engine.fatal
            and not (state.last_reconciliation_summary or {}).get("blockers"),
            "requires_admin_approval": state.paused or state.emergency,
        }

    @app.get("/health/subsystems", dependencies=[Depends(authorize)])
    async def subsystems():
        try:
            async with app.state.database.sessions() as session:
                await session.execute(text("SELECT 1"))
            database_ok = True
        except SQLAlchemyError:
            database_ok = False
        engine = getattr(app.state, "engine", None)
        return {
            "database": database_ok,
            "telegram": listener_status(),
            "okx": bool(engine and engine.healthy),
            "fatal": getattr(app.state, "fatal", False),
        }

    async def guarded_action(action):
        try:
            return await action
        except RiskRejected as exc:
            raise HTTPException(409, str(exc)) from exc
        except ExchangeError as exc:
            raise HTTPException(502, str(exc)) from exc
        except Exception as exc:
            logger.error(
                "admin_action_failed",
                extra={
                    "context": failure_details(exc, "admin_action"),
                },
            )
            raise HTTPException(
                503, "Operation failed; inspect reconciliation diagnostics"
            ) from exc

    @app.get("/admin/status", dependencies=[Depends(authorize)])
    async def status():
        result = await app.state.engine.status()
        result["telegram"] = listener_status()
        return result

    def listener_status():
        listener = getattr(app.state, "listener", None)
        if listener is None or not hasattr(listener, "status"):
            return {"ready": False, "state": "not_started"}
        return listener.status()

    @app.get("/admin/telegram", dependencies=[Depends(authorize)])
    async def telegram_status():
        return listener_status()

    @app.get("/admin/balance", dependencies=[Depends(authorize)])
    async def balance():
        return asdict(await guarded_action(app.state.engine.exchange.balance()))

    @app.get("/admin/positions", dependencies=[Depends(authorize)])
    async def positions():
        return await guarded_action(app.state.engine.owned_positions())

    @app.get("/admin/allocation", dependencies=[Depends(authorize)])
    async def allocation():
        return await guarded_action(app.state.engine.allocator.summary())

    @app.get("/admin/pnl", dependencies=[Depends(authorize)])
    async def pnl():
        return await guarded_action(app.state.engine.reporter.pnl())

    @app.get("/admin/trades", dependencies=[Depends(authorize)])
    async def trades():
        return await app.state.engine.trades()

    @app.get("/admin/stats", dependencies=[Depends(authorize)])
    async def stats():
        return await app.state.engine.stats()

    @app.post("/admin/pause", dependencies=[Depends(authorize)])
    async def pause():
        await app.state.engine.pause()
        return await app.state.engine.status()

    @app.post("/admin/resume", dependencies=[Depends(authorize)])
    async def resume(payload: ResumeRequest | None = None):
        if payload is None or payload.confirmation is None:
            return await guarded_action(app.state.engine.request_resume(actor="api_admin"))
        await guarded_action(
            app.state.engine.resume(
                confirmation=payload.confirmation,
                actor="api_admin",
            )
        )
        return await app.state.engine.status()

    @app.post("/admin/positions/{inst_id}/close", dependencies=[Depends(authorize)])
    async def close(inst_id: str):
        await guarded_action(app.state.engine.close(inst_id))
        return {"status": "closed"}

    return app


app = create_app()
