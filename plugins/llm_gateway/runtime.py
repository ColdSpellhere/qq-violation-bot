from __future__ import annotations

import asyncio
import inspect
import weakref
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

import httpx
from nonebot import get_driver, logger

from plugins.violation_record.config import CONFIG, AppConfig

from .contracts import LLMTask
from .errors import GatewayConfigurationError
from .gateway import Gateway
from .transport import LLMTransport
from .usage import UsageStore


_CLOSE_TIMEOUT_SECONDS = 11.0


@dataclass
class _DriverState:
    config: AppConfig
    enabled: Callable[[], bool]
    client_factory: Callable[..., object]
    transport_factory: Callable[..., object]
    usage_store_factory: Callable[[Path], object]
    drain_timeout: float
    close_timeout: float
    gateway: Gateway | None = None
    transport: object | None = None
    started: bool = False
    closed: bool = False
    failed: bool = False
    init_lock: asyncio.Lock = field(default_factory=asyncio.Lock)


_states: weakref.WeakKeyDictionary[object, _DriverState] = weakref.WeakKeyDictionary()


def _runtime_enabled() -> bool:
    from plugins.feature_control.runtime import FEATURES

    return FEATURES.snapshot().llm_gateway_enabled


def _client_factory(*, limits: httpx.Limits) -> httpx.AsyncClient:
    return httpx.AsyncClient(limits=limits)


def _lane_limits(config: AppConfig) -> dict[str, int]:
    memory = config.llm_gateway_memory_concurrency
    return {
        LLMTask.BUSINESS_INTENT.value: config.llm_gateway_business_concurrency,
        LLMTask.CHAT_REPLY.value: config.llm_gateway_chat_concurrency,
        LLMTask.MEMBER_EXTRACTION.value: memory,
        LLMTask.MEMBER_SUMMARY.value: memory,
        LLMTask.PRIVATE_SUMMARY.value: memory,
        LLMTask.RELATIONSHIP_UPDATE.value: memory,
        LLMTask.IMAGE_DESCRIPTION.value: config.llm_gateway_vision_concurrency,
    }


async def _close(
    resource: object, *, drain_timeout: float, close_timeout: float
) -> None:
    close = getattr(resource, "aclose", None)
    if close is None:
        return
    result = (
        close(drain_timeout=drain_timeout)
        if isinstance(resource, LLMTransport)
        else close()
    )
    if inspect.isawaitable(result):
        await asyncio.wait_for(result, timeout=close_timeout)


async def _ensure_gateway(state: _DriverState) -> Gateway:
    if (
        not state.started
        or state.closed
        or state.failed
        or not state.enabled()
    ):
        raise GatewayConfigurationError()
    if state.gateway is not None:
        return state.gateway

    async with state.init_lock:
        if (
            not state.started
            or state.closed
            or state.failed
            or not state.enabled()
        ):
            raise GatewayConfigurationError()
        if state.gateway is not None:
            return state.gateway

        config = state.config
        client: object | None = None
        transport: object | None = None
        try:
            limits = httpx.Limits(
                max_connections=config.llm_gateway_max_connections,
                max_keepalive_connections=config.llm_gateway_max_connections,
            )
            client = state.client_factory(limits=limits)
            transport = state.transport_factory(
                base_url=config.ai_base_url,
                api_key=config.ai_api_key,
                client=client,
                total_limit=config.llm_gateway_total_concurrency,
                lane_limits=_lane_limits(config),
                max_attempts=config.llm_gateway_max_retries + 1,
            )
            usage_store = state.usage_store_factory(Path(config.chat_archive_path))
            gateway = Gateway(
                transport=transport, usage_store=usage_store, config=config
            )
        except BaseException:
            state.failed = True
            if transport is not None:
                await _close(
                    transport,
                    drain_timeout=state.drain_timeout,
                    close_timeout=state.close_timeout,
                )
            elif client is not None:
                await _close(
                    client,
                    drain_timeout=state.drain_timeout,
                    close_timeout=state.close_timeout,
                )
            raise
        state.transport = transport
        state.gateway = gateway
        return gateway


def setup_lifecycle(
    *,
    driver: object | None = None,
    config: AppConfig = CONFIG,
    enabled: Callable[[], bool] | None = None,
    client_factory: Callable[..., object] = _client_factory,
    transport_factory: Callable[..., object] = LLMTransport,
    usage_store_factory: Callable[[Path], object] = UsageStore,
    drain_timeout: float = 10.0,
    close_timeout: float = _CLOSE_TIMEOUT_SECONDS,
) -> None:
    if driver is None:
        try:
            driver = get_driver()
        except ValueError:
            return
    if driver in _states:
        return
    state = _DriverState(
        config=config,
        enabled=enabled or _runtime_enabled,
        client_factory=client_factory,
        transport_factory=transport_factory,
        usage_store_factory=usage_store_factory,
        drain_timeout=drain_timeout,
        close_timeout=close_timeout,
    )
    _states[driver] = state

    @driver.on_startup
    async def _startup() -> None:
        if state.started and not state.closed:
            return
        state.started = True
        state.closed = False
        if not state.enabled():
            return
        await _ensure_gateway(state)

    @driver.on_shutdown
    async def _shutdown() -> None:
        async with state.init_lock:
            transport = state.transport
            state.gateway = None
            state.transport = None
            state.closed = True
        if transport is None:
            return
        try:
            await _close(
                transport,
                drain_timeout=state.drain_timeout,
                close_timeout=state.close_timeout,
            )
        except asyncio.TimeoutError:
            logger.warning("llm gateway close timed out error_class=TimeoutError")


async def get_gateway(*, driver: object | None = None) -> Gateway:
    if driver is None:
        try:
            driver = get_driver()
        except ValueError as exc:
            raise GatewayConfigurationError() from exc
    state = _states.get(driver)
    if state is None:
        raise GatewayConfigurationError()
    return await _ensure_gateway(state)


def _reset_for_tests() -> None:
    global _states
    _states = weakref.WeakKeyDictionary()


setup_lifecycle()


__all__ = ["get_gateway", "setup_lifecycle"]
