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
from plugins.feature_control.state import FeatureController, FeatureState


def _group_event(group_id: int, *, reply: dict | None = None) -> GroupMessageEvent:
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
        reply=reply,
    )


class ChatArchiveTests(unittest.IsolatedAsyncioTestCase):
    def _controller(
        self, directory: str, *, allowed: tuple[int, ...], chat_enabled: bool = True
    ) -> FeatureController:
        return FeatureController(
            Path(directory) / "features.json",
            FeatureState(
                business_enabled=True,
                chat_enabled=chat_enabled,
                group_chat_enabled=True,
                private_chat_enabled=False,
                group_chat_allowed_group_ids=allowed,
                private_chat_allowed_user_ids=(),
            ),
        )

    async def test_disallowed_group_is_rejected_before_archive_processing(self) -> None:
        event = _group_event(987654321)

        with tempfile.TemporaryDirectory() as directory, patch.object(
            archive_matcher,
            "FEATURES",
            self._controller(directory, allowed=(123456789,)),
        ), patch.object(archive_matcher, "archive_payload") as archive_insert:
            self.assertFalse(archive_matcher._chat_group(event))

        archive_insert.assert_not_called()

    async def test_allowed_group_archive_uses_actual_group_and_updates_identity(self) -> None:
        group_id = 987654321
        event = _group_event(group_id)
        with tempfile.TemporaryDirectory() as directory, patch.object(
            archive_matcher,
            "FEATURES",
            self._controller(directory, allowed=(group_id,)),
        ), patch.object(
            archive_matcher, "archive_payload", return_value=True
        ) as archive_insert, patch.object(
            archive_matcher, "remember_identity"
        ) as remember:
            self.assertTrue(archive_matcher._chat_group(event))
            await archive_matcher.archive_chat_message(event)

        self.assertEqual(group_id, archive_insert.call_args.args[1])
        remember.assert_called_once()
        self.assertEqual("456791", remember.call_args.kwargs["user_id"])

        with tempfile.TemporaryDirectory() as directory, patch.object(
            archive_matcher,
            "FEATURES",
            self._controller(directory, allowed=(group_id,)),
        ), patch.object(archive_matcher, "archive_payload", return_value=True), patch.object(
            archive_matcher, "remember_identity", side_effect=RuntimeError("memory failed")
        ):
            await archive_matcher.archive_chat_message(event)

    async def test_reply_metadata_is_archived_even_when_message_has_no_reply_segment(self) -> None:
        group_id = 987654321
        event = _group_event(
            group_id,
            reply={
                "time": 1785167000,
                "message_type": "group",
                "message_id": 88,
                "real_id": 88,
                "sender": {"user_id": 123, "nickname": "被引用者"},
                "message": Message("被引用原文"),
            },
        )
        with tempfile.TemporaryDirectory() as directory, patch.object(
            archive_matcher,
            "FEATURES",
            self._controller(directory, allowed=(group_id,)),
        ), patch.object(
            archive_matcher, "archive_payload", return_value=True
        ) as archive_insert, patch.object(archive_matcher, "remember_identity"):
            await archive_matcher.archive_chat_message(event)

        self.assertEqual("88", archive_insert.call_args.args[2]["reply_message_id"])

    async def test_global_chat_switch_blocks_archive_candidate(self) -> None:
        group_id = 987654321
        event = _group_event(group_id)
        with tempfile.TemporaryDirectory() as directory, patch.object(
            archive_matcher,
            "FEATURES",
            self._controller(directory, allowed=(group_id,), chat_enabled=False),
        ):
            self.assertFalse(archive_matcher._chat_group(event))

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
