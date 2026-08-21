from __future__ import annotations

import asyncio
import sys
import tempfile
import types
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import MagicMock, patch


try:
    import nonebot  # noqa: F401
except ModuleNotFoundError:
    nonebot = types.ModuleType("nonebot")
    adapters = types.ModuleType("nonebot.adapters")
    onebot = types.ModuleType("nonebot.adapters.onebot")
    v11 = types.ModuleType("nonebot.adapters.onebot.v11")
    rule = types.ModuleType("nonebot.rule")

    class _Matcher:
        def handle(self):
            return lambda func: func

        async def finish(self, *args, **kwargs):
            return None

    class _Rule:
        def __init__(self, *args, **kwargs):
            pass

    class _Message(str):
        pass

    class _MessageSegment:
        @staticmethod
        def image(**kwargs):
            return kwargs

    class _Event:
        pass

    class _GroupMessageEvent(_Event):
        pass

    nonebot.logger = MagicMock()
    nonebot.on_message = lambda **kwargs: _Matcher()
    nonebot.get_bot = MagicMock()
    nonebot.get_bots = MagicMock(return_value={})
    nonebot.get_driver = MagicMock(side_effect=ValueError("not initialized"))
    v11.Bot = object
    v11.Event = _Event
    v11.GroupMessageEvent = _GroupMessageEvent
    v11.Message = _Message
    v11.MessageSegment = _MessageSegment
    rule.Rule = _Rule
    nonebot.adapters = adapters
    adapters.onebot = onebot
    onebot.v11 = v11
    sys.modules.update(
        {
            "nonebot": nonebot,
            "nonebot.adapters": adapters,
            "nonebot.adapters.onebot": onebot,
            "nonebot.adapters.onebot.v11": v11,
            "nonebot.rule": rule,
        }
    )

from plugins.violation_record import db, policy_bridge, scheduler
from plugins.violation_record.config import CONFIG
from plugins.violation_record.deduction_policy import (
    _insert_event,
    process_violation_record,
    sync_count_state,
)
from plugins.violation_record.policy_schema import (
    V102_SCHEMA_VERSION,
    ensure_v102_schema,
)


class FakeBot:
    def __init__(self, *, fail: bool = False, fail_forward: bool = False) -> None:
        self.fail = fail
        self.fail_forward = fail_forward
        self.self_id = "10000"
        self.sent: list[dict] = []
        self.forwarded: list[dict] = []

    async def send_group_msg(self, **kwargs):
        if self.fail:
            raise RuntimeError("offline")
        self.sent.append(kwargs)

    async def call_api(self, api: str, **kwargs):
        self.forwarded.append({"api": api, **kwargs})
        if self.fail_forward:
            raise RuntimeError("forward offline")


class FakeDriver:
    def __init__(self) -> None:
        self.startup = None
        self.shutdown = None

    def on_startup(self, func):
        self.startup = func
        return func

    def on_shutdown(self, func):
        self.shutdown = func
        return func


class PolicySchedulerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name)
        database_path = root / "business.db"
        self.config = replace(
            CONFIG,
            database_path=database_path,
            database_url=f"sqlite:///{database_path}",
            evidence_database_path=root / "evidence.db",
            evidence_root=root / "evidence",
            target_group_id=123456789,
            allowed_group_ids=(123456789,),
            deduction_policy_v102_enabled=True,
        )
        self.patches = (
            patch.object(db, "CONFIG", self.config),
            patch.object(policy_bridge, "CONFIG", self.config),
            patch.object(scheduler, "CONFIG", self.config),
        )
        for item in self.patches:
            item.start()
        db.init_db()
        now = "2026-08-02 12:00:00"
        with db.connect() as conn:
            ensure_v102_schema(conn)
            conn.execute(
                """
                INSERT INTO members(
                    qq_number, qq_nickname, aliases, created_at, updated_at
                ) VALUES('123456', '小明', '[]', ?, ?)
                """,
                (now, now),
            )
            self.member_id = int(conn.execute("SELECT id FROM members").fetchone()["id"])
            conn.execute(
                """
                INSERT INTO member_group_states(
                    member_id, group_area, status, total_count, deduct_count,
                    current_count_cache, created_at, updated_at
                ) VALUES(?, '蜂巢', '正常', 0, 0, 0, ?, ?)
                """,
                (self.member_id, now, now),
            )
            conn.execute(
                """
                INSERT INTO v102_migration_checkpoints(
                    batch_id, schema_version, cutover_at,
                    cutover_record_watermark, source_sha256, backup_sha256,
                    status, created_at, updated_at
                ) VALUES('test-batch', ?, ?, 0, 'source', 'backup', 'applied', ?, ?)
                """,
                (V102_SCHEMA_VERSION, now, now, now),
            )

    def tearDown(self) -> None:
        for item in reversed(self.patches):
            item.stop()
        self.temp.cleanup()

    def _record(self, action: str = "禁言10分钟") -> int:
        when = "2026-08-02 12:00:00"
        with db.connect() as conn:
            cursor = conn.execute(
                """
                INSERT INTO violation_records(
                    member_id, group_area, violation_time, judgement, action,
                    remark, is_countable, count_delta, is_test,
                    created_at, updated_at
                ) VALUES(?, '蜂巢', ?, '刷屏', ?, '无', 1, 1, 0, ?, ?)
                """,
                (self.member_id, when, action, when, when),
            )
            record_id = int(cursor.lastrowid)
            sync_count_state(conn, self.member_id, "蜂巢", updated_at=when)
            process_violation_record(conn, record_id, ingest_time=when)
        return record_id

    def _attempt_statuses(self, outbox_id: int) -> list[str]:
        with db.connect() as conn:
            table = conn.execute(
                """
                SELECT 1 FROM sqlite_master
                WHERE type='table' AND name='v102_notification_attempts'
                """
            ).fetchone()
            self.assertIsNotNone(table, "通知尝试历史表必须存在")
            return [
                row["status"]
                for row in conn.execute(
                    """
                    SELECT status FROM v102_notification_attempts
                    WHERE outbox_id=? ORDER BY attempt_number
                    """,
                    (outbox_id,),
                )
            ]

    def _baseline_cycle_events(self) -> tuple[int, int]:
        now = "2026-08-02 12:00:00"
        with db.connect() as conn:
            baseline_id, _ = _insert_event(
                conn,
                member_id=self.member_id,
                group_area="蜂巢",
                event_type="baseline_migrated",
                effective_time=now,
                event_priority=0,
                source_sequence=1,
                ingest_time=now,
                idempotency_key="migration:test:baseline",
            )
            cycle_id, _ = _insert_event(
                conn,
                member_id=self.member_id,
                group_area="蜂巢",
                event_type="cycle_started",
                effective_time=now,
                event_priority=100,
                source_sequence=1,
                ingest_time=now,
                idempotency_key="migration:test:cycle",
                caused_by_event_id=baseline_id,
                payload={"cycle_type": "normal"},
            )
        return baseline_id, cycle_id

    def test_baseline_migration_cycle_is_audit_only(self) -> None:
        self._baseline_cycle_events()

        with db.connect() as conn:
            queued = policy_bridge.queue_unannounced_events(conn)
            outbox_count = conn.execute(
                "SELECT COUNT(*) FROM v102_notification_outbox"
            ).fetchone()[0]

        self.assertEqual(queued, 0)
        self.assertEqual(outbox_count, 0)

    def test_legacy_baseline_cycle_outbox_is_cancelled_before_send(self) -> None:
        _, cycle_id = self._baseline_cycle_events()
        now = "2026-08-02 12:01:00"
        with db.connect() as conn:
            cursor = conn.execute(
                """
                INSERT INTO v102_notification_outbox(
                    event_id, member_id, group_area, message_type,
                    reminder_slot, message_text, scheduled_at,
                    created_at, updated_at
                ) VALUES(?, ?, '蜂巢', 'policy_event', '',
                         '迁移初始化通知', ?, ?, ?)
                """,
                (cycle_id, self.member_id, now, now, now),
            )
            outbox_id = int(cursor.lastrowid)
        bot = FakeBot()

        sent = asyncio.run(scheduler.deliver_policy_outbox(bot, as_of=now))

        self.assertEqual(sent, 0)
        self.assertEqual(bot.sent, [])
        with db.connect() as conn:
            status = conn.execute(
                "SELECT status FROM v102_notification_outbox WHERE id=?",
                (outbox_id,),
            ).fetchone()["status"]
        self.assertEqual(status, "cancelled")
        self.assertEqual(self._attempt_statuses(outbox_id), ["cancelled"])

    def test_maintenance_queues_automatic_events_without_napcat(self) -> None:
        self._record()

        stats = policy_bridge.run_policy_maintenance("2026-08-02 12:01:00")

        self.assertGreaterEqual(stats["queued_events"], 1)
        with db.connect() as conn:
            rows = conn.execute(
                "SELECT * FROM v102_notification_outbox ORDER BY id"
            ).fetchall()
        self.assertTrue(any(row["message_type"] == "policy_event" for row in rows))

    def test_compensation_recovers_withdrawal_after_bridge_failure(self) -> None:
        record_id = self._record()
        with db.connect() as conn:
            conn.execute(
                """
                UPDATE violation_records
                SET is_withdrawn=1, withdrawn_reason='管理员撤回', updated_at=?
                WHERE id=?
                """,
                ("2026-08-02 12:02:00", record_id),
            )

        with db.connect() as conn:
            compensated = policy_bridge.compensate_unprocessed_records(
                conn, ingest_time="2026-08-02 12:03:00"
            )

        self.assertEqual(compensated, 1)
        with db.connect() as conn:
            original = conn.execute(
                """
                SELECT * FROM v102_policy_events
                WHERE source_record_id=? AND event_type='mute_recorded'
                  AND replay_generation=0
                """,
                (record_id,),
            ).fetchone()
            withdrawn = conn.execute(
                """
                SELECT COUNT(*) FROM v102_policy_events
                WHERE source_record_id=? AND event_type='record_withdrawn'
                """,
                (record_id,),
            ).fetchone()[0]
        self.assertEqual(original["is_effective"], 0)
        self.assertEqual(withdrawn, 1)

    def test_hourly_pending_reminder_slots_are_unique(self) -> None:
        self._record("禁言一小时")

        first = policy_bridge.run_policy_maintenance("2026-08-02 12:01:00")
        duplicate = policy_bridge.run_policy_maintenance("2026-08-02 12:30:00")
        second = policy_bridge.run_policy_maintenance("2026-08-02 13:01:00")

        self.assertEqual(first["queued_reminders"], 1)
        self.assertEqual(duplicate["queued_reminders"], 0)
        self.assertEqual(second["queued_reminders"], 1)
        with db.connect() as conn:
            slots = [
                row["reminder_slot"]
                for row in conn.execute(
                    """
                    SELECT * FROM v102_notification_outbox
                    WHERE message_type='pending_reminder'
                    ORDER BY reminder_slot
                    """
                )
            ]
        self.assertEqual(slots, ["2026080212", "2026080213"])

    def test_outbox_delivery_marks_sent_and_uses_only_target_group(self) -> None:
        self._record()
        policy_bridge.run_policy_maintenance("2026-08-02 12:01:00")
        bot = FakeBot()

        sent = asyncio.run(
            scheduler.deliver_policy_outbox(bot, as_of="2026-08-02 12:01:00")
        )

        self.assertGreaterEqual(sent, 1)
        self.assertTrue(bot.sent)
        self.assertEqual(
            {item["group_id"] for item in bot.sent},
            {123456789},
        )
        with db.connect() as conn:
            statuses = {
                row["status"]
                for row in conn.execute("SELECT status FROM v102_notification_outbox")
            }
        self.assertEqual(statuses, {"sent"})

    def test_failed_delivery_stays_persisted_for_retry(self) -> None:
        self._record()
        policy_bridge.run_policy_maintenance("2026-08-02 12:01:00")

        sent = asyncio.run(
            scheduler.deliver_policy_outbox(
                FakeBot(fail=True), as_of="2026-08-02 12:01:00"
            )
        )

        self.assertEqual(sent, 0)
        with db.connect() as conn:
            row = conn.execute(
                "SELECT * FROM v102_notification_outbox ORDER BY id LIMIT 1"
            ).fetchone()
        self.assertEqual(row["status"], "failed")
        self.assertEqual(row["attempt_count"], 1)
        self.assertIn("RuntimeError", row["last_error"])

    def test_stale_sending_lease_is_reclaimed_and_history_is_preserved(self) -> None:
        self._record()
        policy_bridge.run_policy_maintenance("2026-08-02 12:01:00")

        claimed = scheduler._claim_policy_outbox("2026-08-02 12:01:00", 100)

        self.assertTrue(claimed)
        early_bot = FakeBot()
        self.assertEqual(
            asyncio.run(
                scheduler.deliver_policy_outbox(
                    early_bot, as_of="2026-08-02 12:05:59"
                )
            ),
            0,
        )
        self.assertEqual(early_bot.sent, [])

        individually_recovered = asyncio.run(
            scheduler.deliver_policy_outbox(
                FakeBot(), as_of="2026-08-02 12:06:00"
            )
        )

        self.assertEqual(individually_recovered, 0)
        with db.connect() as conn:
            row = conn.execute(
                "SELECT * FROM v102_notification_outbox ORDER BY id LIMIT 1"
            ).fetchone()
        self.assertEqual(row["status"], "failed")
        self.assertEqual(row["attempt_count"], 1)
        self.assertEqual(
            self._attempt_statuses(int(row["id"])),
            ["lease_expired"],
        )

        summary_bot = FakeBot()
        summarized = asyncio.run(
            scheduler.deliver_missed_policy_summary(
                summary_bot, as_of="2026-08-02 12:06:00"
            )
        )

        self.assertGreaterEqual(summarized, 1)
        self.assertEqual(len(summary_bot.forwarded), 1)
        with db.connect() as conn:
            row = conn.execute(
                "SELECT * FROM v102_notification_outbox ORDER BY id LIMIT 1"
            ).fetchone()
        self.assertEqual(row["status"], "sent")
        self.assertEqual(row["attempt_count"], 1)
        self.assertEqual(
            self._attempt_statuses(int(row["id"])),
            ["lease_expired"],
        )

    def test_failed_delivery_waits_for_summary_instead_of_individual_retry(self) -> None:
        self._record()
        policy_bridge.run_policy_maintenance("2026-08-02 12:01:00")
        failing_bot = FakeBot(fail=True)

        first = asyncio.run(
            scheduler.deliver_policy_outbox(
                failing_bot, as_of="2026-08-02 12:01:00"
            )
        )
        too_early = asyncio.run(
            scheduler.deliver_policy_outbox(
                failing_bot, as_of="2026-08-02 12:05:59"
            )
        )
        individual_retry = asyncio.run(
            scheduler.deliver_policy_outbox(
                FakeBot(), as_of="2026-08-02 12:06:00"
            )
        )

        self.assertEqual((first, too_early, individual_retry), (0, 0, 0))
        with db.connect() as conn:
            row = conn.execute(
                "SELECT * FROM v102_notification_outbox ORDER BY id LIMIT 1"
            ).fetchone()
        self.assertEqual(row["status"], "failed")
        self.assertEqual(row["attempt_count"], 1)
        self.assertEqual(
            self._attempt_statuses(int(row["id"])),
            ["failed"],
        )

    def test_business_off_records_pending_rows_without_sending(self) -> None:
        self._record("禁言一小时")
        bot = FakeBot()
        features = MagicMock()
        features.business_allowed.return_value = False

        with (
            patch.object(scheduler, "FEATURES", features),
            patch.object(scheduler, "get_bots", return_value={"bot": bot}),
        ):
            asyncio.run(
                scheduler.maintenance_tick(
                    now="2026-08-02 12:01:00", run_periodic_files=False
                )
            )

        self.assertEqual(bot.sent, [])
        self.assertEqual(bot.forwarded, [])
        with db.connect() as conn:
            rows = conn.execute(
                "SELECT * FROM v102_notification_outbox ORDER BY id"
            ).fetchall()
        self.assertTrue(rows)
        self.assertEqual({row["status"] for row in rows}, {"failed"})
        self.assertEqual(
            {row["last_error"] for row in rows}, {"business_disabled"}
        )
        self.assertEqual({row["attempt_count"] for row in rows}, {0})

    def test_no_bot_marks_pending_rows_as_offline(self) -> None:
        self._record("禁言一小时")
        features = MagicMock()
        features.business_allowed.return_value = True

        with (
            patch.object(scheduler, "FEATURES", features),
            patch.object(scheduler, "get_bots", return_value={}),
        ):
            asyncio.run(
                scheduler.maintenance_tick(
                    now="2026-08-02 12:01:00", run_periodic_files=False
                )
            )

        with db.connect() as conn:
            rows = conn.execute(
                "SELECT * FROM v102_notification_outbox ORDER BY id"
            ).fetchall()
        self.assertTrue(rows)
        self.assertEqual({row["status"] for row in rows}, {"failed"})
        self.assertEqual({row["last_error"] for row in rows}, {"bot_offline"})
        self.assertEqual({row["attempt_count"] for row in rows}, {0})

    def test_failed_rows_recover_as_one_forward_summary(self) -> None:
        self._record("禁言一小时")
        policy_bridge.run_policy_maintenance("2026-08-02 12:01:00")
        deferred = scheduler.defer_policy_outbox(
            "bot_offline", "2026-08-02 12:01:00"
        )
        bot = FakeBot()

        handled = asyncio.run(
            scheduler.deliver_missed_policy_summary(
                bot, as_of="2026-08-02 12:02:00"
            )
        )

        self.assertGreaterEqual(deferred, 2)
        self.assertEqual(handled, deferred)
        self.assertEqual(bot.sent, [])
        self.assertEqual(len(bot.forwarded), 1)
        forward = bot.forwarded[0]
        self.assertEqual(forward["api"], "send_group_forward_msg")
        self.assertEqual(forward["group_id"], 123456789)
        self.assertEqual(len(forward["messages"]), deferred + 1)
        overview = forward["messages"][0]["data"]["content"]
        self.assertIn("未发送业务提醒概览", overview)
        self.assertIn(
            "时间范围：2026-08-02 12:00:00 至 2026-08-02 12:01:00",
            overview,
        )
        self.assertIn(f"涉及提醒：{deferred} 条", overview)
        self.assertIn(f"业务关闭 0 / QQ离线 {deferred} / 发送失败 0", overview)
        with db.connect() as conn:
            rows = conn.execute(
                "SELECT * FROM v102_notification_outbox ORDER BY id"
            ).fetchall()
        self.assertEqual({row["status"] for row in rows}, {"sent"})
        self.assertEqual(
            {row["sent_at"] for row in rows}, {"2026-08-02 12:02:00"}
        )
        self.assertEqual({row["last_error"] for row in rows}, {None})

    def test_summary_failure_keeps_rows_failed(self) -> None:
        self._record()
        policy_bridge.run_policy_maintenance("2026-08-02 12:01:00")
        deferred = scheduler.defer_policy_outbox(
            "business_disabled", "2026-08-02 12:01:00"
        )
        bot = FakeBot(fail_forward=True)

        handled = asyncio.run(
            scheduler.deliver_missed_policy_summary(
                bot, as_of="2026-08-02 12:02:00"
            )
        )

        self.assertEqual(handled, 0)
        self.assertEqual(len(bot.forwarded), 1)
        self.assertEqual(bot.sent, [])
        with db.connect() as conn:
            rows = conn.execute(
                "SELECT * FROM v102_notification_outbox ORDER BY id"
            ).fetchall()
        self.assertEqual(len(rows), deferred)
        self.assertEqual({row["status"] for row in rows}, {"failed"})
        self.assertEqual(
            {row["last_error"] for row in rows}, {"business_disabled"}
        )
        self.assertEqual({row["sent_at"] for row in rows}, {None})

    def test_successful_summary_marks_valid_rows_sent_and_deduplicates(self) -> None:
        self._record("禁言一小时")
        policy_bridge.run_policy_maintenance("2026-08-02 12:01:00")
        scheduler.defer_policy_outbox("bot_offline", "2026-08-02 12:01:00")
        with db.connect() as conn:
            reminder = conn.execute(
                """
                SELECT * FROM v102_notification_outbox
                WHERE message_type='pending_reminder'
                """
            ).fetchone()
            conn.execute(
                """
                UPDATE v102_pending_actions SET status='resolved', updated_at=?
                WHERE id=?
                """,
                ("2026-08-02 12:01:30", reminder["pending_action_id"]),
            )
        bot = FakeBot()

        first = asyncio.run(
            scheduler.deliver_missed_policy_summary(
                bot, as_of="2026-08-02 12:02:00"
            )
        )
        duplicate = asyncio.run(
            scheduler.deliver_missed_policy_summary(
                bot, as_of="2026-08-02 12:03:00"
            )
        )

        self.assertGreaterEqual(first, 1)
        self.assertEqual(duplicate, 0)
        self.assertEqual(len(bot.forwarded), 1)
        self.assertEqual(len(bot.forwarded[0]["messages"]), first + 1)
        with db.connect() as conn:
            rows = conn.execute(
                "SELECT message_type, status FROM v102_notification_outbox"
            ).fetchall()
        statuses = {row["message_type"]: row["status"] for row in rows}
        self.assertEqual(statuses["policy_event"], "sent")
        self.assertEqual(statuses["pending_reminder"], "cancelled")

    def test_claimed_policy_event_is_cancelled_if_event_becomes_ineffective(self) -> None:
        self._record()
        policy_bridge.run_policy_maintenance("2026-08-02 12:01:00")
        claimed = scheduler._claim_policy_outbox("2026-08-02 12:01:00", 100)
        row = next(item for item in claimed if item["message_type"] == "policy_event")
        with db.connect() as conn:
            conn.execute(
                "UPDATE v102_policy_events SET is_effective=0 WHERE id=?",
                (row["event_id"],),
            )
        bot = FakeBot()

        with patch.object(scheduler, "_claim_policy_outbox", return_value=[row]):
            sent = asyncio.run(
                scheduler.deliver_policy_outbox(
                    bot, as_of="2026-08-02 12:01:01"
                )
            )

        self.assertEqual(sent, 0)
        self.assertEqual(bot.sent, [])
        with db.connect() as conn:
            status = conn.execute(
                "SELECT status FROM v102_notification_outbox WHERE id=?",
                (row["id"],),
            ).fetchone()["status"]
        self.assertEqual(status, "cancelled")
        self.assertEqual(
            self._attempt_statuses(int(row["id"])),
            ["cancelled"],
        )

    def _assert_pending_reminder_is_suppressed(self, pending_status: str) -> None:
        self._record("禁言一小时")
        policy_bridge.run_policy_maintenance("2026-08-02 12:01:00")
        claimed = scheduler._claim_policy_outbox("2026-08-02 12:01:00", 100)
        row = next(
            item for item in claimed if item["message_type"] == "pending_reminder"
        )
        with db.connect() as conn:
            conn.execute(
                """
                UPDATE v102_pending_actions SET status=?, updated_at=?
                WHERE caused_by_event_id=?
                """,
                (pending_status, "2026-08-02 12:01:01", row["event_id"]),
            )
        bot = FakeBot()

        with patch.object(scheduler, "_claim_policy_outbox", return_value=[row]):
            sent = asyncio.run(
                scheduler.deliver_policy_outbox(
                    bot, as_of="2026-08-02 12:01:01"
                )
            )

        self.assertEqual(sent, 0)
        self.assertEqual(bot.sent, [])
        with db.connect() as conn:
            status = conn.execute(
                "SELECT status FROM v102_notification_outbox WHERE id=?",
                (row["id"],),
            ).fetchone()["status"]
        self.assertEqual(status, "cancelled")
        self.assertEqual(
            self._attempt_statuses(int(row["id"])),
            ["cancelled"],
        )

    def test_claimed_pending_reminder_is_cancelled_after_resolution(self) -> None:
        self._assert_pending_reminder_is_suppressed("resolved")

    def test_claimed_pending_reminder_is_cancelled_after_cancellation(self) -> None:
        self._assert_pending_reminder_is_suppressed("cancelled")

    def test_tick_runs_policy_maintenance_when_no_bot_is_connected(self) -> None:
        with (
            patch.object(scheduler, "get_bots", return_value={}),
            patch.object(
                scheduler.policy_bridge,
                "run_policy_maintenance",
                return_value={
                    "compensated": 0,
                    "settled": 0,
                    "queued_events": 0,
                    "queued_reminders": 0,
                },
            ) as maintenance,
        ):
            asyncio.run(
                scheduler.maintenance_tick(
                    now="2026-08-02 12:01:00",
                    run_periodic_files=False,
                )
            )
        maintenance.assert_called_once_with("2026-08-02 12:01:00")

    def test_scheduler_keeps_one_task_and_cancels_it_on_shutdown(self) -> None:
        driver = FakeDriver()

        async def forever():
            await asyncio.Event().wait()

        async def exercise() -> None:
            scheduler._maintenance_task = None
            with (
                patch.object(scheduler, "get_driver", return_value=driver),
                patch.object(scheduler, "init_db"),
                patch.object(scheduler, "_maintenance_loop", side_effect=forever),
            ):
                scheduler.setup_scheduler()
                await driver.startup()
                first = scheduler._maintenance_task
                await driver.startup()
                self.assertIs(scheduler._maintenance_task, first)
                await driver.shutdown()
                self.assertIsNone(scheduler._maintenance_task)
                self.assertTrue(first.cancelled())

        asyncio.run(exercise())

    def test_scheduler_refuses_enabled_policy_without_migration_checkpoint(self) -> None:
        with db.connect() as conn:
            conn.execute("DELETE FROM v102_migration_checkpoints")
        driver = FakeDriver()

        async def exercise() -> None:
            scheduler._maintenance_task = None
            with (
                patch.object(scheduler, "get_driver", return_value=driver),
                patch.object(scheduler, "init_db"),
            ):
                scheduler.setup_scheduler()
                with self.assertRaisesRegex(RuntimeError, "checkpoint"):
                    await driver.startup()
                self.assertIsNone(scheduler._maintenance_task)

        asyncio.run(exercise())


if __name__ == "__main__":
    unittest.main()
