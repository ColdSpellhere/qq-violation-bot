from __future__ import annotations

import hashlib
import json
import os
import sqlite3
from datetime import datetime, timedelta
from pathlib import Path
from uuid import uuid4


MIME_SUFFIX = {
    "image/jpeg": ".jpg",
    "image/png": ".png",
    "image/gif": ".gif",
    "image/webp": ".webp",
}
SCHEMA = """
CREATE TABLE IF NOT EXISTS evidence_files (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    sha256 TEXT NOT NULL UNIQUE,
    relative_path TEXT NOT NULL UNIQUE,
    mime_type TEXT NOT NULL,
    size_bytes INTEGER NOT NULL,
    source_group_id INTEGER NOT NULL,
    source_message_id TEXT NOT NULL,
    created_at TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'stored'
);
CREATE TABLE IF NOT EXISTS evidence_batches (
    id TEXT PRIMARY KEY,
    group_id INTEGER NOT NULL,
    operator_qq TEXT NOT NULL,
    command_message_id TEXT NOT NULL,
    state TEXT NOT NULL,
    created_at TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    violation_id INTEGER,
    target_qq TEXT
);
CREATE TABLE IF NOT EXISTS evidence_batch_items (
    batch_id TEXT NOT NULL,
    evidence_id INTEGER NOT NULL,
    ordinal INTEGER NOT NULL,
    PRIMARY KEY(batch_id, ordinal),
    FOREIGN KEY(batch_id) REFERENCES evidence_batches(id),
    FOREIGN KEY(evidence_id) REFERENCES evidence_files(id)
);
CREATE TABLE IF NOT EXISTS violation_evidence (
    violation_id INTEGER NOT NULL,
    target_qq TEXT NOT NULL,
    evidence_id INTEGER NOT NULL,
    ordinal INTEGER NOT NULL,
    bound_at TEXT NOT NULL,
    PRIMARY KEY(violation_id, evidence_id),
    FOREIGN KEY(evidence_id) REFERENCES evidence_files(id)
);
CREATE INDEX IF NOT EXISTS idx_violation_evidence_record
ON violation_evidence(violation_id, ordinal);
"""


def _now(value: datetime | None = None) -> str:
    return (value or datetime.now()).strftime("%Y-%m-%d %H:%M:%S")


def write_binding_queue(root: Path, batch_id: str, violation_id: int, target_qq: str) -> Path:
    queue = Path(root) / "bind-queue"
    queue.mkdir(parents=True, exist_ok=True)
    queue.chmod(0o700)
    destination = queue / f"{batch_id}.json"
    temporary = queue / f"{batch_id}.{os.getpid()}.part"
    temporary.write_text(
        json.dumps({"batch_id": batch_id, "violation_id": violation_id, "target_qq": target_qq}),
        encoding="utf-8",
    )
    temporary.chmod(0o600)
    temporary.replace(destination)
    return destination


class EvidenceStore:
    def __init__(self, database_path: Path, root: Path):
        self.database_path = Path(database_path)
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self.root.chmod(0o700)
        self.database_path.parent.mkdir(parents=True, exist_ok=True)
        if not self.database_path.exists():
            descriptor = os.open(
                self.database_path,
                os.O_CREAT | os.O_EXCL | os.O_WRONLY,
                0o600,
            )
            os.close(descriptor)
        self._init_schema()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.database_path)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        return conn

    def _init_schema(self) -> None:
        with self._connect() as conn:
            conn.executescript(SCHEMA)
        self.database_path.chmod(0o600)

    def create_batch(self, group_id: int, operator_qq: str, command_message_id: str) -> str:
        batch_id = uuid4().hex
        created = datetime.now()
        with self._connect() as conn:
            conn.execute(
                "INSERT INTO evidence_batches(id,group_id,operator_qq,command_message_id,state,created_at,expires_at) VALUES(?,?,?,?,?,?,?)",
                (
                    batch_id,
                    group_id,
                    operator_qq,
                    command_message_id,
                    "staging",
                    _now(created),
                    _now(created + timedelta(minutes=3)),
                ),
            )
        return batch_id

    def add_bytes(
        self,
        batch_id: str,
        content: bytes,
        mime_type: str,
        source_group_id: int,
        source_message_id: str,
        ordinal: int,
    ) -> int:
        suffix = MIME_SUFFIX[mime_type]
        digest = hashlib.sha256(content).hexdigest()
        relative = Path("images") / digest[:2] / f"{digest}{suffix}"
        absolute = self.root / relative
        absolute.parent.mkdir(parents=True, exist_ok=True)
        absolute.parent.chmod(0o700)
        if not absolute.exists():
            temporary = absolute.with_name(f"{absolute.name}.{os.getpid()}.part")
            temporary.write_bytes(content)
            temporary.chmod(0o600)
            temporary.replace(absolute)
        with self._connect() as conn:
            conn.execute(
                "INSERT INTO evidence_files(sha256,relative_path,mime_type,size_bytes,source_group_id,source_message_id,created_at) VALUES(?,?,?,?,?,?,?) ON CONFLICT(sha256) DO NOTHING",
                (digest, str(relative), mime_type, len(content), source_group_id, source_message_id, _now()),
            )
            evidence_id = int(
                conn.execute("SELECT id FROM evidence_files WHERE sha256=?", (digest,)).fetchone()["id"]
            )
            conn.execute(
                "INSERT INTO evidence_batch_items(batch_id,evidence_id,ordinal) VALUES(?,?,?) ON CONFLICT(batch_id,ordinal) DO UPDATE SET evidence_id=excluded.evidence_id",
                (batch_id, evidence_id, ordinal),
            )
        return evidence_id

    def batch_count(self, batch_id: str) -> int:
        with self._connect() as conn:
            return int(conn.execute("SELECT COUNT(*) FROM evidence_batch_items WHERE batch_id=?", (batch_id,)).fetchone()[0])

    def bind_batch(self, batch_id: str, violation_id: int, target_qq: str) -> None:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT evidence_id,ordinal FROM evidence_batch_items WHERE batch_id=? ORDER BY ordinal",
                (batch_id,),
            ).fetchall()
            if not rows:
                raise ValueError("evidence batch is empty or missing")
            for row in rows:
                conn.execute(
                    "INSERT INTO violation_evidence(violation_id,target_qq,evidence_id,ordinal,bound_at) VALUES(?,?,?,?,?) ON CONFLICT(violation_id,evidence_id) DO NOTHING",
                    (violation_id, target_qq, row["evidence_id"], row["ordinal"], _now()),
                )
            conn.execute(
                "UPDATE evidence_batches SET state='bound',violation_id=?,target_qq=? WHERE id=?",
                (violation_id, target_qq, batch_id),
            )

    def mark_batch(self, batch_id: str, state: str) -> None:
        if state not in {"cancelled", "expired", "error"}:
            raise ValueError("invalid terminal evidence state")
        with self._connect() as conn:
            conn.execute(
                "UPDATE evidence_batches SET state=? WHERE id=? AND violation_id IS NULL",
                (state, batch_id),
            )

    def paths_for_violations(self, violation_ids: list[int]) -> dict[int, tuple[Path, ...]]:
        result: dict[int, tuple[Path, ...]] = {value: () for value in violation_ids}
        if not violation_ids:
            return result
        placeholders = ",".join("?" for _ in violation_ids)
        with self._connect() as conn:
            rows = conn.execute(
                f"SELECT ve.violation_id,ef.relative_path FROM violation_evidence ve JOIN evidence_files ef ON ef.id=ve.evidence_id WHERE ve.violation_id IN ({placeholders}) ORDER BY ve.violation_id,ve.ordinal",
                violation_ids,
            ).fetchall()
        collected: dict[int, list[Path]] = {value: [] for value in violation_ids}
        for row in rows:
            path = self.root / row["relative_path"]
            if path.is_file():
                collected[int(row["violation_id"])].append(path)
        return {key: tuple(paths) for key, paths in collected.items()}

    def queue_binding(self, batch_id: str, violation_id: int, target_qq: str) -> Path:
        return write_binding_queue(self.root, batch_id, violation_id, target_qq)

    def retry_binding_queue(self) -> int:
        completed = 0
        queue = self.root / "bind-queue"
        if not queue.exists():
            return completed
        for path in sorted(queue.glob("*.json")):
            try:
                payload = json.loads(path.read_text(encoding="utf-8"))
                self.bind_batch(payload["batch_id"], int(payload["violation_id"]), str(payload["target_qq"]))
                path.unlink()
                completed += 1
            except (OSError, ValueError, KeyError, json.JSONDecodeError, sqlite3.Error):
                continue
        return completed

    def cleanup_transient(self, now: datetime | None = None) -> dict[str, int]:
        current = now or datetime.now()
        removed_parts = 0
        for path in self.root.rglob("*.part"):
            try:
                if datetime.fromtimestamp(path.stat().st_mtime) < current - timedelta(hours=1):
                    path.unlink()
                    removed_parts += 1
            except OSError:
                continue
        cutoff = _now(current - timedelta(days=7))
        removed_files = 0
        conn = self._connect()
        try:
            batch_ids = [
                str(row["id"])
                for row in conn.execute(
                    "SELECT id FROM evidence_batches WHERE violation_id IS NULL AND state IN ('staging','cancelled','expired','error') AND created_at<?",
                    (cutoff,),
                ).fetchall()
            ]
        finally:
            conn.close()
        for batch_id in batch_ids:
            conn = None
            batch_removed_files = 0
            try:
                conn = self._connect()
                with conn:
                    evidence_ids = [
                        int(row[0])
                        for row in conn.execute(
                            "SELECT evidence_id FROM evidence_batch_items WHERE batch_id=?", (batch_id,)
                        )
                    ]
                    conn.execute("DELETE FROM evidence_batch_items WHERE batch_id=?", (batch_id,))
                    conn.execute("DELETE FROM evidence_batches WHERE id=?", (batch_id,))
                    for evidence_id in evidence_ids:
                        bound = conn.execute(
                            "SELECT 1 FROM violation_evidence WHERE evidence_id=?", (evidence_id,)
                        ).fetchone()
                        pending = conn.execute(
                            "SELECT 1 FROM evidence_batch_items WHERE evidence_id=?", (evidence_id,)
                        ).fetchone()
                        if bound or pending:
                            continue
                        row = conn.execute(
                            "SELECT relative_path FROM evidence_files WHERE id=?", (evidence_id,)
                        ).fetchone()
                        if row:
                            (self.root / row["relative_path"]).unlink(missing_ok=True)
                            conn.execute("DELETE FROM evidence_files WHERE id=?", (evidence_id,))
                            batch_removed_files += 1
            except (OSError, sqlite3.Error):
                continue
            finally:
                if conn is not None:
                    conn.close()
            removed_files += batch_removed_files
        return {"parts": removed_parts, "files": removed_files}
