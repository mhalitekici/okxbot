import asyncio
import json
import logging
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import Mock

import httpx
import pytest
from sqlalchemy import select
from telethon import events, functions, types, utils
from telethon.crypto import AuthKey
from telethon.sessions import StringSession

from okx_bot.app import create_app
from okx_bot.domain import utcnow
from okx_bot.logging import JSONFormatter
from okx_bot.models import BotEvent, RuntimeState, TelegramSignal
from okx_bot.parser import RegexSignalParser
from okx_bot.telegram import listener as listener_module
from okx_bot.telegram.diagnostics import inspect_telegram
from okx_bot.telegram.listener import TelegramListener
from tests.fakes import forum_topic, text
from tests.test_mz_parser import EXAMPLE


class TelegramStub:
    """MTProto test boundary: no network or exchange object exists here."""

    def __init__(self, *, authorized=True, accessible=True, fail_scans=0):
        self.authorized, self.accessible = authorized, accessible
        self.fail_scans = fail_scans
        self.connected = False
        self.connect_count = 0
        self.entity = types.Channel(
            id=456,
            title="SECRET_CHANNEL_TITLE",
            photo=types.ChatPhotoEmpty(),
            date=utcnow(),
            broadcast=True,
            forum=True,
        )
        self.messages, self.handlers = [], []
        self.scanned = asyncio.Event()
        self.options = {}
        self.topics = [forum_topic()]

    def factory(self, *args, **kwargs):
        self.options = kwargs
        return self

    async def connect(self):
        self.connected = True
        self.connect_count += 1

    async def disconnect(self):
        self.connected = False

    def is_connected(self):
        return self.connected

    async def is_user_authorized(self):
        return self.authorized

    async def iter_dialogs(self):
        if self.accessible:
            yield SimpleNamespace(entity=self.entity)

    async def __call__(self, request):
        assert isinstance(request, functions.messages.GetForumTopicsByIDRequest)
        return SimpleNamespace(topics=[t for t in self.topics if t.id in request.topics])

    async def get_messages(self, entity, limit=None, ids=None):
        if ids is not None:
            by_id = {m.id: m for m in self.messages}
            return [by_id.get(identifier) for identifier in ids]
        return sorted(self.messages, key=lambda m: m.id, reverse=True)[:limit]

    async def iter_messages(self, entity, limit, reply_to=None):
        assert reply_to == 23576
        if self.fail_scans:
            self.fail_scans -= 1
            raise OSError("SECRET_PROVIDER_ERROR")
        for message in await self.get_messages(entity, limit):
            yield message
        self.scanned.set()

    def add_event_handler(self, callback, builder):
        self.handlers.append((callback, builder))

    async def emit(self, message, *, edited=False):
        expected = events.MessageEdited if edited else events.NewMessage
        for callback, builder in self.handlers:
            if type(builder) is expected:
                await callback(SimpleNamespace(message=message))


def configure(settings, client):
    settings.telegram_channel_id = utils.get_peer_id(client.entity)
    settings.telegram_api_id = 12345
    settings.telegram_api_hash = "SECRET_API_HASH"
    session = StringSession()
    session.set_dc(2, "149.154.167.51", 443)
    session.auth_key = AuthKey(bytes([7]) * 256)
    settings.telegram_session_string = session.save()


@asynccontextmanager
async def running_listener(settings, engine, monkeypatch, *, fail_scans=0, fast=False):
    client = TelegramStub(fail_scans=fail_scans)
    configure(settings, client)
    # Strategy always stays paused; any accidental execution is a test failure.
    await engine.pause()
    engine._execute = Mock(side_effect=AssertionError("No order execution permitted in this test"))
    selected = (
        settings.model_copy(update={"telegram_scan_interval_seconds": 0}) if fast else settings
    )
    listener = TelegramListener(selected, engine)
    monkeypatch.setattr(listener_module, "TelegramClient", client.factory)
    task = asyncio.create_task(listener.run())
    try:
        await asyncio.wait_for(client.scanned.wait(), timeout=5)
        assert listener.ready
        yield listener, client
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        engine._execute.assert_not_called()


def item(message_id, content=None):
    return SimpleNamespace(
        id=message_id,
        date=utcnow(),
        raw_text=content or text(),
        edit_date=None,
        chat_id=-1000000000456,
        action=None,
        reply_to=types.MessageReplyHeader(forum_topic=True, reply_to_msg_id=23576),
    )


def metadata(caplog, event):
    return [r.context for r in caplog.records if r.getMessage() == event]


def test_channel_id_is_loaded_as_integer_from_dotenv(tmp_path):
    from okx_bot.config import Settings

    env = tmp_path / ".env"
    env.write_text(
        "TELEGRAM_CHANNEL_ID=-1000000000456\nOKX_DEMO=1\nLIVE_TRADING_ENABLED=false\n",
        encoding="utf-8",
    )
    settings = Settings(_env_file=env)
    assert settings.telegram_channel_id == -1000000000456
    assert isinstance(settings.telegram_channel_id, int)
    assert settings.okx_demo and not settings.live_trading_enabled


async def test_startup_registers_handlers_and_logs_connected_subscription(
    settings, engine, monkeypatch, caplog
):
    caplog.set_level(logging.INFO, logger="okx_bot")
    async with running_listener(settings, engine, monkeypatch) as (listener, client):
        assert client.options["auto_reconnect"]
        assert client.options["sequential_updates"]
        assert {type(builder) for _, builder in client.handlers} == {
            events.NewMessage,
            events.MessageEdited,
        }
        assert metadata(caplog, "Telegram connected")[0]["authorized"]
        assert metadata(caplog, "Channel subscribed")[0]["handler_count"] == 2
        assert metadata(caplog, "Telegram listener ready")
        state = listener.status()
        assert state["connected"] and state["authorized"] and state["subscribed"]
        assert state["channel_id"] == settings.telegram_channel_id
        assert state["last_scan_at"]
    assert not listener.ready and not client.connected


async def test_new_message_calls_parser_once_and_logs_result_while_orders_are_disabled(
    settings, engine, exchange, monkeypatch, caplog
):
    caplog.set_level(logging.INFO, logger="okx_bot")
    parser = Mock(wraps=RegexSignalParser())
    engine.parser = parser
    async with running_listener(settings, engine, monkeypatch) as (listener, client):
        message = item(100, text() + "\nSECRET_RAW_MESSAGE")
        await client.emit(message)
        await client.emit(message)
        assert parser.parse.call_count == 1
        outcomes = metadata(caplog, "Signal processed")
        assert len(outcomes) == 1
        assert outcomes[0]["message_id"] == 100
        assert outcomes[0]["coin"] == "BTCUSDT"
        assert outcomes[0]["analysis_result"] == "parsed"
        assert outcomes[0]["trade_status"] == "rejected"
        assert outcomes[0]["reason_code"] == "TRADING_DISABLED"
        assert listener.last_message_id == 100
        assert len(metadata(caplog, "Signal received")) == 1
        assert metadata(caplog, "Signal parser started")
    assert not exchange.submissions
    output = "\n".join(JSONFormatter().format(record) for record in caplog.records)
    assert "SECRET" not in output
    assert settings.telegram_session_string.get_secret_value() not in output


async def test_edited_message_requires_review_and_clears_previous_parse_metadata(
    settings, engine, exchange, monkeypatch, caplog
):
    caplog.set_level(logging.INFO, logger="okx_bot")
    parser = Mock(wraps=RegexSignalParser())
    engine.parser = parser
    async with running_listener(settings, engine, monkeypatch) as (_, client):
        message = item(101)
        await client.emit(message)
        message.raw_text = text().replace("LONG", "")
        await client.emit(message, edited=True)
        assert parser.parse.call_count == 2
        result = metadata(caplog, "Signal processed")[-1]
        assert result["coin"] == "BTCUSDT" and result["analysis_result"] == "needs_review"
        assert result["reason_code"] == "MISSING_DIRECTION"
        assert result["trade_status"] == "needs_review"
        async with engine.db.sessions() as session:
            row = await session.scalar(
                select(TelegramSignal).where(TelegramSignal.message_id == 101)
            )
            assert row.parsed is None
    assert not exchange.submissions


@pytest.mark.parametrize(
    "authorized,accessible,stage",
    [
        (False, True, "telegram_authorization"),
        (True, False, "telegram_channel_lookup"),
    ],
)
async def test_unauthorized_or_inaccessible_channel_fails_visibly_and_closes_client(
    settings, engine, monkeypatch, caplog, authorized, accessible, stage
):
    caplog.set_level(logging.INFO, logger="okx_bot")
    client = TelegramStub(authorized=authorized, accessible=accessible)
    configure(settings, client)
    listener = TelegramListener(settings, engine)
    monkeypatch.setattr(listener_module, "TelegramClient", client.factory)
    with pytest.raises(RuntimeError):
        await listener.run()
    assert not client.connected and not listener.ready
    assert metadata(caplog, "Telegram listener failed")[0]["stage"] == stage
    assert not metadata(caplog, "Channel subscribed")


async def test_temporary_scan_failure_logs_and_recovers_without_orders(
    settings, engine, exchange, monkeypatch, caplog
):
    caplog.set_level(logging.INFO, logger="okx_bot")
    async with running_listener(settings, engine, monkeypatch, fail_scans=1, fast=True) as (
        listener,
        _,
    ):
        assert listener.last_error is None
        assert metadata(caplog, "Telegram disconnected or scan failed")
        assert listener.ready
    assert not exchange.submissions
    assert "SECRET_PROVIDER_ERROR" not in caplog.text


async def test_repeated_disconnect_emits_one_notification_until_recovery(
    settings, engine, exchange, monkeypatch
):
    async with running_listener(settings, engine, monkeypatch, fail_scans=3, fast=True):
        async with engine.db.sessions() as session:
            events = (
                await session.scalars(
                    select(BotEvent).where(BotEvent.kind == "telegram_disconnected")
                )
            ).all()
            assert len(events) == 1
    assert not exchange.submissions


async def test_read_only_diagnose_uses_same_mz_parser_as_worker(settings):
    client = TelegramStub()
    configure(settings, client)
    settings.mztradingbot_channel_id = settings.telegram_channel_id
    client.messages = [item(202, EXAMPLE)]
    report = await inspect_telegram(settings, client_factory=client.factory)
    assert report["latest_message"]["analysis_result"] == "parsed"
    assert report["latest_message"]["parser_name"] == "mztradingbot_v1"
    assert report["latest_message"]["interpretation_rule"]
    assert report["trading_attempted"] is False and report["engine_ingest_called"] is False


async def test_reconnect_logs_and_does_not_register_duplicate_handlers(
    settings, engine, monkeypatch, caplog
):
    caplog.set_level(logging.INFO, logger="okx_bot")
    async with running_listener(settings, engine, monkeypatch, fast=True) as (listener, client):
        client.connected = False
        client.scanned.clear()
        await asyncio.wait_for(client.scanned.wait(), timeout=5)
        assert client.connect_count == 2 and listener.ready
        assert len(client.handlers) == 2
        assert metadata(caplog, "Telegram reconnecting")
        assert metadata(caplog, "Telegram connected")[-1]["reconnected"]


@pytest.mark.parametrize(
    "content,analysis", [(text(), "parsed"), (text().replace("LONG", ""), "needs_review")]
)
async def test_read_only_probe_parses_without_ingest_database_or_trading(
    settings, content, analysis
):
    client = TelegramStub()
    configure(settings, client)
    client.messages = [item(202, content)]
    report = await inspect_telegram(settings, client_factory=client.factory)
    assert report["authorized"] and report["channel_accessible"]
    assert report["latest_message"]["analysis_result"] == analysis
    assert not report["engine_ingest_called"] and not report["trading_attempted"]
    assert client.options["receive_updates"] is False
    assert not client.handlers and not client.connected
    assert "SECRET" not in json.dumps(report)


async def test_telegram_status_endpoint_is_authenticated(engine, settings):
    app = create_app(settings)
    app.state.engine = engine
    app.state.listener = SimpleNamespace(
        status=lambda: {
            "channel_id": settings.telegram_channel_id,
            "connected": True,
            "authorized": True,
            "subscribed": True,
            "ready": True,
        }
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as api:
        assert (await api.get("/admin/telegram")).status_code == 401
        headers = {"Authorization": "Bearer " + settings.admin_api_token.get_secret_value()}
        response = await api.get("/admin/telegram", headers=headers)
        assert response.json()["subscribed"]
        result = await api.get("/admin/status", headers=headers)
        assert result.json()["telegram"]["authorized"]


async def test_first_start_skips_history_without_calling_parser_or_creating_orders(
    engine, settings, exchange, monkeypatch
):
    client = TelegramStub()
    configure(settings, client)
    client.messages = [item(303)]
    engine.parser = Mock(wraps=RegexSignalParser())
    listener = TelegramListener(settings, engine)
    listener.client = client
    listener._entity = client.entity
    await listener._bootstrap()
    assert await listener._process(client.messages[0]) == "ignored_history"
    engine.parser.parse.assert_not_called()
    assert not exchange.submissions
    async with engine.db.sessions() as session:
        assert (await session.get(RuntimeState, 1)).channel_cursor == 303
