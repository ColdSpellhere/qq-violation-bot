from __future__ import annotations

from typing import Any

from nonebot.adapters.onebot.v11 import Message


def is_group_event(event: Any) -> bool:
    return (
        getattr(event, "message_type", None) == "group"
        or getattr(event, "group_id", None) is not None
    )


def addressed_group_admin_message(event: Any) -> Message | None:
    message = Message(getattr(event, "message", ()))
    if not is_group_event(event):
        return message

    self_id = str(getattr(event, "self_id", ""))
    target_index: int | None = None
    for index, segment in enumerate(message):
        if segment.type == "text" and not str(
            segment.data.get("text", "")
        ).strip():
            continue
        if (
            segment.type == "at"
            and str(segment.data.get("qq", "")) == self_id
        ):
            target_index = index
        break
    if target_index is None:
        return None
    return Message(
        segment for index, segment in enumerate(message) if index != target_index
    )


def group_admin_targets_self(event: Any) -> bool:
    return addressed_group_admin_message(event) is not None
