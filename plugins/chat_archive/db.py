from __future__ import annotations

import json
import asyncio
import logging
import os
import sqlite3
from contextlib import closing
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any


logger = logging.getLogger(__name__)


SCHEMA = """
CREATE TABLE IF NOT EXISTS chat_messages (
    message_id TEXT NOT NULL,
    group_id INTEGER NOT NULL,
    event_time INTEGER NOT NULL,
    user_id TEXT NOT NULL,
    sender_json TEXT NOT NULL,
    message_json TEXT NOT NULL,
    plaintext TEXT NOT NULL,
    reply_message_id TEXT,
    created_at TEXT NOT NULL,
    PRIMARY KEY(group_id,message_id)
);
CREATE INDEX IF NOT EXISTS idx_chat_messages_time
ON chat_messages(group_id,event_time);
"""


@dataclass(frozen=True)
class ContextMessage:
    nickname: str
    text: str
    message_id: str = ""
    user_id: str = ""
    at_user_ids: tuple[str, ...] = ()
    reply_message_id: str | None = None
    replied_to_user_id: str | None = None
    image_descriptions: tuple[str, ...] = ()
    is_bot: bool = False
    is_peer_bot: bool = False


def recent_text_context(
    path: Path,
    *,
    group_id: int,
    since_epoch: int,
    limit: int,
    exclude_message_id: str,
    bot_user_id: str,
    include_bot_messages: bool = False,
    peer_bot_user_ids: tuple[str, ...] = (),
) -> list[ContextMessage]:
    if not path.is_file() or limit <= 0:
        return []
    try:
        with closing(sqlite3.connect(path)) as conn, conn:
            boundary = conn.execute(
                "SELECT rowid FROM chat_messages WHERE group_id=? AND message_id=?",
                (group_id, exclude_message_id),
            ).fetchone()
            boundary_rowid = int(boundary[0]) if boundary is not None else None
            has_image_assets = conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='chat_image_assets'"
            ).fetchone()
            if has_image_assets:
                rows = conn.execute(
                    """
                    SELECT m.message_id,m.user_id,m.sender_json,m.message_json,m.plaintext,
                           m.reply_message_id,replied.user_id
                    FROM chat_messages AS m
                    LEFT JOIN chat_messages AS replied
                      ON replied.message_id=m.reply_message_id AND replied.group_id=m.group_id
                    WHERE m.group_id=? AND m.event_time>=?
                      AND m.message_id<>? AND (?=1 OR m.user_id<>?)
                      AND (
                          ? IS NULL OR m.rowid<?
                          OR (
                              m.user_id=? AND m.reply_message_id IS NOT NULL
                              AND EXISTS (
                                  SELECT 1 FROM chat_messages AS trigger
                                  WHERE trigger.group_id=m.group_id
                                    AND trigger.message_id=m.reply_message_id
                                    AND trigger.rowid<?
                              )
                          )
                      )
                      AND (
                          (trim(m.plaintext)<>'' AND substr(ltrim(m.plaintext),1,1)<>'/')
                          OR EXISTS (
                              SELECT 1 FROM chat_image_assets AS asset
                              WHERE asset.group_id=m.group_id AND asset.message_id=m.message_id
                                AND asset.status='ready' AND trim(asset.description)<>''
                          )
                      )
                    ORDER BY m.event_time DESC,m.rowid DESC
                    LIMIT ?
                    """,
                    (
                        group_id,
                        since_epoch,
                        exclude_message_id,
                        int(include_bot_messages),
                        bot_user_id,
                        boundary_rowid,
                        boundary_rowid,
                        bot_user_id,
                        boundary_rowid,
                        limit,
                    ),
                ).fetchall()
                descriptions = {
                    str(message_id): tuple(
                        str(description).strip()
                        for (description,) in conn.execute(
                            """
                            SELECT description FROM chat_image_assets
                            WHERE group_id=? AND message_id=?
                              AND status='ready' AND trim(description)<>''
                            ORDER BY ordinal
                            """,
                            (group_id, message_id),
                        )
                    )
                    for message_id, *_ in rows
                }
            else:
                rows = conn.execute(
                    """
                    SELECT m.message_id,m.user_id,m.sender_json,m.message_json,m.plaintext,
                           m.reply_message_id,replied.user_id
                    FROM chat_messages AS m
                    LEFT JOIN chat_messages AS replied
                      ON replied.message_id=m.reply_message_id AND replied.group_id=m.group_id
                    WHERE m.group_id=? AND m.event_time>=?
                      AND m.message_id<>? AND (?=1 OR m.user_id<>?)
                      AND (
                          ? IS NULL OR m.rowid<?
                          OR (
                              m.user_id=? AND m.reply_message_id IS NOT NULL
                              AND EXISTS (
                                  SELECT 1 FROM chat_messages AS trigger
                                  WHERE trigger.group_id=m.group_id
                                    AND trigger.message_id=m.reply_message_id
                                    AND trigger.rowid<?
                              )
                          )
                      )
                      AND trim(m.plaintext)<>''
                      AND substr(ltrim(m.plaintext),1,1)<>'/'
                    ORDER BY m.event_time DESC,m.rowid DESC
                    LIMIT ?
                    """,
                    (
                        group_id,
                        since_epoch,
                        exclude_message_id,
                        int(include_bot_messages),
                        bot_user_id,
                        boundary_rowid,
                        boundary_rowid,
                        bot_user_id,
                        boundary_rowid,
                        limit,
                    ),
                ).fetchall()
                descriptions = {}
    except (OSError, sqlite3.Error) as exc:
        logger.warning("chat context read failed error_class=%s", type(exc).__name__)
        return []
    context: list[ContextMessage] = []
    for message_id, user_id, sender_json, message_json, plaintext, reply_id, replied_user_id in reversed(rows):
        try:
            sender = json.loads(sender_json)
            if not isinstance(sender, dict):
                sender = {}
        except (TypeError, json.JSONDecodeError):
            sender = {}
        nickname = str(sender.get("card") or sender.get("nickname") or user_id).strip()
        text = str(plaintext).strip()
        try:
            segments = json.loads(message_json)
            if not isinstance(segments, list):
                segments = []
        except (TypeError, json.JSONDecodeError):
            segments = []
        at_user_ids = tuple(
            str(segment.get("data", {}).get("qq"))
            for segment in segments
            if isinstance(segment, dict)
            and segment.get("type") == "at"
            and str(segment.get("data", {}).get("qq") or "").isdigit()
        )
        image_descriptions = descriptions.get(str(message_id), ())
        if text or image_descriptions:
            context.append(
                ContextMessage(
                    nickname or str(user_id),
                    text or "[图片]",
                    message_id=str(message_id),
                    user_id=str(user_id),
                    at_user_ids=at_user_ids,
                    reply_message_id=str(reply_id) if reply_id else None,
                    replied_to_user_id=str(replied_user_id) if replied_user_id else None,
                    image_descriptions=image_descriptions,
                    is_bot=str(user_id) == str(bot_user_id),
                    is_peer_bot=(
                        str(user_id) != str(bot_user_id)
                        and str(user_id) in peer_bot_user_ids
                    ),
                )
            )
    return context


def archived_message_author(path: Path, *, group_id: int, message_id: str | None) -> str | None:
    if not path.is_file() or not message_id:
        return None
    try:
        with closing(sqlite3.connect(path)) as conn, conn:
            row = conn.execute(
                "SELECT user_id FROM chat_messages WHERE group_id=? AND message_id=?",
                (group_id, message_id),
            ).fetchone()
    except (OSError, sqlite3.Error):
        return None
    return str(row[0]) if row else None


def assert_table_rebuild_safe(connection: sqlite3.Connection, table: str) -> None:
    """Fail closed for local extensions whose dependencies need manual review."""
    for kind, name, sql in connection.execute(
        "SELECT type,name,sql FROM sqlite_master WHERE type IN ('view','trigger')"
    ):
        if table.casefold() in str(sql).casefold():
            raise RuntimeError(f"{table} has dependent {kind}; migration requires review")
    for (name,) in connection.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall():
        escaped = str(name).replace('"', '""')
        for foreign_key in connection.execute(f'PRAGMA foreign_key_list("{escaped}")'):
            if str(foreign_key[2]).casefold() == table.casefold():
                raise RuntimeError(f"{table} has foreign key references; migration requires review")


def migrate_archive_schema(connection: sqlite3.Connection) -> bool:
    """Controlled startup migration: preserve rowids used by relationship jobs."""
    columns = connection.execute("PRAGMA table_info(chat_messages)").fetchall()
    if not columns:
        return False
    primary = [row[1] for row in sorted(columns, key=lambda row: row[5]) if row[5]]
    if primary == ["group_id", "message_id"]:
        return False
    if primary != ["message_id"]:
        raise RuntimeError("unsupported archive primary key")
    expected = {"message_id", "group_id", "event_time", "user_id", "sender_json",
                "message_json", "plaintext", "reply_message_id", "created_at"}
    if {row[1] for row in columns} != expected:
        raise RuntimeError("archive schema has unknown columns; refusing lossy migration")
    assert_table_rebuild_safe(connection, "chat_messages")
    # This helper runs inside the private-memory migration transaction and never
    # makes a backup/restore decision on behalf of an already running service.
    indexes = [row[0] for row in connection.execute(
        "SELECT sql FROM sqlite_master WHERE type='index' AND tbl_name='chat_messages' AND sql IS NOT NULL"
    )]
    connection.execute("ALTER TABLE chat_messages RENAME TO chat_messages_before_scope")
    connection.execute(SCHEMA.split(';', 1)[0])
    names = ','.join(row[1] for row in columns)
    connection.execute(f"INSERT INTO chat_messages(rowid,{names}) SELECT rowid,{names} FROM chat_messages_before_scope")
    connection.execute("DROP TABLE chat_messages_before_scope")
    for statement in indexes:
        connection.execute(statement)
    connection.execute("CREATE INDEX IF NOT EXISTS idx_chat_messages_group_time ON chat_messages(group_id,event_time)")
    return True


async def archive_payload_async(path: Path, target_group_id: int, payload: dict[str, Any]) -> bool:
    """Await durability without running SQLite/filesystem work on the event loop."""
    task = asyncio.create_task(asyncio.to_thread(archive_payload, path, target_group_id, payload))
    try:
        return await asyncio.shield(task)
    except asyncio.CancelledError:
        await task
        raise


def archive_payload(path: Path, target_group_id: int, payload: dict[str, Any]) -> bool:
    if int(payload["group_id"]) != target_group_id:
        return False
    path.parent.mkdir(parents=True, exist_ok=True)
    if not path.exists():
        try:
            descriptor = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        except FileExistsError:
            pass
        else:
            os.close(descriptor)
    with closing(sqlite3.connect(path)) as conn, conn:
        conn.executescript(SCHEMA)
        inserted = conn.execute(
            """
            INSERT INTO chat_messages(
                message_id,group_id,event_time,user_id,sender_json,message_json,
                plaintext,reply_message_id,created_at
            ) VALUES(?,?,?,?,?,?,?,?,?)
            ON CONFLICT(group_id,message_id) DO NOTHING
            """,
            (
                str(payload["message_id"]),
                target_group_id,
                int(payload["event_time"]),
                str(payload["user_id"]),
                json.dumps(payload["sender"], ensure_ascii=False, default=str),
                json.dumps(payload["segments"], ensure_ascii=False, default=str),
                str(payload.get("plaintext") or ""),
                str(payload.get("reply_message_id") or "") or None,
                datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            ),
        )
    path.chmod(0o600)
    return inserted.rowcount == 1
