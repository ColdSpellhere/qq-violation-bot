"""Per-member micro-batching for asynchronous memory analysis."""

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass


MemoryCallback = Callable[[int, str, int], Awaitable[None]]
BatchKey = tuple[int, str]


@dataclass
class _PendingBatch:
    count: int
    latest_event_time: int
    timer: asyncio.Task[None]


class MemberMemoryBatcher:
    def __init__(self, *, threshold: int = 5, delay_seconds: float = 60.0):
        self.threshold = threshold
        self.delay_seconds = delay_seconds
        self._pending: dict[BatchKey, _PendingBatch] = {}
        self._locks: dict[BatchKey, asyncio.Lock] = {}
        self._timer_tasks: set[asyncio.Task[None]] = set()
        self._callback_tasks: set[asyncio.Task[None]] = set()

    def add(
        self,
        *,
        group_id: int,
        user_id: str,
        event_time: int,
        callback: MemoryCallback,
    ) -> None:
        key = (group_id, user_id)
        batch = self._pending.get(key)
        if batch is None:
            timer = asyncio.create_task(self._flush_after_delay(key, callback))
            self._timer_tasks.add(timer)
            timer.add_done_callback(self._timer_tasks.discard)
            batch = _PendingBatch(count=0, latest_event_time=event_time, timer=timer)
            self._pending[key] = batch

        batch.count += 1
        batch.latest_event_time = event_time
        if batch.count >= self.threshold:
            self._pending.pop(key, None)
            batch.timer.cancel()
            self._schedule_callback(key, batch.latest_event_time, callback)

    async def drain(self) -> None:
        while self._timer_tasks or self._callback_tasks:
            tasks = tuple(self._timer_tasks | self._callback_tasks)
            if tasks:
                await asyncio.gather(*tasks, return_exceptions=True)
                self._timer_tasks.difference_update(tasks)
                self._callback_tasks.difference_update(tasks)

    async def _flush_after_delay(self, key: BatchKey, callback: MemoryCallback) -> None:
        await asyncio.sleep(self.delay_seconds)
        batch = self._pending.pop(key, None)
        if batch is not None:
            self._schedule_callback(key, batch.latest_event_time, callback)

    def _schedule_callback(
        self, key: BatchKey, event_time: int, callback: MemoryCallback
    ) -> None:
        task = asyncio.create_task(self._run_callback(key, event_time, callback))
        self._callback_tasks.add(task)
        task.add_done_callback(self._callback_tasks.discard)

    async def _run_callback(
        self, key: BatchKey, event_time: int, callback: MemoryCallback
    ) -> None:
        lock = self._locks.setdefault(key, asyncio.Lock())
        async with lock:
            await callback(key[0], key[1], event_time)
