from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Iterable

from nonebot import get_driver, logger, on_message
from nonebot.adapters.onebot.v11 import Bot, Event
from nonebot.exception import FinishedException
from nonebot.rule import Rule

from plugins.feature_control.addressing import addressed_group_admin_message
from plugins.feature_control.runtime import FEATURES
from plugins.violation_record.config import CONFIG

from .commands import (
    MemoryCommandError,
    canonical_memory_command_text,
    is_memory_command,
    parse_memory_command,
)

if TYPE_CHECKING:
    from .service import MemoryGovernanceService


async def is_memory_governance_event(event: Event) -> bool:
    group_id = getattr(event, "group_id", None)
    if group_id is not None and int(group_id) in CONFIG.monitor_only_group_ids:
        return False
    message = addressed_group_admin_message(event)
    if message is None:
        return False
    return is_memory_command(
        canonical_memory_command_text(message)
    )


memory_governance_matcher = on_message(
    rule=Rule(is_memory_governance_event),
    priority=0,
    block=True,
)


def _create_service(
    private_allowed_user_ids: Iterable[str],
) -> "MemoryGovernanceService":
    from .service import MemoryGovernanceService

    return MemoryGovernanceService(
        CONFIG.chat_archive_path,
        private_allowed_user_ids=private_allowed_user_ids,
        member_memory_root=CONFIG.member_memory_root,
    )


async def _send_private_receipt(bot: Bot, *, actor: str, message: str) -> bool:
    try:
        await bot.send_private_msg(user_id=int(actor), message=message)
    except FinishedException:
        raise
    except Exception as exc:
        logger.error(f"记忆治理私聊回执发送失败 error={type(exc).__name__}")
        return False
    return True


@memory_governance_matcher.handle()
async def handle_memory_governance(bot: Bot, event: Event) -> None:
    message = addressed_group_admin_message(event)
    if message is None:
        return
    actor = str(event.user_id)
    superusers = {str(user_id) for user_id in get_driver().config.superusers}
    if actor not in superusers:
        await memory_governance_matcher.finish("你没有记忆治理权限。")
        return

    state = FEATURES.snapshot()
    if not state.memory_governance_enabled:
        await memory_governance_matcher.finish("记忆治理功能已关闭。")
        return

    try:
        command_text = canonical_memory_command_text(
            message
        )
        command = parse_memory_command(
            command_text,
            message,
            group_id=getattr(event, "group_id", None),
            private_allowed_user_ids=state.private_chat_allowed_user_ids,
        )
    except MemoryCommandError:
        await memory_governance_matcher.finish("记忆治理命令格式错误。")
        return
    if command is None:
        await memory_governance_matcher.finish("记忆治理命令格式错误。")
        return

    now = datetime.now(timezone.utc)
    try:
        service = await asyncio.to_thread(_create_service, state.private_chat_allowed_user_ids)
        if command.action == "confirm":
            result = await asyncio.to_thread(service.confirm,
                command.token,
                actor=actor,
                reason=command.reason,
                now=now,
            )
            if not result.success:
                await memory_governance_matcher.finish(
                    "记忆治理操作失败，状态未改变。"
                )
                return
            delivered = await _send_private_receipt(
                bot, actor=actor, message=result.message
            )
            if not delivered:
                if (
                    result.physical_cleanup_complete is False
                    or result.mirror_refresh_complete is False
                ):
                    await memory_governance_matcher.finish(
                        f"{result.message} 私聊回执发送失败。"
                    )
                else:
                    await memory_governance_matcher.finish(
                        "记忆治理变更已提交，但私聊回执发送失败。"
                    )
                return
            await memory_governance_matcher.finish("记忆治理操作结果已私发。")
            return

        if command.action == "cancel":
            result = await asyncio.to_thread(service.cancel, command.token, actor=actor, now=now)
            await memory_governance_matcher.finish(result.message)
            return

        if command.is_write:
            result = await asyncio.to_thread(service.preview, command, actor=actor, now=now)
            receipt = (
                f"{result.preview_text}\n\n操作码：{result.token}\n"
                f"有效期至：{result.expires_at}"
            )
            delivered = await _send_private_receipt(
                bot, actor=actor, message=receipt
            )
            if not delivered:
                await memory_governance_matcher.finish(
                    "记忆治理预览已创建，但私聊回执发送失败。"
                )
                return
            await memory_governance_matcher.finish("记忆治理预览已私发。")
            return

        result = await asyncio.to_thread(service.view, command, actor=actor)
        delivered = await _send_private_receipt(
            bot, actor=actor, message=result.text
        )
        if not delivered:
            await memory_governance_matcher.finish(
                "私聊回执发送失败，未在群内展示。"
            )
            return
        await memory_governance_matcher.finish("记忆治理结果已私发。")
    except FinishedException:
        raise
    except Exception as exc:
        logger.error(f"记忆治理服务异常 error={type(exc).__name__}")
        await memory_governance_matcher.finish("记忆治理服务异常，状态未改变。")


__all__ = [
    "handle_memory_governance",
    "is_memory_governance_event",
    "memory_governance_matcher",
]
