from __future__ import annotations

import sqlite3
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from plugins.violation_record import db


class OnlineBackupTests(unittest.TestCase):
    def test_backup_is_integral_and_contains_committed_rows(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source.db"
            backups = root / "backups"
            with sqlite3.connect(source) as conn:
                conn.execute("CREATE TABLE records(value TEXT NOT NULL)")
                conn.execute("INSERT INTO records VALUES('kept')")
            config = replace(
                db.CONFIG,
                database_path=source,
                database_url=f"sqlite:///{source}",
            )
            with patch.object(db, "CONFIG", config), patch.object(db, "BACKUP_DIR", backups):
                destination = db.backup_database("test")
            self.assertIsNotNone(destination)
            self.assertFalse(any(backups.glob("*.part")))
            with sqlite3.connect(destination) as conn:
                self.assertEqual("ok", conn.execute("PRAGMA integrity_check").fetchone()[0])
                self.assertEqual("kept", conn.execute("SELECT value FROM records").fetchone()[0])

    def test_missing_source_returns_none_without_artifacts(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "missing.db"
            backups = root / "backups"
            config = replace(db.CONFIG, database_path=source, database_url=f"sqlite:///{source}")
            with patch.object(db, "CONFIG", config), patch.object(db, "BACKUP_DIR", backups):
                self.assertIsNone(db.backup_database("test"))
            self.assertFalse(backups.exists())

    def test_uses_sqlite_online_backup_api(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source.db"
            backups = root / "backups"
            backups.mkdir()
            real_connect = sqlite3.connect
            with real_connect(source) as conn:
                conn.execute("CREATE TABLE records(value TEXT NOT NULL)")
            backup_calls: list[tuple[object, object]] = []

            class ConnectionProxy:
                def __init__(self, inner):
                    self.inner = inner

                def __enter__(self):
                    self.inner.__enter__()
                    return self

                def __exit__(self, exc_type, exc, traceback):
                    return self.inner.__exit__(exc_type, exc, traceback)

                def __getattr__(self, name):
                    return getattr(self.inner, name)

                def backup(self, target):
                    target_connection = target.inner if isinstance(target, ConnectionProxy) else target
                    backup_calls.append((self.inner, target_connection))
                    return self.inner.backup(target_connection)

            def tracked_connect(*args, **kwargs):
                return ConnectionProxy(real_connect(*args, **kwargs))

            config = replace(db.CONFIG, database_path=source, database_url=f"sqlite:///{source}")
            with (
                patch.object(db, "CONFIG", config),
                patch.object(db, "BACKUP_DIR", backups),
                patch.object(db.sqlite3, "connect", side_effect=tracked_connect),
            ):
                db.backup_database("api")
            self.assertEqual(1, len(backup_calls))


if __name__ == "__main__":
    unittest.main()
