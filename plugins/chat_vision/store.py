from __future__ import annotations

import hashlib
import sqlite3
import stat
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from .paths import validate_existing_managed_root


TABLE_SCHEMA = """
CREATE TABLE IF NOT EXISTS chat_image_assets (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    group_id INTEGER NOT NULL,
    message_id TEXT NOT NULL,
    ordinal INTEGER NOT NULL,
    source_url TEXT NOT NULL,
    event_time INTEGER NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending'
        CHECK(status IN ('pending','processing','ready','failed')),
    attempts INTEGER NOT NULL DEFAULT 0,
    relative_path TEXT,
    mime_type TEXT,
    byte_size INTEGER,
    sha256 TEXT,
    description TEXT,
    expires_at TEXT,
    deleted_at TEXT,
    error_type TEXT,
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%d %H:%M:%f','now')),
    updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%d %H:%M:%f','now')),
    UNIQUE(group_id, message_id, ordinal)
);
"""

INDEX_SCHEMA = """
CREATE INDEX IF NOT EXISTS idx_chat_image_assets_claimable
ON chat_image_assets(status, attempts, id);
CREATE INDEX IF NOT EXISTS idx_chat_image_assets_expiry
ON chat_image_assets(expires_at, deleted_at);
"""

SCHEMA = TABLE_SCHEMA + INDEX_SCHEMA

_SELECT_FIELDS = (
    "id,group_id,message_id,ordinal,source_url,event_time,status,attempts,"
    "relative_path,mime_type,byte_size,sha256,description,expires_at,deleted_at,"
    "created_at,updated_at"
)


@dataclass(frozen=True)
class ChatImageAsset:
    id: int
    group_id: int
    message_id: str
    ordinal: int
    source_url: str
    event_time: int
    status: str
    attempts: int
    relative_path: str | None
    mime_type: str | None
    byte_size: int | None
    sha256: str | None
    description: str | None
    expires_at: str | None
    deleted_at: str | None
    created_at: str
    updated_at: str


def _asset(row: sqlite3.Row) -> ChatImageAsset:
    return ChatImageAsset(
        id=int(row["id"]),
        group_id=int(row["group_id"]),
        message_id=str(row["message_id"]),
        ordinal=int(row["ordinal"]),
        source_url=str(row["source_url"]),
        event_time=int(row["event_time"]),
        status=str(row["status"]),
        attempts=int(row["attempts"]),
        relative_path=row["relative_path"],
        mime_type=row["mime_type"],
        byte_size=row["byte_size"],
        sha256=row["sha256"],
        description=row["description"],
        expires_at=row["expires_at"],
        deleted_at=row["deleted_at"],
        created_at=str(row["created_at"]),
        updated_at=str(row["updated_at"]),
    )


def read_original_image(
    asset: ChatImageAsset,
    root: Path,
    *,
    now_text: str | None = None,
) -> bytes | None:
    current_time = now_text or datetime.now(UTC).strftime("%Y-%m-%d %H:%M:%S")
    if (
        asset.deleted_at is not None
        or asset.expires_at is None
        or asset.expires_at <= current_time
        or asset.relative_path is None
    ):
        return None

    root = validate_existing_managed_root(root)
    if root is None:
        return None
    try:
        root_resolved = root.resolve(strict=True)
    except (OSError, RuntimeError):
        return None

    relative_path = Path(asset.relative_path)
    if relative_path.is_absolute() or any(
        component in {"", ".", ".."} for component in relative_path.parts
    ):
        return None
    candidate = root
    for component in relative_path.parts:
        candidate /= component
        try:
            mode = candidate.lstat().st_mode
        except (OSError, RuntimeError):
            return None
        if stat.S_ISLNK(mode):
            return None
    try:
        resolved = candidate.resolve(strict=True)
    except (OSError, RuntimeError):
        return None
    if not stat.S_ISREG(mode) or not resolved.is_relative_to(root_resolved):
        return None
    try:
        content = candidate.read_bytes()
    except OSError:
        return None
    if asset.byte_size is not None and len(content) != asset.byte_size:
        return None
    if asset.sha256 is not None and hashlib.sha256(content).hexdigest() != asset.sha256:
        return None
    return content


class ChatVisionStore:
    def __init__(self, database_path: Path):
        self.database_path = Path(database_path)
        self.database_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_schema()

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(self.database_path)
        conn.row_factory = sqlite3.Row
        try:
            yield conn
            conn.commit()
        except BaseException:
            conn.rollback()
            raise
        finally:
            conn.close()

    def _init_schema(self) -> None:
        with self._connect() as conn:
            existing = conn.execute(
                "SELECT sql FROM sqlite_master WHERE type='table' AND name='chat_image_assets'"
            ).fetchone()
            if existing is None:
                conn.executescript(SCHEMA)
                return
            columns = {
                str(row[1])
                for row in conn.execute("PRAGMA table_info(chat_image_assets)")
            }
            table_sql = str(existing[0] or "")
            normalized_table_sql = "".join(table_sql.lower().split())
            has_status_check = (
                "check(statusin('pending','processing','ready','failed'))"
                in normalized_table_sql
            )
            if (
                "created_at" not in columns
                or "updated_at" not in columns
                or not has_status_check
            ):
                self._migrate_legacy_table(conn, columns)
            conn.executescript(INDEX_SCHEMA)

    @staticmethod
    def _migrate_legacy_table(
        conn: sqlite3.Connection,
        columns: set[str],
    ) -> None:
        conn.execute("DROP TABLE IF EXISTS chat_image_assets_legacy_migration")
        conn.execute(
            "ALTER TABLE chat_image_assets RENAME TO chat_image_assets_legacy_migration"
        )
        conn.execute(TABLE_SCHEMA)
        target_columns = (
            "id",
            "group_id",
            "message_id",
            "ordinal",
            "source_url",
            "event_time",
            "status",
            "attempts",
            "relative_path",
            "mime_type",
            "byte_size",
            "sha256",
            "description",
            "expires_at",
            "deleted_at",
            "error_type",
            "created_at",
            "updated_at",
        )
        now_sql = "strftime('%Y-%m-%d %H:%M:%f','now')"
        expressions: list[str] = []
        for column in target_columns:
            if column == "status":
                expressions.append(
                    "CASE WHEN status IN ('pending','processing','ready','failed') "
                    "THEN status ELSE 'failed' END"
                )
            elif column in {"created_at", "updated_at"}:
                if column in columns:
                    expressions.append(f'COALESCE("{column}",{now_sql})')
                else:
                    expressions.append(now_sql)
            elif column in columns:
                expressions.append(f'"{column}"')
            else:
                expressions.append("NULL")
        conn.execute(
            f"INSERT INTO chat_image_assets({','.join(target_columns)}) "
            f"SELECT {','.join(expressions)} FROM chat_image_assets_legacy_migration"
        )
        conn.execute("DROP TABLE chat_image_assets_legacy_migration")

    def recover_interrupted_claims(self) -> None:
        with self._connect() as conn:
            conn.execute(
                "UPDATE chat_image_assets SET status='pending',"
                "updated_at=strftime('%Y-%m-%d %H:%M:%f','now') "
                "WHERE status='processing'"
            )

    @staticmethod
    def _one(conn: sqlite3.Connection, asset_id: int) -> ChatImageAsset | None:
        row = conn.execute(
            f"SELECT {_SELECT_FIELDS} FROM chat_image_assets WHERE id=?",
            (asset_id,),
        ).fetchone()
        return _asset(row) if row is not None else None

    def ensure_pending(
        self, group_id: int, message_id: str, ordinal: int, source_url: str, event_time: int
    ) -> ChatImageAsset:
        with self._connect() as conn:
            conn.execute(
                "INSERT INTO chat_image_assets("
                "group_id,message_id,ordinal,source_url,event_time,status,attempts"
                ") VALUES(?,?,?,?,?,'pending',0) ON CONFLICT(group_id,message_id,ordinal) DO NOTHING",
                (group_id, message_id, ordinal, source_url, event_time),
            )
            row = conn.execute(
                f"SELECT {_SELECT_FIELDS} FROM chat_image_assets "
                "WHERE group_id=? AND message_id=? AND ordinal=?",
                (group_id, message_id, ordinal),
            ).fetchone()
        assert row is not None
        return _asset(row)

    def claim(self, asset_id: int, max_retries: int) -> ChatImageAsset | None:
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            cursor = conn.execute(
                "UPDATE chat_image_assets SET status='processing',attempts=attempts+1,"
                "updated_at=strftime('%Y-%m-%d %H:%M:%f','now') "
                "WHERE id=? AND status IN ('pending','failed') AND attempts<?",
                (asset_id, max_retries),
            )
            if cursor.rowcount != 1:
                return None
            claimed = self._one(conn, asset_id)
        return claimed

    def mark_downloaded(
        self,
        asset_id: int,
        relative_path: str,
        mime_type: str,
        byte_size: int,
        sha256: str,
        expires_at: str,
    ) -> None:
        with self._connect() as conn:
            conn.execute(
                "UPDATE chat_image_assets SET relative_path=?,mime_type=?,byte_size=?,sha256=?,"
                "expires_at=?,updated_at=strftime('%Y-%m-%d %H:%M:%f','now') WHERE id=?",
                (relative_path, mime_type, byte_size, sha256, expires_at, asset_id),
            )

    def mark_ready(self, asset_id: int, description: str) -> None:
        with self._connect() as conn:
            conn.execute(
                "UPDATE chat_image_assets SET status='ready',description=?,error_type=NULL,"
                "updated_at=strftime('%Y-%m-%d %H:%M:%f','now') WHERE id=?",
                (description, asset_id),
            )

    def mark_failed(self, asset_id: int, error_type: str) -> None:
        with self._connect() as conn:
            conn.execute(
                "UPDATE chat_image_assets SET status='failed',error_type=?,"
                "updated_at=strftime('%Y-%m-%d %H:%M:%f','now') WHERE id=?",
                (error_type, asset_id),
            )

    def mark_deleted(self, asset_id: int, deleted_at: str) -> None:
        with self._connect() as conn:
            conn.execute(
                "UPDATE chat_image_assets SET relative_path=NULL,deleted_at=?,"
                "updated_at=strftime('%Y-%m-%d %H:%M:%f','now') WHERE id=?",
                (deleted_at, asset_id),
            )

    def for_message(self, group_id: int, message_id: str) -> list[ChatImageAsset]:
        with self._connect() as conn:
            rows = conn.execute(
                f"SELECT {_SELECT_FIELDS} FROM chat_image_assets "
                "WHERE group_id=? AND message_id=? ORDER BY ordinal",
                (group_id, message_id),
            ).fetchall()
        return [_asset(row) for row in rows]

    def claimable(
        self,
        max_retries: int,
        *,
        after_id: int = 0,
        limit: int = 100,
    ) -> list[ChatImageAsset]:
        if limit <= 0:
            return []
        with self._connect() as conn:
            rows = conn.execute(
                f"SELECT {_SELECT_FIELDS} FROM chat_image_assets "
                "WHERE status IN ('pending','failed') "
                "AND attempts<? AND id>? ORDER BY id LIMIT ?",
                (max_retries, after_id, limit),
            ).fetchall()
        return [_asset(row) for row in rows]

    def expired(self, now_text: str) -> list[ChatImageAsset]:
        with self._connect() as conn:
            rows = conn.execute(
                f"SELECT {_SELECT_FIELDS} FROM chat_image_assets "
                "WHERE expires_at IS NOT NULL AND expires_at<=? "
                "AND relative_path IS NOT NULL AND deleted_at IS NULL ORDER BY expires_at,id",
                (now_text,),
            ).fetchall()
        return [_asset(row) for row in rows]
