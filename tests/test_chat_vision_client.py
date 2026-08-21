import json
import unittest
from unittest.mock import patch

import httpx

from plugins.chat_vision.client import ChatVisionAIError, describe_image, image_data_url


class _Response:
    def __init__(self, content="一名粉发小精灵在飞。", *, error=None, json_error=None):
        self.content = content
        self.error = error
        self.json_error = json_error

    def raise_for_status(self):
        if self.error:
            raise self.error

    def json(self):
        if self.json_error:
            raise self.json_error
        return {"choices": [{"message": {"content": self.content}}]}


class _Client:
    posted = None
    response = _Response()
    error = None

    def __init__(self, *, timeout):
        self.timeout = timeout

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False

    async def post(self, url, *, headers, json):
        type(self).posted = (url, headers, json, self.timeout)
        if type(self).error:
            raise type(self).error
        return type(self).response


class ChatVisionClientTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        _Client.posted = None
        _Client.response = _Response()
        _Client.error = None
        self.content = b"jpeg-bytes"
        self.mime_type = "image/jpeg"
        self.base_url = "https://api.deepseek.com"
        self.api_key = "secret"
        self.model = "deepseek-v4-flash-vision-exp"
        self.timeout = 60

    def _call(self):
        return describe_image(
            self.content,
            self.mime_type,
            base_url=self.base_url,
            api_key=self.api_key,
            model=self.model,
            timeout=self.timeout,
        )

    def _assert_redacted(self, error):
        text = str(error)
        self.assertIsNone(error.__cause__)
        if self.api_key:
            self.assertNotIn(self.api_key, text)
        self.assertNotIn(image_data_url(self.content, self.mime_type), text)
        self.assertNotIn("https://api.deepseek.com/v1/chat/completions", text)

    def test_image_data_url_encodes_bytes_with_declared_mime_type(self):
        self.assertEqual("data:image/jpeg;base64,anBlZy1ieXRlcw==", image_data_url(self.content, self.mime_type))

    async def test_describe_image_posts_openai_vision_payload_and_returns_description(self):
        with patch("plugins.chat_vision.client.httpx.AsyncClient", _Client):
            description = await self._call()

        self.assertEqual("一名粉发小精灵在飞。", description)
        url, headers, payload, timeout = _Client.posted
        self.assertEqual("https://api.deepseek.com/v1/chat/completions", url)
        self.assertEqual("Bearer secret", headers["Authorization"])
        self.assertEqual(60, timeout)
        self.assertEqual("disabled", payload["thinking"]["type"])
        self.assertEqual("deepseek-v4-flash-vision-exp", payload["model"])
        self.assertEqual("image_url", payload["messages"][0]["content"][1]["type"])
        self.assertTrue(
            payload["messages"][0]["content"][1]["image_url"]["url"].startswith(
                "data:image/jpeg;base64,"
            )
        )
        prompt = payload["messages"][0]["content"][0]["text"]
        self.assertIn("简洁", prompt)
        self.assertIn("事实", prompt)
        self.assertIn("中文", prompt)
        self.assertIn("可见文字", prompt)
        self.assertIn("OCR", prompt)
        self.assertIn("不要臆测", prompt)

    async def test_empty_description_raises_redacted_value_error(self):
        _Client.response = _Response("   ")

        with patch("plugins.chat_vision.client.httpx.AsyncClient", _Client), self.assertRaisesRegex(
            ChatVisionAIError, "^ValueError$"
        ) as raised:
            await self._call()

        self._assert_redacted(raised.exception)

    async def test_non_200_response_raises_redacted_http_status_error(self):
        request = httpx.Request("POST", "https://api.deepseek.com/v1/chat/completions")
        response = httpx.Response(503, request=request)
        _Client.response = _Response(
            error=httpx.HTTPStatusError(
                "secret data:image/jpeg;base64,anBlZy1ieXRlcw== "
                "https://api.deepseek.com/v1/chat/completions",
                request=request,
                response=response,
            )
        )

        with patch("plugins.chat_vision.client.httpx.AsyncClient", _Client), self.assertRaisesRegex(
            ChatVisionAIError, "^HTTPStatusError$"
        ) as raised:
            await self._call()

        self._assert_redacted(raised.exception)

    async def test_malformed_json_raises_redacted_json_decode_error(self):
        _Client.response = _Response(
            json_error=json.JSONDecodeError(
                "secret data:image/jpeg;base64,anBlZy1ieXRlcw== "
                "https://api.deepseek.com/v1/chat/completions",
                "{}",
                0,
            )
        )

        with patch("plugins.chat_vision.client.httpx.AsyncClient", _Client), self.assertRaisesRegex(
            ChatVisionAIError, "^JSONDecodeError$"
        ) as raised:
            await self._call()

        self._assert_redacted(raised.exception)

    async def test_missing_api_key_raises_redacted_value_error_without_request(self):
        self.api_key = ""

        with self.assertRaisesRegex(ChatVisionAIError, "^ValueError$") as raised:
            await self._call()

        self.assertIsNone(_Client.posted)
        self._assert_redacted(raised.exception)

    async def test_transport_error_raises_only_exception_type(self):
        _Client.error = httpx.ConnectError(
            "secret data:image/jpeg;base64,anBlZy1ieXRlcw== "
            "https://api.deepseek.com/v1/chat/completions"
        )

        with patch("plugins.chat_vision.client.httpx.AsyncClient", _Client), self.assertRaisesRegex(
            ChatVisionAIError, "^ConnectError$"
        ) as raised:
            await self._call()

        self._assert_redacted(raised.exception)


if __name__ == "__main__":
    unittest.main()
