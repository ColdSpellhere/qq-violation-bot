"""A durable send ledger; ambiguous OneBot results are never blindly retried."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
import sqlite3
import time
from contextlib import contextmanager


def delivery_event_key(self_id: object, kind: str, group_id: object, user_id: object, message_id: object) -> str:
    raw = json.dumps([str(self_id), kind, str(group_id), str(user_id), str(message_id)], separators=(",", ":"))
    return hashlib.sha256(raw.encode()).hexdigest()


class DeliveryLedger:
    def __init__(self, path: Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        new = not self.path.exists()
        with self._connect() as db:
            db.execute("""CREATE TABLE IF NOT EXISTS chat_delivery_parts (
                event_key TEXT NOT NULL, part INTEGER NOT NULL,
                kind TEXT NOT NULL, user_id TEXT NOT NULL, group_id TEXT NOT NULL,
                reply_text TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'pending'
                    CHECK(status IN ('pending','sending','sent','archived','unknown','cancelled')),
                receipt TEXT NOT NULL DEFAULT '', error TEXT NOT NULL DEFAULT '',
                updated_at REAL NOT NULL, PRIMARY KEY(event_key,part))""")
            db.execute("CREATE INDEX IF NOT EXISTS idx_chat_delivery_retention ON chat_delivery_parts(updated_at)")
            db.execute("DELETE FROM chat_delivery_parts WHERE updated_at<?", (time.time() - 30 * 86400,))
        if new:
            self.path.chmod(0o600)

    @contextmanager
    def _connect(self):
        db = sqlite3.connect(self.path, timeout=1)
        db.row_factory = sqlite3.Row
        try:
            with db:
                yield db
        finally:
            db.close()

    def parts(self, key: str) -> list[dict]:
        with self._connect() as db:
            return [dict(row) for row in db.execute(
                "SELECT * FROM chat_delivery_parts WHERE event_key=? ORDER BY part", (key,))]

    def plan(self, key: str, replies, *, kind: str = "group", user_id: str = "", group_id: str = "", source_message_id: str = "") -> list[dict]:
        values = tuple(replies)
        if not values or len(values) > 3 or any(type(item) is not str or len(item) > 1200 for item in values):
            raise ValueError("invalid delivery plan")
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            if kind == "private" and source_message_id:
                live = db.execute("""SELECT 1 FROM private_chat_messages WHERE user_id=?
                    AND message_id=? AND direction='user' AND purged_at IS NULL
                    AND datetime(expires_at)>datetime('now') LIMIT 1""", (str(user_id), source_message_id)).fetchone()
                if not live:
                    return []
            if not db.execute("SELECT 1 FROM chat_delivery_parts WHERE event_key=? LIMIT 1", (key,)).fetchone():
                db.executemany("""INSERT INTO chat_delivery_parts
                    (event_key,part,kind,user_id,group_id,reply_text,updated_at) VALUES(?,?,?,?,?,?,?)""",
                    [(key, i, kind, str(user_id), str(group_id), value, time.time()) for i, value in enumerate(values)])
        return self.parts(key)

    def claim(self, key: str, part: int) -> bool:
        with self._connect() as db:
            return db.execute("""UPDATE chat_delivery_parts SET status='sending',updated_at=?
                WHERE event_key=? AND part=? AND status='pending'""", (time.time(), key, part)).rowcount == 1

    def transition(self, key: str, part: int, *, before: str, after: str, receipt: str = "", error: str = "") -> bool:
        with self._connect() as db:
            return db.execute("""UPDATE chat_delivery_parts SET status=?,receipt=?,error=?,updated_at=?
                WHERE event_key=? AND part=? AND status=?""",
                (after, receipt[:200], error[:80], time.time(), key, part, before)).rowcount == 1


__all__ = ["DeliveryLedger", "delivery_event_key"]
