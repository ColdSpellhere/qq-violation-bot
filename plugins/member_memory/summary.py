from __future__ import annotations

from pathlib import Path

from .ai import generate_memory_summary
from .store import commit_summary, pending_summary_batch

SUMMARY_THRESHOLD = 5
SUMMARY_BATCH_LIMIT = 20


async def refresh_member_summary(path: Path, root: Path, *, group_id: int, user_id: str) -> bool:
    refreshed = False
    while True:
        work = pending_summary_batch(
            path,
            group_id=group_id,
            user_id=user_id,
            threshold=SUMMARY_THRESHOLD,
            limit=SUMMARY_BATCH_LIMIT,
        )
        if work is None:
            return refreshed
        text = await generate_memory_summary(work.summary, work.facts)
        if text is None:
            return refreshed
        if not commit_summary(
            path,
            root,
            group_id=group_id,
            user_id=user_id,
            previous_through_id=work.previous_through_id,
            through_fact_id=work.facts[-1].fact_id,
            summary=text,
        ):
            return refreshed
        refreshed = True
