import asyncio
import sqlite3
import unittest
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import AsyncMock, patch

from plugins.chat_archive.db import archive_payload
from plugins.private_memory.jobs import MemoryJobQueue
from plugins.private_memory.models import ConversationScope, MemoryJob, RelationshipState
from plugins.private_memory.relationship import RelationshipStore
from plugins.private_memory.schema import migrate
from plugins.private_memory.store import PrivateMemoryStore


def _job(job_type: str, *, watermark: int, expected_version: int = 0,
         kind: str = "private", group_id: int | None = None) -> MemoryJob:
    return MemoryJob(
        id=1, job_type=job_type,
        scope=ConversationScope(kind, "200", group_id, "radish-cat"),
        input_through_id=watermark, expected_version=expected_version,
        status="running", attempts=1, next_run_at="", lease_owner="worker",
        lease_expires_at=None, claim_version=1, error_code="", error_summary="",
        created_at="", updated_at="",
    )


class PrivateMemoryProcessorTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.directory = TemporaryDirectory()
        self.database = Path(self.directory.name) / "chat.db"
        migrate(self.database)
        self.store = PrivateMemoryStore(self.database)
        self.relationships = RelationshipStore(self.database)

    async def asyncTearDown(self) -> None:
        self.directory.cleanup()

    def append(self, message_id: str, text: str, event_time: int = 1) -> int:
        return self.store.append_user_message(
            user_id="200", message_id=message_id, text=text,
            event_time=event_time, source_kind="text",
        )

    def processor(self, **changes):
        from plugins.private_memory.processor import PrivateMemoryProcessor

        values = {
            "store": self.store,
            "relationship_store": self.relationships,
            "summarize": AsyncMock(return_value="新的滚动摘要"),
            "extract": AsyncMock(return_value=()),
            "update_relationship": AsyncMock(return_value=None),
            "private_memory_enabled": lambda: True,
            "relationship_enabled": lambda: True,
        }
        values.update(changes)
        return PrivateMemoryProcessor(**values)

    async def test_summary_reloads_only_committed_scope_through_watermark(self) -> None:
        first = self.append("p1", "第一句", 1)
        through = self.append("p2", "第二句", 2)
        self.store.append_user_message(
            user_id="201", message_id="other", text="不能串线",
            event_time=3, source_kind="text",
        )
        summarize = AsyncMock(return_value="新的滚动摘要")
        processor = self.processor(summarize=summarize)

        self.assertTrue(await processor.process(_job("private_summary", watermark=through)))

        previous, messages = summarize.await_args.args
        self.assertEqual("", previous)
        self.assertEqual([first, through], [message.id for message in messages])
        self.assertEqual(["200", "200"], [message.user_id for message in messages])
        summary = self.store.get_summary(user_id="200")
        self.assertEqual("新的滚动摘要", summary.summary_text)
        self.assertEqual(through, summary.summarized_through_id)

    async def test_facts_keep_real_source_are_idempotent_and_filter_secrets(self) -> None:
        from plugins.private_memory.models import PrivateFactCandidate

        through = self.append("p1", "我喜欢火锅；密码是 hunter2", 1)
        candidates = (
            PrivateFactCandidate("200", "喜欢火锅", "p1", "我喜欢火锅"),
            PrivateFactCandidate("200", "密码是 hunter2", "p1", "密码是 hunter2"),
            PrivateFactCandidate("200", "喜欢跑步", "not-real", "我喜欢跑步"),
        )
        processor = self.processor(extract=AsyncMock(return_value=candidates))

        self.assertTrue(await processor.process(_job("private_facts", watermark=through)))
        self.assertTrue(await processor.process(_job("private_facts", watermark=through)))

        facts = self.store.active_facts(user_id="200", limit=10)
        self.assertEqual([("喜欢火锅", "p1", "我喜欢火锅")], [
            (fact.fact_text, fact.source_message_id, fact.source_quote) for fact in facts
        ])

    async def test_private_relationship_uses_private_message_id_watermark(self) -> None:
        from plugins.private_memory.ai import RelationshipCandidate

        watermark = self.append("p9", "也许下次聊电影", 9)
        update = AsyncMock(return_value=RelationshipCandidate(
            state_text="对方似乎愿意继续交流", open_topics=("也许聊电影",),
            preferred_address="", communication_style="轻松",
        ))
        processor = self.processor(update_relationship=update)

        self.assertTrue(await processor.process(_job("relationship", watermark=watermark)))

        state = self.relationships.get_private(user_id="200", persona_id="radish-cat")
        self.assertEqual(watermark, state.source_watermark)
        self.assertEqual("p9", state.source_message_id)
        self.assertIn("似乎", state.state_text)

    async def test_group_relationship_reloads_archived_rows_by_rowid(self) -> None:
        from plugins.private_memory.ai import RelationshipCandidate

        archive_payload(self.database, 123, {
            "message_id": "g1", "group_id": 123, "event_time": 1,
            "user_id": "200", "sender": {"nickname": "群友"},
            "segments": [], "plaintext": "以后聊跑步", "reply_message_id": None,
        })
        with closing(sqlite3.connect(self.database)) as connection:
            watermark = int(connection.execute(
                "SELECT rowid FROM chat_messages WHERE message_id='g1'"
            ).fetchone()[0])
        update = AsyncMock(return_value=RelationshipCandidate(
            state_text="愿意交流", open_topics=("聊跑步",),
            preferred_address="", communication_style="简洁",
        ))
        processor = self.processor(update_relationship=update)

        self.assertTrue(await processor.process(_job(
            "relationship", watermark=watermark, kind="group", group_id=123
        )))
        state = self.relationships.get_group(
            group_id=123, user_id="200", persona_id="radish-cat"
        )
        self.assertEqual(watermark, state.source_watermark)
        self.assertEqual("g1", state.source_message_id)

    async def test_first_group_relationship_starts_at_job_watermark_not_history(self) -> None:
        from plugins.private_memory.ai import RelationshipCandidate

        for message_id, event_time, text in (
            ("old", 1, "启用前历史"),
            ("new", 2, "启用后的第一条"),
        ):
            archive_payload(self.database, 123, {
                "message_id": message_id, "group_id": 123,
                "event_time": event_time, "user_id": "200",
                "sender": {"nickname": "群友"}, "segments": [],
                "plaintext": text, "reply_message_id": None,
            })
        with closing(sqlite3.connect(self.database)) as connection:
            watermark = int(connection.execute(
                "SELECT rowid FROM chat_messages WHERE message_id='new'"
            ).fetchone()[0])
        seen: list[str] = []

        async def update(current, messages):
            seen.extend(message.text for message in messages)
            return RelationshipCandidate("新关系", (), "", "")

        processor = self.processor(update_relationship=update)
        self.assertTrue(await processor.process(_job(
            "relationship", watermark=watermark, kind="group", group_id=123
        )))
        self.assertEqual(["启用后的第一条"], seen)

    async def test_disabled_switch_is_rechecked_after_model_before_commit(self) -> None:
        watermark = self.append("p1", "第一句")
        enabled = True
        started = asyncio.Event()
        resume = asyncio.Event()

        async def summarize(previous, messages):
            started.set()
            await resume.wait()
            return "不应提交"

        processor = self.processor(
            summarize=summarize, private_memory_enabled=lambda: enabled
        )
        task = asyncio.create_task(processor.process(_job(
            "private_summary", watermark=watermark
        )))
        await asyncio.wait_for(started.wait(), 1)
        enabled = False
        resume.set()

        self.assertFalse(await task)
        self.assertIsNone(self.store.get_summary(user_id="200"))

    async def test_clear_while_summary_paused_prevents_summary_revival(self) -> None:
        watermark = self.append("p1", "第一句")
        started = asyncio.Event()
        resume = asyncio.Event()

        async def summarize(previous, messages):
            started.set()
            await resume.wait()
            return "不应复活"

        processor = self.processor(summarize=summarize)
        task = asyncio.create_task(processor.process(_job(
            "private_summary", watermark=watermark
        )))
        await asyncio.wait_for(started.wait(), 1)
        self.store.clear_private_layers(
            user_id="200", actor="1", reason="test", operation_id=1
        )
        resume.set()

        self.assertFalse(await task)
        self.assertIsNone(self.store.get_summary(user_id="200"))

    async def test_stale_summary_job_rebases_from_current_summary(self) -> None:
        first = self.append("p1", "第一句", 1)
        second = self.append("p2", "第二句", 2)
        seen: list[tuple[str, list[int]]] = []

        async def summarize(previous, messages):
            seen.append((previous, [message.id for message in messages]))
            return "第一版摘要" if not previous else "第二版摘要"

        processor = self.processor(summarize=summarize)
        self.assertTrue(await processor.process(_job(
            "private_summary", watermark=first, expected_version=0
        )))
        self.assertTrue(await processor.process(_job(
            "private_summary", watermark=second, expected_version=0
        )))

        summary = self.store.get_summary(user_id="200")
        self.assertEqual("第二版摘要", summary.summary_text)
        self.assertEqual(second, summary.summarized_through_id)
        self.assertEqual([
            ("", [first]),
            ("第一版摘要", [second]),
        ], seen)

    async def test_clear_during_stale_summary_rebase_prevents_revival(self) -> None:
        first = self.append("p1", "第一句", 1)
        second = self.append("p2", "第二句", 2)
        processor = self.processor(summarize=AsyncMock(return_value="第一版摘要"))
        self.assertTrue(await processor.process(_job(
            "private_summary", watermark=first, expected_version=0
        )))
        started = asyncio.Event()
        resume = asyncio.Event()

        async def summarize(previous, messages):
            self.assertEqual("第一版摘要", previous)
            self.assertEqual([second], [message.id for message in messages])
            started.set()
            await resume.wait()
            return "不应复活"

        processor.summarize = summarize
        task = asyncio.create_task(processor.process(_job(
            "private_summary", watermark=second, expected_version=0
        )))
        await asyncio.wait_for(started.wait(), 1)
        self.store.clear_private_layers(
            user_id="200", actor="1", reason="test", operation_id=10
        )
        resume.set()

        self.assertFalse(await task)
        self.assertIsNone(self.store.get_summary(user_id="200"))

    async def test_stale_summary_does_not_rebase_across_purged_source(self) -> None:
        first = self.append("p1", "第一句", 100)
        self.append("p2", "将被清理", 101)
        third = self.append("p3", "第三句", 102)
        processor = self.processor(summarize=AsyncMock(return_value="第一版摘要"))
        self.assertTrue(await processor.process(_job(
            "private_summary", watermark=first, expected_version=0
        )))
        self.store.purge_expired(
            now=datetime.fromtimestamp(102, timezone.utc),
            retention_days=30,
            max_messages=1,
        )
        summarize = AsyncMock(return_value="不应跨越已清理来源")
        processor.summarize = summarize

        self.assertFalse(await processor.process(_job(
            "private_summary", watermark=third, expected_version=0
        )))
        summarize.assert_not_awaited()
        summary = self.store.get_summary(user_id="200")
        self.assertEqual("第一版摘要", summary.summary_text)
        self.assertEqual(first, summary.summarized_through_id)

    async def test_matching_summary_version_does_not_cross_purged_source(self) -> None:
        first = self.append("p1", "第一句", 100)
        self.append("p2", "将被清理", 101)
        third = self.append("p3", "第三句", 102)
        processor = self.processor(summarize=AsyncMock(return_value="第一版摘要"))
        self.assertTrue(await processor.process(_job(
            "private_summary", watermark=first, expected_version=0
        )))
        self.store.purge_expired(
            now=datetime.fromtimestamp(102, timezone.utc),
            retention_days=30,
            max_messages=1,
        )
        summarize = AsyncMock(return_value="不应跨越已清理来源")
        processor.summarize = summarize

        self.assertFalse(await processor.process(_job(
            "private_summary", watermark=third, expected_version=1
        )))
        summarize.assert_not_awaited()
        summary = self.store.get_summary(user_id="200")
        self.assertEqual("第一版摘要", summary.summary_text)
        self.assertEqual(first, summary.summarized_through_id)

    async def test_clear_tombstone_version_allows_only_current_post_clear_job(self) -> None:
        before_clear = self.append("p1", "清理前消息", 1)
        processor = self.processor(summarize=AsyncMock(return_value="清理前摘要"))
        self.assertTrue(await processor.process(_job(
            "private_summary", watermark=before_clear, expected_version=0
        )))
        self.store.clear_private_layers(
            user_id="200", actor="1", reason="test", operation_id=11
        )
        after_clear = self.append("p2", "清理后消息", 2)
        summarize = AsyncMock(return_value="清理后摘要")
        processor.summarize = summarize

        self.assertTrue(await processor.process(_job(
            "private_summary", watermark=after_clear, expected_version=2
        )))
        previous, messages = summarize.await_args.args
        self.assertEqual("", previous)
        self.assertEqual([after_clear], [message.id for message in messages])
        summary = self.store.get_summary(user_id="200")
        self.assertEqual("清理后摘要", summary.summary_text)
        self.assertEqual(after_clear, summary.summarized_through_id)
        self.assertEqual(3, summary.version)

    async def test_old_summary_version_after_clear_fails_before_model(self) -> None:
        before_clear = self.append("p1", "清理前消息", 1)
        self.store.clear_private_layers(
            user_id="200", actor="1", reason="test", operation_id=12
        )
        after_clear = self.append("p2", "清理后消息", 2)
        summarize = AsyncMock(return_value="不应生成")
        processor = self.processor(summarize=summarize)

        self.assertFalse(await processor.process(_job(
            "private_summary", watermark=after_clear, expected_version=0
        )))
        summarize.assert_not_awaited()
        self.assertIsNone(self.store.get_summary(user_id="200"))
        self.assertLess(before_clear, after_clear)

    async def test_purged_history_clear_keeps_user_watermark_for_new_summary(self) -> None:
        old = self.append("p1", "已过期旧消息", 1)
        other = self.store.append_user_message(
            user_id="201", message_id="other", text="其他用户仍保留",
            event_time=9_000_000, source_kind="text",
        )
        self.store.purge_expired(
            now=datetime.fromtimestamp(10_000_000, timezone.utc),
            retention_days=30,
            max_messages=500,
        )
        self.store.clear_private_layers(
            user_id="200", actor="1", reason="test", operation_id=13
        )

        self.assertEqual(
            (1, old), self.store.get_summary_version_state(user_id="200")
        )
        self.assertEqual(
            ["其他用户仍保留"],
            [item.text for item in self.store.recent_context(user_id="201", limit=10)],
        )
        self.assertGreater(other, old)
        new = self.append("p2", "清理后新消息", 10_000_001)
        stale = AsyncMock(return_value="旧任务不能生成")
        processor = self.processor(summarize=stale)
        self.assertFalse(await processor.process(_job(
            "private_summary", watermark=new, expected_version=0
        )))
        stale.assert_not_awaited()

        summarize = AsyncMock(return_value="清理后的摘要")
        processor.summarize = summarize
        self.assertTrue(await processor.process(_job(
            "private_summary", watermark=new, expected_version=1
        )))
        previous, messages = summarize.await_args.args
        self.assertEqual("", previous)
        self.assertEqual([new], [message.id for message in messages])
        summary = self.store.get_summary(user_id="200")
        self.assertEqual("清理后的摘要", summary.summary_text)
        self.assertEqual(new, summary.summarized_through_id)

    async def test_relationship_parse_failure_keeps_old_state(self) -> None:
        first = self.append("p1", "第一次", 1)
        initial = RelationshipState(
            id=0, scope=ConversationScope("private", "200"), state_text="旧状态",
            open_topics=("旧话题",), preferred_address="", communication_style="",
            source_message_id="p1", source_watermark=first, version=1,
            created_at="", updated_at="",
        )
        self.assertTrue(self.relationships.commit(initial, expected_version=0))
        second = self.append("p2", "第二次", 2)
        processor = self.processor(update_relationship=AsyncMock(return_value=None))

        self.assertFalse(await processor.process(_job(
            "relationship", watermark=second, expected_version=1
        )))
        self.assertEqual("旧状态", self.relationships.get_private(
            user_id="200", persona_id="radish-cat"
        ).state_text)

    async def test_stale_later_relationship_job_rebases_and_reaches_latest_watermark(self) -> None:
        from plugins.private_memory.ai import RelationshipCandidate

        first = self.append("p1", "第一条", 1)
        second = self.append("p2", "第二条", 2)
        calls: list[tuple[int, list[int]]] = []

        async def update(current, messages):
            calls.append((current.version if current else 0, [item.id for item in messages]))
            return RelationshipCandidate(
                f"状态{messages[-1].id}", (), "", ""
            )

        processor = self.processor(update_relationship=update)
        first_job = _job("relationship", watermark=first, expected_version=0)
        second_job = _job("relationship", watermark=second, expected_version=0)
        self.assertTrue(await processor.process(first_job))
        self.assertTrue(await processor.process(second_job))

        state = self.relationships.get_private(
            user_id="200", persona_id="radish-cat"
        )
        self.assertEqual(second, state.source_watermark)
        self.assertEqual([(0, [first]), (1, [second])], calls)

    async def test_concurrent_stale_relationship_job_retries_from_winner_watermark(self) -> None:
        from plugins.private_memory.ai import RelationshipCandidate

        first = self.append("p1", "第一条", 1)
        second = self.append("p2", "第二条", 2)
        both_started = asyncio.Event()
        release_second = asyncio.Event()
        started = 0

        async def update(current, messages):
            nonlocal started
            started += 1
            if started == 2:
                both_started.set()
            await both_started.wait()
            if messages[-1].id == second:
                await release_second.wait()
            return RelationshipCandidate(f"状态{messages[-1].id}", (), "", "")

        processor = self.processor(update_relationship=update)
        first_job = _job("relationship", watermark=first, expected_version=0)
        second_job = _job("relationship", watermark=second, expected_version=0)
        first_task = asyncio.create_task(processor.process(first_job))
        second_task = asyncio.create_task(processor.process(second_job))
        await asyncio.wait_for(both_started.wait(), 1)
        self.assertTrue(await first_task)
        release_second.set()
        self.assertFalse(await second_task)
        self.assertTrue(await processor.process(second_job))
        self.assertEqual(
            second,
            self.relationships.get_private(
                user_id="200", persona_id="radish-cat"
            ).source_watermark,
        )

    async def test_stale_relationship_job_cannot_rebase_over_governance(self) -> None:
        from plugins.private_memory.ai import RelationshipCandidate

        first = self.append("p1", "第一条", 1)
        second = self.append("p2", "第二条", 2)
        scope = ConversationScope("private", "200")
        self.assertTrue(self.relationships.commit(RelationshipState(
            id=0, scope=scope, state_text="自动旧状态", open_topics=(),
            preferred_address="", communication_style="",
            source_message_id="p1", source_watermark=first, version=1,
            created_at="", updated_at="",
        ), expected_version=0))
        self.assertTrue(self.relationships.commit(RelationshipState(
            id=0, scope=scope, state_text="治理状态", open_topics=("人工话题",),
            preferred_address="老师", communication_style="正式",
            source_message_id="governance:20", source_watermark=first, version=2,
            created_at="", updated_at="",
        ), expected_version=1))
        update = AsyncMock(return_value=RelationshipCandidate(
            "模型状态", (), "", ""
        ))
        processor = self.processor(update_relationship=update)

        self.assertFalse(await processor.process(_job(
            "relationship", watermark=second, expected_version=1
        )))
        update.assert_not_awaited()
        state = self.relationships.get_private(
            user_id="200", persona_id="radish-cat"
        )
        self.assertEqual("治理状态", state.state_text)
        self.assertEqual(("人工话题",), state.open_topics)
        self.assertEqual(2, state.version)

    async def test_relationship_job_enqueued_after_governance_can_advance(self) -> None:
        from plugins.private_memory.ai import RelationshipCandidate

        first = self.append("p1", "第一条", 1)
        second = self.append("p2", "第二条", 2)
        scope = ConversationScope("private", "200")
        self.assertTrue(self.relationships.commit(RelationshipState(
            id=0, scope=scope, state_text="自动旧状态", open_topics=(),
            preferred_address="", communication_style="",
            source_message_id="p1", source_watermark=first, version=1,
            created_at="", updated_at="",
        ), expected_version=0))
        self.assertTrue(self.relationships.commit(RelationshipState(
            id=0, scope=scope, state_text="治理状态", open_topics=(),
            preferred_address="", communication_style="",
            source_message_id="governance:21", source_watermark=first, version=2,
            created_at="", updated_at="",
        ), expected_version=1))
        processor = self.processor(update_relationship=AsyncMock(
            return_value=RelationshipCandidate("治理后自动状态", (), "", "")
        ))

        self.assertTrue(await processor.process(_job(
            "relationship", watermark=second, expected_version=2
        )))
        state = self.relationships.get_private(
            user_id="200", persona_id="radish-cat"
        )
        self.assertEqual("治理后自动状态", state.state_text)
        self.assertEqual(second, state.source_watermark)
        self.assertEqual(3, state.version)

    async def test_clear_while_relationship_paused_does_not_revive_open_topics(self) -> None:
        from plugins.private_memory.ai import RelationshipCandidate

        first = self.append("p1", "第一次", 1)
        self.assertTrue(self.relationships.commit(RelationshipState(
            id=0, scope=ConversationScope("private", "200"), state_text="旧状态",
            open_topics=("待清理",), preferred_address="", communication_style="",
            source_message_id="p1", source_watermark=first, version=1,
            created_at="", updated_at="",
        ), expected_version=0))
        second = self.append("p2", "第二次", 2)
        started = asyncio.Event()
        resume = asyncio.Event()

        async def update(current, messages):
            started.set()
            await resume.wait()
            return RelationshipCandidate("新状态", ("不应复活",), "", "")

        processor = self.processor(update_relationship=update)
        task = asyncio.create_task(processor.process(_job(
            "relationship", watermark=second, expected_version=1
        )))
        await asyncio.wait_for(started.wait(), 1)
        self.store.clear_private_layers(
            user_id="200", actor="1", reason="test", operation_id=2
        )
        resume.set()

        self.assertFalse(await task)
        state = self.relationships.get_private(
            user_id="200", persona_id="radish-cat"
        )
        self.assertEqual((), state.open_topics)
        self.assertEqual("旧状态", state.state_text)


class PrivateMemoryAIContractTests(unittest.IsolatedAsyncioTestCase):
    async def test_strict_json_rejects_markdown_and_unknown_certainty(self) -> None:
        from plugins.private_memory.ai import ContractError, _parse_relationship

        for content in (
            '```json\n{"state_text":"熟悉","open_topics":[],"preferred_address":"","communication_style":""}\n```',
            '{"state_text":"确定他讨厌聊天","open_topics":[],"preferred_address":"","communication_style":"","certainty":"certain"}',
        ):
            with self.subTest(content=content), self.assertRaises(ContractError):
                _parse_relationship(content)

    async def test_contract_caps_quotes_and_rejects_unknown_fields(self) -> None:
        from plugins.private_memory.ai import ContractError, _parse_facts

        with self.assertRaises(ContractError):
            _parse_facts('{"facts":[],"debug":"private prompt"}', user_id="200")
        parsed = _parse_facts(
            '{"facts":[{"fact_text":"喜欢火锅","source_message_id":"p1",'
            '"source_quote":"' + ("甲" * 150) + '","certainty":"explicit"}]}',
            user_id="200",
        )
        self.assertEqual(120, len(parsed[0].source_quote))

    async def test_certainty_is_required_for_facts_and_relationship(self) -> None:
        from plugins.private_memory.ai import (
            ContractError,
            _parse_facts,
            _parse_relationship,
        )

        with self.assertRaises(ContractError):
            _parse_facts(
                '{"facts":[{"fact_text":"喜欢火锅",'
                '"source_message_id":"p1","source_quote":"喜欢火锅"}]}',
                user_id="200",
            )
        with self.assertRaises(ContractError):
            _parse_relationship(
                '{"state_text":"熟悉","open_topics":[],'
                '"preferred_address":"","communication_style":""}'
            )

    def test_http_status_error_classification_is_explicit(self) -> None:
        from plugins.private_memory.ai import _classify_http_status

        expectations = {
            401: ("auth_error", False),
            403: ("auth_error", False),
            408: ("request_timeout", True),
            429: ("rate_limited", True),
            500: ("server_error", True),
            503: ("server_error", True),
            400: ("client_error", False),
            404: ("client_error", False),
        }
        for status, expected in expectations.items():
            with self.subTest(status=status):
                error = _classify_http_status(status)
                self.assertEqual(expected, (error.code, error.retryable))


class PrivateSecretFilterTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.directory = TemporaryDirectory()
        self.database = Path(self.directory.name) / "chat.db"
        migrate(self.database)
        self.store = PrivateMemoryStore(self.database)
        self.relationships = RelationshipStore(self.database)

    async def asyncTearDown(self) -> None:
        self.directory.cleanup()

    async def test_credentials_in_candidate_or_quote_are_never_persisted(self) -> None:
        from plugins.private_memory.models import PrivateFactCandidate
        from plugins.private_memory.processor import PrivateMemoryProcessor

        secrets = (
            ("API key 是 " + "sk-" + "abcdefghijklmnopqrstuvwxyz", "API key"),
            ("我的密码是 hunter2", "密码"),
            ("client_secret=abcDEF1234567890", "client_secret"),
            ("Bearer eyJhbGciOiJIUzI1NiJ9.payload.signature", "Bearer"),
            (
                "访问令牌 token=" + "ghp_" + "abcdefghijklmnopqrstuvwxyz123456",
                "令牌",
            ),
            ("私钥是 0123456789abcdef0123456789abcdef", "私钥"),
        )
        candidates = []
        watermark = 0
        for index, (text, marker) in enumerate(secrets, 1):
            message_id = f"s{index}"
            watermark = self.store.append_user_message(
                user_id="200", message_id=message_id, text=text,
                event_time=index, source_kind="text",
            )
            candidates.append(PrivateFactCandidate(
                "200", f"保存{marker}", message_id, text
            ))
            if index == 1:
                candidates.append(PrivateFactCandidate(
                    "200", "喜欢配置服务", message_id, text
                ))
        safe_id = self.store.append_user_message(
            user_id="200", message_id="safe", text="我喜欢火锅",
            event_time=99, source_kind="text",
        )
        candidates.append(PrivateFactCandidate(
            "200", "喜欢火锅", "safe", "我喜欢火锅"
        ))
        processor = PrivateMemoryProcessor(
            store=self.store, relationship_store=self.relationships,
            summarize=AsyncMock(), extract=AsyncMock(return_value=tuple(candidates)),
            update_relationship=AsyncMock(), private_memory_enabled=lambda: True,
            relationship_enabled=lambda: True,
        )

        self.assertTrue(await processor.process(_job(
            "private_facts", watermark=safe_id
        )))
        facts = self.store.active_facts(user_id="200", limit=20)
        self.assertEqual(["喜欢火锅"], [fact.fact_text for fact in facts])

    async def test_existing_pii_rules_filter_fact_text_and_source_quote(self) -> None:
        from plugins.private_memory.models import PrivateFactCandidate
        from plugins.private_memory.processor import PrivateMemoryProcessor

        sensitive_values = (
            "手机号 13800138000",
            "身份证 110101199001011234",
            "住址 北京市朝阳区示例路1号",
            "银行卡 6222021234567890123",
            "微信号 wx_example",
            "邮箱 alice@example.com",
            "真实姓名 张三",
            "内部编号 123456",
        )
        candidates = []
        for index, sensitive in enumerate(sensitive_values, 1):
            message_id = f"pii-{index}"
            self.store.append_user_message(
                user_id="200", message_id=message_id,
                text=f"普通证据；{sensitive}", event_time=index,
                source_kind="text",
            )
            candidates.extend((
                PrivateFactCandidate(
                    "200", f"记住{sensitive}", message_id, "普通证据"
                ),
                PrivateFactCandidate(
                    "200", f"普通偏好{index}", message_id, sensitive
                ),
            ))
        safe_id = self.store.append_user_message(
            user_id="200", message_id="safe-pii", text="我喜欢散步",
            event_time=99, source_kind="text",
        )
        candidates.append(PrivateFactCandidate(
            "200", "喜欢散步", "safe-pii", "我喜欢散步"
        ))
        processor = PrivateMemoryProcessor(
            store=self.store, relationship_store=self.relationships,
            summarize=AsyncMock(), extract=AsyncMock(return_value=tuple(candidates)),
            update_relationship=AsyncMock(), private_memory_enabled=lambda: True,
            relationship_enabled=lambda: True,
        )

        self.assertTrue(await processor.process(_job(
            "private_facts", watermark=safe_id
        )))
        facts = self.store.active_facts(user_id="200", limit=50)
        self.assertEqual(["喜欢散步"], [fact.fact_text for fact in facts])

    async def test_malformed_model_output_keeps_classified_error_and_logs_no_body(self) -> None:
        from plugins.private_memory import ai
        from plugins.private_memory.ai import ContractError
        from plugins.private_memory.models import PrivateMessage

        message = PrivateMessage(
            1, "200", "p1", "user", "private-body", "hash", 1,
            "created", "expires",
        )
        with patch.object(
            ai, "_complete", AsyncMock(return_value="private malformed response")
        ), patch.object(ai.logger, "warning") as warning:
            with self.assertRaises(ContractError) as raised:
                await ai.summarize_private_conversation("", (message,))
        self.assertFalse(raised.exception.retryable)
        logged = " ".join(str(item) for item in warning.call_args.args)
        self.assertNotIn("private-body", logged)
        self.assertNotIn("malformed response", logged)


class MemoryWorkerOutcomeTests(unittest.IsolatedAsyncioTestCase):
    async def test_false_processor_result_does_not_report_cancelled_job_success(self) -> None:
        from plugins.private_memory.jobs import MemoryJobWorker

        with TemporaryDirectory() as directory:
            database = Path(directory) / "chat.db"
            migrate(database)
            queue = MemoryJobQueue(database, lease_seconds=10, max_attempts=3)
            job_id = queue.enqueue(
                job_type="private_summary", conversation_kind="private",
                user_id="200", group_id=None, input_through_id=1, expected_version=0,
            )
            started = asyncio.Event()
            resume = asyncio.Event()

            async def processor(job):
                started.set()
                await resume.wait()
                return False

            worker = MemoryJobWorker(
                queue, processor, allowed_job_types=lambda: {"private_summary"},
                concurrency=1, poll_interval=0.005, worker_id="test-worker",
            )
            run = asyncio.create_task(worker.run())
            await asyncio.wait_for(started.wait(), 1)
            with closing(sqlite3.connect(database)) as connection:
                connection.execute(
                    "UPDATE memory_jobs SET status='cancelled',lease_owner=NULL,lease_expires_at=NULL WHERE id=?",
                    (job_id,),
                )
                connection.commit()
            resume.set()
            while queue.get(job_id).status != "cancelled":
                await asyncio.sleep(0)
            worker.stop_intake()
            await asyncio.wait_for(run, 1)
            self.assertEqual("cancelled", queue.get(job_id).status)

    async def test_nonretryable_contract_error_is_failed_without_retry(self) -> None:
        from plugins.private_memory.ai import ContractError
        from plugins.private_memory.jobs import MemoryJobWorker

        with TemporaryDirectory() as directory:
            database = Path(directory) / "chat.db"
            migrate(database)
            queue = MemoryJobQueue(database, lease_seconds=10, max_attempts=3)
            job_id = queue.enqueue(
                job_type="private_summary", conversation_kind="private",
                user_id="200", group_id=None, input_through_id=1, expected_version=0,
            )

            async def processor(job):
                raise ContractError("invalid_json")

            worker = MemoryJobWorker(
                queue, processor, allowed_job_types=lambda: {"private_summary"},
                concurrency=1, poll_interval=0.005, worker_id="test-worker",
            )
            run = asyncio.create_task(worker.run())
            while queue.get(job_id).status not in {"failed", "pending"}:
                await asyncio.sleep(0.002)
            while queue.get(job_id).status != "failed":
                await asyncio.sleep(0.002)
            worker.stop_intake()
            await asyncio.wait_for(run, 1)
            job = queue.get(job_id)
            self.assertEqual(1, job.attempts)
            self.assertEqual("invalid_json", job.error_code)
            self.assertEqual("processing failed", job.error_summary)


if __name__ == "__main__":
    unittest.main()
