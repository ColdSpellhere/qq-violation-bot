import asyncio
import importlib
import logging
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

os.environ.setdefault("TARGET_GROUP_ID", "999000111")

from nonebot.adapters.onebot.v11 import Message, MessageSegment, PrivateMessageEvent

from plugins.chat_archive.db import ContextMessage
from plugins.chat_vision.client import VisionImage
from plugins.chat_vision.download import DownloadedChatImage
from plugins.private_chat import matcher as private_matcher
from plugins.private_chat.conversation import PrivateConversation
from plugins.private_chat.policy import eligible_private_text
from plugins.private_memory.schema import migrate
from plugins.private_memory.store import PrivateMemoryStore


def _private_event(
    text: str = "",
    *,
    image_urls: tuple[str, ...] = (),
    user_id: int = 123456,
    message_id: int = 456,
) -> PrivateMessageEvent:
    segments: list[MessageSegment] = []
    if text:
        segments.append(MessageSegment.text(text))
    segments.extend(
        MessageSegment("image", {"url": url, "file": f"source-{index}"})
        for index, url in enumerate(image_urls)
    )
    message = Message(segments)
    return PrivateMessageEvent(
        time=2_000,
        self_id=999999,
        post_type="message",
        sub_type="friend",
        user_id=user_id,
        message_type="private",
        message_id=message_id,
        message=message,
        original_message=message,
        raw_message=str(message),
        font=0,
        sender={"user_id": user_id, "nickname": f"用户{user_id}"},
    )


def _config(*, vision_enabled: bool = True, max_bytes: int = 10) -> SimpleNamespace:
    return SimpleNamespace(
        chat_vision_enabled=vision_enabled,
        chat_vision_max_bytes=max_bytes,
        chat_vision_timeout=3,
        chat_vision_model="vision-test",
        ai_base_url="https://gateway.invalid",
        ai_api_key="test-key",
        chat_archive_path=Path("/tmp/private-vision-test.db"),
        private_memory_retention_days=30,
        random_chat_sticker_root=Path("/tmp/stickers"),
        random_chat_special_sticker="special.gif",
        random_chat_sticker_probability=0.0,
    )


class _MutableFeatures:
    def __init__(self, *, persistent: bool = False) -> None:
        self.allowed = True
        self.persistent = persistent

    def private_chat_allowed(self, user_id: str) -> bool:
        return self.allowed and str(user_id) == "123456"

    def snapshot(self) -> SimpleNamespace:
        return SimpleNamespace(
            private_memory_enabled=self.persistent,
            relationship_state_enabled=False,
        )


def _vision_result(
    *,
    images: tuple[VisionImage, ...] = (),
    descriptions: tuple[str, ...] = (),
):
    return SimpleNamespace(
        had_image=True,
        images=images,
        descriptions=descriptions,
    )


class PrivateVisionPolicyTests(unittest.TestCase):
    def test_image_only_uses_placeholder_but_image_command_stays_rejected(self) -> None:
        self.assertEqual("[图片]", eligible_private_text("", has_image=True))
        self.assertEqual("看看", eligible_private_text("  看看  ", has_image=True))
        self.assertIsNone(eligible_private_text(" /help ", has_image=True))


class PrivateVisionOrchestratorTests(unittest.IsolatedAsyncioTestCase):
    def _module(self):
        try:
            return importlib.import_module("plugins.private_chat.vision")
        except ModuleNotFoundError:
            self.fail("plugins.private_chat.vision is missing")

    async def test_at_most_four_images_are_downloaded_and_described(self) -> None:
        vision = self._module()
        urls = tuple(f"https://images.invalid/{index}.png" for index in range(5))
        message = _private_event(image_urls=urls).message
        downloaded = DownloadedChatImage(b"x", "image/png", "png")
        with patch.object(
            vision, "download_chat_image", new=AsyncMock(return_value=downloaded)
        ) as download, patch.object(
            vision,
            "describe_image",
            new=AsyncMock(side_effect=lambda *args, **kwargs: "一张测试图片"),
        ) as describe:
            result = await vision.understand_private_images(
                message,
                message_id="456",
                max_bytes=100,
                timeout=3,
                base_url="https://gateway.invalid",
                api_key="test-key",
                model="vision-test",
            )

        self.assertTrue(result.had_image)
        self.assertEqual(4, download.await_count)
        self.assertEqual(4, describe.await_count)
        self.assertEqual((0, 1, 2, 3), tuple(item.ordinal for item in result.images))
        downloaded_urls = tuple(call.args[0] for call in download.await_args_list)
        self.assertEqual(urls[:4], downloaded_urls)

    async def test_total_raw_byte_budget_drops_all_raw_images_but_keeps_descriptions(self) -> None:
        vision = self._module()
        message = _private_event(
            image_urls=("https://images.invalid/a.png", "https://images.invalid/b.png")
        ).message
        downloaded = DownloadedChatImage(b"abc", "image/png", "png")
        describe = AsyncMock(side_effect=("第一张", "第二张"))
        with patch.object(
            vision, "download_chat_image", new=AsyncMock(return_value=downloaded)
        ), patch.object(vision, "describe_image", new=describe):
            result = await vision.understand_private_images(
                message,
                message_id="456",
                max_bytes=5,
                timeout=3,
                base_url="https://gateway.invalid",
                api_key="test-key",
                model="vision-test",
            )

        self.assertEqual((), result.images)
        self.assertEqual(("第一张", "第二张"), result.descriptions)

    async def test_one_image_failure_does_not_discard_the_other_image(self) -> None:
        vision = self._module()
        bad_url = "https://images.invalid/bad.png"
        good_url = "https://images.invalid/good.png"
        message = _private_event(image_urls=(bad_url, good_url)).message

        async def download(url: str, **kwargs):
            if url == bad_url:
                raise ValueError("download failed")
            return DownloadedChatImage(b"good", "image/png", "png")

        with patch.object(
            vision, "download_chat_image", new=AsyncMock(side_effect=download)
        ), patch.object(
            vision, "describe_image", new=AsyncMock(return_value="可用图片")
        ):
            result = await vision.understand_private_images(
                message,
                message_id="456",
                max_bytes=100,
                timeout=3,
                base_url="https://gateway.invalid",
                api_key="test-key",
                model="vision-test",
            )

        self.assertEqual((1,), tuple(item.ordinal for item in result.images))
        self.assertEqual(("可用图片",), result.descriptions)

    async def test_raw_image_survives_description_failure_without_logging_sensitive_data(self) -> None:
        vision = self._module()
        secret_url = "https://images.invalid/private-token.png"
        secret_description = "绝不能进入日志的图片描述"
        message = _private_event(image_urls=(secret_url,)).message
        downloaded = DownloadedChatImage(b"raw", "image/png", "png")
        with patch.object(
            vision, "download_chat_image", new=AsyncMock(return_value=downloaded)
        ), patch.object(
            vision,
            "describe_image",
            new=AsyncMock(side_effect=RuntimeError(secret_description)),
        ), self.assertLogs("plugins.private_chat.vision", level=logging.WARNING) as captured:
            result = await vision.understand_private_images(
                message,
                message_id="456",
                max_bytes=100,
                timeout=3,
                base_url="https://gateway.invalid",
                api_key="test-key",
                model="vision-test",
            )

        self.assertEqual(1, len(result.images))
        self.assertEqual((), result.descriptions)
        log_text = "\n".join(captured.output)
        self.assertNotIn(secret_url, log_text)
        self.assertNotIn(secret_description, log_text)


class PrivateVisionConversationTests(unittest.TestCase):
    def test_source_kind_and_descriptions_are_forwarded_to_private_store(self) -> None:
        store = Mock()
        expected_state = object()
        store.append_user_message_state.return_value = expected_state
        conversation = PrivateConversation(user_id="123456", store=store)
        turn = ContextMessage(
            "用户123456",
            "[图片]",
            message_id="456",
            user_id="123456",
            image_descriptions=("一朵白花",),
        )

        state = conversation.append_user_state(
            turn,
            event_time=2_000,
            source_kind="image",
        )

        self.assertIs(expected_state, state)
        store.append_user_message_state.assert_called_once_with(
            user_id="123456",
            message_id="456",
            text="[图片]",
            event_time=2_000,
            source_kind="image",
            image_descriptions=("一朵白花",),
        )

    def test_completed_descriptions_replace_only_the_matching_user_turn(self) -> None:
        conversation = PrivateConversation(user_id="123456")
        original = ContextMessage(
            "用户123456", "[图片]", message_id="456", user_id="123456"
        )
        other = ContextMessage(
            "用户123456", "下一条", message_id="457", user_id="123456"
        )
        conversation.append(original)
        conversation.append(other)
        completed = ContextMessage(
            "用户123456",
            "[图片]",
            message_id="456",
            user_id="123456",
            image_descriptions=("一朵白花",),
        )

        self.assertTrue(conversation.replace_user_turn(completed))

        snapshot = conversation.snapshot()
        self.assertEqual(("一朵白花",), snapshot[0].image_descriptions)
        self.assertIs(other, snapshot[1])

    def test_cleared_persistent_turn_does_not_resurface_after_memory_is_disabled(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "chat.db"
            migrate(database)
            store = PrivateMemoryStore(database)
            conversation = PrivateConversation(user_id="123456", store=store)
            turn = ContextMessage(
                "用户123456",
                "[图片]",
                message_id="456",
                user_id="123456",
                image_descriptions=("一朵白花",),
            )
            conversation.append_user_state(
                turn,
                event_time=2_000,
                source_kind="image",
            )
            store.clear_private_layers(
                user_id="123456",
                actor="900",
                reason="测试清空",
                operation_id=1,
            )

            self.assertEqual((), conversation.snapshot())
            conversation.use_store(None)

            self.assertEqual((), conversation.snapshot())


class PrivateVisionMatcherTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.features = _MutableFeatures()
        self.config = _config()

    def _runtime(self, *, conversations=None):
        return patch.multiple(
            private_matcher,
            FEATURES=self.features,
            CONFIG=self.config,
            CONVERSATIONS={} if conversations is None else conversations,
        )

    async def test_pure_image_passes_raw_image_and_description_to_generate_reply(self) -> None:
        bot = AsyncMock()
        conversation = PrivateConversation(user_id="123456")
        image = VisionImage(b"raw", "image/png", "456", 0)
        understand = AsyncMock(
            return_value=_vision_result(
                images=(image,), descriptions=("一朵白花",)
            )
        )
        generate = AsyncMock(return_value="看起来是一朵白花")
        with self._runtime(conversations={"123456": conversation}), patch.object(
            private_matcher,
            "understand_private_images",
            new=understand,
            create=True,
        ), patch.object(
            private_matcher, "generate_reply", new=generate
        ), patch.object(private_matcher, "choose_sticker", return_value=None):
            await private_matcher.handle_private_message(
                bot,
                _private_event(image_urls=("https://images.invalid/flower.png",)),
            )

        understand.assert_awaited_once()
        generate.assert_awaited_once()
        kwargs = generate.await_args.kwargs
        self.assertEqual("[图片]", generate.await_args.args[0])
        self.assertEqual((image,), kwargs["images"])
        self.assertEqual(("一朵白花",), kwargs["current"].image_descriptions)
        self.assertEqual(
            ("一朵白花",), conversation.snapshot()[0].image_descriptions
        )
        bot.send_private_msg.assert_awaited_once()

    async def test_text_and_image_preserves_real_text_and_passes_images(self) -> None:
        image = VisionImage(b"raw", "image/png", "456", 0)
        understand = AsyncMock(
            return_value=_vision_result(images=(image,), descriptions=("一只小猫",))
        )
        generate = AsyncMock(return_value="是只小猫")
        with self._runtime(), patch.object(
            private_matcher,
            "understand_private_images",
            new=understand,
            create=True,
        ), patch.object(
            private_matcher, "generate_reply", new=generate
        ), patch.object(private_matcher, "choose_sticker", return_value=None):
            await private_matcher.handle_private_message(
                AsyncMock(),
                _private_event(
                    "这是什么",
                    image_urls=("https://images.invalid/cat.png",),
                ),
            )

        self.assertEqual("这是什么", generate.await_args.args[0])
        self.assertEqual((image,), generate.await_args.kwargs["images"])
        self.assertEqual("这是什么", generate.await_args.kwargs["current"].text)

    async def test_image_command_is_ignored_before_download(self) -> None:
        understand = AsyncMock(return_value=_vision_result())
        generate = AsyncMock(return_value="不应回复")
        bot = AsyncMock()
        with self._runtime(), patch.object(
            private_matcher,
            "understand_private_images",
            new=understand,
            create=True,
        ), patch.object(private_matcher, "generate_reply", new=generate):
            await private_matcher.handle_private_message(
                bot,
                _private_event(
                    "/help",
                    image_urls=("https://images.invalid/command.png",),
                ),
            )

        understand.assert_not_awaited()
        generate.assert_not_awaited()
        bot.send_private_msg.assert_not_awaited()

    async def test_all_image_failures_drop_pure_image_but_mixed_degrades_to_text(self) -> None:
        understand = AsyncMock(return_value=_vision_result())
        generate = AsyncMock(return_value="文字降级回复")
        pure_bot = AsyncMock()
        mixed_bot = AsyncMock()
        with self._runtime(), patch.object(
            private_matcher,
            "understand_private_images",
            new=understand,
            create=True,
        ), patch.object(
            private_matcher, "generate_reply", new=generate
        ), patch.object(private_matcher, "choose_sticker", return_value=None):
            await private_matcher.handle_private_message(
                pure_bot,
                _private_event(image_urls=("https://images.invalid/bad.png",)),
            )
            await private_matcher.handle_private_message(
                mixed_bot,
                _private_event(
                    "至少回答文字",
                    image_urls=("https://images.invalid/bad.png",),
                    message_id=457,
                ),
            )

        generate.assert_awaited_once()
        self.assertEqual("至少回答文字", generate.await_args.args[0])
        self.assertEqual((), generate.await_args.kwargs["images"])
        pure_bot.send_private_msg.assert_not_awaited()
        mixed_bot.send_private_msg.assert_awaited_once()

    async def test_allowlist_removal_during_image_work_prevents_ai_and_send(self) -> None:
        self.features = _MutableFeatures(persistent=True)

        async def remove_allowlist(*args, **kwargs):
            self.features.allowed = False
            return _vision_result(
                images=(VisionImage(b"raw", "image/png", "456", 0),),
                descriptions=("私密描述",),
            )

        understand = AsyncMock(side_effect=remove_allowlist)
        generate = AsyncMock(return_value="不应回复")
        bot = AsyncMock()
        state = SimpleNamespace(
            row_id=1,
            created=True,
            live=True,
            assistant_exists=False,
        )
        conversation = Mock()
        conversation.lock = asyncio.Lock()
        conversation.snapshot.return_value = ()
        conversation.append_user_state.return_value = state
        store = Mock()
        enqueue = Mock()
        with self._runtime(conversations={"123456": conversation}), patch.object(
            private_matcher, "PrivateMemoryStore", return_value=store
        ), patch.object(private_matcher, "MemoryJobQueue", return_value=Mock()), patch.object(
            private_matcher, "RelationshipStore", return_value=Mock()
        ), patch.object(
            private_matcher,
            "understand_private_images",
            new=understand,
            create=True,
        ), patch.object(
            private_matcher, "_enqueue_private_jobs", new=enqueue
        ), patch.object(private_matcher, "generate_reply", new=generate):
            await private_matcher.handle_private_message(
                bot,
                _private_event(image_urls=("https://images.invalid/private.png",)),
            )

        understand.assert_awaited_once()
        store.update_user_image_descriptions.assert_not_called()
        enqueue.assert_not_called()
        generate.assert_not_awaited()
        bot.send_private_msg.assert_not_awaited()

    async def test_memory_clear_during_image_work_prevents_ai_and_send(self) -> None:
        self.features = _MutableFeatures(persistent=True)
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "chat.db"
            migrate(database)
            self.config.chat_archive_path = database

            async def clear_during_vision(*args, **kwargs):
                PrivateMemoryStore(database).clear_private_layers(
                    user_id="123456",
                    actor="900",
                    reason="测试清空",
                    operation_id=1,
                )
                return _vision_result(
                    images=(VisionImage(b"raw", "image/png", "456", 0),),
                    descriptions=("一朵白花",),
                )

            understand = AsyncMock(side_effect=clear_during_vision)
            generate = AsyncMock(return_value="不应回复")
            bot = AsyncMock()
            with self._runtime(), patch.object(
                private_matcher,
                "understand_private_images",
                new=understand,
                create=True,
            ), patch.object(
                private_matcher, "generate_reply", new=generate
            ), patch.object(private_matcher, "choose_sticker", return_value=None):
                await private_matcher.handle_private_message(
                    bot,
                    _private_event(
                        image_urls=("https://images.invalid/private.png",)
                    ),
                )

            understand.assert_awaited_once()
            generate.assert_not_awaited()
            bot.send_private_msg.assert_not_awaited()

    async def test_memory_clear_during_ai_prevents_send(self) -> None:
        self.features = _MutableFeatures(persistent=True)
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "chat.db"
            migrate(database)
            self.config.chat_archive_path = database
            image = VisionImage(b"raw", "image/png", "456", 0)
            understand = AsyncMock(
                return_value=_vision_result(
                    images=(image,), descriptions=("一朵白花",)
                )
            )

            async def clear_during_ai(*args, **kwargs):
                PrivateMemoryStore(database).clear_private_layers(
                    user_id="123456",
                    actor="900",
                    reason="测试清空",
                    operation_id=1,
                )
                return "不应发送"

            generate = AsyncMock(side_effect=clear_during_ai)
            bot = AsyncMock()
            with self._runtime(), patch.object(
                private_matcher,
                "understand_private_images",
                new=understand,
                create=True,
            ), patch.object(
                private_matcher, "generate_reply", new=generate
            ), patch.object(private_matcher, "choose_sticker", return_value=None):
                await private_matcher.handle_private_message(
                    bot,
                    _private_event(
                        image_urls=("https://images.invalid/private.png",)
                    ),
                )

            generate.assert_awaited_once()
            bot.send_private_msg.assert_not_awaited()

    async def test_memory_disable_after_first_reply_stops_remaining_delivery(
        self,
    ) -> None:
        self.features = _MutableFeatures(persistent=True)
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "chat.db"
            migrate(database)
            self.config.chat_archive_path = database
            bot = AsyncMock()

            async def disable_after_send(**kwargs):
                self.features.persistent = False

            bot.send_private_msg.side_effect = disable_after_send
            generate = AsyncMock(return_value=("第一条", "第二条", "第三条"))
            with self._runtime(), patch.object(
                private_matcher, "generate_reply", new=generate
            ), patch.object(private_matcher, "choose_sticker", return_value=None):
                await private_matcher.handle_private_message(
                    bot,
                    _private_event("继续聊"),
                )

            self.assertEqual(1, bot.send_private_msg.await_count)
            context = PrivateMemoryStore(database).recent_context(
                user_id="123456", limit=10
            )
            self.assertEqual([False, True], [message.is_bot for message in context])

    async def test_memory_clear_during_first_reply_stops_delivery_and_persistence(
        self,
    ) -> None:
        self.features = _MutableFeatures(persistent=True)
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "chat.db"
            migrate(database)
            self.config.chat_archive_path = database
            bot = AsyncMock()

            async def clear_after_send(**kwargs):
                PrivateMemoryStore(database).clear_private_layers(
                    user_id="123456",
                    actor="900",
                    reason="测试发送中清空",
                    operation_id=1,
                )

            bot.send_private_msg.side_effect = clear_after_send
            generate = AsyncMock(return_value=("第一条", "第二条", "第三条"))
            with self._runtime(), patch.object(
                private_matcher, "generate_reply", new=generate
            ), patch.object(private_matcher, "choose_sticker", return_value=None):
                await private_matcher.handle_private_message(
                    bot,
                    _private_event("清空这轮"),
                )

            self.assertEqual(1, bot.send_private_msg.await_count)
            context = PrivateMemoryStore(database).recent_context(
                user_id="123456", limit=10
            )
            self.assertEqual((), context)

    async def test_replay_with_existing_assistant_skips_image_download(self) -> None:
        self.features = _MutableFeatures(persistent=True)
        state = SimpleNamespace(
            row_id=1,
            created=False,
            live=True,
            assistant_exists=True,
        )
        conversation = Mock()
        conversation.lock = asyncio.Lock()
        conversation.snapshot.return_value = ()
        conversation.append_user_state.return_value = state
        understand = AsyncMock(return_value=_vision_result())
        generate = AsyncMock(return_value="不应回复")
        with self._runtime(conversations={"123456": conversation}), patch.object(
            private_matcher, "PrivateMemoryStore", return_value=Mock()
        ), patch.object(private_matcher, "MemoryJobQueue", return_value=Mock()), patch.object(
            private_matcher, "RelationshipStore", return_value=Mock()
        ), patch.object(
            private_matcher,
            "understand_private_images",
            new=understand,
            create=True,
        ), patch.object(private_matcher, "generate_reply", new=generate):
            await private_matcher.handle_private_message(
                AsyncMock(),
                _private_event(image_urls=("https://images.invalid/replay.png",)),
            )

        understand.assert_not_awaited()
        generate.assert_not_awaited()

    async def test_persistent_description_is_updated_without_group_vision_scope(self) -> None:
        self.features = _MutableFeatures(persistent=True)
        state = SimpleNamespace(
            row_id=1,
            created=True,
            live=True,
            assistant_exists=False,
        )
        conversation = Mock()
        conversation.lock = asyncio.Lock()
        conversation.snapshot.return_value = ()
        conversation.append_user_state.return_value = state
        store = Mock()
        store.update_user_image_descriptions.return_value = True
        image = VisionImage(b"raw", "image/png", "456", 0)
        understand = AsyncMock(
            return_value=_vision_result(images=(image,), descriptions=("一朵白花",))
        )
        generate = AsyncMock(return_value="回复")
        enqueue = Mock()
        with self._runtime(conversations={"123456": conversation}), patch.object(
            private_matcher, "PrivateMemoryStore", return_value=store
        ), patch.object(private_matcher, "MemoryJobQueue", return_value=Mock()), patch.object(
            private_matcher, "RelationshipStore", return_value=Mock()
        ), patch.object(
            private_matcher,
            "understand_private_images",
            new=understand,
            create=True,
        ), patch.object(
            private_matcher, "_private_profile", return_value=()
        ), patch.object(
            private_matcher, "_enqueue_private_jobs", new=enqueue
        ), patch.object(
            private_matcher, "generate_reply", new=generate
        ), patch.object(private_matcher, "choose_sticker", return_value=None):
            await private_matcher.handle_private_message(
                AsyncMock(),
                _private_event(image_urls=("https://images.invalid/flower.png",)),
            )

        store.update_user_image_descriptions.assert_called_once_with(
            user_id="123456",
            message_id="456",
            image_descriptions=("一朵白花",),
            source_kind="image",
        )
        conversation.replace_user_turn.assert_called_once()
        self.assertEqual((image,), generate.await_args.kwargs["images"])
        self.assertEqual(
            ("一朵白花",),
            generate.await_args.kwargs["current"].image_descriptions,
        )
        enqueue.assert_not_called()


if __name__ == "__main__":
    unittest.main()
