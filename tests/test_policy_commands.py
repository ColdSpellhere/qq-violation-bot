from __future__ import annotations

import json
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from plugins.violation_record import db, policy_commands, service
from plugins.violation_record.config import CONFIG
from plugins.violation_record.deduction_policy import (
    process_violation_record,
    sync_count_state,
    withdraw_violation_record,
)
from plugins.violation_record.policy_commands import (
    PolicyCommandError,
    parse_policy_command,
    preview_policy_command,
    query_policy_command,
)
from plugins.violation_record.policy_schema import ensure_v102_schema
from plugins.violation_record.reply_models import StructuredReply


class PolicyCommandParserTests(unittest.TestCase):
    def test_non_policy_text_returns_none_for_existing_nlp_path(self) -> None:
        self.assertIsNone(parse_policy_command("查蜂巢123456"))
        self.assertIsNone(parse_policy_command("蜂巢小明违规，禁言10分钟"))

    def test_write_commands_require_area_numeric_qq_and_reason(self) -> None:
        command = parse_policy_command("减停 蜂巢 123456 多次违规")
        self.assertEqual(command.name, "manual_stop")
        self.assertEqual(command.group_area, "蜂巢")
        self.assertEqual(command.qq_number, "123456")
        self.assertEqual(command.reason, "多次违规")

        for text in (
            "减停 蜂巢 123456",
            "清除减停 蜂巢 小明 表现良好",
            "续期减停 未知区 123456 继续观察",
        ):
            with self.subTest(text=text), self.assertRaises(PolicyCommandError):
                parse_policy_command(text)

    def test_all_fixed_query_commands_are_parsed_without_nlp(self) -> None:
        cases = {
            "查询减数状态 蜂巢 123456": "query_status",
            "查询减缓名单": "query_slow_list",
            "查询减停名单": "query_stop_list",
            "查询减停建议名单": "query_suggestion_list",
            "查询减数待办": "query_pending",
            "查询减数日志 蜂巢 123456": "query_logs",
        }
        for text, name in cases.items():
            with self.subTest(text=text):
                self.assertEqual(parse_policy_command(text).name, name)


class PolicyCommandServiceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name)
        database_path = root / "business.db"
        test_config = replace(
            CONFIG,
            database_path=database_path,
            database_url=f"sqlite:///{database_path}",
            evidence_database_path=root / "evidence.db",
            evidence_root=root / "evidence",
            deduction_policy_v102_enabled=True,
        )
        self.patches = (
            patch.object(db, "CONFIG", test_config),
            patch.object(service, "CONFIG", test_config),
            patch.object(policy_commands, "CONFIG", test_config),
        )
        for item in self.patches:
            item.start()
        db.init_db()
        with db.connect() as conn:
            ensure_v102_schema(conn)
            now = "2026-08-02 12:00:00"
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
            member_id = conn.execute(
                "SELECT id FROM members WHERE qq_number='123456'"
            ).fetchone()["id"]
            conn.execute(
                """
                INSERT INTO member_group_states(
                    member_id, group_area, status, total_count, deduct_count,
                    current_count_cache, created_at, updated_at
                ) VALUES(?, '蜂巢', '正常', 2, 0, 2, ?, ?)
                """,
                (member_id, now, now),
            )
            conn.execute(
                """
                INSERT INTO v102_policy_state(
                    member_id, group_area, baseline_adjustment,
                    baseline_deduct_count, baseline_status,
                    policy_tag, created_at, updated_at
                ) VALUES(?, '蜂巢', 2, 0, '正常', 'none', ?, ?)
                """,
                (member_id, now, now),
            )

    def tearDown(self) -> None:
        for item in reversed(self.patches):
            item.stop()
        self.temp.cleanup()

    def test_write_preview_uses_operator_isolated_existing_pending_slot(self) -> None:
        command = parse_policy_command("减停 蜂巢 123456 多次违规")
        reply = preview_policy_command(
            command,
            group_id="123456789",
            operator_qq="90001",
            operator_nickname="管理员",
            message_id="m1",
        )

        self.assertIn("小明（123456）", reply)
        self.assertIn("多次违规", reply)
        self.assertIn("请回复“确认”", reply)
        with db.connect() as conn:
            row = conn.execute(
                """
                SELECT * FROM pending_operations
                WHERE group_id='123456789' AND operator_qq='90001'
                """
            ).fetchone()
        self.assertEqual(row["operation_type"], "v102_manual_stop")

    def test_write_preview_rejects_final_warning_cycle(self) -> None:
        with db.connect() as conn:
            member_id = conn.execute(
                "SELECT id FROM members WHERE qq_number='123456'"
            ).fetchone()["id"]
            conn.execute(
                "UPDATE member_group_states SET status='最后警告' WHERE member_id=?",
                (member_id,),
            )
        reply = preview_policy_command(
            parse_policy_command("减停 蜂巢 123456 多次违规"),
            group_id="123456789",
            operator_qq="90001",
            operator_nickname="管理员",
            message_id="m1",
        )
        self.assertIn("不允许", reply)

    def test_existing_confirmation_executes_manual_stop(self) -> None:
        preview_policy_command(
            parse_policy_command("减停 蜂巢 123456 多次违规"),
            group_id="123456789",
            operator_qq="90001",
            operator_nickname="管理员",
            message_id="m1",
        )

        reply = service.confirm_pending(
            "123456789", "90001", "管理员", "confirm-message"
        )

        self.assertIn("已执行减停", reply)
        with db.connect() as conn:
            cycle = conn.execute(
                """
                SELECT c.* FROM v102_policy_cycles c
                JOIN v102_policy_state s ON s.active_cycle_id=c.id
                JOIN members m ON m.id=s.member_id
                WHERE m.qq_number='123456' AND s.group_area='蜂巢'
                """
            ).fetchone()
        self.assertEqual(cycle["cycle_type"], "stop")

    def test_policy_preview_cancellation_is_written_to_operation_log(self) -> None:
        preview_policy_command(
            parse_policy_command("减停 蜂巢 123456 多次违规"),
            group_id="123456789",
            operator_qq="90001",
            operator_nickname="管理员",
            message_id="m-cancel",
        )

        reply = service.cancel_pending("123456789", "90001")

        self.assertEqual(reply, "已取消。")
        with db.connect() as conn:
            log = conn.execute(
                "SELECT * FROM operation_logs ORDER BY id DESC LIMIT 1"
            ).fetchone()
        self.assertEqual(log["operation_type"], "减停取消")
        self.assertEqual(log["operator_qq"], "90001")
        self.assertEqual(log["target_member_id"], 1)
        self.assertIn("多次违规", log["remark"])
        queried = query_policy_command(
            parse_policy_command("查询减数日志 蜂巢 123456")
        )
        self.assertIsInstance(queried, StructuredReply)
        queried_text = "\n".join(item.text for item in queried.records)
        self.assertIn("人工操作#", queried_text)
        self.assertIn("管理员（90001）", queried_text)
        self.assertIn("事由=多次违规", queried_text)
        self.assertIn("操作前=", queried_text)
        self.assertIn("操作后=", queried_text)

    def test_confirmation_revalidates_context_and_logs_rejection(self) -> None:
        preview_policy_command(
            parse_policy_command("减停 蜂巢 123456 多次违规"),
            group_id="123456789",
            operator_qq="90001",
            operator_nickname="管理员",
            message_id="m-reject",
        )
        with db.connect() as conn:
            conn.execute(
                """
                UPDATE member_group_states SET status='最后警告'
                WHERE member_id=1 AND group_area='蜂巢'
                """
            )

        reply = service.confirm_pending(
            "123456789", "90001", "管理员", "confirm-reject"
        )

        self.assertIn("减停未执行", reply)
        with db.connect() as conn:
            log = conn.execute(
                "SELECT * FROM operation_logs ORDER BY id DESC LIMIT 1"
            ).fetchone()
            manual_events = conn.execute(
                """
                SELECT COUNT(*) FROM v102_policy_events
                WHERE event_type='manual_stop_started'
                """
            ).fetchone()[0]
        self.assertEqual(manual_events, 0)
        self.assertEqual(log["operation_type"], "减停失败")
        self.assertEqual(log["operator_qq"], "90001")
        self.assertIn("最后警告", log["after_json"])

    def test_expired_policy_confirmation_is_written_to_operation_log(self) -> None:
        preview_policy_command(
            parse_policy_command("减停 蜂巢 123456 多次违规"),
            group_id="123456789",
            operator_qq="90001",
            operator_nickname="管理员",
            message_id="m-expired",
        )
        with db.connect() as conn:
            conn.execute(
                """
                UPDATE pending_operations SET expires_at='2000-01-01 00:00:00'
                WHERE group_id='123456789' AND operator_qq='90001'
                """
            )

        reply = service.confirm_pending(
            "123456789", "90001", "管理员", "confirm-expired"
        )

        self.assertIn("已过期", reply)
        with db.connect() as conn:
            log = conn.execute(
                "SELECT * FROM operation_logs ORDER BY id DESC LIMIT 1"
            ).fetchone()
        self.assertEqual(log["operation_type"], "减停过期")
        self.assertEqual(log["operator_qq"], "90001")

    def test_confirmed_suggestion_keeps_source_violation_causality(self) -> None:
        with db.connect() as conn:
            member_id = int(
                conn.execute(
                    "SELECT id FROM members WHERE qq_number='123456'"
                ).fetchone()["id"]
            )
            cursor = conn.execute(
                """
                INSERT INTO violation_records(
                    member_id, group_area, violation_time, judgement, action,
                    remark, is_countable, count_delta, is_test,
                    created_at, updated_at
                ) VALUES(?, '蜂巢', '2026-08-02 11:00:00', '严重违规',
                         '禁言一小时', '无', 1, 1, 0,
                         '2026-08-02 11:00:00', '2026-08-02 11:00:00')
                """,
                (member_id,),
            )
            record_id = int(cursor.lastrowid)
            sync_count_state(
                conn, member_id, "蜂巢", updated_at="2026-08-02 11:00:00"
            )
            outcome = process_violation_record(
                conn, record_id, ingest_time="2026-08-02 11:00:00"
            )
            source_event_id = int(outcome.event_id)

        preview_policy_command(
            parse_policy_command("减停 蜂巢 123456 严重违规"),
            group_id="123456789",
            operator_qq="90001",
            operator_nickname="管理员",
            message_id="m-causal",
        )
        with db.connect() as conn:
            pending = conn.execute(
                """
                SELECT payload_json FROM pending_operations
                WHERE group_id='123456789' AND operator_qq='90001'
                """
            ).fetchone()
        self.assertEqual(
            json.loads(pending["payload_json"])["caused_by_event_id"],
            source_event_id,
        )

        service.confirm_pending(
            "123456789", "90001", "管理员", "confirm-causal"
        )
        with db.connect() as conn:
            manual_event = conn.execute(
                """
                SELECT * FROM v102_policy_events
                WHERE event_type='manual_stop_started' AND replay_generation=0
                """
            ).fetchone()
            self.assertEqual(manual_event["caused_by_event_id"], source_event_id)
            withdraw_violation_record(
                conn,
                record_id,
                effective_at="2026-08-03 00:00:00",
                reason="误记录",
            )
            policy = conn.execute(
                """
                SELECT * FROM v102_policy_state
                WHERE member_id=? AND group_area='蜂巢'
                """,
                (member_id,),
            ).fetchone()
            manual_event = conn.execute(
                "SELECT * FROM v102_policy_events WHERE id=?",
                (manual_event["id"],),
            ).fetchone()
        self.assertEqual(manual_event["is_effective"], 0)
        self.assertEqual(policy["policy_tag"], "none")

    def test_lists_are_complete_and_deterministically_ordered(self) -> None:
        now = "2026-08-02 12:00:00"
        with db.connect() as conn:
            for index in range(30):
                qq = f"8{index:05d}"
                cursor = conn.execute(
                    """
                    INSERT INTO members(
                        qq_number, qq_nickname, aliases, created_at, updated_at
                    ) VALUES(?, ?, '[]', ?, ?)
                    """,
                    (qq, f"成员{index:02d}", now, now),
                )
                member_id = int(cursor.lastrowid)
                conn.execute(
                    """
                    INSERT INTO member_group_states(
                        member_id, group_area, status, total_count, deduct_count,
                        current_count_cache, created_at, updated_at
                    ) VALUES(?, '蜂巢', '正常', 3, 0, 3, ?, ?)
                    """,
                    (member_id, now, now),
                )
                conn.execute(
                    """
                    INSERT INTO v102_policy_state(
                        member_id, group_area, policy_tag, slow_level,
                        created_at, updated_at
                    ) VALUES(?, '蜂巢', 'slow', 1, ?, ?)
                    """,
                    (member_id, now, now),
                )

        reply = query_policy_command(parse_policy_command("查询减缓名单"))
        self.assertIsInstance(reply, StructuredReply)
        text = "\n".join(item.text for item in reply.records)
        self.assertEqual(text.count("成员"), 30)
        self.assertLess(text.index("成员00"), text.index("成员29"))


if __name__ == "__main__":
    unittest.main()
