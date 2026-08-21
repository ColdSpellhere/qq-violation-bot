from __future__ import annotations

import asyncio
import sqlite3
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, call, patch

import nonebot
from nonebot.adapters.onebot.v11 import GroupMessageEvent, Message, MessageSegment

try:
    nonebot.get_driver()
except ValueError:
    nonebot.init()

from plugins.chat_vision import lifecycle, matcher, service
from plugins.chat_vision.download import DownloadedChatImage
from plugins.chat_vision.store import ChatVisionStore


GROUP_ID = 999000222
OTHER_GROUP_ID = 999000333
BOT_ID = 999
JPEG_ONE = b"\xff\xd8\xff\xe0one\xff\xd9"
JPEG_TWO = b"\xff\xd8\xff\xe0two\xff\xd9"


def _event(
    *segments: MessageSegment,
    group_id: int = GROUP_ID,
    user_id: int = 123,
    message_id: int = 456,
) -> GroupMessageEvent:
    message = Message(list(segments))
    return GroupMessageEvent(
        time=1_755_734_400,
        self_id=BOT_ID,
        post_type="message",
        sub_type="normal",
        user_id=user_id,
        message_type="group",
        message_id=message_id,
        group_id=group_id,
        message=message,
        original_message=message,
        raw_message=str(message),
        font=0,
        sender={"user_id": user_id, "nickname": "成员", "role": "member"},
    )


def _image(url: str) -> MessageSegment:
    return MessageSegment("image", {"url": url})


class ChatVisionMatcherTests(unittest.TestCase):
    def test_candidate_accepts_enabled_allowed_human_group_message(self) -> None:
        event = _event(_image("https://cdn.example/one.jpg"))
        features = SimpleNamespace(group_chat_allowed=lambda group_id: group_id == GROUP_ID)

        with (
            patch.object(matcher, "CONFIG", SimpleNamespace(chat_vision_enabled=True)),
            patch.object(matcher, "FEATURES", features),
        ):
            self.assertTrue(matcher.chat_image_candidate(event))

    def test_candidate_rejects_disabled_vision(self) -> None:
        event = _event(_image("https://cdn.example/one.jpg"))
        features = SimpleNamespace(group_chat_allowed=lambda group_id: True)

        with (
            patch.object(matcher, "CONFIG", SimpleNamespace(chat_vision_enabled=False)),
            patch.object(matcher, "FEATURES", features),
        ):
            self.assertFalse(matcher.chat_image_candidate(event))

    def test_candidate_rejects_group_outside_runtime_allowlist(self) -> None:
        event = _event(_image("https://cdn.example/one.jpg"), group_id=OTHER_GROUP_ID)
        features = SimpleNamespace(group_chat_allowed=lambda group_id: group_id == GROUP_ID)

        with (
            patch.object(matcher, "CONFIG", SimpleNamespace(chat_vision_enabled=True)),
            patch.object(matcher, "FEATURES", features),
        ):
            self.assertFalse(matcher.chat_image_candidate(event))

    def test_candidate_rejects_non_group_and_self_messages(self) -> None:
        features = SimpleNamespace(group_chat_allowed=lambda group_id: True)
        with (
            patch.object(matcher, "CONFIG", SimpleNamespace(chat_vision_enabled=True)),
            patch.object(matcher, "FEATURES", features),
        ):
            self.assertFalse(matcher.chat_image_candidate(object()))
            self.assertFalse(
                matcher.chat_image_candidate(
                    _event(_image("https://cdn.example/bot.jpg"), user_id=BOT_ID)
                )
            )


class ChatVisionIngestionTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.directory = TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.store = ChatVisionStore(self.root / "chat_archive.db")
        self.config = SimpleNamespace(
            chat_vision_root=self.root / "data" / "chat_vision" / "images",
            chat_vision_retention_days=7,
            chat_vision_max_bytes=1_234_567,
            chat_vision_timeout=17,
            chat_vision_max_retries=3,
            chat_vision_model="vision-test-model",
            ai_base_url="https://ai.example",
            ai_api_key="test-key",
        )

    async def test_all_images_use_stable_ordinals_and_repeat_is_idempotent(self) -> None:
        event = _event(
            MessageSegment.text("两张图"),
            _image("https://cdn.example/one.jpg"),
            _image("https://cdn.example/two.jpg"),
        )
        download = AsyncMock(
            side_effect=[
                DownloadedChatImage(JPEG_ONE, "image/jpeg", "jpg"),
                DownloadedChatImage(JPEG_TWO, "image/jpeg", "jpg"),
            ]
        )

        async def describe(content: bytes, mime_type: str, **kwargs: object) -> str:
            downloaded = [
                asset
                for asset in self.store.for_message(GROUP_ID, "456")
                if asset.sha256 is not None
            ]
            self.assertTrue(any(asset.byte_size == len(content) for asset in downloaded))
            self.assertTrue(all(asset.relative_path is not None for asset in downloaded))
            return "第一张" if content == JPEG_ONE else "第二张"

        with (
            patch.object(service, "STORE", self.store),
            patch.object(service, "CONFIG", self.config),
            patch.object(service, "download_chat_image", new=download),
            patch.object(service, "describe_image", new=describe),
        ):
            first = await service.process_image_event(event)
            second = await service.process_image_event(event)

        self.assertEqual([1, 2], [asset.ordinal for asset in first])
        self.assertEqual(["ready", "ready"], [asset.status for asset in second])
        self.assertEqual([1, 1], [asset.attempts for asset in second])
        self.assertEqual(["第一张", "第二张"], [asset.description for asset in second])
        self.assertEqual(2, download.await_count)
        self.assertEqual(
            [self.config.chat_vision_max_bytes, self.config.chat_vision_max_bytes],
            [item.kwargs["max_bytes"] for item in download.await_args_list],
        )
        self.assertEqual(
            [self.config.chat_vision_timeout, self.config.chat_vision_timeout],
            [item.kwargs["timeout"] for item in download.await_args_list],
        )

    async def test_all_ordinals_exist_before_the_first_network_call(self) -> None:
        event = _event(
            _image("https://cdn.example/one.jpg"),
            _image("https://cdn.example/two.jpg"),
            _image("https://cdn.example/three.jpg"),
        )

        observed_ordinals: list[list[int]] = []

        async def cancelled_download(*args, **kwargs):
            observed_ordinals.append(
                [
                    asset.ordinal
                    for asset in self.store.for_message(GROUP_ID, "456")
                ]
            )
            raise asyncio.CancelledError

        with (
            patch.object(service, "STORE", self.store),
            patch.object(service, "CONFIG", self.config),
            patch.object(service, "download_chat_image", new=cancelled_download),
        ):
            with self.assertRaises(asyncio.CancelledError):
                await service.process_image_event(event)

        self.assertTrue(observed_ordinals)
        self.assertTrue(
            all(ordinals == [1, 2, 3] for ordinals in observed_ordinals)
        )
        self.assertEqual(
            [1, 2, 3],
            [asset.ordinal for asset in self.store.for_message(GROUP_ID, "456")],
        )

    async def test_processing_uses_small_bounded_concurrency_for_all_images(self) -> None:
        event = _event(
            *(
                _image(f"https://cdn.example/{ordinal}.jpg")
                for ordinal in range(1, 7)
            )
        )
        active = 0
        maximum_active = 0
        release = asyncio.Event()

        async def download(*args, **kwargs):
            nonlocal active, maximum_active
            active += 1
            maximum_active = max(maximum_active, active)
            if maximum_active >= 3:
                release.set()
            await asyncio.wait_for(release.wait(), timeout=1)
            await asyncio.sleep(0)
            active -= 1
            return DownloadedChatImage(JPEG_ONE, "image/jpeg", "jpg")

        with (
            patch.object(service, "STORE", self.store),
            patch.object(service, "CONFIG", self.config),
            patch.object(service, "download_chat_image", new=download),
            patch.object(
                service,
                "describe_image",
                new=AsyncMock(return_value="图片"),
            ),
        ):
            assets = await service.process_image_event(event)

        self.assertEqual(3, maximum_active)
        self.assertEqual(6, len(assets))
        self.assertTrue(all(asset.status == "ready" for asset in assets))

    async def test_concurrent_duplicate_events_claim_each_ordinal_once(self) -> None:
        event = _event(
            _image("https://cdn.example/one.jpg"),
            _image("https://cdn.example/two.jpg"),
        )
        started = 0
        barrier = asyncio.Event()

        async def download(*args, **kwargs):
            nonlocal started
            started += 1
            if started == 2:
                barrier.set()
            await asyncio.wait_for(barrier.wait(), timeout=1)
            return DownloadedChatImage(JPEG_ONE, "image/jpeg", "jpg")

        with (
            patch.object(service, "STORE", self.store),
            patch.object(service, "CONFIG", self.config),
            patch.object(service, "download_chat_image", new=download),
            patch.object(
                service,
                "describe_image",
                new=AsyncMock(return_value="图片"),
            ) as describe,
        ):
            first, second = await asyncio.gather(
                service.process_image_event(event),
                service.process_image_event(event),
            )

        self.assertEqual(2, started)
        self.assertEqual(2, describe.await_count)
        self.assertEqual([1, 1], [asset.attempts for asset in first])
        self.assertEqual([1, 1], [asset.attempts for asset in second])

    async def test_message_without_images_creates_no_rows(self) -> None:
        with (
            patch.object(service, "STORE", self.store),
            patch.object(service, "CONFIG", self.config),
            patch.object(service, "download_chat_image", new=AsyncMock()) as download,
            patch.object(service, "describe_image", new=AsyncMock()) as describe,
        ):
            assets = await service.process_image_event(_event(MessageSegment.text("只有文字")))

        self.assertEqual([], assets)
        self.assertEqual([], self.store.for_message(GROUP_ID, "456"))
        download.assert_not_awaited()
        describe.assert_not_awaited()

    async def test_single_image_failure_records_only_type_and_continues(self) -> None:
        event = _event(
            _image("https://cdn.example/broken.jpg"),
            _image("https://cdn.example/two.jpg"),
        )
        download = AsyncMock(
            side_effect=[
                OSError("credential-bearing failure detail"),
                DownloadedChatImage(JPEG_TWO, "image/jpeg", "jpg"),
            ]
        )
        with (
            patch.object(service, "STORE", self.store),
            patch.object(service, "CONFIG", self.config),
            patch.object(service, "download_chat_image", new=download),
            patch.object(service, "describe_image", new=AsyncMock(return_value="第二张")),
        ):
            assets = await service.process_image_event(event)

        self.assertEqual(["failed", "ready"], [asset.status for asset in assets])
        with sqlite3.connect(self.store.database_path) as conn:
            error_type = conn.execute(
                "SELECT error_type FROM chat_image_assets WHERE ordinal=1"
            ).fetchone()[0]
        self.assertEqual("OSError", error_type)
        self.assertNotIn("credential", error_type)

    async def test_retry_with_valid_file_skips_download_and_retries_description(self) -> None:
        event = _event(_image("https://cdn.example/one.jpg"))
        download = AsyncMock(
            return_value=DownloadedChatImage(JPEG_ONE, "image/jpeg", "jpg")
        )
        describe = AsyncMock(side_effect=[RuntimeError("first failure"), "重试成功"])
        with (
            patch.object(service, "STORE", self.store),
            patch.object(service, "CONFIG", self.config),
            patch.object(service, "download_chat_image", new=download),
            patch.object(service, "describe_image", new=describe),
        ):
            failed = await service.process_image_event(event)
            ready = await service.process_image_event(event)

        self.assertEqual("failed", failed[0].status)
        self.assertIsNotNone(failed[0].relative_path)
        self.assertEqual("ready", ready[0].status)
        self.assertEqual("重试成功", ready[0].description)
        self.assertEqual(2, ready[0].attempts)
        download.assert_awaited_once()
        self.assertEqual(2, describe.await_count)

    async def test_mark_downloaded_failure_removes_the_just_written_file(self) -> None:
        event = _event(_image("https://cdn.example/one.jpg"))
        with (
            patch.object(service, "STORE", self.store),
            patch.object(service, "CONFIG", self.config),
            patch.object(
                service,
                "download_chat_image",
                new=AsyncMock(
                    return_value=DownloadedChatImage(
                        JPEG_ONE,
                        "image/jpeg",
                        "jpg",
                    )
                ),
            ),
            patch.object(
                self.store,
                "mark_downloaded",
                side_effect=RuntimeError("database write failed"),
            ),
            patch.object(service, "describe_image", new=AsyncMock()) as describe,
        ):
            assets = await service.process_image_event(event)

        self.assertEqual("failed", assets[0].status)
        self.assertEqual(
            [],
            [path for path in self.config.chat_vision_root.rglob("*") if path.is_file()],
        )
        describe.assert_not_awaited()

    async def test_mark_downloaded_failure_never_unlinks_outside_the_managed_root(
        self,
    ) -> None:
        evidence_root = self.root / "evidence"
        evidence_root.mkdir()
        sentinel = evidence_root / "keep.jpg"
        sentinel.write_bytes(b"evidence")
        self.config.chat_vision_root.mkdir(parents=True)
        event = _event(_image("https://cdn.example/one.jpg"))
        with (
            patch.object(service, "STORE", self.store),
            patch.object(service, "CONFIG", self.config),
            patch.object(
                service,
                "download_chat_image",
                new=AsyncMock(
                    return_value=DownloadedChatImage(
                        JPEG_ONE,
                        "image/jpeg",
                        "jpg",
                    )
                ),
            ),
            patch.object(
                service,
                "write_chat_image",
                return_value=(
                    "../../../evidence/keep.jpg",
                    "synthetic-digest",
                ),
            ),
            patch.object(
                self.store,
                "mark_downloaded",
                side_effect=RuntimeError("database write failed"),
            ),
        ):
            await service.process_image_event(event)

        self.assertEqual(b"evidence", sentinel.read_bytes())

    async def test_recovery_reads_only_store_claimable_rows(self) -> None:
        pending = [
            SimpleNamespace(id=1),
            SimpleNamespace(id=2),
            SimpleNamespace(id=3),
        ]
        store = MagicMock()
        store.claimable.side_effect = [pending[:2], pending[2:], []]
        processor = AsyncMock()

        with patch("plugins.chat_archive.db.recent_text_context") as archive_scan:
            await service.recover_pending(
                store,
                processor,
                max_retries=3,
                batch_size=2,
            )

        self.assertEqual(
            [
                call(3, after_id=0, limit=2),
                call(3, after_id=2, limit=2),
                call(3, after_id=3, limit=2),
            ],
            store.claimable.call_args_list,
        )
        self.assertEqual(
            [call(pending[0]), call(pending[1]), call(pending[2])],
            processor.await_args_list,
        )
        archive_scan.assert_not_called()


class FakeDriver:
    def __init__(self) -> None:
        self.startup_callback = None
        self.shutdown_callback = None

    def on_startup(self, callback):
        self.startup_callback = callback
        return callback

    def on_shutdown(self, callback):
        self.shutdown_callback = callback
        return callback

    async def startup(self) -> None:
        assert self.startup_callback is not None
        await self.startup_callback()

    async def shutdown(self) -> None:
        assert self.shutdown_callback is not None
        await self.shutdown_callback()


class RetainingFakeDriver(FakeDriver):
    def __init__(self) -> None:
        super().__init__()
        self.startup_callbacks = []
        self.shutdown_callbacks = []

    def on_startup(self, callback):
        self.startup_callbacks.append(callback)
        return super().on_startup(callback)

    def on_shutdown(self, callback):
        self.shutdown_callbacks.append(callback)
        return super().on_shutdown(callback)


class ChatVisionLifecycleTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        lifecycle._cleanup_task = None
        lifecycle._store = None

    async def asyncTearDown(self) -> None:
        task = lifecycle._cleanup_task
        if task is not None:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
        lifecycle._cleanup_task = None
        lifecycle._store = None

    async def test_enabled_startup_initializes_recovers_cleans_then_starts_worker(self) -> None:
        driver = FakeDriver()
        store = MagicMock()
        steps: list[str] = []
        store_factory = MagicMock(side_effect=lambda path: steps.append("init") or store)
        store.recover_interrupted_claims.side_effect = lambda: steps.append("reset")

        async def recover(*args: object, **kwargs: object) -> None:
            steps.append("recover")

        async def cleanup(*args: object, **kwargs: object) -> None:
            steps.append("cleanup")

        async def forever(*args: object, **kwargs: object) -> None:
            steps.append("worker")
            await asyncio.Event().wait()

        config = SimpleNamespace(
            chat_archive_path=Path("chat_archive.db"),
            chat_vision_enabled=True,
            chat_vision_max_retries=3,
            chat_vision_root=Path("images"),
        )
        with (
            patch.object(lifecycle, "get_driver", return_value=driver),
            patch.object(lifecycle, "CONFIG", config),
            patch.object(lifecycle, "ChatVisionStore", new=store_factory),
            patch.object(lifecycle, "recover_pending", new=recover),
            patch.object(lifecycle, "cleanup_expired", new=cleanup),
            patch.object(lifecycle, "_daily_cleanup_loop", new=forever),
        ):
            lifecycle.setup_lifecycle()
            await driver.startup()
            await asyncio.sleep(0)
            await driver.shutdown()

        self.assertEqual(["init", "reset", "recover", "cleanup", "worker"], steps)
        store_factory.assert_called_once_with(config.chat_archive_path)

    async def test_disabled_startup_initializes_and_cleans_without_recovery(self) -> None:
        driver = FakeDriver()
        store = MagicMock()

        async def forever(*args: object, **kwargs: object) -> None:
            await asyncio.Event().wait()

        config = SimpleNamespace(
            chat_archive_path=Path("chat_archive.db"),
            chat_vision_enabled=False,
            chat_vision_max_retries=3,
            chat_vision_root=Path("images"),
        )
        with (
            patch.object(lifecycle, "get_driver", return_value=driver),
            patch.object(lifecycle, "CONFIG", config),
            patch.object(lifecycle, "ChatVisionStore", return_value=store) as factory,
            patch.object(lifecycle, "recover_pending", new=AsyncMock()) as recover,
            patch.object(lifecycle, "cleanup_expired", new=AsyncMock()) as cleanup,
            patch.object(lifecycle, "_daily_cleanup_loop", new=forever),
        ):
            lifecycle.setup_lifecycle()
            await driver.startup()
            await driver.shutdown()

        factory.assert_called_once_with(config.chat_archive_path)
        store.recover_interrupted_claims.assert_not_called()
        recover.assert_not_awaited()
        cleanup.assert_awaited_once()

    async def test_repeated_startup_keeps_one_cleanup_task(self) -> None:
        driver = FakeDriver()
        store = MagicMock()
        started = 0

        async def forever(*args: object, **kwargs: object) -> None:
            nonlocal started
            started += 1
            await asyncio.Event().wait()

        config = SimpleNamespace(
            chat_archive_path=Path("chat_archive.db"),
            chat_vision_enabled=False,
            chat_vision_max_retries=3,
            chat_vision_root=Path("images"),
        )
        with (
            patch.object(lifecycle, "get_driver", return_value=driver),
            patch.object(lifecycle, "CONFIG", config),
            patch.object(lifecycle, "ChatVisionStore", return_value=store),
            patch.object(lifecycle, "cleanup_expired", new=AsyncMock()),
            patch.object(lifecycle, "_daily_cleanup_loop", new=forever),
        ):
            lifecycle.setup_lifecycle()
            await driver.startup()
            first = lifecycle._cleanup_task
            await asyncio.sleep(0)
            await driver.startup()
            second = lifecycle._cleanup_task
            await driver.shutdown()

        self.assertIs(first, second)
        self.assertEqual(1, started)
        self.assertTrue(first.cancelled())

    async def test_setup_registers_only_one_callback_pair_per_driver(self) -> None:
        driver = RetainingFakeDriver()
        with patch.object(lifecycle, "get_driver", return_value=driver):
            lifecycle.setup_lifecycle()
            lifecycle.setup_lifecycle()

        self.assertEqual(1, len(driver.startup_callbacks))
        self.assertEqual(1, len(driver.shutdown_callbacks))


if __name__ == "__main__":
    unittest.main()
