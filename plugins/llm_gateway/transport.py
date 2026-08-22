from __future__ import annotations

import asyncio
import math
import random
import time
from collections.abc import Awaitable, Callable, Mapping
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from typing import Any
from urllib.parse import urlsplit

import httpx

from .contracts import GatewayCompletion, GatewayRequest, LLMTask, TokenUsage
from .errors import (
    GatewayAuthenticationError,
    GatewayClientError,
    GatewayConfigurationError,
    GatewayContractError,
    GatewayEmptyContentError,
    GatewayError,
    GatewayRateLimitError,
    GatewayServerError,
    GatewayTimeout,
    GatewayTransportError,
    is_retryable,
)


_RETRYABLE_SERVER_STATUSES = frozenset({500, 502, 503, 504})


class LLMTransport:
    def __init__(
        self,
        *,
        base_url: str,
        api_key: str,
        client: httpx.AsyncClient,
        total_limit: int,
        lane_limits: Mapping[str, int],
        max_attempts: int = 3,
        retry_base_delay: float = 0.5,
        retry_delay_cap: float = 8.0,
        jitter_ratio: float = 0.1,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        random_uniform: Callable[[float, float], float] = random.uniform,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._base_url = self._validate_base_url(base_url)
        if type(api_key) is not str or not api_key.strip():
            raise GatewayConfigurationError()
        if not isinstance(client, httpx.AsyncClient):
            raise GatewayConfigurationError()
        self.client = client
        self._api_key = api_key.strip()
        self._validate_positive_int(total_limit)
        self._validate_positive_int(max_attempts)
        expected_tasks = {task.value for task in LLMTask}
        if set(lane_limits) != expected_tasks:
            raise GatewayConfigurationError()
        for limit in lane_limits.values():
            self._validate_positive_int(limit)
        if not self._is_non_negative_finite(retry_base_delay):
            raise GatewayConfigurationError()
        if not self._is_non_negative_finite(retry_delay_cap):
            raise GatewayConfigurationError()
        if not self._is_non_negative_finite(jitter_ratio) or jitter_ratio > 1:
            raise GatewayConfigurationError()

        self._total = asyncio.Semaphore(total_limit)
        self._lanes = {
            task: asyncio.Semaphore(lane_limits[task]) for task in expected_tasks
        }
        self._max_attempts = max_attempts
        self._retry_base_delay = float(retry_base_delay)
        self._retry_delay_cap = float(retry_delay_cap)
        self._jitter_ratio = float(jitter_ratio)
        self._sleep = sleep
        self._random_uniform = random_uniform
        self._clock = clock
        self._closed = False
        self._close_lock = asyncio.Lock()

    @staticmethod
    def _validate_base_url(value: str) -> str:
        if type(value) is not str or not value.strip():
            raise GatewayConfigurationError()
        candidate = value.strip().rstrip("/")
        parsed = urlsplit(candidate)
        if (
            parsed.scheme not in {"http", "https"}
            or not parsed.netloc
            or parsed.username is not None
            or parsed.password is not None
            or parsed.query
            or parsed.fragment
        ):
            raise GatewayConfigurationError()
        return candidate

    @staticmethod
    def _validate_positive_int(value: object) -> None:
        if type(value) is not int or value <= 0:
            raise GatewayConfigurationError()

    @staticmethod
    def _is_non_negative_finite(value: object) -> bool:
        return type(value) in (int, float) and math.isfinite(value) and value >= 0

    async def complete(self, request: GatewayRequest) -> GatewayCompletion:
        if not isinstance(request, GatewayRequest):
            raise GatewayContractError()
        if self._closed:
            raise GatewayConfigurationError(task=request.task)
        started = self._clock()
        async with self._lanes[request.task.value]:
            async with self._total:
                return await self._complete_with_retries(request, started)

    async def _complete_with_retries(
        self, request: GatewayRequest, started: float
    ) -> GatewayCompletion:
        for attempt in range(1, self._max_attempts + 1):
            response: httpx.Response | None = None
            try:
                response = await self.client.post(
                    f"{self._base_url}/chat/completions",
                    headers={
                        "Authorization": f"Bearer {self._api_key}",
                        "Content-Type": "application/json",
                    },
                    json=request.to_payload(),
                    timeout=request.timeout,
                )
                error = self._classify_response_error(response, request)
                if error is not None:
                    raise error
                return self._parse_completion(
                    response,
                    request,
                    latency_ms=max(0, round((self._clock() - started) * 1000)),
                    retries=attempt - 1,
                )
            except asyncio.CancelledError:
                raise
            except httpx.TimeoutException:
                error = GatewayTimeout(task=request.task)
            except httpx.TransportError:
                error = GatewayTransportError(task=request.task)
            except GatewayError as caught:
                error = caught

            if attempt >= self._max_attempts or not self._should_retry(error):
                raise error
            await self._sleep(self._retry_delay(attempt, response))
        raise AssertionError("unreachable")

    @staticmethod
    def _classify_response_error(
        response: httpx.Response, request: GatewayRequest
    ) -> GatewayError | None:
        status = response.status_code
        kwargs = {"task": request.task, "status_code": status}
        if 200 <= status < 300:
            return None
        if status in {401, 403}:
            return GatewayAuthenticationError(**kwargs)
        if status == 408:
            return GatewayTimeout(**kwargs)
        if status == 429:
            return GatewayRateLimitError(**kwargs)
        if 500 <= status < 600:
            return GatewayServerError(**kwargs)
        if 400 <= status < 500:
            return GatewayClientError(**kwargs)
        return GatewayClientError(**kwargs)

    @staticmethod
    def _parse_completion(
        response: httpx.Response,
        request: GatewayRequest,
        *,
        latency_ms: int,
        retries: int,
    ) -> GatewayCompletion:
        try:
            payload = response.json()
        except (ValueError, TypeError) as exc:
            raise GatewayContractError(task=request.task) from exc
        if not isinstance(payload, Mapping):
            raise GatewayContractError(task=request.task)
        model = payload.get("model")
        choices = payload.get("choices")
        if type(model) is not str or not model.strip():
            raise GatewayContractError(task=request.task)
        if not isinstance(choices, list) or not choices or not isinstance(choices[0], Mapping):
            raise GatewayContractError(task=request.task)
        message = choices[0].get("message")
        if not isinstance(message, Mapping) or "content" not in message:
            raise GatewayContractError(task=request.task)
        content = message["content"]
        if type(content) is not str:
            raise GatewayContractError(task=request.task)
        content = content.strip()
        if not content:
            raise GatewayEmptyContentError(task=request.task)
        usage = LLMTransport._parse_usage(payload.get("usage"), request)
        return GatewayCompletion(
            content=content,
            model=model.strip(),
            usage=usage,
            latency_ms=latency_ms,
            retries=retries,
        )

    @staticmethod
    def _parse_usage(value: Any, request: GatewayRequest) -> TokenUsage:
        if value is None:
            return TokenUsage()
        if not isinstance(value, Mapping):
            raise GatewayContractError(task=request.task)
        values = (
            value.get("prompt_tokens", value.get("input_tokens")),
            value.get("completion_tokens", value.get("output_tokens")),
            value.get("total_tokens"),
        )
        try:
            return TokenUsage(*values)
        except ValueError as exc:
            raise GatewayContractError(task=request.task) from exc

    @staticmethod
    def _should_retry(error: GatewayError) -> bool:
        if isinstance(error, GatewayServerError):
            return error.status_code in _RETRYABLE_SERVER_STATUSES
        return is_retryable(error)

    def _retry_delay(self, attempt: int, response: httpx.Response | None) -> float:
        if response is not None:
            retry_after = self._valid_retry_after(response.headers.get("Retry-After"))
            if retry_after is not None:
                return retry_after
        base = min(
            self._retry_delay_cap,
            self._retry_base_delay * (2 ** (attempt - 1)),
        )
        jitter = self._random_uniform(-self._jitter_ratio, self._jitter_ratio)
        return min(self._retry_delay_cap, max(0.0, base * (1 + jitter)))

    def _valid_retry_after(self, value: str | None) -> float | None:
        if value is None:
            return None
        try:
            if not value.isascii() or not value.isdigit():
                raise ValueError
            delay = float(value)
        except ValueError:
            try:
                parsed = parsedate_to_datetime(value)
                if parsed.tzinfo is None:
                    parsed = parsed.replace(tzinfo=timezone.utc)
                delay = (parsed - datetime.now(timezone.utc)).total_seconds()
            except (TypeError, ValueError, OverflowError):
                return None
        if not math.isfinite(delay) or delay < 0 or delay > self._retry_delay_cap:
            return None
        return delay

    async def aclose(self) -> None:
        async with self._close_lock:
            if self._closed:
                return
            self._closed = True
            await self.client.aclose()
