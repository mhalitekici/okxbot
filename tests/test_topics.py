from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from sqlalchemy import func, select
from telethon import functions, types

from okx_bot.config import Settings
from okx_bot.domain import Message, utcnow
from okx_bot.models import BotEvent, TelegramSignal, TradeOrder
from okx_bot.telegram.admin import AdminBot
from okx_bot.telegram.diagnostics import inspect_telegram
from okx_bot.telegram.listener import TelegramListener
from okx_bot.telegram.topics import message_topic_id, topic_metadata
from tests.fakes import forum_topic, text
from tests.test_listener import Channel
from tests.test_telegram_observability import TelegramStub, configure

TOPIC = 23576


def telegram_message(settings, *, identifier=32876, parent=TOPIC, top=None, forum=True, **changes):
    return SimpleNamespace(
        **{
            "id": identifier,
            "date": utcnow(),
            "raw_text": text(),
            "edit_date": None,
            "chat_id": settings.telegram_channel_id,
            "peer_id": types.PeerChannel(123),
            "action": None,
            "reply_to": types.MessageReplyHeader(
                forum_topic=forum,
                reply_to_msg_id=parent,
                reply_to_top_id=top,
            ),
            **changes,
        }
    )


@pytest.mark.parametrize(
    "top,parent,forum,expected",
    [
        (None, 23576, True, 23576),  # Actual signal room header of message 32876.
        (None, 6, True, 6),  # Actual other-room header of message 32877.
        (23576, 32875, True, 23576),  # A reply to another message within the signal topic.
        (6, 23576, True, 6),  # The direct parent never overrides a different top ID.
        (23576, 6, True, 23576),
        (23576, 23576, False, None),
        (23576, 23576, None, None),
        (None, None, True, None),
        (None, 23576, False, None),
        (0, 23576, True, None),
        (-1, 23576, True, None),
        (None, -1, True, None),
        (None, "23576", True, None),
        (None, True, True, None),
    ],
)
def test_topic_resolution_requires_explicit_forum_header(settings, top, parent, forum, expected):
    assert (
        message_topic_id(telegram_message(settings, top=top, parent=parent, forum=forum))
        == expected
    )


def test_missing_or_different_header_never_infers_topic_from_content_or_forward(settings):
    item = telegram_message(settings, reply_to=None, raw_text=f"{TOPIC} LONG {text()}")
    assert message_topic_id(item) is None
    item.fwd_from = SimpleNamespace(saved_from_msg_id=TOPIC)
    assert message_topic_id(item) is None
    item.reply_to = SimpleNamespace(forum_topic=True, reply_to_msg_id=TOPIC)
    assert message_topic_id(item) is None


def test_cross_chat_reply_does_not_confer_allowed_topic_identity(settings):
    item = telegram_message(settings)
    item.reply_to.reply_to_peer_id = types.PeerChannel(999)
    assert message_topic_id(item) is None
    item.reply_to.reply_to_peer_id = item.peer_id
    assert message_topic_id(item) == TOPIC


def test_topic_creation_service_identifies_its_own_id_for_diagnostics(settings):
    item = telegram_message(
        settings,
        identifier=TOPIC,
        reply_to=None,
        action=types.MessageActionTopicCreate(title="Bot risk", icon_color=0),
    )
    assert message_topic_id(item) == TOPIC


async def no_signal_records(engine):
    async with engine.db.sessions() as session:
        for model in (TelegramSignal, TradeOrder, BotEvent):
            query = select(func.count()).select_from(model)
            if model is BotEvent:
                query = query.where(
                    BotEvent.kind.not_in({"limits_initialized", "allocation_created"})
                )
            assert await session.scalar(query) == 0


@pytest.mark.parametrize(
    "changes",
    [
        {"parent": 6},
        {"parent": 23576, "top": 6},
        {"reply_to": None},
        {"forum": False},
        {"top": 0},
        {"chat_id": -100999},
        {"chat_id": None},
        {"action": types.MessageActionTopicCreate(title="Room", icon_color=0)},
    ],
)
@pytest.mark.parametrize("edited", [False, True])
async def test_other_or_unknown_rooms_never_ingest_notify_or_submit(
    settings, engine, exchange, changes, edited
):
    listener = TelegramListener(settings, engine)
    listener.topic_verified = True
    ingest = AsyncMock(wraps=engine.ingest)
    engine.ingest = ingest
    outcome = await listener._process(telegram_message(settings, **changes), edited=edited)
    assert outcome in {"ignored_topic", "ignored_channel", "ignored_service"}
    ingest.assert_not_awaited()
    assert listener.last_message_id is None
    await no_signal_records(engine)
    bot = AdminBot(settings, engine)
    bot.send = AsyncMock()
    try:
        await bot.deliver_pending()
        bot.send.assert_not_awaited()
    finally:
        await bot.http.aclose()
    assert not exchange.submissions and not exchange.leverage_calls


async def test_topic_metadata_must_be_verified_before_processing(settings, engine, exchange):
    listener = TelegramListener(settings, engine)
    assert await listener._process(telegram_message(settings)) == "ignored_topic"
    await no_signal_records(engine)
    assert not exchange.submissions


@pytest.mark.parametrize("topic", [None, 6, 0, "23576", 23576.0, True])
async def test_engine_independently_rejects_missing_or_wrong_topic(engine, exchange, topic):
    engine.parser = Mock(side_effect=AssertionError("Parser must not run"))
    item = Message(engine.settings.telegram_channel_id, 1, text(), utcnow(), topic_id=topic)
    assert await engine.ingest(item) == "ignored_topic"
    engine.parser.parse.assert_not_called()
    await no_signal_records(engine)
    assert not exchange.submissions


async def test_unconfigured_topic_is_fail_closed_even_for_matching_group(
    settings, engine, exchange
):
    settings.telegram_topic_id = 0
    listener = TelegramListener(settings, engine)
    listener.topic_verified = True
    assert await listener._process(telegram_message(settings)) == "ignored_topic"
    item = Message(settings.telegram_channel_id, 1, text(), utcnow(), topic_id=TOPIC)
    assert await engine.ingest(item) == "ignored_topic"
    await no_signal_records(engine)
    assert not exchange.submissions


@pytest.mark.parametrize("top,parent", [(None, 23576), (23576, 32875)])
async def test_verified_direct_and_nested_topic_messages_reach_risk_checks(
    settings, engine, exchange, top, parent
):
    await engine.pause()
    listener = TelegramListener(settings, engine)
    listener.topic_verified = True
    assert await listener._process(telegram_message(settings, top=top, parent=parent)) == "rejected"
    async with engine.db.sessions() as session:
        signal = await session.scalar(select(TelegramSignal))
        assert signal.parsed["symbol"] == "BTCUSDT"
        assert signal.parsed["topic_id"] == TOPIC
        assert signal.reason == "Trading paused or reconciliation unavailable"
    assert listener.last_message_id == 32876
    assert not exchange.submissions


async def test_polling_queries_only_configured_topic_and_filters_returned_messages(
    settings, engine, exchange
):
    await engine.pause()
    listener = TelegramListener(settings, engine)
    listener.topic_verified = True
    listener.client = Channel(
        [
            telegram_message(settings, identifier=32876),
            telegram_message(settings, identifier=32877, parent=6),
        ]
    )
    await listener.scan()
    assert listener.client.requested_topic == TOPIC
    async with engine.db.sessions() as session:
        ids = list(await session.scalars(select(TelegramSignal.message_id)))
        assert ids == [32876]
    assert not exchange.submissions


async def test_startup_verifies_metadata_and_rejects_deleted_missing_or_nonforum_topic(
    settings, engine
):
    client = TelegramStub()
    listener = TelegramListener(settings, engine)
    listener.client, listener._entity = client, client.entity
    await listener._verify_topic()
    assert listener.topic_verified and listener.topic["id"] == TOPIC
    client.topics = [types.ForumTopicDeleted(TOPIC)]
    with pytest.raises(RuntimeError, match="could not be verified"):
        await listener._verify_topic()
    assert not listener.topic_verified and listener.topic is None
    listener._entity.forum = False
    with pytest.raises(RuntimeError, match="configured forum topic"):
        await listener._verify_topic()


async def test_metadata_request_is_read_only_and_rejects_unrequested_rows():
    class Client:
        async def __call__(self, request):
            assert isinstance(request, functions.messages.GetForumTopicsByIDRequest)
            assert request.topics == [23576]
            return SimpleNamespace(
                topics=[forum_topic(), forum_topic(6), types.ForumTopicDeleted(1)]
            )

    assert list(await topic_metadata(Client(), object(), [TOPIC, TOPIC, 0, -1])) == [TOPIC]


async def test_read_only_diagnose_compares_real_header_shapes_and_current_topic_metadata(settings):
    client = TelegramStub()
    configure(settings, client)
    client.topics = [
        forum_topic(23576, "AL SAT ÖNCÜ SİNYAL ODASI"),
        forum_topic(6, "ŞükoNEWS"),
    ]
    client.messages = [
        telegram_message(
            settings,
            identifier=23576,
            reply_to=None,
            raw_text="",
            action=types.MessageActionTopicCreate(title="Bot risk", icon_color=0),
        ),
        telegram_message(settings, identifier=32877, parent=6),
        telegram_message(settings, identifier=32876),
    ]
    report = await inspect_telegram(
        settings, client_factory=client.factory, message_ids=[23576, 32877, 32876, 99999]
    )
    assert report["configured_topic_verified"]
    root, other, signal, missing = report["messages"]
    assert root["action_type"] == "MessageActionTopicCreate" and root["topic_id"] == TOPIC
    assert root["topic"]["title"] == "AL SAT ÖNCÜ SİNYAL ODASI"
    assert root["action_title"] == "Bot risk" and root["analysis_result"] == "service_message"
    assert other["reply_to_top_id"] is None and other["reply_to_msg_id"] == 6
    assert other["forum_topic"] is True and other["topic"]["title"] == "ŞükoNEWS"
    assert not other["eligible_topic"] and other["analysis_result"] == "ignored_topic"
    assert signal["eligible_topic"] and signal["analysis_result"] == "parsed"
    assert missing == {"message_id": 99999, "available": False}
    assert (
        report["read_only"]
        and not report["engine_ingest_called"]
        and not report["trading_attempted"]
    )
    assert not client.options["receive_updates"]


def test_topic_configuration_defaults_closed_and_rejects_invalid_ids():
    assert Settings(_env_file=None).telegram_topic_id == 0
    for value in [-1, 2**31, "not-a-topic"]:
        with pytest.raises(ValueError):
            Settings(_env_file=None, telegram_topic_id=value)


async def test_wrong_group_is_rejected_even_when_topic_id_matches(engine, exchange):
    item = Message(-100999, 1, text(), utcnow(), topic_id=TOPIC)
    assert await engine.ingest(item) == "ignored_channel"
    await no_signal_records(engine)
    assert not exchange.submissions
