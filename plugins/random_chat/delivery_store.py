"""A durable send ledger; ambiguous OneBot results are never blindly retried."""
from __future__ import annotations

from collections import OrderedDict
import threading
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

    def plan(self, key: str, replies, *, kind: str = "group", user_id: str = "", group_id: str = "", source_message_id: str = "", _terminal_no_reply: bool = False) -> list[dict]:
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
                    (event_key,part,kind,user_id,group_id,reply_text,updated_at,status,error) VALUES(?,?,?,?,?,?,?,?,?)""",
                    [(key, i, kind, str(user_id), str(group_id), value, time.time(),
                      "cancelled" if _terminal_no_reply else "pending", "no_reply" if _terminal_no_reply else "")
                     for i, value in enumerate(values)])
        return self.parts(key)

    def complete_without_reply(self, key: str, **scope) -> None:
        """Retain an empty terminal decision so duplicate events do not rerun AI."""
        self.plan(key, ("",), _terminal_no_reply=True, **scope)

    def claim(self, key: str, part: int) -> bool:
        with self._connect() as db:
            return db.execute("""UPDATE chat_delivery_parts SET status='sending',updated_at=?
                WHERE event_key=? AND part=? AND status='pending'""", (time.time(), key, part)).rowcount == 1

    def transition(self, key: str, part: int, *, before: str, after: str, receipt: str = "", error: str = "") -> bool:
        with self._connect() as db:
            return db.execute("""UPDATE chat_delivery_parts SET status=?,receipt=?,error=?,updated_at=?
                WHERE event_key=? AND part=? AND status=?""",
                (after, receipt[:200], error[:80], time.time(), key, part, before)).rowcount == 1


class MemoryDeliveryLedger:
    """Per-conversation replay protection for users who disabled persistence."""
    def __init__(self, *, max_events: int = 128, ttl_seconds: float = 3600):
        if type(max_events) is not int or max_events < 1 or ttl_seconds <= 0:
            raise ValueError("invalid memory delivery bounds")
        self.max_events = max_events
        self.ttl_seconds = ttl_seconds
        self._events: OrderedDict[str, tuple[float, list[dict]]] = OrderedDict()
        self._lock = threading.Lock()

    def _prune(self):
        now = time.monotonic()
        for key, (created, _) in tuple(self._events.items()):
            if now - created >= self.ttl_seconds:
                del self._events[key]
        while len(self._events) > self.max_events:
            self._events.popitem(last=False)

    def clear(self):
        with self._lock:
            self._events.clear()

    def parts(self, key: str) -> list[dict]:
        with self._lock:
            self._prune()
            event = self._events.get(key)
            return [dict(row) for row in event[1]] if event else []

    def plan(self, key: str, replies, *, kind="private", user_id="", group_id="", source_message_id="", _terminal_no_reply=False) -> list[dict]:
        values = tuple(replies)
        if not values or len(values) > 3 or any(type(item) is not str or len(item) > 1200 for item in values):
            raise ValueError("invalid delivery plan")
        with self._lock:
            self._prune()
            if key not in self._events:
                rows = [dict(event_key=key, part=index, kind=kind, user_id=user_id, group_id=group_id,
                             reply_text=value, status="cancelled" if _terminal_no_reply else "pending",
                             receipt="", error="no_reply" if _terminal_no_reply else "")
                        for index,value in enumerate(values)]
                self._events[key] = (time.monotonic(), rows)
                self._prune()
            return [dict(row) for row in self._events[key][1]]

    def complete_without_reply(self, key: str, **scope) -> None:
        """Retain an empty terminal decision so duplicate events do not rerun AI."""
        self.plan(key, ("",), _terminal_no_reply=True, **scope)

    def claim(self, key: str, part: int) -> bool:
        return self.transition(key,part,before="pending",after="sending")

    def transition(self, key: str, part: int, *, before: str, after: str, receipt="", error="") -> bool:
        with self._lock:
            self._prune()
            event = self._events.get(key)
            if event is None or part >= len(event[1]) or part < 0:
                return False
            row = event[1][part]
            if row["status"] != before:
                return False
            row.update(status=after,receipt=str(receipt)[:200],error=str(error)[:80])
            return True


__all__ = ["DeliveryLedger", "MemoryDeliveryLedger", "delivery_event_key"]
