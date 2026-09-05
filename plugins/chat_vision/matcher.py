from __future__ import annotations

from nonebot import on_message
from nonebot.adapters.onebot.v11 import Event, GroupMessageEvent
from nonebot.rule import Rule

from plugins.feature_control.runtime import FEATURES
from plugins.violation_record.config import CONFIG

from .service import live_event_time_allowed, enqueue_image_event


def chat_image_candidate(event: Event) -> bool:
    image_allowed = getattr(FEATURES, "image_understanding_allowed", lambda: True)
    return (
        CONFIG.chat_vision_enabled
        and bool(image_allowed())
        and isinstance(event, GroupMessageEvent)
        and int(event.user_id) != int(event.self_id)
        and FEATURES.group_chat_allowed(int(event.group_id))
        and live_event_time_allowed(int(event.time))
    )


chat_image_matcher = on_message(
    rule=Rule(chat_image_candidate),
    priority=2,
    block=False,
)


@chat_image_matcher.handle()
async def collect_chat_images(event: GroupMessageEvent) -> None:
    if chat_image_candidate(event):
        await enqueue_image_event(event)
