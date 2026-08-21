import os
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

os.environ.setdefault("TARGET_GROUP_ID", "999000111")

from nonebot.adapters.onebot.v11 import GroupMessageEvent, Message, MessageSegment

from plugins.violation_record import matcher as violation_matcher
from plugins.violation_record.schemas import DEFAULT_INTENT


def _event(text: str = "你叫什么") -> GroupMessageEvent:
    message = Message([MessageSegment.at(999), MessageSegment.text(text)])
    return GroupMessageEvent(
        time=2000,
        self_id=999,
        post_type="message",
        sub_type="normal",
        user_id=123,
        message_type="group",
        message_id=456,
        group_id=999000111,
        message=message,
        original_message=message,
        raw_message=str(message),
        font=0,
        sender={"user_id": 123, "nickname": "成员", "role": "member"},
    )


def _reply_event(text: str = "接着说") -> GroupMessageEvent:
    message = Message([MessageSegment.reply(111), MessageSegment.text(text)])
    return GroupMessageEvent(
        time=2000,
        self_id=999,
        post_type="message",
        sub_type="normal",
        user_id=123,
        message_type="group",
        message_id=456,
        group_id=999000111,
        message=message,
        original_message=message,
        raw_message=str(message),
        font=0,
        sender={"user_id": 123, "nickname": "成员", "role": "member"},
        reply={
            "time": 1999,
            "message_type": "group",
            "message_id": 111,
            "real_id": 111,
            "sender": {"user_id": 999, "nickname": "萝卜猫"},
            "message": Message("上一条机器人回复"),
        },
    )


class BusinessFallbackTests(unittest.IsolatedAsyncioTestCase):
    def test_replying_to_bot_counts_as_direct_address(self):
        with patch.object(
            violation_matcher,
            "CONFIG",
            SimpleNamespace(bot_self_id="", random_chat_direct_fallback_enabled=True),
        ):
            self.assertTrue(violation_matcher._is_at_me(_reply_event()))

    async def test_unknown_business_message_returns_false_without_importing_chat(self):
        bot = AsyncMock()
        intent = DEFAULT_INTENT | {"intent": "unknown"}
        with patch.object(violation_matcher, "grant_admin"), patch.object(
            violation_matcher, "_sync_group_admins", new=AsyncMock()
        ), patch.object(
            violation_matcher, "handle_policy_text", return_value=None
        ), patch.object(
            violation_matcher, "_referenced_message_time", new=AsyncMock(return_value=None)
        ), patch.object(
            violation_matcher, "parse_intent", new=AsyncMock(return_value=intent)
        ), patch.object(
            violation_matcher,
            "handle_intent",
            new=AsyncMock(),
        ) as handle_intent:
            handled = await violation_matcher.handle_business_message(
                bot, _event(), "你叫什么"
            )

        self.assertFalse(handled)
        handle_intent.assert_not_awaited()
        bot.send_group_msg.assert_not_awaited()

    async def test_known_business_intent_sends_reply_and_returns_true(self):
        bot = AsyncMock()
        intent = DEFAULT_INTENT | {"intent": "help"}
        with patch.object(violation_matcher, "grant_admin"), patch.object(
            violation_matcher, "_sync_group_admins", new=AsyncMock()
        ), patch.object(
            violation_matcher, "handle_policy_text", return_value=None
        ), patch.object(
            violation_matcher, "_referenced_message_time", new=AsyncMock(return_value=None)
        ), patch.object(
            violation_matcher, "parse_intent", new=AsyncMock(return_value=intent)
        ), patch.object(
            violation_matcher, "handle_intent", new=AsyncMock(return_value="业务帮助")
        ):
            handled = await violation_matcher.handle_business_message(
                bot, _event("帮助"), "帮助"
            )

        self.assertTrue(handled)
        bot.send_group_msg.assert_awaited_once_with(
            group_id=999000111, message="业务帮助"
        )


if __name__ == "__main__":
    unittest.main()
