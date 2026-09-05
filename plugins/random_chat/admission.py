from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from typing import TypeVar

T = TypeVar("T")
_pending: dict[str, int] = {}
logger = logging.getLogger(__name__)


async def run_chat_turn(key: str, operation: Callable[[], Awaitable[T]], *, timeout: float = 90) -> T | None:
    # Admission precedes conversation lock acquisition; no unbounded waiter list.
    if sum(_pending.values()) >= 32 or _pending.get(key, 0) >= 4:
        logger.warning("Chat turn rejected: admission capacity exhausted")
        return None
    _pending[key] = _pending.get(key, 0) + 1
    try:
        return await asyncio.wait_for(operation(), timeout=timeout)
    except asyncio.TimeoutError:
        logger.warning("Chat turn exceeded total deadline")
        return None
    except Exception as exc:
        logger.warning("Chat turn failed safely: %s", type(exc).__name__)
        return None
    finally:
        remaining = _pending[key] - 1
        if remaining:
            _pending[key] = remaining
        else:
            del _pending[key]


__all__ = ["run_chat_turn"]
