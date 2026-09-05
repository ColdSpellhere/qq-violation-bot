"""Owned and bounded image work shared by group and private chat."""
from __future__ import annotations

import asyncio
import weakref
from collections.abc import Awaitable, Callable
from contextlib import asynccontextmanager
from typing import TypeVar

_MAX_CONCURRENT_IMAGES = 3
_gates: weakref.WeakKeyDictionary = weakref.WeakKeyDictionary()
_Result = TypeVar('_Result')


class VisionGateClosed(Exception):
    pass


@asynccontextmanager
async def vision_slot():
    loop = asyncio.get_running_loop()
    reference = _gates.get(loop)
    gate = reference() if reference is not None else None
    if gate is None:
        gate = asyncio.Semaphore(_MAX_CONCURRENT_IMAGES)
        _gates[loop] = weakref.ref(gate)
    async with gate:
        yield


async def run_while_allowed(
    operation: Callable[[], Awaitable[_Result]], *,
    allowed: Callable[[], bool], timeout: float,
) -> _Result:
    """Observe cancellation and gate changes; always join the owned task."""
    if not allowed():
        raise VisionGateClosed()
    if timeout <= 0:
        raise asyncio.TimeoutError()
    task = asyncio.create_task(operation(), name='chat-vision-operation')
    deadline = asyncio.get_running_loop().time() + timeout
    try:
        while True:
            if not allowed():
                raise VisionGateClosed()
            remaining = deadline - asyncio.get_running_loop().time()
            if remaining <= 0:
                raise asyncio.TimeoutError()
            done, _ = await asyncio.wait({task}, timeout=min(.05, remaining))
            if done:
                if not allowed():
                    raise VisionGateClosed()
                return task.result()
    finally:
        if not task.done():
            task.cancel()
        # Retrieving exceptions even when the gate closed prevents unobserved failures.
        await asyncio.gather(task, return_exceptions=True)
