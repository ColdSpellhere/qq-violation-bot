from __future__ import annotations

from typing import Any

from nonebot import logger, on_message
from nonebot.adapters.onebot.v11 import Event, GroupMessageEvent
from nonebot.rule import Rule

from plugins.feature_control.runtime import FEATURES
from plugins.member_memory.store import remember_identity
from plugins.violation_record.config import CONFIG
from .db import archive_payload


def _reply_id(event: GroupMessageEvent) -> str | None:
    if event.reply is not None:
        for name in ("message_id", "id"):
            value = getattr(event.reply, name, None)
            if value not in (None, ""):
                return str(value)
    for message in (event.original_message, event.message):
        for segment in message:
            if segment.type != "reply":
                continue
            value = segment.data.get("id") or segment.data.get("message_id")
            if value not in (None, ""):
                return str(value)
    return None


def _sender_dict(event: GroupMessageEvent) -> dict[str, Any]:
    sender = event.sender
    if hasattr(sender, "model_dump"):
        return sender.model_dump()
    if hasattr(sender, "dict"):
        return sender.dict()
    return {
        "nickname": getattr(sender, "nickname", None),
        "card": getattr(sender, "card", None),
    }


def _chat_group(event: Event) -> bool:
    return (
        isinstance(event, GroupMessageEvent)
        and FEATURES.group_chat_allowed(int(event.group_id))
    )


archive_matcher = on_message(rule=Rule(_chat_group), priority=1, block=False)


@archive_matcher.handle()
async def archive_chat_message(event: GroupMessageEvent) -> None:
    sender = _sender_dict(event)
    try:
        archived = archive_payload(
            CONFIG.chat_archive_path,
            int(event.group_id),
            {
                "message_id": str(event.message_id),
                "group_id": int(event.group_id),
                "event_time": int(event.time),
                "user_id": str(event.user_id),
                "sender": sender,
                "segments": [
                    {"type": segment.type, "data": dict(segment.data)}
                    for segment in event.message
                ],
                "plaintext": event.get_plaintext(),
                "reply_message_id": _reply_id(event),
            },
        )
    except Exception as exc:
        logger.warning(
            f"目标群归档失败 stage=archive message_id={event.message_id} "
            f"error={type(exc).__name__}"
        )
        return
    if not archived:
        return
    try:
        remember_identity(
            CONFIG.chat_archive_path,
            CONFIG.member_memory_root,
            group_id=int(event.group_id),
            user_id=str(event.user_id),
            nickname=str(sender.get("card") or sender.get("nickname") or event.user_id),
        )
    except Exception as exc:
        logger.warning(
            f"群友身份记忆失败 message_id={event.message_id} error={type(exc).__name__}"
        )
