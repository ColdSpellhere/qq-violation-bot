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
from plugins.random_chat.stickers import choose_sticker
from plugins.violation_record.config import CONFIG

from .conversation import PrivateConversation
from .policy import eligible_private_text


CONVERSATIONS: dict[str, PrivateConversation] = {}
_SUMMARY_LIMIT = 1_200
_FACTS_LIMIT = 1_200
_RELATIONSHIP_LIMIT = 600
_TOPIC_LIMIT = 80
_TOPIC_COUNT = 5


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
    for fact in facts:
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
    text = eligible_private_text(event.get_plaintext())
    if text is None:
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
        try:
            user_event_state = conversation.append_user_state(
                current, event_time=int(event.time)
            )
        except Exception as exc:
            logger.warning(f"私聊用户消息持久化失败：{type(exc).__name__}")
            return

        profiles: tuple[MemberProfile, ...] = ()
        relationship = None
        open_topics: tuple[str, ...] = ()
        legacy_profiles: tuple[MemberProfile, ...] | None = None
        if persistent:
            if not _persistent_allowed(user_id):
                return
            assert store is not None
            assert queue is not None
            assert relationship_store is not None
            assert user_event_state is not None
            if not user_event_state.live or (
                not user_event_state.created and user_event_state.assistant_exists
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
            input_through_id = user_event_state.row_id
            try:
                if user_event_state.created:
                    _enqueue_private_jobs(
                        queue=queue,
                        store=store,
                        relationship_store=relationship_store,
                        user_id=user_id,
                        input_through_id=input_through_id,
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
            reply = await generate_reply(
                text,
                context=context,
                current=current,
                profiles=profiles,
                addressed=True,
                chat_mode="private",
                relationship=relationship,
                open_topics=open_topics,
                legacy_profiles=legacy_profiles,
            )
        except RandomChatAIError as exc:
            logger.warning(f"私聊 AI 回复失败：{type(exc).__name__}")
            return
        if not reply:
            return
        if not FEATURES.private_chat_allowed(user_id):
            return
        if persistent and not _persistent_allowed(user_id):
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

        assistant = ContextMessage(
            "萝卜猫",
            reply,
            message_id=f"bot:{event.message_id}",
            user_id=str(event.self_id),
        )
        try:
            conversation.append_assistant(assistant, event_time=int(event.time))
        except Exception as exc:
            logger.warning(f"私聊助手消息持久化失败：{type(exc).__name__}")
