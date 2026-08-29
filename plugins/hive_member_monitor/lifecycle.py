from __future__ import annotations

import asyncio
import weakref
from typing import Any

from nonebot import get_bots, get_driver, logger
from nonebot.adapters.onebot.v11 import Bot

from plugins.feature_control.runtime import FEATURES
from plugins.violation_record.config import CONFIG

from .service import HiveMemberMonitorService
from .store import MemberSnapshotStore


_service: HiveMemberMonitorService | None = None
_reconcile_task: asyncio.Task[None] | None = None
_registered_drivers: weakref.WeakSet[object] = weakref.WeakSet()


def get_service() -> HiveMemberMonitorService | None:
    return _service


def _runtime_enabled() -> bool:
    return (
        CONFIG.hive_member_monitor_capable
        and CONFIG.hive_member_monitor_enabled
        and FEATURES.snapshot().hive_member_monitor_enabled
    )


async def _sync_safely(bot: Any) -> None:
    service = _service
    if service is None or not _runtime_enabled():
        return
    try:
        count = await service.sync_once(bot)
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        logger.warning(
            "蜂巢群员同步失败 group=%s error=%s",
            CONFIG.hive_member_monitor_group_id,
            type(exc).__name__,
        )
        return
    logger.info(
        "蜂巢群员同步完成 group=%s count=%s",
        CONFIG.hive_member_monitor_group_id,
        count,
    )


def _connected_bot() -> Any | None:
    bots = get_bots()
    if CONFIG.bot_self_id and CONFIG.bot_self_id in bots:
        return bots[CONFIG.bot_self_id]
    return next(iter(bots.values()), None)


async def _reconcile_loop() -> None:
    while True:
        await asyncio.sleep(CONFIG.hive_member_monitor_reconcile_seconds)
        bot = _connected_bot()
        if bot is not None:
            await _sync_safely(bot)


def setup_lifecycle() -> None:
    if not (
        CONFIG.hive_member_monitor_capable
        and CONFIG.hive_member_monitor_enabled
    ):
        return
    try:
        driver = get_driver()
    except ValueError:
        return
    if driver in _registered_drivers:
        return
    _registered_drivers.add(driver)

    @driver.on_startup
    async def _startup() -> None:
        global _service, _reconcile_task
        if _service is None:
            _service = HiveMemberMonitorService(
                config=CONFIG,
                store=MemberSnapshotStore(
                    CONFIG.hive_member_monitor_database_path
                ),
                output_dir=CONFIG.hive_member_monitor_export_dir,
                runtime_enabled=_runtime_enabled,
            )
        if _reconcile_task is None or _reconcile_task.done():
            _reconcile_task = asyncio.create_task(
                _reconcile_loop(), name="hive-member-reconcile"
            )

    @driver.on_bot_connect
    async def _bot_connect(bot: Bot) -> None:
        await _sync_safely(bot)

    @driver.on_shutdown
    async def _shutdown() -> None:
        global _reconcile_task
        task = _reconcile_task
        _reconcile_task = None
        if task is None:
            return
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
