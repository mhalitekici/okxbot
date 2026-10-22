from datetime import timedelta

import httpx
import pytest
from pydantic import ValidationError

from okx_bot.app import create_app
from okx_bot.config import Settings
from okx_bot.domain import utcnow
from okx_bot.telegram.admin import AdminBot
from tests.fakes import update


@pytest.mark.parametrize(
    "fields",
    [
        {"okx_demo": False},
        {"okx_demo": "0"},
        {"live_trading_enabled": True},
        {"live_trading_enabled": "true"},
        {"max_account_risk_percent": 0},
        {"max_entry_price_deviation_percent": -1},
        {"okx_base_url": "https://attacker.test"},
    ],
)
def test_unsafe_config_is_rejected(fields):
    with pytest.raises(ValidationError):
        Settings(_env_file=None, **fields)


def test_example_configuration_parses_with_demo_defaults():
    settings = Settings(_env_file=".env.example")
    assert settings.okx_demo is True
    assert settings.live_trading_enabled is False
    assert settings.max_open_positions == 3
    with pytest.raises(ValueError, match="Missing runtime"):
        settings.validate_runtime()


async def test_http_admin_authentication_and_pause(engine, settings):
    app = create_app(settings)
    app.state.engine = engine
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        assert (await client.get("/health/live")).status_code == 200
        assert (await client.get("/health/ready")).status_code == 503
        assert (await client.get("/admin/status")).status_code == 401
        assert (
            await client.post("/admin/pause", headers={"Authorization": "Bearer wrong"})
        ).status_code == 401
        response = await client.post(
            "/admin/pause", headers={"Authorization": "Bearer " + "x" * 40}
        )
        assert response.status_code == 200
        assert response.json()["paused"] is True


async def test_telegram_admin_id_and_private_chat_are_required(engine, settings):
    bot = AdminBot(settings, engine)
    try:
        assert await bot.command(update(admin_id=999, command="/pause")) is None
        assert await bot.command(update(command="/pause", chat={"id": 42, "type": "group"})) is None
        assert not (await engine.state()).paused
        assert await bot.command(update(command="/pause")) == "Yeni işlemler durduruldu."
        assert (await engine.state()).paused
        assert "İşlem modu: Demo" in (await bot.command(update(command="/status")))
    finally:
        await bot.http.aclose()


async def test_old_mutating_admin_command_is_ignored(engine, settings):
    bot = AdminBot(settings, engine)
    try:
        old = int((utcnow() - timedelta(minutes=2)).timestamp())
        assert "süresi doldu" in await bot.command(update(command="/pause", date=old))
        assert not (await engine.state()).paused
    finally:
        await bot.http.aclose()


async def test_fatal_background_failure_cannot_be_resumed(engine):
    engine.fatal = True
    await engine.reconcile()
    assert not engine.healthy
    with pytest.raises(ValueError, match="restart"):
        await engine.request_resume(actor="test_admin")


async def test_api_resume_requires_two_authenticated_requests(engine, settings):
    await engine.trip("Past failure")
    app = create_app(settings)
    app.state.engine = engine
    headers = {"Authorization": "Bearer " + "x" * 40}
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        assert (await client.post("/admin/resume")).status_code == 401
        preview = await client.post("/admin/resume", headers=headers)
        assert preview.status_code == 200
        assert preview.json()["status"] == "confirmation_required"
        assert (await engine.state()).emergency
        assert (
            await client.post("/admin/resume", headers=headers, json={"confirm": True})
        ).status_code == 422
        response = await client.post(
            "/admin/resume",
            headers=headers,
            json={
                "confirmation": preview.json()["confirmation"],
            },
        )
        assert response.status_code == 200
        assert response.json()["trading_enabled"]
        replay = await client.post(
            "/admin/resume",
            headers=headers,
            json={
                "confirmation": preview.json()["confirmation"],
            },
        )
        assert replay.status_code == 409


async def test_telegram_resume_demands_explicit_confirm_by_admin(engine, settings, exchange):
    await engine.trip("Past failure")
    bot = AdminBot(settings, engine)
    try:
        preview = await bot.command(update(command="/resume"))
        confirmation = next(
            line for line in preview.splitlines() if line.startswith("/resume CONFIRM")
        )
        assert (await engine.state()).emergency
        assert await bot.command(update(admin_id=999, command=confirmation)) is None
        assert (await engine.state()).emergency
        assert "yönetici onayıyla" in await bot.command(update(command=confirmation))
        assert not (await engine.state()).paused
        assert not exchange.submissions
    finally:
        await bot.http.aclose()


def test_importing_asgi_module_does_not_read_env(monkeypatch):
    import importlib

    module = importlib.import_module("okx_bot.app")

    def unexpected():
        raise AssertionError("Settings must be resolved at lifespan startup")

    monkeypatch.setattr(module, "Settings", unexpected)
    assert module.create_app() is not None
