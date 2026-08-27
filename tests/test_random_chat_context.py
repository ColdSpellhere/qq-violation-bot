import hashlib
import asyncio
import json
import os
import sqlite3
import tempfile
import unittest
from contextlib import closing, nullcontext
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

os.environ.setdefault("TARGET_GROUP_ID", "999000111")

from nonebot.adapters.onebot.v11 import GroupMessageEvent, Message, MessageSegment

from plugins.chat_archive.db import SCHEMA, ContextMessage, recent_text_context
from plugins.chat_vision.store import ChatVisionStore
from plugins.random_chat.ai import RandomChatAIError, _format_turn
from plugins.random_chat.matcher import send_random_reply


class RecentTextContextTests(unittest.TestCase):
    def _database(self, directory: str) -> Path:
        path = Path(directory) / "chat.db"
        with closing(sqlite3.connect(path)) as conn:
            conn.executescript(SCHEMA)
            conn.commit()
        return path

    def _insert(
        self,
        path: Path,
        *,
        message_id: str,
        group_id: int = 123,
        event_time: int,
        user_id: str,
        text: str,
        card: str = "",
        nickname: str = "",
        segments: list[dict] | None = None,
        reply_message_id: str | None = None,
    ) -> None:
        with closing(sqlite3.connect(path)) as conn:
            conn.execute(
                """
                INSERT INTO chat_messages(
                    message_id,group_id,event_time,user_id,sender_json,message_json,
                    plaintext,reply_message_id,created_at
                ) VALUES(?,?,?,?,?,?,?,?,?)
                """,
                (
                    message_id,
                    group_id,
                    event_time,
                    user_id,
                    json.dumps({"card": card, "nickname": nickname}, ensure_ascii=False),
                    json.dumps(segments or [], ensure_ascii=False),
                    text,
                    reply_message_id,
                    "2026-08-06 00:00:00",
                ),
            )
            conn.commit()

    def _insert_image_asset(
        self,
        path: Path,
        *,
        group_id: int,
        message_id: str,
        ordinal: int,
        status: str,
        description: str | None,
        deleted_at: str | None = None,
    ) -> None:
        with closing(sqlite3.connect(path)) as conn:
            conn.execute(
                """
                INSERT INTO chat_image_assets(
                    group_id,message_id,ordinal,source_url,event_time,status,attempts,
                    description,deleted_at
                ) VALUES(?,?,?,?,?,?,?,?,?)
                """,
                (
                    group_id,
                    message_id,
                    ordinal,
                    "https://example.invalid/image.jpg",
                    1001,
                    status,
                    0,
                    description,
                    deleted_at,
                ),
            )
            conn.commit()

    def test_includes_ready_image_descriptions_after_original_is_deleted(self):
        with tempfile.TemporaryDirectory() as directory:
            path = self._database(directory)
            with closing(sqlite3.connect(path)) as conn:
                conn.executescript(
                    """
                    CREATE TABLE chat_image_assets (
                        id INTEGER PRIMARY KEY,
                        group_id INTEGER NOT NULL,
                        message_id TEXT NOT NULL,
                        ordinal INTEGER NOT NULL,
                        source_url TEXT NOT NULL,
                        event_time INTEGER NOT NULL,
                        status TEXT NOT NULL,
                        attempts INTEGER NOT NULL,
                        description TEXT,
                        deleted_at TEXT
                    );
                    """
                )
                conn.commit()
            self._insert(
                path,
                message_id="m1",
                event_time=1001,
                user_id="7",
                text="",
                nickname="小花",
            )
            self._insert_image_asset(
                path,
                group_id=123,
                message_id="m1",
                ordinal=0,
                status="ready",
                description="一朵白花",
                deleted_at="2026-08-21 12:00:00",
            )
            self._insert_image_asset(
                path,
                group_id=123,
                message_id="m1",
                ordinal=1,
                status="ready",
                description="一只绿色小虫",
            )
            self._insert(path, message_id="failed", event_time=1002, user_id="8", text="")
            self._insert_image_asset(
                path,
                group_id=123,
                message_id="failed",
                ordinal=0,
                status="failed",
                description=None,
            )

            result = recent_text_context(
                path,
                group_id=123,
                since_epoch=1000,
                limit=20,
                exclude_message_id="none",
                bot_user_id="999",
            )

        self.assertEqual(
            [
                ContextMessage(
                    "小花",
                    "[图片]",
                    message_id="m1",
                    user_id="7",
                    image_descriptions=("一朵白花", "一只绿色小虫"),
                )
            ],
            result,
        )

    def test_formats_image_descriptions_as_system_understanding(self):
        formatted = _format_turn(
            ContextMessage(
                "小花",
                "[图片]",
                message_id="m1",
                user_id="7",
                image_descriptions=("一朵白花", "一只绿色小虫"),
            )
        )

        self.assertEqual(
            "[m1] 小花[QQ:7]：[图片]\n[图片理解：一朵白花]\n[图片理解：一只绿色小虫]",
            formatted,
        )

    def test_filters_and_formats_recent_context(self):
        with tempfile.TemporaryDirectory() as directory:
            path = self._database(directory)
            self._insert(path, message_id="old", event_time=999, user_id="1", text="过期")
            self._insert(path, message_id="other", group_id=456, event_time=1001, user_id="2", text="外群")
            self._insert(path, message_id="bot", event_time=1002, user_id="999", text="机器人")
            self._insert(path, message_id="blank", event_time=1003, user_id="3", text="   ")
            self._insert(path, message_id="command", event_time=1004, user_id="3", text=" /help")
            self._insert(path, message_id="a", event_time=1005, user_id="5", text="火锅", card="群名片", nickname="昵称")
            self._insert(path, message_id="b", event_time=1006, user_id="6", text="同意", nickname="小红")
            self._insert(path, message_id="c", event_time=1007, user_id="7", text="走起")
            self._insert(path, message_id="current", event_time=1008, user_id="4", text="当前消息")

            result = recent_text_context(
                path,
                group_id=123,
                since_epoch=1000,
                limit=20,
                exclude_message_id="current",
                bot_user_id="999",
            )

        self.assertEqual(
            [
                ContextMessage("群名片", "火锅", message_id="a", user_id="5"),
                ContextMessage("小红", "同意", message_id="b", user_id="6"),
                ContextMessage("7", "走起", message_id="c", user_id="7"),
            ],
            result,
        )

    def test_can_include_bot_replies_as_typed_assistant_history(self):
        with tempfile.TemporaryDirectory() as directory:
            path = self._database(directory)
            self._insert(
                path,
                message_id="human",
                event_time=1001,
                user_id="5",
                text="你刚才说什么",
                nickname="群友",
            )
            self._insert(
                path,
                message_id="bot-reply",
                event_time=1002,
                user_id="999",
                text="我说先别急",
                nickname="机器人自己",
            )

            result = recent_text_context(
                path,
                group_id=123,
                since_epoch=1000,
                limit=20,
                exclude_message_id="none",
                bot_user_id="999",
                include_bot_messages=True,
            )

        self.assertEqual(2, len(result))
        self.assertFalse(result[0].is_bot)
        self.assertTrue(result[1].is_bot)
        self.assertEqual("我说先别急", result[1].text)

    def test_marks_configured_peer_bot_without_confusing_it_with_self(self):
        with tempfile.TemporaryDirectory() as directory:
            path = self._database(directory)
            self._insert(
                path,
                message_id="peer-reply",
                event_time=1001,
                user_id="888",
                text="另一个机器人的话",
                nickname="另一个机器人",
            )
            result = recent_text_context(
                path,
                group_id=123,
                since_epoch=1000,
                limit=20,
                exclude_message_id="none",
                bot_user_id="999",
                include_bot_messages=True,
                peer_bot_user_ids=("888",),
            )

        self.assertFalse(result[0].is_bot)
        self.assertTrue(result[0].is_peer_bot)

    def test_preserves_mentions_and_resolves_reply_author(self):
        with tempfile.TemporaryDirectory() as directory:
            path = self._database(directory)
            self._insert(path, message_id="a", event_time=1001, user_id="5", text="原话", nickname="小明")
            self._insert(
                path,
                message_id="b",
                event_time=1002,
                user_id="6",
                text="你说得对",
                nickname="小红",
                segments=[
                    {"type": "reply", "data": {"id": "a"}},
                    {"type": "at", "data": {"qq": "5"}},
                    {"type": "text", "data": {"text": "你说得对"}},
                ],
                reply_message_id="a",
            )
            result = recent_text_context(
                path,
                group_id=123,
                since_epoch=1000,
                limit=20,
                exclude_message_id="none",
                bot_user_id="999",
            )

        self.assertEqual(("5",), result[1].at_user_ids)
        self.assertEqual("a", result[1].reply_message_id)
        self.assertEqual("5", result[1].replied_to_user_id)

    def test_returns_newest_twenty_in_chronological_order(self):
        with tempfile.TemporaryDirectory() as directory:
            path = self._database(directory)
            for index in range(25):
                self._insert(
                    path,
                    message_id=str(index),
                    event_time=1000 + index,
                    user_id=str(index),
                    text=f"消息{index}",
                )
            result = recent_text_context(
                path,
                group_id=123,
                since_epoch=900,
                limit=20,
                exclude_message_id="none",
                bot_user_id="999",
            )
        self.assertEqual(20, len(result))
        self.assertEqual("消息5", result[0].text)
        self.assertEqual("消息24", result[-1].text)

    def test_same_second_messages_keep_database_insertion_order(self):
        with tempfile.TemporaryDirectory() as directory:
            path = self._database(directory)
            self._insert(
                path,
                message_id="900",
                event_time=1001,
                user_id="5",
                text="先发",
            )
            self._insert(
                path,
                message_id="100",
                event_time=1001,
                user_id="999",
                text="后发",
                nickname="机器人自己",
            )
            result = recent_text_context(
                path,
                group_id=123,
                since_epoch=1000,
                limit=20,
                exclude_message_id="none",
                bot_user_id="999",
                include_bot_messages=True,
            )

        self.assertEqual(["先发", "后发"], [item.text for item in result])

    def test_context_watermark_excludes_future_users_but_keeps_prior_bot_reply(self):
        with tempfile.TemporaryDirectory() as directory:
            path = self._database(directory)
            self._insert(path, message_id="a", event_time=1001, user_id="1", text="A")
            self._insert(path, message_id="b", event_time=1001, user_id="2", text="B")
            self._insert(path, message_id="c", event_time=1001, user_id="3", text="C")
            self._insert(
                path,
                message_id="bot-a",
                event_time=1002,
                user_id="999",
                text="reply-A",
                reply_message_id="a",
            )
            result = recent_text_context(
                path,
                group_id=123,
                since_epoch=1000,
                limit=20,
                exclude_message_id="b",
                bot_user_id="999",
                include_bot_messages=True,
            )

        self.assertEqual(["A", "reply-A"], [item.text for item in result])

    def test_missing_database_or_table_returns_empty_list(self):
        with tempfile.TemporaryDirectory() as directory:
            missing = Path(directory) / "missing.db"
            empty = Path(directory) / "empty.db"
            empty.touch()
            for path in (missing, empty):
                self.assertEqual(
                    [],
                    recent_text_context(
                        path,
                        group_id=123,
                        since_epoch=0,
                        limit=20,
                        exclude_message_id="none",
                        bot_user_id="999",
                    ),
                )


def _reply(message_id: int = 111) -> dict:
    return {
        "time": 1000,
        "message_type": "group",
        "message_id": message_id,
        "real_id": message_id,
        "sender": {"user_id": 321, "nickname": "引用者"},
        "message": Message("原消息"),
    }


def _event(
    message: Message | None = None,
    *,
    original_message: Message | None = None,
    reply: dict | None = None,
) -> GroupMessageEvent:
    message = message if message is not None else Message("当前消息")
    event = GroupMessageEvent(
        time=2000,
        self_id=999,
        post_type="message",
        sub_type="normal",
        user_id=123,
        message_type="group",
        message_id=456,
        group_id=789,
        message=message,
        original_message=message,
        raw_message="当前消息",
        font=0,
        sender={"user_id": 123, "nickname": "成员", "role": "member"},
        reply=reply,
    )
    if original_message is not None:
        event.original_message = original_message
    return event


class RandomChatIntegrationTests(unittest.IsolatedAsyncioTestCase):
    def _stored_image(
        self,
        store: ChatVisionStore,
        root: Path,
        *,
        message_id: str,
        ordinal: int,
        content: bytes,
        description: str,
        expires_at: str = "2099-08-28 00:00:00",
    ) -> None:
        filename = f"{message_id}-{ordinal}.jpg"
        (root / filename).write_bytes(content)
        asset = store.ensure_pending(
            789,
            message_id,
            ordinal,
            f"https://example.invalid/{filename}",
            2000,
        )
        store.mark_downloaded(
            asset.id,
            filename,
            "image/jpeg",
            len(content),
            hashlib.sha256(content).hexdigest(),
            expires_at,
        )
        store.mark_ready(asset.id, description)

    @staticmethod
    def _vision_config(database: Path, root: Path) -> SimpleNamespace:
        return SimpleNamespace(
            chat_archive_path=database,
            chat_vision_root=root,
            chat_vision_max_bytes=64,
            member_memory_summary_enabled=False,
            random_chat_sticker_root=root / "stickers",
            random_chat_special_sticker="special.gif",
            random_chat_sticker_probability=0.0,
        )

    async def test_raw_image_count_budget_degrades_to_all_persisted_descriptions(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "data" / "chat_vision" / "images"
            root.mkdir(parents=True)
            database = Path(directory) / "chat.db"
            store = ChatVisionStore(database)
            for ordinal in range(1, 6):
                self._stored_image(
                    store,
                    root,
                    message_id="456",
                    ordinal=ordinal,
                    content=f"raw-{ordinal}".encode(),
                    description=f"第{ordinal}张图的事实",
                )
            message = Message(
                [
                    MessageSegment.image(
                        f"https://example.invalid/{ordinal}.jpg"
                    )
                    for ordinal in range(1, 6)
                ]
            )
            config = self._vision_config(database, root)
            config.chat_vision_max_bytes = 1024
            with patch(
                "plugins.random_chat.matcher.CONFIG",
                config,
            ), patch(
                "plugins.random_chat.matcher.recent_text_context",
                return_value=[],
            ), patch(
                "plugins.random_chat.matcher.archived_message_author",
                return_value=None,
            ), patch(
                "plugins.random_chat.matcher.load_profiles",
                return_value=[],
            ), patch(
                "plugins.random_chat.matcher.generate_reply",
                new=AsyncMock(return_value="按描述回复"),
            ) as generate, patch(
                "plugins.random_chat.matcher.choose_sticker",
                return_value=None,
            ):
                sent = await send_random_reply(AsyncMock(), _event(message), "")

        self.assertTrue(sent)
        self.assertEqual([], list(generate.await_args.kwargs["images"]))
        self.assertEqual(
            tuple(f"第{ordinal}张图的事实" for ordinal in range(1, 6)),
            generate.await_args.kwargs["current"].image_descriptions,
        )

    async def test_raw_image_byte_budget_degrades_without_partial_base64_payload(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "data" / "chat_vision" / "images"
            root.mkdir(parents=True)
            database = Path(directory) / "chat.db"
            store = ChatVisionStore(database)
            for ordinal in range(1, 3):
                self._stored_image(
                    store,
                    root,
                    message_id="456",
                    ordinal=ordinal,
                    content=b"12345678",
                    description=f"第{ordinal}张图的事实",
                )
            message = Message(
                [
                    MessageSegment.image("https://example.invalid/1.jpg"),
                    MessageSegment.image("https://example.invalid/2.jpg"),
                ]
            )
            config = self._vision_config(database, root)
            config.chat_vision_max_bytes = 12
            with patch(
                "plugins.random_chat.matcher.CONFIG",
                config,
            ), patch(
                "plugins.random_chat.matcher.recent_text_context",
                return_value=[],
            ), patch(
                "plugins.random_chat.matcher.archived_message_author",
                return_value=None,
            ), patch(
                "plugins.random_chat.matcher.load_profiles",
                return_value=[],
            ), patch(
                "plugins.random_chat.matcher.generate_reply",
                new=AsyncMock(return_value="按描述回复"),
            ) as generate, patch(
                "plugins.random_chat.matcher.choose_sticker",
                return_value=None,
            ):
                sent = await send_random_reply(AsyncMock(), _event(message), "")

        self.assertTrue(sent)
        self.assertEqual([], list(generate.await_args.kwargs["images"]))
        self.assertEqual(
            ("第1张图的事实", "第2张图的事实"),
            generate.await_args.kwargs["current"].image_descriptions,
        )

    async def test_reads_expected_window_and_sends_contextual_reply(self):
        bot = AsyncMock()
        context = [ContextMessage("小明", "前文", message_id="1", user_id="11")]
        with patch(
            "plugins.random_chat.matcher.recent_text_context", return_value=context
        ) as read_context, patch(
            "plugins.random_chat.matcher.archived_message_author", return_value=None
        ), patch(
            "plugins.random_chat.matcher.load_profiles", return_value=[]
        ), patch(
            "plugins.random_chat.matcher.generate_reply",
            new=AsyncMock(return_value="自然回复"),
        ) as generate, patch(
            "plugins.random_chat.matcher.choose_sticker", return_value=None
        ), patch(
            "plugins.random_chat.matcher.extract_memory_candidates",
            create=True,
            new=AsyncMock(return_value=[]),
        ) as extract, patch(
            "plugins.random_chat.matcher.apply_candidates", create=True
        ) as apply:
            await send_random_reply(bot, _event(), "当前消息")

        read_context.assert_called_once()
        kwargs = read_context.call_args.kwargs
        self.assertEqual(200, kwargs["since_epoch"])
        self.assertEqual(40, kwargs["limit"])
        self.assertEqual("456", kwargs["exclude_message_id"])
        self.assertEqual("999", kwargs["bot_user_id"])
        generated = generate.await_args.kwargs
        self.assertEqual(context, generated["context"])
        self.assertEqual("123", generated["current"].user_id)
        self.assertEqual([], generated["profiles"])
        bot.send_group_msg.assert_awaited_once_with(group_id=789, message="自然回复")
        extract.assert_not_awaited()
        apply.assert_not_called()

    async def test_context_limits_bot_self_history_to_latest_three_replies(self):
        history = [
            item
            for index in range(1, 13)
            for item in (
                ContextMessage(
                    f"成员{index}",
                    f"普通聊天内容{index}",
                    message_id=f"u{index}",
                    user_id=str(100 + index),
                ),
                ContextMessage(
                    "机器人自己",
                    f"机器人此前回复{index}",
                    message_id=f"b{index}",
                    user_id="999",
                    is_bot=True,
                ),
            )
        ]
        config = self._vision_config(Path("/tmp/missing.db"), Path("/tmp"))
        config.chat_context_messages = 8
        config.chat_context_minutes = 30
        config.chat_context_self_messages = 3
        config.peer_bot_user_ids = ()
        bot = AsyncMock()
        bot.send_group_msg.return_value = {}
        with patch(
            "plugins.random_chat.matcher.CONFIG", config
        ), patch(
            "plugins.random_chat.matcher.recent_text_context", return_value=history
        ) as read_context, patch(
            "plugins.random_chat.matcher.archived_message_author", return_value=None
        ), patch(
            "plugins.random_chat.matcher.load_profiles", return_value=[]
        ), patch(
            "plugins.random_chat.matcher.generate_reply",
            new=AsyncMock(return_value="自然回复"),
        ) as generate, patch(
            "plugins.random_chat.matcher.choose_sticker", return_value=None
        ):
            sent = await send_random_reply(bot, _event(), "当前消息")

        self.assertTrue(sent)
        self.assertEqual(18, read_context.call_args.kwargs["limit"])
        selected = generate.await_args.kwargs["context"]
        self.assertLessEqual(len(selected), 8)
        self.assertEqual(
            ["b10", "b11", "b12"],
            [item.message_id for item in selected if item.is_bot],
        )

    async def test_context_collapses_repeated_mention_spam_but_keeps_normal_short_turns(self):
        history = [
            ContextMessage("甲", "好", message_id="short-1", user_id="101"),
            ContextMessage(
                "甲",
                "@kona @kona @kona 看我",
                message_id="spam-1",
                user_id="101",
            ),
            ContextMessage("乙", "好", message_id="short-2", user_id="102"),
            ContextMessage(
                "乙",
                "@KONA  @kona @kona  看我",
                message_id="spam-2",
                user_id="102",
            ),
            ContextMessage("丙", "继续聊花", message_id="normal", user_id="103"),
            ContextMessage(
                "丙",
                "@kona @kona @kona @kona 看我",
                message_id="spam-3",
                user_id="103",
            ),
        ]
        config = self._vision_config(Path("/tmp/missing.db"), Path("/tmp"))
        config.chat_context_messages = 20
        config.chat_context_minutes = 30
        config.chat_context_self_messages = 3
        config.peer_bot_user_ids = ()
        bot = AsyncMock()
        bot.send_group_msg.return_value = {}
        with patch(
            "plugins.random_chat.matcher.CONFIG", config
        ), patch(
            "plugins.random_chat.matcher.recent_text_context", return_value=history
        ), patch(
            "plugins.random_chat.matcher.archived_message_author", return_value=None
        ), patch(
            "plugins.random_chat.matcher.load_profiles", return_value=[]
        ), patch(
            "plugins.random_chat.matcher.generate_reply",
            new=AsyncMock(return_value="自然回复"),
        ) as generate, patch(
            "plugins.random_chat.matcher.choose_sticker", return_value=None
        ):
            sent = await send_random_reply(bot, _event(), "当前消息")

        self.assertTrue(sent)
        selected_ids = [
            item.message_id for item in generate.await_args.kwargs["context"]
        ]
        self.assertEqual(
            ["short-1", "short-2", "normal", "spam-3"], selected_ids
        )

    async def test_context_collapses_reposted_long_text_with_only_formatting_changes(self):
        repeated = "这是一段被连续复制的很长回复，内容完全相同，只是标点和空格略有变化"
        history = [
            ContextMessage("甲", repeated, message_id="copy-1", user_id="101"),
            ContextMessage("乙", "中间还有一条正常消息", message_id="normal", user_id="102"),
            ContextMessage(
                "丙",
                "这是一段被连续复制的很长回复 内容完全相同 只是标点和空格略有变化。",
                message_id="copy-2",
                user_id="101",
            ),
        ]
        config = self._vision_config(Path("/tmp/missing.db"), Path("/tmp"))
        config.chat_context_messages = 20
        config.chat_context_minutes = 30
        config.chat_context_self_messages = 3
        config.peer_bot_user_ids = ()
        bot = AsyncMock()
        bot.send_group_msg.return_value = {}
        with patch(
            "plugins.random_chat.matcher.CONFIG", config
        ), patch(
            "plugins.random_chat.matcher.recent_text_context", return_value=history
        ), patch(
            "plugins.random_chat.matcher.archived_message_author", return_value=None
        ), patch(
            "plugins.random_chat.matcher.load_profiles", return_value=[]
        ), patch(
            "plugins.random_chat.matcher.generate_reply",
            new=AsyncMock(return_value="自然回复"),
        ) as generate, patch(
            "plugins.random_chat.matcher.choose_sticker", return_value=None
        ):
            sent = await send_random_reply(bot, _event(), "当前消息")

        self.assertTrue(sent)
        self.assertEqual(
            ["normal", "copy-2"],
            [item.message_id for item in generate.await_args.kwargs["context"]],
        )

    async def test_context_preserves_trigger_for_retained_bot_reply(self):
        history = [
            ContextMessage(
                "提问者", "最初的问题", message_id="trigger", user_id="321"
            ),
            *[
                ContextMessage(
                    f"路人{index}",
                    f"插入消息{index}",
                    message_id=f"other-{index}",
                    user_id=str(500 + index),
                )
                for index in range(6)
            ],
            ContextMessage(
                "机器人自己",
                "针对最初问题的回答",
                message_id="bot-reply",
                user_id="999",
                reply_message_id="trigger",
                is_bot=True,
            ),
            ContextMessage("路人甲", "最新消息一", message_id="latest-1", user_id="801"),
            ContextMessage("路人乙", "最新消息二", message_id="latest-2", user_id="802"),
        ]
        config = self._vision_config(Path("/tmp/missing.db"), Path("/tmp"))
        config.chat_context_messages = 5
        config.chat_context_minutes = 30
        config.chat_context_self_messages = 3
        config.peer_bot_user_ids = ()
        bot = AsyncMock()
        bot.send_group_msg.return_value = {}
        with patch(
            "plugins.random_chat.matcher.CONFIG", config
        ), patch(
            "plugins.random_chat.matcher.recent_text_context", return_value=history
        ), patch(
            "plugins.random_chat.matcher.archived_message_author", return_value=None
        ), patch(
            "plugins.random_chat.matcher.load_profiles", return_value=[]
        ), patch(
            "plugins.random_chat.matcher.generate_reply",
            new=AsyncMock(return_value="自然回复"),
        ) as generate, patch(
            "plugins.random_chat.matcher.choose_sticker", return_value=None
        ):
            sent = await send_random_reply(bot, _event(), "当前消息")

        self.assertTrue(sent)
        selected_ids = [
            item.message_id for item in generate.await_args.kwargs["context"]
        ]
        self.assertEqual(5, len(selected_ids))
        self.assertIn("bot-reply", selected_ids)
        self.assertIn("trigger", selected_ids)

    async def test_context_limit_preserves_quoted_turn_and_latest_current_speaker_turn(self):
        history = [
            ContextMessage(
                "引用者", "最早的关键原话", message_id="111", user_id="321"
            ),
            ContextMessage(
                "成员", "我前面说过的话", message_id="mine", user_id="123"
            ),
            *[
                ContextMessage(
                    f"路人{index}",
                    f"路人消息{index}",
                    message_id=f"other-{index}",
                    user_id=str(500 + index),
                )
                for index in range(8)
            ],
        ]
        config = self._vision_config(Path("/tmp/missing.db"), Path("/tmp"))
        config.chat_context_messages = 5
        config.chat_context_minutes = 30
        config.chat_context_self_messages = 3
        config.peer_bot_user_ids = ()
        bot = AsyncMock()
        bot.send_group_msg.return_value = {}
        with patch(
            "plugins.random_chat.matcher.CONFIG", config
        ), patch(
            "plugins.random_chat.matcher.recent_text_context", return_value=history
        ), patch(
            "plugins.random_chat.matcher.archived_message_author", return_value="321"
        ), patch(
            "plugins.random_chat.matcher.load_profiles", return_value=[]
        ), patch(
            "plugins.random_chat.matcher.generate_reply",
            new=AsyncMock(return_value="自然回复"),
        ) as generate, patch(
            "plugins.random_chat.matcher.choose_sticker", return_value=None
        ):
            sent = await send_random_reply(
                bot,
                _event(
                    Message([MessageSegment.reply(111), MessageSegment.text("接着说")]),
                    reply=_reply(111),
                ),
                "接着说",
                addressed=True,
            )

        self.assertTrue(sent)
        selected_ids = [
            item.message_id for item in generate.await_args.kwargs["context"]
        ]
        self.assertEqual(5, len(selected_ids))
        self.assertIn("111", selected_ids)
        self.assertIn("mine", selected_ids)
        self.assertEqual(
            selected_ids,
            [item.message_id for item in history if item.message_id in selected_ids],
        )

    async def test_same_group_model_calls_are_serialized_before_context_read(self):
        first_started = asyncio.Event()
        release_first = asyncio.Event()
        call_count = 0

        async def generate(*args, **kwargs):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                first_started.set()
                await release_first.wait()
            return "自然回复"

        with patch(
            "plugins.random_chat.matcher.recent_text_context", return_value=[]
        ) as read_context, patch(
            "plugins.random_chat.matcher.archived_message_author", return_value=None
        ), patch(
            "plugins.random_chat.matcher.load_profiles", return_value=[]
        ), patch(
            "plugins.random_chat.matcher.generate_reply", side_effect=generate
        ), patch(
            "plugins.random_chat.matcher.choose_sticker", return_value=None
        ):
            first = asyncio.create_task(
                send_random_reply(AsyncMock(), _event(), "第一条", addressed=True)
            )
            await first_started.wait()
            second_event = _event()
            second_event.message_id = 457
            second = asyncio.create_task(
                send_random_reply(AsyncMock(), second_event, "第二条", addressed=True)
            )
            await asyncio.sleep(0)
            self.assertEqual(1, read_context.call_count)
            release_first.set()
            await asyncio.gather(first, second)

        self.assertEqual(2, read_context.call_count)

    async def test_includes_quoted_text_and_exact_author_when_archive_window_misses_it(self):
        with patch(
            "plugins.random_chat.matcher.recent_text_context", return_value=[]
        ), patch(
            "plugins.random_chat.matcher.archived_message_author", return_value=None
        ), patch(
            "plugins.random_chat.matcher.load_profiles", return_value=[]
        ), patch(
            "plugins.random_chat.matcher.generate_reply",
            new=AsyncMock(return_value="接住引用"),
        ) as generate, patch(
            "plugins.random_chat.matcher.choose_sticker", return_value=None
        ):
            sent = await send_random_reply(
                AsyncMock(),
                _event(Message([MessageSegment.reply(111), MessageSegment.text("然后呢")]), reply=_reply(111)),
                "然后呢",
                addressed=True,
            )

        self.assertTrue(sent)
        current = generate.await_args.kwargs["current"]
        context = generate.await_args.kwargs["context"]
        self.assertEqual("321", current.replied_to_user_id)
        self.assertEqual(1, len(context))
        self.assertEqual("原消息", context[0].text)
        self.assertEqual("321", context[0].user_id)
        self.assertEqual("111", context[0].message_id)

    async def test_current_member_profile_is_requested_before_context_members(self):
        history = [
            ContextMessage("旧成员", "旧话", message_id="1", user_id="111"),
            ContextMessage("近成员", "近话", message_id="2", user_id="222"),
        ]
        with patch(
            "plugins.random_chat.matcher.recent_text_context", return_value=history
        ), patch(
            "plugins.random_chat.matcher.archived_message_author", return_value=None
        ), patch(
            "plugins.random_chat.matcher.load_profiles", return_value=[]
        ) as load, patch(
            "plugins.random_chat.matcher.generate_reply",
            new=AsyncMock(return_value="自然回复"),
        ), patch(
            "plugins.random_chat.matcher.choose_sticker", return_value=None
        ):
            await send_random_reply(AsyncMock(), _event(), "当前消息")

        requested = list(load.call_args.kwargs["user_ids"])
        self.assertEqual("123", requested[0])
        self.assertEqual(["222", "111"], requested[1:])

    async def test_group_chat_passes_only_current_members_relationship_state(self):
        relationship = SimpleNamespace(
            state_text="最近聊得很熟",
            open_topics=("继续聊月季",),
            preferred_address="小园丁",
            communication_style="自然简短",
        )
        features = SimpleNamespace(
            snapshot=lambda: SimpleNamespace(
                relationship_state_enabled=True,
                prompt_builder_enabled=True,
            )
        )
        relationship_calls: list[dict[str, object]] = []

        def get_group(**kwargs):
            relationship_calls.append(kwargs)
            return relationship

        relationship_store = SimpleNamespace(get_group=get_group)
        with patch(
            "plugins.random_chat.matcher.recent_text_context", return_value=[]
        ), patch(
            "plugins.random_chat.matcher.archived_message_author", return_value=None
        ), patch(
            "plugins.random_chat.matcher.load_profiles", return_value=[]
        ), patch(
            "plugins.random_chat.matcher.FEATURES", features, create=True
        ), patch(
            "plugins.private_memory.relationship.RelationshipStore",
            return_value=relationship_store,
        ) as store_type, patch(
            "plugins.random_chat.matcher.generate_reply",
            new=AsyncMock(return_value="自然回复"),
        ) as generate, patch(
            "plugins.random_chat.matcher.choose_sticker", return_value=None
        ):
            sent = await send_random_reply(AsyncMock(), _event(), "当前消息")

        self.assertTrue(sent)
        store_type.assert_called_once()
        self.assertEqual(
            [{"group_id": 789, "user_id": "123", "persona_id": "radish-cat"}],
            relationship_calls,
        )
        self.assertIs(relationship, generate.await_args.kwargs["relationship"])
        self.assertEqual(
            ("继续聊月季",), generate.await_args.kwargs["open_topics"]
        )

    async def test_group_legacy_prompt_does_not_read_new_relationship_state(self):
        features = SimpleNamespace(
            snapshot=lambda: SimpleNamespace(
                relationship_state_enabled=True,
                prompt_builder_enabled=False,
            )
        )
        with patch(
            "plugins.random_chat.matcher.recent_text_context", return_value=[]
        ), patch(
            "plugins.random_chat.matcher.archived_message_author", return_value=None
        ), patch(
            "plugins.random_chat.matcher.load_profiles", return_value=[]
        ), patch(
            "plugins.random_chat.matcher.FEATURES", features
        ), patch(
            "plugins.private_memory.relationship.RelationshipStore",
        ) as store_type, patch(
            "plugins.random_chat.matcher.generate_reply",
            new=AsyncMock(return_value="自然回复"),
        ), patch(
            "plugins.random_chat.matcher.choose_sticker", return_value=None
        ):
            sent = await send_random_reply(AsyncMock(), _event(), "当前消息")

        self.assertTrue(sent)
        store_type.assert_not_called()

    async def test_appends_one_selected_sticker_to_the_same_message(self):
        bot = AsyncMock()
        sticker = Path("/tmp/radish-cat.gif")
        with patch(
            "plugins.random_chat.matcher.recent_text_context", return_value=[]
        ), patch(
            "plugins.random_chat.matcher.archived_message_author", return_value=None
        ), patch(
            "plugins.random_chat.matcher.load_profiles", return_value=[]
        ), patch(
            "plugins.random_chat.matcher.generate_reply",
            new=AsyncMock(return_value="给你一朵小花"),
        ), patch(
            "plugins.random_chat.matcher.choose_sticker", return_value=sticker
        ):
            sent = await send_random_reply(bot, _event(), "当前消息", addressed=True)

        self.assertTrue(sent)
        message = bot.send_group_msg.await_args.kwargs["message"]
        self.assertEqual("text", message[0].type)
        self.assertEqual("给你一朵小花", message[0].data["text"])
        self.assertEqual("image", message[1].type)
        self.assertEqual("file:///tmp/radish-cat.gif", message[1].data["file"])

    async def test_text_still_sends_when_no_sticker_is_selected(self):
        bot = AsyncMock()
        bot.send_group_msg.return_value = {"message_id": 7001}
        with patch(
            "plugins.random_chat.matcher.recent_text_context", return_value=[]
        ), patch(
            "plugins.random_chat.matcher.archived_message_author", return_value=None
        ), patch(
            "plugins.random_chat.matcher.load_profiles", return_value=[]
        ), patch(
            "plugins.random_chat.matcher.generate_reply",
            new=AsyncMock(return_value="自然回复"),
        ), patch(
            "plugins.random_chat.matcher.choose_sticker", return_value=None
        ), patch(
            "plugins.random_chat.matcher.archive_payload", return_value=True
        ) as archive:
            sent = await send_random_reply(bot, _event(), "当前消息")

        self.assertTrue(sent)
        self.assertEqual("自然回复", bot.send_group_msg.await_args.kwargs["message"])
        payload = archive.call_args.args[2]
        self.assertEqual("999", payload["user_id"])
        self.assertEqual("自然回复", payload["plaintext"])
        self.assertEqual("机器人自己", payload["sender"]["nickname"])
        self.assertEqual("456", payload["reply_message_id"])

    async def test_archives_reply_when_adapter_returns_typed_message_id(self):
        bot = AsyncMock()
        bot.send_group_msg.return_value = SimpleNamespace(message_id=7003)
        with patch(
            "plugins.random_chat.matcher.recent_text_context", return_value=[]
        ), patch(
            "plugins.random_chat.matcher.archived_message_author", return_value=None
        ), patch(
            "plugins.random_chat.matcher.load_profiles", return_value=[]
        ), patch(
            "plugins.random_chat.matcher.generate_reply",
            new=AsyncMock(return_value="自然回复"),
        ), patch(
            "plugins.random_chat.matcher.choose_sticker", return_value=None
        ), patch(
            "plugins.random_chat.matcher.archive_payload", return_value=True
        ) as archive:
            sent = await send_random_reply(bot, _event(), "当前消息")

        self.assertTrue(sent)
        self.assertEqual("7003", archive.call_args.args[2]["message_id"])

    async def test_bot_reply_archive_failure_does_not_stop_remaining_replies(self):
        bot = AsyncMock()
        bot.send_group_msg.return_value = {"message_id": 7002}
        with patch(
            "plugins.random_chat.matcher.recent_text_context", return_value=[]
        ), patch(
            "plugins.random_chat.matcher.archived_message_author", return_value=None
        ), patch(
            "plugins.random_chat.matcher.load_profiles", return_value=[]
        ), patch(
            "plugins.random_chat.matcher.generate_reply",
            new=AsyncMock(return_value=("第一句", "第二句")),
        ), patch(
            "plugins.random_chat.matcher.choose_sticker", return_value=None
        ), patch(
            "plugins.random_chat.matcher.archive_payload",
            side_effect=OSError("archive failed"),
        ):
            sent = await send_random_reply(
                bot, _event(), "当前消息", addressed=True
            )

        self.assertTrue(sent)
        self.assertEqual(2, bot.send_group_msg.await_count)

    async def test_passes_current_and_unexpired_quoted_originals_to_ai(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "data" / "chat_vision" / "images"
            root.mkdir(parents=True)
            database = Path(directory) / "chat.db"
            store = ChatVisionStore(database)
            self._stored_image(
                store,
                root,
                message_id="456",
                ordinal=1,
                content=b"current-raw",
                description="当前图片是一朵花",
            )
            self._stored_image(
                store,
                root,
                message_id="111",
                ordinal=1,
                content=b"quoted-raw",
                description="引用图片是一只蝴蝶",
            )
            self._stored_image(
                store,
                root,
                message_id="111",
                ordinal=2,
                content=b"expired-raw",
                description="已过期图片仍有永久描述",
                expires_at="2020-08-20 00:00:00",
            )
            context = [
                ContextMessage(
                    "引用者",
                    "[图片]",
                    message_id="111",
                    user_id="321",
                    image_descriptions=(
                        "引用图片是一只蝴蝶",
                        "已过期图片仍有永久描述",
                    ),
                )
            ]
            processed_message = Message(
                [MessageSegment.image("https://example.invalid/current.jpg")]
            )
            original_message = Message(
                [
                    MessageSegment.reply(222),
                    MessageSegment.image("https://example.invalid/current.jpg"),
                ]
            )
            bot = AsyncMock()
            with patch(
                "plugins.random_chat.matcher.CONFIG",
                self._vision_config(database, root),
            ), patch(
                "plugins.random_chat.matcher.recent_text_context", return_value=context
            ), patch(
                "plugins.random_chat.matcher.archived_message_author", return_value="321"
            ), patch(
                "plugins.random_chat.matcher.load_profiles", return_value=[]
            ), patch(
                "plugins.random_chat.matcher.generate_reply",
                new=AsyncMock(return_value="看到了"),
            ) as generate, patch(
                "plugins.random_chat.matcher.choose_sticker", return_value=None
            ):
                sent = await send_random_reply(
                    bot,
                    _event(
                        processed_message,
                        original_message=original_message,
                        reply=_reply(111),
                    ),
                    "",
                )

        self.assertTrue(sent)
        generated = generate.await_args.kwargs
        self.assertIn("images", generated)
        if "images" not in generated:
            return
        self.assertEqual(
            [b"current-raw", b"quoted-raw"],
            [item.content for item in generated["images"]],
        )
        self.assertEqual(
            ["456", "111"],
            [item.message_id for item in generated["images"]],
        )
        self.assertEqual("[图片]", generated["current"].text)
        self.assertEqual(
            ("当前图片是一朵花",), generated["current"].image_descriptions
        )
        self.assertEqual(context, generated["context"])
        self.assertIn(
            "已过期图片仍有永久描述",
            generated["context"][0].image_descriptions,
        )

    async def test_deduplicates_originals_by_asset_id(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "data" / "chat_vision" / "images"
            root.mkdir(parents=True)
            database = Path(directory) / "chat.db"
            store = ChatVisionStore(database)
            self._stored_image(
                store,
                root,
                message_id="456",
                ordinal=1,
                content=b"same-asset",
                description="同一张图",
            )
            processed_message = Message(
                [MessageSegment.image("https://example.invalid/current.jpg")]
            )
            original_message = Message(
                [
                    MessageSegment.reply(456),
                    MessageSegment.image("https://example.invalid/current.jpg"),
                ]
            )
            with patch(
                "plugins.random_chat.matcher.CONFIG",
                self._vision_config(database, root),
            ), patch(
                "plugins.random_chat.matcher.recent_text_context", return_value=[]
            ), patch(
                "plugins.random_chat.matcher.archived_message_author", return_value="123"
            ), patch(
                "plugins.random_chat.matcher.load_profiles", return_value=[]
            ), patch(
                "plugins.random_chat.matcher.generate_reply",
                new=AsyncMock(return_value="看到了"),
            ) as generate, patch(
                "plugins.random_chat.matcher.choose_sticker", return_value=None
            ):
                await send_random_reply(
                    AsyncMock(),
                    _event(processed_message, original_message=original_message),
                    "",
                )

        generated = generate.await_args.kwargs
        self.assertIn("images", generated)
        if "images" not in generated:
            return
        self.assertEqual(1, len(generated["images"]))
        self.assertEqual("456", generated["current"].reply_message_id)

    async def test_direct_vision_failure_returns_false_without_sending(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "data" / "chat_vision" / "images"
            root.mkdir(parents=True)
            database = Path(directory) / "chat.db"
            store = ChatVisionStore(database)
            self._stored_image(
                store,
                root,
                message_id="456",
                ordinal=1,
                content=b"current-raw",
                description="当前图片是一朵花",
            )
            message = Message(
                [MessageSegment.image("https://example.invalid/current.jpg")]
            )
            bot = AsyncMock()
            with patch(
                "plugins.random_chat.matcher.CONFIG",
                self._vision_config(database, root),
            ), patch(
                "plugins.random_chat.matcher.recent_text_context", return_value=[]
            ), patch(
                "plugins.random_chat.matcher.archived_message_author", return_value=None
            ), patch(
                "plugins.random_chat.matcher.load_profiles", return_value=[]
            ), patch(
                "plugins.random_chat.matcher.generate_reply",
                new=AsyncMock(side_effect=RandomChatAIError("vision failed")),
            ) as generate:
                sent = await send_random_reply(bot, _event(message), "")

        self.assertFalse(sent)
        bot.send_group_msg.assert_not_awaited()
        self.assertIn("images", generate.await_args.kwargs)
        if "images" in generate.await_args.kwargs:
            self.assertEqual(1, len(generate.await_args.kwargs["images"]))

    async def test_pure_image_without_available_current_original_returns_false(self):
        for mode in ("missing", "expired", "read_error"):
            with self.subTest(mode=mode), tempfile.TemporaryDirectory() as directory:
                root = Path(directory) / "data" / "chat_vision" / "images"
                root.mkdir(parents=True)
                database = Path(directory) / "chat.db"
                store = ChatVisionStore(database)
                if mode != "missing":
                    self._stored_image(
                        store,
                        root,
                        message_id="456",
                        ordinal=1,
                        content=b"expired-current",
                        description="已经过期的当前图片描述",
                        expires_at=(
                            "2020-08-20 00:00:00"
                            if mode == "expired"
                            else "2099-08-20 00:00:00"
                        ),
                    )
                message = Message(
                    [MessageSegment.image("https://example.invalid/current.jpg")]
                )
                bot = AsyncMock()
                read_original = (
                    patch(
                        "plugins.chat_vision.store.read_original_image",
                        side_effect=OSError("read failed"),
                    )
                    if mode == "read_error"
                    else nullcontext()
                )
                with read_original, patch(
                    "plugins.random_chat.matcher.CONFIG",
                    self._vision_config(database, root),
                ), patch(
                    "plugins.random_chat.matcher.recent_text_context", return_value=[]
                ), patch(
                    "plugins.random_chat.matcher.archived_message_author",
                    return_value=None,
                ), patch(
                    "plugins.random_chat.matcher.load_profiles", return_value=[]
                ) as profiles, patch(
                    "plugins.random_chat.matcher.generate_reply",
                    new=AsyncMock(return_value="我看到了花"),
                ) as generate:
                    sent = await send_random_reply(bot, _event(message), "")

                self.assertFalse(sent)
                generate.assert_not_awaited()
                profiles.assert_not_called()
                bot.send_group_msg.assert_not_awaited()

    async def test_mixed_image_without_original_degrades_to_real_text(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "data" / "chat_vision" / "images"
            root.mkdir(parents=True)
            database = Path(directory) / "chat.db"
            store = ChatVisionStore(database)
            self._stored_image(
                store,
                root,
                message_id="456",
                ordinal=1,
                content=b"expired-current",
                description="不可用的当前图片描述",
                expires_at="2020-08-20 00:00:00",
            )
            self._stored_image(
                store,
                root,
                message_id="111",
                ordinal=1,
                content=b"quoted-raw",
                description="引用图片描述",
            )
            processed_message = Message(
                [
                    MessageSegment.image("https://example.invalid/current.jpg"),
                    MessageSegment.text("你觉得这个配色怎么样"),
                ]
            )
            original_message = Message(
                [
                    MessageSegment.reply(111),
                    MessageSegment.image("https://example.invalid/current.jpg"),
                    MessageSegment.text("你觉得这个配色怎么样"),
                ]
            )
            bot = AsyncMock()
            with patch(
                "plugins.random_chat.matcher.CONFIG",
                self._vision_config(database, root),
            ), patch(
                "plugins.random_chat.matcher.recent_text_context", return_value=[]
            ), patch(
                "plugins.random_chat.matcher.archived_message_author", return_value="321"
            ), patch(
                "plugins.random_chat.matcher.load_profiles", return_value=[]
            ), patch(
                "plugins.random_chat.matcher.generate_reply",
                new=AsyncMock(return_value="我没拿到图片，只能先说配色要看整体。"),
            ) as generate, patch(
                "plugins.random_chat.matcher.choose_sticker", return_value=None
            ):
                sent = await send_random_reply(
                    bot,
                    _event(
                        processed_message,
                        original_message=original_message,
                        reply=_reply(111),
                    ),
                    "你觉得这个配色怎么样",
                )

        self.assertTrue(sent)
        self.assertEqual((), tuple(generate.await_args.kwargs["images"]))
        self.assertEqual("你觉得这个配色怎么样", generate.await_args.args[0])
        self.assertEqual(
            "你觉得这个配色怎么样", generate.await_args.kwargs["current"].text
        )
        self.assertEqual((), generate.await_args.kwargs["current"].image_descriptions)

    async def test_empty_text_without_image_does_not_use_image_placeholder(self):
        message = Message([MessageSegment.at(999)])
        with patch(
            "plugins.random_chat.matcher.recent_text_context", return_value=[]
        ), patch(
            "plugins.random_chat.matcher.archived_message_author", return_value=None
        ), patch(
            "plugins.random_chat.matcher.load_profiles", return_value=[]
        ), patch(
            "plugins.random_chat.matcher.generate_reply",
            new=AsyncMock(return_value=None),
        ) as generate:
            sent = await send_random_reply(AsyncMock(), _event(message), "", addressed=True)

        self.assertFalse(sent)
        self.assertEqual("", generate.await_args.args[0])
        self.assertEqual("", generate.await_args.kwargs["current"].text)

    async def test_explicit_old_quoted_image_description_is_added_to_prompt(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "data" / "chat_vision" / "images"
            root.mkdir(parents=True)
            database = Path(directory) / "chat.db"
            store = ChatVisionStore(database)
            self._stored_image(
                store,
                root,
                message_id="111",
                ordinal=1,
                content=b"expired-quoted",
                description="一张很久以前的白花照片",
                expires_at="2020-08-20 00:00:00",
            )
            processed_message = Message("这张图是什么")
            original_message = Message(
                [MessageSegment.reply(111), MessageSegment.text("这张图是什么")]
            )
            with patch(
                "plugins.random_chat.matcher.CONFIG",
                self._vision_config(database, root),
            ), patch(
                "plugins.random_chat.matcher.recent_text_context", return_value=[]
            ), patch(
                "plugins.random_chat.matcher.archived_message_author", return_value="321"
            ), patch(
                "plugins.random_chat.matcher.load_profiles", return_value=[]
            ), patch(
                "plugins.random_chat.matcher.generate_reply",
                new=AsyncMock(return_value=None),
            ) as generate:
                await send_random_reply(
                    AsyncMock(),
                    _event(
                        processed_message,
                        original_message=original_message,
                        reply=_reply(111),
                    ),
                    "这张图是什么",
                    addressed=True,
                )

        self.assertEqual((), tuple(generate.await_args.kwargs["images"]))
        quoted = [
            item
            for item in generate.await_args.kwargs["context"]
            if item.message_id == "111"
        ]
        self.assertEqual(1, len(quoted))
        self.assertEqual(
            ("一张很久以前的白花照片",), quoted[0].image_descriptions
        )

    async def test_archive_ai_and_send_failures_do_not_escape(self):
        bot = AsyncMock()
        bot.send_group_msg.side_effect = RuntimeError("send failed")
        with patch(
            "plugins.random_chat.matcher.recent_text_context",
            side_effect=RuntimeError("archive failed"),
        ), patch(
            "plugins.random_chat.matcher.archived_message_author", return_value=None
        ), patch(
            "plugins.random_chat.matcher.load_profiles", return_value=[]
        ), patch(
            "plugins.random_chat.matcher.generate_reply",
            new=AsyncMock(return_value="回复"),
        ) as generate:
            await send_random_reply(bot, _event(), "当前消息")
        self.assertEqual([], generate.await_args.kwargs["context"])


if __name__ == "__main__":
    unittest.main()
