"""Read-only evidence without engine ingestion, reconciliation or notification delivery."""

import asyncio
import json
from types import SimpleNamespace

import httpx
from sqlalchemy import func, select, text

from okx_bot.config import Settings
from okx_bot.db import Database
from okx_bot.diagnostics import failure_details
from okx_bot.domain import utcnow
from okx_bot.exchange.client import ReadOnlyOKXClient
from okx_bot.exchange.gateway import OKXGateway
from okx_bot.logging import configure_logging
from okx_bot.models import BotEvent, Execution, Position, RuntimeState, TelegramSignal, TradeOrder
from okx_bot.telegram.admin import AdminBot
from okx_bot.telegram.diagnostics import inspect_telegram


async def collect(settings: Settings) -> dict:
    report = {
        "checked_at": utcnow().isoformat(),
        "read_only": True,
        "orders_sent": 0,
        "messages_sent": 0,
        "state_changed": False,
        "demo": settings.okx_demo,
        "live_enabled": settings.live_trading_enabled,
        "configured_source": {
            "channel_id": settings.telegram_channel_id,
            "topic_id": settings.telegram_topic_id,
        },
        "real_order_lifecycle_tested": False,
    }
    db = Database(settings.database_url)
    client = ReadOnlyOKXClient(settings)
    gateway = OKXGateway(client)
    try:
        try:
            async with db.sessions() as session:
                await session.execute(text("SET TRANSACTION READ ONLY"))
                await session.execute(text("SELECT 1"))
                state = await session.get(RuntimeState, 1)
                report["database"] = {
                    "ok": True,
                    "migration": await session.scalar(
                        text("SELECT version_num FROM alembic_version")
                    ),
                    "paused": state.paused,
                    "emergency": state.emergency,
                    "state_revision": state.state_revision,
                    "last_reconciliation_ok": state.last_reconciliation_ok,
                    "counts": {
                        model.__tablename__: await session.scalar(
                            select(func.count()).select_from(model)
                        )
                        for model in (TelegramSignal, TradeOrder, Position, Execution, BotEvent)
                    },
                    "undelivered_events": await session.scalar(
                        select(func.count(BotEvent.id)).where(BotEvent.delivered_at.is_(None))
                    ),
                }
        except Exception as exc:
            report["database"] = {"ok": False, **failure_details(exc, "acceptance_database")}

        report["worker"] = {"available": False}
        async with httpx.AsyncClient(timeout=5, trust_env=False, follow_redirects=False) as http:
            for base in ("http://trading-bot:8000", "http://127.0.0.1:8000"):
                try:
                    headers = {
                        "Authorization": "Bearer " + settings.admin_api_token.get_secret_value()
                    }
                    response = await http.get(base + "/admin/status", headers=headers)
                    if response.status_code != 200:
                        continue
                    status = response.json()
                    ready = await http.get(base + "/health/ready")
                    report["worker"] = {
                        "available": True,
                        "ready": ready.status_code == 200,
                        **{
                            k: status.get(k)
                            for k in (
                                "mode",
                                "paused",
                                "emergency",
                                "trading_enabled",
                                "exchange_healthy",
                            )
                        },
                        "telegram": {
                            k: status.get("telegram", {}).get(k)
                            for k in (
                                "channel_id",
                                "topic_id",
                                "topic_verified",
                                "ready",
                                "connected",
                                "authorized",
                            )
                        },
                    }

                    async def remote_status(snapshot=status):
                        return snapshot

                    # Execute /status locally against the actual worker snapshot. Never send it.
                    bot = AdminBot(settings, SimpleNamespace(status=remote_status))
                    try:
                        reply = await bot.command(
                            {
                                "message": {
                                    "from": {"id": settings.telegram_admin_id},
                                    "chat": {"id": settings.telegram_admin_id, "type": "private"},
                                    "text": "/status",
                                }
                            }
                        )
                        report["admin_command"] = {
                            "rendered_without_delivery": True,
                            "turkish": bool(reply and "OKX İŞLEM BOTU DURUMU" in reply),
                        }
                    finally:
                        await bot.http.aclose()
                    break
                except (httpx.HTTPError, ValueError):
                    continue
            try:
                # getMe is read-only. Never call sendMessage/getUpdates from this probe.
                token = settings.telegram_bot_token.get_secret_value()
                response = await http.get(f"https://api.telegram.org/bot{token}/getMe")
                data = response.json() if response.status_code == 200 else {}
                report["admin_bot"] = {
                    "authenticated": bool(data.get("ok")),
                    "expected_username": str(data.get("result", {}).get("username", "")).lower()
                    == "okxrizikbot",
                    "message_sent": False,
                }
            except Exception as exc:
                report["admin_bot"] = {
                    "authenticated": False,
                    **failure_details(exc, "bot_identity"),
                }

        report["telegram"] = await inspect_telegram(settings, message_ids=[23576, 32877, 32876])
        report["okx"] = {}
        instruments = []
        for name, operation in (
            ("account_config", gateway.account_config),
            ("instruments", gateway.instruments),
            ("balance", gateway.balance),
            ("positions", gateway.positions),
            ("pending_orders", gateway.open_orders),
            ("pending_algos", gateway.open_algo_orders),
        ):
            try:
                value = await operation()
                item = {"ok": True}
                if name == "account_config":
                    item.update({k: value[k] for k in ("acctLv", "posMode")})
                elif name == "balance":
                    item.update(
                        positive_equity=value.equity > 0, positive_available=value.available > 0
                    )
                else:
                    item["count"] = len(value)
                if name == "instruments":
                    instruments = value
                    item["tradable_usdt_swaps"] = sum(
                        r.get("state") == "live" and r.get("settleCcy") == "USDT" for r in value
                    )
                report["okx"][name] = item
            except Exception as exc:
                report["okx"][name] = {"ok": False, **failure_details(exc, name)}

        if report["okx"].get("instruments", {}).get("ok"):
            for item in report["telegram"].get("messages", []):
                if not item.get("coin"):
                    continue
                inst_id = f"{item['coin'][:-4]}-USDT-SWAP"
                matches = [r for r in instruments if r.get("instId") == inst_id]
                item["demo_instrument_status"] = (
                    "absent_from_account_list"
                    if not matches
                    else "tradable"
                    if len(matches) == 1 and matches[0].get("state") == "live"
                    else "not_tradable"
                )
                item["age_within_limit"] = (
                    -5 <= item["age_seconds"] <= settings.max_signal_age_seconds
                )
                item["order_attempted"] = False
            try:
                instrument = await gateway.instrument("BTC-USDT-SWAP")
                quote = await gateway.quote(instrument.inst_id)
                fee, tiers = await gateway.risk_metadata(instrument)
                report["okx"]["risk_inputs"] = {
                    "ok": bool(tiers) and fee >= 0,
                    "bid_ask_valid": True,
                    "spread_percent": str(quote.spread_percent),
                    "tick": str(instrument.tick),
                    "fee_available": fee >= 0,
                    "position_tiers": len(tiers),
                }
                leverage = await client.request(
                    "GET",
                    "/api/v5/account/leverage-info",
                    params={
                        "instId": instrument.inst_id,
                        "mgnMode": "isolated",
                    },
                )
                report["okx"]["leverage_read_only"] = {
                    "ok": True,
                    "ten_x_configured_for_sample": bool(leverage)
                    and all(r.get("lever") == "10" for r in leverage),
                    "leverage_changed": False,
                }
            except Exception as exc:
                report["okx"]["risk_inputs"] = {"ok": False, **failure_details(exc, "risk_inputs")}
        report["okx_read_requests"] = client.read_count
    finally:
        await client.close()
        await db.engine.dispose()
    return report


async def main():
    configure_logging("ERROR")
    try:
        report = await collect(Settings())
    except Exception as exc:
        report = {"read_only": True, "error": failure_details(exc, "acceptance_probe")}
    print(json.dumps(report, ensure_ascii=False, default=str, indent=2))


def run():
    asyncio.run(main())
