import os
import asyncio
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, Mock, patch

os.environ.setdefault("TARGET_GROUP_ID", "999000111")

from plugins.chat_archive.db import ContextMessage
from plugins.feature_control.state import FeatureController, FeatureState
from plugins.private_chat.conversation import PrivateConversation
from plugins.private_chat.policy import eligible_private_text, is_private_candidate
from nonebot.adapters.onebot.v11 import Message, PrivateMessageEvent

from plugins.private_chat import matcher as private_matcher


def _private_event(text: str, *, user_id: int = 123456) -> PrivateMessageEvent:
    message = Message(text)
    return PrivateMessageEvent(
        time=2000,
        self_id=999999,
        post_type="message",
        sub_type="friend",
        user_id=user_id,
        message_type="private",
        message_id=456,
        message=message,
        original_message=message,
        raw_message=text,
        font=0,
        sender={"user_id": user_id, "nickname": "测试者"},
    )


class PrivateChatPolicyTests(unittest.TestCase):
    def test_configuration_defaults_are_safe(self):
        env = os.environ.copy()
        env["TARGET_GROUP_ID"] = "999000111"
        env.pop("PRIVATE_CHAT_ENABLED", None)
        env.pop("PRIVATE_CHAT_ALLOWED_USER_ID", None)
        completed = subprocess.run(
            [
                sys.executable,
                "-c",
                (
                    "from plugins.violation_record.config import CONFIG; "
                    "assert CONFIG.private_chat_enabled is False; "
                    "assert CONFIG.private_chat_allowed_user_id == ''"
                ),
            ],
            env=env,
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(0, completed.returncode, completed.stderr)

    def test_candidate_requires_enabled_exact_human_sender(self):
        self.assertTrue(is_private_candidate(True, "123456", "123456", "999999"))
        self.assertTrue(
            is_private_candidate(True, "123456, 654321", "654321", "999999")
        )
        self.assertFalse(is_private_candidate(False, "123456", "123456", "999999"))
        self.assertFalse(is_private_candidate(True, "123456", "654321", "999999"))
        self.assertFalse(
            is_private_candidate(True, "123456, 654321", "111111", "999999")
        )
        self.assertFalse(is_private_candidate(True, "", "123456", "999999"))
        self.assertFalse(is_private_candidate(True, "abc", "abc", "999999"))
        self.assertFalse(is_private_candidate(True, "999999", "999999", "999999"))

    def test_private_text_rejects_blank_and_commands(self):
        self.assertIsNone(eligible_private_text("   "))
        self.assertIsNone(eligible_private_text(" /help "))
        self.assertEqual("你好", eligible_private_text(" 你好 "))


class PrivateConversationTests(unittest.IsolatedAsyncioTestCase):
    async def test_conversation_keeps_only_twenty_turns(self):
        conversation = PrivateConversation(limit=20)
        for index in range(21):
            conversation.append(
                ContextMessage(
                    str(index),
                    f"消息{index}",
                    message_id=str(index),
                    user_id=str(index),
                )
            )

        snapshot = conversation.snapshot()
        self.assertEqual(20, len(snapshot))
        self.assertEqual("1", snapshot[0].message_id)
        self.assertEqual("20", snapshot[-1].message_id)
        self.assertFalse(conversation.lock.locked())


class PrivateChatMatcherTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        state = FeatureState(
            business_enabled=True,
            chat_enabled=True,
            group_chat_enabled=False,
            private_chat_enabled=True,
            group_chat_allowed_group_ids=(),
            private_chat_allowed_user_ids=("123456", "654321"),
            private_memory_enabled=False,
            relationship_state_enabled=False,
        )
        features = Mock()
        features.snapshot.return_value = state
        features.private_chat_allowed.side_effect = lambda user_id: str(user_id) in {
            "123456",
            "654321",
        }
        self.features_patcher = patch.object(private_matcher, "FEATURES", features)
        self.features_patcher.start()
        self.addCleanup(self.features_patcher.stop)

    async def test_private_gate_requires_parent_child_and_allowlist(self):
        event = _private_event("你好")

        def controller(
            directory: str,
            *,
            chat_enabled: bool = True,
            private_chat_enabled: bool = True,
            allowed: tuple[str, ...] = ("123456",),
        ) -> FeatureController:
            return FeatureController(
                Path(directory) / "features.json",
                FeatureState(
                    business_enabled=True,
                    chat_enabled=chat_enabled,
                    group_chat_enabled=False,
                    private_chat_enabled=private_chat_enabled,
                    group_chat_allowed_group_ids=(),
                    private_chat_allowed_user_ids=allowed,
                ),
            )

        with tempfile.TemporaryDirectory() as directory:
            for features in (
                controller(directory, chat_enabled=False),
                controller(directory, private_chat_enabled=False),
                controller(directory, allowed=("654321",)),
            ):
                with patch.object(private_matcher, "FEATURES", features):
                    self.assertFalse(await private_matcher.private_chat_candidate(event))

            with patch.object(private_matcher, "FEATURES", controller(directory)):
                self.assertTrue(await private_matcher.private_chat_candidate(event))
                self.assertFalse(
                    await private_matcher.private_chat_candidate(
                        _private_event("你好", user_id=999999)
                    )
                )

    async def test_allowed_text_always_replies_in_private_mode_with_sticker(self):
        bot = AsyncMock()
        conversation = PrivateConversation()
        sticker = Path("/tmp/private-flower.gif")
        with patch.object(private_matcher, "CONVERSATIONS", {"123456": conversation}), patch.object(
            private_matcher,
            "generate_reply",
            new=AsyncMock(return_value="给你一朵小花"),
        ) as generate, patch.object(
            private_matcher, "choose_sticker", return_value=sticker
        ):
            await private_matcher.handle_private_message(bot, _private_event("你好"))

        generate.assert_awaited_once()
        kwargs = generate.await_args.kwargs
        self.assertTrue(kwargs["addressed"])
        self.assertEqual("private", kwargs["chat_mode"])
        self.assertEqual((), kwargs["context"])
        self.assertEqual("你好", kwargs["current"].text)
        sent = bot.send_private_msg.await_args.kwargs
        self.assertEqual(123456, sent["user_id"])
        self.assertEqual("text", sent["message"][0].type)
        self.assertEqual("给你一朵小花", sent["message"][0].data["text"])
        self.assertEqual("image", sent["message"][1].type)
        self.assertEqual("file:///tmp/private-flower.gif", sent["message"][1].data["file"])
        self.assertEqual(["你好", "给你一朵小花"], [item.text for item in conversation.snapshot()])

    async def test_allowed_users_have_isolated_conversations(self):
        bot = AsyncMock()
        conversations: dict[str, PrivateConversation] = {}

        async def reply_side_effect(message, **kwargs):
            return f"回复{message}"

        generate = AsyncMock(side_effect=reply_side_effect)
        with patch.object(
            private_matcher, "CONVERSATIONS", conversations, create=True
        ), patch.object(
            private_matcher, "generate_reply", new=generate
        ), patch.object(private_matcher, "choose_sticker", return_value=None):
            await private_matcher.handle_private_message(
                bot, _private_event("甲的第一条", user_id=123456)
            )
            await private_matcher.handle_private_message(
                bot, _private_event("乙的第一条", user_id=654321)
            )
            await private_matcher.handle_private_message(
                bot, _private_event("甲的第二条", user_id=123456)
            )

        first_context = generate.await_args_list[0].kwargs["context"]
        second_context = generate.await_args_list[1].kwargs["context"]
        third_context = generate.await_args_list[2].kwargs["context"]
        self.assertEqual((), first_context)
        self.assertEqual((), second_context)
        self.assertNotIn("甲的第一条", [item.text for item in second_context])
        self.assertNotIn("回复甲的第一条", [item.text for item in second_context])
        self.assertEqual(
            ["甲的第一条", "回复甲的第一条"],
            [item.text for item in third_context],
        )

    async def test_blank_and_command_messages_do_not_call_ai(self):
        bot = AsyncMock()
        generate = AsyncMock(return_value="不应回复")
        with patch.object(private_matcher, "CONVERSATIONS", {}), patch.object(
            private_matcher, "generate_reply", new=generate
        ):
            await private_matcher.handle_private_message(bot, _private_event("   "))
            await private_matcher.handle_private_message(bot, _private_event("/help"))

        generate.assert_not_awaited()
        bot.send_private_msg.assert_not_awaited()

    async def test_ai_and_send_failures_do_not_escape_or_add_fake_bot_turn(self):
        bot = AsyncMock()
        conversation = PrivateConversation()
        with patch.object(private_matcher, "CONVERSATIONS", {"123456": conversation}), patch.object(
            private_matcher,
            "generate_reply",
            new=AsyncMock(side_effect=private_matcher.RandomChatAIError("down")),
        ):
            await private_matcher.handle_private_message(bot, _private_event("第一条"))

        self.assertEqual(["第一条"], [item.text for item in conversation.snapshot()])
        bot.send_private_msg.assert_not_awaited()

        bot.send_private_msg.side_effect = RuntimeError("send failed")
        with patch.object(private_matcher, "CONVERSATIONS", {"123456": conversation}), patch.object(
            private_matcher,
            "generate_reply",
            new=AsyncMock(return_value="未发出的回复"),
        ), patch.object(private_matcher, "choose_sticker", return_value=None):
            await private_matcher.handle_private_message(bot, _private_event("第二条"))

        self.assertEqual(["第一条", "第二条"], [item.text for item in conversation.snapshot()])

    async def test_concurrent_messages_are_serialized(self):
        bot = AsyncMock()
        conversation = PrivateConversation()
        first_started = asyncio.Event()
        release_first = asyncio.Event()

        async def reply_side_effect(message, **kwargs):
            if message == "第一条":
                first_started.set()
                await release_first.wait()
            return f"回复{message}"

        generate = AsyncMock(side_effect=reply_side_effect)
        with patch.object(private_matcher, "CONVERSATIONS", {"123456": conversation}), patch.object(
            private_matcher, "generate_reply", new=generate
        ), patch.object(private_matcher, "choose_sticker", return_value=None):
            first = asyncio.create_task(
                private_matcher.handle_private_message(bot, _private_event("第一条"))
            )
            await first_started.wait()
            second = asyncio.create_task(
                private_matcher.handle_private_message(bot, _private_event("第二条"))
            )
            await asyncio.sleep(0)
            self.assertEqual(1, generate.await_count)
            release_first.set()
            await asyncio.gather(first, second)

        self.assertEqual(
            ["第一条", "回复第一条", "第二条", "回复第二条"],
            [item.text for item in conversation.snapshot()],
        )


if __name__ == "__main__":
    unittest.main()
