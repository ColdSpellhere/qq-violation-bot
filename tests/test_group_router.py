import os
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

os.environ.setdefault("TARGET_GROUP_ID", "999000111")

import nonebot
from nonebot.adapters.onebot.v11 import GroupMessageEvent, Message, MessageSegment

try:
    nonebot.get_driver()
except ValueError:
    nonebot.init()

from plugins.feature_control.state import FeatureController, FeatureState
from plugins.chat_archive import matcher as archive_matcher
from plugins.group_router import matcher as group_router
from plugins.member_memory import matcher as memory_matcher
from plugins.violation_record import matcher as violation_matcher
from plugins.violation_record.schemas import DEFAULT_INTENT


TARGET_GROUP_ID = 999000111
CHAT_GROUP_ID = 999000222


def _group_event(
    text: str,
    *,
    group_id: int = TARGET_GROUP_ID,
    addressed: bool = False,
    image: bool = False,
    reply_image: bool = False,
    user_id: int = 123,
    self_id: int = 999,
) -> GroupMessageEvent:
    segments = []
    if addressed:
        segments.append(MessageSegment.at(self_id))
    if image:
        segments.append(MessageSegment.image("https://example.invalid/chat.jpg"))
    if text:
        segments.append(MessageSegment.text(text))
    message = Message(segments)
    reply = None
    if reply_image:
        reply = {
            "time": 1900,
            "message_type": "group",
            "message_id": 111,
            "real_id": 111,
            "sender": {"user_id": 321, "nickname": "引用者"},
            "message": Message(
                [MessageSegment.image("https://example.invalid/quoted.jpg")]
            ),
        }
    return GroupMessageEvent(
        time=2000,
        self_id=self_id,
        post_type="message",
        sub_type="normal",
        user_id=user_id,
        message_type="group",
        message_id=456,
        group_id=group_id,
        message=message,
        original_message=message,
        raw_message=str(message),
        font=0,
        sender={"user_id": user_id, "nickname": "成员", "role": "member"},
        reply=reply,
    )


class GroupRouterTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.defaults = FeatureState(
            business_enabled=True,
            chat_enabled=True,
            group_chat_enabled=True,
            private_chat_enabled=True,
            group_chat_allowed_group_ids=(CHAT_GROUP_ID,),
            private_chat_allowed_user_ids=("123",),
        )

    def controller(self, **changes) -> FeatureController:
        state = replace(self.defaults, **changes)
        return FeatureController(Path(self.directory.name) / "features.json", state)

    async def test_non_business_group_never_calls_business_parser(self) -> None:
        bot = AsyncMock()
        event = _group_event("你叫什么", group_id=CHAT_GROUP_ID, addressed=True)
        with patch.object(group_router, "FEATURES", self.controller()), patch.object(
            group_router,
            "CONFIG",
            SimpleNamespace(
                target_group_id=TARGET_GROUP_ID,
                random_chat_probability=0.05,
            ),
        ), patch.object(
            violation_matcher,
            "parse_intent",
            new=AsyncMock(side_effect=AssertionError("business parser called")),
        ), patch.object(
            group_router, "send_random_reply", new=AsyncMock(return_value=True)
        ):
            await group_router.route_group_message(bot, event)

    async def test_chat_only_controller_never_calls_business_handler(self) -> None:
        bot = AsyncMock()
        event = _group_event("帮助", addressed=True)
        controller = FeatureController(
            Path(self.directory.name) / "chat-only.json",
            self.defaults,
            business_capable=False,
        )
        with patch.object(group_router, "FEATURES", controller), patch.object(
            group_router,
            "CONFIG",
            SimpleNamespace(
                target_group_id=TARGET_GROUP_ID,
                random_chat_probability=0.05,
            ),
        ), patch.object(
            group_router,
            "handle_business_message",
            new=AsyncMock(side_effect=AssertionError("business handler called")),
        ), patch.object(
            group_router, "send_random_reply", new=AsyncMock(return_value=True)
        ) as casual:
            await group_router.route_group_message(bot, event)

        casual.assert_not_awaited()

    async def test_known_business_request_does_not_call_chat(self) -> None:
        bot = AsyncMock()
        event = _group_event("帮助", addressed=True)
        intent = DEFAULT_INTENT | {"intent": "help"}
        with patch.object(group_router, "FEATURES", self.controller()), patch.object(
            group_router,
            "CONFIG",
            SimpleNamespace(
                target_group_id=TARGET_GROUP_ID,
                random_chat_probability=0.05,
            ),
        ), patch.object(violation_matcher, "grant_admin"), patch.object(
            violation_matcher, "_sync_group_admins", new=AsyncMock()
        ), patch.object(
            violation_matcher, "handle_policy_text", return_value=None
        ), patch.object(
            violation_matcher,
            "_referenced_message_time",
            new=AsyncMock(return_value=None),
        ), patch.object(
            violation_matcher, "parse_intent", new=AsyncMock(return_value=intent)
        ), patch.object(
            violation_matcher,
            "handle_intent",
            new=AsyncMock(return_value="业务帮助"),
        ), patch.object(
            group_router, "send_random_reply", new=AsyncMock()
        ) as casual:
            await group_router.route_group_message(bot, event)

        bot.send_group_msg.assert_awaited_once_with(
            group_id=TARGET_GROUP_ID, message="业务帮助"
        )
        casual.assert_not_awaited()

    async def test_unknown_addressed_business_group_message_falls_through_to_chat(
        self,
    ) -> None:
        bot = AsyncMock()
        event = _group_event("你叫什么", addressed=True)
        intent = DEFAULT_INTENT | {"intent": "unknown"}
        controller = self.controller(
            group_chat_allowed_group_ids=(TARGET_GROUP_ID, CHAT_GROUP_ID)
        )
        with patch.object(group_router, "FEATURES", controller), patch.object(
            group_router,
            "CONFIG",
            SimpleNamespace(
                target_group_id=TARGET_GROUP_ID,
                random_chat_probability=0.05,
            ),
        ), patch.object(violation_matcher, "grant_admin"), patch.object(
            violation_matcher, "_sync_group_admins", new=AsyncMock()
        ), patch.object(
            violation_matcher, "handle_policy_text", return_value=None
        ), patch.object(
            violation_matcher,
            "_referenced_message_time",
            new=AsyncMock(return_value=None),
        ), patch.object(
            violation_matcher, "parse_intent", new=AsyncMock(return_value=intent)
        ), patch.object(
            violation_matcher, "handle_intent", new=AsyncMock()
        ) as handle_intent, patch.object(
            group_router, "send_random_reply", new=AsyncMock(return_value=True)
        ) as casual:
            await group_router.route_group_message(bot, event)

        handle_intent.assert_not_awaited()
        casual.assert_awaited_once_with(bot, event, "你叫什么", addressed=True)

    async def test_addressed_allowed_group_always_chats_without_probability_sample(
        self,
    ) -> None:
        bot = AsyncMock()
        event = _group_event("在吗", group_id=CHAT_GROUP_ID, addressed=True)
        with patch.object(group_router, "FEATURES", self.controller()), patch.object(
            group_router,
            "CONFIG",
            SimpleNamespace(
                target_group_id=TARGET_GROUP_ID,
                random_chat_probability=0.0,
            ),
        ), patch.object(group_router, "should_reply") as should_reply, patch.object(
            group_router, "send_random_reply", new=AsyncMock(return_value=True)
        ) as casual:
            await group_router.route_group_message(bot, event)

        should_reply.assert_not_called()
        casual.assert_awaited_once_with(bot, event, "在吗", addressed=True)

    async def test_addressed_image_only_message_always_chats_without_probability_sample(
        self,
    ) -> None:
        bot = AsyncMock()
        event = _group_event("", group_id=CHAT_GROUP_ID, addressed=True, image=True)
        with patch.object(group_router, "FEATURES", self.controller()), patch.object(
            group_router,
            "CONFIG",
            SimpleNamespace(
                target_group_id=TARGET_GROUP_ID,
                random_chat_probability=0.0,
            ),
        ), patch.object(group_router, "should_reply") as should_reply, patch.object(
            group_router, "send_random_reply", new=AsyncMock(return_value=True)
        ) as casual:
            await group_router.route_group_message(bot, event)

        should_reply.assert_not_called()
        casual.assert_awaited_once_with(bot, event, "", addressed=True)

    async def test_target_group_addressed_image_only_skips_empty_business_prompt(
        self,
    ) -> None:
        bot = AsyncMock()
        event = _group_event("", addressed=True, image=True)
        controller = self.controller(
            group_chat_allowed_group_ids=(TARGET_GROUP_ID, CHAT_GROUP_ID)
        )
        with patch.object(group_router, "FEATURES", controller), patch.object(
            group_router,
            "CONFIG",
            SimpleNamespace(
                target_group_id=TARGET_GROUP_ID,
                random_chat_probability=0.0,
            ),
        ), patch.object(
            group_router, "handle_business_message", new=AsyncMock(return_value=True)
        ) as business, patch.object(
            group_router, "send_random_reply", new=AsyncMock(return_value=True)
        ) as casual:
            await group_router.route_group_message(bot, event)

        business.assert_not_awaited()
        casual.assert_awaited_once_with(bot, event, "", addressed=True)
        bot.send_group_msg.assert_not_awaited()

    async def test_target_group_addressed_real_reply_image_skips_empty_business_prompt(
        self,
    ) -> None:
        bot = AsyncMock()
        event = _group_event("", addressed=True, reply_image=True)
        controller = self.controller(
            group_chat_allowed_group_ids=(TARGET_GROUP_ID, CHAT_GROUP_ID)
        )
        with patch.object(group_router, "FEATURES", controller), patch.object(
            group_router,
            "CONFIG",
            SimpleNamespace(
                target_group_id=TARGET_GROUP_ID,
                random_chat_probability=0.0,
            ),
        ), patch.object(
            group_router,
            "handle_business_message",
            new=AsyncMock(return_value=True),
        ) as business, patch.object(
            group_router,
            "send_random_reply",
            new=AsyncMock(return_value=True),
        ) as casual:
            await group_router.route_group_message(bot, event)

        self.assertIsNotNone(event.reply)
        self.assertTrue(any(segment.type == "image" for segment in event.reply.message))
        business.assert_not_awaited()
        casual.assert_awaited_once_with(bot, event, "", addressed=True)

    async def test_target_group_image_with_business_text_remains_business_first(
        self,
    ) -> None:
        bot = AsyncMock()
        event = _group_event("帮助", addressed=True, image=True)
        controller = self.controller(
            group_chat_allowed_group_ids=(TARGET_GROUP_ID, CHAT_GROUP_ID)
        )
        with patch.object(group_router, "FEATURES", controller), patch.object(
            group_router,
            "CONFIG",
            SimpleNamespace(
                target_group_id=TARGET_GROUP_ID,
                random_chat_probability=1.0,
            ),
        ), patch.object(
            group_router, "handle_business_message", new=AsyncMock(return_value=True)
        ) as business, patch.object(
            group_router, "send_random_reply", new=AsyncMock()
        ) as casual:
            await group_router.route_group_message(bot, event)

        business.assert_awaited_once_with(bot, event, "帮助")
        casual.assert_not_awaited()

    async def test_unaddressed_image_only_message_uses_probability_once(self) -> None:
        bot = AsyncMock()
        event = _group_event("", group_id=CHAT_GROUP_ID, image=True)
        config = SimpleNamespace(
            target_group_id=TARGET_GROUP_ID,
            random_chat_probability=0.25,
        )
        for decision in (False, True):
            with self.subTest(decision=decision), patch.object(
                group_router, "FEATURES", self.controller()
            ), patch.object(group_router, "CONFIG", config), patch.object(
                group_router, "should_reply", return_value=decision
            ) as sample, patch.object(
                group_router, "send_random_reply", new=AsyncMock(return_value=True)
            ) as casual:
                await group_router.route_group_message(bot, event)

            sample.assert_called_once_with(0.25)
            if decision:
                casual.assert_awaited_once_with(bot, event, "")
            else:
                casual.assert_not_awaited()

    async def test_image_with_text_uses_ordinary_text_probability_once(self) -> None:
        bot = AsyncMock()
        event = _group_event("看看这朵花", group_id=CHAT_GROUP_ID, image=True)
        config = SimpleNamespace(
            target_group_id=TARGET_GROUP_ID,
            random_chat_probability=0.25,
        )
        with patch.object(group_router, "FEATURES", self.controller()), patch.object(
            group_router, "CONFIG", config
        ), patch.object(group_router, "should_reply", return_value=True) as sample, patch.object(
            group_router, "send_random_reply", new=AsyncMock(return_value=True)
        ) as casual:
            await group_router.route_group_message(bot, event)

        sample.assert_called_once_with(0.25)
        casual.assert_awaited_once_with(bot, event, "看看这朵花")

    async def test_ordinary_allowed_group_is_probability_gated(self) -> None:
        bot = AsyncMock()
        event = _group_event("随便聊聊", group_id=CHAT_GROUP_ID)
        config = SimpleNamespace(
            target_group_id=TARGET_GROUP_ID,
            random_chat_probability=0.25,
        )
        with patch.object(group_router, "FEATURES", self.controller()), patch.object(
            group_router, "CONFIG", config
        ), patch.object(group_router, "should_reply", return_value=False) as sample, patch.object(
            group_router, "send_random_reply", new=AsyncMock()
        ) as casual:
            await group_router.route_group_message(bot, event)

        sample.assert_called_once_with(0.25)
        casual.assert_not_awaited()

        with patch.object(group_router, "FEATURES", self.controller()), patch.object(
            group_router, "CONFIG", config
        ), patch.object(group_router, "should_reply", return_value=True), patch.object(
            group_router, "send_random_reply", new=AsyncMock(return_value=True)
        ) as casual:
            await group_router.route_group_message(bot, event)

        casual.assert_awaited_once_with(bot, event, "随便聊聊")

    async def test_chat_disabled_blocks_reply_archive_and_memory(self) -> None:
        bot = AsyncMock()
        event = _group_event("不会处理", group_id=CHAT_GROUP_ID, addressed=True)
        controller = self.controller(chat_enabled=False)
        config = SimpleNamespace(
            target_group_id=TARGET_GROUP_ID,
            random_chat_probability=1.0,
        )
        with patch.object(group_router, "FEATURES", controller), patch.object(
            archive_matcher, "FEATURES", controller
        ), patch.object(memory_matcher, "FEATURES", controller), patch.object(
            group_router, "CONFIG", config
        ), patch.object(
            group_router, "send_random_reply", new=AsyncMock()
        ) as casual, patch.object(
            archive_matcher, "archive_payload"
        ) as archive, patch.object(memory_matcher.BATCHER, "add") as remember:
            await group_router.route_group_message(bot, event)
            self.assertFalse(archive_matcher._chat_group(event))
            self.assertFalse(memory_matcher._target_member_message(event))
            await memory_matcher.collect_member_memory(event)

        casual.assert_not_awaited()
        archive.assert_not_called()
        remember.assert_not_called()

    async def test_candidate_accepts_business_or_chat_group_and_rejects_self(self) -> None:
        controller = self.controller()
        config = SimpleNamespace(
            target_group_id=TARGET_GROUP_ID,
            random_chat_probability=0.05,
        )
        with patch.object(group_router, "FEATURES", controller), patch.object(
            group_router, "CONFIG", config
        ):
            self.assertTrue(
                await group_router.group_message_candidate(
                    _group_event("业务", group_id=TARGET_GROUP_ID)
                )
            )
            self.assertTrue(
                await group_router.group_message_candidate(
                    _group_event("闲聊", group_id=CHAT_GROUP_ID)
                )
            )
            self.assertFalse(
                await group_router.group_message_candidate(
                    _group_event("外群", group_id=123456789)
                )
            )
            self.assertFalse(
                await group_router.group_message_candidate(
                    _group_event("机器人自己", group_id=CHAT_GROUP_ID, user_id=999)
                )
            )


if __name__ == "__main__":
    unittest.main()
