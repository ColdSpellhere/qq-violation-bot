from nonebot import on_message
from nonebot.adapters.onebot.v11 import Bot, Event, GroupMessageEvent
from nonebot.rule import Rule

from plugins.feature_control.runtime import FEATURES
from plugins.random_chat.matcher import send_random_reply
from plugins.random_chat.policy import eligible_text, should_reply
from plugins.violation_record.config import CONFIG
from plugins.violation_record.matcher import (
    _is_at_me,
    _plain_without_at,
    handle_business_message,
)


async def group_message_candidate(event: Event) -> bool:
    if not isinstance(event, GroupMessageEvent):
        return False
    if int(event.user_id) == int(event.self_id):
        return False
    group_id = int(event.group_id)
    return (
        group_id == CONFIG.target_group_id
        or FEATURES.group_chat_allowed(group_id)
    )


group_matcher = on_message(
    rule=Rule(group_message_candidate),
    priority=8,
    block=True,
)


def has_image(event: GroupMessageEvent) -> bool:
    return any(segment.type == "image" for segment in event.message)


def replied_message_has_image(event: GroupMessageEvent) -> bool:
    reply = event.reply
    if reply is None:
        return False
    return any(segment.type == "image" for segment in reply.message)


@group_matcher.handle()
async def route_group_message(bot: Bot, event: GroupMessageEvent) -> None:
    group_id = int(event.group_id)
    addressed = _is_at_me(event)
    text = _plain_without_at(event)

    if (
        group_id == CONFIG.target_group_id
        and FEATURES.business_allowed(group_id, CONFIG.target_group_id)
        and addressed
        and (text or not (has_image(event) or replied_message_has_image(event)))
        and await handle_business_message(bot, event, text)
    ):
        return

    if not FEATURES.group_chat_allowed(group_id):
        return
    if addressed:
        await send_random_reply(bot, event, text, addressed=True)
        return

    ordinary_text = eligible_text(text, at_bot=False)
    if (ordinary_text is not None or has_image(event)) and should_reply(
        CONFIG.random_chat_probability
    ):
        await send_random_reply(bot, event, ordinary_text or text)
