"""Forum identity from Telegram headers and read-only topic metadata, never message text."""

from telethon import functions, types


def positive_message_id(value) -> bool:
    return type(value) is int and 0 < value <= 2**31 - 1


def message_topic_id(message) -> int | None:
    if isinstance(getattr(message, "action", None), types.MessageActionTopicCreate):
        return message.id if positive_message_id(message.id) else None
    reply = getattr(message, "reply_to", None)
    if not isinstance(reply, types.MessageReplyHeader) or reply.forum_topic is not True:
        return None
    # A quoted/cross-chat reply must not confer the referenced chat's topic identity.
    if reply.reply_to_peer_id is not None and reply.reply_to_peer_id != getattr(
        message, "peer_id", None
    ):
        return None
    if reply.reply_to_top_id is not None:
        return reply.reply_to_top_id if positive_message_id(reply.reply_to_top_id) else None
    return reply.reply_to_msg_id if positive_message_id(reply.reply_to_msg_id) else None


async def topic_metadata(client, entity, topic_ids: list[int]) -> dict[int, dict]:
    """Only getForumTopicsByID: no read acknowledgements, subscriptions or mutations."""
    requested = sorted({identifier for identifier in topic_ids if positive_message_id(identifier)})
    if not requested:
        return {}
    result = await client(
        functions.messages.GetForumTopicsByIDRequest(peer=entity, topics=requested)
    )
    return {
        topic.id: {
            "id": topic.id,
            "title": topic.title,
            "top_message": topic.top_message,
            "closed": bool(topic.closed),
            "hidden": bool(topic.hidden),
        }
        for topic in result.topics
        if isinstance(topic, types.ForumTopic) and topic.id in requested and topic.title
    }
