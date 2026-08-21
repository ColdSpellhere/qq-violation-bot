import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from plugins.chat_vision.store import ChatVisionStore


class ChatVisionStoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = TemporaryDirectory()
        self.db_path = Path(self.temporary_directory.name) / "chat_vision.db"

    def tearDown(self) -> None:
        self.temporary_directory.cleanup()

    def test_create_is_idempotent_and_preserves_all_ordinals(self) -> None:
        store = ChatVisionStore(self.db_path)
        first = store.ensure_pending(100, "m1", 1, "https://cdn.example/1.jpg", 1000)
        same = store.ensure_pending(100, "m1", 1, "https://cdn.example/1.jpg", 1000)
        second = store.ensure_pending(100, "m1", 2, "https://cdn.example/2.jpg", 1000)

        self.assertEqual(first.id, same.id)
        self.assertEqual([1, 2], [item.ordinal for item in store.for_message(100, "m1")])
        self.assertEqual(2, second.ordinal)

    def test_ready_description_survives_file_deletion(self) -> None:
        store = ChatVisionStore(self.db_path)
        asset = store.ensure_pending(100, "m1", 1, "https://cdn.example/1.jpg", 1000)
        store.mark_downloaded(
            asset.id, "m1-1.jpg", "image/jpeg", 12, "abc",
            "2026-08-28 00:00:00",
        )
        store.mark_ready(asset.id, "一朵花")
        store.mark_deleted(asset.id, "2026-08-29 00:00:00")

        saved = store.for_message(100, "m1")[0]
        self.assertEqual("一朵花", saved.description)
        self.assertIsNone(saved.relative_path)


if __name__ == "__main__":
    unittest.main()
