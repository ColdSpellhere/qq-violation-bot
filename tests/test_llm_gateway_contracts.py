from __future__ import annotations

import asyncio
import json
import math
import unittest
from dataclasses import FrozenInstanceError

from plugins.llm_gateway.contracts import (
    GatewayCompletion,
    GatewayRequest,
    JSONContract,
    LLMTask,
    TokenUsage,
)
from plugins.llm_gateway.errors import (
    GatewayAuthenticationError,
    GatewayClientError,
    GatewayConfigurationError,
    GatewayContractError,
    GatewayEmptyContentError,
    GatewayRateLimitError,
    GatewayServerError,
    GatewayTimeout,
    GatewayTransportError,
    is_retryable,
)


class GatewayContractTests(unittest.TestCase):
    def test_task_values_are_stable_and_complete(self) -> None:
        self.assertEqual(
            {
                "business_intent",
                "chat_reply",
                "member_extraction",
                "member_summary",
                "private_summary",
                "relationship_update",
                "image_description",
            },
            {task.value for task in LLMTask},
        )

    def test_request_and_nested_json_values_are_immutable(self) -> None:
        messages = [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": "hello"},
                    {"type": "image_url", "image_url": {"url": "data:image/png"}},
                ],
            }
        ]
        response_format = {"type": "json_object"}
        request = GatewayRequest(
            task=LLMTask.CHAT_REPLY,
            messages=tuple(messages),
            model="model-a",
            timeout=30.0,
            response_format=response_format,
        )
        messages[0]["role"] = "system"
        response_format["type"] = "text"

        self.assertEqual("user", request.messages[0]["role"])
        self.assertEqual("hello", request.messages[0]["content"][0]["text"])
        self.assertEqual("json_object", request.response_format["type"])
        with self.assertRaises(TypeError):
            request.messages[0]["role"] = "assistant"
        with self.assertRaises(TypeError):
            request.messages[0]["content"][0]["text"] = "changed"
        with self.assertRaises(FrozenInstanceError):
            request.model = "model-b"

    def test_usage_completion_and_json_contract_are_immutable(self) -> None:
        contract = JSONContract(
            name="memory_result",
            schema={"type": "object", "properties": {}},
        )
        usage = TokenUsage(input_tokens=10, output_tokens=4, total_tokens=14)
        completion = GatewayCompletion(
            content="{}",
            model="model-a",
            usage=usage,
            latency_ms=25,
            retries=1,
        )

        self.assertEqual("object", contract.schema["type"])
        self.assertTrue(contract.strict)
        self.assertEqual(14, completion.usage.total_tokens)
        with self.assertRaises(TypeError):
            contract.schema["type"] = "array"
        with self.assertRaises(FrozenInstanceError):
            completion.retries = 2

    def test_json_contract_rejects_invalid_definitions(self) -> None:
        with self.assertRaises(ValueError):
            JSONContract(name="", schema={"type": "object"})
        with self.assertRaises(ValueError):
            JSONContract(name="result", schema={"type": "array"})
        with self.assertRaises(ValueError):
            JSONContract(name="result", schema={"type": "object"}, strict="yes")

    def test_json_values_reject_non_string_keys_and_non_finite_or_custom_values(self) -> None:
        invalid_contents = (
            [{1: "numeric key", "1": "collision"}],
            [{"value": math.nan}],
            [{"value": math.inf}],
            [{"value": -math.inf}],
            [{"value": object()}],
        )

        for content in invalid_contents:
            with self.subTest(content=content):
                with self.assertRaises((TypeError, ValueError)):
                    GatewayRequest(
                        task=LLMTask.CHAT_REPLY,
                        messages=({"role": "user", "content": content},),
                        model="model-a",
                        timeout=30,
                    )

    def test_messages_require_supported_role_and_text_or_multimodal_content(self) -> None:
        invalid_messages = (
            {"content": "hello"},
            {"role": "tool", "content": "hello"},
            {"role": "user"},
            {"role": "user", "content": 42},
            {"role": "user", "content": {"text": "hello"}},
        )

        for message in invalid_messages:
            with self.subTest(message=message):
                with self.assertRaises(ValueError):
                    GatewayRequest(
                        task=LLMTask.CHAT_REPLY,
                        messages=(message,),
                        model="model-a",
                        timeout=30,
                    )

    def test_response_format_requires_supported_type_and_structure(self) -> None:
        invalid_formats = (
            {},
            {"type": "text"},
            {"type": "json_schema"},
            {"type": "json_schema", "json_schema": "schema"},
            {
                "type": "json_schema",
                "json_schema": {"name": "", "schema": {"type": "object"}},
            },
            {
                "type": "json_schema",
                "json_schema": {"name": "result", "schema": {"type": "array"}},
            },
            {
                "type": "json_schema",
                "json_schema": {
                    "name": "result",
                    "schema": {"type": "object"},
                    "strict": "yes",
                },
            },
        )

        for response_format in invalid_formats:
            with self.subTest(response_format=response_format):
                with self.assertRaises(ValueError):
                    GatewayRequest(
                        task=LLMTask.CHAT_REPLY,
                        messages=({"role": "user", "content": "hello"},),
                        model="model-a",
                        timeout=30,
                        response_format=response_format,
                    )

    def test_to_payload_returns_isolated_json_serializable_data(self) -> None:
        request = GatewayRequest(
            task=LLMTask.CHAT_REPLY,
            messages=(
                {
                    "role": "user",
                    "content": [{"type": "text", "text": "hello"}],
                },
            ),
            model="model-a",
            timeout=30,
            temperature=0.5,
            response_format={"type": "json_object"},
            thinking_disabled=True,
        )

        payload = request.to_payload()

        json.dumps(payload, allow_nan=False)
        self.assertIs(type(payload), dict)
        self.assertIs(type(payload["messages"]), list)
        self.assertIs(type(payload["messages"][0]), dict)
        self.assertEqual({"type": "disabled"}, payload["thinking"])
        payload["messages"][0]["content"][0]["text"] = "changed"
        payload["response_format"]["type"] = "changed"
        self.assertEqual("hello", request.messages[0]["content"][0]["text"])
        self.assertEqual("json_object", request.response_format["type"])

    def test_request_numeric_fields_are_strict_and_bounded(self) -> None:
        base = {
            "task": LLMTask.CHAT_REPLY,
            "messages": ({"role": "user", "content": "hello"},),
            "model": "model-a",
        }
        for timeout in (True, False, 0, -1, math.nan, math.inf, -math.inf, "30"):
            with self.subTest(timeout=timeout):
                with self.assertRaises(ValueError):
                    GatewayRequest(**base, timeout=timeout)
        for temperature in (
            True,
            False,
            -0.1,
            2.1,
            math.nan,
            math.inf,
            -math.inf,
            "1",
        ):
            with self.subTest(temperature=temperature):
                with self.assertRaises(ValueError):
                    GatewayRequest(**base, timeout=30, temperature=temperature)

        for temperature in (None, 0, 0.5, 2):
            with self.subTest(valid_temperature=temperature):
                request = GatewayRequest(
                    **base, timeout=0.1, temperature=temperature
                )
                self.assertEqual(temperature, request.temperature)

    def test_usage_and_completion_numeric_fields_reject_bools_and_invalid_values(self) -> None:
        for value in (True, False, -1, 1.5):
            with self.subTest(token_value=value):
                with self.assertRaises(ValueError):
                    TokenUsage(input_tokens=value)
        for field_name in ("latency_ms", "retries"):
            for value in (True, False, -1, 1.5):
                with self.subTest(field_name=field_name, value=value):
                    values = {"latency_ms": 0, "retries": 0, field_name: value}
                    with self.assertRaises(ValueError):
                        GatewayCompletion(
                            content="ok",
                            model="model-a",
                            usage=TokenUsage(),
                            **values,
                        )

        self.assertEqual(0, TokenUsage(input_tokens=0).input_tokens)
        completion = GatewayCompletion(content="ok", model="model-a")
        self.assertEqual((0, 0), (completion.latency_ms, completion.retries))


class GatewayErrorTests(unittest.TestCase):
    def test_retry_classification_is_explicit(self) -> None:
        for error in (
            GatewayTimeout("provider detail"),
            GatewayTransportError("provider detail"),
            GatewayRateLimitError("provider detail", status_code=429),
            GatewayServerError("provider detail", status_code=503),
        ):
            self.assertTrue(is_retryable(error), type(error).__name__)

        for error in (
            GatewayConfigurationError("provider detail"),
            GatewayAuthenticationError("provider detail", status_code=401),
            GatewayClientError("provider detail", status_code=400),
            GatewayContractError("response body"),
            GatewayEmptyContentError("response body"),
            asyncio.CancelledError(),
        ):
            self.assertFalse(is_retryable(error), type(error).__name__)

    def test_error_strings_include_only_class_task_and_status(self) -> None:
        secret = "secret prompt and response"
        error = GatewayServerError(
            secret,
            task=LLMTask.PRIVATE_SUMMARY,
            status_code=502,
        )

        rendered = str(error)

        self.assertEqual(
            "GatewayServerError task=private_summary status=502", rendered
        )
        self.assertNotIn(secret, rendered)

    def test_error_rejects_unrecognized_task_text_that_could_leak(self) -> None:
        with self.assertRaises(ValueError):
            GatewayServerError("detail", task="secret prompt")


if __name__ == "__main__":
    unittest.main()
