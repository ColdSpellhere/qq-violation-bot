from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Sequence


async def deliver_replies(
    replies: Sequence[str],
    *,
    send: Callable[[object], Awaitable[None]],
    decorate_final: Callable[[str], object] | None = None,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    after_send: Callable[[str, int], Awaitable[None]] | None = None,
    interval: float = 0.35,
) -> tuple[str, ...]:
    delivered: list[str] = []
    for index, reply in enumerate(replies):
        if index and interval:
            await sleep(interval)
        message: object = reply
        if index == len(replies) - 1 and decorate_final is not None:
            message = decorate_final(reply)
        try:
            await send(message)
        except Exception:
            break
        delivered.append(reply)
        if after_send is not None:
            try:
                await after_send(reply, index)
            except Exception:
                break
    return tuple(delivered)


__all__ = ["deliver_replies"]
