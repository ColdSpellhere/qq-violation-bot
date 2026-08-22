from __future__ import annotations

import asyncio
import os
import weakref
from datetime import datetime, timezone
from pathlib import Path

from nonebot import get_driver, logger

from plugins.feature_control.runtime import FEATURES
from plugins.violation_record.config import BACKUP_DIR, CONFIG

from .jobs import JobProcessor, MemoryJobQueue, MemoryJobWorker
from .schema import (
    PRIVATE_MEMORY_SCHEMA_VERSION,
    migrate,
    online_backup,
    quick_check,
    schema_version,
)
from .store import PrivateMemoryStore


_registered_drivers: weakref.WeakSet[object] = weakref.WeakSet()
_worker_task: asyncio.Task[None] | None = None
_worker: MemoryJobWorker | None = None
_queue: MemoryJobQueue | None = None
_store: PrivateMemoryStore | None = None
_processor: JobProcessor | None = None


def _allowed_job_types() -> frozenset[str]:
    state = FEATURES.snapshot()
    allowed: set[str] = set()
    if state.private_memory_enabled:
        allowed.update(("private_summary", "private_facts"))
    if state.relationship_state_enabled:
        allowed.add("relationship")
    return frozenset(allowed)


def _backup_name(database: Path) -> str:
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    return f"{database.stem}-pre-private-memory-{timestamp}-{os.getpid()}.sqlite3"


def _ensure_schema(database: Path) -> None:
    database = Path(database)
    if not database.exists():
        migrate(database)
        return
    quick_check(database)
    version = schema_version(database)
    if version == PRIVATE_MEMORY_SCHEMA_VERSION:
        return
    if version > PRIVATE_MEMORY_SCHEMA_VERSION:
        raise RuntimeError(
            f"private memory schema is newer than this service: {version}"
        )
    backup_directory = Path(BACKUP_DIR) / "private_memory"
    if not backup_directory.exists():
        backup_directory.mkdir(parents=True, mode=0o700)
        backup_directory.chmod(0o700)
    destination = backup_directory / _backup_name(database)
    backup = online_backup(database, destination)
    quick_check(backup)
    migrate(database)


def setup_lifecycle(
    *, processor: JobProcessor | None = None, poll_interval: float = 0.25
) -> None:
    if processor is not None:
        set_processor(processor)
    try:
        driver = get_driver()
    except ValueError:
        return
    if driver in _registered_drivers:
        return
    _registered_drivers.add(driver)

    @driver.on_startup
    async def _startup() -> None:
        global _queue, _store, _worker, _worker_task
        database = Path(CONFIG.chat_archive_path)
        _ensure_schema(database)
        _queue = MemoryJobQueue(database)
        _queue.start_intake()
        _queue.recover_expired_leases(now=datetime.now(timezone.utc))
        _store = PrivateMemoryStore(
            database, retention_days=CONFIG.private_memory_retention_days
        )
        _store.purge_expired(
            now=datetime.now(timezone.utc),
            retention_days=CONFIG.private_memory_retention_days,
            max_messages=CONFIG.private_memory_max_messages,
        )
        _worker = MemoryJobWorker(
            _queue,
            _processor,
            allowed_job_types=_allowed_job_types,
            concurrency=2,
            poll_interval=poll_interval,
        )
        if _worker_task is None or _worker_task.done():
            _worker_task = asyncio.create_task(
                _worker.run(), name="private-memory-worker"
            )

    @driver.on_shutdown
    async def _shutdown() -> None:
        global _worker_task
        task = _worker_task
        _worker_task = None
        if _queue is not None:
            _queue.stop_intake()
        if _worker is not None:
            _worker.stop_intake()
        if task is None:
            return
        try:
            await asyncio.wait_for(
                asyncio.shield(task), timeout=CONFIG.private_memory_shutdown_timeout
            )
        except asyncio.TimeoutError:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
            logger.warning("私聊记忆后台任务关闭超时，已安全释放待恢复任务")


async def _cancel_for_tests() -> None:
    global _worker_task
    task = _worker_task
    _worker_task = None
    if task is None or task.done():
        return
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass


def _reset_for_tests() -> None:
    global _worker_task, _worker, _queue, _store, _processor, _registered_drivers
    _worker_task = None
    _worker = None
    _queue = None
    _store = None
    _processor = None
    _registered_drivers = weakref.WeakSet()


def set_processor(processor: JobProcessor) -> None:
    if not callable(processor):
        raise TypeError("processor must be callable")
    global _processor
    _processor = processor
    if _worker is not None:
        _worker.processor = processor


__all__ = ["set_processor", "setup_lifecycle"]
