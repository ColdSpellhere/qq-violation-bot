import sqlite3
import unittest

from plugins.violation_record.policy_schema import (
    REQUIRED_V102_INDEXES,
    REQUIRED_V102_TABLES,
    V102_SCHEMA_VERSION,
    ensure_v102_schema,
    require_v102_ready,
)


NOW = "2026-08-02 12:00:00"


class PolicySchemaTests(unittest.TestCase):
    def setUp(self):
        self.conn = sqlite3.connect(":memory:")
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys = ON")
        self.conn.executescript(
            """
            CREATE TABLE members (
                id INTEGER PRIMARY KEY,
                qq_number TEXT UNIQUE NOT NULL
            );
            CREATE TABLE violation_records (
                id INTEGER PRIMARY KEY,
                member_id INTEGER NOT NULL,
                group_area TEXT NOT NULL,
                is_withdrawn INTEGER NOT NULL DEFAULT 0,
                is_test INTEGER NOT NULL DEFAULT 0,
                is_countable INTEGER NOT NULL DEFAULT 1,
                violation_time TEXT NOT NULL DEFAULT ''
            );
            CREATE TABLE consultation_records (
                id INTEGER PRIMARY KEY,
                member_id INTEGER NOT NULL,
                group_area TEXT NOT NULL,
                consultation_time TEXT NOT NULL DEFAULT ''
            );
            INSERT INTO members(id, qq_number) VALUES(1, '10001');
            CREATE TABLE member_policy_state (
                id INTEGER PRIMARY KEY,
                policy_tag TEXT NOT NULL
            );
            INSERT INTO member_policy_state(id, policy_tag) VALUES(1, 'slowdown');
            """
        )

    def tearDown(self):
        self.conn.close()

    def test_schema_is_namespaced_idempotent_and_preserves_legacy_rows(self):
        ensure_v102_schema(self.conn)
        ensure_v102_schema(self.conn)

        tables = {
            row["name"]
            for row in self.conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }
        indexes = {
            row["name"]
            for row in self.conn.execute(
                "SELECT name FROM sqlite_master WHERE type='index'"
            )
        }

        self.assertTrue(REQUIRED_V102_TABLES <= tables)
        self.assertTrue(REQUIRED_V102_INDEXES <= indexes)
        self.assertTrue(all(name.startswith("v102_") for name in REQUIRED_V102_TABLES))
        self.assertEqual(
            self.conn.execute(
                "SELECT policy_tag FROM member_policy_state WHERE id=1"
            ).fetchone()[0],
            "slowdown",
        )

    def test_schema_upgrade_adds_status_causality_column(self):
        self.conn.executescript(
            """
            CREATE TABLE operation_logs (id INTEGER PRIMARY KEY);
            CREATE TABLE v102_status_bridge_jobs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                operation_log_id INTEGER NOT NULL UNIQUE,
                member_id INTEGER NOT NULL,
                group_area TEXT NOT NULL,
                target_status TEXT NOT NULL,
                effective_at TEXT NOT NULL,
                idempotency_key TEXT NOT NULL UNIQUE,
                job_status TEXT NOT NULL DEFAULT 'pending',
                attempt_count INTEGER NOT NULL DEFAULT 0,
                applied_event_id INTEGER,
                last_error TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            """
        )

        ensure_v102_schema(self.conn)
        ensure_v102_schema(self.conn)

        columns = {
            row["name"]
            for row in self.conn.execute(
                "PRAGMA table_info(v102_status_bridge_jobs)"
            )
        }
        self.assertIn("caused_by_record_id", columns)

    def test_operation_count_constraint_rejects_six(self):
        ensure_v102_schema(self.conn)
        with self.assertRaises(sqlite3.IntegrityError):
            self.conn.execute(
                """
                INSERT INTO v102_policy_state(
                    member_id, group_area, v102_operation_count,
                    created_at, updated_at
                ) VALUES(1, '蜂巢', 6, ?, ?)
                """,
                (NOW, NOW),
            )

    def test_event_idempotency_key_is_unique(self):
        ensure_v102_schema(self.conn)
        values = (
            1,
            "蜂巢",
            "violation_recorded",
            NOW,
            0,
            1,
            NOW,
            "{}",
            "v1.0.2beta",
            "record:1",
            NOW,
        )
        sql = """
            INSERT INTO v102_policy_events(
                member_id, group_area, event_type, effective_time,
                event_priority, source_sequence, ingest_time, payload_json,
                rule_version, idempotency_key, created_at
            ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """
        self.conn.execute(sql, values)
        with self.assertRaises(sqlite3.IntegrityError):
            self.conn.execute(sql, values)

    def test_runtime_readiness_requires_applied_current_checkpoint(self):
        ensure_v102_schema(self.conn)

        with self.assertRaisesRegex(RuntimeError, "checkpoint"):
            require_v102_ready(self.conn)

        self.conn.execute(
            """
            INSERT INTO v102_migration_checkpoints(
                batch_id, schema_version, cutover_at,
                cutover_record_watermark, source_sha256, backup_sha256,
                status, created_at, updated_at
            ) VALUES('ready', ?, ?, 0, ?, ?, 'applied', ?, ?)
            """,
            (V102_SCHEMA_VERSION, NOW, "a" * 64, "b" * 64, NOW, NOW),
        )
        require_v102_ready(self.conn)

        self.conn.execute(
            """
            UPDATE v102_migration_checkpoints
            SET status='rolled_back' WHERE batch_id='ready'
            """
        )
        with self.assertRaisesRegex(RuntimeError, "checkpoint"):
            require_v102_ready(self.conn)

    def test_runtime_readiness_rejects_missing_index_and_schema_drift(self):
        ensure_v102_schema(self.conn)
        self.conn.execute(
            """
            INSERT INTO v102_migration_checkpoints(
                batch_id, schema_version, cutover_at,
                cutover_record_watermark, source_sha256, backup_sha256,
                status, created_at, updated_at
            ) VALUES('wrong', 'v1.0.2beta-old', ?, 0, ?, ?, 'applied', ?, ?)
            """,
            (NOW, "a" * 64, "b" * 64, NOW, NOW),
        )
        with self.assertRaisesRegex(RuntimeError, "schema version"):
            require_v102_ready(self.conn)

        self.conn.execute(
            """
            UPDATE v102_migration_checkpoints
            SET schema_version=? WHERE batch_id='wrong'
            """,
            (V102_SCHEMA_VERSION,),
        )
        self.conn.execute("DROP INDEX idx_v102_events_order")
        with self.assertRaisesRegex(RuntimeError, "indexes"):
            require_v102_ready(self.conn)


if __name__ == "__main__":
    unittest.main()
