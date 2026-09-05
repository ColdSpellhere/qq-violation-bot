from __future__ import annotations

import sqlite3
import unittest

from plugins.violation_record.deduction_policy import (
    Severity,
    classify_severity,
    clear_manual_stop,
    effective_total,
    parse_mute_seconds,
    process_violation_record,
    process_status_change,
    renew_manual_stop,
    replay_member_group,
    settle_due_cycles,
    start_manual_stop,
    sync_count_state,
    withdraw_violation_record,
)
from plugins.violation_record.policy_schema import ensure_v102_schema


NOW = "2026-08-02 12:00:00"


class SeverityTests(unittest.TestCase):
    def test_mute_duration_distinguishes_light_severe_and_unknown(self) -> None:
        self.assertEqual(parse_mute_seconds("禁言10分钟"), 600)
        self.assertEqual(parse_mute_seconds("禁言一小时"), 3600)
        self.assertEqual(classify_severity("警告"), Severity.NONE)
        self.assertEqual(classify_severity("禁言"), Severity.UNKNOWN)

    def test_parser_accepts_arabic_and_chinese_minute_hour_expressions(self) -> None:
        self.assertEqual(parse_mute_seconds("禁言 90 分钟"), 5400)
        self.assertEqual(parse_mute_seconds("禁言两小时"), 7200)
        self.assertEqual(parse_mute_seconds("禁言十五分钟"), 900)
        self.assertEqual(classify_severity("禁言59分钟"), Severity.LIGHT)
        self.assertEqual(classify_severity("禁言1小时"), Severity.SEVERE)

    def test_blank_and_non_mute_actions_do_not_count_as_mutes(self) -> None:
        self.assertIsNone(parse_mute_seconds(None))
        self.assertIsNone(parse_mute_seconds("警告"))
        self.assertEqual(classify_severity(None), Severity.NONE)
        self.assertEqual(classify_severity("警告"), Severity.NONE)


class CountAdapterTests(unittest.TestCase):
    def setUp(self) -> None:
        self.conn = sqlite3.connect(":memory:")
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys = ON")
        self.conn.executescript(
            """
            CREATE TABLE members (
                id INTEGER PRIMARY KEY,
                qq_number TEXT UNIQUE NOT NULL
            );
            CREATE TABLE member_group_states (
                id INTEGER PRIMARY KEY,
                member_id INTEGER NOT NULL,
                group_area TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT '正常',
                locked INTEGER NOT NULL DEFAULT 0,
                total_count INTEGER NOT NULL DEFAULT 0,
                deduct_count INTEGER NOT NULL DEFAULT 0,
                current_count_cache INTEGER NOT NULL DEFAULT 0,
                last_effective_violation_time TEXT,
                last_deduct_time TEXT,
                last_final_warning_time TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                UNIQUE(member_id, group_area)
            );
            CREATE TABLE violation_records (
                id INTEGER PRIMARY KEY,
                member_id INTEGER NOT NULL,
                group_area TEXT NOT NULL,
                violation_time TEXT NOT NULL,
                is_withdrawn INTEGER NOT NULL DEFAULT 0,
                withdrawn_reason TEXT,
                is_test INTEGER NOT NULL DEFAULT 0,
                is_countable INTEGER NOT NULL DEFAULT 1,
                count_delta INTEGER NOT NULL DEFAULT 1
            );
            CREATE TABLE consultation_records (
                id INTEGER PRIMARY KEY,
                member_id INTEGER NOT NULL,
                group_area TEXT NOT NULL,
                consultation_time TEXT NOT NULL
            );
            INSERT INTO members(id, qq_number) VALUES(1, '10001');
            INSERT INTO member_group_states(
                id, member_id, group_area, total_count, deduct_count,
                current_count_cache, created_at, updated_at
            ) VALUES(1, 1, '蜂巢', 2, 2, 0, '2026-08-01 00:00:00', '2026-08-01 00:00:00');
            INSERT INTO violation_records(
                id, member_id, group_area, violation_time, count_delta
            ) VALUES
                (1, 1, '蜂巢', '2026-07-01 10:00:00', 1),
                (2, 1, '蜂巢', '2026-07-02 10:00:00', 1);
            """
        )
        ensure_v102_schema(self.conn)
        self.conn.execute(
            """
            INSERT INTO v102_policy_state(
                member_id, group_area, baseline_adjustment, created_at, updated_at
            ) VALUES(1, '蜂巢', 5, ?, ?)
            """,
            (NOW, NOW),
        )

    def tearDown(self) -> None:
        self.conn.close()

    def test_total_uses_baseline_adjustment(self) -> None:
        self.assertEqual(effective_total(self.conn, 1, "蜂巢"), 7)

    def test_sync_preserves_deduct_count_and_updates_cached_formula(self) -> None:
        state = sync_count_state(self.conn, 1, "蜂巢", updated_at=NOW)

        self.assertEqual(state["total_count"], 7)
        self.assertEqual(state["deduct_count"], 2)
        self.assertEqual(state["current_count_cache"], 5)
        self.assertEqual(state["last_effective_violation_time"], "2026-07-02 10:00:00")

    def test_sync_preserves_legacy_timer_when_baseline_has_no_raw_records(self) -> None:
        old_time = "2026-07-01 09:00:00"
        self.conn.execute("DELETE FROM violation_records")
        self.conn.execute(
            """
            UPDATE member_group_states
            SET total_count=5, deduct_count=2, current_count_cache=3,
                last_effective_violation_time=?
            WHERE id=1
            """,
            (old_time,),
        )

        state = sync_count_state(self.conn, 1, "蜂巢", updated_at=NOW)

        self.assertEqual(state["total_count"], 5)
        self.assertEqual(state["current_count_cache"], 3)
        self.assertEqual(state["last_effective_violation_time"], old_time)

    def test_withdrawn_test_and_non_countable_rows_are_excluded(self) -> None:
        self.conn.executemany(
            """
            INSERT INTO violation_records(
                id, member_id, group_area, violation_time, is_withdrawn,
                is_test, is_countable, count_delta
            ) VALUES(?, 1, '蜂巢', ?, ?, ?, ?, 9)
            """,
            (
                (3, "2026-07-03 10:00:00", 1, 0, 1),
                (4, "2026-07-04 10:00:00", 0, 1, 1),
                (5, "2026-07-05 10:00:00", 0, 0, 0),
            ),
        )
        self.assertEqual(effective_total(self.conn, 1, "蜂巢"), 7)


class PolicyTimelineTests(unittest.TestCase):
    def setUp(self) -> None:
        self.conn = sqlite3.connect(":memory:")
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys = ON")
        self.conn.executescript(
            """
            CREATE TABLE members (
                id INTEGER PRIMARY KEY,
                qq_number TEXT UNIQUE NOT NULL
            );
            CREATE TABLE member_group_states (
                id INTEGER PRIMARY KEY,
                member_id INTEGER NOT NULL,
                group_area TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT '正常',
                locked INTEGER NOT NULL DEFAULT 0,
                total_count INTEGER NOT NULL DEFAULT 0,
                deduct_count INTEGER NOT NULL DEFAULT 0,
                current_count_cache INTEGER NOT NULL DEFAULT 0,
                last_effective_violation_time TEXT,
                last_deduct_time TEXT,
                last_final_warning_time TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                UNIQUE(member_id, group_area)
            );
            CREATE TABLE violation_records (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                member_id INTEGER NOT NULL,
                group_area TEXT NOT NULL,
                violation_time TEXT NOT NULL,
                judgement TEXT NOT NULL DEFAULT '测试违规',
                action TEXT NOT NULL,
                is_withdrawn INTEGER NOT NULL DEFAULT 0,
                withdrawn_reason TEXT,
                is_test INTEGER NOT NULL DEFAULT 0,
                is_countable INTEGER NOT NULL DEFAULT 1,
                count_delta INTEGER NOT NULL DEFAULT 1,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            CREATE TABLE consultation_records (
                id INTEGER PRIMARY KEY,
                member_id INTEGER NOT NULL,
                group_area TEXT NOT NULL,
                consultation_time TEXT NOT NULL
            );
            INSERT INTO members(id, qq_number) VALUES(1, '10001');
            INSERT INTO member_group_states(
                id, member_id, group_area, total_count, deduct_count,
                current_count_cache, created_at, updated_at
            ) VALUES(1, 1, '蜂巢', 0, 0, 0, '2026-01-01 00:00:00', '2026-01-01 00:00:00');
            """
        )
        ensure_v102_schema(self.conn)

    def tearDown(self) -> None:
        self.conn.close()

    def _add_record(self, when: str, action: str = "禁言10分钟") -> int:
        countable = 0 if "警告" in action else 1
        cursor = self.conn.execute(
            """
            INSERT INTO violation_records(
                member_id, group_area, violation_time, action,
                is_countable, count_delta, created_at, updated_at
            ) VALUES(1, '蜂巢', ?, ?, ?, ?, ?, ?)
            """,
            (when, action, countable, countable, when, when),
        )
        record_id = int(cursor.lastrowid)
        sync_count_state(self.conn, 1, "蜂巢", updated_at=when)
        process_violation_record(self.conn, record_id, ingest_time=when)
        return record_id

    def _cycle(self) -> sqlite3.Row | None:
        return self.conn.execute(
            """
            SELECT c.*
            FROM v102_policy_cycles c
            JOIN v102_policy_state s ON s.active_cycle_id=c.id
            WHERE s.member_id=1 AND s.group_area='蜂巢'
            """
        ).fetchone()

    def _policy_state(self) -> sqlite3.Row:
        return self.conn.execute(
            "SELECT * FROM v102_policy_state WHERE member_id=1 AND group_area='蜂巢'"
        ).fetchone()

    def _business_state(self) -> sqlite3.Row:
        return self.conn.execute(
            "SELECT * FROM member_group_states WHERE member_id=1 AND group_area='蜂巢'"
        ).fetchone()

    def _set_baseline_adjustment(self, value: int) -> None:
        self.conn.execute(
            """
            INSERT INTO v102_policy_state(
                member_id, group_area, baseline_adjustment, created_at, updated_at
            ) VALUES(1, '蜂巢', ?, '2026-01-01 00:00:00', '2026-01-01 00:00:00')
            ON CONFLICT(member_id, group_area) DO UPDATE SET
                baseline_adjustment=excluded.baseline_adjustment
            """,
            (value,),
        )
        sync_count_state(
            self.conn, 1, "蜂巢", updated_at="2026-01-01 00:00:00"
        )


class NormalCycleTests(PolicyTimelineTests):
    def test_first_light_mute_starts_fourteen_day_cycle_and_settles_once(self) -> None:
        self._add_record("2026-01-01 00:00:00")

        cycle = self._cycle()
        self.assertEqual(cycle["cycle_type"], "normal")
        self.assertEqual(cycle["start_at"], "2026-01-01 00:00:00")
        self.assertEqual(cycle["due_at"], "2026-01-15 00:00:00")

        self.assertEqual(settle_due_cycles(self.conn, "2026-01-15 00:00:00"), 1)
        self.assertEqual(settle_due_cycles(self.conn, "2026-01-15 00:00:00"), 0)
        self.assertEqual(self._business_state()["deduct_count"], 1)
        self.assertEqual(self._business_state()["current_count_cache"], 0)
        self.assertEqual(self._policy_state()["v102_operation_count"], 1)
        self.assertEqual(self._policy_state()["no_cycle_reason"], "zero_count")
        self.assertIsNone(self._policy_state()["active_cycle_id"])

    def test_warning_has_no_policy_effect(self) -> None:
        self._add_record("2026-01-01 00:00:00", "警告")

        self.assertIsNone(self._cycle())
        self.assertEqual(
            self.conn.execute("SELECT COUNT(*) FROM v102_policy_events").fetchone()[0],
            0,
        )

    def test_violation_at_due_time_is_processed_before_settlement(self) -> None:
        self._add_record("2026-01-01 00:00:00")
        self._add_record("2026-01-15 00:00:00")
        self._add_record("2026-01-15 00:00:00")

        cycle = self._cycle()
        self.assertEqual(cycle["cycle_type"], "slow")
        self.assertEqual(cycle["due_at"], "2026-01-22 00:00:00")
        self.assertEqual(settle_due_cycles(self.conn, "2026-01-15 00:00:00"), 0)

    def test_pending_member_does_not_block_other_due_members(self) -> None:
        self._add_record("2026-01-01 00:00:00", "禁言一小时")
        self.conn.execute(
            "INSERT INTO members(id, qq_number) VALUES(2, '10002')"
        )
        self.conn.execute(
            """
            INSERT INTO member_group_states(
                id, member_id, group_area, total_count, deduct_count,
                current_count_cache, created_at, updated_at
            ) VALUES(2, 2, '蜂巢', 0, 0, 0,
                     '2026-01-01 00:00:00', '2026-01-01 00:00:00')
            """
        )
        cursor = self.conn.execute(
            """
            INSERT INTO violation_records(
                member_id, group_area, violation_time, action,
                is_countable, count_delta, created_at, updated_at
            ) VALUES(2, '蜂巢', '2026-01-01 00:00:00', '禁言10分钟',
                     1, 1, '2026-01-01 00:00:00', '2026-01-01 00:00:00')
            """
        )
        record_id = int(cursor.lastrowid)
        sync_count_state(
            self.conn, 2, "蜂巢", updated_at="2026-01-01 00:00:00"
        )
        process_violation_record(
            self.conn, record_id, ingest_time="2026-01-01 00:00:00"
        )

        self.assertEqual(settle_due_cycles(self.conn, "2026-01-15 00:00:00"), 1)
        first_cycle = self.conn.execute(
            """
            SELECT c.* FROM v102_policy_cycles c
            JOIN v102_policy_state s ON s.active_cycle_id=c.id
            WHERE s.member_id=1 AND s.group_area='蜂巢'
            """
        ).fetchone()
        second_state = self.conn.execute(
            """
            SELECT * FROM member_group_states
            WHERE member_id=2 AND group_area='蜂巢'
            """
        ).fetchone()
        self.assertEqual(first_cycle["status"], "pending_decision")
        self.assertEqual(second_state["deduct_count"], 1)


class SlowCycleTests(PolicyTimelineTests):
    def test_current_count_three_enters_first_progressive_slow_cycle(self) -> None:
        self._add_record("2026-01-01 00:00:00")
        self._add_record("2026-01-02 00:00:00")
        self._add_record("2026-01-03 00:00:00")

        cycle = self._cycle()
        state = self._policy_state()
        self.assertEqual(cycle["cycle_type"], "slow")
        self.assertEqual(cycle["start_at"], "2026-01-01 00:00:00")
        self.assertEqual(cycle["due_at"], "2026-01-22 00:00:00")
        self.assertEqual(state["policy_tag"], "slow")
        self.assertEqual(state["slow_level"], 1)

    def test_each_new_slow_phase_adds_seven_days_without_resetting_level(self) -> None:
        self._add_record("2026-01-01 00:00:00")
        self._add_record("2026-01-02 00:00:00")
        self._add_record("2026-01-03 00:00:00")
        settle_due_cycles(self.conn, "2026-01-22 00:00:00")

        self._add_record("2026-01-23 00:00:00")
        second = self._cycle()
        self.assertEqual(second["cycle_type"], "slow")
        self.assertEqual(second["start_at"], "2026-01-22 00:00:00")
        self.assertEqual(second["due_at"], "2026-02-19 00:00:00")
        self.assertEqual(self._policy_state()["slow_level"], 2)

        settle_due_cycles(self.conn, "2026-02-19 00:00:00")
        self._add_record("2026-02-20 00:00:00")
        third = self._cycle()
        self.assertEqual(third["cycle_type"], "slow")
        self.assertEqual(third["due_at"], "2026-03-26 00:00:00")
        self.assertEqual(self._policy_state()["slow_level"], 3)

    def test_second_slow_light_extends_once_and_third_creates_stop_suggestion(self) -> None:
        self.conn.execute(
            """
            INSERT INTO v102_policy_state(
                member_id, group_area, baseline_adjustment, created_at, updated_at
            ) VALUES(1, '蜂巢', 2, '2026-01-01 00:00:00', '2026-01-01 00:00:00')
            """
        )
        self._add_record("2026-01-01 00:00:00")
        self._add_record("2026-01-02 00:00:00")
        self._add_record("2026-01-03 00:00:00")

        cycle = self._cycle()
        self.assertEqual(cycle["due_at"], "2026-01-29 00:00:00")
        self.assertEqual(cycle["slow_light_count"], 2)

        self._add_record("2026-01-04 00:00:00")
        pending = self.conn.execute(
            """
            SELECT * FROM v102_pending_actions
            WHERE member_id=1 AND group_area='蜂巢' AND status='pending'
            """
        ).fetchone()
        self.assertEqual(pending["action_type"], "stop_suggestion")
        self.assertEqual(self._policy_state()["pending_action_type"], "stop_suggestion")

    def test_severe_mute_immediately_creates_stop_suggestion(self) -> None:
        self._add_record("2026-01-01 00:00:00", "禁言一小时")

        self.assertEqual(self._cycle()["cycle_type"], "normal")
        pending = self.conn.execute(
            "SELECT action_type FROM v102_pending_actions WHERE status='pending'"
        ).fetchone()
        self.assertEqual(pending["action_type"], "stop_suggestion")


class StopCycleTests(PolicyTimelineTests):
    def test_manual_stop_replaces_existing_cycle_and_uses_fixed_thirty_day_due(self) -> None:
        self._add_record("2026-01-01 00:00:00")
        start_manual_stop(
            self.conn,
            member_id=1,
            group_area="蜂巢",
            effective_at="2026-01-02 00:00:00",
            reason="管理确认减停",
            idempotency_key="manual-stop-1",
        )

        cycle = self._cycle()
        self.assertEqual(cycle["cycle_type"], "stop")
        self.assertEqual(cycle["start_at"], "2026-01-02 00:00:00")
        self.assertEqual(cycle["due_at"], "2026-02-01 00:00:00")
        self.assertEqual(cycle["fixed_sequence"], 1)
        self.assertEqual(self._policy_state()["policy_tag"], "stop")
        cancelled = self.conn.execute(
            "SELECT COUNT(*) FROM v102_policy_cycles WHERE status='cancelled'"
        ).fetchone()[0]
        self.assertEqual(cancelled, 1)

    def test_stop_due_waits_for_manager_and_never_auto_decides(self) -> None:
        self._add_record("2026-01-01 00:00:00")
        start_manual_stop(
            self.conn,
            member_id=1,
            group_area="蜂巢",
            effective_at="2026-01-02 00:00:00",
            reason="管理确认减停",
            idempotency_key="manual-stop-1",
        )

        self.assertEqual(settle_due_cycles(self.conn, "2026-02-01 00:00:00"), 1)
        self.assertEqual(self._cycle()["status"], "pending_decision")
        pending = self.conn.execute(
            "SELECT * FROM v102_pending_actions WHERE action_type='stop_decision'"
        ).fetchone()
        self.assertEqual(pending["status"], "pending")
        self.assertEqual(pending["due_at"], "2026-02-01 00:00:00")
        self.assertEqual(self._business_state()["deduct_count"], 0)

    def test_violation_after_due_settles_old_cycle_before_new_event(self) -> None:
        self._add_record("2026-01-01 00:00:00")

        self._add_record("2026-01-15 00:00:01", "禁言一小时")

        self.assertEqual(self._business_state()["deduct_count"], 1)
        self.assertEqual(self._policy_state()["v102_operation_count"], 1)
        cycle = self._cycle()
        self.assertEqual(cycle["cycle_type"], "normal")
        self.assertEqual(cycle["start_at"], "2026-01-15 00:00:01")
        self.assertEqual(cycle["due_at"], "2026-01-29 00:00:01")
        pending = self.conn.execute(
            """
            SELECT * FROM v102_pending_actions
            WHERE action_type='stop_suggestion' AND status='pending'
            """
        ).fetchone()
        self.assertIsNotNone(pending)

    def test_delayed_renewal_uses_previous_endpoint_not_decision_time(self) -> None:
        self._add_record("2026-01-01 00:00:00")
        start_manual_stop(
            self.conn,
            member_id=1,
            group_area="蜂巢",
            effective_at="2026-01-02 00:00:00",
            reason="管理确认减停",
            idempotency_key="manual-stop-1",
        )
        settle_due_cycles(self.conn, "2026-02-01 00:00:00")
        renew_manual_stop(
            self.conn,
            member_id=1,
            group_area="蜂巢",
            effective_at="2026-02-10 08:00:00",
            reason="继续观察",
            idempotency_key="manual-renew-1",
        )

        cycle = self._cycle()
        self.assertEqual(cycle["start_at"], "2026-02-01 00:00:00")
        self.assertEqual(cycle["due_at"], "2026-03-03 00:00:00")
        self.assertEqual(cycle["fixed_sequence"], 2)

    def test_waiting_violation_after_bad_stop_moves_to_renewed_cycle(self) -> None:
        start_manual_stop(
            self.conn,
            member_id=1,
            group_area="蜂巢",
            effective_at="2026-01-01 00:00:00",
            reason="管理确认减停",
            idempotency_key="manual-stop-1",
        )
        self._add_record("2026-01-10 00:00:00", "禁言一小时")
        settle_due_cycles(self.conn, "2026-01-31 00:00:00")
        old_cycle_id = int(self._cycle()["id"])

        self._add_record("2026-02-01 00:00:00", "禁言一小时")

        old_cycle = self.conn.execute(
            "SELECT * FROM v102_policy_cycles WHERE id=?", (old_cycle_id,)
        ).fetchone()
        self.assertEqual(old_cycle["severe_count"], 1)
        renew_manual_stop(
            self.conn,
            member_id=1,
            group_area="蜂巢",
            effective_at="2026-02-02 00:00:00",
            reason="继续观察",
            idempotency_key="manual-renew-1",
        )

        renewed = self._cycle()
        self.assertEqual(renewed["start_at"], "2026-01-31 00:00:00")
        self.assertEqual(renewed["due_at"], "2026-03-02 00:00:00")
        self.assertEqual(renewed["severe_count"], 1)
        settle_due_cycles(self.conn, renewed["due_at"])
        with self.assertRaisesRegex(ValueError, "评价不良"):
            clear_manual_stop(
                self.conn,
                member_id=1,
                group_area="蜂巢",
                effective_at="2026-03-03 00:00:00",
                reason="尝试解除",
                idempotency_key="manual-clear-1",
            )

    def test_good_stop_can_be_cleared_with_one_reduction(self) -> None:
        self._set_baseline_adjustment(2)
        start_manual_stop(
            self.conn,
            member_id=1,
            group_area="蜂巢",
            effective_at="2026-01-01 00:00:00",
            reason="管理确认减停",
            idempotency_key="manual-stop-1",
        )
        self._add_record("2026-01-10 00:00:00")
        settle_due_cycles(self.conn, "2026-01-31 00:00:00")

        clear_manual_stop(
            self.conn,
            member_id=1,
            group_area="蜂巢",
            effective_at="2026-02-02 00:00:00",
            reason="期内表现良好",
            idempotency_key="manual-clear-1",
        )

        self.assertEqual(self._business_state()["deduct_count"], 1)
        self.assertEqual(self._policy_state()["v102_operation_count"], 1)
        self.assertEqual(self._policy_state()["policy_tag"], "none")
        self.assertEqual(self._cycle()["cycle_type"], "normal")
        self.assertEqual(self._cycle()["start_at"], "2026-02-02 00:00:00")

    def test_bad_stop_rejects_clear(self) -> None:
        self._set_baseline_adjustment(1)
        start_manual_stop(
            self.conn,
            member_id=1,
            group_area="蜂巢",
            effective_at="2026-01-01 00:00:00",
            reason="管理确认减停",
            idempotency_key="manual-stop-1",
        )
        self._add_record("2026-01-10 00:00:00")
        self._add_record("2026-01-11 00:00:00")
        settle_due_cycles(self.conn, "2026-01-31 00:00:00")

        with self.assertRaisesRegex(ValueError, "评价不良"):
            clear_manual_stop(
                self.conn,
                member_id=1,
                group_area="蜂巢",
                effective_at="2026-02-02 00:00:00",
                reason="尝试解除",
                idempotency_key="manual-clear-1",
            )


class FinalWarningTests(PolicyTimelineTests):
    def test_status_change_after_due_settles_previous_cycle_first(self) -> None:
        self._add_record("2026-01-01 00:00:00")

        process_status_change(
            self.conn,
            member_id=1,
            group_area="蜂巢",
            status="已质询",
            effective_at="2026-01-15 00:00:01",
            idempotency_key="status-consulted-after-due",
        )

        self.assertEqual(self._business_state()["deduct_count"], 1)
        self.assertEqual(self._policy_state()["v102_operation_count"], 1)
        cycle = self._cycle()
        self.assertEqual(cycle["cycle_type"], "slow")
        self.assertEqual(cycle["start_at"], "2026-01-15 00:00:01")
        self.assertEqual(cycle["due_at"], "2026-02-05 00:00:01")

    def test_final_warning_recovers_after_ninety_days_and_requests_two(self) -> None:
        self._set_baseline_adjustment(3)
        process_status_change(
            self.conn,
            member_id=1,
            group_area="蜂巢",
            status="最后警告",
            effective_at="2026-01-01 00:00:00",
            idempotency_key="status-final-1",
        )

        self.assertEqual(self._cycle()["cycle_type"], "final_warning")
        self.assertEqual(self._cycle()["due_at"], "2026-04-01 00:00:00")
        self.assertEqual(settle_due_cycles(self.conn, "2026-04-01 00:00:00"), 1)
        self.assertEqual(self._business_state()["status"], "已质询")
        self.assertEqual(self._business_state()["deduct_count"], 2)
        self.assertEqual(self._policy_state()["v102_operation_count"], 1)
        self.assertEqual(self._policy_state()["policy_tag"], "none")
        self.assertEqual(self._cycle()["cycle_type"], "normal")
        self.assertEqual(self._cycle()["start_at"], "2026-04-01 00:00:00")

    def test_final_warning_recovery_does_not_downgrade_two_to_one(self) -> None:
        self._set_baseline_adjustment(1)
        process_status_change(
            self.conn,
            member_id=1,
            group_area="蜂巢",
            status="最后警告",
            effective_at="2026-01-01 00:00:00",
            idempotency_key="status-final-1",
        )
        settle_due_cycles(self.conn, "2026-04-01 00:00:00")

        self.assertEqual(self._business_state()["deduct_count"], 0)
        self.assertEqual(self._policy_state()["v102_operation_count"], 0)
        pending = self.conn.execute(
            """
            SELECT * FROM v102_pending_actions
            WHERE action_type='final_warning_recovery_review' AND status='pending'
            """
        ).fetchone()
        self.assertIsNotNone(pending)
        self.assertEqual(self._cycle()["cycle_type"], "normal")

    def test_zero_count_final_warning_recovery_keeps_review_pending(self) -> None:
        process_status_change(
            self.conn,
            member_id=1,
            group_area="蜂巢",
            status="最后警告",
            effective_at="2026-01-01 00:00:00",
            idempotency_key="status-final-1",
        )
        settle_due_cycles(self.conn, "2026-04-01 00:00:00")

        self.assertEqual(self._policy_state()["no_cycle_reason"], "zero_count")
        self.assertEqual(
            self._policy_state()["pending_action_type"],
            "final_warning_recovery_review",
        )

    def test_any_mute_in_final_warning_creates_remove_member_pending(self) -> None:
        self._set_baseline_adjustment(2)
        process_status_change(
            self.conn,
            member_id=1,
            group_area="蜂巢",
            status="最后警告",
            effective_at="2026-01-01 00:00:00",
            idempotency_key="status-final-1",
        )
        self._add_record("2026-04-01 00:00:00")

        self.assertEqual(settle_due_cycles(self.conn, "2026-04-01 00:00:00"), 0)
        self.assertEqual(self._business_state()["status"], "最后警告")
        self.assertEqual(self._cycle()["status"], "pending_decision")
        pending = self.conn.execute(
            """
            SELECT * FROM v102_pending_actions
            WHERE action_type='remove_member' AND status='pending'
            """
        ).fetchone()
        self.assertIsNotNone(pending)

    def test_manual_consulted_status_starts_slow_even_at_zero(self) -> None:
        process_status_change(
            self.conn,
            member_id=1,
            group_area="蜂巢",
            status="已质询",
            effective_at="2026-01-01 00:00:00",
            idempotency_key="status-consulted-1",
        )

        self.assertEqual(self._cycle()["cycle_type"], "slow")
        self.assertEqual(self._cycle()["due_at"], "2026-01-22 00:00:00")

    def test_terminal_status_stops_timer_but_preserves_stop_tag(self) -> None:
        start_manual_stop(
            self.conn,
            member_id=1,
            group_area="蜂巢",
            effective_at="2026-01-01 00:00:00",
            reason="管理确认减停",
            idempotency_key="manual-stop-before-terminal",
        )

        process_status_change(
            self.conn,
            member_id=1,
            group_area="蜂巢",
            status="已移出",
            effective_at="2026-01-02 00:00:00",
            idempotency_key="status-terminal-preserve-tag",
        )

        self.assertEqual(self._business_state()["status"], "已移出")
        self.assertEqual(self._policy_state()["policy_tag"], "stop")
        self.assertEqual(self._policy_state()["no_cycle_reason"], "terminal_status")
        self.assertIsNone(self._policy_state()["active_cycle_id"])


class ReplayTests(PolicyTimelineTests):
    def _add_backfill(
        self,
        violation_time: str,
        ingest_time: str,
        action: str = "禁言10分钟",
    ) -> int:
        cursor = self.conn.execute(
            """
            INSERT INTO violation_records(
                member_id, group_area, violation_time, action,
                is_countable, count_delta, created_at, updated_at
            ) VALUES(1, '蜂巢', ?, ?, 1, 1, ?, ?)
            """,
            (violation_time, action, ingest_time, ingest_time),
        )
        record_id = int(cursor.lastrowid)
        sync_count_state(self.conn, 1, "蜂巢", updated_at=ingest_time)
        process_violation_record(self.conn, record_id, ingest_time=ingest_time)
        return record_id

    def test_withdrawal_reverses_settlement_and_operation_count(self) -> None:
        record_id = self._add_record("2026-01-01 00:00:00")
        settle_due_cycles(self.conn, "2026-01-15 00:00:00")

        withdraw_violation_record(
            self.conn,
            record_id,
            effective_at="2026-01-16 00:00:00",
            reason="误记录",
        )

        self.assertEqual(self._business_state()["total_count"], 0)
        self.assertEqual(self._business_state()["deduct_count"], 0)
        self.assertEqual(self._policy_state()["v102_operation_count"], 0)
        self.assertEqual(self._policy_state()["no_cycle_reason"], "zero_count")
        original = self.conn.execute(
            """
            SELECT * FROM v102_policy_events
            WHERE source_record_id=? AND replay_generation=0
              AND event_type='mute_recorded'
            """,
            (record_id,),
        ).fetchone()
        self.assertEqual(original["is_effective"], 0)
        self.assertIsNotNone(original["reversed_by_event_id"])

    def test_withdrawal_restores_original_timer_without_resetting_elapsed_time(self) -> None:
        self._add_record("2026-01-01 00:00:00")
        self._add_record("2026-01-02 00:00:00")
        record_id = self._add_record("2026-01-03 00:00:00")
        self.assertEqual(self._cycle()["cycle_type"], "slow")

        withdraw_violation_record(
            self.conn,
            record_id,
            effective_at="2026-01-10 00:00:00",
            reason="误记录",
        )

        self.assertEqual(self._business_state()["total_count"], 2)
        self.assertEqual(self._policy_state()["policy_tag"], "none")
        self.assertEqual(self._policy_state()["slow_level"], 0)
        self.assertEqual(self._cycle()["cycle_type"], "normal")
        self.assertEqual(self._cycle()["start_at"], "2026-01-01 00:00:00")
        self.assertEqual(self._cycle()["due_at"], "2026-01-15 00:00:00")

    def test_unrelated_manual_stop_survives_withdrawal(self) -> None:
        record_id = self._add_record("2026-01-01 00:00:00")
        start_manual_stop(
            self.conn,
            member_id=1,
            group_area="蜂巢",
            effective_at="2026-01-02 00:00:00",
            reason="独立管理决定",
            idempotency_key="manual-stop-independent",
        )

        withdraw_violation_record(
            self.conn,
            record_id,
            effective_at="2026-01-03 00:00:00",
            reason="误记录",
        )

        self.assertEqual(self._policy_state()["policy_tag"], "stop")
        self.assertEqual(self._cycle()["cycle_type"], "stop")
        self.assertEqual(self._cycle()["due_at"], "2026-02-01 00:00:00")

    def test_explicitly_caused_manual_stop_is_preserved_for_review_after_withdrawal(self) -> None:
        record_id = self._add_record("2026-01-01 00:00:00")
        source_event_id = self.conn.execute(
            """
            SELECT id FROM v102_policy_events
            WHERE source_record_id=? AND event_type='mute_recorded'
            """,
            (record_id,),
        ).fetchone()["id"]
        start_manual_stop(
            self.conn,
            member_id=1,
            group_area="蜂巢",
            effective_at="2026-01-02 00:00:00",
            reason="因误记录减停",
            idempotency_key="manual-stop-caused",
            caused_by_event_id=source_event_id,
        )

        withdraw_violation_record(
            self.conn,
            record_id,
            effective_at="2026-01-03 00:00:00",
            reason="误记录",
        )

        self.assertEqual(self._policy_state()["policy_tag"], "stop")
        self.assertEqual(self._cycle()["cycle_type"], "stop")
        self.assertEqual(self._policy_state()["pending_action_type"], "replay_review")
        self.assertEqual(self._business_state()["total_count"], 0)

    def test_repeated_replay_has_same_canonical_projection(self) -> None:
        self._add_record("2026-01-01 00:00:00")
        trigger = self.conn.execute(
            "SELECT id FROM v102_policy_events WHERE event_type='mute_recorded'"
        ).fetchone()["id"]

        replay_member_group(
            self.conn,
            1,
            "蜂巢",
            trigger_event_id=trigger,
            as_of="2026-01-10 00:00:00",
        )
        first = (
            self._business_state()["deduct_count"],
            self._policy_state()["policy_tag"],
            self._policy_state()["slow_level"],
            self._policy_state()["v102_operation_count"],
            self._cycle()["cycle_type"],
            self._cycle()["start_at"],
            self._cycle()["due_at"],
        )
        replay_member_group(
            self.conn,
            1,
            "蜂巢",
            trigger_event_id=trigger,
            as_of="2026-01-10 00:00:00",
        )
        second = (
            self._business_state()["deduct_count"],
            self._policy_state()["policy_tag"],
            self._policy_state()["slow_level"],
            self._policy_state()["v102_operation_count"],
            self._cycle()["cycle_type"],
            self._cycle()["start_at"],
            self._cycle()["due_at"],
        )
        self.assertEqual(first, second)

    def test_replay_does_not_let_earlier_records_see_future_counts(self) -> None:
        self._add_record("2026-01-01 00:00:00")
        self._add_record("2026-01-02 00:00:00")
        self._add_record("2026-01-03 00:00:00")
        before = self._cycle()
        self.assertEqual(before["cycle_type"], "slow")
        self.assertEqual(before["due_at"], "2026-01-22 00:00:00")
        self.assertEqual(before["slow_light_count"], 0)
        self.assertEqual(before["slow_extended"], 0)
        trigger = self.conn.execute(
            """
            SELECT id FROM v102_policy_events
            WHERE source_record_id=1 AND event_type='mute_recorded'
              AND replay_generation=0
            """
        ).fetchone()["id"]

        replay_member_group(
            self.conn,
            1,
            "蜂巢",
            trigger_event_id=trigger,
            as_of="2026-01-10 00:00:00",
        )

        after = self._cycle()
        self.assertEqual(after["cycle_type"], "slow")
        self.assertEqual(after["due_at"], "2026-01-22 00:00:00")
        self.assertEqual(after["slow_light_count"], 0)
        self.assertEqual(after["slow_extended"], 0)

    def test_recent_cycle_backfill_recomputes_settlement_and_following_cycle(self) -> None:
        self._add_record("2026-01-01 00:00:00")
        settle_due_cycles(self.conn, "2026-01-15 00:00:00")

        self._add_backfill(
            "2026-01-10 00:00:00",
            "2026-01-20 00:00:00",
        )

        self.assertEqual(self._business_state()["total_count"], 2)
        self.assertEqual(self._business_state()["deduct_count"], 1)
        self.assertEqual(self._policy_state()["v102_operation_count"], 1)
        self.assertEqual(self._cycle()["cycle_type"], "normal")
        self.assertEqual(self._cycle()["start_at"], "2026-01-15 00:00:00")
        self.assertEqual(self._cycle()["due_at"], "2026-01-29 00:00:00")

    def test_backfill_inside_just_closed_final_warning_replays_recovery(self) -> None:
        self._set_baseline_adjustment(3)
        process_status_change(
            self.conn,
            member_id=1,
            group_area="蜂巢",
            status="最后警告",
            effective_at="2026-01-01 00:00:00",
            idempotency_key="status-final-backfill",
        )
        settle_due_cycles(self.conn, "2026-04-01 00:00:00")

        self._add_backfill(
            "2026-03-01 00:00:00",
            "2026-04-10 00:00:00",
        )

        self.assertEqual(self._business_state()["status"], "最后警告")
        self.assertEqual(self._business_state()["deduct_count"], 0)
        self.assertEqual(self._policy_state()["v102_operation_count"], 0)
        self.assertEqual(self._cycle()["cycle_type"], "final_warning")
        self.assertEqual(self._cycle()["status"], "pending_decision")
        pending = self.conn.execute(
            """
            SELECT * FROM v102_pending_actions
            WHERE action_type='remove_member' AND status='pending'
            """
        ).fetchone()
        self.assertIsNotNone(pending)

    def test_backdated_status_replays_later_existing_violation(self) -> None:
        self._set_baseline_adjustment(2)
        self._add_record("2026-01-10 00:00:00")

        process_status_change(
            self.conn,
            member_id=1,
            group_area="蜂巢",
            status="最后警告",
            effective_at="2026-01-01 00:00:00",
            ingest_time="2026-01-20 00:00:00",
            idempotency_key="status-final-backdated",
        )

        status_event = self.conn.execute(
            """
            SELECT * FROM v102_policy_events
            WHERE event_type='status_changed' AND replay_generation=0
            """
        ).fetchone()
        self.assertEqual(status_event["effective_time"], "2026-01-01 00:00:00")
        self.assertEqual(status_event["ingest_time"], "2026-01-20 00:00:00")
        self.assertEqual(self._business_state()["status"], "最后警告")
        self.assertEqual(self._cycle()["cycle_type"], "final_warning")
        self.assertEqual(self._cycle()["status"], "pending_decision")
        pending = self.conn.execute(
            """
            SELECT * FROM v102_pending_actions
            WHERE action_type='remove_member' AND status='pending'
            """
        ).fetchone()
        self.assertIsNotNone(pending)

    def test_old_severe_backfill_only_changes_count_and_does_not_create_suggestion(self) -> None:
        self._set_baseline_adjustment(2)
        self._add_record("2026-01-01 00:00:00")
        self.assertEqual(self._cycle()["cycle_type"], "slow")

        self._add_backfill(
            "2025-12-01 00:00:00",
            "2026-01-02 00:00:00",
            "禁言一小时",
        )

        self.assertEqual(self._business_state()["current_count_cache"], 4)
        self.assertEqual(self._cycle()["severe_count"], 0)
        suggestion = self.conn.execute(
            """
            SELECT COUNT(*) FROM v102_pending_actions
            WHERE action_type='stop_suggestion' AND status='pending'
            """
        ).fetchone()[0]
        self.assertEqual(suggestion, 0)


if __name__ == "__main__":
    unittest.main()
