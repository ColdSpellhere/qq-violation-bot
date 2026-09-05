import asyncio
import os
import sqlite3
import tempfile
import unittest
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

os.environ.setdefault("TARGET_GROUP_ID", "999000111")

from nonebot.adapters.onebot.v11 import Message, PrivateMessageEvent

from plugins.feature_control.state import FeatureController, FeatureState
from plugins.private_chat import matcher as private_matcher
from plugins.private_chat.conversation import PrivateConversation
from plugins.private_memory.models import ConversationScope, PrivateFactCandidate, RelationshipState
from plugins.private_memory.relationship import RelationshipStore
from plugins.private_memory.schema import migrate
from plugins.private_memory.store import PrivateMemoryStore


def _event(text: str, *, user_id: int = 200, message_id: int = 456) -> PrivateMessageEvent:
    message = Message(text)
    return PrivateMessageEvent(
        time=int(datetime.now(timezone.utc).timestamp()),
        self_id=999_999,
        post_type="message",
        sub_type="friend",
        user_id=user_id,
        message_type="private",
        message_id=message_id,
        message=message,
        original_message=message,
        raw_message=text,
        font=0,
        sender={"user_id": user_id, "nickname": f"用户{user_id}"},
    )


class PrivateMemoryIntegrationTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.database = self.root / "chat.db"
        migrate(self.database)
        self.features = FeatureController(
            self.root / "features.json",
            FeatureState(
                business_enabled=True,
                chat_enabled=True,
                group_chat_enabled=False,
                private_chat_enabled=True,
                group_chat_allowed_group_ids=(),
                private_chat_allowed_user_ids=("200", "300"),
                private_memory_enabled=True,
                relationship_state_enabled=True,
            ),
        )
        self.config = SimpleNamespace(
            chat_archive_path=self.database,
            private_memory_retention_days=30,
            random_chat_sticker_root=self.root / "stickers",
            random_chat_special_sticker="special.gif",
            random_chat_sticker_probability=0.0,
        )
        self.store = PrivateMemoryStore(self.database)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def _patch_runtime(self):
        return patch.multiple(
            private_matcher,
            FEATURES=self.features,
            CONFIG=self.config,
            CONVERSATIONS={},
        )

    def _messages(self, user_id: str = "200"):
        return self.store.recent_context(user_id=user_id, limit=20)

    def _job_types(self, user_id: str = "200") -> list[str]:
        with closing(sqlite3.connect(self.database)) as connection:
            return [
                str(row[0])
                for row in connection.execute(
                    "SELECT job_type FROM memory_jobs WHERE user_id=? ORDER BY id",
                    (user_id,),
                )
            ]

    async def test_user_is_committed_before_ai_and_ai_failure_keeps_turn(self) -> None:
        observed: list[str] = []

        async def fail_after_observing(*args, **kwargs):
            observed.extend(item.text for item in self._messages())
            raise private_matcher.RandomChatAIError("down")

        with self._patch_runtime(), patch.object(
            private_matcher, "generate_reply", new=AsyncMock(side_effect=fail_after_observing)
        ):
            await private_matcher.handle_private_message(AsyncMock(), _event("先记住这一条"))

        self.assertEqual(["先记住这一条"], observed)
        self.assertEqual(["先记住这一条"], [item.text for item in self._messages()])
        self.assertEqual(
            ["private_summary", "private_facts", "relationship"], self._job_types()
        )

    async def test_send_failure_does_not_persist_assistant_but_success_does(self) -> None:
        failed_bot = AsyncMock()
        failed_bot.send_private_msg.side_effect = RuntimeError("send failed")
        with self._patch_runtime(), patch.object(
            private_matcher, "generate_reply", new=AsyncMock(return_value="没有发出去")
        ), patch.object(private_matcher, "choose_sticker", return_value=None):
            await private_matcher.handle_private_message(failed_bot, _event("第一条"))
        self.assertEqual(["第一条"], [item.text for item in self._messages()])

        successful_bot = AsyncMock()
        sticker = self.root / "private-sticker.gif"
        with self._patch_runtime(), patch.object(
            private_matcher, "generate_reply", new=AsyncMock(return_value="已经发出去")
        ), patch.object(private_matcher, "choose_sticker", return_value=sticker):
            await private_matcher.handle_private_message(
                successful_bot, _event("第二条", message_id=457)
            )
        self.assertEqual(
            ["第一条", "第二条", "已经发出去"],
            [item.text for item in self._messages()],
        )
        self.assertNotIn(str(sticker), "".join(item.text for item in self._messages()))

    async def test_new_instances_restore_one_user_without_sharing_context_or_locks(self) -> None:
        first = PrivateConversation(limit=20, user_id="200", store=self.store)
        first.append_user(
            private_matcher.ContextMessage("甲", "甲的消息", message_id="a1", user_id="200"),
            event_time=100,
        )
        restarted = PrivateConversation(limit=20, user_id="200", store=self.store)
        other = PrivateConversation(limit=20, user_id="300", store=self.store)

        self.assertEqual(["甲的消息"], [item.text for item in restarted.snapshot()])
        self.assertEqual((), other.snapshot())
        self.assertIs(first.lock, restarted.lock)
        self.assertIsNot(first.lock, other.lock)

    async def test_two_instances_for_one_user_serialize_real_work(self) -> None:
        first = PrivateConversation(limit=20, user_id="200", store=self.store)
        second = PrivateConversation(limit=20, user_id="200", store=self.store)
        first_entered = asyncio.Event()
        release_first = asyncio.Event()
        order: list[str] = []

        async def first_job() -> None:
            async with first.lock:
                order.append("first-start")
                first_entered.set()
                await release_first.wait()
                order.append("first-end")

        async def second_job() -> None:
            await first_entered.wait()
            async with second.lock:
                order.append("second")

        first_task = asyncio.create_task(first_job())
        await first_entered.wait()
        second_task = asyncio.create_task(second_job())
        await asyncio.sleep(0)
        self.assertEqual(["first-start"], order)
        release_first.set()
        await asyncio.gather(first_task, second_task)
        self.assertEqual(["first-start", "first-end", "second"], order)

    async def test_disabled_memory_keeps_twenty_turn_in_memory_behavior(self) -> None:
        self.features.set_switch("private_memory_enabled", False, "admin")
        conversation = PrivateConversation(limit=20)
        for index in range(21):
            conversation.append(
                private_matcher.ContextMessage(
                    str(index), f"消息{index}", message_id=str(index), user_id="200"
                )
            )
        self.assertEqual(20, len(conversation.snapshot()))
        self.assertEqual("消息1", conversation.snapshot()[0].text)

        with self._patch_runtime(), patch.object(
            private_matcher, "generate_reply", new=AsyncMock(return_value="旧路径回复")
        ), patch.object(private_matcher, "choose_sticker", return_value=None), patch.object(
            private_matcher,
            "PrivateMemoryStore",
            side_effect=AssertionError("must not open store"),
            create=True,
        ):
            await private_matcher.handle_private_message(AsyncMock(), _event("旧路径"))
        self.assertEqual((), self._messages())
        self.assertEqual([], self._job_types())

    async def test_allowlist_removal_prevents_new_persistence_and_jobs(self) -> None:
        self.features.remove_allowed("private_chat", "200", "admin")
        generate = AsyncMock(return_value="不应调用")
        with self._patch_runtime(), patch.object(private_matcher, "generate_reply", new=generate):
            await private_matcher.handle_private_message(AsyncMock(), _event("已移除"))
        generate.assert_not_awaited()
        self.assertEqual((), self._messages())
        self.assertEqual([], self._job_types())

    async def test_runtime_disable_after_user_commit_creates_no_later_jobs_or_assistant(self) -> None:
        original = PrivateMemoryStore.append_user_message_state

        def append_then_disable(store, **kwargs):
            watermark = original(store, **kwargs)
            self.features.set_switch("private_memory_enabled", False, "admin")
            self.features.set_switch("relationship_state_enabled", False, "admin")
            return watermark

        generate = AsyncMock(return_value="不应继续")
        with self._patch_runtime(), patch.object(
            PrivateMemoryStore,
            "append_user_message_state",
            new=append_then_disable,
        ), patch.object(private_matcher, "generate_reply", new=generate):
            await private_matcher.handle_private_message(AsyncMock(), _event("只保留用户消息"))

        generate.assert_not_awaited()
        self.assertEqual(["只保留用户消息"], [item.text for item in self._messages()])
        self.assertEqual([], self._job_types())

    async def test_allowlist_removal_during_ai_prevents_send_and_assistant_commit(self) -> None:
        async def remove_then_reply(*args, **kwargs):
            self.features.remove_allowed("private_chat", "200", "admin")
            return "不应发送"

        bot = AsyncMock()
        with self._patch_runtime(), patch.object(
            private_matcher, "generate_reply", new=AsyncMock(side_effect=remove_then_reply)
        ):
            await private_matcher.handle_private_message(bot, _event("调用中移除"))

        bot.send_private_msg.assert_not_awaited()
        self.assertEqual(["调用中移除"], [item.text for item in self._messages()])

    async def test_successful_send_commits_assistant_even_if_runtime_closes_during_send(self) -> None:
        async def send_then_close(*args, **kwargs):
            self.features.remove_allowed("private_chat", "200", "admin")
            self.features.set_switch("private_memory_enabled", False, "admin")
            return {"message_id": 999}

        bot = AsyncMock()
        bot.send_private_msg.side_effect = send_then_close
        with self._patch_runtime(), patch.object(
            private_matcher, "generate_reply", new=AsyncMock(return_value="已经实际发送")
        ), patch.object(private_matcher, "choose_sticker", return_value=None):
            await private_matcher.handle_private_message(bot, _event("需要一致历史"))

        bot.send_private_msg.assert_awaited_once()
        self.assertEqual(
            ["需要一致历史", "已经实际发送"],
            [item.text for item in self._messages()],
        )

    async def test_replay_with_existing_assistant_has_no_ai_send_or_new_jobs(self) -> None:
        bot = AsyncMock()
        generate = AsyncMock(side_effect=("首次回复", "不同的重放回复"))
        with self._patch_runtime(), patch.object(
            private_matcher, "generate_reply", new=generate
        ), patch.object(private_matcher, "choose_sticker", return_value=None):
            await private_matcher.handle_private_message(bot, _event("重放事件"))
            with closing(sqlite3.connect(self.database)) as connection:
                connection.execute("UPDATE memory_jobs SET status='succeeded'")
                connection.commit()
            await private_matcher.handle_private_message(bot, _event("重放事件"))

        self.assertEqual(1, generate.await_count)
        self.assertEqual(1, bot.send_private_msg.await_count)
        self.assertEqual(["重放事件", "首次回复"], [item.text for item in self._messages()])
        self.assertEqual(
            ["private_summary", "private_facts", "relationship"], self._job_types()
        )

    async def test_replay_without_assistant_recovers_but_does_not_requeue(self) -> None:
        other_source = self.store.append_user_message(
            user_id="300", message_id="456", text="另一用户", event_time=1, source_kind="text"
        )
        self.store.append_assistant_message(
            user_id="300",
            source_message_id="456",
            bot_user_id="999999",
            text="另一用户的回复",
            event_time=2,
        )
        self.assertGreater(other_source, 0)

        bot = AsyncMock()
        generate = AsyncMock(
            side_effect=(private_matcher.RandomChatAIError("down"), "恢复回复")
        )
        with self._patch_runtime(), patch.object(
            private_matcher, "generate_reply", new=generate
        ), patch.object(private_matcher, "choose_sticker", return_value=None):
            await private_matcher.handle_private_message(bot, _event("恢复事件"))
            await private_matcher.handle_private_message(bot, _event("恢复事件"))

        self.assertEqual(2, generate.await_count)
        self.assertEqual(1, bot.send_private_msg.await_count)
        self.assertEqual((), generate.await_args_list[1].kwargs["context"])
        self.assertEqual(["恢复事件", "恢复回复"], [item.text for item in self._messages()])
        self.assertEqual(
            ["private_summary", "private_facts", "relationship"], self._job_types()
        )

    async def test_replay_of_purged_user_event_is_ignored(self) -> None:
        self.store.append_user_message(
            user_id="200", message_id="456", text="已经清理", event_time=1, source_kind="text"
        )
        self.store.purge_expired(
            now=datetime.now(timezone.utc), retention_days=30, max_messages=500
        )
        bot = AsyncMock()
        generate = AsyncMock(return_value="不应恢复")
        with self._patch_runtime(), patch.object(
            private_matcher, "generate_reply", new=generate
        ), patch.object(private_matcher, "choose_sticker", return_value=None):
            await private_matcher.handle_private_message(bot, _event("已经清理"))

        generate.assert_not_awaited()
        bot.send_private_msg.assert_not_awaited()
        self.assertEqual([], self._job_types())

    async def test_private_call_receives_bounded_layered_memory(self) -> None:
        source_id = self.store.append_user_message(
            user_id="200", message_id="seed", text="我喜欢火锅", event_time=100, source_kind="text"
        )
        self.assertTrue(
            self.store.commit_summary(
                user_id="200",
                summary_text="摘" * 1_500,
                source_start_id=source_id,
                source_end_id=source_id,
                expected_through_id=0,
                expected_version=0,
            )
        )
        self.store.append_fact(
            PrivateFactCandidate("200", "喜欢火锅", "seed", "我喜欢火锅")
        )
        self.store.append_fact(
            PrivateFactCandidate("200", "长事实" * 500, "seed", "我喜欢火锅")
        )
        relationship = RelationshipStore(self.database)
        self.assertTrue(
            relationship.commit(
                RelationshipState(
                    id=0,
                    scope=ConversationScope("private", "200"),
                    state_text="熟悉" * 300,
                    open_topics=tuple(f"话题{index}" for index in range(5)),
                    preferred_address="",
                    communication_style="",
                    source_message_id="seed",
                    source_watermark=source_id,
                    version=1,
                    created_at="",
                    updated_at="",
                ),
                expected_version=0,
            )
        )
        generate = AsyncMock(return_value="回复")
        with self._patch_runtime(), patch.object(
            private_matcher, "generate_reply", new=generate
        ), patch.object(private_matcher, "choose_sticker", return_value=None):
            await private_matcher.handle_private_message(
                AsyncMock(), _event("继续聊", message_id=458)
            )

        profile = generate.await_args.kwargs["profiles"][0]
        self.assertIn("摘" * 1_200, profile.summary)
        self.assertNotIn("摘" * 1_201, profile.summary)
        self.assertNotIn("熟悉" * 300, profile.summary)
        self.assertNotIn("话题4", profile.summary)
        self.assertEqual(1_200, sum(len(item.text) for item in profile.traits))
        # 优先装入最新事实；单条长事实也必须遵守同一个总字符预算。
        self.assertEqual("长事实" * 400, profile.traits[0].text)
        self.assertEqual("private", generate.await_args.kwargs["chat_mode"])
        self.assertEqual(
            "熟悉" * 300,
            generate.await_args.kwargs["relationship"].state_text,
        )
        self.assertEqual(
            tuple(f"话题{index}" for index in range(5)),
            generate.await_args.kwargs["open_topics"],
        )
        legacy_profile = generate.await_args.kwargs["legacy_profiles"][0]
        self.assertIn("熟悉" * 300, legacy_profile.summary)
        self.assertIn("话题4", legacy_profile.summary)

        with closing(sqlite3.connect(self.database)) as connection:
            jobs = connection.execute(
                "SELECT job_type,input_through_id,expected_version FROM memory_jobs "
                "WHERE user_id='200' ORDER BY id"
            ).fetchall()
        current_watermark = self._messages()[-2].message_id
        self.assertEqual("458", current_watermark)
        self.assertEqual(
            [("private_summary", 2, 1), ("private_facts", 2, 0), ("relationship", 2, 1)],
            jobs,
        )


if __name__ == "__main__":
    unittest.main()
