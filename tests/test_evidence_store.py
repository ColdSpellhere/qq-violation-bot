from __future__ import annotations

import os
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest import mock

from plugins.violation_record.evidence_store import EvidenceStore


JPEG = b"\xff\xd8\xff\xe0" + (b"x" * 32) + b"\xff\xd9"
JPEG_2 = b"\xff\xd8\xff\xe1" + (b"y" * 32) + b"\xff\xd9"


class EvidenceStoreTests(unittest.TestCase):
    def test_one_record_binds_multiple_deduplicated_images(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "evidence"
            store = EvidenceStore(Path(directory) / "evidence.db", root)
            batch = store.create_batch(123456789, "90001", "cmd-1")
            first = store.add_bytes(batch, JPEG, "image/jpeg", 123456789, "src-1", 1)
            second = store.add_bytes(batch, JPEG, "image/jpeg", 123456789, "src-1", 2)
            store.add_bytes(batch, JPEG_2, "image/jpeg", 123456789, "src-1", 3)
            self.assertEqual(first, second)
            store.bind_batch(batch, 42, "123456")
            paths = store.paths_for_violations([42])
            self.assertEqual(2, len(paths[42]))
            self.assertTrue(all(path.is_file() for path in paths[42]))

    def test_old_record_without_mapping_returns_empty_tuple(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = EvidenceStore(Path(directory) / "evidence.db", Path(directory) / "evidence")
            self.assertEqual({7: ()}, store.paths_for_violations([7]))

    def test_binding_queue_retries_and_bound_file_survives_cleanup(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "evidence"
            store = EvidenceStore(Path(directory) / "evidence.db", root)
            batch = store.create_batch(123456789, "90001", "cmd-2")
            store.add_bytes(batch, JPEG, "image/jpeg", 123456789, "src-2", 1)
            queue_path = store.queue_binding(batch, 84, "654321")
            self.assertTrue(queue_path.is_file())
            self.assertEqual(1, store.retry_binding_queue())
            bound_path = store.paths_for_violations([84])[84][0]
            store.cleanup_transient()
            self.assertTrue(bound_path.is_file())

    def test_cleanup_continues_after_old_part_unlink_failure(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "evidence"
            store = EvidenceStore(Path(directory) / "evidence.db", root)
            current = datetime(2026, 1, 10, 12, 0, 0)
            old_timestamp = (current - timedelta(hours=2)).timestamp()
            first = root / "a.part"
            second = root / "b.part"
            first.write_bytes(b"first")
            second.write_bytes(b"second")
            os.utime(first, (old_timestamp, old_timestamp))
            os.utime(second, (old_timestamp, old_timestamp))
            real_unlink = Path.unlink

            def unlink_with_first_failure(path: Path, *args: object, **kwargs: object) -> None:
                if path == first:
                    raise OSError("simulated unlink failure")
                real_unlink(path, *args, **kwargs)

            with mock.patch.object(Path, "unlink", autospec=True, side_effect=unlink_with_first_failure):
                try:
                    result = store.cleanup_transient(now=current)
                except OSError as error:
                    self.fail(f"cleanup should isolate part failures: {error}")

            self.assertEqual({"parts": 1, "files": 0}, result)
            self.assertTrue(first.exists())
            self.assertFalse(second.exists())

    def test_cleanup_rolls_back_failed_batch_and_continues_with_next_batch(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "evidence"
            store = EvidenceStore(Path(directory) / "evidence.db", root)
            first_batch = store.create_batch(123456789, "90001", "cmd-failed")
            second_batch = store.create_batch(123456789, "90001", "cmd-next")
            first_evidence = store.add_bytes(first_batch, JPEG, "image/jpeg", 123456789, "src-failed", 1)
            second_evidence = store.add_bytes(second_batch, JPEG_2, "image/jpeg", 123456789, "src-next", 1)
            current = datetime(2026, 1, 10, 12, 0, 0)

            with store._connect() as conn:
                conn.execute(
                    "UPDATE evidence_batches SET created_at=? WHERE id IN (?,?)",
                    ("2025-12-01 00:00:00", first_batch, second_batch),
                )
                first_path = root / conn.execute(
                    "SELECT relative_path FROM evidence_files WHERE id=?", (first_evidence,)
                ).fetchone()["relative_path"]
                second_path = root / conn.execute(
                    "SELECT relative_path FROM evidence_files WHERE id=?", (second_evidence,)
                ).fetchone()["relative_path"]

            real_unlink = Path.unlink

            def unlink_with_first_failure(path: Path, *args: object, **kwargs: object) -> None:
                if path == first_path:
                    raise OSError("simulated batch image unlink failure")
                real_unlink(path, *args, **kwargs)

            with mock.patch.object(Path, "unlink", autospec=True, side_effect=unlink_with_first_failure):
                try:
                    first_result = store.cleanup_transient(now=current)
                except OSError as error:
                    self.fail(f"cleanup should isolate batch failures: {error}")

            self.assertEqual({"parts": 0, "files": 1}, first_result)
            self.assertTrue(first_path.is_file())
            self.assertFalse(second_path.exists())
            with store._connect() as conn:
                self.assertEqual(1, conn.execute("SELECT COUNT(*) FROM evidence_batches WHERE id=?", (first_batch,)).fetchone()[0])
                self.assertEqual(1, conn.execute("SELECT COUNT(*) FROM evidence_batch_items WHERE batch_id=?", (first_batch,)).fetchone()[0])
                self.assertEqual(1, conn.execute("SELECT COUNT(*) FROM evidence_files WHERE id=?", (first_evidence,)).fetchone()[0])
                self.assertEqual(0, conn.execute("SELECT COUNT(*) FROM evidence_batches WHERE id=?", (second_batch,)).fetchone()[0])
                self.assertEqual(0, conn.execute("SELECT COUNT(*) FROM evidence_files WHERE id=?", (second_evidence,)).fetchone()[0])

            second_result = store.cleanup_transient(now=current)
            self.assertEqual({"parts": 0, "files": 1}, second_result)
            self.assertFalse(first_path.exists())
            with store._connect() as conn:
                self.assertEqual(0, conn.execute("SELECT COUNT(*) FROM evidence_batches WHERE id=?", (first_batch,)).fetchone()[0])
                self.assertEqual(0, conn.execute("SELECT COUNT(*) FROM evidence_files WHERE id=?", (first_evidence,)).fetchone()[0])


if __name__ == "__main__":
    unittest.main()
