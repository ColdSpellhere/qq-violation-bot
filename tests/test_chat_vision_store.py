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

    def test_ensure_pending_rejects_non_positive_group_before_insert(self) -> None:
        store = ChatVisionStore(self.db_path)

        for group_id in (0, -1):
            with self.subTest(group_id=group_id):
                with self.assertRaisesRegex(ValueError, "positive"):
                    store.ensure_pending(
                        group_id,
                        f"m{group_id}",
                        1,
                        "https://cdn.example/image.jpg",
                        1000,
                    )

        with sqlite3.connect(self.db_path) as conn:
            count = conn.execute("SELECT COUNT(*) FROM chat_image_assets").fetchone()[0]
        self.assertEqual(0, count)

    def test_schema_has_status_check_and_audit_timestamps(self) -> None:
        store = ChatVisionStore(self.db_path)
        asset = store.ensure_pending(
            100,
            "m1",
            1,
            "https://cdn.example/1.jpg",
            1000,
        )

        self.assertTrue(hasattr(asset, "created_at"))
        self.assertTrue(hasattr(asset, "updated_at"))
        if not hasattr(asset, "created_at") or not hasattr(asset, "updated_at"):
            return
        self.assertTrue(asset.created_at)
        self.assertTrue(asset.updated_at)
        with sqlite3.connect(self.db_path) as conn:
            with self.assertRaises(sqlite3.IntegrityError):
                conn.execute(
                    "INSERT INTO chat_image_assets("
                    "group_id,message_id,ordinal,source_url,event_time,status"
                    ") VALUES(?,?,?,?,?,?)",
                    (100, "bad", 1, "https://cdn.example/bad.jpg", 1000, "unknown"),
                )

    def test_existing_table_is_migrated_without_losing_assets(self) -> None:
        with sqlite3.connect(self.db_path) as conn:
            conn.executescript(
                """
                CREATE TABLE chat_image_assets (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    group_id INTEGER NOT NULL,
                    message_id TEXT NOT NULL,
                    ordinal INTEGER NOT NULL,
                    source_url TEXT NOT NULL,
                    event_time INTEGER NOT NULL,
                    status TEXT NOT NULL DEFAULT 'pending',
                    attempts INTEGER NOT NULL DEFAULT 0,
                    relative_path TEXT,
                    mime_type TEXT,
                    byte_size INTEGER,
                    sha256 TEXT,
                    description TEXT,
                    expires_at TEXT,
                    deleted_at TEXT,
                    error_type TEXT,
                    UNIQUE(group_id, message_id, ordinal)
                );
                INSERT INTO chat_image_assets(
                    group_id,message_id,ordinal,source_url,event_time,status,attempts,
                    description
                ) VALUES(100,'legacy',1,'https://cdn.example/legacy.jpg',1000,'ready',1,
                         '一朵老花');
                """
            )

        store = ChatVisionStore(self.db_path)

        saved = store.for_message(100, "legacy")
        self.assertEqual(1, len(saved))
        self.assertEqual("一朵老花", saved[0].description)
        self.assertEqual("ready", saved[0].status)
        self.assertTrue(hasattr(saved[0], "created_at"))
        self.assertTrue(hasattr(saved[0], "updated_at"))
        with sqlite3.connect(self.db_path) as conn:
            columns = {
                row[1] for row in conn.execute("PRAGMA table_info(chat_image_assets)")
            }
            self.assertIn("created_at", columns)
            self.assertIn("updated_at", columns)
            with self.assertRaises(sqlite3.IntegrityError):
                conn.execute(
                    "UPDATE chat_image_assets SET status='invalid' WHERE message_id='legacy'"
                )

    def test_existing_audit_columns_do_not_mask_a_missing_status_check(self) -> None:
        with sqlite3.connect(self.db_path) as conn:
            conn.executescript(
                """
                CREATE TABLE chat_image_assets (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    group_id INTEGER NOT NULL,
                    message_id TEXT NOT NULL,
                    ordinal INTEGER NOT NULL,
                    source_url TEXT NOT NULL,
                    event_time INTEGER NOT NULL,
                    status TEXT NOT NULL DEFAULT 'pending',
                    attempts INTEGER NOT NULL DEFAULT 0 CHECK(attempts >= 0),
                    relative_path TEXT,
                    mime_type TEXT,
                    byte_size INTEGER,
                    sha256 TEXT,
                    description TEXT,
                    expires_at TEXT,
                    deleted_at TEXT,
                    error_type TEXT,
                    created_at TEXT,
                    updated_at TEXT,
                    UNIQUE(group_id, message_id, ordinal)
                );
                INSERT INTO chat_image_assets(
                    group_id,message_id,ordinal,source_url,event_time,status,attempts
                ) VALUES(100,'partial',1,'https://cdn.example/partial.jpg',1000,'ready',1);
                """
            )

        store = ChatVisionStore(self.db_path)
        saved = store.for_message(100, "partial")[0]

        self.assertNotEqual("None", saved.created_at)
        self.assertNotEqual("None", saved.updated_at)
        with sqlite3.connect(self.db_path) as conn:
            with self.assertRaises(sqlite3.IntegrityError):
                conn.execute(
                    "UPDATE chat_image_assets SET status='invalid' WHERE message_id='partial'"
                )

    def test_every_state_change_refreshes_updated_at(self) -> None:
        store = ChatVisionStore(self.db_path)
        asset = store.ensure_pending(
            100,
            "m1",
            1,
            "https://cdn.example/1.jpg",
            1000,
        )

        with sqlite3.connect(self.db_path) as conn:
            columns = {
                row[1] for row in conn.execute("PRAGMA table_info(chat_image_assets)")
            }
        self.assertIn("updated_at", columns)
        if "updated_at" not in columns:
            return

        def assert_refreshes(action) -> None:
            with sqlite3.connect(self.db_path) as conn:
                conn.execute(
                    "UPDATE chat_image_assets SET updated_at='2000-01-01 00:00:00' WHERE id=?",
                    (asset.id,),
                )
            action()
            with sqlite3.connect(self.db_path) as conn:
                updated_at = conn.execute(
                    "SELECT updated_at FROM chat_image_assets WHERE id=?",
                    (asset.id,),
                ).fetchone()[0]
            self.assertNotEqual("2000-01-01 00:00:00", updated_at)

        assert_refreshes(lambda: store.claim(asset.id, max_retries=3))
        assert_refreshes(
            lambda: store.mark_downloaded(
                asset.id,
                "m1-1.jpg",
                "image/jpeg",
                12,
                "abc",
                "2026-08-28 00:00:00",
            )
        )
        assert_refreshes(lambda: store.mark_failed(asset.id, "test"))
        assert_refreshes(lambda: store.claim(asset.id, max_retries=3))
        assert_refreshes(lambda: store.mark_ready(asset.id, "一朵花"))
        assert_refreshes(lambda: store.mark_deleted(asset.id, "2026-08-29 00:00:00"))

        interrupted = store.ensure_pending(
            100,
            "m2",
            1,
            "https://cdn.example/2.jpg",
            1000,
        )
        store.claim(interrupted.id, max_retries=3)
        with sqlite3.connect(self.db_path) as conn:
            conn.execute(
                "UPDATE chat_image_assets SET updated_at='2000-01-01 00:00:00' WHERE id=?",
                (interrupted.id,),
            )
        store.recover_interrupted_claims()
        with sqlite3.connect(self.db_path) as conn:
            updated_at = conn.execute(
                "SELECT updated_at FROM chat_image_assets WHERE id=?",
                (interrupted.id,),
            ).fetchone()[0]
        self.assertNotEqual("2000-01-01 00:00:00", updated_at)

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

    def test_retry_due_time_survives_store_reopen(self):
        store=ChatVisionStore(self.db_path)
        asset=store.ensure_pending(100,"retry-time",1,"https://cdn.example/a.jpg",2000)
        with patch("plugins.chat_vision.store.time.time",return_value=100):
            store.claim(asset.id,3)
            store.mark_failed(asset.id,"synthetic",retry_delay=10)
            self.assertEqual([],store.claimable(3))
        reopened=ChatVisionStore(self.db_path)
        with patch("plugins.chat_vision.store.time.time",return_value=109):
            self.assertIsNone(reopened.claim(asset.id,3))
        with patch("plugins.chat_vision.store.time.time",return_value=110):
            self.assertEqual(asset.id,reopened.claim(asset.id,3).id)

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

    def test_claimable_filters_old_and_nonretryable_failures(self) -> None:
        store = ChatVisionStore(self.db_path)
        old = store.ensure_pending(
            100, "old", 1, "https://cdn.example/old.jpg", 1000
        )
        retryable = store.ensure_pending(
            100, "retryable", 1, "https://cdn.example/retry.jpg", 2000
        )
        payment_code = store.ensure_pending(
            100, "payment-code", 1, "https://cdn.example/payment.jpg", 2001
        )
        payment_class = store.ensure_pending(
            100, "payment-class", 1, "https://cdn.example/payment2.jpg", 2002
        )
        pending = store.ensure_pending(
            100, "pending", 1, "https://cdn.example/pending.jpg", 2003
        )
        for asset, error_type in (
            (old, "server_error"),
            (retryable, "server_error"),
            (payment_code, "payment_required"),
            (payment_class, "GatewayPaymentRequiredError"),
        ):
            self.assertIsNotNone(store.claim(asset.id, max_retries=3))
            store.mark_failed(asset.id, error_type)

        claimable = store.claimable(max_retries=3, min_event_time=2000)

        self.assertEqual([retryable.id, pending.id], [asset.id for asset in claimable])

    def test_nonretryable_failure_cannot_be_claimed_directly(self) -> None:
        store = ChatVisionStore(self.db_path)

        for ordinal, error_type in enumerate(
            ("payment_required", "GatewayPaymentRequiredError"), start=1
        ):
            asset = store.ensure_pending(
                100,
                f"payment-{ordinal}",
                ordinal,
                "https://cdn.example/payment.jpg",
                2000,
            )
            self.assertIsNotNone(store.claim(asset.id, max_retries=3))
            store.mark_failed(asset.id, error_type)

            self.assertIsNone(store.claim(asset.id, max_retries=3))

    def test_legacy_non_positive_group_is_not_claimable(self) -> None:
        store = ChatVisionStore(self.db_path)
        with sqlite3.connect(self.db_path) as conn:
            cursor = conn.execute(
                "INSERT INTO chat_image_assets("
                "group_id,message_id,ordinal,source_url,event_time,status,attempts"
                ") VALUES(0,'legacy-private',1,'https://cdn.example/image.jpg',"
                "2000,'pending',0)"
            )
            asset_id = int(cursor.lastrowid)

        self.assertEqual([], store.claimable(max_retries=3, min_event_time=1000))
        self.assertIsNone(store.claim(asset_id, max_retries=3))

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
        root = Path(self.temporary_directory.name) / "data" / "chat_vision" / "images"
        root.mkdir(parents=True)
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
        root = Path(self.temporary_directory.name) / "data" / "chat_vision" / "images"
        root.mkdir(parents=True)
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
