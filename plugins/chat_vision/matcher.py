from __future__ import annotations

from nonebot import on_message
from nonebot.adapters.onebot.v11 import Event, GroupMessageEvent
from nonebot.rule import Rule

from plugins.feature_control.runtime import FEATURES
from plugins.violation_record.config import CONFIG

from .service import process_image_event


def chat_image_candidate(event: Event) -> bool:
    return (
        CONFIG.chat_vision_enabled
        and isinstance(event, GroupMessageEvent)
        and int(event.user_id) != int(event.self_id)
        and FEATURES.group_chat_allowed(int(event.group_id))
    )


chat_image_matcher = on_message(
    rule=Rule(chat_image_candidate),
    priority=2,
    block=False,
)


@chat_image_matcher.handle()
async def collect_chat_images(event: GroupMessageEvent) -> None:
    if chat_image_candidate(event):
        await process_image_event(event)
