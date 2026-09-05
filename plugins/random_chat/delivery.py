from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable, Sequence
from .delivery_store import DeliveryLedger

logger = logging.getLogger(__name__)


async def deliver_replies(
    replies: Sequence[str],
    *,
    send: Callable[[object], Awaitable[object]],
    decorate_final: Callable[[str], object] | None = None,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    after_send: Callable[[str, int], Awaitable[None]] | None = None,
    interval: float = 0.35,
    ledger: DeliveryLedger | None = None,
    delivery_key: str = "",
    kind: str = "group",
    user_id: str = "",
    group_id: str = "",
    source_message_id: str = "",
    allowed: Callable[[], bool] = lambda: True,
    restore_receipt: Callable[[int, str], None] | None = None,
) -> tuple[str, ...]:
    rows = await asyncio.to_thread(ledger.plan, delivery_key, replies, kind=kind, user_id=user_id, group_id=group_id, source_message_id=source_message_id) if ledger else []
    if ledger and not rows:
        return ()
    if rows:
        replies = tuple(row["reply_text"] for row in rows)
    delivered: list[str] = []
    for index, reply in enumerate(replies):
        row = rows[index] if rows else None
        status = row["status"] if row else "pending"
        if status == "archived":
            delivered.append(reply)
            continue
        if status in {"sending", "unknown", "cancelled"}:
            logger.warning("Chat delivery stopped: key=%s part=%s state=%s", delivery_key[:16], index, status)
            break
        if not allowed():
            break
        if status == "sent":
            if restore_receipt:
                restore_receipt(index, row["receipt"])
            try:
                if after_send:
                    await after_send(reply, index)
                await asyncio.to_thread(ledger.transition, delivery_key, index, before="sent", after="archived", receipt=row["receipt"])
                delivered.append(reply)
            except Exception as exc:
                logger.warning("Chat delivery archive recovery failed: %s", type(exc).__name__)
                break
            continue
        if index and interval:
            await sleep(interval)
        message: object = reply
        if index == len(replies) - 1 and decorate_final is not None:
            message = decorate_final(reply)
        if not allowed():
            break
        if ledger and not await asyncio.to_thread(ledger.claim, delivery_key, index):
            break
        try:
            result = await send(message)
        except BaseException as exc:
            if ledger:
                await asyncio.to_thread(ledger.transition, delivery_key, index, before="sending", after="unknown", error=type(exc).__name__)
            if isinstance(exc, (asyncio.CancelledError, KeyboardInterrupt, SystemExit)):
                raise
            logger.warning("Chat delivery send result unknown: %s", type(exc).__name__)
            break
        receipt_value = result.get("message_id") if isinstance(result, dict) else getattr(result, "message_id", None)
        receipt = str(receipt_value) if receipt_value is not None else ""
        if ledger and not await asyncio.to_thread(ledger.transition, delivery_key, index, before="sending", after="sent", receipt=receipt):
            break
        if restore_receipt:
            restore_receipt(index, receipt)
        delivered.append(reply)
        if after_send is not None:
            try:
                await after_send(reply, index)
            except Exception as exc:
                logger.warning("Chat sent but archive failed: key=%s part=%s error=%s", delivery_key[:16], index, type(exc).__name__)
                break
        if ledger:
            await asyncio.to_thread(ledger.transition, delivery_key, index, before="sent", after="archived", receipt=receipt)
    return tuple(delivered)


__all__ = ["deliver_replies"]
