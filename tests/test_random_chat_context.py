import json
import os
import sqlite3
import tempfile
import unittest
from pathlib import Path

os.environ.setdefault("TARGET_GROUP_ID", "999000111")

from plugins.chat_archive.db import SCHEMA, ContextMessage, recent_text_context


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
                    "[]",
                    text,
                    None,
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
                ContextMessage("群名片", "火锅"),
                ContextMessage("小红", "同意"),
                ContextMessage("7", "走起"),
            ],
            result,
        )

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


if __name__ == "__main__":
    unittest.main()
