import hashlib
import json
import sqlite3
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from plugins.memory_governance.commands import MemoryCommand, MemoryScope
from plugins.memory_governance.service import MemoryGovernanceService
from plugins.member_memory.store import load_profiles
from plugins.private_memory.schema import migrate


NOW = datetime(2026, 8, 23, 8, 0, tzinfo=timezone.utc)


class MemoryGovernanceServiceTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.db = Path(self.temporary.name) / "chat.db"
        migrate(self.db)
        self.service = MemoryGovernanceService(
            self.db,
            private_allowed_user_ids=("200",),
            persona_id="radish-cat",
        )

    def tearDown(self):
        self.temporary.cleanup()

    @staticmethod
    def private_scope() -> MemoryScope:
        return MemoryScope("private", "200")

    @staticmethod
    def group_scope() -> MemoryScope:
        return MemoryScope("group", "300", group_id=123)

    def preview(self, command: MemoryCommand, actor: str = "900"):
        return self.service.preview(command, actor=actor, now=NOW)

    def confirm(self, token: str, actor: str = "900", reason: str = "管理员核实"):
        return self.service.confirm(token, actor=actor, reason=reason, now=NOW)

    def test_preview_stores_only_hash_and_canonical_payload_for_ten_minutes(self):
        command = MemoryCommand("add_fact", scope=self.private_scope(), content="喜欢清淡口味")
        preview = self.preview(command)
        self.assertGreaterEqual(len(preview.token), 32)
        with sqlite3.connect(self.db) as connection:
            connection.row_factory = sqlite3.Row
            row = connection.execute("SELECT * FROM memory_pending_operations").fetchone()
        self.assertNotIn(preview.token, tuple(row))
        self.assertEqual(hashlib.sha256(preview.token.encode()).hexdigest(), row["confirmation_token_hash"])
        self.assertEqual("2026-08-23T08:10:00Z", row["expires_at"])
        self.assertEqual(json.dumps(json.loads(row["payload_json"]), ensure_ascii=False, sort_keys=True, separators=(",", ":")), row["payload_json"])

    def test_token_is_actor_bound_and_payload_tampering_is_rejected(self):
        preview = self.preview(MemoryCommand("add_fact", scope=self.private_scope(), content="喜欢清淡口味"))
        denied = self.confirm(preview.token, actor="901")
        self.assertFalse(denied.success)
        with sqlite3.connect(self.db) as connection:
            self.assertIsNone(connection.execute("SELECT consumed_at FROM memory_pending_operations").fetchone()[0])
            connection.execute("UPDATE memory_pending_operations SET payload_json='{}'")
        tampered = self.confirm(preview.token)
        self.assertFalse(tampered.success)
        with sqlite3.connect(self.db) as connection:
            self.assertEqual(0, connection.execute("SELECT count(*) FROM private_memory_facts").fetchone()[0])
            pending = connection.execute("SELECT consumed_at FROM memory_pending_operations").fetchone()[0]
            audit = connection.execute(
                "SELECT result,error_code FROM memory_governance_audit"
            ).fetchone()
        self.assertIsNotNone(pending)
        self.assertEqual(("failed", "payload_invalid"), audit)
        self.assertTrue(self.confirm(preview.token).already_consumed)

    def test_token_expires_and_confirmation_reason_is_mandatory(self):
        preview = self.preview(MemoryCommand("add_fact", scope=self.private_scope(), content="喜欢清淡口味"))
        self.assertFalse(self.service.confirm(preview.token, actor="900", reason=" ", now=NOW).success)
        expired = self.service.confirm(
            preview.token, actor="900", reason="核实", now=NOW + timedelta(minutes=10, microseconds=1)
        )
        self.assertFalse(expired.success)

    def test_add_fact_is_admin_confirmed_audited_and_once_only(self):
        preview = self.preview(MemoryCommand("add_fact", scope=self.private_scope(), content="喜欢清淡口味"))
        first = self.confirm(preview.token)
        second = self.confirm(preview.token)
        self.assertTrue(first.success)
        self.assertFalse(second.success)
        self.assertTrue(second.already_consumed)
        with sqlite3.connect(self.db) as connection:
            fact = connection.execute(
                "SELECT fact_text,trust_level,status,source_message_id FROM private_memory_facts"
            ).fetchone()
            audit = connection.execute(
                "SELECT operation_id,operator_user_id,target_kind,target_user_id,operation_type,before_hash,after_hash,reason,result,created_at FROM memory_governance_audit"
            ).fetchone()
        self.assertEqual(("喜欢清淡口味", "admin_confirmed", "active"), fact[:3])
        self.assertRegex(fact[3], r"^governance:[1-9][0-9]*$")
        self.assertEqual(("900", "private", "200", "add_fact", "管理员核实", "success"), (audit[1], audit[2], audit[3], audit[4], audit[7], audit[8]))
        self.assertEqual(64, len(audit[5]))
        self.assertEqual(64, len(audit[6]))

    def test_modify_creates_new_version_and_supersedes_old_private_fact(self):
        added = self.preview(MemoryCommand("add_fact", scope=self.private_scope(), content="旧事实"))
        self.assertTrue(self.confirm(added.token).success)
        with sqlite3.connect(self.db) as connection:
            old_id = connection.execute("SELECT id FROM private_memory_facts").fetchone()[0]
        modified = self.preview(MemoryCommand("modify_fact", fact_kind="private", memory_id=old_id, content="新事实"))
        self.assertTrue(self.confirm(modified.token).success)
        with sqlite3.connect(self.db) as connection:
            rows = connection.execute(
                "SELECT id,fact_text,status,supersedes_id,version,trust_level FROM private_memory_facts ORDER BY id"
            ).fetchall()
        self.assertEqual("superseded", rows[0][2])
        self.assertEqual(("新事实", "active", old_id, 2, "admin_confirmed"), rows[1][1:])

    def test_delete_is_soft_and_preserves_history(self):
        added = self.preview(MemoryCommand("add_fact", scope=self.private_scope(), content="待删除"))
        self.assertTrue(self.confirm(added.token).success)
        with sqlite3.connect(self.db) as connection:
            fact_id = connection.execute("SELECT id FROM private_memory_facts").fetchone()[0]
        deleted = self.preview(MemoryCommand("delete_fact", fact_kind="private", memory_id=fact_id))
        self.assertTrue(self.confirm(deleted.token).success)
        with sqlite3.connect(self.db) as connection:
            row = connection.execute("SELECT fact_text,status,deleted_at FROM private_memory_facts WHERE id=?", (fact_id,)).fetchone()
        self.assertEqual(("待删除", "deleted"), row[:2])
        self.assertIsNotNone(row[2])

    def test_group_fact_uses_scope_and_governance_source(self):
        with sqlite3.connect(self.db) as connection:
            connection.execute("INSERT INTO member_memories VALUES(123,'300','群友','[]','[]','old')")
        preview = self.preview(MemoryCommand("add_fact", scope=self.group_scope(), content="喜欢养花"))
        self.assertTrue(self.confirm(preview.token).success)
        with sqlite3.connect(self.db) as connection:
            row = connection.execute("SELECT group_id,user_id,trait,trust_level,status,evidence_message_id FROM member_memory_facts").fetchone()
        self.assertEqual((123, "300", "喜欢养花", "admin_confirmed", "active"), row[:5])
        self.assertRegex(row[5], r"^governance:[1-9][0-9]*$")

    def test_group_fact_for_new_profile_is_visible_to_normal_reads(self):
        preview = self.preview(MemoryCommand("add_fact", scope=self.group_scope(), content="喜欢养花"))
        self.assertTrue(self.confirm(preview.token).success)
        profile = load_profiles(self.db, group_id=123, user_ids=["300"])[0]
        self.assertEqual(["喜欢养花"], [fact.text for fact in profile.traits])

    def test_relationship_update_preserves_task4_watermark(self):
        initial = self.preview(MemoryCommand("update_relation", scope=self.private_scope(), content="初次熟悉"))
        self.assertTrue(self.confirm(initial.token).success)
        with sqlite3.connect(self.db) as connection:
            connection.execute("UPDATE relationship_states SET source_watermark=17")
        update = self.preview(MemoryCommand("update_relation", scope=self.private_scope(), content="交流自然"))
        self.assertTrue(self.confirm(update.token).success)
        with sqlite3.connect(self.db) as connection:
            row = connection.execute("SELECT state_text,source_message_id,source_watermark,version FROM relationship_states").fetchone()
        self.assertEqual(("交流自然", 17, 2), (row[0], row[2], row[3]))
        self.assertRegex(row[1], r"^governance:[1-9][0-9]*$")

    def test_clear_private_layers_is_exact_and_preview_contains_counts(self):
        with sqlite3.connect(self.db) as connection:
            connection.execute(
                "INSERT INTO private_chat_messages(user_id,message_id,direction,text,content_hash,event_time,created_at,expires_at,source_kind) VALUES('200','m1','user','秘密','hash',1,'old','future','text')"
            )
            connection.execute(
                "INSERT INTO private_conversation_summaries VALUES('200','摘要',1,1,1,1,'old','old')"
            )
            connection.execute(
                "INSERT INTO private_memory_facts(user_id,fact_text,normalized_text,source_message_id,source_quote,trust_level,status,version,created_at,updated_at) VALUES('200','长期事实','长期事实','governance:1','长期事实','admin_confirmed','active',1,'old','old')"
            )
            connection.execute(
                "INSERT INTO relationship_states(conversation_kind,group_id,user_id,persona_id,state_text,open_topics_json,source_message_id,source_watermark,version,created_at,updated_at) VALUES('private',NULL,'200','radish-cat','关系保留','[\"待续\"]','governance:1',0,1,'old','old')"
            )
            connection.execute(
                "INSERT INTO memory_jobs(job_type,conversation_kind,group_id,user_id,input_through_id,expected_version,status,next_run_at,created_at,updated_at) VALUES('private_summary','private',NULL,'200',1,0,'pending','now','old','old')"
            )
        preview = self.preview(MemoryCommand("clear_private", scope=self.private_scope()))
        self.assertIn("1", preview.preview_text)
        self.assertTrue(self.confirm(preview.token).success)
        with sqlite3.connect(self.db) as connection:
            message = connection.execute("SELECT text,purged_at FROM private_chat_messages").fetchone()
            summary = connection.execute("SELECT summary_text FROM private_conversation_summaries").fetchone()[0]
            relation = connection.execute("SELECT state_text,open_topics_json FROM relationship_states").fetchone()
            fact = connection.execute("SELECT fact_text,status FROM private_memory_facts").fetchone()
            job = connection.execute("SELECT status FROM memory_jobs").fetchone()[0]
        self.assertEqual("", message[0])
        self.assertIsNotNone(message[1])
        self.assertEqual("", summary)
        self.assertEqual(("关系保留", "[]"), relation)
        self.assertEqual(("长期事实", "active"), fact)
        self.assertEqual("cancelled", job)

    def test_clear_preview_is_bound_to_exact_private_layer_snapshot(self):
        with sqlite3.connect(self.db) as connection:
            connection.execute(
                "INSERT INTO private_chat_messages(user_id,message_id,direction,text,content_hash,event_time,created_at,expires_at,source_kind) VALUES('200','m1','user','第一条','hash1',1,'old','future','text')"
            )
        preview = self.preview(MemoryCommand("clear_private", scope=self.private_scope()))
        with sqlite3.connect(self.db) as connection:
            connection.execute("UPDATE private_chat_messages SET text='',purged_at='external' WHERE message_id='m1'")
            connection.execute(
                "INSERT INTO private_chat_messages(user_id,message_id,direction,text,content_hash,event_time,created_at,expires_at,source_kind) VALUES('200','m2','user','预览后新增','hash2',2,'new','future','text')"
            )
        result = self.confirm(preview.token)
        self.assertFalse(result.success)
        with sqlite3.connect(self.db) as connection:
            rows = connection.execute("SELECT text,purged_at FROM private_chat_messages ORDER BY id").fetchall()
        self.assertEqual([("", "external"), ("预览后新增", None)], rows)

    def test_cancel_is_actor_bound_idempotent_and_audited(self):
        preview = self.preview(MemoryCommand("add_fact", scope=self.private_scope(), content="不会写入"))
        denied = self.service.cancel(preview.token, actor="901", now=NOW)
        self.assertFalse(denied.cancelled)
        first = self.service.cancel(preview.token, actor="900", now=NOW)
        second = self.service.cancel(preview.token, actor="900", now=NOW)
        self.assertTrue(first.cancelled)
        self.assertTrue(second.already_consumed)
        with sqlite3.connect(self.db) as connection:
            self.assertEqual(0, connection.execute("SELECT count(*) FROM private_memory_facts").fetchone()[0])
            self.assertEqual("cancelled", connection.execute("SELECT result FROM memory_governance_audit").fetchone()[0])

    def test_apply_and_audit_share_one_transaction(self):
        preview = self.preview(MemoryCommand("add_fact", scope=self.private_scope(), content="必须回滚"))
        with patch.object(self.service, "_insert_audit", side_effect=sqlite3.OperationalError("audit failed")):
            result = self.confirm(preview.token)
        self.assertFalse(result.success)
        with sqlite3.connect(self.db) as connection:
            self.assertEqual(0, connection.execute("SELECT count(*) FROM private_memory_facts").fetchone()[0])
            self.assertIsNone(connection.execute("SELECT consumed_at FROM memory_pending_operations").fetchone()[0])

    def test_apply_sql_abort_rolls_back_writes_but_consumes_and_audits_failure(self):
        with sqlite3.connect(self.db) as connection:
            connection.execute(
                """
                CREATE TRIGGER reject_governance_fact
                BEFORE INSERT ON private_memory_facts
                BEGIN SELECT RAISE(ABORT, 'do-not-expose-this-marker'); END
                """
            )
        preview = self.preview(MemoryCommand("add_fact", scope=self.private_scope(), content="触发失败"))
        result = self.confirm(preview.token)
        self.assertFalse(result.success)
        with sqlite3.connect(self.db) as connection:
            self.assertEqual(0, connection.execute("SELECT count(*) FROM private_memory_facts").fetchone()[0])
            self.assertIsNotNone(connection.execute(
                "SELECT consumed_at FROM memory_pending_operations WHERE id=?", (preview.operation_id,)
            ).fetchone()[0])
            audit = connection.execute(
                "SELECT result,error_code,reason FROM memory_governance_audit WHERE operation_id=?",
                (preview.operation_id,),
            ).fetchone()
        self.assertEqual(("failed", "db_error", "管理员核实"), audit)
        self.assertTrue(self.confirm(preview.token).already_consumed)

    def test_clear_uses_secure_delete_and_truncates_wal_bytes(self):
        marker = "UNIQUE-PRIVATE-RAW-MARKER-7f9c2b"
        with sqlite3.connect(self.db) as connection:
            connection.execute("PRAGMA journal_mode=WAL")
            connection.execute(
                "INSERT INTO private_chat_messages(user_id,message_id,direction,text,content_hash,event_time,created_at,expires_at,source_kind) VALUES('200','raw-marker','user',?,'hash',1,'old','future','text')",
                (marker,),
            )
        preview = self.preview(MemoryCommand("clear_private", scope=self.private_scope()))
        result = self.confirm(preview.token)
        self.assertTrue(result.success)
        self.assertTrue(result.physical_cleanup_complete)
        marker_bytes = marker.encode("utf-8")
        self.assertNotIn(marker_bytes, self.db.read_bytes())
        wal = Path(str(self.db) + "-wal")
        if wal.exists():
            self.assertNotIn(marker_bytes, wal.read_bytes())
        with sqlite3.connect(self.db) as connection:
            audit = connection.execute(
                "SELECT result,error_code FROM memory_governance_audit WHERE operation_id=?",
                (preview.operation_id,),
            ).fetchone()
        self.assertEqual(("success", ""), audit)

    def test_checkpoint_busy_reports_committed_physical_cleanup_pending(self):
        with sqlite3.connect(self.db) as connection:
            connection.execute(
                "INSERT INTO private_chat_messages(user_id,message_id,direction,text,content_hash,event_time,created_at,expires_at,source_kind) VALUES('200','busy','user','正文','hash',1,'old','future','text')"
            )
        preview = self.preview(MemoryCommand("clear_private", scope=self.private_scope()))
        with patch("plugins.memory_governance.service._checkpoint_truncate", return_value=False):
            result = self.confirm(preview.token)
        self.assertTrue(result.success)
        self.assertFalse(result.physical_cleanup_complete)
        self.assertIn("已提交", result.message)
        self.assertIn("物理清理未完成", result.message)
        with sqlite3.connect(self.db) as connection:
            self.assertEqual("", connection.execute("SELECT text FROM private_chat_messages").fetchone()[0])

    def test_real_checkpoint_busy_is_persisted_as_searchable_audit_warning(self):
        with sqlite3.connect(self.db) as connection:
            connection.execute("PRAGMA journal_mode=WAL")
            connection.execute(
                "INSERT INTO private_chat_messages(user_id,message_id,direction,text,content_hash,event_time,created_at,expires_at,source_kind) VALUES('200','real-busy','user','正文','hash',1,'old','future','text')"
            )
            connection.commit()
        preview = self.preview(MemoryCommand("clear_private", scope=self.private_scope()))

        reader = sqlite3.connect(self.db)
        try:
            reader.execute("BEGIN")
            reader.execute("SELECT text FROM private_chat_messages").fetchone()
            result = self.confirm(preview.token)
        finally:
            reader.close()

        self.assertTrue(result.success)
        self.assertFalse(result.physical_cleanup_complete)
        with sqlite3.connect(self.db) as connection:
            audit = connection.execute(
                "SELECT result,error_code FROM memory_governance_audit WHERE operation_id=?",
                (preview.operation_id,),
            ).fetchone()
        self.assertEqual(("success", "physical_cleanup_pending"), audit)

    def test_clear_without_summary_creates_tombstone_through_all_history(self):
        with sqlite3.connect(self.db) as connection:
            connection.executemany(
                "INSERT INTO private_chat_messages(user_id,message_id,direction,text,content_hash,event_time,created_at,expires_at,purged_at,source_kind) VALUES('200',?,'user',?,'hash',1,'old','future',?,'text')",
                (("old-purged", "", "earlier"), ("live", "当前正文", None)),
            )
        preview = self.preview(MemoryCommand("clear_private", scope=self.private_scope()))
        self.assertTrue(self.confirm(preview.token).success)
        with sqlite3.connect(self.db) as connection:
            tombstone = connection.execute(
                "SELECT summary_text,source_start_id,source_end_id,summarized_through_id,version FROM private_conversation_summaries WHERE user_id='200'"
            ).fetchone()
        self.assertEqual(("", 0, 0, 2, 1), tombstone)

    def test_group_writes_refresh_only_group_member_json_mirror(self):
        root = Path(self.temporary.name) / "member-json"
        service = MemoryGovernanceService(
            self.db,
            private_allowed_user_ids=("200",),
            member_memory_root=root,
        )
        add = service.preview(
            MemoryCommand("add_fact", scope=self.group_scope(), content="旧群事实"),
            actor="900",
            now=NOW,
        )
        self.assertTrue(service.confirm(add.token, actor="900", reason="核实", now=NOW).success)
        mirror = root / "123" / "300.json"
        self.assertEqual(["旧群事实"], [item["text"] for item in json.loads(mirror.read_text())["traits"]])
        modify = service.preview(
            MemoryCommand("modify_fact", fact_kind="group", memory_id=1, content="新群事实"),
            actor="900",
            now=NOW,
        )
        self.assertTrue(service.confirm(modify.token, actor="900", reason="修正", now=NOW).success)
        payload = json.loads(mirror.read_text())
        self.assertEqual(["新群事实"], [item["text"] for item in payload["traits"]])
        self.assertNotIn("旧群事实", mirror.read_text())
        delete = service.preview(
            MemoryCommand("delete_fact", fact_kind="group", memory_id=2),
            actor="900",
            now=NOW,
        )
        self.assertTrue(service.confirm(delete.token, actor="900", reason="删除", now=NOW).success)
        self.assertNotIn("新群事实", mirror.read_text())
        self.assertFalse((root / "private" / "200.json").exists())

    def test_mirror_failure_is_reported_after_database_commit_for_retry(self):
        root = Path(self.temporary.name) / "member-json"
        service = MemoryGovernanceService(
            self.db,
            private_allowed_user_ids=("200",),
            member_memory_root=root,
        )
        preview = service.preview(
            MemoryCommand("add_fact", scope=self.group_scope(), content="已提交事实"),
            actor="900",
            now=NOW,
        )
        with patch("plugins.memory_governance.service._write_mirror", return_value=False):
            result = service.confirm(preview.token, actor="900", reason="核实", now=NOW)
        self.assertTrue(result.success)
        self.assertFalse(result.mirror_refresh_complete)
        self.assertIn("镜像刷新失败", result.message)
        with sqlite3.connect(self.db) as connection:
            self.assertEqual("已提交事实", connection.execute(
                "SELECT trait FROM member_memory_facts WHERE status='active'"
            ).fetchone()[0])
            self.assertEqual("mirror_refresh_failed", connection.execute(
                "SELECT error_code FROM memory_governance_audit WHERE operation_id=?",
                (preview.operation_id,),
            ).fetchone()[0])

    def test_mirror_exception_cannot_hide_committed_database_write(self):
        root = Path(self.temporary.name) / "member-json"
        service = MemoryGovernanceService(
            self.db,
            private_allowed_user_ids=("200",),
            member_memory_root=root,
        )
        preview = service.preview(
            MemoryCommand("add_fact", scope=self.group_scope(), content="已提交事实"),
            actor="900",
            now=NOW,
        )
        with patch(
            "plugins.memory_governance.service._write_mirror",
            side_effect=OSError("sensitive mirror path"),
        ):
            result = service.confirm(preview.token, actor="900", reason="核实", now=NOW)
        self.assertTrue(result.success)
        self.assertFalse(result.mirror_refresh_complete)
        self.assertIn("镜像刷新失败", result.message)

    def test_previews_name_exact_scope_target_before_after_and_impact(self):
        add = self.preview(MemoryCommand("add_fact", scope=self.private_scope(), content="旧事实"))
        self.assertIn("私聊 / QQ 200", add.preview_text)
        self.assertIn("新增：旧事实", add.preview_text)
        self.assertTrue(self.confirm(add.token).success)
        modify = self.preview(MemoryCommand("modify_fact", fact_kind="private", memory_id=1, content="新事实"))
        self.assertIn("私聊 / QQ 200", modify.preview_text)
        self.assertIn("原内容：旧事实", modify.preview_text)
        self.assertIn("新内容：新事实", modify.preview_text)
        delete = self.preview(MemoryCommand("delete_fact", fact_kind="private", memory_id=1))
        self.assertIn("私聊 / QQ 200", delete.preview_text)
        self.assertIn("原内容：旧事实", delete.preview_text)
        relation = self.preview(MemoryCommand("update_relation", scope=self.private_scope(), content="交流自然"))
        self.assertIn("私聊 / QQ 200", relation.preview_text)
        self.assertIn("新状态：交流自然", relation.preview_text)
        clear = self.preview(MemoryCommand("clear_private", scope=self.private_scope()))
        self.assertIn("私聊 / QQ 200", clear.preview_text)
        self.assertIn("原文", clear.preview_text)
        for item in (add, modify, delete, relation, clear):
            self.assertLessEqual(len(item.preview_text), 1600)

    def test_view_supports_facts_relationship_and_status(self):
        preview = self.preview(MemoryCommand("add_fact", scope=self.private_scope(), content="可查看事实"))
        self.assertTrue(self.confirm(preview.token).success)
        facts = self.service.view(MemoryCommand("view_facts", scope=self.private_scope()), actor="900")
        relation = self.service.view(MemoryCommand("view_relation", scope=self.private_scope()), actor="900")
        status = self.service.view(MemoryCommand("status"), actor="900")
        help_result = self.service.view(MemoryCommand("help"), actor="900")
        self.assertIn("P-1", facts.text)
        self.assertIn("可查看事实", facts.text)
        self.assertIn("暂无", relation.text)
        self.assertNotIn("200", status.text)
        self.assertIn("/记忆 添加", help_result.text)

    def test_stale_confirm_is_failed_audited_and_consumed_once(self):
        added = self.preview(MemoryCommand("add_fact", scope=self.private_scope(), content="原事实"))
        self.assertTrue(self.confirm(added.token).success)
        preview = self.preview(MemoryCommand("modify_fact", fact_kind="private", memory_id=1, content="新事实"))
        with sqlite3.connect(self.db) as connection:
            connection.execute("UPDATE private_memory_facts SET version=version+1 WHERE id=1")
        result = self.confirm(preview.token)
        self.assertFalse(result.success)
        self.assertTrue(self.confirm(preview.token).already_consumed)
        with sqlite3.connect(self.db) as connection:
            row = connection.execute(
                "SELECT result,reason,error_code FROM memory_governance_audit WHERE operation_id=?",
                (preview.operation_id,),
            ).fetchone()
        self.assertEqual(("failed", "管理员核实", "conflict"), row)

    def test_private_target_must_remain_in_existing_allowlist(self):
        service = MemoryGovernanceService(self.db, private_allowed_user_ids=())
        with self.assertRaises(ValueError):
            service.preview(MemoryCommand("add_fact", scope=self.private_scope(), content="不允许"), actor="900", now=NOW)

    def test_confirm_rechecks_allowlist_after_preview(self):
        preview = self.preview(MemoryCommand("add_fact", scope=self.private_scope(), content="不得落库"))
        self.service.private_allowed_user_ids = frozenset()
        result = self.confirm(preview.token)
        self.assertFalse(result.success)
        with sqlite3.connect(self.db) as connection:
            self.assertEqual(0, connection.execute("SELECT count(*) FROM private_memory_facts").fetchone()[0])

    def test_private_fact_id_writes_recheck_current_allowlist(self):
        added = self.preview(MemoryCommand("add_fact", scope=self.private_scope(), content="已有事实"))
        self.assertTrue(self.confirm(added.token).success)
        service = MemoryGovernanceService(self.db, private_allowed_user_ids=())
        for action in ("modify_fact", "delete_fact"):
            with self.assertRaises(ValueError):
                service.preview(
                    MemoryCommand(action, fact_kind="private", memory_id=1, content="修正"),
                    actor="900",
                    now=NOW,
                )

    def test_clear_relationship_topics_records_governance_source_and_preserves_watermark(self):
        with sqlite3.connect(self.db) as connection:
            connection.execute(
                "INSERT INTO relationship_states(conversation_kind,group_id,user_id,persona_id,state_text,open_topics_json,source_message_id,source_watermark,version,created_at,updated_at) VALUES('private',NULL,'200','radish-cat','关系保留','[\"待续\"]','message-17',17,1,'old','old')"
            )
        preview = self.preview(MemoryCommand("clear_private", scope=self.private_scope()))
        self.assertTrue(self.confirm(preview.token).success)
        with sqlite3.connect(self.db) as connection:
            row = connection.execute(
                "SELECT source_message_id,source_watermark,version FROM relationship_states"
            ).fetchone()
        self.assertRegex(row[0], r"^governance:[1-9][0-9]*$")
        self.assertEqual((17, 2), row[1:])

    def test_group_fact_delete_invalidates_stale_summary_and_prompt(self):
        with sqlite3.connect(self.db) as connection:
            connection.execute("INSERT INTO member_memories VALUES(123,'300','群友','[]','[]','old')")
            connection.execute(
                "INSERT INTO member_memory_facts(group_id,user_id,trait,evidence_message_id,created_at,updated_at) VALUES(123,'300','旧事实','m1','old','old')"
            )
            connection.execute(
                "INSERT INTO member_memory_summaries VALUES(123,'300','包含旧事实',1,'old')"
            )
        preview = self.preview(MemoryCommand("delete_fact", fact_kind="group", memory_id=1))
        self.assertTrue(self.confirm(preview.token).success)
        profile = load_profiles(self.db, group_id=123, user_ids=["300"], compact=True)[0]
        self.assertEqual("", profile.summary)
        self.assertEqual((), profile.traits)

    def test_service_revalidates_typed_content_lengths(self):
        with self.assertRaises(ValueError):
            self.preview(MemoryCommand("add_fact", scope=self.private_scope(), content="字" * 81))
        with self.assertRaises(ValueError):
            self.preview(MemoryCommand("update_relation", scope=self.private_scope(), content="字" * 601))

    def test_expired_cancel_consumes_token_without_applying(self):
        preview = self.preview(MemoryCommand("add_fact", scope=self.private_scope(), content="不会写入"))
        result = self.service.cancel(
            preview.token, actor="900", now=NOW + timedelta(minutes=10)
        )
        self.assertFalse(result.cancelled)
        self.assertTrue(self.service.cancel(preview.token, actor="900", now=NOW).already_consumed)


if __name__ == "__main__":
    unittest.main()
