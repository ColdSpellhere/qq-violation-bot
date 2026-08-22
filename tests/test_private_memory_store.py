from __future__ import annotations

import hashlib
import json
import sqlite3
import stat
import tempfile
import unittest
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

from plugins.private_memory.models import PrivateFactCandidate
from plugins.private_memory.schema import PRIVATE_MEMORY_SCHEMA_VERSION, migrate
from plugins.private_memory.store import PrivateMemoryStore


UTC = timezone.utc


class PrivateMemoryStoreTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.database = Path(temporary.name) / "chat_archive.db"
        migrate(self.database)
        self.store = PrivateMemoryStore(self.database)

    def append_user(
        self,
        message_id: str,
        text: str,
        event_time: int,
        *,
        user_id: str = "200",
    ) -> int:
        return self.store.append_user_message(
            user_id=user_id,
            message_id=message_id,
            text=text,
            event_time=event_time,
            source_kind="text",
        )

    def test_database_is_private_and_user_and_assistant_appends_are_idempotent(self) -> None:
        user_row = self.append_user("u-1", "你好", 1_700_000_000)
        duplicate_user = self.append_user("u-1", "重放时不覆盖", 1_700_000_100)
        assistant_row = self.store.append_assistant_message(
            user_id="200",
            source_message_id="u-1",
            bot_user_id="2727968581",
            text="你好呀",
            event_time=1_700_000_001,
        )
        duplicate_assistant = self.store.append_assistant_message(
            user_id="200",
            source_message_id="u-1",
            bot_user_id="2727968581",
            text="重复发送不覆盖",
            event_time=1_700_000_002,
        )

        self.assertEqual(user_row, duplicate_user)
        self.assertEqual(assistant_row, duplicate_assistant)
        self.assertNotEqual(user_row, assistant_row)
        self.assertEqual(0o600, stat.S_IMODE(self.database.stat().st_mode))
        with closing(sqlite3.connect(self.database)) as connection:
            rows = connection.execute(
                "SELECT direction,text FROM private_chat_messages ORDER BY id"
            ).fetchall()
        self.assertEqual([("user", "你好"), ("assistant", "你好呀")], rows)

    def test_store_never_implicitly_creates_or_migrates_a_database(self) -> None:
        missing = self.database.with_name("missing.db")
        with self.assertRaisesRegex(RuntimeError, "schema version"):
            PrivateMemoryStore(missing)
        self.assertFalse(missing.exists())

        legacy = self.database.with_name("legacy.db")
        with closing(sqlite3.connect(legacy)) as connection:
            connection.execute("CREATE TABLE legacy_marker(value TEXT)")
            connection.commit()
        with self.assertRaisesRegex(RuntimeError, "schema version"):
            PrivateMemoryStore(legacy)
        with closing(sqlite3.connect(legacy)) as connection:
            tables = {
                str(row[0])
                for row in connection.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                )
            }
        self.assertEqual({"legacy_marker"}, tables)

    def test_store_rejects_a_newer_or_older_schema_version(self) -> None:
        with closing(sqlite3.connect(self.database)) as connection:
            connection.execute(
                "UPDATE private_memory_schema_meta SET schema_version=99 WHERE singleton=1"
            )
            connection.commit()
        with self.assertRaisesRegex(
            RuntimeError, f"expected {PRIVATE_MEMORY_SCHEMA_VERSION}, got 99"
        ):
            PrivateMemoryStore(self.database)

    def test_configured_retention_sets_message_expiry(self) -> None:
        store = PrivateMemoryStore(self.database, retention_days=7)
        store.append_user_message(
            user_id="200",
            message_id="seven-days",
            text="短期保留",
            event_time=1_700_000_000,
            source_kind="text",
        )
        with closing(sqlite3.connect(self.database)) as connection:
            expires_at = connection.execute(
                "SELECT expires_at FROM private_chat_messages WHERE message_id='seven-days'"
            ).fetchone()[0]
        self.assertEqual("2023-11-21T22:13:20Z", expires_at)

    def test_assistant_message_exists_only_after_explicit_post_send_append(self) -> None:
        self.append_user("u-1", "在吗", 1_700_000_000)

        before_delivery = self.store.recent_context(user_id="200", limit=10)
        self.assertEqual(["在吗"], [item.text for item in before_delivery])

        self.store.append_assistant_message(
            user_id="200",
            source_message_id="u-1",
            bot_user_id="2727968581",
            text="在的",
            event_time=1_700_000_001,
        )
        after_delivery = self.store.recent_context(user_id="200", limit=10)
        self.assertEqual(["在吗", "在的"], [item.text for item in after_delivery])

    def test_assistant_requires_a_live_source_user_message_for_the_same_user(self) -> None:
        self.append_user("other-source", "别人的消息", 100, user_id="300")
        for user_id, source_message_id in (
            ("200", "missing"),
            ("200", "other-source"),
        ):
            with self.subTest(user_id=user_id, source_message_id=source_message_id):
                with self.assertRaisesRegex(ValueError, "live source user message"):
                    self.store.append_assistant_message(
                        user_id=user_id,
                        source_message_id=source_message_id,
                        bot_user_id="2727968581",
                        text="不应记录",
                        event_time=101,
                    )

        own = self.append_user("purged-source", "已清理", 1, user_id="200")
        self.assertGreater(own, 0)
        self.store.purge_expired(
            now=datetime(2026, 8, 22, tzinfo=UTC),
            retention_days=30,
            max_messages=500,
        )
        with self.assertRaisesRegex(ValueError, "live source user message"):
            self.store.append_assistant_message(
                user_id="200",
                source_message_id="purged-source",
                bot_user_id="2727968581",
                text="也不应记录",
                event_time=102,
            )

    def test_assistant_replay_survives_source_purge_without_overwriting(self) -> None:
        now = datetime(2026, 8, 22, tzinfo=UTC)
        now_epoch = int(now.timestamp())
        self.append_user("source", "稍后清理", now_epoch - (31 * 86_400))
        original_id = self.store.append_assistant_message(
            user_id="200",
            source_message_id="source",
            bot_user_id="2727968581",
            text="已经成功发送",
            event_time=now_epoch,
        )
        self.store.purge_expired(
            now=now,
            retention_days=30,
            max_messages=500,
        )

        replay_id = self.store.append_assistant_message(
            user_id="200",
            source_message_id="source",
            bot_user_id="2727968581",
            text="重放不得覆盖",
            event_time=now_epoch + 1,
        )

        self.assertEqual(original_id, replay_id)
        with closing(sqlite3.connect(self.database)) as connection:
            row = connection.execute(
                """
                SELECT text,event_time,purged_at FROM private_chat_messages
                WHERE id=?
                """,
                (original_id,),
            ).fetchone()
        self.assertEqual(("已经成功发送", now_epoch, None), row)

    def test_recent_context_is_strictly_isolated_ordered_and_limited(self) -> None:
        self.append_user("a-1", "甲一", 100, user_id="200")
        self.append_user("b-1", "乙一", 101, user_id="300")
        self.append_user("a-2", "甲二", 102, user_id="200")
        self.append_user("a-3", "甲三", 103, user_id="200")

        context = self.store.recent_context(user_id="200", limit=2)

        self.assertEqual(["a-2", "a-3"], [item.message_id for item in context])
        self.assertEqual(["甲二", "甲三"], [item.text for item in context])
        self.assertTrue(all(item.user_id == "200" for item in context))
        self.assertEqual((), self.store.recent_context(user_id="200", limit=0))

    def test_retention_applies_age_and_per_user_count_with_exact_survivors(self) -> None:
        day = 86_400
        now_epoch = 2_000_000_000
        cutoff = now_epoch - (30 * day)
        self.append_user("old", "太旧", cutoff - 1)
        self.append_user("boundary", "边界保留", cutoff)
        for index in range(1, 502):
            self.append_user(f"recent-{index:03d}", f"消息 {index}", cutoff + index)
        self.append_user("other-old", "另一个用户过期", cutoff - 1, user_id="300")
        self.append_user("other-new", "另一个用户保留", cutoff + 1, user_id="300")

        report = self.store.purge_expired(
            now=datetime.fromtimestamp(now_epoch, UTC),
            retention_days=30,
            max_messages=500,
        )

        self.assertEqual(4, report.purged_messages)
        self.assertEqual(
            ["recent-002", *[f"recent-{index:03d}" for index in range(3, 502)]],
            self._live_message_ids("200"),
        )
        self.assertEqual(["other-new"], self._live_message_ids("300"))

    def test_purge_removes_body_but_keeps_hash_timestamp_and_purge_time(self) -> None:
        text = "  保留哈希的正文  "
        self.append_user("old", text, 1)

        now = datetime(2026, 8, 22, tzinfo=UTC)
        self.store.purge_expired(now=now, retention_days=30, max_messages=500)

        with closing(sqlite3.connect(self.database)) as connection:
            row = connection.execute(
                "SELECT text,content_hash,event_time,purged_at FROM private_chat_messages"
            ).fetchone()
        self.assertEqual("", row[0])
        self.assertEqual(hashlib.sha256(text.encode("utf-8")).hexdigest(), row[1])
        self.assertEqual(1, row[2])
        self.assertEqual("2026-08-22T00:00:00Z", row[3])
        self.assertEqual((), self.store.recent_context(user_id="200", limit=10))

    def test_raw_pruning_does_not_remove_summary_or_facts_and_caps_source_quote(self) -> None:
        row_id = self.append_user("old", "原文", 1)
        self.assertTrue(self.store.commit_summary(
            user_id="200",
            summary_text="摘要保留",
            source_start_id=row_id,
            source_end_id=row_id,
            expected_through_id=0,
            expected_version=0,
        ))
        long_quote = "  " + ("很 长 的 原 文 " * 30) + "  "
        fact_id = self.store.append_fact(PrivateFactCandidate(
            user_id="200",
            fact_text="喜欢植物",
            source_message_id="old",
            source_quote=long_quote,
        ))

        self.store.purge_expired(
            now=datetime(2026, 8, 22, tzinfo=UTC),
            retention_days=30,
            max_messages=500,
        )

        summary = self.store.get_summary(user_id="200")
        facts = self.store.active_facts(user_id="200", limit=10)
        self.assertEqual("摘要保留", summary.summary_text if summary else "")
        self.assertEqual([fact_id], [item.id for item in facts])
        self.assertLessEqual(len(facts[0].source_quote), 120)
        self.assertNotEqual(" ".join(long_quote.split()), facts[0].source_quote)

    def test_summary_commit_uses_both_watermark_and_version_optimistically(self) -> None:
        first = self.append_user("m-1", "第一条", 100)
        second = self.append_user("m-2", "第二条", 101)

        self.assertTrue(self.store.commit_summary(
            user_id="200",
            summary_text="第一版",
            source_start_id=first,
            source_end_id=first,
            expected_through_id=0,
            expected_version=0,
        ))
        current = self.store.get_summary(user_id="200")
        self.assertIsNotNone(current)
        self.assertEqual((first, 1), (current.summarized_through_id, current.version))
        self.assertFalse(self.store.commit_summary(
            user_id="200",
            summary_text="旧任务",
            source_start_id=first,
            source_end_id=second,
            expected_through_id=0,
            expected_version=0,
        ))
        self.assertFalse(self.store.commit_summary(
            user_id="200",
            summary_text="错误版本",
            source_start_id=first,
            source_end_id=second,
            expected_through_id=first,
            expected_version=2,
        ))
        self.assertTrue(self.store.commit_summary(
            user_id="200",
            summary_text="第二版",
            source_start_id=second,
            source_end_id=second,
            expected_through_id=first,
            expected_version=1,
        ))
        updated = self.store.get_summary(user_id="200")
        self.assertEqual(("第二版", second, 2), (
            updated.summary_text,
            updated.summarized_through_id,
            updated.version,
        ))

    def test_summary_version_state_exposes_tombstone_without_public_summary(self) -> None:
        self.assertEqual(
            (0, 0), self.store.get_summary_version_state(user_id="200")
        )
        watermark = self.append_user("before-clear", "清理前消息", 1)
        self.store.clear_private_layers(
            user_id="200", actor="1", reason="test", operation_id=41
        )

        self.assertIsNone(self.store.get_summary(user_id="200"))
        self.assertEqual(
            (1, watermark), self.store.get_summary_version_state(user_id="200")
        )

    def test_summary_source_endpoints_cannot_cross_private_users(self) -> None:
        own = self.append_user("own", "自己的消息", 100, user_id="200")
        other = self.append_user("other", "别人的消息", 101, user_id="300")

        committed = self.store.commit_summary(
            user_id="200",
            summary_text="不能把别人的消息作为结束水位",
            source_start_id=own,
            source_end_id=other,
            expected_through_id=0,
            expected_version=0,
        )

        self.assertFalse(committed)
        self.assertIsNone(self.store.get_summary(user_id="200"))

    def test_summary_must_start_at_first_unsummarized_live_message(self) -> None:
        first = self.append_user("first", "第一条", 100)
        second = self.append_user("second", "第二条", 101)
        third = self.append_user("third", "第三条", 102)

        self.assertFalse(self.store.commit_summary(
            user_id="200",
            summary_text="跳过第一条",
            source_start_id=second,
            source_end_id=third,
            expected_through_id=0,
            expected_version=0,
        ))
        self.assertTrue(self.store.commit_summary(
            user_id="200",
            summary_text="先总结第一条",
            source_start_id=first,
            source_end_id=first,
            expected_through_id=0,
            expected_version=0,
        ))
        self.assertFalse(self.store.commit_summary(
            user_id="200",
            summary_text="又跳过第二条",
            source_start_id=third,
            source_end_id=third,
            expected_through_id=first,
            expected_version=1,
        ))

    def test_summary_rejects_purged_source_rows(self) -> None:
        first = self.append_user("old", "已经清理", 1)
        self.store.purge_expired(
            now=datetime(2026, 8, 22, tzinfo=UTC),
            retention_days=30,
            max_messages=500,
        )

        self.assertFalse(self.store.commit_summary(
            user_id="200",
            summary_text="不能总结已清理正文",
            source_start_id=first,
            source_end_id=first,
            expected_through_id=0,
            expected_version=0,
        ))

    def test_facts_preserve_source_update_version_trust_and_status(self) -> None:
        self.append_user("m-1", "我喜欢花草", 100)
        fact_id = self.store.append_fact(
            PrivateFactCandidate("200", " 喜欢  花草 ", "m-1", " 我喜欢花草 "),
            trust_level="admin_confirmed",
        )
        duplicate = self.store.append_fact(
            PrivateFactCandidate("200", "喜欢 花草", "m-1", "不同引用不覆盖"),
            trust_level="ai_extracted",
        )

        self.assertEqual(fact_id, duplicate)
        fact = self.store.active_facts(user_id="200", limit=10)[0]
        self.assertEqual("喜欢 花草", fact.fact_text)
        self.assertEqual("m-1", fact.source_message_id)
        self.assertEqual("我喜欢花草", fact.source_quote)
        self.assertEqual("admin_confirmed", fact.trust_level)
        self.assertEqual("active", fact.status)
        self.assertEqual(1, fact.version)
        self.assertTrue(fact.created_at)
        self.assertEqual(fact.created_at, fact.updated_at)

    def test_ai_fact_requires_a_live_same_user_source_but_governance_can_bypass(self) -> None:
        self.append_user("other", "别人喜欢植物", 100, user_id="300")
        for source_message_id in ("missing", "other", "governance:manual-1"):
            with self.subTest(source_message_id=source_message_id):
                with self.assertRaisesRegex(ValueError, "live source user message"):
                    self.store.append_fact(PrivateFactCandidate(
                        "200", "喜欢植物", source_message_id, "喜欢植物"
                    ))

        self.append_user("purged", "清理前来源", 1, user_id="200")
        self.store.purge_expired(
            now=datetime(2026, 8, 22, tzinfo=UTC),
            retention_days=30,
            max_messages=500,
        )
        with self.assertRaisesRegex(ValueError, "live source user message"):
            self.store.append_fact(PrivateFactCandidate(
                "200", "不能引用已清理来源", "purged", "清理前来源"
            ))

        manual_id = self.store.append_fact(
            PrivateFactCandidate(
                "200", "人工确认喜欢植物", "governance:manual-1", "管理员确认"
            ),
            trust_level="admin_confirmed",
        )
        self.assertIsInstance(manual_id, int)

    def test_admin_confirmation_upgrades_existing_ai_fact_but_never_downgrades(self) -> None:
        self.append_user("m-1", "我喜欢花草", 100)
        fact_id = self.store.append_fact(
            PrivateFactCandidate("200", "喜欢花草", "m-1", "我喜欢花草")
        )
        before = self.store.active_facts(user_id="200", limit=10)[0]

        with patch(
            "plugins.private_memory.store._now_text",
            return_value="2026-08-23T01:02:03Z",
        ):
            upgraded_id = self.store.append_fact(
                PrivateFactCandidate("200", "喜欢花草", "m-1", "人工复核：喜欢花草"),
                trust_level="admin_confirmed",
            )
        upgraded = self.store.active_facts(user_id="200", limit=10)[0]
        downgraded_id = self.store.append_fact(
            PrivateFactCandidate("200", "喜欢花草", "m-1", "AI 再次抽取"),
            trust_level="ai_extracted",
        )
        after = self.store.active_facts(user_id="200", limit=10)[0]

        self.assertEqual(fact_id, upgraded_id)
        self.assertEqual(fact_id, downgraded_id)
        self.assertEqual("admin_confirmed", upgraded.trust_level)
        self.assertEqual(before.version + 1, upgraded.version)
        self.assertEqual("2026-08-23T01:02:03Z", upgraded.updated_at)
        self.assertEqual(upgraded, after)

    def test_ai_fact_replay_survives_source_purge_without_overwriting(self) -> None:
        self.append_user("m-1", "来源消息", 1)
        original_id = self.store.append_fact(
            PrivateFactCandidate("200", "喜欢花草", "m-1", "来源消息")
        )
        before = self.store.active_facts(user_id="200", limit=10)[0]
        self.store.purge_expired(
            now=datetime(2026, 8, 22, tzinfo=UTC),
            retention_days=30,
            max_messages=500,
        )

        replay_id = self.store.append_fact(
            PrivateFactCandidate("200", "喜欢花草", "m-1", "重放不得覆盖")
        )

        self.assertEqual(original_id, replay_id)
        self.assertEqual(before, self.store.active_facts(user_id="200", limit=10)[0])

    def test_existing_ai_fact_can_be_admin_upgraded_after_source_purge(self) -> None:
        self.append_user("m-1", "来源消息", 1)
        fact_id = self.store.append_fact(
            PrivateFactCandidate("200", "喜欢花草", "m-1", "来源消息")
        )
        before = self.store.active_facts(user_id="200", limit=10)[0]
        self.store.purge_expired(
            now=datetime(2026, 8, 22, tzinfo=UTC),
            retention_days=30,
            max_messages=500,
        )

        with patch(
            "plugins.private_memory.store._now_text",
            return_value="2026-08-24T01:02:03Z",
        ):
            upgraded_id = self.store.append_fact(
                PrivateFactCandidate("200", "喜欢花草", "m-1", "管理员复核原有来源"),
                trust_level="admin_confirmed",
            )

        upgraded = self.store.active_facts(user_id="200", limit=10)[0]
        self.assertEqual(fact_id, upgraded_id)
        self.assertEqual("admin_confirmed", upgraded.trust_level)
        self.assertEqual(before.version + 1, upgraded.version)
        self.assertEqual("管理员复核原有来源", upgraded.source_quote)
        self.assertEqual("2026-08-24T01:02:03Z", upgraded.updated_at)

    def test_clear_private_layers_preserves_facts_relationship_and_audit(self) -> None:
        message_id = self.append_user("m-1", "等待清理", 100)
        self.store.commit_summary(
            user_id="200",
            summary_text="等待清理的摘要",
            source_start_id=message_id,
            source_end_id=message_id,
            expected_through_id=0,
            expected_version=0,
        )
        fact_id = self.store.append_fact(
            PrivateFactCandidate("200", "保留事实", "m-1", "等待清理")
        )
        with closing(sqlite3.connect(self.database)) as connection:
            now = "2026-08-22T00:00:00Z"
            connection.execute(
                """
                INSERT INTO relationship_states(
                    conversation_kind,group_id,user_id,persona_id,state_text,open_topics_json,
                    preferred_address,communication_style,source_message_id,source_watermark,
                    version,created_at,updated_at
                ) VALUES('private',NULL,'200','radish-cat','关系保留',?,
                         '称呼','风格','m-1',1,3,?,?)
                """,
                (json.dumps(["待续一", "待续二"]), now, now),
            )
            connection.execute(
                """
                INSERT INTO memory_jobs(
                    job_type,conversation_kind,group_id,user_id,persona_id,input_through_id,
                    expected_version,status,attempts,next_run_at,created_at,updated_at
                ) VALUES('private_summary','private',NULL,'200','radish-cat',1,1,
                         'pending',0,?,?,?)
                """,
                (now, now, now),
            )
            connection.execute(
                """
                INSERT INTO memory_jobs(
                    job_type,conversation_kind,group_id,user_id,persona_id,input_through_id,
                    expected_version,status,attempts,next_run_at,created_at,updated_at
                ) VALUES('private_facts','private',NULL,'200','radish-cat',1,1,
                         'pending',0,?,?,?)
                """,
                (now, now, now),
            )
            connection.execute(
                """
                INSERT INTO memory_governance_audit(
                    operation_id,operator_user_id,target_kind,target_user_id,operation_type,
                    reason,result,created_at
                ) VALUES(8,'admin','private','200','seed','保留','success',?)
                """,
                (now,),
            )
            connection.commit()

        report = self.store.clear_private_layers(
            user_id="200", actor="admin", reason="用户要求", operation_id=9
        )

        self.assertEqual(1, report.purged_messages)
        self.assertEqual(1, report.summaries_deleted)
        self.assertEqual(1, report.topics_cleared)
        self.assertEqual(1, report.jobs_cancelled)
        self.assertEqual((), self.store.recent_context(user_id="200", limit=10))
        self.assertIsNone(self.store.get_summary(user_id="200"))
        self.assertEqual([fact_id], [item.id for item in self.store.active_facts(
            user_id="200", limit=10
        )])
        with closing(sqlite3.connect(self.database)) as connection:
            relationship = connection.execute(
                "SELECT state_text,open_topics_json,version FROM relationship_states "
                "WHERE conversation_kind='private' AND user_id='200'"
            ).fetchone()
            jobs = connection.execute(
                "SELECT job_type,status FROM memory_jobs WHERE user_id='200' ORDER BY job_type"
            ).fetchall()
            audit = connection.execute(
                "SELECT operation_id,result FROM memory_governance_audit ORDER BY id"
            ).fetchall()
        self.assertEqual(("关系保留", "[]", 4), relationship)
        self.assertEqual(
            [("private_facts", "pending"), ("private_summary", "cancelled")],
            jobs,
        )
        self.assertEqual([(8, "success"), (9, "success")], audit)

    def test_clear_writes_tombstone_cancels_running_jobs_and_blocks_stale_resurrection(self) -> None:
        old = self.append_user("old", "清空前", 100)
        with closing(sqlite3.connect(self.database)) as connection:
            now = "2026-08-22T00:00:00Z"
            for status, watermark in (("pending", 1), ("running", 2)):
                connection.execute(
                    """
                    INSERT INTO memory_jobs(
                        job_type,conversation_kind,group_id,user_id,persona_id,
                        input_through_id,expected_version,status,attempts,next_run_at,
                        created_at,updated_at
                    ) VALUES('private_summary','private',NULL,'200','radish-cat',?,0,?,0,?,?,?)
                    """,
                    (watermark, status, now, now, now),
                )
            connection.commit()

        report = self.store.clear_private_layers(
            user_id="200", actor="admin", reason="清空", operation_id=11
        )
        new = self.append_user("new", "清空后", 200)

        self.assertEqual(2, report.jobs_cancelled)
        self.assertIsNone(self.store.get_summary(user_id="200"))
        with closing(sqlite3.connect(self.database)) as connection:
            tombstone = connection.execute(
                """
                SELECT summary_text,source_start_id,source_end_id,
                       summarized_through_id,version
                FROM private_conversation_summaries WHERE user_id='200'
                """
            ).fetchone()
            statuses = [
                str(row[0])
                for row in connection.execute(
                    "SELECT status FROM memory_jobs WHERE user_id='200' ORDER BY id"
                )
            ]
        self.assertEqual(("", 0, 0, old, 1), tombstone)
        self.assertEqual(["cancelled", "cancelled"], statuses)
        self.assertFalse(self.store.commit_summary(
            user_id="200",
            summary_text="旧任务不能复活",
            source_start_id=old,
            source_end_id=old,
            expected_through_id=0,
            expected_version=0,
        ))
        self.assertTrue(self.store.commit_summary(
            user_id="200",
            summary_text="清空后的新摘要",
            source_start_id=new,
            source_end_id=new,
            expected_through_id=old,
            expected_version=1,
        ))

    def test_wal_is_truncated_after_purge_and_raw_marker_is_not_recoverable(self) -> None:
        sentinel = "PRIVATE-WAL-MARKER-20260822-UNIQUE"
        marker = sentinel + ("-独特正文" * 1024)
        with closing(sqlite3.connect(self.database)) as connection:
            self.assertEqual("wal", connection.execute("PRAGMA journal_mode=WAL").fetchone()[0])
        self.append_user("old-marker", marker, 1)
        wal = Path(str(self.database) + "-wal")
        before = self.database.read_bytes() + (wal.read_bytes() if wal.exists() else b"")
        self.assertIn(sentinel.encode("utf-8"), before)

        report = self.store.purge_expired(
            now=datetime(2026, 8, 22, tzinfo=UTC),
            retention_days=30,
            max_messages=500,
        )

        after = self.database.read_bytes() + (wal.read_bytes() if wal.exists() else b"")
        self.assertTrue(report.checkpoint_complete)
        self.assertNotIn(sentinel.encode("utf-8"), after)
        self.assertTrue(not wal.exists() or wal.stat().st_size == 0)

    def test_checkpoint_busy_does_not_misreport_committed_purge_as_failure(self) -> None:
        self.append_user("old", "已提交清理", 1)
        with patch("plugins.private_memory.store._checkpoint_truncate", return_value=False):
            report = self.store.purge_expired(
                now=datetime(2026, 8, 22, tzinfo=UTC),
                retention_days=30,
                max_messages=500,
            )

        self.assertEqual(1, report.purged_messages)
        self.assertFalse(report.checkpoint_complete)
        self.assertEqual((), self.store.recent_context(user_id="200", limit=10))

    def test_clear_also_truncates_wal_after_committing_body_removal(self) -> None:
        sentinel = "PRIVATE-CLEAR-WAL-MARKER-20260822-UNIQUE"
        marker = sentinel + ("-清空正文" * 1024)
        with closing(sqlite3.connect(self.database)) as connection:
            self.assertEqual("wal", connection.execute("PRAGMA journal_mode=WAL").fetchone()[0])
        self.append_user("clear-marker", marker, 100)
        wal = Path(str(self.database) + "-wal")
        before = self.database.read_bytes() + (wal.read_bytes() if wal.exists() else b"")
        self.assertIn(sentinel.encode("utf-8"), before)

        report = self.store.clear_private_layers(
            user_id="200", actor="admin", reason="安全清空", operation_id=12
        )

        after = self.database.read_bytes() + (wal.read_bytes() if wal.exists() else b"")
        self.assertTrue(report.checkpoint_complete)
        self.assertNotIn(sentinel.encode("utf-8"), after)
        self.assertTrue(not wal.exists() or wal.stat().st_size == 0)

    def test_user_ids_must_be_positive_ascii_decimal(self) -> None:
        invalid = ("", "0", "-1", "+1", " 1", "1 ", "１２３", "١٢٣", "abc")
        for user_id in invalid:
            with self.subTest(user_id=user_id):
                with self.assertRaises(ValueError):
                    self.store.recent_context(user_id=user_id, limit=1)
        with self.assertRaises(ValueError):
            self.append_user("m", "text", 1, user_id="０")

    def _live_message_ids(self, user_id: str) -> list[str]:
        with closing(sqlite3.connect(self.database)) as connection:
            return [
                str(row[0])
                for row in connection.execute(
                    """
                    SELECT message_id FROM private_chat_messages
                    WHERE user_id=? AND purged_at IS NULL ORDER BY id
                    """,
                    (user_id,),
                )
            ]


if __name__ == "__main__":
    unittest.main()
