from __future__ import annotations

import logging
import math
import time
from collections.abc import Mapping, Sequence
from typing import Callable, Protocol

from .contracts import GatewayCompletion, GatewayRequest, LLMProvider, LLMTask
from .errors import GatewayConfigurationError, GatewayError
from .providers import ECONOMY_MODEL


_JSON_OBJECT: Mapping[str, object] = {"type": "json_object"}


class _Transport(Protocol):
    async def complete(self, request: GatewayRequest) -> GatewayCompletion: ...


class _UsageRecorder(Protocol):
    def record_success(
        self, request: GatewayRequest, completion: GatewayCompletion
    ) -> None: ...

    def record_failure(
        self,
        request: GatewayRequest,
        *,
        latency_ms: int,
        retries: int,
        error: GatewayError,
    ) -> None: ...


class _GatewayConfig(Protocol):
    ai_model: str
    ai_timeout: float
    chat_vision_model: str
    chat_vision_timeout: float
    glm_model: str
    glm_timeout: float


class Gateway:
    """Route already-built messages without owning any domain prompt logic."""

    def __init__(
        self,
        *,
        transport: _Transport,
        usage_store: _UsageRecorder,
        config: _GatewayConfig,
        economy_mode_enabled: Callable[[], bool] | None = None,
        logger: logging.Logger | None = None,
        clock=time.monotonic,
    ) -> None:
        self._transport = transport
        self._usage_store = usage_store
        self._config = config
        self._economy_mode_enabled = economy_mode_enabled or (lambda: False)
        self._logger = logger or logging.getLogger(__name__)
        self._clock = clock
        self._validate_config()

    def _validate_config(self) -> None:
        for value in (self._config.ai_model, self._config.chat_vision_model):
            if type(value) is not str or not value.strip():
                raise GatewayConfigurationError()
        for value in (self._config.ai_timeout, self._config.chat_vision_timeout):
            if (
                type(value) not in (int, float)
                or not math.isfinite(value)
                or value <= 0
            ):
                raise GatewayConfigurationError()

    def _economy_settings(
        self,
        temperature: float | None,
        *,
        economy_mode: bool | None = None,
    ) -> tuple[LLMProvider, str, float, float | None, bool]:
        if economy_mode is not None and type(economy_mode) is not bool:
            raise GatewayConfigurationError()
        if economy_mode is None:
            try:
                enabled = bool(self._economy_mode_enabled())
            except Exception as exc:
                raise GatewayConfigurationError() from exc
        else:
            enabled = economy_mode
        if not enabled:
            return (
                LLMProvider.PRIMARY,
                self._config.ai_model,
                self._config.ai_timeout,
                temperature,
                False,
            )
        model = getattr(self._config, "glm_model", "")
        timeout = getattr(self._config, "glm_timeout", 0)
        if type(model) is not str or model.strip() != ECONOMY_MODEL:
            raise GatewayConfigurationError()
        if (
            type(timeout) not in (int, float)
            or not math.isfinite(timeout)
            or timeout <= 0
        ):
            raise GatewayConfigurationError()
        if temperature is not None and temperature < 0.1:
            temperature = 0.1
        return LLMProvider.ECONOMY, model.strip(), timeout, temperature, True

    @staticmethod
    def _validate_model_selection(economy_mode: bool | None) -> None:
        if economy_mode is not None and type(economy_mode) is not bool:
            raise GatewayConfigurationError()

    async def parse_business_intent(
        self,
        messages: Sequence[dict[str, object]],
        *,
        economy_mode: bool | None = None,
    ) -> str:
        self._validate_model_selection(economy_mode)
        return await self._run(
            messages,
            task=LLMTask.BUSINESS_INTENT,
            provider=LLMProvider.PRIMARY,
            model=self._config.ai_model,
            timeout=self._config.ai_timeout,
            temperature=0,
            response_format=_JSON_OBJECT,
        )

    async def generate_chat_reply(
        self,
        messages: Sequence[dict[str, object]],
        *,
        images: bool,
        economy_mode: bool | None = None,
    ) -> str:
        if images:
            self._validate_model_selection(economy_mode)
            return await self._run(
                messages,
                task=LLMTask.CHAT_REPLY,
                provider=LLMProvider.PRIMARY,
                model=self._config.chat_vision_model,
                timeout=self._config.chat_vision_timeout,
                thinking_disabled=True,
            )
        provider, model, timeout, temperature, thinking_disabled = (
            self._economy_settings(0.8, economy_mode=economy_mode)
        )
        return await self._run(
            messages,
            task=LLMTask.CHAT_REPLY,
            provider=provider,
            model=model,
            timeout=timeout,
            temperature=temperature,
            thinking_disabled=thinking_disabled,
        )

    async def extract_member_memories(
        self,
        messages: Sequence[dict[str, object]],
        *,
        economy_mode: bool | None = None,
    ) -> str:
        self._validate_model_selection(economy_mode)
        return await self._run(
            messages,
            task=LLMTask.MEMBER_EXTRACTION,
            provider=LLMProvider.PRIMARY,
            model=self._config.ai_model,
            timeout=self._config.ai_timeout,
            temperature=0.1,
        )

    async def summarize_member_memory(
        self,
        messages: Sequence[dict[str, object]],
        *,
        economy_mode: bool | None = None,
    ) -> str:
        self._validate_model_selection(economy_mode)
        return await self._run(
            messages,
            task=LLMTask.MEMBER_SUMMARY,
            provider=LLMProvider.PRIMARY,
            model=self._config.ai_model,
            timeout=self._config.ai_timeout,
            temperature=0.1,
        )

    async def extract_private_facts(
        self,
        messages: Sequence[dict[str, object]],
        *,
        economy_mode: bool | None = None,
    ) -> str:
        self._validate_model_selection(economy_mode)
        return await self._run(
            messages,
            task=LLMTask.PRIVATE_FACT_EXTRACTION,
            provider=LLMProvider.PRIMARY,
            model=self._config.ai_model,
            timeout=self._config.ai_timeout,
            temperature=0.1,
            response_format=_JSON_OBJECT,
        )

    async def summarize_private_conversation(
        self,
        messages: Sequence[dict[str, object]],
        *,
        economy_mode: bool | None = None,
    ) -> str:
        self._validate_model_selection(economy_mode)
        return await self._run(
            messages,
            task=LLMTask.PRIVATE_SUMMARY,
            provider=LLMProvider.PRIMARY,
            model=self._config.ai_model,
            timeout=self._config.ai_timeout,
            temperature=0.1,
            response_format=_JSON_OBJECT,
        )

    async def update_relationship_state(
        self,
        messages: Sequence[dict[str, object]],
        *,
        economy_mode: bool | None = None,
    ) -> str:
        self._validate_model_selection(economy_mode)
        return await self._run(
            messages,
            task=LLMTask.RELATIONSHIP_UPDATE,
            provider=LLMProvider.PRIMARY,
            model=self._config.ai_model,
            timeout=self._config.ai_timeout,
            temperature=0.1,
            response_format=_JSON_OBJECT,
        )

    async def describe_image(
        self,
        messages: Sequence[dict[str, object]],
        *,
        economy_mode: bool | None = None,
    ) -> str:
        self._validate_model_selection(economy_mode)
        return await self._run(
            messages,
            task=LLMTask.IMAGE_DESCRIPTION,
            provider=LLMProvider.PRIMARY,
            model=self._config.chat_vision_model,
            timeout=self._config.chat_vision_timeout,
            thinking_disabled=True,
        )

    async def _run(
        self,
        messages: Sequence[dict[str, object]],
        *,
        task: LLMTask,
        provider: LLMProvider = LLMProvider.PRIMARY,
        model: str,
        timeout: float,
        temperature: float | None = None,
        response_format: Mapping[str, object] | None = None,
        thinking_disabled: bool = False,
    ) -> str:
        if provider is LLMProvider.ECONOMY and any(
            type(message.get("content")) is not str for message in messages
        ):
            raise GatewayConfigurationError(task=task)
        request = GatewayRequest(
            task=task,
            provider=provider,
            messages=tuple(messages),
            model=model,
            timeout=timeout,
            temperature=temperature,
            response_format=response_format,
            thinking_disabled=thinking_disabled,
        )
        started = self._clock()
        try:
            completion = await self._transport.complete(request)
        except GatewayError as error:
            latency_ms = max(0, round((self._clock() - started) * 1000))
            self._record_failure(request, latency_ms=latency_ms, error=error)
            raise
        self._record_success(request, completion)
        return completion.content

    def _record_success(
        self, request: GatewayRequest, completion: GatewayCompletion
    ) -> None:
        try:
            self._usage_store.record_success(request, completion)
        except Exception as exc:
            self._logger.warning(
                "llm usage write failed error_class=%s", type(exc).__name__
            )

    def _record_failure(
        self,
        request: GatewayRequest,
        *,
        latency_ms: int,
        error: GatewayError,
    ) -> None:
        try:
            self._usage_store.record_failure(
                request, latency_ms=latency_ms, retries=error.retries, error=error
            )
        except Exception as exc:
            self._logger.warning(
                "llm usage write failed error_class=%s", type(exc).__name__
            )


__all__ = ["Gateway"]
