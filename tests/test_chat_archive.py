from __future__ import annotations

import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import nonebot
from nonebot.adapters.onebot.v11 import GroupMessageEvent, Message

try:
    nonebot.get_driver()
except ValueError:
    nonebot.init()

from plugins.chat_archive.db import archive_payload
from plugins.chat_archive import matcher as archive_matcher
from plugins.violation_record import matcher as violation_matcher


def _group_event(group_id: int) -> GroupMessageEvent:
    message = Message("边界测试消息")
    return GroupMessageEvent(
        time=1785168002,
        self_id=10000,
        post_type="message",
        sub_type="normal",
        user_id=456791,
        message_type="group",
        message_id=104,
        group_id=group_id,
        message=message,
        original_message=message,
        raw_message="边界测试消息",
        font=0,
        sender={"user_id": 456791, "nickname": "外群成员", "role": "member"},
    )


class ChatArchiveTests(unittest.IsolatedAsyncioTestCase):
    async def test_non_target_event_is_rejected_before_all_custom_processing(self) -> None:
        event = _group_event(987654321)

        with (
            patch.object(violation_matcher, "parse_intent") as parse_intent,
            patch.object(violation_matcher, "grant_admin") as grant_admin,
            patch.object(
                violation_matcher,
                "capture_referenced_images",
            ) as capture_referenced_images,
            patch.object(archive_matcher, "archive_payload") as archive_insert,
        ):
            self.assertFalse(await violation_matcher.only_allowed_group(event))
            self.assertFalse(archive_matcher._target_group(event))

        parse_intent.assert_not_called()
        grant_admin.assert_not_called()
        capture_referenced_images.assert_not_called()
        archive_insert.assert_not_called()

    def test_only_target_group_is_archived_idempotently(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "chat.db"
            payload = {
                "message_id": "101",
                "group_id": 123456789,
                "event_time": 1785168000,
                "user_id": "456789",
                "sender": {"card": "记录员"},
                "segments": [{"type": "text", "data": {"text": "证据"}}],
                "plaintext": "证据",
                "reply_message_id": "99",
            }
            self.assertTrue(archive_payload(path, 123456789, payload))
            self.assertTrue(archive_payload(path, 123456789, payload))
            outside = dict(payload, message_id="102", group_id=987654321)
            self.assertFalse(archive_payload(path, 123456789, outside))
            with sqlite3.connect(path) as conn:
                row = conn.execute(
                    "SELECT group_id,user_id,message_json,reply_message_id FROM chat_messages"
                ).fetchone()
                count = conn.execute("SELECT COUNT(*) FROM chat_messages").fetchone()[0]
            self.assertEqual(1, count)
            self.assertEqual((123456789, "456789"), row[:2])
            self.assertIn('"type": "text"', row[2])
            self.assertEqual("99", row[3])

    def test_target_archive_preserves_original_metadata(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "chat.db"
            sender = {"card": "记录员", "nickname": "原始昵称", "role": "admin"}
            segments = [
                {"type": "reply", "data": {"id": "88"}},
                {"type": "image", "data": {"file": "proof.jpg", "url": "https://example.invalid/proof.jpg"}},
            ]
            payload = {
                "message_id": "103",
                "group_id": 123456789,
                "event_time": 1785168001,
                "user_id": "456790",
                "sender": sender,
                "segments": segments,
                "plaintext": "引用图片",
                "reply_message_id": "88",
            }
            self.assertTrue(archive_payload(path, 123456789, payload))
            with sqlite3.connect(path) as conn:
                row = conn.execute(
                    "SELECT event_time,sender_json,message_json,plaintext,reply_message_id "
                    "FROM chat_messages WHERE message_id='103'"
                ).fetchone()
            self.assertEqual(1785168001, row[0])
            self.assertEqual(sender, json.loads(row[1]))
            self.assertEqual(segments, json.loads(row[2]))
            self.assertEqual("引用图片", row[3])
            self.assertEqual("88", row[4])


if __name__ == "__main__":
    unittest.main()
