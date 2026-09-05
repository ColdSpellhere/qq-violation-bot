from __future__ import annotations

import asyncio
import logging
import re
import sqlite3
import uuid
from contextlib import closing
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import AbstractSet, Awaitable, Callable

from .models import ConversationScope, MemoryJob, MemoryJobContinuation, validate_persona_id
from .schema import PRIVATE_MEMORY_SCHEMA_VERSION, schema_version


JobProcessor = Callable[[MemoryJob], Awaitable[bool | None | MemoryJobContinuation]]
AllowedJobTypesProvider = Callable[[], AbstractSet[str]]
logger = logging.getLogger(__name__)

_JOB_TYPES = {"private_summary", "private_facts", "relationship", "member_facts"}
_KINDS = {"private", "group"}
_USER_ID_RE = re.compile(r"[1-9][0-9]*", re.ASCII)
_ERROR_CODE_RE = re.compile(r"[^a-z0-9_]+")


def _utc_text(value: datetime) -> str:
    if value.tzinfo is None:
        raise ValueError("datetime must be timezone-aware")
    return value.astimezone(timezone.utc).replace(microsecond=0).isoformat().replace(
        "+00:00", "Z"
    )


def _now() -> datetime:
    return datetime.now(timezone.utc)


class MemoryJobQueue:
    def __init__(
        self,
        path: Path,
        *,
        lease_seconds: int = 60,
        max_attempts: int = 3,
        backoff_base_seconds: int = 5,
        relationship_debounce_seconds: int = 60,
        relationship_max_wait_seconds: int = 300,
        member_batch_delay_seconds: int = 60,
        member_batch_threshold: int = 5,
    ) -> None:
        self.path = Path(path)
        if schema_version(self.path) != PRIVATE_MEMORY_SCHEMA_VERSION:
            raise RuntimeError("private memory schema version mismatch")
        for name, value in (
            ("lease_seconds", lease_seconds),
            ("max_attempts", max_attempts),
            ("backoff_base_seconds", backoff_base_seconds),
        ):
            if isinstance(value, bool) or not isinstance(value, int) or value < 1:
                raise ValueError(f"{name} must be a positive integer")
        if (type(relationship_debounce_seconds) is not int or type(relationship_max_wait_seconds) is not int
            or relationship_debounce_seconds < 0 or relationship_max_wait_seconds < relationship_debounce_seconds):
            raise ValueError("invalid relationship debounce")
        if type(member_batch_delay_seconds) is not int or member_batch_delay_seconds < 0:
            raise ValueError("invalid member batch delay")
        if type(member_batch_threshold) is not int or member_batch_threshold < 1:
            raise ValueError("invalid member batch threshold")
        self.member_batch_delay_seconds = member_batch_delay_seconds
        self.member_batch_threshold = member_batch_threshold
        self.relationship_debounce_seconds = relationship_debounce_seconds
        self.relationship_max_wait_seconds = relationship_max_wait_seconds
        self.lease_seconds = lease_seconds
        self.max_attempts = max_attempts
        self.backoff_base_seconds = backoff_base_seconds
        self._accepting = True

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA busy_timeout=5000")
        return connection

    @staticmethod
    def _validate_scope(
        *, conversation_kind: str, user_id: str, group_id: int | None
    ) -> None:
        if conversation_kind not in _KINDS:
            raise ValueError("unknown conversation_kind")
        if not isinstance(user_id, str) or _USER_ID_RE.fullmatch(user_id) is None:
            raise ValueError("user_id must be a positive ASCII decimal string")
        if conversation_kind == "private" and group_id is not None:
            raise ValueError("private jobs must not have group_id")
        if conversation_kind == "group" and (
            isinstance(group_id, bool) or not isinstance(group_id, int) or group_id <= 0
        ):
            raise ValueError("group jobs require a positive group_id")

    def start_intake(self) -> None:
        self._accepting = True

    def stop_intake(self) -> None:
        self._accepting = False

    def enqueue(
        self,
        *,
        job_type: str,
        conversation_kind: str,
        user_id: str,
        group_id: int | None,
        input_through_id: int,
        expected_version: int,
        persona_id: str = "radish-cat",
    ) -> int:
        if not self._accepting:
            raise RuntimeError("memory job intake is stopped")
        if job_type not in _JOB_TYPES:
            raise ValueError("unknown memory job type")
        if job_type in {"private_summary", "private_facts"} and conversation_kind != "private":
            raise ValueError(f"{job_type} jobs require private conversation scope")
        if job_type == "member_facts" and conversation_kind != "group":
            raise ValueError("member_facts jobs require group conversation scope")
        self._validate_scope(
            conversation_kind=conversation_kind, user_id=user_id, group_id=group_id
        )
        persona_id = validate_persona_id(persona_id)
        for name, value in (
            ("input_through_id", input_through_id),
            ("expected_version", expected_version),
        ):
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise ValueError(f"{name} must be a non-negative integer")
        now_value = _now()
        now = _utc_text(now_value)
        with closing(self._connect()) as connection:
            try:
                connection.execute("BEGIN IMMEDIATE")
                exact = connection.execute(
                    "SELECT id FROM memory_jobs WHERE status IN ('pending','running') AND job_type=? "
                    "AND conversation_kind=? AND ifnull(group_id,-1)=ifnull(?,-1) AND user_id=? "
                    "AND persona_id=? AND input_through_id=? ORDER BY id LIMIT 1",
                    (job_type,conversation_kind,group_id,user_id,persona_id,input_through_id),
                ).fetchone()
                if exact is not None:
                    connection.commit()
                    return int(exact[0])
                if job_type in {"private_facts", "relationship", "member_facts"}:
                    pending = connection.execute(
                        "SELECT id,input_through_id,created_at,expected_version,input_count FROM memory_jobs "
                        "WHERE status='pending' AND attempts=0 AND job_type=? AND conversation_kind=? "
                        "AND ifnull(group_id,-1)=ifnull(?,-1) AND user_id=? AND persona_id=? ORDER BY id LIMIT 1",
                        (job_type, conversation_kind, group_id, user_id, persona_id),
                    ).fetchone()
                    if pending is not None:
                        if input_through_id > int(pending['input_through_id']):
                            created = datetime.fromisoformat(str(pending['created_at']).replace('Z','+00:00'))
                            due = min(now_value + timedelta(seconds=self.relationship_debounce_seconds),
                                      created + timedelta(seconds=self.relationship_max_wait_seconds)) if job_type == 'relationship' else now_value
                            count = int(pending['input_count']) + 1
                            if job_type == 'member_facts':
                                due = now_value if count >= self.member_batch_threshold else created + timedelta(seconds=self.member_batch_delay_seconds)
                            connection.execute(
                                "UPDATE memory_jobs SET input_through_id=?,input_count=?,next_run_at=?,updated_at=? WHERE id=?",
                                (input_through_id, count, _utc_text(due), now, int(pending['id'])),
                            )
                        connection.commit()
                        return int(pending['id'])
                row = connection.execute(
                    """
                    SELECT id FROM memory_jobs
                    WHERE status IN ('pending','running')
                      AND job_type=? AND conversation_kind=?
                      AND ((group_id IS NULL AND ? IS NULL) OR group_id=?)
                      AND user_id=? AND persona_id=? AND input_through_id=?
                    ORDER BY id LIMIT 1
                    """,
                    (
                        job_type,
                        conversation_kind,
                        group_id,
                        group_id,
                        user_id,
                        persona_id,
                        input_through_id,
                    ),
                ).fetchone()
                if row is None:
                    cursor = connection.execute(
                        """
                        INSERT INTO memory_jobs(
                            job_type,conversation_kind,group_id,user_id,persona_id,
                            input_through_id,expected_version,status,attempts,next_run_at,
                            lease_owner,lease_expires_at,claim_version,error_code,
                            error_summary,created_at,updated_at,input_from_id
                        ) VALUES(?,?,?,?,?,?,?,'pending',0,?,NULL,NULL,0,'','',?,?,?)
                        """,
                        (
                            job_type,
                            conversation_kind,
                            group_id,
                            user_id,
                            persona_id,
                            input_through_id,
                            expected_version,
                            _utc_text(now_value + timedelta(seconds=self.relationship_debounce_seconds)) if job_type == "relationship"
                            else _utc_text(now_value + timedelta(seconds=self.member_batch_delay_seconds)) if job_type == "member_facts" else now,
                            now,
                            now,
                            input_through_id,
                        ),
                    )
                    job_id = int(cursor.lastrowid)
                else:
                    job_id = int(row["id"])
                connection.commit()
                return job_id
            except Exception:
                connection.rollback()
                raise

    def recover_expired_leases(self, *, now: datetime) -> int:
        now_text = _utc_text(now)
        with closing(self._connect()) as connection:
            try:
                connection.execute("BEGIN IMMEDIATE")
                cursor = connection.execute(
                    """
                    UPDATE memory_jobs
                    SET status=CASE WHEN attempts>=? THEN 'failed' ELSE 'pending' END,
                        next_run_at=?,lease_owner=NULL,lease_expires_at=NULL,
                        error_code=CASE WHEN attempts>=? THEN 'lease_expired' ELSE '' END,
                        error_summary='',updated_at=?
                    WHERE status='running' AND lease_expires_at IS NOT NULL
                      AND lease_expires_at<=?
                    """,
                    (self.max_attempts, now_text, self.max_attempts, now_text, now_text),
                )
                connection.commit()
                return int(cursor.rowcount)
            except Exception:
                connection.rollback()
                raise

    def claim(
        self,
        *,
        worker_id: str,
        now: datetime,
        limit: int,
        allowed_job_types: AbstractSet[str],
    ) -> tuple[MemoryJob, ...]:
        if not isinstance(worker_id, str) or not worker_id.strip():
            raise ValueError("worker_id must not be empty")
        if isinstance(limit, bool) or not isinstance(limit, int) or limit < 1:
            raise ValueError("limit must be a positive integer")
        if isinstance(allowed_job_types, (str, bytes)) or not isinstance(
            allowed_job_types, AbstractSet
        ):
            raise ValueError("allowed_job_types must be a set of known job types")
        allowed = frozenset(allowed_job_types)
        if not allowed.issubset(_JOB_TYPES):
            raise ValueError("allowed_job_types contains an unknown job type")
        if not allowed:
            return ()
        now_text = _utc_text(now)
        lease_text = _utc_text(now + timedelta(seconds=self.lease_seconds))
        claimed_ids: list[int] = []
        with closing(self._connect()) as connection:
            try:
                connection.execute("BEGIN IMMEDIATE")
                running_keys = {
                    (
                        str(row["conversation_kind"]),
                        int(row["group_id"]) if row["group_id"] is not None else None,
                        str(row["user_id"]),
                        str(row["persona_id"]),
                    )
                    for row in connection.execute(
                        """SELECT conversation_kind,group_id,user_id,persona_id
                           FROM memory_jobs WHERE status='running'"""
                    )
                }
                seen: set[tuple[str, int | None, str, str]] = set()
                rows = connection.execute(
                    """
                    SELECT * FROM memory_jobs
                    WHERE status='pending' AND attempts<?
                    ORDER BY id
                    """,
                    (self.max_attempts,),
                ).fetchall()
                for row in rows:
                    if str(row["job_type"]) not in allowed:
                        continue
                    key = (
                        str(row["conversation_kind"]),
                        int(row["group_id"]) if row["group_id"] is not None else None,
                        str(row["user_id"]),
                        str(row["persona_id"]),
                    )
                    if key in seen:
                        continue
                    seen.add(key)
                    if key in running_keys or str(row["next_run_at"]) > now_text:
                        continue
                    cursor = connection.execute(
                        """
                        UPDATE memory_jobs
                        SET status='running',attempts=attempts+1,lease_owner=?,
                            lease_expires_at=?,claim_version=claim_version+1,
                            error_code='',error_summary='',updated_at=?
                        WHERE id=? AND status='pending'
                        """,
                        (worker_id, lease_text, now_text, int(row["id"])),
                    )
                    if cursor.rowcount:
                        claimed_ids.append(int(row["id"]))
                        running_keys.add(key)
                    if len(claimed_ids) >= limit:
                        break
                connection.commit()
                return tuple(self.get(job_id) for job_id in claimed_ids)
            except Exception:
                connection.rollback()
                raise

    def finish(
        self,
        job: MemoryJob,
        *,
        worker_id: str,
        status: str,
        error_code: str = "",
        error_summary: str = "",
        retryable: bool = True,
        now: datetime | None = None,
    ) -> bool:
        if status not in {"succeeded", "failed"}:
            raise ValueError("finish status must be succeeded or failed")
        current = now or _now()
        now_text = _utc_text(current)
        final_status = status
        next_run_at = now_text
        safe_code = ""
        safe_summary = ""
        if status == "failed":
            safe_code = _ERROR_CODE_RE.sub("_", str(error_code).strip().lower()).strip("_")[:64]
            safe_code = safe_code or "processing_error"
            safe_summary = "processing failed"
            if retryable and job.attempts < self.max_attempts:
                final_status = "pending"
                delay = self.backoff_base_seconds * (2 ** (job.attempts - 1))
                next_run_at = _utc_text(current + timedelta(seconds=delay))
        with closing(self._connect()) as connection:
            cursor = connection.execute(
                """
                UPDATE memory_jobs
                SET status=?,next_run_at=?,lease_owner=NULL,lease_expires_at=NULL,
                    error_code=?,error_summary=?,updated_at=?
                WHERE id=? AND status='running' AND lease_owner=? AND claim_version=?
                """,
                (
                    final_status,
                    next_run_at,
                    safe_code,
                    safe_summary,
                    now_text,
                    job.id,
                    worker_id,
                    job.claim_version,
                ),
            )
            connection.commit()
            return bool(cursor.rowcount)

    def release_owned(self, *, worker_id: str, now: datetime | None = None) -> int:
        now_text = _utc_text(now or _now())
        with closing(self._connect()) as connection:
            cursor = connection.execute(
                """
                UPDATE memory_jobs
                SET status='pending',next_run_at=?,lease_owner=NULL,lease_expires_at=NULL,
                    error_code='',error_summary='',updated_at=?
                WHERE status='running' AND lease_owner=?
                """,
                (now_text, now_text, worker_id),
            )
            connection.commit()
            return int(cursor.rowcount)

    def defer(
        self, job: MemoryJob, *, worker_id: str, now: datetime | None = None
    ) -> bool:
        """Release one still-owned job after a safe no-op processor result."""
        current = now or _now()
        now_text = _utc_text(current)
        exhausted = job.attempts >= self.max_attempts
        next_run_at = _utc_text(
            current
            + timedelta(
                seconds=self.backoff_base_seconds * (2 ** (job.attempts - 1))
            )
        )
        with closing(self._connect()) as connection:
            cursor = connection.execute(
                """
                UPDATE memory_jobs
                SET status=?,next_run_at=?,lease_owner=NULL,lease_expires_at=NULL,
                    error_code=?,error_summary='',updated_at=?
                WHERE id=? AND status='running' AND lease_owner=? AND claim_version=?
                """,
                (
                    "failed" if exhausted else "pending",
                    now_text if exhausted else next_run_at,
                    "no_domain_write" if exhausted else "",
                    now_text,
                    job.id,
                    worker_id,
                    job.claim_version,
                ),
            )
            connection.commit()
            return bool(cursor.rowcount)

    def renew(self, job: MemoryJob, *, worker_id: str, now: datetime | None = None) -> bool:
        current = now or _now()
        with closing(self._connect()) as connection:
            result = connection.execute(
                "UPDATE memory_jobs SET lease_expires_at=?,updated_at=? "
                "WHERE id=? AND status='running' AND lease_owner=? AND claim_version=?",
                (_utc_text(current + timedelta(seconds=self.lease_seconds)), _utc_text(current),
                 job.id, worker_id, job.claim_version),
            )
            connection.commit()
            return bool(result.rowcount)

    def continue_job(self, job: MemoryJob, *, worker_id: str) -> bool:
        """Yield a bounded successful batch without consuming a retry attempt."""
        now = _utc_text(_now())
        with closing(self._connect()) as connection:
            cursor = connection.execute(
                "UPDATE memory_jobs SET status='pending',attempts=max(0,attempts-1),"
                "next_run_at=?,lease_owner=NULL,lease_expires_at=NULL,updated_at=? "
                "WHERE id=? AND status='running' AND lease_owner=? AND claim_version=?",
                (now, now, job.id, worker_id, job.claim_version),
            )
            connection.commit()
            return bool(cursor.rowcount)

    def get(self, job_id: int) -> MemoryJob:
        with closing(self._connect()) as connection:
            row = connection.execute("SELECT * FROM memory_jobs WHERE id=?", (job_id,)).fetchone()
        if row is None:
            raise KeyError(job_id)
        return self._from_row(row)

    @staticmethod
    def _from_row(row: sqlite3.Row) -> MemoryJob:
        return MemoryJob(
            id=int(row["id"]),
            job_type=str(row["job_type"]),
            scope=ConversationScope(
                conversation_kind=str(row["conversation_kind"]),
                group_id=int(row["group_id"]) if row["group_id"] is not None else None,
                user_id=str(row["user_id"]),
                persona_id=str(row["persona_id"]),
            ),
            input_through_id=int(row["input_through_id"]),
            expected_version=int(row["expected_version"]),
            status=str(row["status"]),
            attempts=int(row["attempts"]),
            next_run_at=str(row["next_run_at"]),
            lease_owner=str(row["lease_owner"]) if row["lease_owner"] is not None else None,
            lease_expires_at=(
                str(row["lease_expires_at"]) if row["lease_expires_at"] is not None else None
            ),
            claim_version=int(row["claim_version"]),
            error_code=str(row["error_code"]),
            error_summary=str(row["error_summary"]),
            created_at=str(row["created_at"]),
            updated_at=str(row["updated_at"]),
            input_from_id=int(row["input_from_id"]),
        )


async def _queue_io(method, *args, **kwargs):
    # SQLite calls cannot be cancelled once their thread starts. Wait for the
    # operation before releasing leases, otherwise a late claim can outlive shutdown.
    task = asyncio.create_task(asyncio.to_thread(method, *args, **kwargs))
    try:
        return await asyncio.shield(task)
    except asyncio.CancelledError:
        await task
        raise


class MemoryJobWorker:
    """Run injected processors within queue ownership boundaries.

    Processors that write domain state must use their store's watermark/version
    compare-and-swap before committing. A false ``finish`` result protects only the
    queue row and cannot undo a stale write after a governance clear.
    """

    def __init__(
        self,
        queue: MemoryJobQueue,
        processor: JobProcessor | None,
        *,
        allowed_job_types: AllowedJobTypesProvider,
        concurrency: int = 2,
        poll_interval: float = 0.25,
        worker_id: str | None = None,
    ) -> None:
        if isinstance(concurrency, bool) or not isinstance(concurrency, int) or concurrency < 1:
            raise ValueError("concurrency must be a positive integer")
        if poll_interval <= 0:
            raise ValueError("poll_interval must be positive")
        self.queue = queue
        self.processor = processor
        self.allowed_job_types = allowed_job_types
        self.concurrency = concurrency
        self.poll_interval = poll_interval
        self.worker_id = worker_id or f"memory-{uuid.uuid4().hex}"
        self._stopping = asyncio.Event()
        self._active: set[asyncio.Task[None]] = set()

    def stop_intake(self) -> None:
        self._stopping.set()

    async def _heartbeat(self, job: MemoryJob) -> None:
        while True:
            await asyncio.sleep(max(0.1, self.queue.lease_seconds / 3))
            try:
                if not await _queue_io(self.queue.renew, job, worker_id=self.worker_id):
                    return
            except Exception as exc:
                logger.warning("memory job lease renewal failed error=%s", type(exc).__name__)

    async def _process(self, job: MemoryJob) -> None:
        heartbeat = asyncio.create_task(self._heartbeat(job))
        try:
            await self._process_owned(job)
        finally:
            heartbeat.cancel()
            await asyncio.gather(heartbeat, return_exceptions=True)

    async def _process_owned(self, job: MemoryJob) -> None:
        try:
            if self.processor is None:
                await _queue_io(self.queue.release_owned, worker_id=self.worker_id)
                return
            processed = await self.processor(job)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            try:
                await _queue_io(self.queue.finish,
                    job,
                    worker_id=self.worker_id,
                    status="failed",
                    error_code=str(getattr(exc, "code", type(exc).__name__)),
                    retryable=bool(getattr(exc, "retryable", True)),
                )
            except Exception as finish_exc:
                logger.warning(
                    "memory job failure finalization failed error=%s",
                    type(finish_exc).__name__,
                )
        else:
            try:
                if processed is MemoryJobContinuation.MORE:
                    await _queue_io(self.queue.continue_job, job, worker_id=self.worker_id)
                elif processed is False:
                    await _queue_io(self.queue.defer, job, worker_id=self.worker_id)
                else:
                    await _queue_io(self.queue.finish, job, worker_id=self.worker_id, status="succeeded")
            except Exception as exc:
                logger.warning(
                    "memory job success finalization failed error=%s",
                    type(exc).__name__,
                )

    async def _wait(self) -> None:
        try:
            await asyncio.wait_for(self._stopping.wait(), timeout=self.poll_interval)
        except asyncio.TimeoutError:
            pass

    async def run(self) -> None:
        try:
            while True:
                completed = {task for task in self._active if task.done()}
                if completed:
                    self._active.difference_update(completed)
                    await asyncio.gather(*completed)
                if self._stopping.is_set():
                    if not self._active:
                        return
                    done, _ = await asyncio.wait(
                        self._active, return_when=asyncio.FIRST_COMPLETED
                    )
                    self._active.difference_update(done)
                    await asyncio.gather(*done)
                    continue
                try:
                    allowed_job_types = frozenset(self.allowed_job_types())
                except Exception as exc:
                    logger.warning(
                        "memory job gate read failed error=%s", type(exc).__name__
                    )
                    await self._wait()
                    continue
                if not allowed_job_types or self.processor is None:
                    await self._wait()
                    continue
                capacity = self.concurrency - len(self._active)
                if capacity > 0:
                    try:
                        claimed = await _queue_io(self.queue.claim,
                            worker_id=self.worker_id,
                            now=_now(),
                            limit=capacity,
                            allowed_job_types=allowed_job_types,
                        )
                    except Exception as exc:
                        logger.warning(
                            "memory job claim failed error=%s", type(exc).__name__
                        )
                        await self._wait()
                        continue
                    for job in claimed:
                        self._active.add(asyncio.create_task(self._process(job)))
                    if claimed:
                        await asyncio.sleep(0)
                        continue
                await self._wait()
        except asyncio.CancelledError:
            for task in self._active:
                task.cancel()
            if self._active:
                await asyncio.gather(*self._active, return_exceptions=True)
            try:
                await _queue_io(self.queue.release_owned, worker_id=self.worker_id)
            except Exception as exc:
                logger.warning(
                    "memory job cancellation release failed error=%s",
                    type(exc).__name__,
                )
            raise


__all__ = ["JobProcessor", "MemoryJobQueue", "MemoryJobWorker"]
