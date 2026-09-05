"""Instance-local durable delivery ledger. Stores only the redacted report.

No API can make a remote QQ send atomic with a local SQLite commit. A timeout
or interrupted send is therefore retained as delivery_unknown for explicit
operator resolution; only a definite OneBot rejection is retried automatically.
"""
from __future__ import annotations

import hashlib
import os
import sqlite3
import stat
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

MAX_REPORT_CHARS = 2048
MAX_PENDING_ALERTS = 10000
DEFAULT_ATTEMPT_LIMIT = 5
LEASE_SECONDS = 90
_SCHEMA = """
CREATE TABLE IF NOT EXISTS content_alert_outbox (
    event_key TEXT PRIMARY KEY, alert_id TEXT NOT NULL UNIQUE,
    self_id TEXT NOT NULL, source_group_id INTEGER NOT NULL,
    source_message_id TEXT NOT NULL, report_group_id INTEGER NOT NULL,
    rule_generation TEXT NOT NULL, report_text TEXT NOT NULL,
    status TEXT NOT NULL CHECK(status IN
        ('pending','leased','sending','delivered','delivery_unknown','exhausted')),
    attempt_count INTEGER NOT NULL DEFAULT 0,
    attempt_limit INTEGER NOT NULL DEFAULT 5,
    next_attempt_at REAL NOT NULL, lease_token TEXT NOT NULL DEFAULT '',
    lease_until REAL, last_error TEXT NOT NULL DEFAULT '',
    receipt_message_id TEXT NOT NULL DEFAULT '',
    created_at REAL NOT NULL, updated_at REAL NOT NULL, delivered_at REAL
);
CREATE INDEX IF NOT EXISTS content_alert_due
ON content_alert_outbox(status,next_attempt_at,created_at);
CREATE TABLE IF NOT EXISTS content_alert_attempts (
    event_key TEXT NOT NULL, attempt_number INTEGER NOT NULL,
    status TEXT NOT NULL, started_at REAL NOT NULL, finished_at REAL,
    error_code TEXT NOT NULL DEFAULT '', receipt_message_id TEXT NOT NULL DEFAULT '',
    PRIMARY KEY(event_key,attempt_number)
);
CREATE TABLE IF NOT EXISTS content_alert_operator_actions (
    id INTEGER PRIMARY KEY, event_key TEXT NOT NULL, action TEXT NOT NULL,
    actor TEXT NOT NULL, created_at REAL NOT NULL
);
"""


def event_identity(self_id: object, group_id: object, message_id: object) -> tuple[str, str]:
    key = hashlib.sha256(f"{self_id}:{group_id}:{message_id}".encode()).hexdigest()
    return key, f"KA-{key[:12]}"


class AlertOutbox:
    def __init__(self, path: Path):
        self.path = Path(os.path.abspath(path))

    def _prepare_path(self) -> None:
        for item in (*reversed(self.path.parents), self.path):
            if item.is_symlink():
                raise ValueError("alert outbox path must not contain symlinks")
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        if self.path.exists():
            info = self.path.stat()
            if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                raise ValueError("alert outbox must be a regular single-link file")
        else:
            try:
                descriptor = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
                os.close(descriptor)
            except FileExistsError:
                # A concurrent first enqueue may have created it; validate again.
                self._prepare_path()
        self.path.chmod(0o600)

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        self._prepare_path()
        connection = sqlite3.connect(self.path, timeout=2)
        connection.row_factory = sqlite3.Row
        try:
            connection.execute("PRAGMA busy_timeout=2000")
            connection.executescript(_SCHEMA)
            connection.execute("BEGIN IMMEDIATE")
            yield connection
            connection.commit()
        except BaseException:
            connection.rollback()
            raise
        finally:
            connection.close()

    def enqueue(self, *, self_id: str, source_group_id: int, source_message_id: str,
                report_group_id: int, rule_generation: str, report_text: str,
                now: float) -> tuple[str, bool]:
        if not report_text or len(report_text) > MAX_REPORT_CHARS:
            raise ValueError("alert report exceeds persistence budget")
        if len(rule_generation) > 512:
            raise ValueError("alert rule generation exceeds metadata budget")
        key, alert_id = event_identity(self_id, source_group_id, source_message_id)
        with self._connect() as connection:
            if connection.execute("SELECT 1 FROM content_alert_outbox WHERE event_key=?", (key,)).fetchone():
                return key, False
            count = connection.execute(
                "SELECT COUNT(*) FROM content_alert_outbox WHERE status<>'delivered'"
            ).fetchone()[0]
            if count >= MAX_PENDING_ALERTS:
                raise RuntimeError("alert outbox pending capacity reached")
            connection.execute(
                "INSERT INTO content_alert_outbox(event_key,alert_id,self_id,source_group_id,"
                "source_message_id,report_group_id,rule_generation,report_text,status,"
                "next_attempt_at,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,'pending',?,?,?)",
                (key, alert_id, self_id, source_group_id, source_message_id,
                 report_group_id, rule_generation, report_text, now, now, now),
            )
        return key, True

    @staticmethod
    def _recover(connection: sqlite3.Connection, now: float) -> None:
        # No network send has begun for a merely leased row.
        connection.execute(
            "UPDATE content_alert_outbox SET status='pending',lease_token='',lease_until=NULL,"
            "updated_at=? WHERE status='leased' AND lease_until<=?", (now, now),
        )
        expired = connection.execute(
            "SELECT event_key,attempt_count FROM content_alert_outbox "
            "WHERE status='sending' AND lease_until<=?", (now,),
        ).fetchall()
        for row in expired:
            connection.execute(
                "UPDATE content_alert_attempts SET status='delivery_unknown',"
                "error_code='send_interrupted',finished_at=? WHERE event_key=? AND attempt_number=?",
                (now, row['event_key'], row['attempt_count']),
            )
        connection.execute(
            "UPDATE content_alert_outbox SET status='delivery_unknown',last_error='send_interrupted',"
            "lease_token='',lease_until=NULL,updated_at=? "
            "WHERE status='sending' AND lease_until<=?", (now, now),
        )

    def recover(self, now: float) -> None:
        if not self.path.exists():
            return
        with self._connect() as connection:
            self._recover(connection, now)

    def claim(self, *, now: float, self_id: str, event_key: str | None = None) -> dict | None:
        if not self.path.exists():
            return None
        with self._connect() as connection:
            self._recover(connection, now)
            row = connection.execute(
                "SELECT * FROM content_alert_outbox WHERE status='pending' AND self_id=? "
                "AND next_attempt_at<=? AND (? IS NULL OR event_key=?) "
                "ORDER BY created_at,event_key LIMIT 1", (self_id, now, event_key, event_key),
            ).fetchone()
            if row is None:
                return None
            token = uuid.uuid4().hex
            connection.execute(
                "UPDATE content_alert_outbox SET status='leased',lease_token=?,lease_until=?,"
                "updated_at=? WHERE event_key=?", (token, now + LEASE_SECONDS, now, row['event_key']),
            )
            return {**dict(row), 'status': 'leased', 'lease_token': token}

    def release(self, row: dict, *, now: float) -> None:
        with self._connect() as connection:
            connection.execute(
                "UPDATE content_alert_outbox SET status='pending',lease_token='',lease_until=NULL,"
                "next_attempt_at=?,updated_at=? WHERE event_key=? AND lease_token=? AND status='leased'",
                (now + 5, now, row['event_key'], row['lease_token']),
            )

    def begin_send(self, row: dict, *, now: float) -> bool:
        with self._connect() as connection:
            updated = connection.execute(
                "UPDATE content_alert_outbox SET status='sending',attempt_count=attempt_count+1,"
                "updated_at=? WHERE event_key=? AND lease_token=? AND status='leased' AND lease_until>?",
                (now, row['event_key'], row['lease_token'], now),
            ).rowcount
            if not updated:
                return False
            current = connection.execute(
                "SELECT attempt_count FROM content_alert_outbox WHERE event_key=?", (row['event_key'],)
            ).fetchone()
            connection.execute(
                "INSERT INTO content_alert_attempts(event_key,attempt_number,status,started_at) "
                "VALUES(?,?,'sending',?)", (row['event_key'], current[0], now),
            )
            row['attempt_count'] = current[0]
        return True

    def finish(self, row: dict, *, outcome: str, now: float, receipt: str = '') -> bool:
        if outcome not in {'delivered', 'rejected', 'delivery_unknown'}:
            raise ValueError("invalid alert delivery outcome")
        with self._connect() as connection:
            current = connection.execute(
                "SELECT * FROM content_alert_outbox WHERE event_key=? AND lease_token=? AND status='sending'",
                (row['event_key'], row['lease_token']),
            ).fetchone()
            if current is None:
                return False
            status = outcome
            if outcome == 'rejected':
                status = 'exhausted' if current['attempt_count'] >= current['attempt_limit'] else 'pending'
            delay = min(300, 5 * (2 ** min(current['attempt_count'] - 1, 6)))
            error = '' if outcome == 'delivered' else 'onebot_rejected' if outcome == 'rejected' else 'send_result_unknown'
            connection.execute(
                "UPDATE content_alert_outbox SET status=?,last_error=?,next_attempt_at=?,"
                "lease_token='',lease_until=NULL,receipt_message_id=?,updated_at=?,delivered_at=? "
                "WHERE event_key=?",
                (status, error, now + delay, str(receipt)[:128], now,
                 now if outcome == 'delivered' else None, row['event_key']),
            )
            connection.execute(
                "UPDATE content_alert_attempts SET status=?,error_code=?,finished_at=?,receipt_message_id=? "
                "WHERE event_key=? AND attempt_number=?",
                (outcome, error, now, str(receipt)[:128], row['event_key'], current['attempt_count']),
            )
        return True

    def abort_unsent(self, row: dict, *, now: float) -> None:
        """The feature gate closed after claim; no QQ invocation took place."""
        with self._connect() as connection:
            changed = connection.execute(
                "UPDATE content_alert_outbox SET status='pending',lease_token='',lease_until=NULL,"
                "next_attempt_at=?,updated_at=?,attempt_limit=attempt_limit+1 "
                "WHERE event_key=? AND lease_token=? AND status='sending'",
                (now + 5, now, row['event_key'], row['lease_token']),
            ).rowcount
            if changed:
                connection.execute(
                    "UPDATE content_alert_attempts SET status='not_sent',finished_at=?,"
                    "error_code='feature_disabled' WHERE event_key=? AND attempt_number=?",
                    (now, row['event_key'], row['attempt_count']),
                )

    def states(self, alert_id: str | None = None) -> list[dict]:
        if not self.path.exists():
            return []
        with self._connect() as connection:
            if alert_id is not None:
                rows = connection.execute(
                    "SELECT alert_id,status,attempt_count,last_error,rule_generation FROM content_alert_outbox "
                    "WHERE alert_id=?", (alert_id,),
                ).fetchall()
            else:
                rows = connection.execute(
                    "SELECT status,COUNT(*) AS count FROM content_alert_outbox GROUP BY status"
                ).fetchall()
            return [dict(row) for row in rows]

    def unresolved(self) -> list[dict]:
        if not self.path.exists():
            return []
        with self._connect() as connection:
            return [dict(row) for row in connection.execute(
                "SELECT alert_id,status,attempt_count FROM content_alert_outbox "
                "WHERE status IN ('delivery_unknown','exhausted') "
                "ORDER BY updated_at,event_key LIMIT 10"
            ).fetchall()]

    def resolve(self, alert_id: str, *, action: str, actor: str, now: float) -> bool:
        if action not in {'retry', 'confirm_delivered'}:
            raise ValueError("invalid alert resolution")
        if not self.path.exists():
            return False
        with self._connect() as connection:
            row = connection.execute(
                "SELECT event_key FROM content_alert_outbox WHERE alert_id=? "
                "AND status IN ('delivery_unknown','exhausted')", (alert_id,),
            ).fetchone()
            if row is None:
                return False
            connection.execute(
                "UPDATE content_alert_outbox SET status=?,next_attempt_at=?,last_error='',updated_at=?,"
                "attempt_limit=attempt_count+?,delivered_at=? WHERE event_key=?",
                ('pending' if action == 'retry' else 'delivered', now, now,
                 DEFAULT_ATTEMPT_LIMIT, now if action == 'confirm_delivered' else None, row['event_key']),
            )
            connection.execute(
                "INSERT INTO content_alert_operator_actions(event_key,action,actor,created_at) VALUES(?,?,?,?)",
                (row['event_key'], action, str(actor)[:128], now),
            )
        return True
