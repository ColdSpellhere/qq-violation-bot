from nonebot import logger, on_message
from nonebot.adapters.onebot.v11 import Bot, Event, GroupMessageEvent
from nonebot.rule import Rule

from plugins.chat_archive.db import ContextMessage, archived_message_author, recent_text_context
from plugins.member_memory.ai import extract_memory_candidates
from plugins.member_memory.store import apply_candidates, load_profiles
from plugins.violation_record.config import CONFIG

from .ai import RandomChatAIError, generate_reply
from .policy import eligible_text, is_candidate, should_reply


async def random_chat_candidate(event: Event) -> bool:
    return isinstance(event, GroupMessageEvent) and is_candidate(
        CONFIG.random_chat_enabled,
        CONFIG.target_group_id,
        int(event.group_id),
        int(event.user_id),
        int(event.self_id),
    )


matcher = on_message(rule=Rule(random_chat_candidate), priority=9, block=False)


async def send_random_reply(bot: Bot, event: GroupMessageEvent, text: str) -> None:
    try:
        context = recent_text_context(
            CONFIG.chat_archive_path,
            group_id=int(event.group_id),
            since_epoch=int(event.time) - 1800,
            limit=20,
            exclude_message_id=str(event.message_id),
            bot_user_id=str(event.self_id),
        )
    except Exception as exc:
        logger.warning(f"随机闲聊读取上下文失败：{type(exc).__name__}")
        context = []
    at_user_ids = tuple(
        str(segment.data.get("qq"))
        for segment in event.message
        if segment.type == "at" and str(segment.data.get("qq") or "").isdigit()
    )
    reply_message_id = next(
        (
            str(segment.data.get("id") or segment.data.get("message_id"))
            for segment in event.message
            if segment.type == "reply" and (segment.data.get("id") or segment.data.get("message_id"))
        ),
        None,
    )
    current = ContextMessage(
        event.sender.card or event.sender.nickname or str(event.user_id),
        text,
        message_id=str(event.message_id),
        user_id=str(event.user_id),
        at_user_ids=at_user_ids,
        reply_message_id=reply_message_id,
        replied_to_user_id=archived_message_author(
            CONFIG.chat_archive_path,
            group_id=int(event.group_id),
            message_id=reply_message_id,
        ),
    )
    memory_context = [*context, current]
    profiles = load_profiles(
        CONFIG.chat_archive_path,
        group_id=int(event.group_id),
        user_ids=[item.user_id for item in memory_context],
    )
    try:
        reply = await generate_reply(text, context=context, current=current, profiles=profiles)
    except RandomChatAIError as exc:
        logger.warning(f"随机闲聊 AI 回复失败：{exc}")
        return
    if reply:
        try:
            await bot.send_group_msg(group_id=int(event.group_id), message=reply)
        except Exception as exc:
            logger.warning(f"随机闲聊群消息发送失败：{type(exc).__name__}")
    try:
        candidates = await extract_memory_candidates(memory_context)
        apply_candidates(
            CONFIG.chat_archive_path,
            CONFIG.member_memory_root,
            group_id=int(event.group_id),
            context=memory_context,
            candidates=candidates,
        )
    except Exception as exc:
        logger.warning(f"群友记忆更新失败：{type(exc).__name__}")


@matcher.handle()
async def _(bot: Bot, event: GroupMessageEvent) -> None:
    text_parts: list[str] = []
    at_bot = False
    self_id = str(event.self_id)
    for segment in event.message:
        if segment.type == "at" and str(segment.data.get("qq")) == self_id:
            at_bot = True
        elif segment.type == "text":
            text_parts.append(str(segment.data.get("text", "")))
    text = eligible_text(" ".join(text_parts), at_bot=at_bot)
    if text is None or not should_reply(CONFIG.random_chat_probability):
        return
    await send_random_reply(bot, event, text)
