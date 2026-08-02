from __future__ import annotations

import tempfile
import unittest
from dataclasses import replace
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import patch

from plugins.violation_record import db, policy_bridge, service
from plugins.violation_record.config import CONFIG
from plugins.violation_record.policy_schema import (
    V102_SCHEMA_VERSION,
    ensure_v102_schema,
)


class PolicyIntegrationTests(unittest.TestCase):
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
            deduction_policy_v102_enabled=True,
        )
        self.patches = (
            patch.object(db, "CONFIG", self.config),
            patch.object(service, "CONFIG", self.config),
            patch.object(policy_bridge, "CONFIG", self.config),
        )
        for item in self.patches:
            item.start()
        db.init_db()
        now = "2026-08-02 12:00:00"
        with db.connect() as conn:
            ensure_v102_schema(conn)
            conn.execute(
                """
                INSERT INTO admins(
                    qq_number, nickname, aliases, is_active, created_at, updated_at
                ) VALUES('90001', '管理员', '[]', 1, ?, ?)
                """,
                (now, now),
            )
            conn.execute(
                """
                INSERT INTO members(
                    qq_number, qq_nickname, aliases, created_at, updated_at
                ) VALUES('123456', '小明', '[]', ?, ?)
                """,
                (now, now),
            )
            self.member_id = int(
                conn.execute(
                    "SELECT id FROM members WHERE qq_number='123456'"
                ).fetchone()["id"]
            )
            self.admin_id = int(
                conn.execute(
                    "SELECT id FROM admins WHERE qq_number='90001'"
                ).fetchone()["id"]
            )
            conn.execute(
                """
                INSERT INTO member_group_states(
                    member_id, group_area, status, total_count, deduct_count,
                    current_count_cache, created_at, updated_at
                ) VALUES(?, '蜂巢', '正常', 0, 0, 0, ?, ?)
                """,
                (self.member_id, now, now),
            )

    def tearDown(self) -> None:
        for item in reversed(self.patches):
            item.stop()
        self.temp.cleanup()

    def _record_payload(self, when: str = "2026-08-02 10:00:00") -> dict:
        return {
            "member_id": self.member_id,
            "group_area": "蜂巢",
            "violation_time": when,
            "judgement": "刷屏",
            "action": "禁言10分钟",
            "handler_admin_id": self.admin_id,
            "recorder_admin_id": self.admin_id,
            "remark": "无",
            "is_countable": 1,
            "count_delta": 1,
            "is_test": 0,
        }

    def _confirm_record(self) -> str:
        service._set_pending(
            "123456789",
            "90001",
            "create_violation",
            {"record": self._record_payload(), "message_id": "record-m1"},
        )
        return service.confirm_pending(
            "123456789", "90001", "管理员", "confirm-m1"
        )

    def test_record_commits_before_policy_bridge_and_starts_cycle(self) -> None:
        reply = self._confirm_record()

        self.assertIn("已记录", reply)
        with db.connect() as conn:
            record_count = conn.execute(
                "SELECT COUNT(*) FROM violation_records"
            ).fetchone()[0]
            event_count = conn.execute(
                "SELECT COUNT(*) FROM v102_policy_events WHERE event_type='mute_recorded'"
            ).fetchone()[0]
            cycle = conn.execute(
                "SELECT * FROM v102_policy_cycles WHERE status='active'"
            ).fetchone()
        self.assertEqual(record_count, 1)
        self.assertEqual(event_count, 1)
        self.assertEqual(cycle["cycle_type"], "normal")

    def test_uncovered_legacy_scope_is_calibrated_before_query_and_record(self) -> None:
        old_time = "2026-07-01 09:00:00"
        cutover = "2026-08-02 09:00:00"
        with db.connect() as conn:
            conn.execute(
                """
                UPDATE member_group_states
                SET total_count=5, deduct_count=1, current_count_cache=4,
                    last_effective_violation_time=?, last_deduct_time=?
                WHERE member_id=? AND group_area='蜂巢'
                """,
                (old_time, old_time, self.member_id),
            )
            conn.execute(
                """
                INSERT INTO violation_records(
                    member_id, group_area, violation_time, judgement, action,
                    is_countable, count_delta, is_test, created_at, updated_at
                ) VALUES(?, '蜂巢', ?, '历史违规', '禁言10分钟',
                         1, 1, 0, ?, ?)
                """,
                (self.member_id, old_time, old_time, old_time),
            )
            conn.execute(
                """
                INSERT INTO v102_migration_checkpoints(
                    batch_id, schema_version, cutover_at,
                    cutover_record_watermark, source_sha256, backup_sha256,
                    status, created_at, updated_at
                ) VALUES('integration-cutover', ?, ?, 1,
                         'source', 'backup', 'applied', ?, ?)
                """,
                (V102_SCHEMA_VERSION, cutover, cutover, cutover),
            )

        result = service.query_member(
            {
                "group_area": "蜂巢",
                "target": {"qq_number": "123456", "qq_nickname": None},
                "query": {"recent_days": 14},
            },
            "90001",
            "管理员",
            False,
            "query-uncovered",
        )

        self.assertIsNotNone(result)
        with db.connect() as conn:
            state = conn.execute(
                """
                SELECT total_count, deduct_count, current_count_cache
                FROM member_group_states
                WHERE member_id=? AND group_area='蜂巢'
                """,
                (self.member_id,),
            ).fetchone()
            policy = conn.execute(
                """
                SELECT baseline_adjustment, active_cycle_id
                FROM v102_policy_state
                WHERE member_id=? AND group_area='蜂巢'
                """,
                (self.member_id,),
            ).fetchone()
        self.assertEqual(tuple(state), (5, 1, 4))
        self.assertEqual(policy["baseline_adjustment"], 4)
        self.assertIsNone(policy["active_cycle_id"])

        self._confirm_record()

        with db.connect() as conn:
            state = conn.execute(
                """
                SELECT total_count, deduct_count, current_count_cache
                FROM member_group_states
                WHERE member_id=? AND group_area='蜂巢'
                """,
                (self.member_id,),
            ).fetchone()
            cycle = conn.execute(
                "SELECT cycle_type FROM v102_policy_cycles WHERE status='active'"
            ).fetchone()
        self.assertEqual(tuple(state), (6, 1, 5))
        self.assertEqual(cycle["cycle_type"], "slow")

    def test_uncovered_legacy_scope_is_snapshotted_before_withdraw(self) -> None:
        old_time = "2026-07-01 09:00:00"
        with db.connect() as conn:
            conn.execute(
                """
                UPDATE member_group_states
                SET total_count=5, deduct_count=1, current_count_cache=4,
                    last_effective_violation_time=?, last_deduct_time=?
                WHERE member_id=? AND group_area='蜂巢'
                """,
                (old_time, old_time, self.member_id),
            )
            record_id = int(
                conn.execute(
                    """
                    INSERT INTO violation_records(
                        member_id, group_area, violation_time, judgement, action,
                        is_countable, count_delta, is_test, created_at, updated_at
                    ) VALUES(?, '蜂巢', ?, '历史违规', '禁言10分钟',
                             1, 1, 0, ?, ?)
                    """,
                    (self.member_id, old_time, old_time, old_time),
                ).lastrowid
            )
        service._set_pending(
            "123456789",
            "90001",
            "withdraw_latest",
            {
                "record_id": record_id,
                "member_id": self.member_id,
                "group_area": "蜂巢",
            },
        )

        reply = service.confirm_pending(
            "123456789", "90001", "管理员", "withdraw-uncovered"
        )

        self.assertIn("已撤回", reply)
        with db.connect() as conn:
            state = conn.execute(
                """
                SELECT total_count, deduct_count, current_count_cache
                FROM member_group_states
                WHERE member_id=? AND group_area='蜂巢'
                """,
                (self.member_id,),
            ).fetchone()
            policy = conn.execute(
                """
                SELECT baseline_adjustment, baseline_total_count,
                       baseline_raw_total, active_cycle_id
                FROM v102_policy_state
                WHERE member_id=? AND group_area='蜂巢'
                """,
                (self.member_id,),
            ).fetchone()
        self.assertEqual(tuple(state), (4, 1, 3))
        self.assertEqual(tuple(policy), (4, 5, 1, None))

    def test_uncovered_final_warning_scope_first_mute_creates_remove_pending(self) -> None:
        with db.connect() as conn:
            conn.execute(
                """
                UPDATE member_group_states
                SET status='最后警告', total_count=2, deduct_count=0,
                    current_count_cache=2,
                    last_final_warning_time='2026-07-01 00:00:00'
                WHERE member_id=? AND group_area='蜂巢'
                """,
                (self.member_id,),
            )

        reply = self._confirm_record()

        self.assertIn("已记录", reply)
        with db.connect() as conn:
            cycle = conn.execute(
                """
                SELECT c.* FROM v102_policy_cycles c
                JOIN v102_policy_state p ON p.active_cycle_id=c.id
                WHERE p.member_id=? AND p.group_area='蜂巢'
                """,
                (self.member_id,),
            ).fetchone()
            pending = conn.execute(
                """
                SELECT * FROM v102_pending_actions
                WHERE member_id=? AND group_area='蜂巢'
                  AND action_type='remove_member' AND status='pending'
                """,
                (self.member_id,),
            ).fetchone()
        self.assertEqual(cycle["cycle_type"], "final_warning")
        self.assertEqual(cycle["start_at"], "2026-07-01 00:00:00")
        self.assertEqual(cycle["status"], "pending_decision")
        self.assertIsNotNone(pending)

    def test_policy_bridge_failure_never_rolls_back_existing_record(self) -> None:
        with (
            patch.object(
                policy_bridge,
                "bridge_violation_record",
                side_effect=RuntimeError("fixture"),
            ),
            patch.object(service.logger, "exception") as logged,
        ):
            reply = self._confirm_record()

        self.assertIn("已记录", reply)
        with db.connect() as conn:
            self.assertEqual(
                conn.execute("SELECT COUNT(*) FROM violation_records").fetchone()[0],
                1,
            )
            self.assertEqual(
                conn.execute("SELECT COUNT(*) FROM v102_policy_events").fetchone()[0],
                0,
            )
        logged.assert_called_once()

    def test_withdraw_confirmation_replays_policy_after_business_commit(self) -> None:
        self._confirm_record()
        with db.connect() as conn:
            record_id = int(
                conn.execute("SELECT id FROM violation_records").fetchone()["id"]
            )
        service._set_pending(
            "123456789",
            "90001",
            "withdraw_latest",
            {
                "record_id": record_id,
                "member_id": self.member_id,
                "group_area": "蜂巢",
            },
        )

        reply = service.confirm_pending(
            "123456789", "90001", "管理员", "withdraw-m1"
        )

        self.assertIn("已撤回", reply)
        with db.connect() as conn:
            record = conn.execute(
                "SELECT * FROM violation_records WHERE id=?", (record_id,)
            ).fetchone()
            original = conn.execute(
                """
                SELECT * FROM v102_policy_events
                WHERE source_record_id=? AND event_type='mute_recorded'
                  AND replay_generation=0
                """,
                (record_id,),
            ).fetchone()
        self.assertEqual(record["is_withdrawn"], 1)
        self.assertEqual(original["is_effective"], 0)

    def test_final_warning_confirmation_starts_ninety_day_cycle(self) -> None:
        service._set_pending(
            "123456789",
            "90001",
            "consultation",
            {
                "member_id": self.member_id,
                "group_area": "蜂巢",
                "consultation_type": "最后警告",
                "consultation_time": "2026-08-02 11:00:00",
                "result": "通过",
                "status_after": "最后警告",
            },
        )

        service.confirm_pending(
            "123456789", "90001", "管理员", "final-warning-m1"
        )

        with db.connect() as conn:
            cycle = conn.execute(
                "SELECT * FROM v102_policy_cycles WHERE status='active'"
            ).fetchone()
        self.assertEqual(cycle["cycle_type"], "final_warning")
        self.assertEqual(cycle["due_at"], "2026-10-31 11:00:00")

    def test_explicit_record_caused_status_reverts_with_withdrawal(self) -> None:
        self._confirm_record()
        with db.connect() as conn:
            record_id = int(
                conn.execute("SELECT id FROM violation_records").fetchone()["id"]
            )
            mute_event_id = int(
                conn.execute(
                    """
                    SELECT id FROM v102_policy_events
                    WHERE source_record_id=? AND event_type='mute_recorded'
                      AND replay_generation=0
                    """,
                    (record_id,),
                ).fetchone()["id"]
            )
        preview = service.preview_consultation(
            {
                "intent": "final_warning",
                "group_area": "蜂巢",
                "target": {"qq_number": "123456", "qq_nickname": None},
                "status_update": {
                    "time": "2026-08-02 11:00:00",
                    "result": "通过",
                },
                "_reply_message_id": "record-m1",
            },
            "123456789",
            "90001",
            "管理员",
            "caused-status-preview",
        )
        self.assertIn(f"关联违规记录：#{record_id}", preview)

        service.confirm_pending(
            "123456789", "90001", "管理员", "caused-status"
        )

        with db.connect() as conn:
            status_event = conn.execute(
                """
                SELECT * FROM v102_policy_events
                WHERE event_type='status_changed' AND replay_generation=0
                """
            ).fetchone()
        self.assertEqual(status_event["caused_by_event_id"], mute_event_id)

        service._set_pending(
            "123456789",
            "90001",
            "withdraw_latest",
            {
                "record_id": record_id,
                "member_id": self.member_id,
                "group_area": "蜂巢",
            },
        )
        service.confirm_pending(
            "123456789", "90001", "管理员", "withdraw-caused-status"
        )

        with db.connect() as conn:
            state = conn.execute(
                """
                SELECT status, last_final_warning_time
                FROM member_group_states
                WHERE member_id=? AND group_area='蜂巢'
                """,
                (self.member_id,),
            ).fetchone()
            status_event = conn.execute(
                """
                SELECT * FROM v102_policy_events
                WHERE event_type='status_changed' AND replay_generation=0
                """
            ).fetchone()
        self.assertEqual(state["status"], "正常")
        self.assertIsNone(state["last_final_warning_time"])
        self.assertEqual(status_event["is_effective"], 0)

    def test_causal_status_confirmation_rejects_record_withdrawn_after_preview(self) -> None:
        self._confirm_record()
        with db.connect() as conn:
            record_id = int(
                conn.execute("SELECT id FROM violation_records").fetchone()["id"]
            )
            now = "2026-08-02 12:00:00"
            conn.execute(
                """
                INSERT INTO admins(
                    qq_number, nickname, aliases, is_active, created_at, updated_at
                ) VALUES('90002', '复核管理员', '[]', 1, ?, ?)
                """,
                (now, now),
            )

        preview = service.preview_consultation(
            {
                "intent": "final_warning",
                "group_area": "蜂巢",
                "target": {"qq_number": "123456", "qq_nickname": None},
                "status_update": {
                    "time": "2026-08-02 11:00:00",
                    "result": "通过",
                },
                "_reply_message_id": "record-m1",
            },
            "123456789",
            "90001",
            "管理员",
            "causal-race-preview",
        )
        self.assertIn(f"关联违规记录：#{record_id}", preview)

        service._set_pending(
            "123456789",
            "90002",
            "withdraw_latest",
            {
                "record_id": record_id,
                "member_id": self.member_id,
                "group_area": "蜂巢",
            },
        )
        withdrawn = service.confirm_pending(
            "123456789", "90002", "复核管理员", "causal-race-withdraw"
        )
        self.assertIn("已撤回", withdrawn)

        reply = service.confirm_pending(
            "123456789", "90001", "管理员", "causal-race-confirm"
        )

        self.assertIn("关联的违规记录已撤回", reply)
        with db.connect() as conn:
            state = conn.execute(
                """
                SELECT status, last_final_warning_time
                FROM member_group_states
                WHERE member_id=? AND group_area='蜂巢'
                """,
                (self.member_id,),
            ).fetchone()
            consultation_count = int(
                conn.execute("SELECT COUNT(*) FROM consultation_records").fetchone()[0]
            )
            status_job_count = int(
                conn.execute("SELECT COUNT(*) FROM v102_status_bridge_jobs").fetchone()[0]
            )
            rejection = conn.execute(
                """
                SELECT operation_type, remark FROM operation_logs
                WHERE message_id='causal-race-confirm'
                """
            ).fetchone()
        self.assertEqual(tuple(state), ("正常", None))
        self.assertEqual(consultation_count, 0)
        self.assertEqual(status_job_count, 0)
        self.assertEqual(rejection["operation_type"], "最后警告失败")
        self.assertEqual(rejection["remark"], "关联违规记录已撤回")

    def test_failed_status_bridge_is_durable_and_can_be_compensated(self) -> None:
        service._set_pending(
            "123456789",
            "90001",
            "consultation",
            {
                "member_id": self.member_id,
                "group_area": "蜂巢",
                "consultation_type": "最后警告",
                "consultation_time": "2026-08-02 11:00:00",
                "result": "通过",
                "status_after": "最后警告",
            },
        )
        with patch.object(
            policy_bridge,
            "process_status_change",
            side_effect=RuntimeError("fixture"),
        ):
            reply = service.confirm_pending(
                "123456789", "90001", "管理员", "final-warning-durable"
            )

        self.assertIn("已保存", reply)
        with db.connect() as conn:
            state = conn.execute(
                """
                SELECT status FROM member_group_states
                WHERE member_id=? AND group_area='蜂巢'
                """,
                (self.member_id,),
            ).fetchone()
            event_count = conn.execute(
                "SELECT COUNT(*) FROM v102_policy_events WHERE event_type='status_changed'"
            ).fetchone()[0]
            job = conn.execute(
                "SELECT * FROM v102_status_bridge_jobs"
            ).fetchone()
        self.assertEqual(state["status"], "最后警告")
        self.assertEqual(event_count, 0)
        self.assertEqual(job["job_status"], "failed")
        self.assertEqual(job["attempt_count"], 1)

        recovered = policy_bridge.process_status_bridge_jobs(
            as_of="2026-08-02 23:00:00"
        )

        self.assertEqual(recovered, 1)
        with db.connect() as conn:
            job = conn.execute(
                "SELECT * FROM v102_status_bridge_jobs"
            ).fetchone()
            event = conn.execute(
                """
                SELECT * FROM v102_policy_events
                WHERE event_type='status_changed' AND replay_generation=0
                """
            ).fetchone()
        self.assertEqual(job["job_status"], "applied")
        self.assertEqual(job["attempt_count"], 2)
        self.assertEqual(job["applied_event_id"], event["id"])
        self.assertEqual(event["effective_time"], "2026-08-02 11:00:00")
        self.assertEqual(event["ingest_time"], "2026-08-02 23:00:00")

    def test_feature_gate_disables_legacy_weekly_deduction(self) -> None:
        old = (datetime.now() - timedelta(days=8)).strftime("%Y-%m-%d %H:%M:%S")
        with db.connect() as conn:
            conn.execute(
                """
                UPDATE member_group_states
                SET total_count=1, current_count_cache=1,
                    last_effective_violation_time=?, last_deduct_time=?
                WHERE member_id=? AND group_area='蜂巢'
                """,
                (old, old, self.member_id),
            )

        self.assertEqual(service.automatic_maintenance(), [])
        with db.connect() as conn:
            deduct_count = conn.execute(
                """
                SELECT deduct_count FROM member_group_states
                WHERE member_id=? AND group_area='蜂巢'
                """,
                (self.member_id,),
            ).fetchone()["deduct_count"]
        self.assertEqual(deduct_count, 0)


if __name__ == "__main__":
    unittest.main()
