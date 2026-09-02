from __future__ import annotations

from nonebot import logger, on_notice
from nonebot.adapters.onebot.v11 import (
    Bot,
    Event,
    GroupDecreaseNoticeEvent,
    GroupIncreaseNoticeEvent,
)
from nonebot.rule import Rule

from plugins.feature_control.runtime import FEATURES
from plugins.violation_record.config import CONFIG

from .lifecycle import get_service


def _monitor_enabled() -> bool:
    return (
        CONFIG.hive_member_monitor_capable
        and CONFIG.hive_member_monitor_enabled
        and FEATURES.snapshot().hive_member_monitor_enabled
    )


def _monitored_group(group_id: int) -> bool:
    return int(group_id) in CONFIG.hive_member_monitor_group_ids


def _target_group_decrease(event: Event) -> bool:
    return (
        _monitor_enabled()
        and isinstance(event, GroupDecreaseNoticeEvent)
        and _monitored_group(int(event.group_id))
    )


def _target_group_increase(event: Event) -> bool:
    return (
        _monitor_enabled()
        and isinstance(event, GroupIncreaseNoticeEvent)
        and _monitored_group(int(event.group_id))
        and int(event.user_id) != int(event.self_id)
    )


decrease_matcher = on_notice(
    rule=Rule(_target_group_decrease), priority=1, block=False
)
increase_matcher = on_notice(
    rule=Rule(_target_group_increase), priority=1, block=False
)


@decrease_matcher.handle()
async def monitor_group_decrease(
    bot: Bot, event: GroupDecreaseNoticeEvent
) -> None:
    service = get_service(int(event.group_id))
    if service is None:
        logger.warning(
            "群员监控尚未完成初始化，退群事件已跳过 group=%s",
            event.group_id,
        )
        return
    try:
        await service.handle_group_decrease(
            bot,
            group_id=int(event.group_id),
            user_id=int(event.user_id),
            sub_type=str(event.sub_type),
            event_time=int(event.time),
            operator_id=int(event.operator_id),
        )
    except Exception as exc:
        logger.warning(
            "蜂巢退群通知发送失败 group=%s user=%s error=%s",
            event.group_id,
            event.user_id,
            type(exc).__name__,
        )


@increase_matcher.handle()
async def monitor_group_increase(
    bot: Bot, event: GroupIncreaseNoticeEvent
) -> None:
    service = get_service(int(event.group_id))
    if service is None:
        return
    try:
        await service.handle_group_increase(
            bot,
            group_id=int(event.group_id),
            user_id=int(event.user_id),
        )
    except Exception as exc:
        logger.warning(
            "蜂巢入群成员快照更新失败 group=%s user=%s error=%s",
            event.group_id,
            event.user_id,
            type(exc).__name__,
        )
