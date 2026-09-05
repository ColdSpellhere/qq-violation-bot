from dataclasses import replace
import asyncio

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
from plugins.feature_control.runtime import FEATURES
from plugins.member_memory.store import MemberProfile, MemoryTrait
from plugins.private_memory.jobs import MemoryJobQueue
from plugins.private_memory.models import RelationshipState
from plugins.private_memory.relationship import RelationshipStore
from plugins.private_memory.store import PrivateMemoryStore
from plugins.random_chat.ai import RandomChatAIError, generate_reply
from plugins.random_chat.delivery import deliver_replies
from plugins.random_chat.delivery_store import DeliveryLedger, delivery_event_key
from plugins.random_chat.admission import run_chat_turn
from plugins.random_chat.stickers import choose_sticker
from plugins.violation_record.config import CONFIG

from .conversation import PrivateConversation
from .policy import eligible_private_text
from .vision import understand_private_images


CONVERSATIONS: dict[str, PrivateConversation] = {}
_SUMMARY_LIMIT = 1_200
_FACTS_LIMIT = 1_200
_RELATIONSHIP_LIMIT = 600
_TOPIC_LIMIT = 80
_TOPIC_COUNT = 5
_BUSY_NOTICE = "现在请求有点多，我暂时没接住，过一会儿再叫我吧。"


async def private_chat_candidate(event: Event) -> bool:
    return (
        isinstance(event, PrivateMessageEvent)
        and str(event.user_id) != str(event.self_id)
        and FEATURES.private_chat_allowed(str(event.user_id))
    )


private_matcher = on_message(
    rule=Rule(private_chat_candidate),
    priority=5,
    block=True,
)


def _persistent_allowed(user_id: str) -> bool:
    return (
        FEATURES.private_chat_allowed(user_id)
        and FEATURES.snapshot().private_memory_enabled
    )


def _private_profile(
    *,
    store: PrivateMemoryStore,
    user_id: str,
    nickname: str,
) -> tuple[MemberProfile, ...]:
    summary = store.get_summary(user_id=user_id)
    facts = store.active_facts(user_id=user_id, limit=100)

    summary_parts: list[str] = []
    if summary is not None:
        summary_parts.append("私聊摘要：" + summary.summary_text[:_SUMMARY_LIMIT])

    fact_budget = _FACTS_LIMIT
    traits: list[MemoryTrait] = []
    for fact in sorted(facts, key=lambda item: (item.updated_at, item.id), reverse=True):
        if fact_budget <= 0:
            break
        text = fact.fact_text[:fact_budget]
        if not text:
            continue
        traits.append(
            MemoryTrait(
                text=text,
                evidence_message_id=fact.source_message_id,
                updated_at=fact.updated_at,
                fact_id=fact.id,
            )
        )
        fact_budget -= len(text)

    if not summary_parts and not traits:
        return ()
    return (
        MemberProfile(
            group_id=0,
            user_id=user_id,
            nickname=nickname,
            aliases=(),
            traits=tuple(traits),
            updated_at=summary.updated_at if summary is not None else "",
            summary="\n".join(summary_parts),
        ),
    )


def _legacy_private_profiles(
    profiles: tuple[MemberProfile, ...],
    *,
    relationship: RelationshipState | None,
    user_id: str,
    nickname: str,
) -> tuple[MemberProfile, ...]:
    if relationship is None:
        return profiles
    summary_parts: list[str] = []
    if profiles and profiles[0].summary:
        summary_parts.append(profiles[0].summary)
    if relationship.state_text:
        summary_parts.append(
            "关系状态：" + relationship.state_text[:_RELATIONSHIP_LIMIT]
        )
    topics = tuple(
        topic[:_TOPIC_LIMIT] for topic in relationship.open_topics[:_TOPIC_COUNT]
    )
    if topics:
        summary_parts.append("未完话题：" + "；".join(topics))
    base = profiles[0] if profiles else None
    if base is None and not summary_parts:
        return ()
    return (
        MemberProfile(
            group_id=0,
            user_id=user_id,
            nickname=nickname,
            aliases=base.aliases if base else (),
            traits=base.traits if base else (),
            updated_at=relationship.updated_at,
            summary="\n".join(summary_parts),
        ),
    )


def _enqueue_private_jobs(
    *,
    queue: MemoryJobQueue,
    store: PrivateMemoryStore,
    relationship_store: RelationshipStore,
    user_id: str,
    input_through_id: int,
) -> None:
    if _persistent_allowed(user_id):
        summary_version, _ = store.get_summary_version_state(user_id=user_id)
        if _persistent_allowed(user_id):
            queue.enqueue(
                job_type="private_summary",
                conversation_kind="private",
                user_id=user_id,
                group_id=None,
                input_through_id=input_through_id,
                expected_version=summary_version,
            )
        if _persistent_allowed(user_id):
            queue.enqueue(
                job_type="private_facts",
                conversation_kind="private",
                user_id=user_id,
                group_id=None,
                input_through_id=input_through_id,
                expected_version=0,
            )
    if _persistent_allowed(user_id) and FEATURES.snapshot().relationship_state_enabled:
        relationship = relationship_store.get_private(
            user_id=user_id, persona_id="radish-cat"
        )
        if _persistent_allowed(user_id) and FEATURES.snapshot().relationship_state_enabled:
            queue.enqueue(
                job_type="relationship",
                conversation_kind="private",
                user_id=user_id,
                group_id=None,
                input_through_id=input_through_id,
                expected_version=relationship.version if relationship else 0,
            )


@private_matcher.handle()
async def handle_private_message(bot: Bot, event: PrivateMessageEvent) -> None:
    await run_chat_turn(f"private:{event.self_id}:{event.user_id}", lambda: _handle_private_message(bot, event))


async def _handle_private_message(bot: Bot, event: PrivateMessageEvent) -> None:
    plain_text = event.get_plaintext()
    has_image = any(segment.type == "image" for segment in event.message)
    text = eligible_private_text(plain_text, has_image=has_image)
    if text is None:
        return
    real_text = plain_text.strip()
    image_allowed = getattr(FEATURES, "image_understanding_allowed", lambda: True)
    vision_enabled = has_image and bool(
        getattr(CONFIG, "chat_vision_enabled", False)
    ) and bool(image_allowed())
    if has_image and not real_text and not vision_enabled:
        return

    user_id = str(event.user_id)
    if not FEATURES.private_chat_allowed(user_id):
        return
    conversation = CONVERSATIONS.setdefault(
        user_id, PrivateConversation(limit=20, user_id=user_id)
    )
    async with conversation.lock:
        if not FEATURES.private_chat_allowed(user_id):
            return
        conversation.user_id = user_id
        persistent = FEATURES.snapshot().private_memory_enabled
        ledger = await asyncio.to_thread(DeliveryLedger, CONFIG.chat_archive_path) if persistent else None
        delivery_key = delivery_event_key(event.self_id, "private", "", event.user_id, event.message_id)
        saved_parts = await asyncio.to_thread(ledger.parts, delivery_key) if ledger else []
        if saved_parts and all(row["status"] == "archived" for row in saved_parts):
            return
        if any(row["status"] in {"unknown", "sending", "cancelled"} for row in saved_parts):
            logger.warning("私聊投递等待核对 key={}", delivery_key[:16])
            return
        store: PrivateMemoryStore | None = None
        queue: MemoryJobQueue | None = None
        relationship_store: RelationshipStore | None = None
        if persistent:
            try:
                store = PrivateMemoryStore(
                    CONFIG.chat_archive_path,
                    retention_days=CONFIG.private_memory_retention_days,
                )
                queue = MemoryJobQueue(CONFIG.chat_archive_path)
                relationship_store = RelationshipStore(CONFIG.chat_archive_path)
            except Exception as exc:
                logger.warning(f"私聊记忆初始化失败：{type(exc).__name__}")
                return
        conversation.use_store(store)
        context = conversation.snapshot()
        current = ContextMessage(
            event.sender.nickname or str(event.user_id),
            text,
            message_id=str(event.message_id),
            user_id=str(event.user_id),
        )
        if has_image:
            source_kind = "text_image" if real_text else "image"
        else:
            source_kind = "text"
        try:
            user_event_state = conversation.append_user_state(
                current,
                event_time=int(event.time),
                source_kind=source_kind,
            )
        except Exception as exc:
            logger.warning(f"私聊用户消息持久化失败：{type(exc).__name__}")
            return

        if persistent:
            if not _persistent_allowed(user_id):
                return
            assert store is not None
            assert queue is not None
            assert relationship_store is not None
            assert user_event_state is not None
            if not user_event_state.live or (
                not user_event_state.created and user_event_state.assistant_exists and not saved_parts
            ):
                return
            if not user_event_state.created:
                context = tuple(
                    item
                    for item in context
                    if not (
                        item.user_id == user_id
                        and item.message_id == current.message_id
                    )
                )

        def current_event_is_live() -> bool:
            if store is None or user_event_state is None:
                return True
            try:
                return store.user_event_is_live(
                    user_id=user_id,
                    message_id=str(event.message_id),
                    row_id=user_event_state.row_id,
                )
            except Exception as exc:
                logger.warning(
                    f"私聊用户消息存活检查失败：{type(exc).__name__}"
                )
                return False

        images = ()
        descriptions: tuple[str, ...] = ()
        if vision_enabled and not saved_parts:
            try:
                vision = await understand_private_images(
                    event.message,
                    message_id=str(event.message_id),
                    max_bytes=CONFIG.chat_vision_max_bytes,
                    timeout=CONFIG.chat_vision_timeout,
                    base_url=CONFIG.ai_base_url,
                    api_key=CONFIG.ai_api_key,
                    model=CONFIG.chat_vision_model,
                )
                images = vision.images
                descriptions = vision.descriptions
            except Exception as exc:
                logger.warning(f"私聊图片理解失败：{type(exc).__name__}")

        if not FEATURES.private_chat_allowed(user_id):
            return
        if persistent and not _persistent_allowed(user_id):
            return
        if not current_event_is_live():
            return
        if has_image and not real_text and not images and not descriptions and not saved_parts:
            return

        if descriptions:
            current = replace(current, image_descriptions=descriptions)
            conversation.replace_user_turn(current)
            if store is not None:
                try:
                    updated = store.update_user_image_descriptions(
                        user_id=user_id,
                        message_id=str(event.message_id),
                        image_descriptions=descriptions,
                        source_kind=source_kind,
                    )
                    if not updated:
                        logger.warning("私聊图片描述持久化未更新")
                        return
                except Exception as exc:
                    logger.warning(
                        f"私聊图片描述持久化失败：{type(exc).__name__}"
                    )
                    return

        if not FEATURES.private_chat_allowed(user_id):
            return
        if persistent and not _persistent_allowed(user_id):
            return
        if not current_event_is_live():
            return
        if has_image and not real_text and not bool(image_allowed()):
            return

        profiles: tuple[MemberProfile, ...] = ()
        relationship = None
        open_topics: tuple[str, ...] = ()
        legacy_profiles: tuple[MemberProfile, ...] | None = None
        if persistent:
            assert store is not None
            assert queue is not None
            assert relationship_store is not None
            assert user_event_state is not None
            try:
                if user_event_state.created and real_text:
                    _enqueue_private_jobs(
                        queue=queue,
                        store=store,
                        relationship_store=relationship_store,
                        user_id=user_id,
                        input_through_id=user_event_state.row_id,
                    )
                profiles = _private_profile(
                    store=store,
                    user_id=user_id,
                    nickname=current.nickname,
                )
                if FEATURES.snapshot().relationship_state_enabled:
                    relationship = relationship_store.get_private(
                        user_id=user_id, persona_id="radish-cat"
                    )
                    if relationship is not None:
                        open_topics = relationship.open_topics
                legacy_profiles = _legacy_private_profiles(
                    profiles,
                    relationship=relationship,
                    user_id=user_id,
                    nickname=current.nickname,
                )
            except Exception as exc:
                logger.warning(f"私聊记忆任务准备失败：{type(exc).__name__}")
            if not _persistent_allowed(user_id):
                return
            if not FEATURES.snapshot().relationship_state_enabled:
                relationship = None
                open_topics = ()
        try:
            reply = tuple(row["reply_text"] for row in saved_parts) if saved_parts else await generate_reply(
                text,
                context=context,
                current=current,
                profiles=profiles,
                addressed=True,
                chat_mode="private",
                relationship=relationship,
                open_topics=open_topics,
                legacy_profiles=legacy_profiles,
                max_messages=3,
                images=images,
                real_text_present=bool(real_text),
            )
        except RandomChatAIError as exc:
            logger.warning(f"私聊 AI 回复失败：{type(exc).__name__}")
            can_send_notice = (
                exc.retry_later
                and FEATURES.private_chat_allowed(user_id)
                and (not persistent or _persistent_allowed(user_id))
                and current_event_is_live()
            )
            if can_send_notice:
                try:
                    await bot.send_private_msg(
                        user_id=int(event.user_id), message=_BUSY_NOTICE
                    )
                except Exception as send_exc:
                    logger.warning(
                        f"私聊繁忙提示发送失败：{type(send_exc).__name__}"
                    )
            return
        replies = (reply,) if isinstance(reply, str) else tuple(reply or ())
        if not replies:
            return
        if not FEATURES.private_chat_allowed(user_id):
            return
        if persistent and not _persistent_allowed(user_id):
            return
        if not current_event_is_live():
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

        def decorate(value: str) -> Message:
            message = Message(MessageSegment.text(value))
            if sticker is not None:
                message += MessageSegment.image(file=f"file://{sticker}")
            return message

        async def persist(value: str, index: int) -> None:
            if not current_event_is_live():
                raise RuntimeError("private message was cleared")
            assistant = ContextMessage(
                "机器人自己",
                value,
                message_id=f"bot:{event.message_id}:{index + 1}",
                user_id=str(event.self_id),
                is_bot=True,
            )
            conversation.append_assistant(assistant, event_time=int(event.time))

        async def send(message: object) -> object:
            if not FEATURES.private_chat_allowed(user_id):
                raise RuntimeError("private chat access changed")
            if persistent and not _persistent_allowed(user_id):
                raise RuntimeError("private memory access changed")
            if not current_event_is_live():
                raise RuntimeError("private message was cleared")
            if not isinstance(message, Message):
                message = Message(MessageSegment.text(str(message)))
            return await bot.send_private_msg(user_id=int(event.user_id), message=message)

        delivered = await deliver_replies(
            replies[:3],
            send=send,
            decorate_final=decorate,
            after_send=persist,
            ledger=ledger,
            delivery_key=delivery_key,
            kind="private",
            user_id=user_id,
            source_message_id=str(event.message_id),
            allowed=lambda: (
                FEATURES.private_chat_allowed(user_id)
                and (not persistent or _persistent_allowed(user_id))
                and current_event_is_live()
            ),
        )
        if not delivered:
            logger.warning("私聊消息发送失败")
