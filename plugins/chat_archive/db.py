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
                SELECT user_id,sender_json,plaintext
                FROM chat_messages
                WHERE group_id=? AND event_time>=?
                  AND message_id<>? AND user_id<>?
                  AND trim(plaintext)<>''
                  AND substr(ltrim(plaintext),1,1)<>'/'
                ORDER BY event_time DESC,message_id DESC
                LIMIT ?
                """,
                (group_id, since_epoch, exclude_message_id, bot_user_id, limit),
            ).fetchall()
    except (OSError, sqlite3.Error):
        return []
    context: list[ContextMessage] = []
    for user_id, sender_json, plaintext in reversed(rows):
        try:
            sender = json.loads(sender_json)
            if not isinstance(sender, dict):
                sender = {}
        except (TypeError, json.JSONDecodeError):
            sender = {}
        nickname = str(sender.get("card") or sender.get("nickname") or user_id).strip()
        text = str(plaintext).strip()
        if text:
            context.append(ContextMessage(nickname or str(user_id), text))
    return context


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
