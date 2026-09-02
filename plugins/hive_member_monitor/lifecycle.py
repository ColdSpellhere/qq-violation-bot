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


_services: dict[int, HiveMemberMonitorService] = {}
_reconcile_task: asyncio.Task[None] | None = None
_registered_drivers: weakref.WeakSet[object] = weakref.WeakSet()


def get_service(group_id: int | None = None) -> HiveMemberMonitorService | None:
    if group_id is not None:
        return _services.get(int(group_id))
    if len(_services) == 1:
        return next(iter(_services.values()))
    return None


def build_services(
    *,
    config: Any,
    store: MemberSnapshotStore,
    runtime_enabled: Any,
) -> dict[int, HiveMemberMonitorService]:
    group_ids = tuple(
        int(value)
        for value in getattr(config, "hive_member_monitor_group_ids", ())
        if int(value) > 0
    )
    if not group_ids:
        legacy_group_id = int(
            getattr(config, "hive_member_monitor_group_id", 0)
        )
        group_ids = (legacy_group_id,) if legacy_group_id > 0 else ()
    return {
        group_id: HiveMemberMonitorService(
            config=config,
            store=store,
            output_dir=config.hive_member_monitor_export_dir,
            monitor_group_id=group_id,
            group_label=config.hive_member_monitor_group_label(group_id),
            runtime_enabled=runtime_enabled,
        )
        for group_id in dict.fromkeys(group_ids)
    }


def _runtime_enabled() -> bool:
    return (
        CONFIG.hive_member_monitor_capable
        and CONFIG.hive_member_monitor_enabled
        and FEATURES.snapshot().hive_member_monitor_enabled
    )


async def _sync_service_safely(
    bot: Any, service: HiveMemberMonitorService
) -> None:
    if not _runtime_enabled():
        return
    try:
        count = await service.sync_once(bot)
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        logger.warning(
            "群员同步失败 group=%s label=%s error=%s",
            service.monitor_group_id,
            service.group_label,
            type(exc).__name__,
        )
        return
    logger.info(
        "群员同步完成 group=%s label=%s count=%s",
        service.monitor_group_id,
        service.group_label,
        count,
    )


async def _sync_safely(bot: Any) -> None:
    if not _runtime_enabled():
        return
    for service in tuple(_services.values()):
        await _sync_service_safely(bot, service)


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
        global _reconcile_task
        if not _services:
            store = MemberSnapshotStore(
                CONFIG.hive_member_monitor_database_path
            )
            _services.update(
                build_services(
                    config=CONFIG,
                    store=store,
                    runtime_enabled=_runtime_enabled,
                )
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
            _services.clear()
            return
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        _services.clear()
