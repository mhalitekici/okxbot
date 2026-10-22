import asyncio
import logging
from datetime import UTC

from telethon import TelegramClient, events, utils
from telethon.sessions import StringSession

from okx_bot.config import Settings
from okx_bot.diagnostics import failure_details
from okx_bot.domain import Message, utcnow
from okx_bot.models import RuntimeState
from okx_bot.telegram.topics import message_topic_id, topic_metadata

logger = logging.getLogger(__name__)


def load_session(settings: Settings) -> str:
    value = settings.telegram_session_string.get_secret_value()
    return value or settings.telegram_session_file.read_text(encoding="utf-8").strip()


async def find_channel(client, channel_id: int):
    # A StringSession may have no cached entities, so resolve membership from dialogs.
    async for dialog in client.iter_dialogs():
        if utils.get_peer_id(dialog.entity) == channel_id:
            return dialog.entity
    return None


class TelegramListener:
    def __init__(self, settings: Settings, engine):
        self.settings, self.engine = settings, engine
        self.client: TelegramClient | None = None
        self.ready = False
        self._baseline = 0
        self._entity = None
        self.authorized = False
        self.subscribed = False
        self.handler_count = 0
        self.last_scan_at = None
        self.last_message_at = None
        self.last_message_id = None
        self.last_error = None
        self.topic_verified = False
        self.topic = None

    def status(self) -> dict:
        connected = bool(self.client and self.client.is_connected())
        return {
            "channel_id": self.settings.telegram_channel_id,
            "topic_id": self.settings.telegram_topic_id,
            "topic_verified": self.topic_verified and connected,
            "topic": self.topic,
            "connected": connected,
            "authorized": self.authorized,
            "subscribed": self.subscribed and connected,
            "handler_count": self.handler_count,
            "ready": self.ready and connected,
            "last_scan_at": self.last_scan_at.isoformat() if self.last_scan_at else None,
            "last_message_at": self.last_message_at.isoformat() if self.last_message_at else None,
            "last_message_id": self.last_message_id,
            "last_error": self.last_error,
        }

    async def _process(self, message, *, edited=False):
        if getattr(message, "chat_id", None) != self.settings.telegram_channel_id:
            return "ignored_channel"
        if message.id <= self._baseline:
            logger.debug(
                "Historical Telegram message skipped",
                extra={
                    "context": {
                        "channel_id": self.settings.telegram_channel_id,
                        "message_id": message.id,
                        "trade_status": "ignored_history",
                    }
                },
            )
            return "ignored_history"
        if getattr(message, "action", None) is not None:
            return "ignored_service"
        topic_id = message_topic_id(message)
        if (
            not self.topic_verified
            or not self.settings.telegram_topic_id
            or topic_id != self.settings.telegram_topic_id
        ):
            return "ignored_topic"
        context = {
            "channel_id": self.settings.telegram_channel_id,
            "message_id": message.id,
            "topic_id": topic_id,
            "edited": edited,
        }
        logger.debug("Telegram message received", extra={"context": context})
        try:
            result = await self.engine.ingest(
                Message(
                    channel_id=self.settings.telegram_channel_id,
                    message_id=message.id,
                    text=message.raw_text or "",
                    date=message.date.astimezone(UTC),
                    edited=edited,
                    topic_id=topic_id,
                )
            )
            if result not in {"duplicate", "ignored_history", "ignored_channel", "ignored_topic"}:
                self.last_message_at, self.last_message_id = utcnow(), message.id
            return result
        except Exception as exc:
            logger.error(
                "Telegram message processing failed",
                extra={
                    "context": {
                        **context,
                        **failure_details(exc, "telegram_message_processing"),
                    }
                },
            )
            raise

    async def _verify_topic(self):
        self.topic_verified = False
        self.topic = None
        if not self.settings.telegram_topic_id or not getattr(self._entity, "forum", False):
            raise RuntimeError("A configured forum topic is required; new signals disabled")
        topics = await topic_metadata(self.client, self._entity, [self.settings.telegram_topic_id])
        self.topic = topics.get(self.settings.telegram_topic_id)
        if self.topic is None:
            raise RuntimeError(
                "Configured Telegram topic could not be verified; new signals disabled"
            )
        self.topic_verified = True

    async def _bootstrap(self):
        state = await self.engine.state()
        if state.channel_id is not None and state.channel_id != self.settings.telegram_channel_id:
            raise RuntimeError("Channel changed: use a separate database for another channel")
        if state.channel_cursor is None:
            started = utcnow()
            latest = await self.client.get_messages(self._entity, limit=1)
            self._baseline = latest[0].id if latest else 0
            async with self.engine.db.sessions.begin() as session:
                saved = await session.get(RuntimeState, 1)
                saved.channel_id = self.settings.telegram_channel_id
                saved.channel_cursor = self._baseline
                saved.channel_started_at = started
            await self.engine.event("telegram_initial_baseline", cursor=self._baseline)
            logger.info(
                "Telegram baseline initialized",
                extra={
                    "context": {
                        "channel_id": self.settings.telegram_channel_id,
                        "cursor": self._baseline,
                        "historical_trading": False,
                    }
                },
            )
        else:
            # Startup timestamp remains persisted; historical edits cannot revive old signals.
            self._baseline = 0
            logger.info(
                "Telegram cursor restored",
                extra={
                    "context": {
                        "channel_id": self.settings.telegram_channel_id,
                        "cursor": state.channel_cursor,
                    }
                },
            )

    async def scan(self):
        state = await self.engine.state()
        cursor = state.channel_cursor or 0
        # Polling complements Telethon's automatic reconnect and MessageEdited updates.
        # It also catches edits of recently seen, incomplete signals after a disconnect.
        recent = []
        if not self.topic_verified:
            return
        async for message in self.client.iter_messages(
            self._entity, reply_to=self.settings.telegram_topic_id, limit=501
        ):
            recent.append(message)
            if (
                message.id <= cursor
                and (utcnow() - message.date).total_seconds() > self.settings.max_signal_age_seconds
            ):
                break
        unseen = [m for m in recent if m.id > cursor]
        if len(unseen) > 500:
            await self.engine.trip("Telegram catch-up overflow; review channel before resuming")
        for message in reversed(recent):
            if message.id > cursor or message.edit_date:
                await self._process(message, edited=bool(message.edit_date))
        if unseen:
            async with self.engine.db.sessions.begin() as session:
                saved = await session.get(RuntimeState, 1)
                saved.channel_cursor = max(cursor, max(m.id for m in unseen))
        self.last_scan_at = utcnow()
        logger.debug(
            "Telegram channel scan complete",
            extra={
                "context": {
                    "channel_id": self.settings.telegram_channel_id,
                    "new_messages": len(unseen),
                }
            },
        )

    async def run(self):
        logger.info(
            "Telegram listener starting",
            extra={
                "context": {
                    "channel_id": self.settings.telegram_channel_id,
                    "session_source": "environment"
                    if self.settings.telegram_session_string.get_secret_value()
                    else "persistent_file",
                }
            },
        )
        session_string = load_session(self.settings)
        self.client = TelegramClient(
            StringSession(session_string),
            self.settings.telegram_api_id,
            self.settings.telegram_api_hash.get_secret_value(),
            auto_reconnect=True,
            connection_retries=10,
            sequential_updates=True,
        )
        stage = "telegram_connect"
        try:
            await self.client.connect()
            stage = "telegram_authorization"
            if not await self.client.is_user_authorized():
                raise RuntimeError("Telegram session unauthorized; run telegram-session")
            self.authorized = True
            logger.info(
                "Telegram connected",
                extra={
                    "context": {
                        "channel_id": self.settings.telegram_channel_id,
                        "authorized": True,
                    }
                },
            )
            stage = "telegram_channel_lookup"
            self._entity = await find_channel(self.client, self.settings.telegram_channel_id)
            if self._entity is None:
                raise RuntimeError("Configured Telegram channel is not in this account's dialogs")
            stage = "telegram_topic_lookup"
            await self._verify_topic()
            stage = "telegram_bootstrap"
            await self._bootstrap()

            async def new_message(event):
                await self._process(event.message)

            async def edited_message(event):
                await self._process(event.message, edited=True)

            self.client.add_event_handler(new_message, events.NewMessage(chats=self._entity))
            self.client.add_event_handler(edited_message, events.MessageEdited(chats=self._entity))
            self.subscribed, self.handler_count = True, 2
            logger.info(
                "Channel subscribed",
                extra={
                    "context": {
                        "channel_id": self.settings.telegram_channel_id,
                        "handlers": ["NewMessage", "MessageEdited"],
                        "handler_count": self.handler_count,
                    }
                },
            )
            stage = "telegram_scan"
            while True:
                try:
                    if not self.client.is_connected():
                        self.ready = False
                        logger.warning(
                            "Telegram reconnecting",
                            extra={
                                "context": {
                                    "channel_id": self.settings.telegram_channel_id,
                                }
                            },
                        )
                        await self.client.connect()
                        self.authorized = await self.client.is_user_authorized()
                        if not self.authorized:
                            raise RuntimeError("Telegram session unauthorized after reconnect")
                        await self._verify_topic()
                        logger.info(
                            "Telegram connected",
                            extra={
                                "context": {
                                    "channel_id": self.settings.telegram_channel_id,
                                    "reconnected": True,
                                }
                            },
                        )
                    await self.scan()
                    if not self.ready:
                        logger.info(
                            "Telegram listener ready",
                            extra={
                                "context": {
                                    "channel_id": self.settings.telegram_channel_id,
                                }
                            },
                        )
                    self.ready = True
                    self.last_error = None
                except (OSError, ConnectionError, TimeoutError) as exc:
                    self.ready = False
                    previous_error = self.last_error
                    self.last_error = failure_details(exc, "telegram_scan")
                    logger.warning(
                        "Telegram disconnected or scan failed",
                        extra={
                            "context": {
                                "channel_id": self.settings.telegram_channel_id,
                                **self.last_error,
                            },
                        },
                    )
                    if not previous_error:
                        await self.engine.event("telegram_disconnected")
                await asyncio.sleep(self.settings.telegram_scan_interval_seconds)
        except Exception as exc:
            self.last_error = failure_details(exc, stage)
            logger.error(
                "Telegram listener failed",
                extra={
                    "context": {
                        "channel_id": self.settings.telegram_channel_id,
                        **self.last_error,
                    }
                },
            )
            raise
        finally:
            self.ready = self.subscribed = self.authorized = False
            self.topic_verified = False
            self.handler_count = 0
            await self.client.disconnect()
            logger.info(
                "Telegram listener stopped",
                extra={
                    "context": {
                        "channel_id": self.settings.telegram_channel_id,
                    }
                },
            )
