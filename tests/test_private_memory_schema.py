from __future__ import annotations

import os
import sqlite3
import stat
import subprocess
import sys
import tempfile
import unittest
from contextlib import closing
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from plugins.private_memory.schema import (
    PRIVATE_MEMORY_SCHEMA_VERSION,
    migrate,
    online_backup,
    quick_check,
)
from plugins.private_memory import schema as schema_module
from scripts import migrate_private_memory


PROJECT_ROOT = Path(__file__).resolve().parents[1]

EXPECTED_TABLES = {
    "private_chat_messages",
    "private_conversation_summaries",
    "private_memory_facts",
    "relationship_states",
    "memory_jobs",
    "memory_pending_operations",
    "memory_governance_audit",
    "llm_usage_events",
    "private_memory_schema_meta",
}

EXPECTED_INDEXES = {
    "idx_private_chat_messages_user_id",
    "idx_private_chat_messages_expiry",
    "idx_private_memory_facts_active",
    "idx_relationship_states_scope",
    "idx_memory_jobs_runnable",
    "idx_memory_pending_operations_expiry",
}

EXPECTED_UNIQUE_INDEXES = {
    "idx_relationship_states_group_unique",
    "idx_relationship_states_private_unique",
    "idx_memory_jobs_active_unique",
}

EXPECTED_MEMBER_FACT_COLUMNS = {
    "trust_level",
    "status",
    "supersedes_id",
    "updated_at",
    "version",
    "deleted_at",
}

EXPECTED_PRIVATE_MESSAGE_COLUMNS = {
    "image_descriptions_json",
}


def object_sql(database: Path, kind: str, name: str) -> str:
    with closing(sqlite3.connect(database)) as connection:
        row = connection.execute(
            "SELECT sql FROM sqlite_master WHERE type=? AND name=?", (kind, name)
        ).fetchone()
    return str(row[0]) if row and row[0] else ""


class PrivateMemorySchemaTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.alias_root = Path(temporary.name)
        self.root = self.alias_root.resolve()
        self.database = self.root / "chat_archive.db"

    def test_empty_database_gets_exact_schema_constraints_and_indexes(self) -> None:
        report = migrate(self.database)

        self.assertEqual(3, PRIVATE_MEMORY_SCHEMA_VERSION)
        self.assertEqual(PRIVATE_MEMORY_SCHEMA_VERSION, report.schema_version)
        with closing(sqlite3.connect(self.database)) as connection:
            tables = {
                row[0]
                for row in connection.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                )
            }
            indexes = {
                row[0]
                for row in connection.execute(
                    "SELECT name FROM sqlite_master WHERE type='index'"
                )
            }
        self.assertTrue(EXPECTED_TABLES <= tables)
        self.assertTrue(EXPECTED_INDEXES <= indexes)
        with closing(sqlite3.connect(self.database)) as connection:
            unique_indexes = {
                row[1]
                for table in ("relationship_states", "memory_jobs")
                for row in connection.execute(f"PRAGMA index_list({table})")
                if int(row[2]) == 1
            }
        self.assertTrue(EXPECTED_UNIQUE_INDEXES <= unique_indexes)
        with closing(sqlite3.connect(self.database)) as connection:
            private_message_columns = {
                str(row[1])
                for row in connection.execute(
                    "PRAGMA table_info(private_chat_messages)"
                )
            }
        self.assertTrue(
            EXPECTED_PRIVATE_MESSAGE_COLUMNS <= private_message_columns
        )
        self.assertIn("CHECK(direction IN ('user','assistant'))", object_sql(
            self.database, "table", "private_chat_messages"
        ))
        self.assertIn("CHECK(trust_level IN ('ai_extracted','admin_confirmed'))", object_sql(
            self.database, "table", "private_memory_facts"
        ))
        self.assertIn("CHECK(status IN ('active','superseded','deleted'))", object_sql(
            self.database, "table", "private_memory_facts"
        ))
        self.assertIn("CHECK(conversation_kind IN ('group','private'))", object_sql(
            self.database, "table", "relationship_states"
        ))
        jobs_sql = object_sql(self.database, "table", "memory_jobs")
        self.assertIn("CHECK(job_type IN ('private_summary','private_facts','relationship'))", jobs_sql)
        self.assertIn("CHECK(status IN ('pending','running','succeeded','failed','cancelled'))", jobs_sql)
        self.assertIn("CHECK(result IN ('success','failed','cancelled'))", object_sql(
            self.database, "table", "memory_governance_audit"
        ))
        self.assertEqual("ok", quick_check(self.database))

    def test_migration_default_backup_directory_matches_daily_retention(self) -> None:
        args = migrate_private_memory.parse_args([])
        self.assertEqual(
            PROJECT_ROOT / "backups" / "private_memory",
            args.backup_dir,
        )

    def test_confirmation_token_hash_requires_lowercase_sha256_hex(self) -> None:
        migrate(self.database)
        base_values = (
            "operator",
            "add_fact",
            "private",
            None,
            "200",
            None,
            "{}",
            "preview",
            "2026-08-23T00:00:00Z",
            None,
            "2026-08-22T00:00:00Z",
        )
        with closing(sqlite3.connect(self.database)) as connection:
            connection.execute(
                """
                INSERT INTO memory_pending_operations(
                    confirmation_token_hash,operator_user_id,operation_type,target_kind,
                    target_group_id,target_user_id,target_memory_id,payload_json,preview_text,
                    expires_at,consumed_at,created_at
                ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)
                """,
                ("a" * 64, *base_values),
            )
            for invalid in ("a" * 63, "A" * 64, "g" * 64):
                with self.subTest(invalid=invalid[:4]):
                    with self.assertRaises(sqlite3.IntegrityError):
                        connection.execute(
                            """
                            INSERT INTO memory_pending_operations(
                                confirmation_token_hash,operator_user_id,operation_type,target_kind,
                                target_group_id,target_user_id,target_memory_id,payload_json,preview_text,
                                expires_at,consumed_at,created_at
                            ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)
                            """,
                            (invalid, *base_values),
                        )

    def test_scope_and_active_job_unique_indexes_reject_duplicates(self) -> None:
        migrate(self.database)
        with closing(sqlite3.connect(self.database)) as connection:
            relationship_values = (
                "7",
                "radish-cat",
                "",
                "[]",
                "",
                "",
                "m1",
                1,
                1,
                "2026-08-22T00:00:00Z",
                "2026-08-22T00:00:00Z",
            )
            connection.execute(
                """
                INSERT INTO relationship_states(
                    conversation_kind,group_id,user_id,persona_id,state_text,open_topics_json,
                    preferred_address,communication_style,source_message_id,source_watermark,
                    version,created_at,updated_at
                ) VALUES('group',123,?,?,?,?,?,?,?,?,?,?,?)
                """,
                relationship_values,
            )
            with self.assertRaises(sqlite3.IntegrityError):
                connection.execute(
                    """
                    INSERT INTO relationship_states(
                        conversation_kind,group_id,user_id,persona_id,state_text,open_topics_json,
                        preferred_address,communication_style,source_message_id,source_watermark,
                        version,created_at,updated_at
                    ) VALUES('group',123,?,?,?,?,?,?,?,?,?,?,?)
                    """,
                    relationship_values,
                )
            connection.execute(
                """
                INSERT INTO relationship_states(
                    conversation_kind,group_id,user_id,persona_id,state_text,open_topics_json,
                    preferred_address,communication_style,source_message_id,source_watermark,
                    version,created_at,updated_at
                ) VALUES('private',NULL,?,?,?,?,?,?,?,?,?,?,?)
                """,
                relationship_values,
            )
            with self.assertRaises(sqlite3.IntegrityError):
                connection.execute(
                    """
                    INSERT INTO relationship_states(
                        conversation_kind,group_id,user_id,persona_id,state_text,open_topics_json,
                        preferred_address,communication_style,source_message_id,source_watermark,
                        version,created_at,updated_at
                    ) VALUES('private',NULL,?,?,?,?,?,?,?,?,?,?,?)
                    """,
                    relationship_values,
                )
            job_values = (
                "private_summary",
                "private",
                None,
                "7",
                "radish-cat",
                1,
                0,
                "pending",
                0,
                "2026-08-22T00:00:00Z",
                None,
                None,
                0,
                "",
                "",
                "2026-08-22T00:00:00Z",
                "2026-08-22T00:00:00Z",
            )
            placeholders = ",".join("?" for _ in job_values)
            connection.execute(
                f"INSERT INTO memory_jobs("  # noqa: S608 - fixed local schema identifiers
                "job_type,conversation_kind,group_id,user_id,persona_id,input_through_id,"
                "expected_version,status,attempts,next_run_at,lease_owner,lease_expires_at,"
                "claim_version,error_code,error_summary,created_at,updated_at"
                f") VALUES({placeholders})",
                job_values,
            )
            with self.assertRaises(sqlite3.IntegrityError):
                connection.execute(
                    f"INSERT INTO memory_jobs("  # noqa: S608 - fixed local schema identifiers
                    "job_type,conversation_kind,group_id,user_id,persona_id,input_through_id,"
                    "expected_version,status,attempts,next_run_at,lease_owner,lease_expires_at,"
                    "claim_version,error_code,error_summary,created_at,updated_at"
                    f") VALUES({placeholders})",
                    job_values,
                )

    def test_legacy_rows_survive_and_member_facts_are_backfilled_idempotently(self) -> None:
        with closing(sqlite3.connect(self.database)) as connection:
            connection.executescript(
                """
                CREATE TABLE chat_messages (
                    message_id TEXT PRIMARY KEY,
                    group_id INTEGER NOT NULL,
                    plaintext TEXT NOT NULL
                );
                INSERT INTO chat_messages VALUES ('legacy-message', 123, 'kept');
                CREATE TABLE member_memory_facts (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    group_id INTEGER NOT NULL,
                    user_id TEXT NOT NULL,
                    trait TEXT NOT NULL,
                    evidence_message_id TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    legacy_note TEXT DEFAULT 'legacy',
                    UNIQUE(group_id,user_id,trait,evidence_message_id)
                );
                INSERT INTO member_memory_facts(
                    group_id,user_id,trait,evidence_message_id,created_at,legacy_note
                ) VALUES
                    (123,'7','喜欢花','m1','2026-08-20 01:02:03','kept-1'),
                    (123,'8','喜欢树','m2','2026-08-20 02:03:04','kept-2');
                """
            )
            legacy_columns = connection.execute("PRAGMA table_info(member_memory_facts)").fetchall()
            legacy_rows = connection.execute(
                """
                SELECT id,group_id,user_id,trait,evidence_message_id,created_at,legacy_note
                FROM member_memory_facts ORDER BY id
                """
            ).fetchall()
            connection.commit()

        first = migrate(self.database)
        with closing(sqlite3.connect(self.database)) as connection:
            connection.execute(
                "UPDATE private_memory_schema_meta SET updated_at='sentinel' WHERE singleton=1"
            )
            connection.commit()
        second = migrate(self.database)

        self.assertEqual(PRIVATE_MEMORY_SCHEMA_VERSION, first.schema_version)
        self.assertEqual(first.schema_version, second.schema_version)
        with closing(sqlite3.connect(self.database)) as connection:
            self.assertEqual(
                "kept",
                connection.execute("SELECT plaintext FROM chat_messages").fetchone()[0],
            )
            columns = {
                row[1] for row in connection.execute("PRAGMA table_info(member_memory_facts)")
            }
            row = connection.execute(
                """
                SELECT trait,trust_level,status,supersedes_id,updated_at,version,deleted_at
                FROM member_memory_facts WHERE id=1
                """
            ).fetchone()
            preserved_columns = [
                item
                for item in connection.execute("PRAGMA table_info(member_memory_facts)").fetchall()
                if item[1] in {legacy[1] for legacy in legacy_columns}
            ]
            preserved_rows = connection.execute(
                """
                SELECT id,group_id,user_id,trait,evidence_message_id,created_at,legacy_note
                FROM member_memory_facts ORDER BY id
                """
            ).fetchall()
            meta_updated_at = connection.execute(
                "SELECT updated_at FROM private_memory_schema_meta WHERE singleton=1"
            ).fetchone()[0]
        self.assertTrue(EXPECTED_MEMBER_FACT_COLUMNS <= columns)
        self.assertEqual(
            ("喜欢花", "ai_extracted", "active", None, "2026-08-20 01:02:03", 1, None),
            row,
        )
        self.assertEqual("sentinel", meta_updated_at)
        self.assertEqual(legacy_columns, preserved_columns)
        self.assertEqual(legacy_rows, preserved_rows)

    def test_v2_private_messages_gain_image_descriptions_idempotently(self) -> None:
        with closing(sqlite3.connect(self.database)) as connection:
            connection.executescript(
                """
                CREATE TABLE private_chat_messages (
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
                );
                CREATE TABLE private_memory_schema_meta (
                    singleton INTEGER PRIMARY KEY CHECK(singleton=1),
                    schema_version INTEGER NOT NULL CHECK(schema_version > 0),
                    updated_at TEXT NOT NULL
                );
                INSERT INTO private_memory_schema_meta
                    (singleton,schema_version,updated_at)
                VALUES(1,2,'v2-sentinel');
                INSERT INTO private_chat_messages(
                    user_id,message_id,direction,text,content_hash,event_time,
                    created_at,expires_at,purged_at,source_kind,source_message_id
                ) VALUES(
                    '200','legacy-private','user','旧私聊正文','legacy-hash',1,
                    '2026-08-22T00:00:00Z','2026-09-22T00:00:00Z',NULL,'text',NULL
                );
                """
            )
            connection.commit()

        first = migrate(self.database)
        second = migrate(self.database)

        self.assertEqual(3, first.schema_version)
        self.assertEqual(1, first.columns_added)
        self.assertEqual(0, second.columns_added)
        with closing(sqlite3.connect(self.database)) as connection:
            columns = {
                str(row[1])
                for row in connection.execute(
                    "PRAGMA table_info(private_chat_messages)"
                )
            }
            row = connection.execute(
                """
                SELECT user_id,message_id,text,content_hash,image_descriptions_json
                FROM private_chat_messages WHERE message_id='legacy-private'
                """
            ).fetchone()
            version = connection.execute(
                "SELECT schema_version FROM private_memory_schema_meta WHERE singleton=1"
            ).fetchone()[0]
        self.assertIn("image_descriptions_json", columns)
        self.assertEqual(
            ("200", "legacy-private", "旧私聊正文", "legacy-hash", "[]"),
            row,
        )
        self.assertEqual(3, version)

    def test_migrate_rejects_failed_source_quick_check_before_schema_write(self) -> None:
        self.database.write_bytes(b"not a sqlite database")
        original = self.database.read_bytes()

        with self.assertRaises(sqlite3.DatabaseError):
            migrate(self.database)

        self.assertEqual(original, self.database.read_bytes())

    def test_apply_backs_up_and_verifies_before_first_schema_write(self) -> None:
        with closing(sqlite3.connect(self.database)) as connection:
            connection.execute("CREATE TABLE marker(value TEXT NOT NULL)")
            connection.execute("INSERT INTO marker VALUES ('original')")
            connection.commit()
        backup_dir = self.root / "backups"
        events: list[str] = []
        real_backup = migrate_private_memory.online_backup
        real_migrate = migrate_private_memory.migrate

        def observed_backup(source: Path, destination: Path) -> Path:
            events.append("backup")
            result = real_backup(source, destination)
            self.assertEqual("ok", quick_check(result))
            with closing(sqlite3.connect(result)) as connection:
                self.assertEqual("original", connection.execute("SELECT value FROM marker").fetchone()[0])
            return result

        def observed_migrate(path: Path):
            events.append("migrate")
            return real_migrate(path)

        with patch.object(migrate_private_memory, "online_backup", side_effect=observed_backup), patch.object(
            migrate_private_memory, "migrate", side_effect=observed_migrate
        ):
            report, backup = migrate_private_memory.apply_migration(self.database, backup_dir)

        self.assertEqual(["backup", "migrate"], events)
        self.assertEqual(PRIVATE_MEMORY_SCHEMA_VERSION, report.schema_version)
        self.assertTrue(backup.is_file())

    def test_apply_refuses_invalid_backup_before_migration(self) -> None:
        with closing(sqlite3.connect(self.database)) as connection:
            connection.execute("CREATE TABLE marker(value TEXT NOT NULL)")
            connection.commit()
        before = self.database.read_bytes()
        bad_backup = self.root / "bad.sqlite3"

        def invalid_backup(_source: Path, _destination: Path) -> Path:
            bad_backup.write_bytes(b"broken")
            return bad_backup

        with patch.object(migrate_private_memory, "online_backup", side_effect=invalid_backup), patch.object(
            migrate_private_memory, "migrate"
        ) as migrate_mock:
            with self.assertRaises(sqlite3.DatabaseError):
                migrate_private_memory.apply_migration(self.database, self.root / "backups")

        migrate_mock.assert_not_called()
        self.assertEqual(before, self.database.read_bytes())

    def test_apply_prunes_expired_managed_backups_before_new_backup(self) -> None:
        with closing(sqlite3.connect(self.database)) as connection:
            connection.execute("CREATE TABLE marker(value TEXT NOT NULL)")
            connection.commit()
        backup_dir = self.root / "backups"
        backup_dir.mkdir(mode=0o700)
        expired = backup_dir / (
            "chat_archive_before_private_memory_20200101T000000000000Z.sqlite3"
        )
        unrelated = backup_dir / "manual-archive.sqlite3"
        expired.write_bytes(b"expired managed backup")
        unrelated.write_bytes(b"operator managed")
        old_time = datetime(2020, 1, 1, tzinfo=timezone.utc).timestamp()
        os.utime(expired, (old_time, old_time))
        os.utime(unrelated, (old_time, old_time))

        _report, new_backup = migrate_private_memory.apply_migration(
            self.database, backup_dir
        )

        self.assertFalse(expired.exists())
        self.assertTrue(unrelated.exists())
        self.assertTrue(new_backup.exists())

    def test_online_backup_uses_owner_only_permissions_and_refuses_collisions(self) -> None:
        with closing(sqlite3.connect(self.database)) as connection:
            connection.execute("CREATE TABLE marker(value TEXT NOT NULL)")
            connection.commit()
        backup_dir = self.root / "new-backups"
        destination = backup_dir / "snapshot.sqlite3"

        result = online_backup(self.database, destination)

        self.assertEqual(0o700, stat.S_IMODE(backup_dir.stat().st_mode))
        self.assertEqual(0o600, stat.S_IMODE(result.stat().st_mode))
        destination.write_bytes(b"do-not-overwrite")
        with self.assertRaises(FileExistsError):
            online_backup(self.database, destination)
        self.assertEqual(b"do-not-overwrite", destination.read_bytes())
        with self.assertRaises(ValueError):
            online_backup(self.database, self.database)
        hardlink = self.root / "same-database-hardlink.db"
        os.link(self.database, hardlink)
        with self.assertRaises(ValueError):
            online_backup(self.database, hardlink)

    def test_backup_retention_deletes_only_expired_managed_regular_files(self) -> None:
        backup_dir = self.root / "backups"
        backup_dir.mkdir(mode=0o700)
        now = datetime(2026, 8, 23, tzinfo=timezone.utc)
        old = backup_dir / "chat_archive_before_private_memory_20260701T000000000000Z.sqlite3"
        recent = backup_dir / "chat_archive_before_private_memory_20260822T000000000000Z.sqlite3"
        lifecycle_old = backup_dir / "chat_archive-pre-private-memory-20260701T000000000000Z-42.sqlite3"
        unrelated = backup_dir / "unrelated.sqlite3"
        target = self.root / "outside.sqlite3"
        symlink = backup_dir / "chat_archive_before_private_memory_20260702T000000000000Z.sqlite3"
        for path in (old, recent, lifecycle_old, unrelated, target):
            path.write_bytes(b"fixture")
        symlink.symlink_to(target)
        old_time = (now - timedelta(days=31)).timestamp()
        recent_time = (now - timedelta(days=1)).timestamp()
        for path in (old, lifecycle_old, unrelated, target):
            os.utime(path, (old_time, old_time))
        os.utime(recent, (recent_time, recent_time))

        prune = getattr(
            schema_module,
            "prune_private_memory_backups",
            lambda directory, **kwargs: 0,
        )
        deleted = prune(backup_dir, now=now, retention_days=30)

        self.assertEqual(2, deleted)
        self.assertFalse(old.exists())
        self.assertFalse(lifecycle_old.exists())
        self.assertTrue(recent.exists())
        self.assertTrue(unrelated.exists())
        self.assertTrue(symlink.is_symlink())
        self.assertTrue(target.exists())

    def test_backup_retention_rejects_symlink_directory_and_ancestor(self) -> None:
        real_parent = self.root / "real"
        real_parent.mkdir(mode=0o700)
        real_backups = real_parent / "backups"
        real_backups.mkdir(mode=0o700)
        direct_link = self.root / "direct-link"
        direct_link.symlink_to(real_backups, target_is_directory=True)
        ancestor_link = self.root / "ancestor-link"
        ancestor_link.symlink_to(real_parent, target_is_directory=True)
        prune = getattr(
            schema_module,
            "prune_private_memory_backups",
            lambda directory, **kwargs: 0,
        )
        for directory in (direct_link, ancestor_link / "backups"):
            with self.subTest(directory=directory), self.assertRaises(ValueError):
                prune(
                    directory,
                    now=datetime(2026, 8, 23, tzinfo=timezone.utc),
                    retention_days=30,
                )

    def test_migration_cli_loads_shell_hostile_instance_dotenv_without_sourcing(self) -> None:
        env_file = self.root / ".env"
        synthetic_id = "".join(("246", "813", "579"))
        env_file.write_text(
            f"TARGET_GROUP_ID={synthetic_id}\n"
            "SHELL_HOSTILE_VALUE=[one, two]\n",
            encoding="utf-8",
        )
        database = self.root / "data" / "chat_archive.db"
        database.parent.mkdir()
        with closing(sqlite3.connect(database)) as connection:
            connection.execute("CREATE TABLE marker(value TEXT)")
            connection.commit()
        backup_dir = self.root / "backups" / "private_memory"
        backup_dir.mkdir(parents=True, mode=0o700)
        environment = os.environ.copy()
        environment.pop("TARGET_GROUP_ID", None)
        environment["BOT_INSTANCE_ROOT"] = str(self.root)
        result = subprocess.run(
            [
                sys.executable,
                str(PROJECT_ROOT / "scripts/migrate_private_memory.py"),
                "--database",
                str(database),
                "--backup-dir",
                str(backup_dir),
            ],
            cwd=PROJECT_ROOT,
            env=environment,
            capture_output=True,
            text=True,
        )
        self.assertEqual(0, result.returncode, result.stderr)
        self.assertIn("preflight=ok", result.stdout)

    @unittest.skipUnless(Path("/var").is_symlink(), "requires the macOS /var alias")
    def test_cli_rejects_noncanonical_system_alias_before_preflight_or_apply(self) -> None:
        self.assertNotEqual(self.alias_root, self.root)
        with closing(sqlite3.connect(self.database)) as connection:
            connection.execute("CREATE TABLE marker(value TEXT NOT NULL)")
            connection.execute("INSERT INTO marker VALUES ('unchanged')")
            connection.commit()
        backup_dir = self.root / "backups" / "private_memory"
        backup_dir.mkdir(parents=True, mode=0o700)
        alias_database = self.alias_root / self.database.name
        alias_backup_dir = self.alias_root / "backups" / "private_memory"
        before = self.database.read_bytes()
        base_command = [
            sys.executable,
            "scripts/migrate_private_memory.py",
            "--database",
            str(alias_database),
            "--backup-dir",
            str(alias_backup_dir),
        ]

        for apply_args in ((), ("--apply",)):
            with self.subTest(mode=apply_args or ("preflight",)):
                result = subprocess.run(
                    [*base_command, *apply_args],
                    cwd=PROJECT_ROOT,
                    capture_output=True,
                    text=True,
                    check=False,
                )
                self.assertNotEqual(0, result.returncode)
                self.assertIn("symlink", result.stderr)
                self.assertEqual(before, self.database.read_bytes())
                self.assertEqual([], list(backup_dir.iterdir()))

        canonical_command = [
            sys.executable,
            "scripts/migrate_private_memory.py",
            "--database",
            str(self.database),
            "--backup-dir",
            str(backup_dir),
        ]
        preflight = subprocess.run(
            canonical_command,
            cwd=PROJECT_ROOT,
            capture_output=True,
            text=True,
            check=False,
        )
        applied = subprocess.run(
            [*canonical_command, "--apply"],
            cwd=PROJECT_ROOT,
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(0, preflight.returncode, preflight.stderr)
        self.assertEqual(0, applied.returncode, applied.stderr)
        self.assertEqual(1, len(list(backup_dir.glob("*.sqlite3"))))

    def test_online_backup_rejects_existing_insecure_directory_without_chmod(self) -> None:
        with closing(sqlite3.connect(self.database)) as connection:
            connection.execute("CREATE TABLE marker(value TEXT NOT NULL)")
            connection.commit()
        backup_dir = self.root / "insecure-backups"
        backup_dir.mkdir(mode=0o755)
        backup_dir.chmod(0o755)

        with self.assertRaises(PermissionError):
            online_backup(self.database, backup_dir / "snapshot.sqlite3")

        self.assertEqual(0o755, stat.S_IMODE(backup_dir.stat().st_mode))
        self.assertFalse((backup_dir / "snapshot.sqlite3").exists())

    def test_cli_rejects_missing_or_non_regular_source_without_creating_backup_dir(self) -> None:
        backup_dir = self.root / "backups"
        sources = (self.root / "missing.db", self.root / "database-directory")
        sources[1].mkdir()
        for source in sources:
            for apply_args in ((), ("--apply",)):
                with self.subTest(source=source.name, mode=apply_args or ("preflight",)):
                    completed = subprocess.run(
                        [
                            sys.executable,
                            "scripts/migrate_private_memory.py",
                            "--database",
                            str(source),
                            "--backup-dir",
                            str(backup_dir),
                            *apply_args,
                        ],
                        cwd=PROJECT_ROOT,
                        capture_output=True,
                        text=True,
                        check=False,
                    )
                    self.assertNotEqual(0, completed.returncode)
                    self.assertFalse(backup_dir.exists())

    def test_cli_rejects_insecure_existing_backup_dir_without_chmod(self) -> None:
        with closing(sqlite3.connect(self.database)) as connection:
            connection.execute("CREATE TABLE marker(value TEXT NOT NULL)")
            connection.commit()
        backup_dir = self.root / "insecure-backups"
        backup_dir.mkdir(mode=0o755)
        backup_dir.chmod(0o755)

        for apply_args in ((), ("--apply",)):
            with self.subTest(mode=apply_args or ("preflight",)):
                completed = subprocess.run(
                    [
                        sys.executable,
                        "scripts/migrate_private_memory.py",
                        "--database",
                        str(self.database),
                        "--backup-dir",
                        str(backup_dir),
                        *apply_args,
                    ],
                    cwd=PROJECT_ROOT,
                    capture_output=True,
                    text=True,
                    check=False,
                )
                self.assertNotEqual(0, completed.returncode)
                self.assertEqual(0o755, stat.S_IMODE(backup_dir.stat().st_mode))
                self.assertEqual([], list(backup_dir.iterdir()))

    def test_preflight_cli_does_not_write_and_apply_failure_is_nonzero(self) -> None:
        with closing(sqlite3.connect(self.database)) as connection:
            connection.execute("CREATE TABLE marker(value TEXT NOT NULL)")
            connection.execute("INSERT INTO marker VALUES ('original')")
            connection.commit()
        before = self.database.read_bytes()
        command = [
            sys.executable,
            "scripts/migrate_private_memory.py",
            "--database",
            str(self.database),
            "--backup-dir",
            str(self.root / "backups"),
        ]

        preflight = subprocess.run(
            command,
            cwd=PROJECT_ROOT,
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(0, preflight.returncode, preflight.stderr)
        self.assertEqual(before, self.database.read_bytes())
        self.assertFalse((self.root / "backups").exists())

        corrupt = self.root / "corrupt.db"
        corrupt.write_bytes(b"broken sqlite")
        corrupt_before = corrupt.read_bytes()
        wal = Path(f"{corrupt}-wal")
        wal.write_bytes(b"keep-wal")
        failed = subprocess.run(
            [*command[:3], str(corrupt), *command[4:], "--apply"],
            cwd=PROJECT_ROOT,
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertNotEqual(0, failed.returncode)
        self.assertEqual(corrupt_before, corrupt.read_bytes())
        self.assertEqual(b"keep-wal", wal.read_bytes())


if __name__ == "__main__":
    unittest.main()
