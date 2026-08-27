from __future__ import annotations

import unittest
from unittest.mock import AsyncMock, patch

from plugins.llm_gateway.errors import (
    GatewayAuthenticationError,
    GatewayContractError,
    GatewayPaymentRequiredError,
)


class _Features:
    def __init__(self, enabled: bool) -> None:
        self.enabled = enabled

    def llm_gateway_allowed(self, domain: str) -> bool:
        if domain != "vision":
            raise AssertionError(domain)
        return self.enabled


class _Gateway:
    def __init__(self) -> None:
        self.describe_image = AsyncMock(return_value="  一朵粉色月季。  ")


class VisionGatewayMigrationTests(unittest.IsolatedAsyncioTestCase):
    async def test_enabled_gateway_receives_domain_built_multimodal_messages(self) -> None:
        from plugins.chat_vision import client

        gateway = _Gateway()
        with (
            patch.object(client, "FEATURES", _Features(True), create=True),
            patch.object(
                client, "get_gateway", AsyncMock(return_value=gateway), create=True
            ),
            patch.object(
                client,
                "_legacy_describe_image",
                AsyncMock(side_effect=AssertionError("legacy path used")),
                create=True,
            ),
        ):
            result = await client.describe_image(
                b"jpeg-bytes",
                "image/jpeg",
                base_url="https://api.deepseek.com",
                api_key="secret",
                model="vision-model",
                timeout=60,
            )

        self.assertEqual("一朵粉色月季。", result)
        messages = gateway.describe_image.await_args.args[0]
        self.assertEqual(1, len(messages))
        self.assertEqual("user", messages[0]["role"])
        content = messages[0]["content"]
        self.assertEqual("text", content[0]["type"])
        self.assertIn("简洁、事实性的中文", content[0]["text"])
        self.assertIn("可见文字（OCR）", content[0]["text"])
        self.assertIn("不要臆测", content[0]["text"])
        self.assertEqual("image_url", content[1]["type"])
        self.assertEqual(
            "data:image/jpeg;base64,anBlZy1ieXRlcw==",
            content[1]["image_url"]["url"],
        )

    async def test_runtime_switch_hot_selects_legacy_gateway_and_legacy(self) -> None:
        from plugins.chat_vision import client

        features = _Features(False)
        gateway = _Gateway()
        legacy = AsyncMock(side_effect=("legacy-one", "legacy-two"))
        getter = AsyncMock(return_value=gateway)
        kwargs = dict(
            base_url="https://api.deepseek.com",
            api_key="secret",
            model="vision-model",
            timeout=60,
        )
        with (
            patch.object(client, "FEATURES", features, create=True),
            patch.object(client, "get_gateway", getter, create=True),
            patch.object(client, "_legacy_describe_image", legacy, create=True),
        ):
            self.assertEqual("legacy-one", await client.describe_image(b"x", "image/png", **kwargs))
            features.enabled = True
            self.assertEqual("一朵粉色月季。", await client.describe_image(b"x", "image/png", **kwargs))
            features.enabled = False
            self.assertEqual("legacy-two", await client.describe_image(b"x", "image/png", **kwargs))

        self.assertEqual(2, legacy.await_count)
        getter.assert_awaited_once()
        gateway.describe_image.assert_awaited_once()

    async def test_gateway_errors_are_publicly_redacted_by_class_only(self) -> None:
        from plugins.chat_vision import client

        marker = "data:image/jpeg;base64,PRIVATE-BYTES"
        for error in (
            GatewayAuthenticationError(marker),
            GatewayContractError(marker),
        ):
            gateway = _Gateway()
            gateway.describe_image.side_effect = error
            with (
                self.subTest(error=type(error).__name__),
                patch.object(client, "FEATURES", _Features(True), create=True),
                patch.object(
                    client, "get_gateway", AsyncMock(return_value=gateway), create=True
                ),
                self.assertRaisesRegex(
                    client.ChatVisionAIError, f"^{type(error).__name__}$"
                ) as raised,
            ):
                await client.describe_image(
                    b"PRIVATE-BYTES",
                    "image/jpeg",
                    base_url="https://api.deepseek.com",
                    api_key="secret",
                    model="vision-model",
                    timeout=60,
                )
            self.assertNotIn("PRIVATE", str(raised.exception))
            self.assertIsNone(raised.exception.__cause__)

    async def test_payment_required_is_redacted_and_marked_non_retryable(self) -> None:
        from plugins.chat_vision import client

        marker = "data:image/jpeg;base64,PRIVATE-BYTES"
        gateway = _Gateway()
        gateway.describe_image.side_effect = GatewayPaymentRequiredError(
            marker,
            status_code=402,
        )
        with (
            patch.object(client, "FEATURES", _Features(True), create=True),
            patch.object(
                client, "get_gateway", AsyncMock(return_value=gateway), create=True
            ),
            self.assertRaises(client.ChatVisionAIError) as raised,
        ):
            await client.describe_image(
                b"PRIVATE-BYTES",
                "image/jpeg",
                base_url="https://api.deepseek.com",
                api_key="secret",
                model="vision-model",
                timeout=60,
            )

        self.assertEqual("GatewayPaymentRequiredError", str(raised.exception))
        self.assertEqual("payment_required", raised.exception.code)
        self.assertFalse(raised.exception.retryable)
        self.assertNotIn("PRIVATE", str(raised.exception))
        self.assertIsNone(raised.exception.__cause__)

    async def test_missing_key_keeps_existing_redacted_value_error_before_gateway(self) -> None:
        from plugins.chat_vision import client

        getter = AsyncMock(side_effect=AssertionError("gateway must not start"))
        with (
            patch.object(client, "FEATURES", _Features(True), create=True),
            patch.object(client, "get_gateway", getter, create=True),
            self.assertRaisesRegex(client.ChatVisionAIError, "^ValueError$"),
        ):
            await client.describe_image(
                b"bytes",
                "image/jpeg",
                base_url="https://api.deepseek.com",
                api_key="",
                model="vision-model",
                timeout=60,
            )
        getter.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()
