from __future__ import annotations

import asyncio
import time
from typing import TYPE_CHECKING
import weakref

from nonebot import logger
from nonebot.adapters.onebot.v11 import Bot, GroupMessageEvent, Message, MessageSegment

from plugins.chat_archive.db import (
    ContextMessage,
    archive_payload,
    archived_message_author,
    recent_text_context,
)
from plugins.feature_control.runtime import FEATURES
from plugins.member_memory.store import load_profiles
from plugins.violation_record.config import CONFIG

from .ai import RandomChatAIError, generate_reply
from .context import context_candidate_limit, select_chat_context
from .delivery import deliver_replies
from .stickers import choose_sticker

if TYPE_CHECKING:
    from plugins.chat_vision.client import VisionImage


_RAW_REPLY_MAX_IMAGES = 4
_BUSY_NOTICE = "现在请求有点多，我暂时没接住，过一会儿再叫我吧。"
_GROUP_LOCKS: weakref.WeakValueDictionary[str, asyncio.Lock] = (
    weakref.WeakValueDictionary()
)


def _group_lock(self_id: int, group_id: int) -> asyncio.Lock:
    key = f"{self_id}:{group_id}"
    lock = _GROUP_LOCKS.get(key)
    if lock is None:
        lock = asyncio.Lock()
        _GROUP_LOCKS[key] = lock
    return lock


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


def _reply_sender_user_id(event: GroupMessageEvent) -> str | None:
    if event.reply is None:
        return None
    value = getattr(getattr(event.reply, "sender", None), "user_id", None)
    normalized = str(value or "").strip()
    return normalized if normalized.isdigit() else None


def _quoted_context(
    event: GroupMessageEvent,
    *,
    message_id: str | None,
    author_user_id: str | None,
    image_descriptions: tuple[str, ...],
) -> ContextMessage | None:
    if event.reply is None or not message_id:
        return None
    reply_message = getattr(event.reply, "message", None)
    text = (
        str(reply_message.extract_plain_text()).strip()
        if reply_message is not None and hasattr(reply_message, "extract_plain_text")
        else ""
    )
    if not text and not image_descriptions:
        return None
    sender = getattr(event.reply, "sender", None)
    nickname = str(
        getattr(sender, "card", "")
        or getattr(sender, "nickname", "")
        or author_user_id
        or "被引用消息"
    ).strip()
    at_user_ids = tuple(
        str(segment.data.get("qq"))
        for segment in (reply_message or ())
        if segment.type == "at" and str(segment.data.get("qq") or "").isdigit()
    )
    return ContextMessage(
        nickname=nickname,
        text=text or "[图片]",
        message_id=message_id,
        user_id=author_user_id or "",
        at_user_ids=at_user_ids,
        image_descriptions=image_descriptions,
        is_bot=str(author_user_id or "") == str(event.self_id),
    )


async def send_random_reply(
    bot: Bot,
    event: GroupMessageEvent,
    text: str,
    *,
    addressed: bool = False,
    required: bool = False,
) -> bool:
    async with _group_lock(int(event.self_id), int(event.group_id)):
        return await _send_random_reply_locked(
            bot,
            event,
            text,
            addressed=addressed,
            required=required,
        )


async def _send_random_reply_locked(
    bot: Bot,
    event: GroupMessageEvent,
    text: str,
    *,
    addressed: bool = False,
    required: bool = False,
) -> bool:
    context_limit = getattr(CONFIG, "chat_context_messages", 20)
    try:
        context = recent_text_context(
            CONFIG.chat_archive_path,
            group_id=int(event.group_id),
            since_epoch=int(event.time)
            - (60 * getattr(CONFIG, "chat_context_minutes", 30)),
            limit=context_candidate_limit(context_limit),
            exclude_message_id=str(event.message_id),
            bot_user_id=str(event.self_id),
            include_bot_messages=True,
            peer_bot_user_ids=getattr(CONFIG, "peer_bot_user_ids", ()),
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
    image_allowed = getattr(FEATURES, "image_understanding_allowed", lambda: True)
    image_understanding_enabled = bool(image_allowed())
    current_descriptions: tuple[str, ...] = ()
    referenced_descriptions: tuple[str, ...] = ()
    images: list[VisionImage] = []
    raw_budget_exceeded = False
    if image_understanding_enabled and (current_has_image or reply_message_id):
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
    if not image_understanding_enabled and not stripped_text:
        return False
    if not stripped_text and not bool(image_allowed()):
        return False
    replied_to_user_id = _reply_sender_user_id(event) or archived_message_author(
        CONFIG.chat_archive_path,
        group_id=int(event.group_id),
        message_id=reply_message_id,
    )
    if reply_message_id and not any(
        item.message_id == reply_message_id for item in context
    ):
        quoted = _quoted_context(
            event,
            message_id=reply_message_id,
            author_user_id=replied_to_user_id,
            image_descriptions=referenced_descriptions,
        )
        if quoted is not None:
            context.append(quoted)
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
    context = select_chat_context(
        context,
        limit=context_limit,
        max_self_messages=getattr(CONFIG, "chat_context_self_messages", 3),
        quoted_message_id=reply_message_id,
        current_user_id=current.user_id,
    )
    memory_user_ids = [
        current.user_id,
        *(item for item in (replied_to_user_id, *at_user_ids) if item),
        *(item.user_id for item in reversed(context)),
    ]
    profiles = load_profiles(
        CONFIG.chat_archive_path,
        group_id=int(event.group_id),
        user_ids=memory_user_ids,
        compact=True,
        include_summary=CONFIG.member_memory_summary_enabled,
    )
    relationship = None
    open_topics: tuple[str, ...] = ()
    feature_state = FEATURES.snapshot()
    relationship_prompt_enabled = (
        feature_state.relationship_state_enabled
        and feature_state.prompt_builder_enabled
    )
    if relationship_prompt_enabled:
        try:
            from plugins.private_memory.relationship import RelationshipStore

            relationship = RelationshipStore(CONFIG.chat_archive_path).get_group(
                group_id=int(event.group_id),
                user_id=str(event.user_id),
                persona_id="radish-cat",
            )
            if relationship is not None:
                open_topics = relationship.open_topics
        except Exception as exc:
            logger.warning(
                f"随机闲聊读取关系状态失败：{type(exc).__name__}"
            )
            relationship = None
            open_topics = ()
    if not relationship_prompt_enabled:
        relationship = None
        open_topics = ()
    try:
        reply = await generate_reply(
            current_text,
            context=context,
            current=current,
            profiles=profiles,
            addressed=addressed,
            required_reply=required,
            images=images,
            relationship=relationship,
            open_topics=open_topics,
            max_messages=3 if addressed or required else 1,
            real_text_present=bool(stripped_text),
        )
    except RandomChatAIError as exc:
        logger.warning(f"随机闲聊 AI 回复失败：{type(exc).__name__}")
        if (
            addressed
            and exc.retry_later
            and FEATURES.group_chat_allowed(int(event.group_id))
        ):
            try:
                await bot.send_group_msg(
                    group_id=int(event.group_id), message=_BUSY_NOTICE
                )
                return True
            except Exception as send_exc:
                logger.warning(
                    f"随机闲聊繁忙提示发送失败：{type(send_exc).__name__}"
                )
        return False
    replies = (reply,) if isinstance(reply, str) else tuple(reply or ())
    if replies:
        try:
            sticker = choose_sticker(
                CONFIG.random_chat_sticker_root,
                special_filename=CONFIG.random_chat_special_sticker,
                attachment_probability=CONFIG.random_chat_sticker_probability,
            )
            def decorate(value: str) -> Message:
                message = Message(MessageSegment.text(value))
                if sticker is not None:
                    message += MessageSegment.image(file=f"file://{sticker}")
                return message

            send_results: list[object] = []

            async def send(message: object) -> None:
                if not isinstance(message, Message):
                    message = Message(MessageSegment.text(str(message)))
                result = await bot.send_group_msg(
                    group_id=int(event.group_id), message=message
                )
                send_results.append(result)

            async def archive_reply(value: str, index: int) -> None:
                result = send_results[index] if index < len(send_results) else None
                message_id = (
                    result.get("message_id")
                    if isinstance(result, dict)
                    else getattr(result, "message_id", None)
                )
                if message_id in (None, ""):
                    return
                try:
                    archive_payload(
                        CONFIG.chat_archive_path,
                        int(event.group_id),
                        {
                            "message_id": str(message_id),
                            "group_id": int(event.group_id),
                            "event_time": int(time.time()),
                            "user_id": str(event.self_id),
                            "sender": {"nickname": "机器人自己"},
                            "segments": [
                                {"type": "text", "data": {"text": value}}
                            ],
                            "plaintext": value,
                            "reply_message_id": str(event.message_id),
                        },
                    )
                except Exception as exc:
                    logger.warning(
                        f"随机闲聊归档自身回复失败：{type(exc).__name__}"
                    )

            delivered = await deliver_replies(
                replies[: (3 if addressed or required else 1)],
                send=send,
                decorate_final=decorate,
                after_send=archive_reply,
            )
            return bool(delivered)
        except Exception as exc:
            logger.warning(f"随机闲聊群消息发送失败：{type(exc).__name__}")
    return False
