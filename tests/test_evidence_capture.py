from __future__ import annotations

import unittest

import httpx

from plugins.violation_record.evidence_capture import download_image


JPEG = b"\xff\xd8\xff\xe0" + (b"x" * 32) + b"\xff\xd9"
PUBLIC_RESOLVER = lambda host: ["93.184.216.34"]


class EvidenceCaptureTests(unittest.IsolatedAsyncioTestCase):
    def _client(self, content: bytes, mime_type: str) -> httpx.AsyncClient:
        transport = httpx.MockTransport(
            lambda request: httpx.Response(
                200,
                headers={"content-type": mime_type},
                content=content,
            )
        )
        return httpx.AsyncClient(transport=transport)

    async def test_valid_jpeg_is_returned(self) -> None:
        async with self._client(JPEG, "image/jpeg") as client:
            downloaded = await download_image(
                "https://multimedia.nt.qq.com.cn/evidence.jpg",
                client=client,
                resolver=PUBLIC_RESOLVER,
                max_bytes=1024,
            )
        self.assertEqual("image/jpeg", downloaded.mime_type)
        self.assertEqual(JPEG, downloaded.content)

    async def test_private_destination_is_rejected(self) -> None:
        async with self._client(JPEG, "image/jpeg") as client:
            with self.assertRaisesRegex(ValueError, "non-public"):
                await download_image(
                    "https://example.invalid/evidence.jpg",
                    client=client,
                    resolver=lambda host: ["127.0.0.1"],
                    max_bytes=1024,
                )

    async def test_non_image_body_is_rejected(self) -> None:
        async with self._client(b"not-an-image", "text/plain") as client:
            with self.assertRaisesRegex(ValueError, "supported image"):
                await download_image(
                    "https://multimedia.nt.qq.com.cn/evidence.txt",
                    client=client,
                    resolver=PUBLIC_RESOLVER,
                    max_bytes=1024,
                )

    async def test_oversized_image_is_rejected(self) -> None:
        async with self._client(JPEG * 100, "image/jpeg") as client:
            with self.assertRaisesRegex(ValueError, "size limit"):
                await download_image(
                    "https://multimedia.nt.qq.com.cn/large.jpg",
                    client=client,
                    resolver=PUBLIC_RESOLVER,
                    max_bytes=32,
                )


if __name__ == "__main__":
    unittest.main()
