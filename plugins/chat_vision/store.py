from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from pathlib import Path


SCHEMA = """
CREATE TABLE IF NOT EXISTS chat_image_assets (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    group_id INTEGER NOT NULL,
    message_id TEXT NOT NULL,
    ordinal INTEGER NOT NULL,
    source_url TEXT NOT NULL,
    event_time INTEGER NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending',
    attempts INTEGER NOT NULL DEFAULT 0,
    relative_path TEXT,
    mime_type TEXT,
    byte_size INTEGER,
    sha256 TEXT,
    description TEXT,
    expires_at TEXT,
    deleted_at TEXT,
    error_type TEXT,
    UNIQUE(group_id, message_id, ordinal)
);
CREATE INDEX IF NOT EXISTS idx_chat_image_assets_claimable
ON chat_image_assets(status, attempts, id);
CREATE INDEX IF NOT EXISTS idx_chat_image_assets_expiry
ON chat_image_assets(expires_at, deleted_at);
"""


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
    )


class ChatVisionStore:
    def __init__(self, database_path: Path):
        self.database_path = Path(database_path)
        self.database_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_schema()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.database_path)
        conn.row_factory = sqlite3.Row
        return conn

    def _init_schema(self) -> None:
        with self._connect() as conn:
            conn.executescript(SCHEMA)

    def recover_interrupted_claims(self) -> None:
        with self._connect() as conn:
            conn.execute(
                "UPDATE chat_image_assets SET status='pending' WHERE status='processing'"
            )

    @staticmethod
    def _one(conn: sqlite3.Connection, asset_id: int) -> ChatImageAsset | None:
        row = conn.execute(
            "SELECT id,group_id,message_id,ordinal,source_url,event_time,status,attempts,"
            "relative_path,mime_type,byte_size,sha256,description,expires_at,deleted_at "
            "FROM chat_image_assets WHERE id=?",
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
                "SELECT id,group_id,message_id,ordinal,source_url,event_time,status,attempts,"
                "relative_path,mime_type,byte_size,sha256,description,expires_at,deleted_at "
                "FROM chat_image_assets WHERE group_id=? AND message_id=? AND ordinal=?",
                (group_id, message_id, ordinal),
            ).fetchone()
        assert row is not None
        return _asset(row)

    def claim(self, asset_id: int, max_retries: int) -> ChatImageAsset | None:
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            cursor = conn.execute(
                "UPDATE chat_image_assets SET status='processing',attempts=attempts+1 "
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
                "expires_at=? WHERE id=?",
                (relative_path, mime_type, byte_size, sha256, expires_at, asset_id),
            )

    def mark_ready(self, asset_id: int, description: str) -> None:
        with self._connect() as conn:
            conn.execute(
                "UPDATE chat_image_assets SET status='ready',description=?,error_type=NULL WHERE id=?",
                (description, asset_id),
            )

    def mark_failed(self, asset_id: int, error_type: str) -> None:
        with self._connect() as conn:
            conn.execute(
                "UPDATE chat_image_assets SET status='failed',error_type=? WHERE id=?",
                (error_type, asset_id),
            )

    def mark_deleted(self, asset_id: int, deleted_at: str) -> None:
        with self._connect() as conn:
            conn.execute(
                "UPDATE chat_image_assets SET relative_path=NULL,deleted_at=? WHERE id=?",
                (deleted_at, asset_id),
            )

    def for_message(self, group_id: int, message_id: str) -> list[ChatImageAsset]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT id,group_id,message_id,ordinal,source_url,event_time,status,attempts,"
                "relative_path,mime_type,byte_size,sha256,description,expires_at,deleted_at "
                "FROM chat_image_assets WHERE group_id=? AND message_id=? ORDER BY ordinal",
                (group_id, message_id),
            ).fetchall()
        return [_asset(row) for row in rows]

    def claimable(self, max_retries: int) -> list[ChatImageAsset]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT id,group_id,message_id,ordinal,source_url,event_time,status,attempts,"
                "relative_path,mime_type,byte_size,sha256,description,expires_at,deleted_at "
                "FROM chat_image_assets WHERE status IN ('pending','failed') "
                "AND attempts<? ORDER BY id",
                (max_retries,),
            ).fetchall()
        return [_asset(row) for row in rows]

    def expired(self, now_text: str) -> list[ChatImageAsset]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT id,group_id,message_id,ordinal,source_url,event_time,status,attempts,"
                "relative_path,mime_type,byte_size,sha256,description,expires_at,deleted_at "
                "FROM chat_image_assets WHERE expires_at IS NOT NULL AND expires_at<=? "
                "AND relative_path IS NOT NULL AND deleted_at IS NULL ORDER BY expires_at,id",
                (now_text,),
            ).fetchall()
        return [_asset(row) for row in rows]
