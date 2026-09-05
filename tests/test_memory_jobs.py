from __future__ import annotations

import asyncio
import os
import sqlite3
import stat
import unittest
from contextlib import closing
from datetime import datetime, timedelta, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

from plugins.private_memory.schema import PRIVATE_MEMORY_SCHEMA_VERSION, migrate, schema_version


UTC = timezone.utc
NOW = datetime(2026, 8, 23, 1, 2, 3, tzinfo=UTC)


class FakeDriver:
    def __init__(self) -> None:
        self.startup_callbacks = []
        self.shutdown_callbacks = []

    def on_startup(self, callback):
        self.startup_callbacks.append(callback)
        return callback

    def on_shutdown(self, callback):
        self.shutdown_callbacks.append(callback)
        return callback

    async def startup(self) -> None:
        for callback in self.startup_callbacks:
            await callback()

    async def shutdown(self) -> None:
        for callback in reversed(self.shutdown_callbacks):
            await callback()


class MemoryJobQueueTests(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.database = Path(self.directory.name) / "chat_archive.db"
        migrate(self.database)
        from plugins.private_memory.jobs import MemoryJobQueue

        now_patch = patch("plugins.private_memory.jobs._now", return_value=NOW)
        now_patch.start()
        self.addCleanup(now_patch.stop)
        self.queue = MemoryJobQueue(
            self.database, lease_seconds=10, max_attempts=3, backoff_base_seconds=2, relationship_debounce_seconds=0
        )

    def enqueue(self, user_id: str = "200", watermark: int = 1, **changes: object) -> int:
        values = {
            "job_type": "private_summary",
            "conversation_kind": "private",
            "user_id": user_id,
            "group_id": None,
            "input_through_id": watermark,
            "expected_version": 0,
        }
        values.update(changes)
        return self.queue.enqueue(**values)

    def test_enqueue_is_idempotent_for_exact_scope_type_and_watermark(self) -> None:
        first = self.enqueue()
        second = self.enqueue()
        self.assertEqual(first, second)
        with closing(sqlite3.connect(self.database)) as connection:
            self.assertEqual(1, connection.execute("SELECT count(*) FROM memory_jobs").fetchone()[0])

    def test_terminal_job_can_be_enqueued_again_with_a_new_expected_version(self) -> None:
        first_id = self.enqueue()
        first = self.queue.claim(
            worker_id="worker-a", now=NOW, limit=1,
            allowed_job_types={"private_summary"},
        )[0]
        self.assertTrue(self.queue.finish(first, worker_id="worker-a", status="succeeded"))

        second_id = self.enqueue(expected_version=4)

        self.assertNotEqual(first_id, second_id)
        self.assertEqual(4, self.queue.get(second_id).expected_version)
        self.assertEqual(second_id, self.enqueue(expected_version=9))

    def test_enqueue_strictly_validates_scope_identifiers_and_job_type(self) -> None:
        invalid = (
            {"job_type": "unknown"},
            {"job_type": "private_summary", "conversation_kind": "group", "group_id": 123},
            {"job_type": "private_facts", "conversation_kind": "group", "group_id": 123},
            {"conversation_kind": "room"},
            {"user_id": "２００"},
            {"user_id": "0"},
            {"conversation_kind": "private", "group_id": 123},
            {"conversation_kind": "group", "group_id": None},
            {"conversation_kind": "group", "group_id": 0},
            {"input_through_id": -1},
            {"expected_version": -1},
            {"persona_id": "Radish-Cat"},
            {"persona_id": "萝卜猫"},
            {"persona_id": "radish_cat"},
            {"persona_id": "-radish"},
            {"persona_id": "radish-"},
            {"persona_id": "a" * 65},
        )
        for changes in invalid:
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                self.enqueue(**changes)

    def test_claim_uses_finite_lease_and_serializes_each_conversation(self) -> None:
        first_id = self.enqueue(watermark=1)
        self.enqueue(watermark=2, job_type="private_facts")
        other_id = self.enqueue(user_id="201")

        claimed = self.queue.claim(
            worker_id="worker-a", now=NOW, limit=4,
            allowed_job_types={"private_summary", "private_facts", "relationship"},
        )

        self.assertEqual([first_id, other_id], [job.id for job in claimed])
        self.assertTrue(all(job.lease_owner == "worker-a" for job in claimed))
        self.assertTrue(all(job.lease_expires_at == "2026-08-23T01:02:13Z" for job in claimed))
        self.assertTrue(all(job.claim_version == 1 for job in claimed))

    def test_finish_requires_owner_claim_version_and_live_running_status(self) -> None:
        job_id = self.enqueue()
        job = self.queue.claim(
            worker_id="owner", now=NOW, limit=1,
            allowed_job_types={"private_summary"},
        )[0]
        stale = job.__class__(**{**job.__dict__, "claim_version": job.claim_version - 1})
        self.assertFalse(self.queue.finish(stale, worker_id="owner", status="succeeded"))
        self.assertFalse(self.queue.finish(job, worker_id="other", status="succeeded"))

        with closing(sqlite3.connect(self.database)) as connection:
            connection.execute(
                "UPDATE memory_jobs SET status='cancelled',lease_owner=NULL,lease_expires_at=NULL WHERE id=?",
                (job_id,),
            )
            connection.commit()
        self.assertFalse(self.queue.finish(job, worker_id="owner", status="succeeded"))

    def test_recover_expired_lease_and_reclaim_increments_claim_version(self) -> None:
        self.enqueue()
        first = self.queue.claim(
            worker_id="dead", now=NOW, limit=1,
            allowed_job_types={"private_summary"},
        )[0]
        self.assertEqual(0, self.queue.recover_expired_leases(now=NOW + timedelta(seconds=9)))
        self.assertEqual(1, self.queue.recover_expired_leases(now=NOW + timedelta(seconds=10)))
        second = self.queue.claim(
            worker_id="new", now=NOW + timedelta(seconds=10), limit=1,
            allowed_job_types={"private_summary"},
        )[0]
        self.assertEqual(first.id, second.id)
        self.assertEqual(2, second.claim_version)

    def test_failures_back_off_then_stop_at_max_attempts_with_sanitized_error(self) -> None:
        self.enqueue()
        first = self.queue.claim(
            worker_id="w", now=NOW, limit=1,
            allowed_job_types={"private_summary"},
        )[0]
        self.assertTrue(self.queue.finish(
            first,
            worker_id="w",
            status="failed",
            error_code="Timeout Error!!",
            error_summary="secret-key=abc\nrequest body should not be persisted",
            now=NOW,
        ))
        self.assertEqual((), self.queue.claim(
            worker_id="w", now=NOW + timedelta(seconds=1), limit=1,
            allowed_job_types={"private_summary"},
        ))
        second = self.queue.claim(
            worker_id="w", now=NOW + timedelta(seconds=2), limit=1,
            allowed_job_types={"private_summary"},
        )[0]
        self.assertTrue(self.queue.finish(
            second, worker_id="w", status="failed", error_code="Timeout Error!!",
            now=NOW + timedelta(seconds=2),
        ))
        third = self.queue.claim(
            worker_id="w", now=NOW + timedelta(seconds=6), limit=1,
            allowed_job_types={"private_summary"},
        )[0]
        self.assertTrue(self.queue.finish(
            third, worker_id="w", status="failed", error_code="Timeout Error!!",
            now=NOW + timedelta(seconds=6),
        ))

        with closing(sqlite3.connect(self.database)) as connection:
            row = connection.execute(
                "SELECT status,attempts,error_code,error_summary FROM memory_jobs"
            ).fetchone()
        self.assertEqual("failed", row[0])
        self.assertEqual(3, row[1])
        self.assertEqual("timeout_error", row[2])
        self.assertNotIn("secret", row[3].lower())
        self.assertNotIn("request body", row[3].lower())

    def test_claim_only_takes_explicitly_allowed_job_types(self) -> None:
        summary = self.enqueue(user_id="200", watermark=1)
        relationship = self.enqueue(
            user_id="201", watermark=1, job_type="relationship"
        )

        claimed = self.queue.claim(
            worker_id="w", now=NOW, limit=5,
            allowed_job_types={"relationship"},
        )

        self.assertEqual([relationship], [job.id for job in claimed])
        self.assertEqual("pending", self.queue.get(summary).status)

    def test_two_queue_instances_concurrently_claim_one_job_once(self) -> None:
        from concurrent.futures import ThreadPoolExecutor
        from plugins.private_memory.jobs import MemoryJobQueue

        job_id = self.enqueue()
        second_queue = MemoryJobQueue(
            self.database, lease_seconds=10, max_attempts=3, backoff_base_seconds=2, relationship_debounce_seconds=0
        )

        def claim(queue, worker):
            return queue.claim(
                worker_id=worker, now=NOW, limit=1,
                allowed_job_types={"private_summary"},
            )

        with ThreadPoolExecutor(max_workers=2) as executor:
            results = list(executor.map(
                lambda args: claim(*args),
                ((self.queue, "one"), (second_queue, "two")),
            ))

        claimed = [job for result in results for job in result]
        self.assertEqual([job_id], [job.id for job in claimed])


class MemoryWorkerTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.directory = TemporaryDirectory()
        self.database = Path(self.directory.name) / "chat_archive.db"
        migrate(self.database)
        from plugins.private_memory.jobs import MemoryJobQueue

        self.queue = MemoryJobQueue(
            self.database, lease_seconds=10, max_attempts=3, backoff_base_seconds=1
        )

    async def asyncTearDown(self) -> None:
        self.directory.cleanup()

    def enqueue(self, user_id: str, watermark: int) -> int:
        return self.queue.enqueue(
            job_type="private_summary",
            conversation_kind="private",
            user_id=user_id,
            group_id=None,
            input_through_id=watermark,
            expected_version=0,
        )

    async def test_disabled_switch_does_not_claim_or_invoke_callback(self) -> None:
        from plugins.private_memory.jobs import MemoryJobWorker

        self.enqueue("200", 1)
        callback = AsyncMock()
        worker = MemoryJobWorker(
            self.queue, callback, allowed_job_types=lambda: frozenset(),
            concurrency=2, poll_interval=0.01
        )
        task = asyncio.create_task(worker.run(), name="test-memory-worker")
        await asyncio.sleep(0.03)
        worker.stop_intake()
        await asyncio.wait_for(task, timeout=1)
        callback.assert_not_awaited()
        self.assertEqual("pending", self.queue.get(1).status)

    async def test_same_user_is_ordered_while_different_users_run_concurrently(self) -> None:
        from plugins.private_memory.jobs import MemoryJobWorker

        self.enqueue("200", 1)
        self.enqueue("200", 2)
        self.enqueue("201", 1)
        active_users: set[str] = set()
        simultaneous = asyncio.Event()
        calls: list[tuple[str, int]] = []

        async def callback(job):
            self.assertNotIn(job.scope.user_id, active_users)
            active_users.add(job.scope.user_id)
            calls.append((job.scope.user_id, job.input_through_id))
            if len(active_users) == 2:
                simultaneous.set()
            await asyncio.wait_for(simultaneous.wait(), timeout=1)
            await asyncio.sleep(0)
            active_users.remove(job.scope.user_id)

        worker = MemoryJobWorker(
            self.queue, callback,
            allowed_job_types=lambda: frozenset({"private_summary"}),
            concurrency=2, poll_interval=0.005
        )
        task = asyncio.create_task(worker.run())
        await asyncio.wait_for(simultaneous.wait(), timeout=1)
        while self.queue.get(2).status != "succeeded":
            await asyncio.sleep(0.005)
        worker.stop_intake()
        await asyncio.wait_for(task, timeout=1)

        self.assertEqual([("200", 1), ("200", 2)], [item for item in calls if item[0] == "200"])
        self.assertEqual("succeeded", self.queue.get(3).status)

    async def test_cancelled_worker_releases_owned_running_job_for_restart(self) -> None:
        from plugins.private_memory.jobs import MemoryJobWorker

        self.enqueue("200", 1)
        started = asyncio.Event()

        async def callback(job):
            started.set()
            await asyncio.Event().wait()

        worker = MemoryJobWorker(
            self.queue, callback,
            allowed_job_types=lambda: frozenset({"private_summary"}),
            concurrency=1, poll_interval=0.005
        )
        task = asyncio.create_task(worker.run())
        await asyncio.wait_for(started.wait(), timeout=1)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertEqual("pending", self.queue.get(1).status)
        self.assertIsNone(self.queue.get(1).lease_owner)

    async def test_claim_exception_is_logged_by_type_and_worker_recovers(self) -> None:
        from plugins.private_memory.jobs import MemoryJobWorker

        self.enqueue("200", 1)
        original_claim = self.queue.claim
        claim_calls = 0
        processed = asyncio.Event()

        def flaky_claim(**kwargs):
            nonlocal claim_calls
            claim_calls += 1
            if claim_calls == 1:
                raise sqlite3.OperationalError("sensitive database detail")
            return original_claim(**kwargs)

        async def callback(job):
            processed.set()

        self.queue.claim = flaky_claim  # type: ignore[method-assign]
        worker = MemoryJobWorker(
            self.queue, callback,
            allowed_job_types=lambda: frozenset({"private_summary"}),
            concurrency=1, poll_interval=0.005,
        )
        task = asyncio.create_task(worker.run())
        await asyncio.wait_for(processed.wait(), timeout=1)
        worker.stop_intake()
        await asyncio.wait_for(task, timeout=1)

        self.assertGreaterEqual(claim_calls, 2)
        self.assertEqual("succeeded", self.queue.get(1).status)

    async def test_finish_exception_leaves_job_running_and_worker_processes_other_scope(self) -> None:
        from plugins.private_memory.jobs import MemoryJobWorker

        self.enqueue("200", 1)
        self.enqueue("201", 1)
        original_finish = self.queue.finish
        finish_calls = 0
        both_processed = asyncio.Event()
        processed: list[str] = []

        def flaky_finish(*args, **kwargs):
            nonlocal finish_calls
            finish_calls += 1
            if finish_calls == 1:
                raise sqlite3.OperationalError("sensitive finish detail")
            return original_finish(*args, **kwargs)

        async def callback(job):
            processed.append(job.scope.user_id)
            if len(processed) == 2:
                both_processed.set()

        self.queue.finish = flaky_finish  # type: ignore[method-assign]
        worker = MemoryJobWorker(
            self.queue, callback,
            allowed_job_types=lambda: frozenset({"private_summary"}),
            concurrency=1, poll_interval=0.005,
        )
        task = asyncio.create_task(worker.run())
        await asyncio.wait_for(both_processed.wait(), timeout=1)
        worker.stop_intake()
        await asyncio.wait_for(task, timeout=1)

        self.assertEqual(["200", "201"], processed)
        self.assertEqual("running", self.queue.get(1).status)
        self.assertEqual("succeeded", self.queue.get(2).status)

    async def test_release_failure_during_cancellation_still_propagates_cancelled_error(self) -> None:
        from plugins.private_memory.jobs import MemoryJobWorker

        self.enqueue("200", 1)
        started = asyncio.Event()

        async def callback(job):
            started.set()
            await asyncio.Event().wait()

        worker = MemoryJobWorker(
            self.queue, callback,
            allowed_job_types=lambda: frozenset({"private_summary"}),
            concurrency=1, poll_interval=0.005,
        )
        task = asyncio.create_task(worker.run())
        await asyncio.wait_for(started.wait(), timeout=1)
        self.queue.release_owned = MagicMock(side_effect=sqlite3.OperationalError("detail"))
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task


class MemoryLifecycleTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.directory = TemporaryDirectory()
        self.root = Path(self.directory.name).resolve()
        from plugins.private_memory import lifecycle

        lifecycle._reset_for_tests()

    async def asyncTearDown(self) -> None:
        from plugins.private_memory import lifecycle

        await lifecycle._cancel_for_tests()
        lifecycle._reset_for_tests()
        self.directory.cleanup()

    def config(self, **changes: object) -> SimpleNamespace:
        values = {
            "chat_archive_path": self.root / "chat_archive.db",
            "private_memory_retention_days": 30,
            "private_memory_max_messages": 500,
            "private_memory_shutdown_timeout": 0.2,
        }
        values.update(changes)
        return SimpleNamespace(**values)

    def test_allowed_job_types_follow_independent_switch_combinations(self) -> None:
        from plugins.private_memory import lifecycle

        combinations = (
            (False, False, frozenset()),
            (True, False, frozenset({"private_summary", "private_facts"})),
            (False, True, frozenset({"relationship"})),
            (True, True, frozenset({"private_summary", "private_facts", "relationship"})),
        )
        for private_enabled, relationship_enabled, expected in combinations:
            with self.subTest(
                private_enabled=private_enabled,
                relationship_enabled=relationship_enabled,
            ):
                state = SimpleNamespace(
                    private_memory_enabled=private_enabled,
                    relationship_state_enabled=relationship_enabled,
                )
                features = SimpleNamespace(snapshot=lambda: state)
                with patch.object(lifecycle, "FEATURES", features):
                    self.assertEqual(expected, lifecycle._allowed_job_types())

    async def test_startup_migrates_fresh_database_without_backup(self) -> None:
        from plugins.private_memory import lifecycle

        driver = FakeDriver()
        config = self.config()
        with (
            patch.object(lifecycle, "get_driver", return_value=driver),
            patch.object(lifecycle, "CONFIG", config),
            patch.object(lifecycle, "BACKUP_DIR", self.root / "backups"),
            patch.object(lifecycle, "online_backup", wraps=lifecycle.online_backup) as backup,
        ):
            lifecycle.setup_lifecycle(processor=None)
            await driver.startup()
            await driver.shutdown()
        self.assertEqual(PRIVATE_MEMORY_SCHEMA_VERSION, schema_version(config.chat_archive_path))
        backup.assert_not_called()

    async def test_legacy_database_is_verified_backed_up_then_migrated(self) -> None:
        from plugins.private_memory import lifecycle

        database = self.root / "chat_archive.db"
        with closing(sqlite3.connect(database)) as connection:
            from plugins.chat_archive.db import SCHEMA
            connection.executescript(SCHEMA.replace('message_id TEXT NOT NULL,','message_id TEXT PRIMARY KEY,').replace(',\n    PRIMARY KEY(group_id,message_id)',''))
            connection.execute("INSERT INTO chat_messages VALUES('m',123,1,'7','{}','[]','kept',NULL,'old')")
            connection.commit()
        driver = FakeDriver()
        steps: list[str] = []
        real_backup = lifecycle.online_backup
        real_migrate = lifecycle.migrate

        def observed_backup(source, destination):
            steps.append("backup")
            return real_backup(source, destination)

        def observed_migrate(path):
            steps.append("migrate")
            return real_migrate(path)

        with (
            patch.object(lifecycle, "get_driver", return_value=driver),
            patch.object(lifecycle, "CONFIG", self.config()),
            patch.object(lifecycle, "BACKUP_DIR", self.root / "backups"),
            patch.object(lifecycle, "online_backup", side_effect=observed_backup),
            patch.object(lifecycle, "migrate", side_effect=observed_migrate),
        ):
            lifecycle.setup_lifecycle(processor=None)
            await driver.startup()
            await driver.shutdown()
        self.assertEqual(["backup", "migrate"], steps)
        private_backup_dir = self.root / "backups" / "private_memory"
        self.assertEqual(0o700, stat.S_IMODE(private_backup_dir.stat().st_mode))
        backups = list(private_backup_dir.glob("*.sqlite3"))
        self.assertEqual(1, len(backups))
        with closing(sqlite3.connect(backups[0])) as connection:
            self.assertEqual("kept", connection.execute("SELECT plaintext FROM chat_messages").fetchone()[0])

    async def test_current_schema_is_only_verified_not_migrated_or_backed_up(self) -> None:
        from plugins.private_memory import lifecycle

        config = self.config()
        migrate(config.chat_archive_path)
        driver = FakeDriver()
        with (
            patch.object(lifecycle, "get_driver", return_value=driver),
            patch.object(lifecycle, "CONFIG", config),
            patch.object(lifecycle, "migrate") as migration,
            patch.object(lifecycle, "online_backup") as backup,
            patch.object(lifecycle, "quick_check", return_value="ok") as check,
        ):
            lifecycle.setup_lifecycle(processor=None)
            await driver.startup()
            await driver.shutdown()
        migration.assert_not_called()
        backup.assert_not_called()
        check.assert_called_with(config.chat_archive_path)

    async def test_startup_recovers_expired_jobs_and_runs_retention(self) -> None:
        from plugins.private_memory import lifecycle

        config = self.config()
        migrate(config.chat_archive_path)
        driver = FakeDriver()
        queue = MagicMock()
        queue.recover_expired_leases.return_value = 1
        store = MagicMock()
        worker = MagicMock()
        worker.run = AsyncMock(return_value=None)
        with (
            patch.object(lifecycle, "get_driver", return_value=driver),
            patch.object(lifecycle, "CONFIG", config),
            patch.object(lifecycle, "BACKUP_DIR", self.root / "backups"),
            patch.object(lifecycle, "MemoryJobQueue", return_value=queue),
            patch("plugins.private_memory.store.PrivateMemoryStore", return_value=store),
            patch.object(lifecycle, "MemoryJobWorker", return_value=worker),
        ):
            lifecycle.setup_lifecycle(processor=None)
            await driver.startup()
            await driver.shutdown()
        queue.recover_expired_leases.assert_called_once()
        store.purge_expired.assert_called_once()

    async def test_retention_runs_at_startup_then_every_day_and_shutdown_cancels_loop(self) -> None:
        from plugins.private_memory import lifecycle

        class FakeClock:
            def __init__(self) -> None:
                self.current = NOW
                self.sleeps: list[float] = []
                self.waiter: asyncio.Future[None] | None = None

            def now(self) -> datetime:
                return self.current

            async def sleep(self, seconds: float) -> None:
                self.sleeps.append(seconds)
                self.waiter = asyncio.get_running_loop().create_future()
                await self.waiter

            def advance_one_day(self) -> None:
                self.current += timedelta(days=1)
                assert self.waiter is not None
                self.waiter.set_result(None)

        config = self.config()
        migrate(config.chat_archive_path)
        driver = FakeDriver()
        clock = FakeClock()
        queue = MagicMock()
        store = MagicMock()
        store.purge_expired.side_effect = (
            SimpleNamespace(checkpoint_complete=False),
            SimpleNamespace(checkpoint_complete=True),
        )
        worker = MagicMock()
        worker.run = AsyncMock(return_value=None)
        with (
            patch.object(lifecycle, "get_driver", return_value=driver),
            patch.object(lifecycle, "CONFIG", config),
            patch.object(lifecycle, "BACKUP_DIR", self.root / "backups"),
            patch.object(lifecycle, "MemoryJobQueue", return_value=queue),
            patch("plugins.private_memory.store.PrivateMemoryStore", return_value=store),
            patch.object(lifecycle, "MemoryJobWorker", return_value=worker),
            patch.object(lifecycle, "_utc_now", new=clock.now, create=True),
            patch.object(lifecycle, "_sleep", new=clock.sleep, create=True),
            patch.object(
                lifecycle, "prune_private_memory_backups", create=True
            ) as prune_backups,
            patch.object(lifecycle.logger, "warning") as warning,
        ):
            lifecycle.setup_lifecycle(processor=AsyncMock())
            await driver.startup()
            await asyncio.sleep(0)
            self.assertEqual([86_400], clock.sleeps)
            store.purge_expired.assert_called_once_with(
                now=NOW,
                retention_days=30,
                max_messages=500,
            )

            clock.advance_one_day()
            for _ in range(100):
                if prune_backups.call_count == 2:
                    break
                await asyncio.sleep(0.01)
            self.assertEqual(2, store.purge_expired.call_count)
            self.assertEqual(NOW + timedelta(days=1), store.purge_expired.call_args.kwargs["now"])
            warning.assert_called_once()
            self.assertIn("checkpoint", warning.call_args.args[0])
            self.assertEqual(2, prune_backups.call_count)
            self.assertEqual(
                self.root / "backups" / "private_memory",
                prune_backups.call_args.args[0],
            )
            self.assertEqual(30, prune_backups.call_args.kwargs["retention_days"])

            retention_task = lifecycle._retention_task
            await driver.shutdown()

        self.assertTrue(retention_task.done())
        self.assertIsNone(lifecycle._retention_task)

    async def test_daily_retention_prunes_both_managed_backup_names_only(self) -> None:
        from plugins.private_memory import lifecycle

        backup_dir = self.root / "backups" / "private_memory"
        backup_dir.mkdir(parents=True, mode=0o700)
        migration = backup_dir / (
            "chat_archive_before_private_memory_20260701T000000000000Z.sqlite3"
        )
        service = backup_dir / (
            "chat_archive-pre-private-memory-20260701T000000000000Z-42.sqlite3"
        )
        unrelated = backup_dir / "manual.sqlite3"
        symlink_target = self.root / "symlink-target.sqlite3"
        symlink = backup_dir / (
            "chat_archive_before_private_memory_20260702T000000000000Z.sqlite3"
        )
        hardlink_target = self.root / "hardlink-target.sqlite3"
        hardlink = backup_dir / (
            "chat_archive_before_private_memory_20260703T000000000000Z.sqlite3"
        )
        for path in (migration, service, unrelated, symlink_target, hardlink_target):
            path.write_bytes(b"fixture")
        symlink.symlink_to(symlink_target)
        os.link(hardlink_target, hardlink)
        old_time = (NOW - timedelta(days=30, seconds=1)).timestamp()
        for path in (migration, service, unrelated, hardlink_target):
            os.utime(path, (old_time, old_time))

        store = MagicMock()
        store.purge_expired.return_value = SimpleNamespace(checkpoint_complete=True)
        sleep_count = 0

        async def one_daily_tick(seconds: float) -> None:
            nonlocal sleep_count
            self.assertEqual(86_400, seconds)
            sleep_count += 1
            if sleep_count > 1:
                raise asyncio.CancelledError

        with (
            patch.object(lifecycle, "CONFIG", self.config()),
            patch.object(lifecycle, "BACKUP_DIR", self.root / "backups"),
            patch.object(lifecycle, "_utc_now", return_value=NOW),
            patch.object(lifecycle, "_sleep", new=one_daily_tick),
        ):
            with self.assertRaises(asyncio.CancelledError):
                await lifecycle._run_daily_retention(store)

        store.purge_expired.assert_called_once()
        self.assertFalse(migration.exists())
        self.assertFalse(service.exists())
        self.assertTrue(unrelated.exists())
        self.assertTrue(symlink.is_symlink())
        self.assertTrue(symlink_target.exists())
        self.assertTrue(hardlink.exists())
        self.assertTrue(hardlink_target.exists())

    async def test_shutdown_waits_for_fast_inflight_callback(self) -> None:
        from plugins.private_memory import lifecycle

        config = self.config(private_memory_shutdown_timeout=1.0)
        migrate(config.chat_archive_path)
        driver = FakeDriver()
        done = asyncio.Event()

        async def processor(job):
            await asyncio.sleep(0.02)
            done.set()

        with (
            patch.object(lifecycle, "get_driver", return_value=driver),
            patch.object(lifecycle, "CONFIG", config),
            patch.object(lifecycle, "BACKUP_DIR", self.root / "backups"),
            patch.object(
                lifecycle, "_allowed_job_types",
                return_value=frozenset({"private_summary"}),
            ),
        ):
            lifecycle.setup_lifecycle(processor=processor, poll_interval=0.005)
            await driver.startup()
            lifecycle._queue.enqueue(
                job_type="private_summary", conversation_kind="private", user_id="200",
                group_id=None, input_through_id=1, expected_version=0,
            )
            while lifecycle._queue.get(1).status != "running":
                await asyncio.sleep(0.002)
            await driver.shutdown()
        self.assertTrue(done.is_set())
        self.assertEqual("succeeded", lifecycle._queue.get(1).status)

    async def test_shutdown_timeout_cancels_and_releases_inflight_job(self) -> None:
        from plugins.private_memory import lifecycle

        config = self.config(private_memory_shutdown_timeout=0.02)
        migrate(config.chat_archive_path)
        driver = FakeDriver()
        cancelled = asyncio.Event()

        async def processor(job):
            try:
                await asyncio.Event().wait()
            finally:
                cancelled.set()

        with (
            patch.object(lifecycle, "get_driver", return_value=driver),
            patch.object(lifecycle, "CONFIG", config),
            patch.object(lifecycle, "BACKUP_DIR", self.root / "backups"),
            patch.object(
                lifecycle, "_allowed_job_types",
                return_value=frozenset({"private_summary"}),
            ),
        ):
            lifecycle.setup_lifecycle(processor=processor, poll_interval=0.005)
            await driver.startup()
            lifecycle._queue.enqueue(
                job_type="private_summary", conversation_kind="private", user_id="200",
                group_id=None, input_through_id=1, expected_version=0,
            )
            while lifecycle._queue.get(1).status != "running":
                await asyncio.sleep(0.002)
            await driver.shutdown()
        self.assertTrue(cancelled.is_set())
        self.assertEqual("pending", lifecycle._queue.get(1).status)
        self.assertIsNone(lifecycle._queue.get(1).lease_owner)

    async def test_setup_is_idempotent_per_driver(self) -> None:
        from plugins.private_memory import lifecycle

        driver = FakeDriver()
        with patch.object(lifecycle, "get_driver", return_value=driver):
            lifecycle.setup_lifecycle(processor=None)
            lifecycle.setup_lifecycle(processor=None)
        self.assertEqual(1, len(driver.startup_callbacks))
        self.assertEqual(1, len(driver.shutdown_callbacks))

    async def test_processor_can_be_injected_after_plugin_hook_registration(self) -> None:
        from plugins.private_memory import lifecycle

        config = self.config(private_memory_shutdown_timeout=1.0)
        migrate(config.chat_archive_path)
        driver = FakeDriver()
        processed = asyncio.Event()

        async def processor(job):
            processed.set()

        with (
            patch.object(lifecycle, "get_driver", return_value=driver),
            patch.object(lifecycle, "CONFIG", config),
            patch.object(
                lifecycle, "_allowed_job_types",
                return_value=frozenset({"private_summary"}),
            ),
        ):
            lifecycle.setup_lifecycle()
            lifecycle.set_processor(processor)
            await driver.startup()
            lifecycle._queue.enqueue(
                job_type="private_summary", conversation_kind="private", user_id="200",
                group_id=None, input_through_id=1, expected_version=0,
            )
            await asyncio.wait_for(processed.wait(), timeout=1)
            await driver.shutdown()

        self.assertEqual("succeeded", lifecycle._queue.get(1).status)

    async def test_startup_assembles_and_runs_default_private_memory_processor(self) -> None:
        from plugins.private_memory import lifecycle
        from plugins.private_memory import processor as processor_module
        from plugins.private_memory.processor import PrivateMemoryProcessor

        config = self.config(private_memory_shutdown_timeout=1.0)
        driver = FakeDriver()
        enabled = SimpleNamespace(
            private_memory_enabled=True,
            relationship_state_enabled=False,
        )
        features = SimpleNamespace(snapshot=lambda: enabled)
        with (
            patch.object(lifecycle, "get_driver", return_value=driver),
            patch.object(lifecycle, "CONFIG", config),
            patch.object(lifecycle, "FEATURES", features),
            patch.object(processor_module, "FEATURES", features),
            patch.object(lifecycle, "BACKUP_DIR", self.root / "backups"),
        ):
            lifecycle.setup_lifecycle(poll_interval=0.005)
            await driver.startup()
            self.assertIsInstance(lifecycle._worker.processor, PrivateMemoryProcessor)
            message_id = lifecycle._store.append_user_message(
                user_id="200", message_id="p1", text="只测试生产装配",
                event_time=1, source_kind="text",
            )
            lifecycle._worker.processor.summarize = AsyncMock(
                return_value="装配后的摘要"
            )
            job_id = lifecycle._queue.enqueue(
                job_type="private_summary", conversation_kind="private",
                user_id="200", group_id=None, input_through_id=message_id,
                expected_version=0,
            )
            async def wait_for_success() -> None:
                while lifecycle._queue.get(job_id).status != "succeeded":
                    await asyncio.sleep(0.002)

            await asyncio.wait_for(wait_for_success(), timeout=1)
            await driver.shutdown()

        self.assertEqual(
            "装配后的摘要",
            lifecycle._store.get_summary(user_id="200").summary_text,
        )


if __name__ == "__main__":
    unittest.main()
