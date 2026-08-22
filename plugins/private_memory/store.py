from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import unicodedata
from contextlib import closing
from datetime import datetime, timedelta, timezone
from pathlib import Path

from plugins.chat_archive.db import ContextMessage

from .models import (
    ClearReport,
    PrivateFact,
    PrivateFactCandidate,
    PrivateSummary,
    PurgeReport,
)
from .schema import PRIVATE_MEMORY_SCHEMA_VERSION, schema_version


_USER_ID_RE = re.compile(r"[1-9][0-9]*", re.ASCII)
_SOURCE_QUOTE_LIMIT = 120


def _validate_user_id(user_id: str) -> str:
    if not isinstance(user_id, str) or _USER_ID_RE.fullmatch(user_id) is None:
        raise ValueError("user_id must be a positive ASCII decimal string")
    return user_id


def _validate_nonempty(value: str, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field} must not be empty")
    return value


def _normalize_text(text: str) -> str:
    if not isinstance(text, str):
        raise TypeError("text must be a string")
    return unicodedata.normalize("NFC", text)


def _normalize_compact(text: str) -> str:
    return " ".join(_normalize_text(text).split())


def _utc_text(value: datetime) -> str:
    if value.tzinfo is None:
        raise ValueError("datetime must be timezone-aware")
    normalized = value.astimezone(timezone.utc).replace(microsecond=0)
    return normalized.isoformat().replace("+00:00", "Z")


def _now_text() -> str:
    return _utc_text(datetime.now(timezone.utc))


def _epoch_text(epoch: int) -> str:
    return _utc_text(datetime.fromtimestamp(epoch, timezone.utc))


def _checkpoint_truncate(path: Path) -> bool:
    try:
        with closing(sqlite3.connect(path, timeout=0.1)) as connection:
            row = connection.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
        return row is not None and int(row[0]) == 0
    except sqlite3.Error:
        return False


class PrivateMemoryStore:
    def __init__(self, path: Path, *, retention_days: int = 30):
        self.path = Path(path)
        current_version = schema_version(self.path)
        if current_version != PRIVATE_MEMORY_SCHEMA_VERSION:
            raise RuntimeError(
                "private memory schema version mismatch: "
                f"expected {PRIVATE_MEMORY_SCHEMA_VERSION}, got {current_version}"
            )
        if (
            isinstance(retention_days, bool)
            or not isinstance(retention_days, int)
            or retention_days < 1
        ):
            raise ValueError("retention_days must be a positive integer")
        self.retention_days = retention_days

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("PRAGMA busy_timeout=5000")
        return connection

    def append_user_message(
        self,
        *,
        user_id: str,
        message_id: str,
        text: str,
        event_time: int,
        source_kind: str,
    ) -> int:
        return self._append_message(
            user_id=_validate_user_id(user_id),
            message_id=_validate_nonempty(message_id, "message_id"),
            direction="user",
            text=text,
            event_time=event_time,
            source_kind=_validate_nonempty(source_kind, "source_kind"),
            source_message_id=None,
            require_live_user_source=False,
        )

    def append_assistant_message(
        self,
        *,
        user_id: str,
        source_message_id: str,
        bot_user_id: str,
        text: str,
        event_time: int,
    ) -> int:
        user_id = _validate_user_id(user_id)
        source_message_id = _validate_nonempty(source_message_id, "source_message_id")
        bot_user_id = _validate_user_id(bot_user_id)
        return self._append_message(
            user_id=user_id,
            message_id=f"assistant:{source_message_id}",
            direction="assistant",
            text=text,
            event_time=event_time,
            source_kind=f"bot:{bot_user_id}",
            source_message_id=source_message_id,
            require_live_user_source=True,
        )

    def _append_message(
        self,
        *,
        user_id: str,
        message_id: str,
        direction: str,
        text: str,
        event_time: int,
        source_kind: str,
        source_message_id: str | None,
        require_live_user_source: bool,
    ) -> int:
        if isinstance(event_time, bool) or not isinstance(event_time, int) or event_time < 0:
            raise ValueError("event_time must be a non-negative integer")
        normalized = _normalize_text(text)
        content_hash = hashlib.sha256(normalized.encode("utf-8")).hexdigest()
        created_at = _now_text()
        expires_at = _epoch_text(event_time + (self.retention_days * 86_400))
        with closing(self._connect()) as connection:
            try:
                connection.execute("BEGIN IMMEDIATE")
                existing = connection.execute(
                    """
                    SELECT id FROM private_chat_messages
                    WHERE user_id=? AND direction=? AND message_id=?
                    """,
                    (user_id, direction, message_id),
                ).fetchone()
                if existing is not None:
                    connection.commit()
                    return int(existing["id"])
                if require_live_user_source:
                    source = connection.execute(
                        """
                        SELECT 1 FROM private_chat_messages
                        WHERE user_id=? AND direction='user' AND message_id=?
                          AND purged_at IS NULL
                        """,
                        (user_id, source_message_id),
                    ).fetchone()
                    if source is None:
                        raise ValueError(
                            "assistant message requires a live source user message "
                            "for the same user"
                        )
                connection.execute(
                    """
                    INSERT INTO private_chat_messages(
                        user_id,message_id,direction,text,content_hash,event_time,
                        created_at,expires_at,purged_at,source_kind,source_message_id
                    ) VALUES(?,?,?,?,?,?,?,?,NULL,?,?)
                    ON CONFLICT(user_id,direction,message_id) DO NOTHING
                    """,
                    (
                        user_id,
                        message_id,
                        direction,
                        normalized,
                        content_hash,
                        event_time,
                        created_at,
                        expires_at,
                        source_kind,
                        source_message_id,
                    ),
                )
                row = connection.execute(
                    """
                    SELECT id FROM private_chat_messages
                    WHERE user_id=? AND direction=? AND message_id=?
                    """,
                    (user_id, direction, message_id),
                ).fetchone()
                if row is None:
                    raise sqlite3.DatabaseError("message insert did not produce a row")
                connection.commit()
                return int(row["id"])
            except Exception:
                connection.rollback()
                raise

    def recent_context(self, *, user_id: str, limit: int) -> tuple[ContextMessage, ...]:
        user_id = _validate_user_id(user_id)
        if isinstance(limit, bool) or not isinstance(limit, int):
            raise ValueError("limit must be an integer")
        if limit <= 0:
            return ()
        with closing(self._connect()) as connection:
            rows = connection.execute(
                """
                SELECT message_id,direction,text,source_kind
                FROM private_chat_messages
                WHERE user_id=? AND purged_at IS NULL
                ORDER BY event_time DESC,id DESC
                LIMIT ?
                """,
                (user_id, limit),
            ).fetchall()
        rows.reverse()
        return tuple(
            ContextMessage(
                nickname="萝卜猫" if row["direction"] == "assistant" else user_id,
                text=str(row["text"]),
                message_id=str(row["message_id"]),
                user_id=(
                    str(row["source_kind"])[4:]
                    if row["direction"] == "assistant"
                    and str(row["source_kind"]).startswith("bot:")
                    else user_id
                ),
            )
            for row in rows
        )

    def get_summary(self, *, user_id: str) -> PrivateSummary | None:
        user_id = _validate_user_id(user_id)
        with closing(self._connect()) as connection:
            row = connection.execute(
                """
                SELECT user_id,summary_text,source_start_id,source_end_id,
                       summarized_through_id,version,created_at,updated_at
                FROM private_conversation_summaries WHERE user_id=?
                """,
                (user_id,),
            ).fetchone()
        if row is None or not str(row["summary_text"]):
            return None
        return PrivateSummary(
            user_id=str(row["user_id"]),
            summary_text=str(row["summary_text"]),
            source_start_id=int(row["source_start_id"]),
            source_end_id=int(row["source_end_id"]),
            summarized_through_id=int(row["summarized_through_id"]),
            version=int(row["version"]),
            created_at=str(row["created_at"]),
            updated_at=str(row["updated_at"]),
        )

    def get_summary_version_state(self, *, user_id: str) -> tuple[int, int]:
        user_id = _validate_user_id(user_id)
        with closing(self._connect()) as connection:
            row = connection.execute(
                """
                SELECT version,summarized_through_id
                FROM private_conversation_summaries WHERE user_id=?
                """,
                (user_id,),
            ).fetchone()
        if row is None:
            return (0, 0)
        return (int(row["version"]), int(row["summarized_through_id"]))

    def commit_summary(
        self,
        *,
        user_id: str,
        summary_text: str,
        source_start_id: int,
        source_end_id: int,
        expected_through_id: int,
        expected_version: int,
    ) -> bool:
        user_id = _validate_user_id(user_id)
        summary_text = _normalize_compact(summary_text)
        if not summary_text:
            raise ValueError("summary_text must not be empty")
        values = (source_start_id, source_end_id, expected_through_id, expected_version)
        if any(isinstance(value, bool) or not isinstance(value, int) or value < 0 for value in values):
            raise ValueError("summary ids and versions must be non-negative integers")
        if source_start_id <= 0 or source_end_id < source_start_id:
            raise ValueError("summary source range is invalid")
        if source_end_id <= expected_through_id:
            raise ValueError("summary watermark must advance")
        now = _now_text()
        with closing(self._connect()) as connection:
            try:
                connection.execute("BEGIN IMMEDIATE")
                current = connection.execute(
                    """
                    SELECT summarized_through_id,version
                    FROM private_conversation_summaries WHERE user_id=?
                    """,
                    (user_id,),
                ).fetchone()
                current_through = int(current["summarized_through_id"]) if current else 0
                current_version = int(current["version"]) if current else 0
                if (current_through, current_version) != (
                    expected_through_id,
                    expected_version,
                ):
                    connection.rollback()
                    return False
                live_rows = connection.execute(
                    """
                    SELECT id FROM private_chat_messages
                    WHERE user_id=? AND purged_at IS NULL AND id>?
                    ORDER BY id
                    """,
                    (user_id, expected_through_id),
                ).fetchall()
                live_ids = [int(row["id"]) for row in live_rows]
                if (
                    not live_ids
                    or live_ids[0] != source_start_id
                    or source_end_id not in live_ids
                ):
                    connection.rollback()
                    return False
                if current is None:
                    connection.execute(
                        """
                        INSERT INTO private_conversation_summaries(
                            user_id,summary_text,source_start_id,source_end_id,
                            summarized_through_id,version,created_at,updated_at
                        ) VALUES(?,?,?,?,?,1,?,?)
                        """,
                        (
                            user_id,
                            summary_text,
                            source_start_id,
                            source_end_id,
                            source_end_id,
                            now,
                            now,
                        ),
                    )
                else:
                    cursor = connection.execute(
                        """
                        UPDATE private_conversation_summaries
                        SET summary_text=?,source_start_id=?,source_end_id=?,
                            summarized_through_id=?,version=version+1,updated_at=?
                        WHERE user_id=? AND summarized_through_id=? AND version=?
                        """,
                        (
                            summary_text,
                            source_start_id,
                            source_end_id,
                            source_end_id,
                            now,
                            user_id,
                            expected_through_id,
                            expected_version,
                        ),
                    )
                    if cursor.rowcount != 1:
                        connection.rollback()
                        return False
                connection.commit()
                return True
            except Exception:
                connection.rollback()
                raise

    def append_fact(
        self,
        candidate: PrivateFactCandidate,
        *,
        trust_level: str = "ai_extracted",
    ) -> int | None:
        user_id = _validate_user_id(candidate.user_id)
        fact_text = _normalize_compact(candidate.fact_text)
        source_message_id = _validate_nonempty(
            candidate.source_message_id, "source_message_id"
        )
        source_quote = _normalize_compact(candidate.source_quote)[:_SOURCE_QUOTE_LIMIT]
        if not fact_text or not source_quote:
            return None
        if trust_level not in {"ai_extracted", "admin_confirmed"}:
            raise ValueError("invalid trust_level")
        normalized_text = fact_text.casefold()
        now = _now_text()
        with closing(self._connect()) as connection:
            try:
                connection.execute("BEGIN IMMEDIATE")
                existing = connection.execute(
                    """
                    SELECT id,trust_level FROM private_memory_facts
                    WHERE user_id=? AND normalized_text=? AND source_message_id=?
                    """,
                    (user_id, normalized_text, source_message_id),
                ).fetchone()
                if existing is not None:
                    if (
                        str(existing["trust_level"]) == "ai_extracted"
                        and trust_level == "admin_confirmed"
                    ):
                        connection.execute(
                            """
                            UPDATE private_memory_facts
                            SET fact_text=?,source_quote=?,trust_level='admin_confirmed',
                                version=version+1,updated_at=?
                            WHERE id=? AND trust_level='ai_extracted'
                            """,
                            (fact_text, source_quote, now, int(existing["id"])),
                        )
                    connection.commit()
                    return int(existing["id"])
                governance_source = source_message_id.startswith("governance:")
                if not (trust_level == "admin_confirmed" and governance_source):
                    source = connection.execute(
                        """
                        SELECT 1 FROM private_chat_messages
                        WHERE user_id=? AND direction='user' AND message_id=?
                          AND purged_at IS NULL
                        """,
                        (user_id, source_message_id),
                    ).fetchone()
                    if source is None:
                        raise ValueError(
                            "fact requires a live source user message for the same user"
                        )
                connection.execute(
                    """
                    INSERT INTO private_memory_facts(
                        user_id,fact_text,normalized_text,source_message_id,source_quote,
                        trust_level,status,supersedes_id,version,created_at,updated_at,deleted_at
                    ) VALUES(?,?,?,?,?,?,'active',NULL,1,?,?,NULL)
                    """,
                    (
                        user_id,
                        fact_text,
                        normalized_text,
                        source_message_id,
                        source_quote,
                        trust_level,
                        now,
                        now,
                    ),
                )
                row = connection.execute(
                    """
                    SELECT id FROM private_memory_facts
                    WHERE user_id=? AND normalized_text=? AND source_message_id=?
                    """,
                    (user_id, normalized_text, source_message_id),
                ).fetchone()
                connection.commit()
                return int(row["id"]) if row else None
            except Exception:
                connection.rollback()
                raise

    def active_facts(self, *, user_id: str, limit: int) -> tuple[PrivateFact, ...]:
        user_id = _validate_user_id(user_id)
        if isinstance(limit, bool) or not isinstance(limit, int):
            raise ValueError("limit must be an integer")
        if limit <= 0:
            return ()
        with closing(self._connect()) as connection:
            rows = connection.execute(
                """
                SELECT id,user_id,fact_text,source_message_id,source_quote,trust_level,
                       status,supersedes_id,version,created_at,updated_at,deleted_at
                FROM private_memory_facts
                WHERE user_id=? AND status='active'
                ORDER BY id DESC LIMIT ?
                """,
                (user_id, limit),
            ).fetchall()
        rows.reverse()
        return tuple(self._fact_from_row(row) for row in rows)

    def purge_expired(
        self,
        *,
        now: datetime,
        retention_days: int,
        max_messages: int,
    ) -> PurgeReport:
        if (
            isinstance(retention_days, bool)
            or not isinstance(retention_days, int)
            or retention_days < 1
        ):
            raise ValueError("retention_days must be a positive integer")
        if (
            isinstance(max_messages, bool)
            or not isinstance(max_messages, int)
            or max_messages < 1
        ):
            raise ValueError("max_messages must be a positive integer")
        if now.tzinfo is None:
            raise ValueError("now must be timezone-aware")
        now_utc = now.astimezone(timezone.utc)
        cutoff = int((now_utc - timedelta(days=retention_days)).timestamp())
        purged_at = _utc_text(now_utc)
        with closing(self._connect()) as connection:
            try:
                connection.execute("PRAGMA secure_delete=ON")
                connection.execute("BEGIN IMMEDIATE")
                purge_ids = {
                    int(row["id"])
                    for row in connection.execute(
                        """
                        SELECT id FROM private_chat_messages
                        WHERE purged_at IS NULL AND event_time < ?
                        """,
                        (cutoff,),
                    )
                }
                users = [
                    str(row["user_id"])
                    for row in connection.execute(
                        """
                        SELECT DISTINCT user_id FROM private_chat_messages
                        WHERE purged_at IS NULL AND event_time >= ?
                        """,
                        (cutoff,),
                    )
                ]
                for user_id in users:
                    rows = connection.execute(
                        """
                        SELECT id FROM private_chat_messages
                        WHERE user_id=? AND purged_at IS NULL AND event_time >= ?
                        ORDER BY event_time DESC,id DESC
                        """,
                        (user_id, cutoff),
                    ).fetchall()
                    purge_ids.update(int(row["id"]) for row in rows[max_messages:])
                if purge_ids:
                    placeholders = ",".join("?" for _ in purge_ids)
                    connection.execute(
                        f"UPDATE private_chat_messages "  # noqa: S608 - placeholders only
                        f"SET text='',purged_at=? WHERE id IN ({placeholders})",
                        (purged_at, *sorted(purge_ids)),
                    )
                connection.commit()
            except Exception:
                connection.rollback()
                raise
        return PurgeReport(
            purged_messages=len(purge_ids),
            checkpoint_complete=_checkpoint_truncate(self.path),
        )

    def clear_private_layers(
        self,
        *,
        user_id: str,
        actor: str,
        reason: str,
        operation_id: int,
    ) -> ClearReport:
        user_id = _validate_user_id(user_id)
        actor = _validate_nonempty(actor, "actor")
        reason = _validate_nonempty(reason, "reason")
        if (
            isinstance(operation_id, bool)
            or not isinstance(operation_id, int)
            or operation_id <= 0
        ):
            raise ValueError("operation_id must be a positive integer")
        now = _now_text()
        with closing(self._connect()) as connection:
            try:
                connection.execute("PRAGMA secure_delete=ON")
                connection.execute("BEGIN IMMEDIATE")
                message_rows = connection.execute(
                    """
                    SELECT id FROM private_chat_messages
                    WHERE user_id=? AND purged_at IS NULL
                    """,
                    (user_id,),
                ).fetchall()
                message_ids = [int(row["id"]) for row in message_rows]
                cleared_through = int(
                    connection.execute(
                        "SELECT COALESCE(MAX(id),0) "
                        "FROM private_chat_messages WHERE user_id=?",
                        (user_id,),
                    ).fetchone()[0]
                )
                connection.execute(
                    """
                    UPDATE private_chat_messages SET text='',purged_at=?
                    WHERE user_id=? AND purged_at IS NULL
                    """,
                    (now, user_id),
                )
                summary = connection.execute(
                    "SELECT summary_text FROM private_conversation_summaries WHERE user_id=?",
                    (user_id,),
                ).fetchone()
                summaries_deleted = 1 if summary and str(summary["summary_text"]) else 0
                if summary is None:
                    connection.execute(
                        """
                        INSERT INTO private_conversation_summaries(
                            user_id,summary_text,source_start_id,source_end_id,
                            summarized_through_id,version,created_at,updated_at
                        ) VALUES(?,'',0,0,?,1,?,?)
                        """,
                        (user_id, cleared_through, now, now),
                    )
                else:
                    connection.execute(
                        """
                        UPDATE private_conversation_summaries
                        SET summary_text='',source_start_id=0,source_end_id=0,
                            summarized_through_id=?,version=version+1,updated_at=?
                        WHERE user_id=?
                        """,
                        (cleared_through, now, user_id),
                    )
                topics_cursor = connection.execute(
                    """
                    UPDATE relationship_states
                    SET open_topics_json='[]',version=version+1,updated_at=?
                    WHERE conversation_kind='private' AND user_id=?
                      AND trim(open_topics_json)<>'[]'
                    """,
                    (now, user_id),
                )
                jobs_cursor = connection.execute(
                    """
                    UPDATE memory_jobs
                    SET status='cancelled',lease_owner=NULL,lease_expires_at=NULL,
                        error_code='',error_summary='',updated_at=?
                    WHERE conversation_kind='private' AND user_id=?
                      AND job_type='private_summary' AND status IN ('pending','running')
                    """,
                    (now, user_id),
                )
                before_hash = hashlib.sha256(
                    json.dumps(
                        {
                            "message_ids": message_ids,
                            "summaries": summaries_deleted,
                            "topics": topics_cursor.rowcount,
                            "jobs": jobs_cursor.rowcount,
                        },
                        sort_keys=True,
                    ).encode("utf-8")
                ).hexdigest()
                connection.execute(
                    """
                    INSERT INTO memory_governance_audit(
                        operation_id,operator_user_id,target_kind,target_group_id,
                        target_user_id,target_memory_id,operation_type,before_hash,
                        after_hash,reason,result,error_code,created_at
                    ) VALUES(?,?,'private',NULL,?,NULL,'clear_private_layers',?,
                             '',?,'success','',?)
                    """,
                    (operation_id, actor, user_id, before_hash, reason, now),
                )
                connection.commit()
            except Exception:
                connection.rollback()
                raise
        return ClearReport(
            purged_messages=len(message_ids),
            summaries_deleted=summaries_deleted,
            topics_cleared=topics_cursor.rowcount,
            jobs_cancelled=jobs_cursor.rowcount,
            checkpoint_complete=_checkpoint_truncate(self.path),
        )

    @staticmethod
    def _fact_from_row(row: sqlite3.Row) -> PrivateFact:
        return PrivateFact(
            id=int(row["id"]),
            user_id=str(row["user_id"]),
            fact_text=str(row["fact_text"]),
            source_message_id=str(row["source_message_id"]),
            source_quote=str(row["source_quote"]),
            trust_level=str(row["trust_level"]),
            status=str(row["status"]),
            supersedes_id=(
                int(row["supersedes_id"]) if row["supersedes_id"] is not None else None
            ),
            version=int(row["version"]),
            created_at=str(row["created_at"]),
            updated_at=str(row["updated_at"]),
            deleted_at=str(row["deleted_at"]) if row["deleted_at"] is not None else None,
        )


__all__ = ["PrivateMemoryStore"]
