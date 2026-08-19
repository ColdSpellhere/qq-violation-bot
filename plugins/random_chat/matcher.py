from nonebot import logger, on_message
from nonebot.adapters.onebot.v11 import Bot, Event, GroupMessageEvent, Message, MessageSegment
from nonebot.rule import Rule

from plugins.chat_archive.db import ContextMessage, archived_message_author, recent_text_context
from plugins.member_memory.store import load_profiles
from plugins.violation_record.config import CONFIG

from .ai import RandomChatAIError, generate_reply
from .policy import eligible_text, is_candidate, should_reply
from .stickers import choose_sticker


async def random_chat_candidate(event: Event) -> bool:
    return isinstance(event, GroupMessageEvent) and is_candidate(
        CONFIG.random_chat_enabled,
        CONFIG.target_group_id,
        int(event.group_id),
        int(event.user_id),
        int(event.self_id),
    )


matcher = on_message(rule=Rule(random_chat_candidate), priority=9, block=False)


async def send_random_reply(
    bot: Bot, event: GroupMessageEvent, text: str, *, addressed: bool = False
) -> bool:
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
        compact=True,
        include_summary=CONFIG.member_memory_summary_enabled,
    )
    try:
        reply = await generate_reply(
            text,
            context=context,
            current=current,
            profiles=profiles,
            addressed=addressed,
        )
    except RandomChatAIError as exc:
        logger.warning(f"随机闲聊 AI 回复失败：{exc}")
        return False
    if reply:
        try:
            sticker = choose_sticker(
                CONFIG.random_chat_sticker_root,
                special_filename=CONFIG.random_chat_special_sticker,
                attachment_probability=CONFIG.random_chat_sticker_probability,
            )
            message: str | Message = reply
            if sticker is not None:
                message = Message(reply)
                message += MessageSegment.image(file=f"file://{sticker}")
            await bot.send_group_msg(group_id=int(event.group_id), message=message)
            return True
        except Exception as exc:
            logger.warning(f"随机闲聊群消息发送失败：{type(exc).__name__}")
    return False
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
