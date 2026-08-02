from __future__ import annotations

import sqlite3


V102_SCHEMA_VERSION = "v1.0.2beta-2"

REQUIRED_V102_TABLES = frozenset(
    {
        "v102_policy_events",
        "v102_policy_cycles",
        "v102_policy_state",
        "v102_pending_actions",
        "v102_notification_outbox",
        "v102_notification_attempts",
        "v102_status_bridge_jobs",
        "v102_migration_checkpoints",
        "v102_baseline_audit",
    }
)

REQUIRED_V102_INDEXES = frozenset(
    {
        "idx_v102_events_order",
        "idx_v102_events_source_record",
        "idx_v102_cycles_due",
        "idx_v102_cycles_member_start",
        "idx_v102_cycles_one_active",
        "idx_v102_state_tag_area",
        "idx_v102_state_pending_action",
        "idx_v102_pending_due",
        "idx_v102_outbox_scheduled",
        "idx_v102_status_jobs_pending",
        "idx_v102_baseline_batch",
        "idx_violation_policy_replay",
        "idx_consultation_policy_replay",
    }
)

REQUIRED_V102_COLUMNS = {
    "v102_policy_state": frozenset(
        {
            "baseline_total_count",
            "baseline_current_count",
            "baseline_raw_total",
            "baseline_record_watermark",
            "baseline_locked",
            "baseline_last_effective_violation_time",
            "baseline_last_deduct_time",
            "baseline_last_final_warning_time",
        }
    ),
    "v102_baseline_audit": frozenset(
        {
            "old_locked",
            "old_last_effective_violation_time",
            "old_last_deduct_time",
            "old_last_final_warning_time",
        }
    ),
    "v102_status_bridge_jobs": frozenset({"caused_by_record_id"}),
}


SCHEMA_STATEMENTS = (
    """
    CREATE TABLE IF NOT EXISTS v102_policy_events (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        member_id INTEGER NOT NULL,
        group_area TEXT NOT NULL,
        event_type TEXT NOT NULL,
        effective_time TEXT NOT NULL,
        event_priority INTEGER NOT NULL,
        source_sequence INTEGER NOT NULL DEFAULT 0,
        ingest_time TEXT NOT NULL,
        source_record_id INTEGER,
        caused_by_event_id INTEGER,
        reversed_by_event_id INTEGER,
        superseded_by_replay_id INTEGER,
        replay_generation INTEGER NOT NULL DEFAULT 0,
        payload_json TEXT NOT NULL DEFAULT '{}',
        rule_version TEXT NOT NULL,
        idempotency_key TEXT NOT NULL UNIQUE,
        is_effective INTEGER NOT NULL DEFAULT 1 CHECK(is_effective IN (0, 1)),
        created_at TEXT NOT NULL,
        FOREIGN KEY(member_id) REFERENCES members(id),
        FOREIGN KEY(source_record_id) REFERENCES violation_records(id),
        FOREIGN KEY(caused_by_event_id) REFERENCES v102_policy_events(id),
        FOREIGN KEY(reversed_by_event_id) REFERENCES v102_policy_events(id),
        FOREIGN KEY(superseded_by_replay_id) REFERENCES v102_policy_events(id)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS v102_policy_cycles (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        member_id INTEGER NOT NULL,
        group_area TEXT NOT NULL,
        cycle_type TEXT NOT NULL CHECK(
            cycle_type IN ('normal', 'slow', 'stop', 'final_warning')
        ),
        start_at TEXT NOT NULL,
        due_at TEXT NOT NULL,
        status TEXT NOT NULL DEFAULT 'active' CHECK(
            status IN ('active', 'pending_decision', 'closed', 'cancelled')
        ),
        slow_level INTEGER NOT NULL DEFAULT 0 CHECK(slow_level >= 0),
        light_count INTEGER NOT NULL DEFAULT 0 CHECK(light_count >= 0),
        normal_light_count INTEGER NOT NULL DEFAULT 0 CHECK(normal_light_count >= 0),
        slow_light_count INTEGER NOT NULL DEFAULT 0 CHECK(slow_light_count >= 0),
        slow_extended INTEGER NOT NULL DEFAULT 0 CHECK(slow_extended IN (0, 1)),
        suggestion_rejected INTEGER NOT NULL DEFAULT 0 CHECK(
            suggestion_rejected IN (0, 1)
        ),
        severe_count INTEGER NOT NULL DEFAULT 0 CHECK(severe_count >= 0),
        fixed_sequence INTEGER NOT NULL DEFAULT 0 CHECK(fixed_sequence >= 0),
        replay_generation INTEGER NOT NULL DEFAULT 0 CHECK(replay_generation >= 0),
        closed_reason TEXT,
        evaluation_owner_cycle_id INTEGER,
        decision_event_id INTEGER,
        settlement_event_id INTEGER,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        FOREIGN KEY(member_id) REFERENCES members(id),
        FOREIGN KEY(evaluation_owner_cycle_id) REFERENCES v102_policy_cycles(id),
        FOREIGN KEY(decision_event_id) REFERENCES v102_policy_events(id),
        FOREIGN KEY(settlement_event_id) REFERENCES v102_policy_events(id)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS v102_policy_state (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        member_id INTEGER NOT NULL,
        group_area TEXT NOT NULL,
        policy_tag TEXT NOT NULL DEFAULT 'none' CHECK(
            policy_tag IN ('none', 'slow', 'stop')
        ),
        slow_level INTEGER NOT NULL DEFAULT 0 CHECK(slow_level >= 0),
        v102_operation_count INTEGER NOT NULL DEFAULT 0 CHECK(
            v102_operation_count BETWEEN 0 AND 5
        ),
        baseline_adjustment INTEGER NOT NULL DEFAULT 0,
        baseline_total_count INTEGER NOT NULL DEFAULT 0,
        baseline_deduct_count INTEGER NOT NULL DEFAULT 0 CHECK(
            baseline_deduct_count >= 0
        ),
        baseline_current_count INTEGER NOT NULL DEFAULT 0,
        baseline_raw_total INTEGER NOT NULL DEFAULT 0,
        baseline_record_watermark INTEGER NOT NULL DEFAULT 0,
        baseline_locked INTEGER NOT NULL DEFAULT 0 CHECK(
            baseline_locked IN (0, 1)
        ),
        baseline_status TEXT NOT NULL DEFAULT '正常',
        baseline_last_effective_violation_time TEXT,
        baseline_last_deduct_time TEXT,
        baseline_last_final_warning_time TEXT,
        baseline_initialized_at TEXT,
        active_cycle_id INTEGER,
        no_cycle_reason TEXT CHECK(
            no_cycle_reason IS NULL OR no_cycle_reason IN (
                'zero_count', 'operation_limit', 'terminal_status'
            )
        ),
        pending_action_type TEXT,
        last_processed_event_id INTEGER,
        state_version INTEGER NOT NULL DEFAULT 0 CHECK(state_version >= 0),
        last_reason TEXT,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        UNIQUE(member_id, group_area),
        FOREIGN KEY(member_id) REFERENCES members(id),
        FOREIGN KEY(active_cycle_id) REFERENCES v102_policy_cycles(id),
        FOREIGN KEY(last_processed_event_id) REFERENCES v102_policy_events(id)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS v102_pending_actions (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        member_id INTEGER NOT NULL,
        group_area TEXT NOT NULL,
        action_type TEXT NOT NULL,
        status TEXT NOT NULL DEFAULT 'pending' CHECK(
            status IN ('pending', 'resolved', 'cancelled')
        ),
        due_at TEXT,
        next_reminder_at TEXT,
        reason TEXT NOT NULL,
        caused_by_event_id INTEGER,
        decision_event_id INTEGER,
        idempotency_key TEXT NOT NULL UNIQUE,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        FOREIGN KEY(member_id) REFERENCES members(id),
        FOREIGN KEY(caused_by_event_id) REFERENCES v102_policy_events(id),
        FOREIGN KEY(decision_event_id) REFERENCES v102_policy_events(id)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS v102_notification_outbox (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        event_id INTEGER NOT NULL,
        pending_action_id INTEGER,
        member_id INTEGER,
        group_area TEXT,
        message_type TEXT NOT NULL,
        reminder_slot TEXT NOT NULL DEFAULT '',
        message_text TEXT NOT NULL,
        scheduled_at TEXT NOT NULL,
        status TEXT NOT NULL DEFAULT 'pending' CHECK(
            status IN ('pending', 'sending', 'sent', 'failed', 'cancelled')
        ),
        attempt_count INTEGER NOT NULL DEFAULT 0 CHECK(attempt_count >= 0),
        last_error TEXT,
        sent_at TEXT,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        UNIQUE(event_id, message_type, reminder_slot),
        FOREIGN KEY(event_id) REFERENCES v102_policy_events(id),
        FOREIGN KEY(pending_action_id) REFERENCES v102_pending_actions(id),
        FOREIGN KEY(member_id) REFERENCES members(id)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS v102_notification_attempts (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        outbox_id INTEGER NOT NULL,
        attempt_number INTEGER NOT NULL CHECK(attempt_number > 0),
        status TEXT NOT NULL CHECK(
            status IN (
                'sending', 'sent', 'failed', 'cancelled', 'lease_expired'
            )
        ),
        started_at TEXT NOT NULL,
        finished_at TEXT,
        detail TEXT,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        UNIQUE(outbox_id, attempt_number),
        FOREIGN KEY(outbox_id) REFERENCES v102_notification_outbox(id)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS v102_status_bridge_jobs (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        operation_log_id INTEGER NOT NULL UNIQUE,
        member_id INTEGER NOT NULL,
        group_area TEXT NOT NULL,
        target_status TEXT NOT NULL,
        caused_by_record_id INTEGER,
        effective_at TEXT NOT NULL,
        idempotency_key TEXT NOT NULL UNIQUE,
        job_status TEXT NOT NULL DEFAULT 'pending' CHECK(
            job_status IN ('pending', 'processing', 'applied', 'failed')
        ),
        attempt_count INTEGER NOT NULL DEFAULT 0 CHECK(attempt_count >= 0),
        applied_event_id INTEGER,
        last_error TEXT,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        FOREIGN KEY(operation_log_id) REFERENCES operation_logs(id),
        FOREIGN KEY(member_id) REFERENCES members(id),
        FOREIGN KEY(caused_by_record_id) REFERENCES violation_records(id),
        FOREIGN KEY(applied_event_id) REFERENCES v102_policy_events(id)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS v102_migration_checkpoints (
        batch_id TEXT PRIMARY KEY,
        schema_version TEXT NOT NULL,
        cutover_at TEXT NOT NULL,
        cutover_record_watermark INTEGER NOT NULL CHECK(
            cutover_record_watermark >= 0
        ),
        source_sha256 TEXT NOT NULL,
        backup_sha256 TEXT NOT NULL,
        status TEXT NOT NULL CHECK(
            status IN ('applied', 'rolled_back')
        ),
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS v102_baseline_audit (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        batch_id TEXT NOT NULL,
        member_id INTEGER NOT NULL,
        group_area TEXT NOT NULL,
        old_total_count INTEGER NOT NULL,
        old_deduct_count INTEGER NOT NULL,
        old_current_count INTEGER NOT NULL,
        old_baseline_adjustment INTEGER NOT NULL DEFAULT 0,
        old_locked INTEGER NOT NULL DEFAULT 0 CHECK(old_locked IN (0, 1)),
        old_last_effective_violation_time TEXT,
        old_last_deduct_time TEXT,
        old_last_final_warning_time TEXT,
        approved_current_count INTEGER NOT NULL CHECK(
            approved_current_count >= 0
        ),
        new_total_count INTEGER NOT NULL,
        new_baseline_adjustment INTEGER NOT NULL,
        was_created INTEGER NOT NULL DEFAULT 0 CHECK(was_created IN (0, 1)),
        source_sheet TEXT NOT NULL,
        source_row INTEGER NOT NULL CHECK(source_row > 0),
        created_at TEXT NOT NULL,
        UNIQUE(batch_id, member_id, group_area),
        FOREIGN KEY(batch_id) REFERENCES v102_migration_checkpoints(batch_id),
        FOREIGN KEY(member_id) REFERENCES members(id)
    )
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_v102_events_order
    ON v102_policy_events(
        member_id, group_area, effective_time,
        event_priority, source_sequence, id
    )
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_v102_events_source_record
    ON v102_policy_events(source_record_id)
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_v102_cycles_due
    ON v102_policy_cycles(status, due_at)
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_v102_cycles_member_start
    ON v102_policy_cycles(member_id, group_area, start_at)
    """,
    """
    CREATE UNIQUE INDEX IF NOT EXISTS idx_v102_cycles_one_active
    ON v102_policy_cycles(member_id, group_area)
    WHERE status IN ('active', 'pending_decision')
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_v102_state_tag_area
    ON v102_policy_state(policy_tag, group_area)
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_v102_state_pending_action
    ON v102_policy_state(pending_action_type, group_area)
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_v102_pending_due
    ON v102_pending_actions(status, next_reminder_at, due_at)
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_v102_outbox_scheduled
    ON v102_notification_outbox(status, scheduled_at, id)
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_v102_status_jobs_pending
    ON v102_status_bridge_jobs(job_status, updated_at, id)
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_v102_baseline_batch
    ON v102_baseline_audit(batch_id, group_area, member_id)
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_violation_policy_replay
    ON violation_records(
        member_id, group_area, is_withdrawn, is_test,
        is_countable, violation_time, id
    )
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_consultation_policy_replay
    ON consultation_records(member_id, group_area, consultation_time, id)
    """,
)


def configure_v102_connection(conn: sqlite3.Connection) -> None:
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA busy_timeout = 5000")


def ensure_v102_schema(conn: sqlite3.Connection) -> None:
    configure_v102_connection(conn)
    for statement in SCHEMA_STATEMENTS:
        conn.execute(statement)
    cycle_columns = {
        row["name"] if isinstance(row, sqlite3.Row) else row[1]
        for row in conn.execute("PRAGMA table_info(v102_policy_cycles)")
    }
    additions = {
        "normal_light_count": "INTEGER NOT NULL DEFAULT 0 CHECK(normal_light_count >= 0)",
        "slow_light_count": "INTEGER NOT NULL DEFAULT 0 CHECK(slow_light_count >= 0)",
        "slow_extended": "INTEGER NOT NULL DEFAULT 0 CHECK(slow_extended IN (0, 1))",
        "suggestion_rejected": "INTEGER NOT NULL DEFAULT 0 CHECK(suggestion_rejected IN (0, 1))",
        "fixed_sequence": "INTEGER NOT NULL DEFAULT 0 CHECK(fixed_sequence >= 0)",
        "replay_generation": "INTEGER NOT NULL DEFAULT 0 CHECK(replay_generation >= 0)",
        "closed_reason": "TEXT",
    }
    for name, definition in additions.items():
        if name not in cycle_columns:
            conn.execute(
                f"ALTER TABLE v102_policy_cycles ADD COLUMN {name} {definition}"
            )
    state_columns = {
        row["name"] if isinstance(row, sqlite3.Row) else row[1]
        for row in conn.execute("PRAGMA table_info(v102_policy_state)")
    }
    state_additions = {
        "baseline_total_count": "INTEGER NOT NULL DEFAULT 0",
        "baseline_deduct_count": "INTEGER NOT NULL DEFAULT 0 CHECK(baseline_deduct_count >= 0)",
        "baseline_current_count": "INTEGER NOT NULL DEFAULT 0",
        "baseline_raw_total": "INTEGER NOT NULL DEFAULT 0",
        "baseline_record_watermark": "INTEGER NOT NULL DEFAULT 0",
        "baseline_locked": "INTEGER NOT NULL DEFAULT 0 CHECK(baseline_locked IN (0, 1))",
        "baseline_status": "TEXT NOT NULL DEFAULT '正常'",
        "baseline_last_effective_violation_time": "TEXT",
        "baseline_last_deduct_time": "TEXT",
        "baseline_last_final_warning_time": "TEXT",
        "baseline_initialized_at": "TEXT",
    }
    for name, definition in state_additions.items():
        if name not in state_columns:
            conn.execute(
                f"ALTER TABLE v102_policy_state ADD COLUMN {name} {definition}"
            )
    audit_columns = {
        row["name"] if isinstance(row, sqlite3.Row) else row[1]
        for row in conn.execute("PRAGMA table_info(v102_baseline_audit)")
    }
    audit_additions = {
        "old_locked": "INTEGER NOT NULL DEFAULT 0 CHECK(old_locked IN (0, 1))",
        "old_last_effective_violation_time": "TEXT",
        "old_last_deduct_time": "TEXT",
        "old_last_final_warning_time": "TEXT",
    }
    for name, definition in audit_additions.items():
        if name not in audit_columns:
            conn.execute(
                f"ALTER TABLE v102_baseline_audit ADD COLUMN {name} {definition}"
            )
    job_columns = {
        row["name"] if isinstance(row, sqlite3.Row) else row[1]
        for row in conn.execute("PRAGMA table_info(v102_status_bridge_jobs)")
    }
    if "caused_by_record_id" not in job_columns:
        conn.execute(
            """
            ALTER TABLE v102_status_bridge_jobs
            ADD COLUMN caused_by_record_id INTEGER
                REFERENCES violation_records(id)
            """
        )


def v102_schema_ready(conn: sqlite3.Connection) -> bool:
    tables = {
        row[0]
        for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()
    }
    return REQUIRED_V102_TABLES <= tables


def require_v102_schema(conn: sqlite3.Connection) -> None:
    if not v102_schema_ready(conn):
        raise RuntimeError(
            "v1.0.2beta policy schema is not applied; run scripts/migrate_v102.py"
        )


def invalid_v102_baseline_snapshots(
    conn: sqlite3.Connection, *, batch_id: str | None = None
) -> int:
    where = "WHERE a.batch_id=?" if batch_id is not None else ""
    parameters = (batch_id,) if batch_id is not None else ()
    return int(
        conn.execute(
            f"""
            SELECT COUNT(*)
            FROM v102_baseline_audit a
            JOIN v102_migration_checkpoints c ON c.batch_id=a.batch_id
            LEFT JOIN v102_policy_state p
              ON p.member_id=a.member_id AND p.group_area=a.group_area
            {where}
              {"AND" if where else "WHERE"} (
                   p.member_id IS NULL
                OR p.baseline_total_count!=a.old_total_count
                OR p.baseline_deduct_count!=a.old_deduct_count
                OR p.baseline_current_count!=a.old_current_count
                OR p.baseline_raw_total!=(
                    a.new_total_count-a.new_baseline_adjustment
                )
                OR p.baseline_record_watermark!=c.cutover_record_watermark
                OR p.baseline_locked!=a.old_locked
                OR p.baseline_last_effective_violation_time
                   IS NOT a.old_last_effective_violation_time
                OR p.baseline_last_deduct_time IS NOT a.old_last_deduct_time
                OR p.baseline_last_final_warning_time
                   IS NOT a.old_last_final_warning_time
              )
            """,
            parameters,
        ).fetchone()[0]
    )


def v102_readiness_errors(conn: sqlite3.Connection) -> tuple[str, ...]:
    tables = {
        row[0]
        for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()
    }
    indexes = {
        row[0]
        for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='index'"
        ).fetchall()
    }
    errors: list[str] = []
    snapshot_columns_ready = True
    missing_tables = sorted(REQUIRED_V102_TABLES - tables)
    missing_indexes = sorted(REQUIRED_V102_INDEXES - indexes)
    if missing_tables:
        errors.append(f"missing tables: {', '.join(missing_tables)}")
    if missing_indexes:
        errors.append(f"missing indexes: {', '.join(missing_indexes)}")
    for table, required_columns in REQUIRED_V102_COLUMNS.items():
        if table not in tables:
            continue
        columns = {
            row["name"] if isinstance(row, sqlite3.Row) else row[1]
            for row in conn.execute(f"PRAGMA table_info({table})")
        }
        missing_columns = sorted(required_columns - columns)
        if missing_columns:
            snapshot_columns_ready = False
            errors.append(
                f"missing columns in {table}: {', '.join(missing_columns)}"
            )
    if "v102_migration_checkpoints" not in tables:
        errors.append("applied migration checkpoint is missing")
        return tuple(errors)
    checkpoint = conn.execute(
        """
        SELECT * FROM v102_migration_checkpoints
        WHERE status='applied'
        ORDER BY cutover_at DESC, batch_id DESC LIMIT 1
        """
    ).fetchone()
    if checkpoint is None:
        errors.append("applied migration checkpoint is missing")
    else:
        schema_version = (
            checkpoint["schema_version"]
            if isinstance(checkpoint, sqlite3.Row)
            else checkpoint[1]
        )
        if schema_version != V102_SCHEMA_VERSION:
            errors.append(
                "schema version mismatch: "
                f"expected={V102_SCHEMA_VERSION} actual={schema_version}"
            )
    if REQUIRED_V102_TABLES <= tables and snapshot_columns_ready:
        invalid_snapshots = invalid_v102_baseline_snapshots(conn)
        if invalid_snapshots:
            errors.append(f"invalid baseline snapshots: {invalid_snapshots}")
    return tuple(errors)


def require_v102_ready(conn: sqlite3.Connection) -> None:
    errors = v102_readiness_errors(conn)
    if errors:
        raise RuntimeError(
            "v1.0.2beta runtime readiness failed: " + "; ".join(errors)
        )
