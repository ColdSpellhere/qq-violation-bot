from nonebot import logger, on_message
from nonebot.adapters.onebot.v11 import (
    Bot,
    Event,
    Message,
    MessageSegment,
    PrivateMessageEvent,
)
from nonebot.rule import Rule

from plugins.chat_archive.db import ContextMessage
from plugins.random_chat.ai import RandomChatAIError, generate_reply
from plugins.random_chat.stickers import choose_sticker
from plugins.violation_record.config import CONFIG

from .conversation import PrivateConversation
from .policy import eligible_private_text, is_private_candidate


CONVERSATION = PrivateConversation(limit=20)


async def private_chat_candidate(event: Event) -> bool:
    return isinstance(event, PrivateMessageEvent) and is_private_candidate(
        CONFIG.private_chat_enabled,
        CONFIG.private_chat_allowed_user_id,
        str(event.user_id),
        str(event.self_id),
    )


private_matcher = on_message(
    rule=Rule(private_chat_candidate),
    priority=5,
    block=True,
)


@private_matcher.handle()
async def handle_private_message(bot: Bot, event: PrivateMessageEvent) -> None:
    text = eligible_private_text(event.get_plaintext())
    if text is None:
        return

    async with CONVERSATION.lock:
        context = CONVERSATION.snapshot()
        current = ContextMessage(
            event.sender.nickname or str(event.user_id),
            text,
            message_id=str(event.message_id),
            user_id=str(event.user_id),
        )
        CONVERSATION.append(current)
        try:
            reply = await generate_reply(
                text,
                context=context,
                current=current,
                addressed=True,
                chat_mode="private",
            )
        except RandomChatAIError as exc:
            logger.warning(f"私聊 AI 回复失败：{type(exc).__name__}")
            return
        if not reply:
            return

        try:
            sticker = choose_sticker(
                CONFIG.random_chat_sticker_root,
                special_filename=CONFIG.random_chat_special_sticker,
                attachment_probability=CONFIG.random_chat_sticker_probability,
            )
        except Exception as exc:
            logger.warning(f"私聊表情包选择失败：{type(exc).__name__}")
            sticker = None

        message: str | Message = reply
        if sticker is not None:
            message = Message(reply)
            message += MessageSegment.image(file=f"file://{sticker}")
        try:
            await bot.send_private_msg(user_id=int(event.user_id), message=message)
        except Exception as exc:
            logger.warning(f"私聊消息发送失败：{type(exc).__name__}")
            return

        CONVERSATION.append(
            ContextMessage(
                "萝卜猫",
                reply,
                message_id=f"bot:{event.message_id}",
                user_id=str(event.self_id),
            )
        )
