from __future__ import annotations

import asyncio
from collections.abc import Callable, Mapping

from nonebot import logger

from .service import ContentAlertService


class AlertDeliveryWorker:
    """Bounded runtime drain, independent of receiving the same message again."""

    def __init__(self, service: ContentAlertService, bots: Callable[[], Mapping],
                 *, interval: float = 2.0):
        self.service = service
        self.bots = bots
        self.interval = max(0.05, interval)
        self._task: asyncio.Task | None = None

    async def tick(self) -> None:
        await asyncio.to_thread(self.service.outbox.recover, float(self.service._clock()))
        if not self.service._runtime_enabled() or not self.service._accepting:
            return
        for bot in tuple(self.bots().values()):
            await self.service.deliver_pending(bot, limit=10)

    async def _run(self) -> None:
        while True:
            try:
                await self.tick()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                # Never log reports, terms, payloads, or upstream exception text.
                logger.error(f"关键词告警后台投递失败 error={type(exc).__name__}")
            await asyncio.sleep(self.interval)

    async def start(self) -> None:
        self.service._accepting = True
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(self._run(), name="content-alert-delivery")

    async def stop(self) -> None:
        self.service._accepting = False
        task, self._task = self._task, None
        if task is None:
            return
        task.cancel()
        try:
            await asyncio.wait_for(task, timeout=5)
        except (asyncio.CancelledError, asyncio.TimeoutError):
            # Persisted in-flight leases are resolved conservatively on restart.
            pass
