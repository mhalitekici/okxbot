from datetime import timedelta
from types import SimpleNamespace

import pytest
from sqlalchemy import select
from telethon import types

from okx_bot.domain import utcnow
from okx_bot.models import RuntimeState, TelegramSignal
from okx_bot.telegram.listener import TelegramListener
from tests.fakes import text
from tests.test_mz_parser import EXAMPLE


class Channel:
    def __init__(self, messages):
        self.messages = messages

    async def get_messages(self, entity, limit):
        return sorted(self.messages, key=lambda m: m.id, reverse=True)[:limit]

    async def iter_messages(self, entity, limit, reply_to=None):
        self.requested_topic = reply_to
        for message in await self.get_messages(entity, limit):
            yield message


def item(identifier, age=0, topic=23576):
    return SimpleNamespace(
        id=identifier,
        date=utcnow() - timedelta(seconds=age),
        raw_text=text(),
        edit_date=None,
        chat_id=-100123,
        action=None,
        reply_to=types.MessageReplyHeader(forum_topic=True, reply_to_msg_id=topic),
    )


async def test_initial_start_skips_existing_messages_then_catches_new_message(engine, exchange):
    listener = TelegramListener(engine.settings, engine)
    listener.topic_verified = True
    listener.client = Channel([item(10, age=5)])
    await listener._bootstrap()
    await listener.scan()
    assert not exchange.submissions
    listener.client.messages.append(item(11))
    await listener.scan()
    assert len(exchange.submissions) == 1
    assert (await engine.state()).channel_cursor == 11


async def test_reconnect_backfill_rejects_expired_messages(engine, exchange):
    async with engine.db.sessions.begin() as session:
        state = await session.get(RuntimeState, 1)
        state.channel_id = engine.settings.telegram_channel_id
        state.channel_cursor = 10
        state.channel_started_at = utcnow() - timedelta(minutes=10)
    listener = TelegramListener(engine.settings, engine)
    listener.topic_verified = True
    listener.client = Channel([item(12), item(11, age=120)])
    await listener._bootstrap()
    await listener.scan()
    assert len(exchange.submissions) == 1
    async with engine.db.sessions() as session:
        old = await session.scalar(select(TelegramSignal).where(TelegramSignal.message_id == 11))
        assert old.status == "rejected"


@pytest.mark.parametrize("caption,expected", [(EXAMPLE, "rejected"), (None, "needs_review")])
async def test_media_uses_only_telethon_caption_without_guessing_image(
    engine, exchange, caption, expected
):
    from okx_bot.parser import ChannelSignalParser

    engine.parser = ChannelSignalParser(
        engine.settings.telegram_channel_id, engine.settings.telegram_topic_id
    )
    await engine.pause()
    listener = TelegramListener(engine.settings, engine)
    listener.topic_verified = True
    media_message = SimpleNamespace(
        id=55,
        date=utcnow(),
        raw_text=caption,
        media=object(),
        chat_id=engine.settings.telegram_channel_id,
        action=None,
        reply_to=types.MessageReplyHeader(forum_topic=True, reply_to_msg_id=23576),
    )
    assert await listener._process(media_message) == expected
    async with engine.db.sessions() as session:
        saved = await session.scalar(select(TelegramSignal))
        assert saved.original_text == (caption or "")
        if caption:
            assert saved.parsed["symbol"] == "CTUSDT"
        else:
            assert saved.parsed is None
    assert not exchange.submissions
