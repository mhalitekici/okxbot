"""Telegram connection/channel/parser checks without ingestion or trading access."""

from telethon import TelegramClient
from telethon.sessions import StringSession

from okx_bot.config import Settings
from okx_bot.diagnostics import failure_details
from okx_bot.domain import Message, ReviewRequired, utcnow
from okx_bot.parser import ChannelSignalParser, symbol_candidates
from okx_bot.telegram.listener import find_channel, load_session
from okx_bot.telegram.topics import message_topic_id, topic_metadata


async def inspect_telegram(
    settings: Settings, *, client_factory=TelegramClient, message_ids: list[int] | None = None
) -> dict:
    report = {
        "channel_id": settings.telegram_channel_id,
        "configured_topic_id": settings.telegram_topic_id,
        "configured_topic_verified": False,
        "read_only": True,
        "engine_ingest_called": False,
        "trading_attempted": False,
        "receive_updates": False,
        "demo": settings.okx_demo,
        "live_enabled": settings.live_trading_enabled,
    }
    client = None
    stage = "telegram_session_load"
    try:
        value = load_session(settings)
        client = client_factory(
            StringSession(value),
            settings.telegram_api_id,
            settings.telegram_api_hash.get_secret_value(),
            receive_updates=False,
            auto_reconnect=False,
            connection_retries=1,
            request_retries=1,
        )
        stage = "telegram_connect"
        await client.connect()
        report["connected"] = client.is_connected()
        stage = "telegram_authorization"
        report["authorized"] = await client.is_user_authorized()
        if not report["authorized"]:
            return report
        stage = "telegram_channel_lookup"
        entity = await find_channel(client, settings.telegram_channel_id)
        report["channel_accessible"] = entity is not None
        if entity is None:
            return report
        report["group_forum"] = bool(getattr(entity, "forum", False))
        stage = "telegram_read_latest"
        messages = (
            await client.get_messages(entity, ids=message_ids)
            if message_ids
            else await client.get_messages(entity, limit=1)
        )
        report["latest_message"] = None
        results = []
        report["messages"] = results
        for index, message in enumerate(messages):
            if message is None:
                results.append(
                    {
                        "message_id": message_ids[index] if message_ids else None,
                        "available": False,
                    }
                )
                continue
            candidates = symbol_candidates(message.raw_text or "")
            reply = getattr(message, "reply_to", None)
            action = getattr(message, "action", None)
            result = {
                "message_id": message.id,
                "available": True,
                "reply_to_top_id": getattr(reply, "reply_to_top_id", None),
                "reply_to_msg_id": getattr(reply, "reply_to_msg_id", None),
                "forum_topic": getattr(reply, "forum_topic", None),
                "topic_id": message_topic_id(message),
                "action_type": type(action).__name__ if action else None,
                "action_title": getattr(action, "title", None),
                "eligible_topic": False,
                "coin_candidates": candidates,
                "coin": candidates[0] if len(candidates) == 1 else None,
                "age_seconds": int((utcnow() - message.date).total_seconds()),
            }
            results.append(result)
        stage = "telegram_topic_lookup"
        ids = [row["topic_id"] for row in results if row.get("topic_id") is not None]
        topics = (
            await topic_metadata(client, entity, ids + [settings.telegram_topic_id])
            if report["group_forum"]
            else {}
        )
        report["topics"] = list(topics.values())
        report["configured_topic_verified"] = settings.telegram_topic_id in topics
        for message, result in zip(messages, results, strict=True):
            if message is None:
                continue
            result["topic"] = topics.get(result["topic_id"])
            result["eligible_topic"] = bool(
                report["configured_topic_verified"]
                and result["topic_id"] == settings.telegram_topic_id
            )
            if getattr(message, "action", None) is not None:
                result["analysis_result"] = "service_message"
                continue
            if not result["eligible_topic"]:
                result["analysis_result"] = "ignored_topic"
                continue
            try:
                parsed = ChannelSignalParser(
                    settings.mztradingbot_channel_id, settings.telegram_topic_id
                ).parse(
                    Message(
                        settings.telegram_channel_id,
                        message.id,
                        message.raw_text or "",
                        message.date,
                        topic_id=result["topic_id"],
                    )
                )
                result.update(
                    coin=parsed.symbol,
                    direction=parsed.direction.value,
                    analysis_result="parsed",
                    parser_name=parsed.parser_name,
                    interpretation_rule=parsed.interpretation_rule,
                    entry=str(parsed.entry),
                    range_percent=str(parsed.range_percent),
                    take_profit=str(parsed.take_profit),
                    stop_loss=str(parsed.stop_loss),
                    automation_policy=parsed.automation_policy,
                )
            except (ReviewRequired, ValueError) as exc:
                result.update(
                    analysis_result="needs_review",
                    reason_code=exc.code if isinstance(exc, ReviewRequired) else "INVALID_SIGNAL",
                )
        if not message_ids and results:
            report["latest_message"] = results[0]
    except Exception as exc:
        report["error"] = failure_details(exc, stage)
    finally:
        if client is not None:
            await client.disconnect()
    return report
