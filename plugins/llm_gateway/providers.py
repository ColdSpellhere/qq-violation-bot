from __future__ import annotations

import asyncio
import math
from collections.abc import Mapping
from dataclasses import replace
from typing import Protocol

from .contracts import GatewayCompletion, GatewayRequest, LLMProvider, LLMTask
from .errors import GatewayConfigurationError, GatewayRateLimitError, GatewayTimeout


ECONOMY_BASE_URL = "https://open.bigmodel.cn/api/paas/v4"
ECONOMY_MODEL = "glm-4.7-flash"


def validate_economy_provider(*, base_url: object, model: object) -> None:
    if type(base_url) is not str or base_url.strip().rstrip("/") != ECONOMY_BASE_URL:
        raise GatewayConfigurationError()
    if type(model) is not str or model.strip() != ECONOMY_MODEL:
        raise GatewayConfigurationError()


class _ProviderTransport(Protocol):
    async def complete(self, request: GatewayRequest) -> GatewayCompletion: ...

    async def aclose(self, *, drain_timeout: float = 10.0) -> None: ...


class ProviderRouterTransport:
    """Route to an explicitly selected provider without implicit fallback."""

    def __init__(
        self,
        *,
        primary: _ProviderTransport | None,
        economy: _ProviderTransport | None,
        total_limit: int,
        lane_limits: Mapping[str, int],
        max_pending: int | None = None,
    ) -> None:
        if type(total_limit) is not int or total_limit <= 0:
            raise GatewayConfigurationError()
        expected_tasks = {task.value for task in LLMTask}
        if set(lane_limits) != expected_tasks or any(
            type(limit) is not int or limit <= 0 for limit in lane_limits.values()
        ):
            raise GatewayConfigurationError()
        self._primary = primary
        if max_pending is not None and (type(max_pending) is not int or max_pending <= 0):
            raise GatewayConfigurationError()
        self._admission_limit = total_limit + (max_pending if max_pending is not None else total_limit * 4)
        self._economy = economy
        self._total = asyncio.Semaphore(total_limit)
        self._lanes = {
            task: asyncio.Semaphore(lane_limits[task]) for task in expected_tasks
        }
        self._accepting = True
        self._closed = False
        self._admission_lock = asyncio.Lock()
        self._close_lock = asyncio.Lock()
        self._active_tasks: set[asyncio.Task[object]] = set()
        self._drained = asyncio.Event()
        self._drained.set()

    async def complete(self, request: GatewayRequest) -> GatewayCompletion:
        if not isinstance(request, GatewayRequest):
            raise GatewayConfigurationError()
        resource = self._transport_for(request)
        async with self._admission_lock:
            if not self._accepting or self._closed:
                raise GatewayConfigurationError(task=request.task)
            if len(self._active_tasks) >= self._admission_limit:
                raise GatewayRateLimitError(task=request.task)
            task = asyncio.current_task()
            if task is None:
                raise GatewayConfigurationError(task=request.task)
            self._active_tasks.add(task)
            self._drained.clear()
        try:
            deadline = asyncio.get_running_loop().time() + request.timeout
            try:
                return await asyncio.wait_for(
                    self._complete_admitted(resource, request, deadline), timeout=request.timeout
                )
            except asyncio.TimeoutError:
                raise GatewayTimeout(task=request.task) from None
        finally:
            self._active_tasks.discard(task)
            if not self._active_tasks:
                self._drained.set()

    async def _complete_admitted(self, resource: _ProviderTransport, request: GatewayRequest,
                                 deadline: float) -> GatewayCompletion:
        async with self._lanes[request.task.value]:
            async with self._total:
                remaining = deadline - asyncio.get_running_loop().time()
                if remaining <= 0:
                    raise GatewayTimeout(task=request.task)
                return await resource.complete(replace(request, timeout=min(request.timeout, remaining)))

    def _transport_for(self, request: GatewayRequest) -> _ProviderTransport:
        if request.provider is LLMProvider.PRIMARY:
            if self._primary is None:
                raise GatewayConfigurationError(task=request.task)
            return self._primary
        if request.provider is not LLMProvider.ECONOMY or self._economy is None:
            raise GatewayConfigurationError(task=request.task)
        if request.model != ECONOMY_MODEL or not request.thinking_disabled:
            raise GatewayConfigurationError(task=request.task)
        if request.temperature is not None and request.temperature > 1:
            raise GatewayConfigurationError(task=request.task)
        if (
            request.response_format is not None
            and request.response_format.get("type") not in {"text", "json_object"}
        ):
            raise GatewayConfigurationError(task=request.task)
        if any(type(message.get("content")) is not str for message in request.messages):
            raise GatewayConfigurationError(task=request.task)
        return self._economy

    def _resources(self) -> tuple[_ProviderTransport, ...]:
        resources: list[_ProviderTransport] = []
        seen: set[int] = set()
        for resource in (self._primary, self._economy):
            if resource is None or id(resource) in seen:
                continue
            seen.add(id(resource))
            resources.append(resource)
        return tuple(resources)

    async def _cancel_active_calls(self) -> None:
        active = tuple(self._active_tasks)
        for task in active:
            task.cancel()
        if active:
            await asyncio.gather(*active, return_exceptions=True)

    @staticmethod
    async def _close_resources(
        resources: tuple[_ProviderTransport, ...], *, drain_timeout: float
    ) -> BaseException | None:
        results = await asyncio.gather(
            *(
                resource.aclose(drain_timeout=drain_timeout)
                for resource in resources
            ),
            return_exceptions=True,
        )
        return next(
            (result for result in results if isinstance(result, BaseException)),
            None,
        )

    async def aclose(self, *, drain_timeout: float = 10.0) -> None:
        if (
            type(drain_timeout) not in (int, float)
            or not math.isfinite(drain_timeout)
            or drain_timeout < 0
        ):
            raise GatewayConfigurationError()
        async with self._close_lock:
            if self._closed:
                return
            loop = asyncio.get_running_loop()
            started = loop.time()
            resources = self._resources()
            try:
                async with self._admission_lock:
                    self._accepting = False
                waited_for_active = bool(self._active_tasks)
                if self._active_tasks:
                    try:
                        await asyncio.wait_for(
                            self._drained.wait(), timeout=float(drain_timeout)
                        )
                    except asyncio.TimeoutError:
                        await self._cancel_active_calls()
                remaining = (
                    max(
                        0.0,
                        float(drain_timeout) - (loop.time() - started),
                    )
                    if waited_for_active
                    else float(drain_timeout)
                )
                first_error = await self._close_resources(
                    resources,
                    drain_timeout=remaining,
                )
                if first_error is not None:
                    raise first_error
                self._closed = True
            except asyncio.CancelledError:
                self._accepting = False
                for task in tuple(self._active_tasks):
                    task.cancel()
                raise


__all__ = [
    "ECONOMY_BASE_URL",
    "ECONOMY_MODEL",
    "ProviderRouterTransport",
    "validate_economy_provider",
]
