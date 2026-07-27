from __future__ import annotations

from typing import Any

from nonebot import logger, on_message
from nonebot.adapters.onebot.v11 import Event, GroupMessageEvent
from nonebot.rule import Rule

from plugins.violation_record.config import CONFIG
from .db import archive_payload


def _reply_id(event: GroupMessageEvent) -> str | None:
    for segment in event.message:
        if segment.type == "reply":
            value = segment.data.get("id") or segment.data.get("message_id")
            return str(value) if value is not None else None
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


def _target_group(event: Event) -> bool:
    return (
        isinstance(event, GroupMessageEvent)
        and int(event.group_id) == CONFIG.target_group_id
    )


archive_matcher = on_message(rule=Rule(_target_group), priority=1, block=False)


@archive_matcher.handle()
async def archive_target_message(event: GroupMessageEvent) -> None:
    try:
        archive_payload(
            CONFIG.chat_archive_path,
            CONFIG.target_group_id,
            {
                "message_id": str(event.message_id),
                "group_id": int(event.group_id),
                "event_time": int(event.time),
                "user_id": str(event.user_id),
                "sender": _sender_dict(event),
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
