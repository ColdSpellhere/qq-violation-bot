from __future__ import annotations

from typing import TYPE_CHECKING

from nonebot import logger
from nonebot.adapters.onebot.v11 import Bot, GroupMessageEvent, Message, MessageSegment

from plugins.chat_archive.db import ContextMessage, archived_message_author, recent_text_context
from plugins.member_memory.store import load_profiles
from plugins.violation_record.config import CONFIG

from .ai import RandomChatAIError, generate_reply
from .stickers import choose_sticker

if TYPE_CHECKING:
    from plugins.chat_vision.client import VisionImage


_RAW_REPLY_MAX_IMAGES = 4


def _reply_message_id(event: GroupMessageEvent) -> str | None:
    if event.reply:
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
    reply_message_id = _reply_message_id(event)
    stripped_text = text.strip()
    current_has_image = any(segment.type == "image" for segment in event.message)
    current_text = stripped_text or ("[图片]" if current_has_image else "")
    current_descriptions: tuple[str, ...] = ()
    referenced_descriptions: tuple[str, ...] = ()
    images: list[VisionImage] = []
    raw_budget_exceeded = False
    if current_has_image or reply_message_id:
        try:
            from plugins.chat_vision.client import VisionImage
            from plugins.chat_vision.store import ChatVisionStore, read_original_image

            store = ChatVisionStore(CONFIG.chat_archive_path)
            assets = []
            seen_asset_ids: set[int] = set()
            for message_id in (str(event.message_id), reply_message_id):
                if message_id is None:
                    continue
                for asset in store.for_message(int(event.group_id), message_id):
                    if asset.id in seen_asset_ids:
                        continue
                    seen_asset_ids.add(asset.id)
                    assets.append(asset)
            current_descriptions = tuple(
                asset.description.strip()
                for asset in assets
                if asset.message_id == str(event.message_id)
                and asset.status == "ready"
                and asset.description
                and asset.description.strip()
            )
            referenced_descriptions = tuple(
                asset.description.strip()
                for asset in assets
                if reply_message_id
                and asset.message_id == reply_message_id
                and asset.status == "ready"
                and asset.description
                and asset.description.strip()
            )
            for asset in assets:
                content = read_original_image(asset, CONFIG.chat_vision_root)
                if content is None or not asset.mime_type:
                    continue
                if (
                    len(images) >= _RAW_REPLY_MAX_IMAGES
                    or sum(len(item.content) for item in images) + len(content)
                    > CONFIG.chat_vision_max_bytes
                ):
                    raw_budget_exceeded = True
                    continue
                images.append(
                    VisionImage(
                        content=content,
                        mime_type=asset.mime_type,
                        message_id=asset.message_id,
                        ordinal=asset.ordinal,
                    )
                )
            if raw_budget_exceeded:
                images.clear()
        except Exception as exc:
            logger.warning(f"随机闲聊读取图片原图失败：{type(exc).__name__}")
    has_current_original = any(
        image.message_id == str(event.message_id) for image in images
    )
    if current_has_image and not has_current_original:
        if raw_budget_exceeded and current_descriptions:
            pass
        elif not stripped_text:
            return False
        else:
            current_descriptions = ()
            images.clear()
    replied_to_user_id = archived_message_author(
        CONFIG.chat_archive_path,
        group_id=int(event.group_id),
        message_id=reply_message_id,
    )
    if referenced_descriptions and not any(
        item.message_id == reply_message_id for item in context
    ):
        context.append(
            ContextMessage(
                replied_to_user_id or "被引用消息",
                "[图片]",
                message_id=reply_message_id or "",
                user_id=replied_to_user_id or "",
                image_descriptions=referenced_descriptions,
            )
        )
    current = ContextMessage(
        event.sender.card or event.sender.nickname or str(event.user_id),
        current_text,
        message_id=str(event.message_id),
        user_id=str(event.user_id),
        at_user_ids=at_user_ids,
        reply_message_id=reply_message_id,
        replied_to_user_id=replied_to_user_id,
        image_descriptions=current_descriptions,
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
            current_text,
            context=context,
            current=current,
            profiles=profiles,
            addressed=addressed,
            images=images,
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
