import hashlib
import sqlite3
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from plugins.chat_vision.store import ChatVisionStore, read_original_image


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

    def test_database_operations_close_their_connections(self) -> None:
        connections = []
        real_connect = sqlite3.connect

        def tracking_connect(*args, **kwargs):
            connection = real_connect(*args, **kwargs)
            connections.append(connection)
            return connection

        with patch("plugins.chat_vision.store.sqlite3.connect", side_effect=tracking_connect):
            store = ChatVisionStore(self.db_path)
            store.ensure_pending(100, "m1", 1, "https://cdn.example/1.jpg", 1000)
            store.for_message(100, "m1")

        self.assertEqual(3, len(connections))
        for connection in connections:
            with self.assertRaises(sqlite3.ProgrammingError):
                connection.execute("SELECT 1")

    def test_explicit_recovery_makes_interrupted_claim_claimable(self) -> None:
        store = ChatVisionStore(self.db_path)
        asset = store.ensure_pending(100, "m1", 1, "https://cdn.example/1.jpg", 1000)
        self.assertIsNotNone(store.claim(asset.id, max_retries=3))

        store.recover_interrupted_claims()

        self.assertEqual([asset.id], [item.id for item in store.claimable(max_retries=3)])

    def test_reads_available_original_from_regular_file_under_root(self) -> None:
        root = Path(self.temporary_directory.name) / "images"
        root.mkdir()
        content = b"raw-image"
        (root / "m1.jpg").write_bytes(content)
        store = ChatVisionStore(self.db_path)
        asset = store.ensure_pending(100, "m1", 1, "https://cdn.example/1.jpg", 1000)
        store.mark_downloaded(
            asset.id,
            "m1.jpg",
            "image/jpeg",
            len(content),
            hashlib.sha256(content).hexdigest(),
            "2099-08-28 00:00:00",
        )
        asset = store.for_message(100, "m1")[0]
        self.assertEqual(
            content,
            read_original_image(asset, root, now_text="2026-08-21 00:00:00"),
        )

    def test_rejects_expired_deleted_outside_and_symlink_originals(self) -> None:
        root = Path(self.temporary_directory.name) / "images"
        root.mkdir()
        outside = Path(self.temporary_directory.name) / "outside.jpg"
        outside.write_bytes(b"outside")
        (root / "link.jpg").symlink_to(outside)
        (root / "directory.jpg").mkdir()
        store = ChatVisionStore(self.db_path)
        fixtures = (
            ("expired", "link.jpg", "2026-08-20 00:00:00", None),
            ("deleted", "link.jpg", "2099-08-28 00:00:00", "2026-08-21 00:00:00"),
            ("outside", "../outside.jpg", "2099-08-28 00:00:00", None),
            ("symlink", "link.jpg", "2099-08-28 00:00:00", None),
            ("directory", "directory.jpg", "2099-08-28 00:00:00", None),
            ("missing", "missing.jpg", "2099-08-28 00:00:00", None),
        )
        assets = []
        for ordinal, (message_id, relative_path, expires_at, deleted_at) in enumerate(
            fixtures, start=1
        ):
            asset = store.ensure_pending(
                100, message_id, ordinal, "https://cdn.example/image.jpg", 1000
            )
            store.mark_downloaded(
                asset.id, relative_path, "image/jpeg", 7, "unused", expires_at
            )
            if deleted_at is not None:
                store.mark_deleted(asset.id, deleted_at)
            assets.append(store.for_message(100, message_id)[0])
        for asset in assets:
            with self.subTest(message_id=asset.message_id):
                self.assertIsNone(
                    read_original_image(
                        asset, root, now_text="2026-08-21 00:00:00"
                    )
                )


if __name__ == "__main__":
    unittest.main()
