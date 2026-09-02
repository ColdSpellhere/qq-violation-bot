from __future__ import annotations

import time

from nonebot import get_driver, logger, on_message
from nonebot.adapters.onebot.v11 import (
    Bot,
    Event,
    GroupMessageEvent,
    Message,
    MessageSegment,
)
from nonebot.rule import Rule

from plugins.feature_control.addressing import addressed_group_admin_message
from plugins.feature_control.runtime import FEATURES
from plugins.violation_record.config import CONFIG

from .catalog import ManagedKeywordCatalog
from .commands import execute_keyword_command, is_keyword_command
from .rules import KeywordRuleStore
from .service import ContentAlertService

RULE_STORE = KeywordRuleStore(CONFIG.content_alert_rules_path)
BACKGROUND_RULE_STORE = KeywordRuleStore(CONFIG.content_alert_background_rules_path)
MANAGED_CATALOG = ManagedKeywordCatalog(CONFIG.content_alert_managed_catalog_path)


def _runtime_enabled() -> bool:
    return (
        CONFIG.content_alert_enabled
        and CONFIG.content_alert_capable
        and FEATURES.snapshot().content_alert_enabled
    )


ALERT_SERVICE = ContentAlertService(
    rule_store=RULE_STORE,
    background_rule_store=BACKGROUND_RULE_STORE,
    managed_catalog=MANAGED_CATALOG,
    source_group_labels={
        int(group_id): CONFIG.hive_member_monitor_group_label(int(group_id))
        for group_id in CONFIG.content_alert_source_group_ids
    },
    report_group_id=CONFIG.content_alert_report_group_id,
    peer_bot_user_ids=CONFIG.peer_bot_user_ids,
    runtime_enabled=_runtime_enabled,
    clock=time.time,
)


async def is_source_alert_event(event: Event) -> bool:
    if not isinstance(event, GroupMessageEvent) or not _runtime_enabled():
        return False
    if int(event.group_id) not in CONFIG.content_alert_source_group_ids:
        return False
    actor = str(event.user_id)
    return actor != str(event.self_id) and actor not in {
        str(user_id) for user_id in CONFIG.peer_bot_user_ids
    }


def extract_keyword_command(event: Event, *, report_group_id: int) -> str | None:
    group_id = getattr(event, "group_id", None)
    if group_id is None:
        message = Message(
            getattr(event, "original_message", None) or getattr(event, "message", ())
        )
    else:
        if int(group_id) != int(report_group_id):
            return None
        message = addressed_group_admin_message(event)
        if message is None:
            return None
    if any(segment.type == "at" for segment in message):
        return None
    text = message.extract_plain_text().strip()
    return text if is_keyword_command(text) else None


async def is_keyword_command_event(event: Event) -> bool:
    return (
        CONFIG.content_alert_enabled
        and CONFIG.content_alert_capable
        and extract_keyword_command(
            event,
            report_group_id=CONFIG.content_alert_report_group_id,
        )
        is not None
    )


keyword_command_matcher = on_message(
    rule=Rule(is_keyword_command_event),
    priority=0,
    block=True,
)
alert_matcher = on_message(
    rule=Rule(is_source_alert_event),
    priority=1,
    block=False,
)


@keyword_command_matcher.handle()
async def handle_keyword_command(event: Event) -> None:
    text = extract_keyword_command(
        event,
        report_group_id=CONFIG.content_alert_report_group_id,
    )
    if text is None:
        return
    actor = str(event.user_id)
    superusers = {str(user_id) for user_id in get_driver().config.superusers}
    if actor not in superusers:
        await keyword_command_matcher.finish("你没有违禁词管理权限。")
        return
    try:
        reply = execute_keyword_command(text, RULE_STORE, actor=actor)
    except Exception as exc:  # noqa: BLE001 - event handlers must fail closed
        logger.error(f"违禁词规则命令失败 error={type(exc).__name__}")
        await keyword_command_matcher.finish("操作失败，规则未改变。")
        return
    await keyword_command_matcher.finish(MessageSegment.text(reply))


@alert_matcher.handle()
async def handle_content_alert(bot: Bot, event: GroupMessageEvent) -> None:
    try:
        await ALERT_SERVICE.handle_event(bot, event)
    except Exception as exc:  # noqa: BLE001 - delivery must not break routing
        logger.warning(
            f"关键词告警发送失败 group={event.group_id} "
            f"message_id={event.message_id} error={type(exc).__name__}"
        )


__all__ = (
    "ALERT_SERVICE",
    "BACKGROUND_RULE_STORE",
    "MANAGED_CATALOG",
    "RULE_STORE",
    "alert_matcher",
    "extract_keyword_command",
    "handle_content_alert",
    "handle_keyword_command",
    "is_keyword_command_event",
    "is_source_alert_event",
    "keyword_command_matcher",
)
