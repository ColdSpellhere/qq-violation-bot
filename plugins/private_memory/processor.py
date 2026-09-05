from __future__ import annotations

import asyncio
import hashlib
import sqlite3
from dataclasses import replace
from collections.abc import Awaitable, Callable, Sequence
from contextlib import closing

from plugins.feature_control.runtime import FEATURES
from plugins.member_memory.store import SENSITIVE_RE
from plugins.member_memory.safety import contains_secret as _contains_secret

from .ai import (
    RelationshipCandidate,
    extract_private_facts,
    generate_relationship_candidate,
    summarize_private_conversation,
)
from .models import (
    ConversationScope,
    MemoryJob,
    MemoryJobContinuation,
    PrivateFactCandidate,
    PrivateMessage,
    RelationshipState,
)
from .relationship import RelationshipStore
from .store import PrivateMemoryStore


SummaryCallable = Callable[[str, Sequence[PrivateMessage]], Awaitable[str | None]]
ExtractCallable = Callable[
    [Sequence[PrivateMessage]], Awaitable[tuple[PrivateFactCandidate, ...]]
]
RelationshipCallable = Callable[
    [RelationshipState | None, Sequence[PrivateMessage]],
    Awaitable[RelationshipCandidate | None],
]
Gate = Callable[[], bool]


def _private_memory_enabled() -> bool:
    return bool(FEATURES.snapshot().private_memory_enabled)


def _relationship_enabled() -> bool:
    return bool(FEATURES.snapshot().relationship_state_enabled)


def _background_memory_allowed() -> bool:
    controller_gate = getattr(FEATURES, "background_memory_allowed", None)
    if callable(controller_gate):
        return bool(controller_gate())
    return True


class PrivateMemoryProcessor:
    def __init__(
        self,
        *,
        store: PrivateMemoryStore,
        relationship_store: RelationshipStore,
        summarize: SummaryCallable = summarize_private_conversation,
        extract: ExtractCallable = extract_private_facts,
        update_relationship: RelationshipCallable = generate_relationship_candidate,
        private_memory_enabled: Gate = _private_memory_enabled,
        relationship_enabled: Gate = _relationship_enabled,
        background_memory_allowed: Gate = _background_memory_allowed,
        batch_messages: int = 20,
        batch_chars: int = 12_000,
    ) -> None:
        if type(batch_messages) is not int or batch_messages < 1 or type(batch_chars) is not int or batch_chars < 1:
            raise ValueError("memory batch limits must be positive integers")
        self.batch_messages = batch_messages
        self.batch_chars = batch_chars
        self.store = store
        self.relationship_store = relationship_store
        self.summarize = summarize
        self.extract = extract
        self.update_relationship = update_relationship
        self.private_memory_enabled = private_memory_enabled
        self.relationship_enabled = relationship_enabled
        self.background_memory_allowed = background_memory_allowed

    async def __call__(self, job: MemoryJob) -> bool | MemoryJobContinuation:
        return await self.process(job)

    async def process(self, job: MemoryJob) -> bool | MemoryJobContinuation:
        if job.job_type == "member_facts":
            from plugins.member_memory.processing import process_member_job
            from plugins.violation_record.config import CONFIG
            return await process_member_job(job, path=self.store.path, root=CONFIG.member_memory_root,
                allowed=lambda group_id: FEATURES.group_chat_allowed(group_id) and self.background_memory_allowed(),
                summary_enabled=CONFIG.member_memory_summary_enabled,
                batch_messages=self.batch_messages, batch_chars=self.batch_chars)
        if job.job_type == "private_summary":
            return await self._process_summary(job)
        if job.job_type == "private_facts":
            return await self._process_facts(job)
        if job.job_type == "relationship":
            return await self._process_relationship(job)
        raise ValueError("unknown memory job type")

    async def _process_summary(self, job: MemoryJob) -> bool | MemoryJobContinuation:
        if (
            job.scope.conversation_kind != "private"
            or not self.private_memory_enabled()
            or not self.background_memory_allowed()
        ):
            return False
        current = await asyncio.to_thread(self.store.get_summary, user_id=job.scope.user_id)
        current_version, previous_through = await asyncio.to_thread(self.store.get_summary_version_state,
            user_id=job.scope.user_id
        )
        stale = current_version != job.expected_version
        if stale and (
            current is None
            or current_version < job.expected_version
            or job.input_through_id <= previous_through
        ):
            return False
        expected_version = current_version
        messages = await asyncio.to_thread(self._private_messages,
            job.scope, after=previous_through, through=job.input_through_id, limit=self.batch_messages)
        if not messages:
            return False
        messages = self._bounded_messages(messages)
        end = messages[-1].id
        live_ids = await asyncio.to_thread(self._private_live_interval_ids,
            job.scope, after=previous_through, through=end)
        if not live_ids:
            return False
        if not self.background_memory_allowed():
            return False
        summary = await self.summarize(current.summary_text if current else "", messages)
        if (
            summary is None
            or _contains_secret(summary)
            or not self.private_memory_enabled()
            or not self.background_memory_allowed()
        ):
            return False
        committed = await asyncio.to_thread(self.store.commit_summary,
            user_id=job.scope.user_id,
            summary_text=summary,
            source_start_id=live_ids[0],
            source_end_id=end,
            expected_through_id=previous_through,
            expected_version=expected_version,
            expected_live_ids=live_ids,
        )
        if not committed:
            return False
        return MemoryJobContinuation.MORE if end < job.input_through_id else True

    async def _process_facts(self, job: MemoryJob) -> bool | MemoryJobContinuation:
        if (job.scope.conversation_kind != "private" or not self.private_memory_enabled()
            or not self.background_memory_allowed()):
            return False
        through, version = await asyncio.to_thread(self.store.fact_progress, user_id=job.scope.user_id)
        if through >= job.input_through_id:
            return True
        messages = await asyncio.to_thread(
            self._private_messages, job.scope, after=through,
            through=job.input_through_id, user_only=True, limit=self.batch_messages,
        )
        if not messages:
            # Retention may have removed the entire pending interval.
            return await asyncio.to_thread(self.store.commit_fact_batch,
                user_id=job.scope.user_id, candidates=(), expected_through_id=through,
                expected_version=version, through_id=job.input_through_id, expected_source_ids=())
        messages = self._bounded_messages(messages)
        if not self.background_memory_allowed():
            return False
        candidates = await self.extract(messages)
        if not self.private_memory_enabled() or not self.background_memory_allowed():
            return False
        sources = {message.message_id: message for message in messages}
        valid = []
        for candidate in candidates:
            source = sources.get(candidate.source_message_id)
            if (candidate.user_id != job.scope.user_id or source is None
                or not candidate.source_quote or candidate.source_quote not in source.text
                or SENSITIVE_RE.search(candidate.fact_text) or SENSITIVE_RE.search(candidate.source_quote)
                or _contains_secret(candidate.fact_text) or _contains_secret(candidate.source_quote)):
                continue
            valid.append(candidate)
        end = messages[-1].id
        committed = await asyncio.to_thread(self.store.commit_fact_batch,
            user_id=job.scope.user_id, candidates=valid, expected_through_id=through,
            expected_version=version, through_id=end,
            expected_source_ids=tuple(message.id for message in messages))
        if not committed:
            return False
        return MemoryJobContinuation.MORE if end < job.input_through_id else True

    def _bounded_messages(self, messages: Sequence[PrivateMessage]) -> tuple[PrivateMessage, ...]:
        result = []
        remaining = self.batch_chars
        for message in messages[:self.batch_messages]:
            if remaining <= 0:
                break
            text = message.text[:remaining]
            result.append(replace(message, text=text))
            remaining -= len(text)
        return tuple(result)

    async def _process_relationship(self, job: MemoryJob) -> bool:
        if (
            not self.relationship_enabled()
            or not self.background_memory_allowed()
        ):
            return False
        current = await asyncio.to_thread(self._relationship, job.scope)
        current_version = current.version if current else 0
        current_watermark = current.source_watermark if current else 0
        if current_watermark >= job.input_through_id:
            return True
        if current_version < job.expected_version:
            return False
        if (
            current_version > job.expected_version
            and current is not None
            and current.source_message_id.startswith("governance:")
        ):
            return False
        expected_version = current_version
        if job.scope.conversation_kind == "private":
            messages = await asyncio.to_thread(self._private_messages,
                job.scope,
                after=max(0, (job.input_from_id or job.input_through_id) - 1),
                through=job.input_through_id,
                user_only=True,
            )
        elif job.scope.conversation_kind == "group":
            messages = await asyncio.to_thread(self._group_messages,
                job.scope,
                after=max(0, (job.input_from_id or job.input_through_id) - 1),
                through=job.input_through_id,
            )
        else:
            raise ValueError("unknown conversation kind")
        if not messages or messages[-1].id != job.input_through_id:
            return False
        if not self.background_memory_allowed():
            return False
        messages = messages[-self.batch_messages:]
        share = max(1, self.batch_chars // len(messages))
        messages = tuple(replace(message, text=message.text[:share]) for message in messages)
        candidate = await self.update_relationship(current, messages)
        if (
            candidate is None
            or not self.relationship_enabled()
            or not self.background_memory_allowed()
        ):
            return False
        if any(_contains_secret(text) for text in (
            candidate.state_text, candidate.preferred_address,
            candidate.communication_style, *candidate.open_topics,
        )):
            return False
        source = messages[-1]
        state = RelationshipState(
            id=current.id if current else 0,
            scope=job.scope,
            state_text=candidate.state_text,
            open_topics=candidate.open_topics,
            preferred_address=candidate.preferred_address,
            communication_style=candidate.communication_style,
            source_message_id=source.message_id,
            source_watermark=source.id,
            version=expected_version + 1,
            created_at=current.created_at if current else "",
            updated_at="",
        )
        return await asyncio.to_thread(self.relationship_store.commit, state, expected_version=expected_version)

    def _relationship(self, scope: ConversationScope) -> RelationshipState | None:
        if scope.conversation_kind == "private":
            return self.relationship_store.get_private(
                user_id=scope.user_id, persona_id=scope.persona_id
            )
        if scope.group_id is None:
            raise ValueError("group scope requires group_id")
        return self.relationship_store.get_group(
            group_id=scope.group_id,
            user_id=scope.user_id,
            persona_id=scope.persona_id,
        )

    def _private_messages(
        self,
        scope: ConversationScope,
        *,
        after: int,
        through: int,
        user_only: bool = False,
        limit: int | None = None,
    ) -> tuple[PrivateMessage, ...]:
        direction = " AND message.direction='user'" if user_only else ""
        with closing(sqlite3.connect(self.store.path)) as connection:
            connection.row_factory = sqlite3.Row
            rows = connection.execute(
                """
                SELECT message.id,message.user_id,message.message_id,message.direction,
                       message.text,message.content_hash,message.event_time,
                       message.created_at,message.expires_at,message.purged_at,
                       message.source_kind,message.source_message_id,
                       source.source_kind AS source_user_kind
                FROM private_chat_messages AS message
                LEFT JOIN private_chat_messages AS source
                  ON message.direction='assistant'
                 AND source.user_id=message.user_id
                 AND source.direction='user'
                 AND source.message_id=message.source_message_id
                WHERE message.user_id=? AND message.purged_at IS NULL
                  AND message.id>? AND message.id<=?
                  AND NOT (
                      message.direction='user' AND message.source_kind='image'
                  )
                  AND NOT (
                      message.direction='assistant'
                      AND source.source_kind IN ('image','text_image')
                  )
                """ + direction + " ORDER BY message.id" + (" LIMIT ?" if limit else ""),
                (scope.user_id, after, through, limit) if limit else (scope.user_id, after, through),
            ).fetchall()
        return tuple(
            PrivateMessage(
                id=int(row["id"]), user_id=str(row["user_id"]),
                message_id=str(row["message_id"]), direction=str(row["direction"]),
                text=str(row["text"]), content_hash=str(row["content_hash"]),
                event_time=int(row["event_time"]), created_at=str(row["created_at"]),
                expires_at=str(row["expires_at"]), purged_at=None,
                source_kind=str(row["source_kind"]),
                source_message_id=(
                    str(row["source_message_id"])
                    if row["source_message_id"] is not None else None
                ),
            )
            for row in rows
        )

    def _private_live_interval_ids(
        self, scope: ConversationScope, *, after: int, through: int
    ) -> tuple[int, ...]:
        with closing(sqlite3.connect(self.store.path)) as connection:
            rows = connection.execute(
                """
                SELECT id,purged_at FROM private_chat_messages
                WHERE user_id=? AND id>? AND id<=? ORDER BY id
                """,
                (scope.user_id, after, through),
            ).fetchall()
        if (
            not rows
            or int(rows[-1][0]) != through
        ):
            return ()
        return tuple(int(row[0]) for row in rows if row[1] is None)

    def _group_messages(
        self, scope: ConversationScope, *, after: int, through: int
    ) -> tuple[PrivateMessage, ...]:
        if scope.group_id is None:
            raise ValueError("group scope requires group_id")
        with closing(sqlite3.connect(self.store.path)) as connection:
            connection.row_factory = sqlite3.Row
            rows = connection.execute(
                """
                SELECT rowid,message_id,user_id,plaintext,event_time,created_at
                FROM chat_messages
                WHERE group_id=? AND user_id=? AND rowid>? AND rowid<=?
                  AND trim(plaintext)<>'' AND substr(ltrim(plaintext),1,1)<>'/'
                ORDER BY rowid
                """,
                (scope.group_id, scope.user_id, after, through),
            ).fetchall()
        return tuple(
            PrivateMessage(
                id=int(row["rowid"]), user_id=str(row["user_id"]),
                message_id=str(row["message_id"]), direction="user",
                text=str(row["plaintext"]),
                content_hash=hashlib.sha256(str(row["plaintext"]).encode()).hexdigest(),
                event_time=int(row["event_time"]), created_at=str(row["created_at"]),
                expires_at="", source_kind="group_text",
            )
            for row in rows
        )


__all__ = ["PrivateMemoryProcessor"]
