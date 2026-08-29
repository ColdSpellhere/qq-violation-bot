from __future__ import annotations

import os
import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import nonebot
from nonebot.adapters.onebot.v11 import GroupMessageEvent, Message, PrivateMessageEvent


os.environ.setdefault("TARGET_GROUP_ID", "999000111")

try:
    nonebot.get_driver()
except ValueError:
    nonebot.init()

from plugins.chat_archive import matcher as archive_matcher
from plugins.chat_archive.db import ContextMessage
from plugins.feature_control.state import FeatureController, FeatureState
from plugins.member_memory import matcher as member_matcher
from plugins.private_chat import matcher as private_matcher
from plugins.private_memory.schema import migrate


def _features(root: Path, *, economy: bool = True) -> FeatureController:
    return FeatureController(
        root / "features.json",
        FeatureState(
            business_enabled=True,
            chat_enabled=True,
            group_chat_enabled=True,
            private_chat_enabled=True,
            group_chat_allowed_group_ids=(123,),
            private_chat_allowed_user_ids=("200",),
            private_memory_enabled=True,
            relationship_state_enabled=True,
            economy_mode_enabled=economy,
        ),
    )


def _group_event(*, message_id: int = 104) -> GroupMessageEvent:
    message = Message("我喜欢月季，下次继续聊")
    return GroupMessageEvent(
        time=1_785_168_002,
        self_id=10_000,
        post_type="message",
        sub_type="normal",
        user_id=456_791,
        message_type="group",
        message_id=message_id,
        group_id=123,
        message=message,
        original_message=message,
        raw_message=str(message),
        font=0,
        sender={"user_id": 456_791, "nickname": "群友", "role": "member"},
    )


def _private_event(*, message_id: int) -> PrivateMessageEvent:
    message = Message("记住我喜欢月季")
    return PrivateMessageEvent(
        time=2_000 + message_id,
        self_id=999_999,
        post_type="message",
        sub_type="friend",
        user_id=200,
        message_type="private",
        message_id=message_id,
        message=message,
        original_message=message,
        raw_message=str(message),
        font=0,
        sender={"user_id": 200, "nickname": "用户200"},
    )


class EconomyModeGroupBackgroundTests(unittest.IsolatedAsyncioTestCase):
    async def test_group_archive_survives_but_new_memory_work_waits_until_mode_is_off(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            database = root / "chat.db"
            config = SimpleNamespace(
                chat_archive_path=database,
                member_memory_root=root / "member-memory",
                bot_self_id="10000",
                member_memory_summary_enabled=True,
                target_group_id=123,
            )
            features = _features(root)
            event = _group_event()

            with (
                patch.object(archive_matcher, "FEATURES", features),
                patch.object(archive_matcher, "CONFIG", config),
                patch.object(member_matcher, "FEATURES", features),
                patch.object(member_matcher, "CONFIG", config),
                patch.object(member_matcher.BATCHER, "add") as add,
                patch.object(member_matcher, "_enqueue_group_relationship") as enqueue,
            ):
                await archive_matcher.archive_chat_message(event)
                await member_matcher.collect_member_memory(event)
                features.set_switch("economy_mode_enabled", False, "admin")
                await member_matcher.collect_member_memory(event)

            with closing(sqlite3.connect(database)) as connection:
                archived = connection.execute(
                    "SELECT plaintext FROM chat_messages WHERE message_id=?",
                    (str(event.message_id),),
                ).fetchone()

            self.assertEqual((event.get_plaintext(),), archived)
            self.assertEqual(1, add.call_count)
            self.assertEqual(1, enqueue.call_count)

    async def test_batch_queued_before_economy_mode_does_not_start_member_llm(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            features = _features(root)
            config = SimpleNamespace(
                chat_archive_path=root / "chat.db",
                member_memory_root=root / "member-memory",
                bot_self_id="10000",
                member_memory_summary_enabled=True,
            )
            context = [
                ContextMessage(
                    "群友",
                    "我喜欢月季",
                    message_id="104",
                    user_id="456791",
                )
            ]
            extract = AsyncMock(return_value=[])

            with (
                patch.object(member_matcher, "FEATURES", features),
                patch.object(member_matcher, "CONFIG", config),
                patch.object(
                    member_matcher, "recent_text_context", return_value=context
                ) as recent,
                patch.object(member_matcher, "extract_memory_candidates", extract),
                patch.object(
                    member_matcher, "apply_candidates", return_value=0
                ) as apply,
            ):
                await member_matcher.analyze_member_memory(123, "456791", 2_000)
                features.set_switch("economy_mode_enabled", False, "admin")
                await member_matcher.analyze_member_memory(123, "456791", 2_000)

            recent.assert_called_once()
            extract.assert_awaited_once_with(context)
            apply.assert_called_once()


class EconomyModePrivateBackgroundTests(unittest.IsolatedAsyncioTestCase):
    async def test_private_raw_turn_is_persisted_without_memory_jobs_until_mode_is_off(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            database = root / "chat.db"
            migrate(database)
            features = _features(root)
            config = SimpleNamespace(
                chat_archive_path=database,
                private_memory_retention_days=30,
                random_chat_sticker_root=root / "stickers",
                random_chat_special_sticker="special.gif",
                random_chat_sticker_probability=0.0,
                chat_vision_enabled=False,
            )
            bot = AsyncMock()

            with (
                patch.multiple(
                    private_matcher,
                    FEATURES=features,
                    CONFIG=config,
                    CONVERSATIONS={},
                ),
                patch.object(
                    private_matcher,
                    "generate_reply",
                    new=AsyncMock(return_value="收到"),
                ),
                patch.object(private_matcher, "choose_sticker", return_value=None),
            ):
                await private_matcher.handle_private_message(
                    bot, _private_event(message_id=456)
                )
                with closing(sqlite3.connect(database)) as connection:
                    economy_raw = connection.execute(
                        "SELECT text FROM private_chat_messages "
                        "WHERE user_id='200' AND direction='user' AND message_id='456'"
                    ).fetchone()
                    economy_jobs = connection.execute(
                        "SELECT job_type FROM memory_jobs ORDER BY id"
                    ).fetchall()

                features.set_switch("economy_mode_enabled", False, "admin")
                await private_matcher.handle_private_message(
                    bot, _private_event(message_id=457)
                )

            with closing(sqlite3.connect(database)) as connection:
                normal_source = connection.execute(
                    "SELECT id FROM private_chat_messages "
                    "WHERE user_id='200' AND direction='user' AND message_id='457'"
                ).fetchone()
                normal_jobs = connection.execute(
                    "SELECT job_type FROM memory_jobs WHERE input_through_id=? ORDER BY id",
                    (int(normal_source[0]),),
                ).fetchall()

            self.assertEqual(("记住我喜欢月季",), economy_raw)
            self.assertEqual([], economy_jobs)
            self.assertEqual(
                [("private_summary",), ("private_facts",), ("relationship",)],
                normal_jobs,
            )


class EconomyModeWorkerGateTests(unittest.TestCase):
    def test_pending_memory_jobs_pause_in_economy_mode_and_resume_after_exit(
        self,
    ) -> None:
        from plugins.private_memory import lifecycle

        with tempfile.TemporaryDirectory() as directory:
            features = _features(Path(directory))
            with patch.object(lifecycle, "FEATURES", features):
                economy_allowed = lifecycle._allowed_job_types()
                features.set_switch("economy_mode_enabled", False, "admin")
                normal_allowed = lifecycle._allowed_job_types()

        self.assertEqual(frozenset(), economy_allowed)
        self.assertEqual(
            frozenset({"private_summary", "private_facts", "relationship"}),
            normal_allowed,
        )


if __name__ == "__main__":
    unittest.main()
