"""Collect target-group messages for independent member-memory analysis."""

from __future__ import annotations

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
    BATCHER.add(
        group_id=int(event.group_id),
        user_id=str(event.user_id),
        event_time=int(event.time),
        callback=analyze_member_memory,
    )


async def analyze_member_memory(group_id: int, user_id: str, event_time: int) -> None:
    if not FEATURES.group_chat_allowed(group_id):
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
        candidates = await extract_memory_candidates(context)
        member_candidates = [
            item
            for item in candidates
            if str(item.get("user_id") or "") == user_id
        ]
        if not FEATURES.group_chat_allowed(group_id):
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
