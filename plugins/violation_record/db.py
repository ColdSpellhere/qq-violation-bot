import json
import os
import sqlite3
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Any, Iterator

from .config import BACKUP_DIR, CONFIG, ensure_dirs, parse_admin_seed


def now_str() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def compact_time() -> str:
    return datetime.now().strftime("%Y%m%d_%H%M%S")


def backup_database(reason: str = "manual") -> Path | None:
    ensure_dirs()
    source_path = CONFIG.database_path
    if not source_path.exists():
        return None
    BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    destination = BACKUP_DIR / f"db_backup_{reason}_{compact_time()}.sqlite3"
    temporary = destination.with_suffix(destination.suffix + f".{os.getpid()}.part")
    try:
        temporary.unlink(missing_ok=True)
        descriptor = os.open(temporary, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        os.close(descriptor)
        with sqlite3.connect(source_path) as source, sqlite3.connect(temporary) as target:
            source.backup(target)
        with sqlite3.connect(temporary) as check:
            result = check.execute("PRAGMA integrity_check").fetchone()[0]
        if result != "ok":
            raise sqlite3.DatabaseError(f"backup integrity_check returned {result!r}")
        temporary.chmod(0o600)
        temporary.replace(destination)
        return destination
    except Exception:
        temporary.unlink(missing_ok=True)
        raise


@contextmanager
def connect() -> Iterator[sqlite3.Connection]:
    ensure_dirs()
    CONFIG.database_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(CONFIG.database_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA busy_timeout = 5000")
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def init_db() -> None:
    if CONFIG.database_path.exists():
        backup_database("before_migrate")
    with connect() as conn:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS members (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                qq_number TEXT UNIQUE NOT NULL,
                qq_nickname TEXT,
                aliases TEXT DEFAULT '[]',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS admins (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                qq_number TEXT UNIQUE NOT NULL,
                nickname TEXT NOT NULL,
                aliases TEXT DEFAULT '[]',
                is_active INTEGER NOT NULL DEFAULT 1,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS member_group_states (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                member_id INTEGER NOT NULL,
                group_area TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT '正常',
                locked INTEGER NOT NULL DEFAULT 0,
                total_count INTEGER NOT NULL DEFAULT 0,
                deduct_count INTEGER NOT NULL DEFAULT 0,
                current_count_cache INTEGER NOT NULL DEFAULT 0,
                last_effective_violation_time TEXT,
                last_deduct_time TEXT,
                last_final_warning_time TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                UNIQUE(member_id, group_area),
                FOREIGN KEY(member_id) REFERENCES members(id)
            );

            CREATE TABLE IF NOT EXISTS violation_records (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                member_id INTEGER NOT NULL,
                group_area TEXT NOT NULL,
                violation_time TEXT NOT NULL,
                judgement TEXT NOT NULL,
                action TEXT NOT NULL,
                handler_admin_id INTEGER,
                recorder_admin_id INTEGER,
                remark TEXT DEFAULT '无',
                is_countable INTEGER NOT NULL DEFAULT 1,
                count_delta INTEGER NOT NULL DEFAULT 1,
                is_withdrawn INTEGER NOT NULL DEFAULT 0,
                withdrawn_reason TEXT,
                is_test INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                FOREIGN KEY(member_id) REFERENCES members(id),
                FOREIGN KEY(handler_admin_id) REFERENCES admins(id),
                FOREIGN KEY(recorder_admin_id) REFERENCES admins(id)
            );

            CREATE TABLE IF NOT EXISTS consultation_records (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                member_id INTEGER NOT NULL,
                group_area TEXT NOT NULL,
                consultation_type TEXT NOT NULL,
                consultation_time TEXT NOT NULL,
                consultant_admin_id INTEGER,
                result TEXT NOT NULL,
                status_after TEXT NOT NULL,
                remark TEXT DEFAULT '无',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                FOREIGN KEY(member_id) REFERENCES members(id),
                FOREIGN KEY(consultant_admin_id) REFERENCES admins(id)
            );

            CREATE TABLE IF NOT EXISTS operation_logs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                group_area TEXT,
                operation_type TEXT NOT NULL,
                source TEXT NOT NULL,
                operator_qq TEXT,
                operator_nickname TEXT,
                target_member_id INTEGER,
                before_json TEXT,
                after_json TEXT,
                message_id TEXT,
                created_at TEXT NOT NULL,
                remark TEXT,
                FOREIGN KEY(target_member_id) REFERENCES members(id)
            );

            CREATE TABLE IF NOT EXISTS pending_operations (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                group_id TEXT NOT NULL,
                operator_qq TEXT NOT NULL,
                operation_type TEXT NOT NULL,
                payload_json TEXT NOT NULL,
                expires_at TEXT NOT NULL,
                created_at TEXT NOT NULL,
                UNIQUE(group_id, operator_qq)
            );

            CREATE TABLE IF NOT EXISTS business_notification_outbox (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                idempotency_key TEXT NOT NULL UNIQUE,
                message_type TEXT NOT NULL,
                message_text TEXT NOT NULL,
                reason TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'pending' CHECK(
                    status IN ('pending', 'failed', 'sending', 'sent')
                ),
                sent_at TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );

            CREATE INDEX IF NOT EXISTS idx_business_notification_outbox_status
            ON business_notification_outbox(status, updated_at, id);
            """
        )
        ensure_business_notification_outbox_schema(conn)
        ensure_schema_extensions(conn)
        seed_admins(conn)


def _column_names(conn: sqlite3.Connection, table: str) -> set[str]:
    return {row["name"] for row in conn.execute(f"PRAGMA table_info({table})").fetchall()}


def _add_column_if_missing(conn: sqlite3.Connection, table: str, column: str, definition: str) -> None:
    if column not in _column_names(conn, table):
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")


def ensure_business_notification_outbox_schema(conn: sqlite3.Connection) -> None:
    table_sql = conn.execute(
        """
        SELECT sql FROM sqlite_master
        WHERE type='table' AND name='business_notification_outbox'
        """
    ).fetchone()["sql"]
    if "status IN ('pending', 'failed', 'sending', 'sent')" in table_sql:
        return

    conn.execute("BEGIN IMMEDIATE")
    conn.execute("DROP INDEX IF EXISTS idx_business_notification_outbox_status")
    conn.execute(
        """
        ALTER TABLE business_notification_outbox
        RENAME TO business_notification_outbox_legacy_state
        """
    )
    conn.execute(
        """
        CREATE TABLE business_notification_outbox (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            idempotency_key TEXT NOT NULL UNIQUE,
            message_type TEXT NOT NULL,
            message_text TEXT NOT NULL,
            reason TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'pending' CHECK(
                status IN ('pending', 'failed', 'sending', 'sent')
            ),
            sent_at TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )
        """
    )
    conn.execute(
        """
        INSERT INTO business_notification_outbox(
            id, idempotency_key, message_type, message_text, reason,
            status, sent_at, created_at, updated_at
        )
        SELECT
            id, idempotency_key, message_type, message_text, reason,
            status, sent_at, created_at, updated_at
        FROM business_notification_outbox_legacy_state
        """
    )
    conn.execute("DROP TABLE business_notification_outbox_legacy_state")
    conn.execute(
        """
        CREATE INDEX idx_business_notification_outbox_status
        ON business_notification_outbox(status, updated_at, id)
        """
    )


LEGACY_IMPORT_INDEXES = (
    "idx_violation_import_source",
    "idx_consultation_import_source",
)


LEGACY_IMPORT_COLUMNS = {
    "member_group_states": (
        "import_total_count",
        "import_source_sheet",
        "import_area_inferred",
        "import_note",
    ),
    "violation_records": (
        "import_batch_id",
        "import_source_file",
        "import_source_sheet",
        "import_source_area_inferred",
        "import_source_row",
        "import_source_col",
        "raw_member_text",
        "raw_record_text",
        "handler_admin_name_text",
        "recorder_admin_name_text",
        "imported_at",
    ),
    "consultation_records": (
        "import_batch_id",
        "import_source_file",
        "import_source_sheet",
        "import_source_row",
        "raw_record_text",
        "consultant_admin_name_text",
        "imported_at",
    ),
}


def _append_remark(remark: str | None, notes: list[str]) -> str:
    base = (remark or "").strip()
    additions = [note for note in notes if note and note not in base]
    if not additions:
        return base or "无"
    if not base or base == "无":
        return "；".join(additions)
    return f"{base}；{'；'.join(additions)}"


def _preserve_legacy_admin_text(conn: sqlite3.Connection) -> None:
    violation_columns = _column_names(conn, "violation_records")
    if {"handler_admin_name_text", "recorder_admin_name_text"}.issubset(violation_columns):
        rows = conn.execute(
            """
            SELECT id, remark, handler_admin_name_text, recorder_admin_name_text
            FROM violation_records
            WHERE COALESCE(TRIM(handler_admin_name_text), '') != ''
                OR COALESCE(TRIM(recorder_admin_name_text), '') != ''
            """
        ).fetchall()
        for row in rows:
            handler = (row["handler_admin_name_text"] or "").strip()
            recorder = (row["recorder_admin_name_text"] or "").strip()
            notes = []
            if handler:
                notes.append(f"历史处理人：{handler}")
            if recorder and recorder != "Excel导入":
                notes.append(f"历史记录人：{recorder}")
            remark = _append_remark(row["remark"], notes)
            if remark != (row["remark"] or "").strip():
                conn.execute("UPDATE violation_records SET remark=? WHERE id=?", (remark, row["id"]))

    consultation_columns = _column_names(conn, "consultation_records")
    if "consultant_admin_name_text" in consultation_columns:
        rows = conn.execute(
            """
            SELECT id, remark, consultant_admin_name_text
            FROM consultation_records
            WHERE COALESCE(TRIM(consultant_admin_name_text), '') != ''
            """
        ).fetchall()
        for row in rows:
            consultant = (row["consultant_admin_name_text"] or "").strip()
            notes = [] if consultant == "Excel导入" else [f"历史质询人：{consultant}"]
            remark = _append_remark(row["remark"], notes)
            if remark != (row["remark"] or "").strip():
                conn.execute("UPDATE consultation_records SET remark=? WHERE id=?", (remark, row["id"]))


def _drop_column_if_exists(conn: sqlite3.Connection, table: str, column: str) -> None:
    if column in _column_names(conn, table):
        conn.execute(f"ALTER TABLE {table} DROP COLUMN {column}")


def ensure_schema_extensions(conn: sqlite3.Connection) -> None:
    """Remove legacy import-only columns from existing SQLite databases."""
    _preserve_legacy_admin_text(conn)
    for index in LEGACY_IMPORT_INDEXES:
        conn.execute(f"DROP INDEX IF EXISTS {index}")
    for table, columns in LEGACY_IMPORT_COLUMNS.items():
        for column in columns:
            _drop_column_if_exists(conn, table, column)


def seed_admins(conn: sqlite3.Connection) -> None:
    for admin in parse_admin_seed(CONFIG.admin_seed):
        ts = now_str()
        conn.execute(
            """
            INSERT INTO admins(qq_number, nickname, aliases, is_active, created_at, updated_at)
            VALUES(?, ?, ?, 1, ?, ?)
            ON CONFLICT(qq_number) DO UPDATE SET
                nickname=excluded.nickname,
                aliases=excluded.aliases,
                is_active=1,
                updated_at=excluded.updated_at
            """,
            (admin["qq_number"], admin["nickname"], admin["aliases"], ts, ts),
        )


def row_to_dict(row: sqlite3.Row | None) -> dict[str, Any] | None:
    return dict(row) if row else None


def rows_to_dicts(rows: list[sqlite3.Row]) -> list[dict[str, Any]]:
    return [dict(r) for r in rows]


def dump_json(data: Any) -> str:
    return json.dumps(data, ensure_ascii=False, default=str)
