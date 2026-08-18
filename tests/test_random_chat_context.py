import json
import os
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

os.environ.setdefault("TARGET_GROUP_ID", "999000111")

from nonebot.adapters.onebot.v11 import GroupMessageEvent, Message

from plugins.chat_archive.db import SCHEMA, ContextMessage, recent_text_context
from plugins.random_chat import matcher as random_chat_matcher
from plugins.random_chat.matcher import send_random_reply
from plugins.violation_record.config import CONFIG


class RecentTextContextTests(unittest.TestCase):
    def _database(self, directory: str) -> Path:
        path = Path(directory) / "chat.db"
        with sqlite3.connect(path) as conn:
            conn.executescript(SCHEMA)
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
        with sqlite3.connect(path) as conn:
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

    def test_filters_and_formats_recent_context(self):
        with tempfile.TemporaryDirectory() as directory:
            path = self._database(directory)
            self._insert(path, message_id="old", event_time=999, user_id="1", text="过期")
            self._insert(path, message_id="other", group_id=456, event_time=1001, user_id="2", text="外群")
            self._insert(path, message_id="bot", event_time=1002, user_id="999", text="机器人")
            self._insert(path, message_id="blank", event_time=1003, user_id="3", text="   ")
            self._insert(path, message_id="command", event_time=1004, user_id="3", text=" /help")
            self._insert(path, message_id="current", event_time=1005, user_id="4", text="当前消息")
            self._insert(path, message_id="a", event_time=1006, user_id="5", text="火锅", card="群名片", nickname="昵称")
            self._insert(path, message_id="b", event_time=1007, user_id="6", text="同意", nickname="小红")
            self._insert(path, message_id="c", event_time=1008, user_id="7", text="走起")

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


def _event() -> GroupMessageEvent:
    message = Message("当前消息")
    return GroupMessageEvent(
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
    )


class RandomChatIntegrationTests(unittest.IsolatedAsyncioTestCase):
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
        self.assertEqual(20, kwargs["limit"])
        self.assertEqual("456", kwargs["exclude_message_id"])
        self.assertEqual("999", kwargs["bot_user_id"])
        generated = generate.await_args.kwargs
        self.assertEqual(context, generated["context"])
        self.assertEqual("123", generated["current"].user_id)
        self.assertEqual([], generated["profiles"])
        bot.send_group_msg.assert_awaited_once_with(group_id=789, message="自然回复")
        extract.assert_not_awaited()
        apply.assert_not_called()

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
        ):
            sent = await send_random_reply(bot, _event(), "当前消息")

        self.assertTrue(sent)
        self.assertEqual("自然回复", bot.send_group_msg.await_args.kwargs["message"])

    async def test_probability_gate_uses_config_and_skips_send_when_false(self):
        bot = AsyncMock()
        with patch(
            "plugins.random_chat.matcher.should_reply", return_value=False
        ) as should_reply, patch(
            "plugins.random_chat.matcher.send_random_reply", new=AsyncMock()
        ) as send:
            await random_chat_matcher._(bot, _event())

        should_reply.assert_called_once_with(CONFIG.random_chat_probability)
        send.assert_not_awaited()

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
