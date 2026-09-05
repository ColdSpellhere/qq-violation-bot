from __future__ import annotations

import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from scripts.instance_backup import create_backup, verify_backup


class InstanceBackupTests(unittest.TestCase):
    def test_state_keeps_configuration_recovery_copies(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            instance = root / 'instances' / 'kona'
            (instance / 'data').mkdir(parents=True)
            (instance / '.env').write_text('BOT_MODE=chat_only\n')
            for name in ('runtime_features.json.bak', 'keywords.json.bak'):
                (instance / 'data' / name).write_text('{"recovery":true}')
            manifest = verify_backup(create_backup(root, 'kona'))
            self.assertIn('data/runtime_features.json.bak', manifest['files'])
            self.assertIn('data/keywords.json.bak', manifest['files'])

    def test_damaged_sqlite_header_fails_backup_instead_of_omitting_database(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            instance = root / 'instances' / 'kona'
            (instance / 'data').mkdir(parents=True)
            (instance / '.env').write_text('BOT_MODE=chat_only\n')
            (instance / 'data' / 'chat_archive.db').write_bytes(b'damaged database')
            with self.assertRaisesRegex(ValueError, 'database header'):
                create_backup(root, 'kona')

    def test_failed_verification_removes_only_its_incomplete_snapshot(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            instance = root / 'instances' / 'kona'
            (instance / 'data').mkdir(parents=True)
            (instance / '.env').write_text('BOT_MODE=chat_only\n')
            existing = create_backup(root, 'kona')
            with patch('scripts.instance_backup.verify_backup', side_effect=ValueError('synthetic failure')):
                with self.assertRaises(ValueError):
                    create_backup(root, 'kona')
            self.assertEqual([], list(existing.parent.glob('.pending-*')))
            self.assertTrue(existing.is_dir())
            verify_backup(existing)

    def test_canonical_external_database_and_restore_integrity_are_recorded(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            instance = root / "instances" / "carrot"
            (instance / "data").mkdir(parents=True)
            legacy = root / "legacy.db"
            with sqlite3.connect(legacy) as db:
                db.execute("CREATE TABLE entries(value TEXT)")
                db.execute("INSERT INTO entries VALUES('synthetic')")
            (instance / ".env").write_text(f"DATABASE_URL=sqlite:///{legacy}\nTOKEN=synthetic-private-value\n")
            (instance / "character.md").write_text("private persona")
            (instance / "data" / "rules.json").write_text('{"rule":"synthetic"}')
            snapshot = create_backup(root, "carrot", mode="state")
            manifest = verify_backup(snapshot)
            self.assertEqual([str(legacy)], [item["source"] for item in manifest["databases"]])
            restored = snapshot / manifest["databases"][0]["file"]
            with sqlite3.connect(restored) as db:
                self.assertEqual([("synthetic",)], db.execute("SELECT value FROM entries").fetchall())
            self.assertNotIn("synthetic-private-value", json.dumps(manifest))
            self.assertEqual(0o700, snapshot.stat().st_mode & 0o777)
            self.assertEqual(0o600, restored.stat().st_mode & 0o777)
            restored.write_bytes(b"corrupt")
            with self.assertRaises(ValueError):
                verify_backup(snapshot)

    def test_full_backup_includes_media_but_state_backup_records_exclusion(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            instance = root / "instances" / "kona"
            (instance / "data" / "images").mkdir(parents=True)
            (instance / ".env").write_text("BOT_MODE=chat_only\n")
            (instance / "data" / "images" / "one.jpg").write_bytes(b"synthetic image")
            state = verify_backup(create_backup(root, "kona", mode="state"))
            self.assertNotIn("data/images/one.jpg", state["files"])
            full = verify_backup(create_backup(root, "kona", mode="full"))
            self.assertIn("data/images/one.jpg", full["files"])

    def test_symlink_data_is_rejected_instead_of_silently_incomplete_backup(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            instance = root / "instances" / "kona"
            (instance / "data").mkdir(parents=True)
            (instance / ".env").write_text("BOT_MODE=chat_only\n")
            (root / "outside").write_text("synthetic")
            (instance / "data" / "link.json").symlink_to(root / "outside")
            with self.assertRaises(ValueError):
                create_backup(root, "kona", mode="full")

    def test_retention_only_prunes_own_verified_snapshots_and_keeps_history(self):
        from scripts.instance_backup import prune_managed
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            instance = root / "instances" / "kona"
            (instance / "data").mkdir(parents=True)
            (instance / ".env").write_text("BOT_MODE=chat_only\n")
            snapshots = [create_backup(root, "kona", mode="state") for _ in range(4)]
            historical = snapshots[0].parent / "audit-original"
            historical.mkdir()
            (historical / "evidence").write_text("keep")
            removed = prune_managed(root, "kona", keep_state=2, keep_full=2)
            self.assertEqual(2, len(removed))
            self.assertTrue(historical.exists())
            self.assertTrue(all(path.exists() for path in snapshots[2:]))
