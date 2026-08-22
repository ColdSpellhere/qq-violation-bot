from __future__ import annotations

import json
import re
import sqlite3
import unicodedata
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .models import ConversationScope, RelationshipState, validate_persona_id
from .schema import PRIVATE_MEMORY_SCHEMA_VERSION, schema_version


MAX_STATE_TEXT_LENGTH = 600
MAX_OPEN_TOPICS = 5
MAX_OPEN_TOPIC_LENGTH = 80
MAX_PREFERRED_ADDRESS_LENGTH = 40
MAX_COMMUNICATION_STYLE_LENGTH = 200

_USER_ID_RE = re.compile(r"[1-9][0-9]*", re.ASCII)
_GOVERNANCE_SOURCE_RE = re.compile(r"governance:([1-9][0-9]*)", re.ASCII)


def _validate_user_id(user_id: str) -> str:
    if not isinstance(user_id, str) or _USER_ID_RE.fullmatch(user_id) is None:
        raise ValueError("user_id must be a positive ASCII decimal string")
    return user_id


def _validate_group_id(group_id: int) -> int:
    if isinstance(group_id, bool) or not isinstance(group_id, int) or group_id <= 0:
        raise ValueError("group_id must be a positive integer")
    return group_id


def _validate_source_message_id(source_message_id: str) -> str:
    if not isinstance(source_message_id, str):
        raise TypeError("source_message_id must be a string")
    if not source_message_id or len(source_message_id) > 128:
        raise ValueError("source_message_id must contain 1 to 128 characters")
    if source_message_id != source_message_id.strip():
        raise ValueError("source_message_id must not have surrounding whitespace")
    if not source_message_id.isascii():
        raise ValueError("source_message_id must contain ASCII characters only")
    if any(
        unicodedata.category(character).startswith("C")
        for character in source_message_id
    ):
        raise ValueError("source_message_id must not contain control characters")
    return source_message_id


def _governance_operation_id(source_message_id: str) -> int | None:
    match = _GOVERNANCE_SOURCE_RE.fullmatch(source_message_id)
    if source_message_id.startswith("governance:"):
        if match is None:
            raise ValueError("governance source must contain a positive operation id")
        return int(match.group(1))
    return None


def _validate_text(value: str, *, field: str, maximum: int) -> str:
    if not isinstance(value, str):
        raise TypeError(f"{field} must be a string")
    if len(value) > maximum:
        raise ValueError(f"{field} exceeds {maximum} characters")
    return value


def _validate_topics(open_topics: tuple[str, ...]) -> tuple[str, ...]:
    if not isinstance(open_topics, tuple):
        raise TypeError("open_topics must be a tuple")
    if len(open_topics) > MAX_OPEN_TOPICS:
        raise ValueError(f"open_topics exceeds {MAX_OPEN_TOPICS} items")
    for topic in open_topics:
        if not isinstance(topic, str):
            raise TypeError("each open topic must be a string")
        if not topic.strip():
            raise ValueError("open topics must not be empty")
        if len(topic) > MAX_OPEN_TOPIC_LENGTH:
            raise ValueError(
                f"open topic exceeds {MAX_OPEN_TOPIC_LENGTH} characters"
            )
    return open_topics


def _validate_nonnegative_int(value: int, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(f"{field} must be a non-negative integer")
    return value


def _now_text() -> str:
    return (
        datetime.now(timezone.utc)
        .replace(microsecond=0)
        .isoformat()
        .replace("+00:00", "Z")
    )


class RelationshipStore:
    def __init__(self, path: Path):
        self.path = Path(path)
        current_version = schema_version(self.path)
        if current_version != PRIVATE_MEMORY_SCHEMA_VERSION:
            raise RuntimeError(
                "private memory schema version mismatch: "
                f"expected {PRIVATE_MEMORY_SCHEMA_VERSION}, got {current_version}"
            )

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("PRAGMA busy_timeout=5000")
        return connection

    def get_group(
        self, *, group_id: int, user_id: str, persona_id: str
    ) -> RelationshipState | None:
        group_id = _validate_group_id(group_id)
        user_id = _validate_user_id(user_id)
        persona_id = validate_persona_id(persona_id)
        with closing(self._connect()) as connection:
            row = connection.execute(
                """
                SELECT id,conversation_kind,group_id,user_id,persona_id,state_text,
                       open_topics_json,preferred_address,communication_style,
                       source_message_id,source_watermark,version,created_at,updated_at
                FROM relationship_states
                WHERE conversation_kind='group' AND group_id=?
                  AND user_id=? AND persona_id=?
                """,
                (group_id, user_id, persona_id),
            ).fetchone()
        return self._from_row(row) if row is not None else None

    def get_private(
        self, *, user_id: str, persona_id: str
    ) -> RelationshipState | None:
        user_id = _validate_user_id(user_id)
        persona_id = validate_persona_id(persona_id)
        with closing(self._connect()) as connection:
            row = connection.execute(
                """
                SELECT id,conversation_kind,group_id,user_id,persona_id,state_text,
                       open_topics_json,preferred_address,communication_style,
                       source_message_id,source_watermark,version,created_at,updated_at
                FROM relationship_states
                WHERE conversation_kind='private' AND group_id IS NULL
                  AND user_id=? AND persona_id=?
                """,
                (user_id, persona_id),
            ).fetchone()
        return self._from_row(row) if row is not None else None

    def commit(
        self, candidate: RelationshipState, *, expected_version: int
    ) -> bool:
        if not isinstance(candidate, RelationshipState):
            raise TypeError("candidate must be a RelationshipState")
        expected_version = _validate_nonnegative_int(
            expected_version, "expected_version"
        )
        scope = self._validate_scope(candidate.scope)
        state_text = _validate_text(
            candidate.state_text,
            field="state_text",
            maximum=MAX_STATE_TEXT_LENGTH,
        )
        open_topics = _validate_topics(candidate.open_topics)
        preferred_address = _validate_text(
            candidate.preferred_address,
            field="preferred_address",
            maximum=MAX_PREFERRED_ADDRESS_LENGTH,
        )
        communication_style = _validate_text(
            candidate.communication_style,
            field="communication_style",
            maximum=MAX_COMMUNICATION_STYLE_LENGTH,
        )
        source_message_id = _validate_source_message_id(candidate.source_message_id)
        source_watermark = _validate_nonnegative_int(
            candidate.source_watermark, "source_watermark"
        )
        governance_operation_id = _governance_operation_id(source_message_id)
        candidate_version = _validate_nonnegative_int(candidate.version, "version")
        if candidate_version != expected_version + 1:
            raise ValueError("candidate version must equal expected_version + 1")

        topics_json = json.dumps(
            open_topics, ensure_ascii=False, separators=(",", ":")
        )
        now = _now_text()
        with closing(self._connect()) as connection:
            try:
                connection.execute("BEGIN IMMEDIATE")
                current = self._select_current(connection, scope)
                if current is None:
                    if expected_version != 0:
                        connection.rollback()
                        return False
                    if governance_operation_id is not None and source_watermark != 0:
                        raise ValueError(
                            "governance must preserve the initial zero message watermark"
                        )
                    self._validate_source(
                        connection,
                        scope=scope,
                        source_message_id=source_message_id,
                        source_watermark=source_watermark,
                        governance_operation_id=governance_operation_id,
                    )
                    cursor = connection.execute(
                        """
                        INSERT INTO relationship_states(
                            conversation_kind,group_id,user_id,persona_id,state_text,
                            open_topics_json,preferred_address,communication_style,
                            source_message_id,source_watermark,version,created_at,updated_at
                        ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)
                        ON CONFLICT DO NOTHING
                        """,
                        (
                            scope.conversation_kind,
                            scope.group_id,
                            scope.user_id,
                            scope.persona_id,
                            state_text,
                            topics_json,
                            preferred_address,
                            communication_style,
                            source_message_id,
                            source_watermark,
                            candidate_version,
                            now,
                            now,
                        ),
                    )
                    if cursor.rowcount != 1:
                        connection.rollback()
                        return False
                    connection.commit()
                    return True

                current_version = int(current["version"])
                if current_version != expected_version:
                    connection.rollback()
                    return False
                current_watermark = int(current["source_watermark"])
                if governance_operation_id is not None:
                    if source_watermark != current_watermark:
                        raise ValueError(
                            "governance must preserve the current message watermark"
                        )
                elif source_watermark <= current_watermark:
                    raise ValueError("source watermark must advance")
                self._validate_source(
                    connection,
                    scope=scope,
                    source_message_id=source_message_id,
                    source_watermark=source_watermark,
                    governance_operation_id=governance_operation_id,
                )

                cursor = self._update_current(
                    connection,
                    scope=scope,
                    state_text=state_text,
                    topics_json=topics_json,
                    preferred_address=preferred_address,
                    communication_style=communication_style,
                    source_message_id=source_message_id,
                    source_watermark=source_watermark,
                    candidate_version=candidate_version,
                    expected_version=expected_version,
                    updated_at=now,
                )
                if cursor.rowcount != 1:
                    connection.rollback()
                    return False
                connection.commit()
                return True
            except Exception:
                connection.rollback()
                raise

    @staticmethod
    def _validate_scope(scope: ConversationScope) -> ConversationScope:
        if not isinstance(scope, ConversationScope):
            raise TypeError("scope must be a ConversationScope")
        user_id = _validate_user_id(scope.user_id)
        persona_id = validate_persona_id(scope.persona_id)
        if scope.conversation_kind == "group":
            group_id: int | None = _validate_group_id(scope.group_id)  # type: ignore[arg-type]
        elif scope.conversation_kind == "private":
            if scope.group_id is not None:
                raise ValueError("private scope must not have a group_id")
            group_id = None
        else:
            raise ValueError("conversation_kind must be 'group' or 'private'")
        return ConversationScope(
            conversation_kind=scope.conversation_kind,
            group_id=group_id,
            user_id=user_id,
            persona_id=persona_id,
        )

    @staticmethod
    def _validate_source(
        connection: sqlite3.Connection,
        *,
        scope: ConversationScope,
        source_message_id: str,
        source_watermark: int,
        governance_operation_id: int | None,
    ) -> None:
        if governance_operation_id is not None:
            return
        if source_watermark == 0:
            raise ValueError("automatic source watermark must be positive")

        try:
            if scope.conversation_kind == "private":
                source = connection.execute(
                    """
                    SELECT 1 FROM private_chat_messages
                    WHERE id=? AND user_id=? AND direction='user'
                      AND purged_at IS NULL AND message_id=?
                    """,
                    (source_watermark, scope.user_id, source_message_id),
                ).fetchone()
            else:
                source = connection.execute(
                    """
                    SELECT 1 FROM chat_messages
                    WHERE group_id=? AND user_id=? AND message_id=? AND rowid=?
                    """,
                    (
                        scope.group_id,
                        scope.user_id,
                        source_message_id,
                        source_watermark,
                    ),
                ).fetchone()
        except sqlite3.OperationalError as exc:
            if "no such table" not in str(exc):
                raise
            source = None
        if source is None:
            raise ValueError("source message does not match the relationship scope")

    @staticmethod
    def _select_current(
        connection: sqlite3.Connection, scope: ConversationScope
    ) -> sqlite3.Row | None:
        if scope.conversation_kind == "group":
            return connection.execute(
                """
                SELECT version,source_watermark FROM relationship_states
                WHERE conversation_kind='group' AND group_id=?
                  AND user_id=? AND persona_id=?
                """,
                (scope.group_id, scope.user_id, scope.persona_id),
            ).fetchone()
        return connection.execute(
            """
            SELECT version,source_watermark FROM relationship_states
            WHERE conversation_kind='private' AND group_id IS NULL
              AND user_id=? AND persona_id=?
            """,
            (scope.user_id, scope.persona_id),
        ).fetchone()

    @staticmethod
    def _update_current(
        connection: sqlite3.Connection,
        *,
        scope: ConversationScope,
        state_text: str,
        topics_json: str,
        preferred_address: str,
        communication_style: str,
        source_message_id: str,
        source_watermark: int,
        candidate_version: int,
        expected_version: int,
        updated_at: str,
    ) -> sqlite3.Cursor:
        values: tuple[Any, ...] = (
            state_text,
            topics_json,
            preferred_address,
            communication_style,
            source_message_id,
            source_watermark,
            candidate_version,
            updated_at,
        )
        if scope.conversation_kind == "group":
            return connection.execute(
                """
                UPDATE relationship_states
                SET state_text=?,open_topics_json=?,preferred_address=?,
                    communication_style=?,source_message_id=?,source_watermark=?,
                    version=?,updated_at=?
                WHERE conversation_kind='group' AND group_id=?
                  AND user_id=? AND persona_id=? AND version=?
                """,
                values
                + (
                    scope.group_id,
                    scope.user_id,
                    scope.persona_id,
                    expected_version,
                ),
            )
        return connection.execute(
            """
            UPDATE relationship_states
            SET state_text=?,open_topics_json=?,preferred_address=?,
                communication_style=?,source_message_id=?,source_watermark=?,
                version=?,updated_at=?
            WHERE conversation_kind='private' AND group_id IS NULL
              AND user_id=? AND persona_id=? AND version=?
            """,
            values + (scope.user_id, scope.persona_id, expected_version),
        )

    @staticmethod
    def _from_row(row: sqlite3.Row) -> RelationshipState:
        try:
            decoded = json.loads(str(row["open_topics_json"]))
        except (TypeError, ValueError) as exc:
            raise ValueError("stored open_topics_json is invalid") from exc
        if not isinstance(decoded, list):
            raise ValueError("stored open_topics_json must be a list")
        topics = _validate_topics(tuple(decoded))
        scope = RelationshipStore._validate_scope(
            ConversationScope(
                conversation_kind=str(row["conversation_kind"]),
                group_id=(
                    int(row["group_id"]) if row["group_id"] is not None else None
                ),
                user_id=str(row["user_id"]),
                persona_id=str(row["persona_id"]),
            )
        )
        state_text = _validate_text(
            str(row["state_text"]),
            field="state_text",
            maximum=MAX_STATE_TEXT_LENGTH,
        )
        preferred_address = _validate_text(
            str(row["preferred_address"]),
            field="preferred_address",
            maximum=MAX_PREFERRED_ADDRESS_LENGTH,
        )
        communication_style = _validate_text(
            str(row["communication_style"]),
            field="communication_style",
            maximum=MAX_COMMUNICATION_STYLE_LENGTH,
        )
        source_message_id = _validate_source_message_id(str(row["source_message_id"]))
        source_watermark = _validate_nonnegative_int(
            int(row["source_watermark"]), "source_watermark"
        )
        version = _validate_nonnegative_int(int(row["version"]), "version")
        if version == 0:
            raise ValueError("version must be positive")
        if int(row["id"]) <= 0:
            raise ValueError("relationship row id must be positive")
        governance_operation_id = _governance_operation_id(source_message_id)
        if governance_operation_id is None and source_watermark == 0:
            raise ValueError("automatic source watermark must be positive")
        return RelationshipState(
            id=int(row["id"]),
            scope=scope,
            state_text=state_text,
            open_topics=topics,
            preferred_address=preferred_address,
            communication_style=communication_style,
            source_message_id=source_message_id,
            source_watermark=source_watermark,
            version=version,
            created_at=str(row["created_at"]),
            updated_at=str(row["updated_at"]),
        )
