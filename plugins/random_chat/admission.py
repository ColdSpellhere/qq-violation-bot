from __future__ import annotations

import asyncio
import logging
import time
import weakref
from collections.abc import Awaitable, Callable
from contextvars import ContextVar
from dataclasses import dataclass
from typing import TypeVar

T = TypeVar("T")
_pending: dict[str, int] = {}
_draining: set[asyncio.Task] = set()
logger = logging.getLogger(__name__)


@dataclass
class _TurnState:
    deadline: float
    ended: bool = False


_turn: ContextVar[_TurnState | None] = ContextVar("chat_turn", default=None)
_io_gates: weakref.WeakKeyDictionary = weakref.WeakKeyDictionary()


def chat_turn_allowed() -> bool:
    state = _turn.get()
    return state is None or (not state.ended and time.monotonic() < state.deadline)


def check_chat_deadline() -> None:
    if not chat_turn_allowed():
        raise asyncio.TimeoutError("chat turn deadline exceeded")


async def run_chat_io(operation, *args, check_deadline: bool = True, **kwargs):
    """Bound blocking I/O and await its completion before releasing a slot.

    A Python thread cannot be forcibly interrupted. Cancellation waits for the
    submitted operation, then prevents subsequent model/send work. Cleanup may
    opt out of the deadline check to finish a previously started transaction.
    """
    loop = asyncio.get_running_loop()
    gate = _io_gates.setdefault(loop, asyncio.Semaphore(8))
    if check_deadline:
        check_chat_deadline()
    async with gate:
        if check_deadline:
            check_chat_deadline()
        task = asyncio.create_task(asyncio.to_thread(operation, *args, **kwargs))
        try:
            result = await asyncio.shield(task)
        except asyncio.CancelledError:
            # Repeated cancellation must not release the slot while its thread
            # still runs; shielding also preserves the actual claim result.
            while not task.done():
                try:
                    await asyncio.shield(task)
                except asyncio.CancelledError:
                    continue
                except Exception:
                    break
            if not task.cancelled() and task.exception() is not None:
                logger.warning("Cancelled chat I/O completed with error=%s", type(task.exception()).__name__)
            raise
    if check_deadline:
        check_chat_deadline()
    return result


async def run_chat_turn(key: str, operation: Callable[[], Awaitable[T]], *, timeout: float = 90) -> T | None:
    # Admission precedes conversation lock acquisition; no unbounded waiter list.
    if sum(_pending.values()) >= 32 or _pending.get(key, 0) >= 4:
        logger.warning("Chat turn rejected: admission capacity exhausted")
        return None
    _pending[key] = _pending.get(key, 0) + 1
    state = _TurnState(time.monotonic() + max(0, timeout))
    token = _turn.set(state)
    task = asyncio.create_task(operation())

    def release(completed: asyncio.Task) -> None:
        _draining.discard(completed)
        # Consume background exceptions without retaining prompt/output content.
        if not completed.cancelled():
            completed.exception()
        remaining = _pending[key] - 1
        if remaining:
            _pending[key] = remaining
        else:
            del _pending[key]

    try:
        done, _ = await asyncio.wait({task}, timeout=max(0, timeout))
        if not done:
            raise asyncio.TimeoutError("chat turn deadline exceeded")
        result = task.result()
        # A synchronous operation can delay the loop timer, so verify elapsed
        # time even when it completed without yielding.
        check_chat_deadline()
        return result
    except asyncio.TimeoutError:
        logger.warning("Chat turn exceeded total deadline")
        return None
    except Exception as exc:
        logger.warning("Chat turn failed safely: %s", type(exc).__name__)
        return None
    finally:
        state.ended = True
        _turn.reset(token)
        if task.done():
            release(task)
        else:
            # Return on time. Cleanup keeps its conversation, I/O and admission
            # slots until the actual work finishes, and cannot send after ended.
            _draining.add(task)
            task.add_done_callback(release)
            task.cancel()


__all__ = ["run_chat_turn", "run_chat_io", "chat_turn_allowed", "check_chat_deadline"]
