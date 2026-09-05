from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

os.environ.setdefault("TARGET_GROUP_ID", "999000111")

from nonebot.adapters.onebot.v11 import (
    GroupMessageEvent,
    Message,
    PrivateMessageEvent,
)

from plugins.feature_control.state import FeatureState
from plugins.llm_gateway.errors import (
    GatewayConfigurationError,
    GatewayContractError,
    GatewayRateLimitError,
    GatewayServerError,
    GatewayTimeout,
)
from plugins.private_chat import matcher as private_matcher
from plugins.private_chat.conversation import PrivateConversation
from plugins.private_memory.schema import migrate
from plugins.private_memory.store import PrivateMemoryStore
from plugins.random_chat import ai as random_ai
from plugins.random_chat import matcher as random_matcher


_BUSY_NOTICE = "现在请求有点多，我暂时没接住，过一会儿再叫我吧。"


def _temporary_ai_error() -> random_ai.RandomChatAIError:
    error = random_ai.RandomChatAIError("temporary")
    error.retry_later = True
    return error


def _group_event() -> GroupMessageEvent:
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


def _private_event() -> PrivateMessageEvent:
    message = Message("你好")
    return PrivateMessageEvent(
        time=2000,
        self_id=999999,
        post_type="message",
        sub_type="friend",
        user_id=123456,
        message_type="private",
        message_id=456,
        message=message,
        original_message=message,
        raw_message="你好",
        font=0,
        sender={"user_id": 123456, "nickname": "测试者"},
    )


class _GatewayFeatures:
    def snapshot(self) -> SimpleNamespace:
        return SimpleNamespace(
            economy_mode_enabled=False,
            llm_gateway_enabled=True,
            llm_gateway_chat_enabled=True,
            relationship_state_enabled=False,
            prompt_builder_enabled=False,
            web_search_enabled=False,
        )


def _gateway_config() -> SimpleNamespace:
    return SimpleNamespace(
        ai_api_key="synthetic-primary-key",
        glm_api_key="synthetic-economy-key",
        chat_context_messages=20,
        tavily_api_key="",
    )


class RandomChatAIErrorClassificationTests(unittest.IsolatedAsyncioTestCase):
    async def test_gateway_temporary_errors_are_marked_retry_later(self) -> None:
        failures = (
            GatewayRateLimitError(status_code=429),
            GatewayTimeout(),
        )
        for phase in ("initialization", "request"):
            for failure in failures:
                gateway = SimpleNamespace(
                    generate_chat_reply=AsyncMock(side_effect=failure)
                )
                get_gateway = (
                    AsyncMock(side_effect=failure)
                    if phase == "initialization"
                    else AsyncMock(return_value=gateway)
                )
                with self.subTest(phase=phase, error=type(failure).__name__), patch(
                    "plugins.random_chat.ai.CONFIG", _gateway_config()
                ), patch(
                    "plugins.random_chat.ai.FEATURES", _GatewayFeatures()
                ), patch(
                    "plugins.random_chat.ai.get_gateway", new=get_gateway
                ), patch(
                    "plugins.random_chat.ai.load_character_prompt", return_value="角色"
                ):
                    with self.assertRaises(random_ai.RandomChatAIError) as raised:
                        await random_ai.generate_reply("你好", addressed=True)

                self.assertIs(
                    True,
                    getattr(raised.exception, "retry_later", None),
                )

    async def test_gateway_configuration_and_contract_errors_are_not_retry_later(
        self,
    ) -> None:
        failures = (
            GatewayConfigurationError(),
            GatewayContractError(),
        )
        for phase in ("initialization", "request"):
            for failure in failures:
                gateway = SimpleNamespace(
                    generate_chat_reply=AsyncMock(side_effect=failure)
                )
                get_gateway = (
                    AsyncMock(side_effect=failure)
                    if phase == "initialization"
                    else AsyncMock(return_value=gateway)
                )
                with self.subTest(phase=phase, error=type(failure).__name__), patch(
                    "plugins.random_chat.ai.CONFIG", _gateway_config()
                ), patch(
                    "plugins.random_chat.ai.FEATURES", _GatewayFeatures()
                ), patch(
                    "plugins.random_chat.ai.get_gateway", new=get_gateway
                ), patch(
                    "plugins.random_chat.ai.load_character_prompt", return_value="角色"
                ):
                    with self.assertRaises(random_ai.RandomChatAIError) as raised:
                        await random_ai.generate_reply("你好", addressed=True)

                self.assertIs(
                    False,
                    getattr(raised.exception, "retry_later", None),
                )

    async def test_only_retryable_gateway_server_status_is_marked_retry_later(
        self,
    ) -> None:
        for status_code, expected in ((501, False), (503, True)):
            with self.subTest(status_code=status_code):
                converted = random_ai._chat_error(
                    GatewayServerError(status_code=status_code)
                )

                self.assertIs(expected, converted.retry_later)


class ChatOutputTypeBoundaryTests(unittest.TestCase):
    def test_non_string_chat_output_fails_closed(self) -> None:
        for content in (
            ["不能发送的列表"],
            {"messages": ["不能发送的映射"]},
            b"cannot-send-bytes",
            123,
        ):
            with self.subTest(content_type=type(content).__name__):
                self.assertEqual(
                    (),
                    random_ai.parse_chat_replies(content, max_messages=3),
                )


class GroupTemporaryFailureFeedbackTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        import tempfile
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        features = Mock()
        features.snapshot.return_value = SimpleNamespace(
            relationship_state_enabled=False,
            prompt_builder_enabled=False,
        )
        features.image_understanding_allowed.return_value = False
        features.group_chat_allowed.return_value = True
        self.features = features
        config = SimpleNamespace(
            chat_archive_path=Path(directory.name) / "chat.db",
            chat_context_messages=20,
            chat_context_minutes=30,
            chat_context_self_messages=3,
            peer_bot_user_ids=(),
            member_memory_summary_enabled=False,
            random_chat_sticker_root=Path("/tmp/stickers"),
            random_chat_special_sticker="special.gif",
            random_chat_sticker_probability=0.0,
        )
        self.patchers = (
            patch.object(random_matcher, "FEATURES", features),
            patch.object(random_matcher, "CONFIG", config),
            patch.object(random_matcher, "recent_text_context", return_value=[]),
            patch.object(
                random_matcher, "archived_message_author", return_value=None
            ),
            patch.object(random_matcher, "load_profiles", return_value=[]),
        )
        for patcher in self.patchers:
            patcher.start()
            self.addCleanup(patcher.stop)

    async def test_addressed_temporary_failure_sends_one_fixed_notice(self) -> None:
        bot = AsyncMock()
        with patch.object(
            random_matcher,
            "generate_reply",
            new=AsyncMock(side_effect=_temporary_ai_error()),
        ):
            sent = await random_matcher.send_random_reply(
                bot,
                _group_event(),
                "当前消息",
                addressed=True,
            )

        self.assertTrue(sent)
        bot.send_group_msg.assert_awaited_once_with(
            group_id=789,
            message=_BUSY_NOTICE,
        )

    async def test_random_temporary_failure_stays_silent(self) -> None:
        bot = AsyncMock()
        with patch.object(
            random_matcher,
            "generate_reply",
            new=AsyncMock(side_effect=_temporary_ai_error()),
        ):
            sent = await random_matcher.send_random_reply(
                bot,
                _group_event(),
                "当前消息",
            )

        self.assertFalse(sent)
        bot.send_group_msg.assert_not_awaited()

    async def test_required_only_temporary_failure_stays_silent(self) -> None:
        bot = AsyncMock()
        with patch.object(
            random_matcher,
            "generate_reply",
            new=AsyncMock(side_effect=_temporary_ai_error()),
        ):
            sent = await random_matcher.send_random_reply(
                bot,
                _group_event(),
                "当前消息",
                required=True,
            )

        self.assertFalse(sent)
        bot.send_group_msg.assert_not_awaited()

    async def test_addressed_busy_notice_rechecks_group_runtime_access(self) -> None:
        bot = AsyncMock()

        async def disable_then_fail(*args, **kwargs):
            self.features.group_chat_allowed.return_value = False
            raise _temporary_ai_error()

        with patch.object(
            random_matcher,
            "generate_reply",
            new=AsyncMock(side_effect=disable_then_fail),
        ):
            sent = await random_matcher.send_random_reply(
                bot,
                _group_event(),
                "当前消息",
                addressed=True,
            )

        self.assertFalse(sent)
        bot.send_group_msg.assert_not_awaited()

    async def test_ai_cq_code_is_always_sent_as_text_in_group(self) -> None:
        cq_text = "[CQ:at,qq=all]"
        for sticker in (None, Path("/tmp/trusted-sticker.gif")):
            bot = AsyncMock()
            bot.send_group_msg.return_value = {}
            with self.subTest(sticker=sticker):
                with patch.object(
                    random_matcher,
                    "generate_reply",
                    new=AsyncMock(return_value=cq_text),
                ), patch.object(
                    random_matcher,
                    "choose_sticker",
                    return_value=sticker,
                ):
                    sent = await random_matcher.send_random_reply(
                        bot,
                        _group_event().model_copy(update={"message_id": 458 if sticker is None else 459}),
                        "当前消息",
                        addressed=True,
                    )

                self.assertTrue(sent)
                outbound = bot.send_group_msg.await_args.kwargs["message"]
                self.assertIsInstance(outbound, Message)
                segments = tuple(outbound)
                expected_types = (
                    ("text",)
                    if sticker is None
                    else ("text", "image")
                )
                self.assertEqual(
                    expected_types,
                    tuple(item.type for item in segments),
                )
                self.assertEqual(cq_text, segments[0].data["text"])
                self.assertNotIn("at", (item.type for item in segments))
                self.assertNotIn("reply", (item.type for item in segments))
                if sticker is not None:
                    self.assertEqual(
                        f"file://{sticker}",
                        segments[1].data["file"],
                    )


class PrivateTemporaryFailureFeedbackTests(unittest.IsolatedAsyncioTestCase):
    async def test_allowed_private_temporary_failure_sends_notice_without_bot_history(
        self,
    ) -> None:
        state = FeatureState(
            business_enabled=True,
            chat_enabled=True,
            group_chat_enabled=False,
            private_chat_enabled=True,
            group_chat_allowed_group_ids=(),
            private_chat_allowed_user_ids=("123456",),
            private_memory_enabled=False,
            relationship_state_enabled=False,
        )
        features = Mock()
        features.snapshot.return_value = state
        features.private_chat_allowed.side_effect = (
            lambda user_id: str(user_id) == "123456"
        )
        conversation = PrivateConversation()
        bot = AsyncMock()
        with patch.object(private_matcher, "FEATURES", features), patch.object(
            private_matcher,
            "CONVERSATIONS",
            {"123456": conversation},
        ), patch.object(
            private_matcher,
            "generate_reply",
            new=AsyncMock(side_effect=_temporary_ai_error()),
        ):
            await private_matcher.handle_private_message(bot, _private_event())

        bot.send_private_msg.assert_awaited_once_with(
            user_id=123456,
            message=_BUSY_NOTICE,
        )
        self.assertEqual(
            ["你好"],
            [item.text for item in conversation.snapshot()],
        )

    async def test_ai_cq_code_is_always_sent_as_text_in_private(self) -> None:
        cq_text = "[CQ:at,qq=all]"
        state = FeatureState(
            business_enabled=True,
            chat_enabled=True,
            group_chat_enabled=False,
            private_chat_enabled=True,
            group_chat_allowed_group_ids=(),
            private_chat_allowed_user_ids=("123456",),
            private_memory_enabled=False,
            relationship_state_enabled=False,
        )
        features = Mock()
        features.snapshot.return_value = state
        features.private_chat_allowed.side_effect = (
            lambda user_id: str(user_id) == "123456"
        )
        config = SimpleNamespace(
            chat_vision_enabled=False,
            random_chat_sticker_root=Path("/tmp/stickers"),
            random_chat_special_sticker="special.gif",
            random_chat_sticker_probability=0.0,
        )
        for sticker in (None, Path("/tmp/trusted-sticker.gif")):
            bot = AsyncMock()
            conversation = PrivateConversation()
            with self.subTest(sticker=sticker):
                with patch.object(
                    private_matcher, "FEATURES", features
                ), patch.object(
                    private_matcher, "CONFIG", config
                ), patch.object(
                    private_matcher,
                    "CONVERSATIONS",
                    {"123456": conversation},
                ), patch.object(
                    private_matcher,
                    "generate_reply",
                    new=AsyncMock(return_value=cq_text),
                ), patch.object(
                    private_matcher,
                    "choose_sticker",
                    return_value=sticker,
                ):
                    await private_matcher.handle_private_message(
                        bot, _private_event()
                    )

                outbound = bot.send_private_msg.await_args.kwargs["message"]
                self.assertIsInstance(outbound, Message)
                segments = tuple(outbound)
                expected_types = (
                    ("text",)
                    if sticker is None
                    else ("text", "image")
                )
                self.assertEqual(
                    expected_types,
                    tuple(item.type for item in segments),
                )
                self.assertEqual(cq_text, segments[0].data["text"])
                self.assertNotIn("at", (item.type for item in segments))
                self.assertNotIn("reply", (item.type for item in segments))
                if sticker is not None:
                    self.assertEqual(
                        f"file://{sticker}",
                        segments[1].data["file"],
                    )

    async def test_governance_clear_during_ai_suppresses_stale_busy_notice(
        self,
    ) -> None:
        state = FeatureState(
            business_enabled=True,
            chat_enabled=True,
            group_chat_enabled=False,
            private_chat_enabled=True,
            group_chat_allowed_group_ids=(),
            private_chat_allowed_user_ids=("123456",),
            private_memory_enabled=True,
            relationship_state_enabled=False,
        )
        features = Mock()
        features.snapshot.return_value = state
        features.private_chat_allowed.side_effect = (
            lambda user_id: str(user_id) == "123456"
        )
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "chat.db"
            migrate(database)
            config = SimpleNamespace(
                chat_vision_enabled=False,
                chat_archive_path=database,
                private_memory_retention_days=30,
                random_chat_sticker_root=Path("/tmp/stickers"),
                random_chat_special_sticker="special.gif",
                random_chat_sticker_probability=0.0,
            )

            async def clear_then_fail(*args, **kwargs):
                PrivateMemoryStore(database).clear_private_layers(
                    user_id="123456",
                    actor="900",
                    reason="测试清空",
                    operation_id=1,
                )
                raise _temporary_ai_error()

            bot = AsyncMock()
            with patch.object(private_matcher, "FEATURES", features), patch.object(
                private_matcher, "CONFIG", config
            ), patch.object(
                private_matcher, "CONVERSATIONS", {}
            ), patch.object(
                private_matcher, "_enqueue_private_jobs", new=Mock()
            ), patch.object(
                private_matcher,
                "generate_reply",
                new=AsyncMock(side_effect=clear_then_fail),
            ):
                await private_matcher.handle_private_message(bot, _private_event())

            bot.send_private_msg.assert_not_awaited()
            self.assertEqual(
                (),
                PrivateMemoryStore(database).recent_context(
                    user_id="123456", limit=10
                ),
            )


if __name__ == "__main__":
    unittest.main()
