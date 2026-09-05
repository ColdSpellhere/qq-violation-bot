from __future__ import annotations

import math
from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import Enum
from types import MappingProxyType
from typing import Any


class LLMTask(str, Enum):
    BUSINESS_INTENT = "business_intent"
    CHAT_REPLY = "chat_reply"
    MEMBER_EXTRACTION = "member_extraction"
    MEMBER_SUMMARY = "member_summary"
    PRIVATE_SUMMARY = "private_summary"
    PRIVATE_FACT_EXTRACTION = "private_fact_extraction"
    RELATIONSHIP_UPDATE = "relationship_update"
    IMAGE_DESCRIPTION = "image_description"


class LLMProvider(str, Enum):
    PRIMARY = "primary"
    ECONOMY = "economy"


def _freeze_json(value: Any) -> Any:
    if isinstance(value, Mapping):
        frozen: dict[str, object] = {}
        for key, item in value.items():
            if type(key) is not str:
                raise TypeError("JSON object keys must be strings")
            frozen[key] = _freeze_json(item)
        return MappingProxyType(frozen)
    if type(value) in (list, tuple):
        return tuple(_freeze_json(item) for item in value)
    if value is None or type(value) in (bool, str, int):
        return value
    if type(value) is float:
        if not math.isfinite(value):
            raise ValueError("JSON numbers must be finite")
        return value
    raise TypeError(f"unsupported JSON value: {type(value).__name__}")


def _thaw_json(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {key: _thaw_json(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return [_thaw_json(item) for item in value]
    return value


def _is_finite_number(value: Any) -> bool:
    return type(value) in (int, float) and math.isfinite(value)


def _validate_message(message: Mapping[str, object]) -> None:
    role = message.get("role")
    if type(role) is not str or role not in {"system", "user", "assistant"}:
        raise ValueError("message role must be system, user, or assistant")
    if "content" not in message:
        raise ValueError("message content is required")
    content = message["content"]
    if type(content) not in (str, list, tuple):
        raise ValueError("message content must be text or a multimodal array")
    if type(content) in (list, tuple):
        if not content:
            raise ValueError("multimodal content must not be empty")
        for part in content:
            if not isinstance(part, Mapping):
                raise ValueError("each multimodal part must be a mapping")
            part_type = part.get("type")
            if part_type == "text":
                text = part.get("text")
                if type(text) is not str or not text.strip():
                    raise ValueError("text parts require non-empty text")
            elif part_type == "image_url":
                image_url = part.get("image_url")
                if not isinstance(image_url, Mapping):
                    raise ValueError("image_url parts require an image_url mapping")
                url = image_url.get("url")
                if type(url) is not str or not url.strip():
                    raise ValueError("image_url parts require a non-empty string URL")
            else:
                raise ValueError("unsupported multimodal part type")
    _freeze_json(message)


def _validate_response_format(response_format: Mapping[str, object]) -> None:
    format_type = response_format.get("type")
    if format_type == "json_object":
        _freeze_json(response_format)
        return
    if format_type != "json_schema":
        raise ValueError("unsupported response_format type")
    json_schema = response_format.get("json_schema")
    if not isinstance(json_schema, Mapping):
        raise ValueError("json_schema response format requires json_schema")
    name = json_schema.get("name")
    if type(name) is not str or not name.strip():
        raise ValueError("json_schema name must not be empty")
    schema = json_schema.get("schema")
    if not isinstance(schema, Mapping) or schema.get("type") != "object":
        raise ValueError("json_schema schema must describe an object")
    if "strict" in json_schema and type(json_schema["strict"]) is not bool:
        raise ValueError("json_schema strict must be boolean")
    _freeze_json(response_format)


@dataclass(frozen=True)
class JSONContract:
    name: str
    schema: Mapping[str, object]
    strict: bool = True

    def __post_init__(self) -> None:
        if type(self.name) is not str or not self.name.strip():
            raise ValueError("JSON contract name must not be empty")
        if type(self.strict) is not bool:
            raise ValueError("JSON contract strict must be boolean")
        if not isinstance(self.schema, Mapping) or self.schema.get("type") != "object":
            raise ValueError("JSON contract schema must describe an object")
        object.__setattr__(self, "schema", _freeze_json(self.schema))

    @property
    def response_format(self) -> Mapping[str, object]:
        return _freeze_json(
            {
                "type": "json_schema",
                "json_schema": {
                    "name": self.name,
                    "strict": self.strict,
                    "schema": self.schema,
                },
            }
        )


@dataclass(frozen=True)
class GatewayRequest:
    task: LLMTask
    messages: tuple[Mapping[str, object], ...]
    model: str
    timeout: float
    temperature: float | None = None
    response_format: Mapping[str, object] | None = None
    thinking_disabled: bool = False
    provider: LLMProvider = LLMProvider.PRIMARY
    max_output_tokens: int | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.task, LLMTask):
            raise ValueError("task must be an LLMTask")
        if not isinstance(self.provider, LLMProvider):
            raise ValueError("provider must be an LLMProvider")
        if not isinstance(self.messages, tuple):
            raise ValueError("messages must be a tuple")
        if not self.messages:
            raise ValueError("messages must not be empty")
        if not all(isinstance(message, Mapping) for message in self.messages):
            raise ValueError("each message must be a mapping")
        for message in self.messages:
            _validate_message(message)
        if type(self.model) is not str or not self.model.strip():
            raise ValueError("model must not be empty")
        if not _is_finite_number(self.timeout) or self.timeout <= 0:
            raise ValueError("timeout must be a positive finite number")
        if self.temperature is not None and (
            not _is_finite_number(self.temperature)
            or not 0 <= self.temperature <= 2
        ):
            raise ValueError("temperature must be a finite number from 0 to 2")
        if type(self.thinking_disabled) is not bool:
            raise ValueError("thinking_disabled must be boolean")
        if self.max_output_tokens is not None and (
            type(self.max_output_tokens) is not int or not 1 <= self.max_output_tokens <= 8192
        ):
            raise ValueError("max_output_tokens must be an integer from 1 to 8192")
        object.__setattr__(self, "messages", _freeze_json(self.messages))
        if self.response_format is not None:
            if not isinstance(self.response_format, Mapping):
                raise ValueError("response_format must be a mapping")
            _validate_response_format(self.response_format)
            object.__setattr__(
                self, "response_format", _freeze_json(self.response_format)
            )

    def to_payload(self) -> dict[str, object]:
        payload: dict[str, object] = {
            "model": self.model,
            "messages": _thaw_json(self.messages),
        }
        if self.temperature is not None:
            payload["temperature"] = self.temperature
        if self.response_format is not None:
            payload["response_format"] = _thaw_json(self.response_format)
        if self.thinking_disabled:
            payload["thinking"] = {"type": "disabled"}
        if self.max_output_tokens is not None:
            payload["max_tokens"] = self.max_output_tokens
        elif self.task is not LLMTask.BUSINESS_INTENT:
            # Preserve the business parser contract; bound chat/derived-memory output.
            payload["max_tokens"] = 1024 if self.task in {
                LLMTask.CHAT_REPLY, LLMTask.IMAGE_DESCRIPTION, LLMTask.RELATIONSHIP_UPDATE
            } else 4096
        return payload


@dataclass(frozen=True)
class TokenUsage:
    input_tokens: int | None = None
    output_tokens: int | None = None
    total_tokens: int | None = None

    def __post_init__(self) -> None:
        for value in (self.input_tokens, self.output_tokens, self.total_tokens):
            if value is not None and (type(value) is not int or value < 0):
                raise ValueError("token counts must be non-negative integers")


@dataclass(frozen=True)
class GatewayCompletion:
    content: str
    model: str
    usage: TokenUsage = field(default_factory=TokenUsage)
    latency_ms: int = 0
    retries: int = 0

    def __post_init__(self) -> None:
        if type(self.content) is not str:
            raise ValueError("content must be text")
        if type(self.model) is not str or not self.model.strip():
            raise ValueError("model must not be empty")
        if not isinstance(self.usage, TokenUsage):
            raise ValueError("usage must be TokenUsage")
        if type(self.latency_ms) is not int or self.latency_ms < 0:
            raise ValueError("latency_ms must be a non-negative integer")
        if type(self.retries) is not int or self.retries < 0:
            raise ValueError("retries must be a non-negative integer")
