"""Bound SQLite offloading, including work whose async caller was cancelled."""
from __future__ import annotations

import asyncio
import weakref
from concurrent.futures import ThreadPoolExecutor
from functools import partial
from typing import Callable, TypeVar

_Result = TypeVar('_Result')
_executor = ThreadPoolExecutor(max_workers=3, thread_name_prefix='chat-vision-storage')
_gates: weakref.WeakKeyDictionary = weakref.WeakKeyDictionary()
_pending: weakref.WeakKeyDictionary = weakref.WeakKeyDictionary()


async def storage_call(operation: Callable[..., _Result], *args, **kwargs) -> _Result:
    loop = asyncio.get_running_loop()
    reference = _gates.get(loop)
    gate = reference() if reference is not None else None
    if gate is None:
        gate = asyncio.Semaphore(3)
        _gates[loop] = weakref.ref(gate)
    await gate.acquire()
    try:
        future = loop.run_in_executor(_executor, partial(operation, *args, **kwargs))
    except BaseException:
        gate.release()
        raise
    pending = _pending.setdefault(loop, set())
    pending.add(future)

    def finished(completed: asyncio.Future) -> None:
        pending.discard(completed)
        gate.release()
        if not completed.cancelled():
            completed.exception()  # Observe failures even if the caller stopped waiting.

    future.add_done_callback(finished)
    # Cancellation leaves the thread's slot occupied until its completion callback.
    return await asyncio.shield(future)


async def drain_storage_calls() -> None:
    pending = tuple(_pending.get(asyncio.get_running_loop(), ()))
    if pending:
        await asyncio.gather(*(asyncio.shield(future) for future in pending), return_exceptions=True)
