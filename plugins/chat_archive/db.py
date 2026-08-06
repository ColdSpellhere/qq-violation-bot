from __future__ import annotations

import json
import os
import sqlite3
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any


SCHEMA = """
CREATE TABLE IF NOT EXISTS chat_messages (
    message_id TEXT PRIMARY KEY,
    group_id INTEGER NOT NULL,
    event_time INTEGER NOT NULL,
    user_id TEXT NOT NULL,
    sender_json TEXT NOT NULL,
    message_json TEXT NOT NULL,
    plaintext TEXT NOT NULL,
    reply_message_id TEXT,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_chat_messages_time
ON chat_messages(event_time, message_id);
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


def recent_text_context(
    path: Path,
    *,
    group_id: int,
    since_epoch: int,
    limit: int,
    exclude_message_id: str,
    bot_user_id: str,
) -> list[ContextMessage]:
    if not path.is_file() or limit <= 0:
        return []
    try:
        with sqlite3.connect(path) as conn:
            rows = conn.execute(
                """
                SELECT m.message_id,m.user_id,m.sender_json,m.message_json,m.plaintext,
                       m.reply_message_id,replied.user_id
                FROM chat_messages AS m
                LEFT JOIN chat_messages AS replied
                  ON replied.message_id=m.reply_message_id AND replied.group_id=m.group_id
                WHERE m.group_id=? AND m.event_time>=?
                  AND m.message_id<>? AND m.user_id<>?
                  AND trim(m.plaintext)<>''
                  AND substr(ltrim(m.plaintext),1,1)<>'/'
                ORDER BY m.event_time DESC,m.message_id DESC
                LIMIT ?
                """,
                (group_id, since_epoch, exclude_message_id, bot_user_id, limit),
            ).fetchall()
    except (OSError, sqlite3.Error):
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
        if text:
            context.append(
                ContextMessage(
                    nickname or str(user_id),
                    text,
                    message_id=str(message_id),
                    user_id=str(user_id),
                    at_user_ids=at_user_ids,
                    reply_message_id=str(reply_id) if reply_id else None,
                    replied_to_user_id=str(replied_user_id) if replied_user_id else None,
                )
            )
    return context


def archived_message_author(path: Path, *, group_id: int, message_id: str | None) -> str | None:
    if not path.is_file() or not message_id:
        return None
    try:
        with sqlite3.connect(path) as conn:
            row = conn.execute(
                "SELECT user_id FROM chat_messages WHERE group_id=? AND message_id=?",
                (group_id, message_id),
            ).fetchone()
    except (OSError, sqlite3.Error):
        return None
    return str(row[0]) if row else None


def archive_payload(path: Path, target_group_id: int, payload: dict[str, Any]) -> bool:
    if int(payload["group_id"]) != target_group_id:
        return False
    path.parent.mkdir(parents=True, exist_ok=True)
    if not path.exists():
        descriptor = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        os.close(descriptor)
    with sqlite3.connect(path) as conn:
        conn.executescript(SCHEMA)
        conn.execute(
            """
            INSERT INTO chat_messages(
                message_id,group_id,event_time,user_id,sender_json,message_json,
                plaintext,reply_message_id,created_at
            ) VALUES(?,?,?,?,?,?,?,?,?)
            ON CONFLICT(message_id) DO NOTHING
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
    return True
