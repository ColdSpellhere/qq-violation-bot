from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Callable

from .ai import MemberMemoryError, generate_memory_summary
from .store import commit_summary, pending_summary_batch

SUMMARY_THRESHOLD = 5
SUMMARY_BATCH_LIMIT = 20


async def refresh_member_summary(path: Path, root: Path, *, group_id: int, user_id: str, strict: bool = False, allowed: Callable[[], bool] | None = None, max_batches: int | None = None) -> bool:
    if max_batches is not None and (type(max_batches) is not int or max_batches < 1):
        raise ValueError("max_batches must be positive")
    completed = 0
    refreshed = False
    while True:
        if allowed is not None and not allowed():
            return refreshed
        work = await asyncio.to_thread(
            pending_summary_batch, path,
            group_id=group_id,
            user_id=user_id,
            threshold=SUMMARY_THRESHOLD,
            limit=SUMMARY_BATCH_LIMIT,
        )
        if work is None:
            return refreshed
        text = await generate_memory_summary(work.summary, work.facts)
        if text is None:
            if strict:
                raise MemberMemoryError("member summary failed")
            return refreshed
        if allowed is not None and not allowed():
            return refreshed
        if not await asyncio.to_thread(
            commit_summary, path,
            root,
            group_id=group_id,
            user_id=user_id,
            previous_through_id=work.previous_through_id,
            through_fact_id=work.facts[-1].fact_id,
            summary=text,
            expected_fact_versions=work.fact_versions,
        ):
            if strict:
                raise MemberMemoryError("member summary commit rejected")
            return refreshed
        refreshed = True
        completed += 1
        if max_batches is not None and completed >= max_batches:
            return refreshed
