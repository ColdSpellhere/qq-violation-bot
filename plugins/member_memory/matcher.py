"""Collect target-group messages for independent member-memory analysis."""

from __future__ import annotations

import sqlite3
from contextlib import closing

from nonebot import logger, on_message
from nonebot.adapters.onebot.v11 import Event, GroupMessageEvent
from nonebot.rule import Rule

from plugins.chat_archive.db import recent_text_context
from plugins.feature_control.runtime import FEATURES
from plugins.member_memory.ai import extract_memory_candidates
from plugins.member_memory.batcher import MemberMemoryBatcher
from plugins.member_memory.store import apply_candidates
from plugins.member_memory.summary import refresh_member_summary
from plugins.violation_record.config import CONFIG


BATCHER = MemberMemoryBatcher(threshold=5, delay_seconds=60.0)


def _background_memory_allowed() -> bool:
    return not bool(getattr(FEATURES.snapshot(), "economy_mode_enabled", False))


def _enqueue_group_relationship(event: GroupMessageEvent) -> None:
    """Queue from the archived row; never invoke relationship AI inline."""
    # This matcher loads before the private-memory NoneBot plugin. Import lazily so
    # Python does not initialize that package before NoneBot can register it.
    from plugins.private_memory.jobs import MemoryJobQueue
    from plugins.private_memory.relationship import RelationshipStore

    group_id = int(event.group_id)
    user_id = str(event.user_id)
    message_id = str(event.message_id)
    with closing(sqlite3.connect(CONFIG.chat_archive_path)) as connection:
        row = connection.execute(
            "SELECT rowid FROM chat_messages "
            "WHERE group_id=? AND user_id=? AND message_id=?",
            (group_id, user_id, message_id),
        ).fetchone()
    if row is None or not FEATURES.snapshot().relationship_state_enabled:
        return
    relationship = RelationshipStore(CONFIG.chat_archive_path).get_group(
        group_id=group_id, user_id=user_id, persona_id="radish-cat"
    )
    if not FEATURES.snapshot().relationship_state_enabled:
        return
    MemoryJobQueue(CONFIG.chat_archive_path).enqueue(
        job_type="relationship",
        conversation_kind="group",
        group_id=group_id,
        user_id=user_id,
        input_through_id=int(row[0]),
        expected_version=relationship.version if relationship else 0,
    )


def _target_member_message(event: Event) -> bool:
    return (
        isinstance(event, GroupMessageEvent)
        and FEATURES.group_chat_allowed(int(event.group_id))
        and int(event.user_id) != int(event.self_id)
    )


memory_matcher = on_message(
    rule=Rule(_target_member_message),
    priority=2,
    block=False,
)


@memory_matcher.handle()
async def collect_member_memory(event: GroupMessageEvent) -> None:
    if not FEATURES.group_chat_allowed(int(event.group_id)):
        return
    text = event.get_plaintext().strip()
    if not text or text.startswith("/"):
        return
    if not _background_memory_allowed():
        return
    BATCHER.add(
        group_id=int(event.group_id),
        user_id=str(event.user_id),
        event_time=int(event.time),
        callback=analyze_member_memory,
    )
    if FEATURES.snapshot().relationship_state_enabled:
        try:
            _enqueue_group_relationship(event)
        except Exception as exc:
            logger.warning(
                "group relationship enqueue failed group_id=%s user_id=%s error=%s",
                event.group_id,
                event.user_id,
                type(exc).__name__,
            )


async def analyze_member_memory(group_id: int, user_id: str, event_time: int) -> None:
    if (
        not FEATURES.group_chat_allowed(group_id)
        or not _background_memory_allowed()
    ):
        return
    try:
        context = recent_text_context(
            CONFIG.chat_archive_path,
            group_id=group_id,
            since_epoch=event_time - 1800,
            limit=20,
            exclude_message_id="",
            bot_user_id=str(CONFIG.bot_self_id),
        )
        if not _background_memory_allowed():
            return
        candidates = await extract_memory_candidates(context)
        member_candidates = [
            item
            for item in candidates
            if str(item.get("user_id") or "") == user_id
        ]
        if (
            not FEATURES.group_chat_allowed(group_id)
            or not _background_memory_allowed()
        ):
            return
        applied = apply_candidates(
            CONFIG.chat_archive_path,
            CONFIG.member_memory_root,
            group_id=group_id,
            context=context,
            candidates=member_candidates,
        )
        if applied > 0 and CONFIG.member_memory_summary_enabled:
            await refresh_member_summary(
                CONFIG.chat_archive_path,
                CONFIG.member_memory_root,
                group_id=group_id,
                user_id=user_id,
            )
    except Exception as exc:
        logger.warning(
            f"群友记忆分析失败 group_id={group_id} user_id={user_id} "
            f"error={type(exc).__name__}"
        )
