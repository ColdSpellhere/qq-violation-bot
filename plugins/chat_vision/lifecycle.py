from __future__ import annotations

import asyncio
import weakref
from datetime import datetime, timezone

from nonebot import get_driver, logger

from plugins.feature_control.runtime import FEATURES
from plugins.violation_record.config import CONFIG

from .service import (
    cleanup_expired,
    start_workers,
    stop_workers,
    set_store,
)
from .store import ChatVisionStore


_CLEANUP_INTERVAL_SECONDS = 24 * 60 * 60
_cleanup_task: asyncio.Task[None] | None = None
_store: ChatVisionStore | None = None
_registered_drivers: weakref.WeakSet[object] = weakref.WeakSet()


def _now_text() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def _now_timestamp() -> int:
    return int(datetime.now(timezone.utc).timestamp())


async def _cleanup_once(store: ChatVisionStore) -> None:
    try:
        await cleanup_expired(store, CONFIG.chat_vision_root, now_text=_now_text())
    except Exception as exc:
        logger.warning(f"群聊图片清理失败 error={type(exc).__name__}")


async def _daily_cleanup_loop(store: ChatVisionStore) -> None:
    while True:
        await asyncio.sleep(_CLEANUP_INTERVAL_SECONDS)
        await _cleanup_once(store)


def setup_lifecycle() -> None:
    try:
        driver = get_driver()
    except ValueError:
        return
    if driver in _registered_drivers:
        return
    _registered_drivers.add(driver)

    @driver.on_startup
    async def _startup() -> None:
        global _cleanup_task, _store
        if _store is None:
            _store = ChatVisionStore(CONFIG.chat_archive_path)
        store = _store
        set_store(store)

        if CONFIG.chat_vision_enabled:
            start_workers(store)
        await _cleanup_once(store)
        if _cleanup_task is None or _cleanup_task.done():
            _cleanup_task = asyncio.create_task(
                _daily_cleanup_loop(store), name="chat-vision-cleanup"
            )

    @driver.on_shutdown
    async def _shutdown() -> None:
        global _cleanup_task
        await stop_workers()
        task = _cleanup_task
        _cleanup_task = None
        if task is None:
            return
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
