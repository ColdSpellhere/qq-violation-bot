from __future__ import annotations

import hashlib
import os
import stat
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

import httpx

from plugins.chat_vision.download import DownloadedChatImage, download_chat_image, write_chat_image
from plugins.chat_vision.service import cleanup_expired
from plugins.chat_vision.store import ChatVisionStore


JPEG = b"\xff\xd8\xff\xe0" + (b"x" * 32) + b"\xff\xd9"
PUBLIC_RESOLVER = lambda host: ["93.184.216.34"]


class ChatVisionFileTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.temporary_directory = TemporaryDirectory()
        self.root = Path(self.temporary_directory.name)
        self.store = ChatVisionStore(self.root / "data" / "chat_vision.db")

    def tearDown(self) -> None:
        self.temporary_directory.cleanup()

    def _client(self, content: bytes, mime_type: str) -> httpx.AsyncClient:
        transport = httpx.MockTransport(
            lambda request: httpx.Response(
                200,
                headers={"content-type": mime_type},
                content=content,
            )
        )
        return httpx.AsyncClient(transport=transport)

    async def test_valid_jpeg_is_downloaded_and_stored_with_restricted_permissions(self) -> None:
        async with self._client(JPEG, "image/jpeg") as client:
            image = await download_chat_image(
                "https://cdn.example/flower.jpg",
                client=client,
                resolver=PUBLIC_RESOLVER,
                max_bytes=1024,
            )

        chat_root = self.root / "data" / "chat_vision" / "images"
        relative_path, digest = write_chat_image(
            chat_root,
            group_id=100,
            event_time=1_755_734_400,
            message_id="m1",
            ordinal=1,
            image=image,
        )
        stored = chat_root / relative_path

        self.assertEqual("image/jpeg", image.mime_type)
        self.assertEqual("jpg", image.extension)
        self.assertEqual("100/2025-08-21/m1-1.jpg", relative_path)
        self.assertEqual(JPEG, stored.read_bytes())
        self.assertEqual(hashlib.sha256(JPEG).hexdigest(), digest)
        self.assertEqual(0o600, stat.S_IMODE(stored.stat().st_mode))

    async def test_download_rejects_content_over_the_byte_limit(self) -> None:
        async with self._client(JPEG * 100, "image/jpeg") as client:
            with self.assertRaisesRegex(ValueError, "size limit"):
                await download_chat_image(
                    "https://cdn.example/large.jpg",
                    client=client,
                    resolver=PUBLIC_RESOLVER,
                    max_bytes=32,
                )

    async def test_download_rejects_mime_type_with_invalid_signature(self) -> None:
        async with self._client(b"not a jpeg", "image/jpeg") as client:
            with self.assertRaisesRegex(ValueError, "supported image"):
                await download_chat_image(
                    "https://cdn.example/not-image.jpg",
                    client=client,
                    resolver=PUBLIC_RESOLVER,
                    max_bytes=1024,
                )

    async def test_download_rejects_private_resolved_address(self) -> None:
        async with self._client(JPEG, "image/jpeg") as client:
            with self.assertRaisesRegex(ValueError, "non-public"):
                await download_chat_image(
                    "https://cdn.example/private.jpg",
                    client=client,
                    resolver=lambda host: ["127.0.0.1"],
                    max_bytes=1024,
                )

    async def test_download_redacts_url_from_http_status_error(self) -> None:
        transport = httpx.MockTransport(
            lambda request: httpx.Response(404, request=request)
        )
        async with httpx.AsyncClient(transport=transport) as client:
            with self.assertRaisesRegex(ValueError, "HTTP status error") as raised:
                await download_chat_image(
                    "https://cdn.example/image.jpg?access_token=secret-token",
                    client=client,
                    resolver=PUBLIC_RESOLVER,
                    max_bytes=1024,
                )

        self.assertNotIn("secret-token", str(raised.exception))
        self.assertNotIn("cdn.example", str(raised.exception))

    async def test_download_redacts_url_from_transport_error(self) -> None:
        def failing_transport(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("request to https://cdn.example/?token=secret-token failed", request=request)

        async with httpx.AsyncClient(transport=httpx.MockTransport(failing_transport)) as client:
            with self.assertRaisesRegex(ValueError, "request failed") as raised:
                await download_chat_image(
                    "https://cdn.example/image.jpg?access_token=secret-token",
                    client=client,
                    resolver=PUBLIC_RESOLVER,
                    max_bytes=1024,
                )

        self.assertNotIn("secret-token", str(raised.exception))
        self.assertNotIn("cdn.example", str(raised.exception))

    async def test_write_refuses_a_symlinked_destination_directory(self) -> None:
        chat_root = self.root / "data" / "chat_vision" / "images"
        outside = self.root / "outside"
        outside.mkdir()
        chat_root.mkdir(parents=True)
        os.symlink(outside, chat_root / "100")

        with self.assertRaisesRegex(ValueError, "symlink"):
            write_chat_image(
                chat_root,
                group_id=100,
                event_time=1_755_734_400,
                message_id="m1",
                ordinal=1,
                image=DownloadedChatImage(JPEG, "image/jpeg", "jpg"),
            )

    async def test_cleanup_refuses_to_unlink_a_symlinked_asset(self) -> None:
        chat_root = self.root / "data" / "chat_vision" / "images"
        outside = self.root / "outside.jpg"
        outside.write_bytes(b"outside")
        relative_path = "100/2026-08-21/m1-1.jpg"
        asset_path = chat_root / relative_path
        asset_path.parent.mkdir(parents=True)
        os.symlink(outside, asset_path)
        asset = self.store.ensure_pending(100, "m1", 1, "https://cdn.example/1.jpg", 1000)
        self.store.mark_downloaded(
            asset.id,
            relative_path,
            "image/jpeg",
            len(JPEG),
            hashlib.sha256(JPEG).hexdigest(),
            "2026-08-28 00:00:00",
        )

        await cleanup_expired(self.store, chat_root, now_text="2026-08-29 00:00:00")

        self.assertTrue(asset_path.is_symlink())
        self.assertEqual(b"outside", outside.read_bytes())
        self.assertEqual(relative_path, self.store.for_message(100, "m1")[0].relative_path)

    async def test_cleanup_does_not_mark_a_missing_asset_as_deleted(self) -> None:
        relative_path = "100/2026-08-21/missing-1.jpg"
        asset = self.store.ensure_pending(100, "missing", 1, "https://cdn.example/1.jpg", 1000)
        self.store.mark_downloaded(
            asset.id,
            relative_path,
            "image/jpeg",
            len(JPEG),
            hashlib.sha256(JPEG).hexdigest(),
            "2026-08-28 00:00:00",
        )

        await cleanup_expired(
            self.store,
            self.root / "data" / "chat_vision" / "images",
            now_text="2026-08-29 00:00:00",
        )

        self.assertEqual(relative_path, self.store.for_message(100, "missing")[0].relative_path)

    async def test_cleanup_never_touches_evidence_sibling(self) -> None:
        chat_root = self.root / "data" / "chat_vision" / "images"
        evidence_root = self.root / "evidence"
        evidence_root.mkdir(parents=True)
        sentinel = evidence_root / "keep.jpg"
        sentinel.write_bytes(b"evidence")
        asset_path = chat_root / "100" / "2026-08-21" / "m1-1.jpg"
        asset_path.parent.mkdir(parents=True)
        asset_path.write_bytes(b"chat")
        asset = self.store.ensure_pending(100, "m1", 1, "https://cdn.example/1.jpg", 1000)
        self.store.mark_downloaded(
            asset.id,
            "100/2026-08-21/m1-1.jpg",
            "image/jpeg",
            len(JPEG),
            hashlib.sha256(JPEG).hexdigest(),
            "2026-08-28 00:00:00",
        )

        await cleanup_expired(self.store, chat_root, now_text="2026-08-29 00:00:00")

        self.assertFalse(asset_path.exists())
        self.assertIsNone(self.store.for_message(100, "m1")[0].relative_path)
        self.assertEqual(b"evidence", sentinel.read_bytes())

    async def test_cleanup_refuses_root_symlink_that_points_to_evidence(self) -> None:
        evidence_root = self.root / "evidence"
        asset_path = evidence_root / "100" / "2026-08-21" / "m1-1.jpg"
        asset_path.parent.mkdir(parents=True)
        asset_path.write_bytes(b"evidence")
        chat_root = self.root / "data" / "chat_vision" / "images"
        chat_root.parent.mkdir(parents=True)
        os.symlink(evidence_root, chat_root)
        asset = self.store.ensure_pending(100, "m1", 1, "https://cdn.example/1.jpg", 1000)
        self.store.mark_downloaded(
            asset.id,
            "100/2026-08-21/m1-1.jpg",
            "image/jpeg",
            len(JPEG),
            hashlib.sha256(JPEG).hexdigest(),
            "2026-08-28 00:00:00",
        )

        await cleanup_expired(self.store, chat_root, now_text="2026-08-29 00:00:00")

        self.assertEqual(b"evidence", asset_path.read_bytes())
        self.assertEqual("100/2026-08-21/m1-1.jpg", self.store.for_message(100, "m1")[0].relative_path)

    async def test_cleanup_refuses_an_intermediate_symlink_inside_the_root(self) -> None:
        chat_root = self.root / "data" / "chat_vision" / "images"
        alternate = chat_root / "alternate"
        asset_path = alternate / "2026-08-21" / "m1-1.jpg"
        asset_path.parent.mkdir(parents=True)
        asset_path.write_bytes(b"chat")
        os.symlink(alternate, chat_root / "100")
        asset = self.store.ensure_pending(100, "m1", 1, "https://cdn.example/1.jpg", 1000)
        self.store.mark_downloaded(
            asset.id,
            "100/2026-08-21/m1-1.jpg",
            "image/jpeg",
            len(JPEG),
            hashlib.sha256(JPEG).hexdigest(),
            "2026-08-28 00:00:00",
        )

        await cleanup_expired(self.store, chat_root, now_text="2026-08-29 00:00:00")

        self.assertEqual(b"chat", asset_path.read_bytes())
        self.assertEqual("100/2026-08-21/m1-1.jpg", self.store.for_message(100, "m1")[0].relative_path)


if __name__ == "__main__":
    unittest.main()
