from __future__ import annotations

import os
import sqlite3
import stat
from contextlib import closing
from pathlib import Path

from .models import MigrationReport


PRIVATE_MEMORY_SCHEMA_VERSION = 1

_TABLE_STATEMENTS = (
    """
    CREATE TABLE IF NOT EXISTS private_chat_messages (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id TEXT NOT NULL,
        message_id TEXT NOT NULL,
        direction TEXT NOT NULL CHECK(direction IN ('user','assistant')),
        text TEXT NOT NULL,
        content_hash TEXT NOT NULL,
        event_time INTEGER NOT NULL,
        created_at TEXT NOT NULL,
        expires_at TEXT NOT NULL,
        purged_at TEXT,
        source_kind TEXT NOT NULL,
        source_message_id TEXT,
        UNIQUE(user_id,direction,message_id)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS private_conversation_summaries (
        user_id TEXT PRIMARY KEY,
        summary_text TEXT NOT NULL,
        source_start_id INTEGER NOT NULL,
        source_end_id INTEGER NOT NULL,
        summarized_through_id INTEGER NOT NULL,
        version INTEGER NOT NULL DEFAULT 1 CHECK(version > 0),
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        CHECK(source_start_id >= 0),
        CHECK(source_end_id >= source_start_id),
        CHECK(summarized_through_id >= source_end_id)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS private_memory_facts (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id TEXT NOT NULL,
        fact_text TEXT NOT NULL,
        normalized_text TEXT NOT NULL,
        source_message_id TEXT NOT NULL,
        source_quote TEXT NOT NULL DEFAULT '',
        trust_level TEXT NOT NULL DEFAULT 'ai_extracted'
            CHECK(trust_level IN ('ai_extracted','admin_confirmed')),
        status TEXT NOT NULL DEFAULT 'active'
            CHECK(status IN ('active','superseded','deleted')),
        supersedes_id INTEGER,
        version INTEGER NOT NULL DEFAULT 1 CHECK(version > 0),
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        deleted_at TEXT,
        UNIQUE(user_id,normalized_text,source_message_id),
        FOREIGN KEY(supersedes_id) REFERENCES private_memory_facts(id)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS relationship_states (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        conversation_kind TEXT NOT NULL
            CHECK(conversation_kind IN ('group','private')),
        group_id INTEGER,
        user_id TEXT NOT NULL,
        persona_id TEXT NOT NULL,
        state_text TEXT NOT NULL DEFAULT '',
        open_topics_json TEXT NOT NULL DEFAULT '[]',
        preferred_address TEXT NOT NULL DEFAULT '',
        communication_style TEXT NOT NULL DEFAULT '',
        source_message_id TEXT NOT NULL DEFAULT '',
        source_watermark INTEGER NOT NULL DEFAULT 0 CHECK(source_watermark >= 0),
        version INTEGER NOT NULL DEFAULT 1 CHECK(version > 0),
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        CHECK(
            (conversation_kind='group' AND group_id IS NOT NULL)
            OR (conversation_kind='private' AND group_id IS NULL)
        )
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS memory_jobs (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        job_type TEXT NOT NULL
            CHECK(job_type IN ('private_summary','private_facts','relationship')),
        conversation_kind TEXT NOT NULL
            CHECK(conversation_kind IN ('group','private')),
        group_id INTEGER,
        user_id TEXT NOT NULL,
        persona_id TEXT NOT NULL DEFAULT 'radish-cat',
        input_through_id INTEGER NOT NULL CHECK(input_through_id >= 0),
        expected_version INTEGER NOT NULL CHECK(expected_version >= 0),
        status TEXT NOT NULL DEFAULT 'pending'
            CHECK(status IN ('pending','running','succeeded','failed','cancelled')),
        attempts INTEGER NOT NULL DEFAULT 0 CHECK(attempts >= 0),
        next_run_at TEXT NOT NULL,
        lease_owner TEXT,
        lease_expires_at TEXT,
        claim_version INTEGER NOT NULL DEFAULT 0 CHECK(claim_version >= 0),
        error_code TEXT NOT NULL DEFAULT '',
        error_summary TEXT NOT NULL DEFAULT '',
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        CHECK(
            (conversation_kind='group' AND group_id IS NOT NULL)
            OR (conversation_kind='private' AND group_id IS NULL)
        )
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS memory_pending_operations (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        confirmation_token_hash TEXT NOT NULL UNIQUE CHECK(
            length(confirmation_token_hash)=64
            AND confirmation_token_hash NOT GLOB '*[^0-9a-f]*'
        ),
        operator_user_id TEXT NOT NULL,
        operation_type TEXT NOT NULL,
        target_kind TEXT NOT NULL CHECK(target_kind IN ('group','private','fact','relationship')),
        target_group_id INTEGER,
        target_user_id TEXT NOT NULL,
        target_memory_id INTEGER,
        payload_json TEXT NOT NULL,
        preview_text TEXT NOT NULL,
        expires_at TEXT NOT NULL,
        consumed_at TEXT,
        created_at TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS memory_governance_audit (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        operation_id INTEGER,
        operator_user_id TEXT NOT NULL,
        target_kind TEXT NOT NULL,
        target_group_id INTEGER,
        target_user_id TEXT NOT NULL,
        target_memory_id INTEGER,
        operation_type TEXT NOT NULL,
        before_hash TEXT NOT NULL DEFAULT '',
        after_hash TEXT NOT NULL DEFAULT '',
        reason TEXT NOT NULL,
        result TEXT NOT NULL CHECK(result IN ('success','failed','cancelled')),
        error_code TEXT NOT NULL DEFAULT '',
        created_at TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS private_memory_schema_meta (
        singleton INTEGER PRIMARY KEY CHECK(singleton=1),
        schema_version INTEGER NOT NULL CHECK(schema_version > 0),
        updated_at TEXT NOT NULL
    )
    """,
)

_INDEX_STATEMENTS = (
    "CREATE INDEX IF NOT EXISTS idx_private_chat_messages_user_id "
    "ON private_chat_messages(user_id,id)",
    "CREATE INDEX IF NOT EXISTS idx_private_chat_messages_expiry "
    "ON private_chat_messages(expires_at,id) WHERE purged_at IS NULL",
    "CREATE INDEX IF NOT EXISTS idx_private_memory_facts_active "
    "ON private_memory_facts(user_id,id) WHERE status='active'",
    "CREATE INDEX IF NOT EXISTS idx_relationship_states_scope "
    "ON relationship_states(conversation_kind,group_id,user_id,persona_id)",
    "CREATE UNIQUE INDEX IF NOT EXISTS idx_relationship_states_group_unique "
    "ON relationship_states(group_id,user_id,persona_id) WHERE conversation_kind='group'",
    "CREATE UNIQUE INDEX IF NOT EXISTS idx_relationship_states_private_unique "
    "ON relationship_states(user_id,persona_id) WHERE conversation_kind='private'",
    "CREATE INDEX IF NOT EXISTS idx_memory_jobs_runnable "
    "ON memory_jobs(status,next_run_at,id)",
    "CREATE UNIQUE INDEX IF NOT EXISTS idx_memory_jobs_active_unique "
    "ON memory_jobs(job_type,conversation_kind,ifnull(group_id,-1),user_id,persona_id,input_through_id) "
    "WHERE status IN ('pending','running')",
    "CREATE INDEX IF NOT EXISTS idx_memory_pending_operations_expiry "
    "ON memory_pending_operations(expires_at,id) WHERE consumed_at IS NULL",
)

_MEMBER_FACT_COLUMNS = (
    ("trust_level", "TEXT NOT NULL DEFAULT 'ai_extracted' CHECK(trust_level IN ('ai_extracted','admin_confirmed'))"),
    ("status", "TEXT NOT NULL DEFAULT 'active' CHECK(status IN ('active','superseded','deleted'))"),
    ("supersedes_id", "INTEGER"),
    ("updated_at", "TEXT"),
    ("version", "INTEGER NOT NULL DEFAULT 1 CHECK(version > 0)"),
    ("deleted_at", "TEXT"),
)


def quick_check(path: Path) -> str:
    path = Path(path)
    if not path.exists():
        return "ok"
    with closing(sqlite3.connect(f"file:{path}?mode=ro", uri=True)) as connection:
        row = connection.execute("PRAGMA quick_check").fetchone()
    if row is None:
        raise sqlite3.DatabaseError("quick_check returned no result")
    result = str(row[0])
    if result != "ok":
        raise sqlite3.DatabaseError(f"quick_check returned {result!r}")
    return result


def require_regular_database(path: Path) -> Path:
    path = Path(path)
    if not path.exists():
        raise FileNotFoundError(f"database does not exist: {path}")
    if not path.is_file():
        raise ValueError(f"database is not a regular file: {path}")
    return path


def validate_backup_directory(path: Path) -> Path:
    path = Path(path)
    if not path.exists():
        return path
    if not path.is_dir():
        raise ValueError(f"backup directory is not a directory: {path}")
    mode = stat.S_IMODE(path.stat().st_mode)
    if mode != 0o700:
        raise PermissionError(
            f"existing backup directory must have mode 0700, got {mode:04o}: {path}"
        )
    return path


def schema_version(path: Path) -> int:
    path = Path(path)
    if not path.exists():
        return 0
    try:
        with closing(sqlite3.connect(f"file:{path}?mode=ro", uri=True)) as connection:
            row = connection.execute(
                "SELECT schema_version FROM private_memory_schema_meta WHERE singleton=1"
            ).fetchone()
    except sqlite3.OperationalError:
        return 0
    return int(row[0]) if row else 0


def _existing_objects(connection: sqlite3.Connection, kind: str) -> set[str]:
    return {
        str(row[0])
        for row in connection.execute("SELECT name FROM sqlite_master WHERE type=?", (kind,))
    }


def migrate(path: Path) -> MigrationReport:
    path = Path(path)
    if path.exists():
        quick_check(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    created_file = not path.exists()
    if created_file:
        descriptor = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        os.close(descriptor)

    tables_created = 0
    columns_added = 0
    try:
        with closing(sqlite3.connect(path)) as connection:
            connection.execute("PRAGMA foreign_keys=ON")
            connection.execute("PRAGMA busy_timeout=5000")
            before_tables = _existing_objects(connection, "table")
            connection.execute("BEGIN IMMEDIATE")
            for statement in _TABLE_STATEMENTS:
                connection.execute(statement)

            member_facts_exists = connection.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='member_memory_facts'"
            ).fetchone()
            if member_facts_exists:
                existing_columns = {
                    str(row[1])
                    for row in connection.execute("PRAGMA table_info(member_memory_facts)")
                }
                for name, definition in _MEMBER_FACT_COLUMNS:
                    if name not in existing_columns:
                        connection.execute(
                            f"ALTER TABLE member_memory_facts ADD COLUMN {name} {definition}"
                        )
                        columns_added += 1
                connection.execute(
                    "UPDATE member_memory_facts SET updated_at=created_at "
                    "WHERE updated_at IS NULL OR trim(updated_at)=''"
                )

            for statement in _INDEX_STATEMENTS:
                connection.execute(statement)
            connection.execute(
                """
                INSERT INTO private_memory_schema_meta(singleton,schema_version,updated_at)
                VALUES(1,?,strftime('%Y-%m-%dT%H:%M:%fZ','now'))
                ON CONFLICT(singleton) DO UPDATE SET
                    schema_version=excluded.schema_version,
                    updated_at=excluded.updated_at
                WHERE private_memory_schema_meta.schema_version < excluded.schema_version
                """,
                (PRIVATE_MEMORY_SCHEMA_VERSION,),
            )
            connection.commit()
            after_tables = _existing_objects(connection, "table")
            tables_created = len((after_tables - before_tables) & {
                "private_chat_messages",
                "private_conversation_summaries",
                "private_memory_facts",
                "relationship_states",
                "memory_jobs",
                "memory_pending_operations",
                "memory_governance_audit",
                "private_memory_schema_meta",
            })
        path.chmod(0o600)
        quick_check(path)
        return MigrationReport(PRIVATE_MEMORY_SCHEMA_VERSION, tables_created, columns_added)
    except Exception:
        if created_file:
            try:
                path.unlink()
            except OSError:
                pass
        raise


def online_backup(source: Path, destination: Path) -> Path:
    source = require_regular_database(source)
    destination = Path(destination)
    if source.resolve() == destination.resolve():
        raise ValueError("backup source and destination must be different files")
    if destination.exists() and os.path.samefile(source, destination):
        raise ValueError("backup source and destination must be different files")
    if destination.exists():
        raise FileExistsError(f"backup destination already exists: {destination}")
    validate_backup_directory(destination.parent)
    quick_check(source)
    if not destination.parent.exists():
        destination.parent.mkdir(parents=True, mode=0o700)
        destination.parent.chmod(0o700)
    temporary = destination.with_suffix(destination.suffix + f".{os.getpid()}.part")
    try:
        temporary.unlink(missing_ok=True)
        descriptor = os.open(temporary, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        os.close(descriptor)
        temporary.chmod(0o600)
        with closing(sqlite3.connect(f"file:{source}?mode=ro", uri=True)) as source_connection, closing(
            sqlite3.connect(temporary)
        ) as target_connection:
            source_connection.backup(target_connection)
        quick_check(temporary)
        temporary.chmod(0o600)
        temporary.replace(destination)
        quick_check(destination)
        return destination
    except Exception:
        temporary.unlink(missing_ok=True)
        raise
