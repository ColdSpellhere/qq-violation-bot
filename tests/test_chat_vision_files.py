from __future__ import annotations

import hashlib
import os
import stat
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import patch

import httpx
import httpcore

from plugins.chat_vision import download as download_module
from plugins.chat_vision import service
from plugins.chat_vision.download import (
    DownloadedChatImage,
    download_chat_image,
    write_chat_image,
)
from plugins.chat_vision.service import cleanup_expired
from plugins.chat_vision.store import ChatVisionStore


JPEG = b"\xff\xd8\xff\xe0" + (b"x" * 32) + b"\xff\xd9"
PUBLIC_RESOLVER = lambda host: ["93.184.216.34"]


class _MemoryStream:
    def __init__(self, response: bytes, *, peer: str = "93.184.216.34") -> None:
        self.response = response
        self.peer = peer
        self.closed = False

    async def read(self, max_bytes: int, timeout: float | None = None) -> bytes:
        chunk, self.response = self.response[:max_bytes], self.response[max_bytes:]
        return chunk

    async def write(self, buffer: bytes, timeout: float | None = None) -> None:
        return None

    async def aclose(self) -> None:
        self.closed = True

    async def start_tls(self, ssl_context, server_hostname=None, timeout=None):
        return self

    def get_extra_info(self, name: str):
        if name == "server_addr":
            return (self.peer, 443)
        if name == "is_readable":
            return False
        return None


class _MemoryBackend:
    def __init__(
        self,
        content: bytes = b"",
        mime_type: str = "image/jpeg",
        *,
        status: int = 200,
        error: BaseException | None = None,
    ) -> None:
        reason = "OK" if status == 200 else "Error"
        headers = (
            f"HTTP/1.1 {status} {reason}\r\n"
            f"Content-Type: {mime_type}\r\n"
            f"Content-Length: {len(content)}\r\n"
            "Connection: close\r\n\r\n"
        ).encode("ascii")
        self.stream = _MemoryStream(headers + content)
        self.error = error

    async def connect_tcp(self, host: str, port: int, **kwargs):
        if self.error is not None:
            raise self.error
        return self.stream

    async def connect_unix_socket(self, *args, **kwargs):
        raise AssertionError("Unix socket must not be used")

    async def sleep(self, seconds: float) -> None:
        return None


class ChatVisionFileTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.temporary_directory = TemporaryDirectory()
        self.root = Path(self.temporary_directory.name)
        self.store = ChatVisionStore(self.root / "data" / "chat_vision.db")
        self.chat_root = self.root / "data" / "chat_vision" / "images"
        self.config_patch = patch.object(
            service,
            "CONFIG",
            SimpleNamespace(chat_vision_root=self.chat_root),
        )
        self.config_patch.start()

    def tearDown(self) -> None:
        self.config_patch.stop()
        self.temporary_directory.cleanup()

    def _backend(
        self,
        content: bytes,
        mime_type: str,
        *,
        status: int = 200,
        error: BaseException | None = None,
    ) -> _MemoryBackend:
        return _MemoryBackend(
            content,
            mime_type,
            status=status,
            error=error,
        )

    async def test_valid_jpeg_is_downloaded_and_stored_with_restricted_permissions(self) -> None:
        image = await download_chat_image(
            "https://cdn.example/flower.jpg",
            resolver=PUBLIC_RESOLVER,
            max_bytes=1024,
            timeout=5,
            network_backend=self._backend(JPEG, "image/jpeg"),
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
        for directory in (
            chat_root.parent,
            chat_root,
            stored.parent.parent,
            stored.parent,
        ):
            with self.subTest(directory=directory):
                self.assertEqual(0o700, stat.S_IMODE(directory.stat().st_mode))

    async def test_pinned_backend_rejects_private_actual_peer_after_public_precheck(
        self,
    ) -> None:
        class FakeStream:
            def __init__(self) -> None:
                self.closed = False

            def get_extra_info(self, name: str):
                return ("127.0.0.1", 443) if name == "server_addr" else None

            async def aclose(self) -> None:
                self.closed = True

        class FakeBackend:
            def __init__(self) -> None:
                self.stream = FakeStream()
                self.hosts: list[str] = []

            async def connect_tcp(self, host: str, port: int, **kwargs):
                self.hosts.append(host)
                return self.stream

        backend_type = getattr(download_module, "PinnedNetworkBackend", None)
        self.assertIsNotNone(
            backend_type,
            "download transport must bind the validated DNS result to the connection",
        )
        if backend_type is None:
            return
        backend = FakeBackend()
        pinned = backend_type(
            "cdn.example",
            ("93.184.216.34",),
            backend=backend,
        )

        with self.assertRaisesRegex(ValueError, "peer"):
            await pinned.connect_tcp("cdn.example", 443)

        self.assertEqual(["93.184.216.34"], backend.hosts)
        self.assertTrue(backend.stream.closed)

    async def test_download_rejects_content_over_the_byte_limit(self) -> None:
        with self.assertRaisesRegex(ValueError, "size limit"):
            await download_chat_image(
                "https://cdn.example/large.jpg",
                resolver=PUBLIC_RESOLVER,
                max_bytes=32,
                timeout=5,
                network_backend=self._backend(JPEG * 100, "image/jpeg"),
            )

    async def test_download_rejects_mime_type_with_invalid_signature(self) -> None:
        with self.assertRaisesRegex(ValueError, "supported image"):
            await download_chat_image(
                "https://cdn.example/not-image.jpg",
                resolver=PUBLIC_RESOLVER,
                max_bytes=1024,
                timeout=5,
                network_backend=self._backend(b"not a jpeg", "image/jpeg"),
            )

    async def test_download_rejects_private_resolved_address(self) -> None:
        with self.assertRaisesRegex(ValueError, "non-public"):
            await download_chat_image(
                "https://cdn.example/private.jpg",
                resolver=lambda host: ["127.0.0.1"],
                max_bytes=1024,
                timeout=5,
                network_backend=self._backend(JPEG, "image/jpeg"),
            )

    async def test_download_redacts_url_from_http_status_error(self) -> None:
        with self.assertRaisesRegex(ValueError, "HTTP status error") as raised:
            await download_chat_image(
                "https://cdn.example/image.jpg?access_token=secret-token",
                resolver=PUBLIC_RESOLVER,
                max_bytes=1024,
                timeout=5,
                network_backend=self._backend(b"", "image/jpeg", status=404),
            )

        self.assertNotIn("secret-token", str(raised.exception))
        self.assertNotIn("cdn.example", str(raised.exception))

    async def test_download_redacts_url_from_transport_error(self) -> None:
        with self.assertRaisesRegex(ValueError, "request failed") as raised:
            await download_chat_image(
                "https://cdn.example/image.jpg?access_token=secret-token",
                resolver=PUBLIC_RESOLVER,
                max_bytes=1024,
                timeout=5,
                network_backend=self._backend(
                    error=httpcore.ConnectError(
                        "request to https://cdn.example/?token=secret-token failed"
                    ),
                    content=b"",
                    mime_type="image/jpeg",
                ),
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

        self.assertTrue(asset_path.exists())
        if not asset_path.exists():
            return
        self.assertEqual(b"evidence", asset_path.read_bytes())
        self.assertEqual("100/2026-08-21/m1-1.jpg", self.store.for_message(100, "m1")[0].relative_path)

    async def test_cleanup_refuses_parent_symlink_that_points_to_evidence(self) -> None:
        evidence_root = self.root / "evidence"
        asset_path = evidence_root / "chat_vision" / "images" / "100" / "2026-08-21" / "m1-1.jpg"
        asset_path.parent.mkdir(parents=True)
        asset_path.write_bytes(b"evidence")
        sentinel = evidence_root / "keep.sentinel"
        sentinel.write_bytes(b"never-delete")
        data_link = self.root / "linked-data"
        os.symlink(evidence_root, data_link)
        chat_root = data_link / "chat_vision" / "images"
        asset = self.store.ensure_pending(100, "m1", 1, "https://cdn.example/1.jpg", 1000)
        self.store.mark_downloaded(
            asset.id,
            "100/2026-08-21/m1-1.jpg",
            "image/jpeg",
            len(JPEG),
            hashlib.sha256(JPEG).hexdigest(),
            "2026-08-28 00:00:00",
        )

        with patch.object(
            service,
            "CONFIG",
            SimpleNamespace(chat_vision_root=chat_root),
        ):
            await cleanup_expired(
                self.store,
                chat_root,
                now_text="2026-08-29 00:00:00",
            )

        self.assertTrue(asset_path.exists())
        if not asset_path.exists():
            return
        self.assertEqual(b"evidence", asset_path.read_bytes())
        self.assertEqual(b"never-delete", sentinel.read_bytes())
        self.assertEqual(
            "100/2026-08-21/m1-1.jpg",
            self.store.for_message(100, "m1")[0].relative_path,
        )

    async def test_cleanup_accepts_only_the_exact_configured_root(self) -> None:
        configured_root = self.chat_root
        configured_root.mkdir(parents=True)
        other_root = self.root / "data" / "chat_vision" / "other-images"
        asset_path = other_root / "100" / "2026-08-21" / "m1-1.jpg"
        asset_path.parent.mkdir(parents=True)
        asset_path.write_bytes(b"not-managed")
        asset = self.store.ensure_pending(100, "m1", 1, "https://cdn.example/1.jpg", 1000)
        self.store.mark_downloaded(
            asset.id,
            "100/2026-08-21/m1-1.jpg",
            "image/jpeg",
            len(JPEG),
            hashlib.sha256(JPEG).hexdigest(),
            "2026-08-28 00:00:00",
        )

        await cleanup_expired(
            self.store,
            other_root,
            now_text="2026-08-29 00:00:00",
        )

        self.assertTrue(asset_path.exists())
        if not asset_path.exists():
            return
        self.assertEqual(b"not-managed", asset_path.read_bytes())
        self.assertEqual(
            "100/2026-08-21/m1-1.jpg",
            self.store.for_message(100, "m1")[0].relative_path,
        )

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
