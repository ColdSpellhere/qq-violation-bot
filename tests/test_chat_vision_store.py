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

    def test_failed_asset_is_claimable_only_below_retry_limit(self) -> None:
        store = ChatVisionStore(self.db_path)
        asset = store.ensure_pending(100, "m1", 1, "https://cdn.example/1.jpg", 1000)
        first_claim = store.claim(asset.id, max_retries=2)
        self.assertIsNotNone(first_claim)
        store.mark_downloaded(
            asset.id, "m1-1.jpg", "image/jpeg", 12, "abc",
            "2026-08-28 00:00:00",
        )
        store.mark_failed(asset.id, "model_error")

        self.assertEqual([asset.id], [item.id for item in store.claimable(max_retries=2)])
        retry_claim = store.claim(asset.id, max_retries=2)
        self.assertIsNotNone(retry_claim)
        store.mark_failed(asset.id, "model_error")

        self.assertEqual([], store.claimable(max_retries=2))
        self.assertIsNone(store.claim(asset.id, max_retries=2))
        saved = store.for_message(100, "m1")[0]
        self.assertEqual("m1-1.jpg", saved.relative_path)
        self.assertEqual(2, saved.attempts)

    def test_second_store_constructor_does_not_reset_active_claim(self) -> None:
        first_store = ChatVisionStore(self.db_path)
        asset = first_store.ensure_pending(100, "m1", 1, "https://cdn.example/1.jpg", 1000)
        self.assertIsNotNone(first_store.claim(asset.id, max_retries=3))

        second_store = ChatVisionStore(self.db_path)

        self.assertEqual("processing", second_store.for_message(100, "m1")[0].status)
        self.assertIsNone(second_store.claim(asset.id, max_retries=3))

    def test_explicit_recovery_makes_interrupted_claim_claimable(self) -> None:
        store = ChatVisionStore(self.db_path)
        asset = store.ensure_pending(100, "m1", 1, "https://cdn.example/1.jpg", 1000)
        self.assertIsNotNone(store.claim(asset.id, max_retries=3))

        store.recover_interrupted_claims()

        self.assertEqual([asset.id], [item.id for item in store.claimable(max_retries=3)])


if __name__ == "__main__":
    unittest.main()
