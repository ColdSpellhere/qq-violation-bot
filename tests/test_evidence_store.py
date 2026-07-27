from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

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


if __name__ == "__main__":
    unittest.main()
