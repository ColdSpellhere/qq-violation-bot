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
from .providers import ProviderRouterTransport, validate_economy_provider
from .transport import LLMTransport
from .usage import UsageStore


_CLOSE_TIMEOUT_SECONDS = 11.0
_pending_close_tasks: set[asyncio.Future[object]] = set()


@dataclass
class _DriverState:
    config: AppConfig
    enabled: Callable[[], bool]
    economy_mode_enabled: Callable[[], bool]
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

    state = FEATURES.snapshot()
    return state.llm_gateway_enabled or state.economy_mode_enabled


def _economy_mode_enabled() -> bool:
    from plugins.feature_control.runtime import FEATURES

    return FEATURES.snapshot().economy_mode_enabled


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
        LLMTask.PRIVATE_FACT_EXTRACTION.value: memory,
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
        if isinstance(resource, (LLMTransport, ProviderRouterTransport))
        else close()
    )
    if inspect.isawaitable(result):
        task = asyncio.ensure_future(result)
        _pending_close_tasks.add(task)

        def _consume_close_result(completed: asyncio.Future[object]) -> None:
            _pending_close_tasks.discard(completed)
            try:
                completed.exception()
            except (asyncio.CancelledError, Exception):
                pass

        task.add_done_callback(_consume_close_result)
        cancellation_grace = min(0.1, max(0.0, float(close_timeout)) / 2)
        normal_timeout = max(0.0, float(close_timeout) - cancellation_grace)
        try:
            done, _pending = await asyncio.wait({task}, timeout=normal_timeout)
        except asyncio.CancelledError:
            task.cancel()
            raise
        if task not in done:
            task.cancel()
            if cancellation_grace:
                await asyncio.wait({task}, timeout=cancellation_grace)
            raise asyncio.TimeoutError()
        task.result()


async def _cleanup_failed_initialization(
    owned_transports: list[tuple[object, object]],
    unowned_clients: list[object],
    *,
    drain_timeout: float,
    close_timeout: float,
) -> None:
    grouped: dict[int, tuple[object, list[object]]] = {}
    for transport, client in owned_transports:
        entry = grouped.get(id(transport))
        if entry is None:
            grouped[id(transport)] = (transport, [client])
        else:
            entry[1].append(client)
    entries = tuple(grouped.values())
    results = await asyncio.gather(
        *(
            _close(
                transport,
                drain_timeout=drain_timeout,
                close_timeout=close_timeout,
            )
            for transport, _clients in entries
        ),
        return_exceptions=True,
    )
    fallback_clients = list(unowned_clients)
    for (_transport, clients), result in zip(entries, results):
        if isinstance(result, BaseException):
            logger.warning(
                "llm gateway initialization cleanup failed "
                f"error_class={type(result).__name__}"
            )
            fallback_clients.extend(clients)
    unique_clients = tuple({id(client): client for client in fallback_clients}.values())
    fallback_results = await asyncio.gather(
        *(
            _close(
                client,
                drain_timeout=drain_timeout,
                close_timeout=close_timeout,
            )
            for client in unique_clients
        ),
        return_exceptions=True,
    )
    for result in fallback_results:
        if isinstance(result, BaseException):
            logger.warning(
                "llm gateway client cleanup failed "
                f"error_class={type(result).__name__}"
            )


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
        owned_transports: list[tuple[object, object]] = []
        unowned_clients: list[object] = []
        transport: object | None = None
        try:
            economy_available = bool(
                getattr(config, "economy_provider_available", False)
            )
            economy_active = bool(state.economy_mode_enabled())
            primary_available = (
                type(getattr(config, "ai_api_key", None)) is str
                and bool(config.ai_api_key.strip())
            )
            if economy_active and not economy_available:
                raise GatewayConfigurationError()
            if not primary_available and not economy_active:
                raise GatewayConfigurationError()
            if economy_available:
                validate_economy_provider(
                    base_url=config.glm_base_url,
                    model=config.glm_model,
                )
            limits = httpx.Limits(
                max_connections=config.llm_gateway_max_connections,
                max_keepalive_connections=config.llm_gateway_max_connections,
            )
            primary_transport: object | None = None
            if primary_available:
                primary_client = state.client_factory(limits=limits)
                unowned_clients.append(primary_client)
                primary_transport = state.transport_factory(
                    base_url=config.ai_base_url,
                    api_key=config.ai_api_key,
                    client=primary_client,
                    total_limit=config.llm_gateway_total_concurrency,
                    lane_limits=_lane_limits(config),
                    max_attempts=config.llm_gateway_max_retries + 1,
                )
                owned_transports.append((primary_transport, primary_client))
                unowned_clients.pop()
            economy_transport: object | None = None
            if economy_available:
                economy_client = state.client_factory(limits=limits)
                unowned_clients.append(economy_client)
                economy_transport = state.transport_factory(
                    base_url=config.glm_base_url,
                    api_key=config.glm_api_key,
                    client=economy_client,
                    total_limit=config.llm_gateway_total_concurrency,
                    lane_limits=_lane_limits(config),
                    max_attempts=config.llm_gateway_max_retries + 1,
                )
                owned_transports.append((economy_transport, economy_client))
                unowned_clients.pop()
                transport = ProviderRouterTransport(
                    primary=primary_transport,
                    economy=economy_transport,
                    total_limit=config.llm_gateway_total_concurrency,
                    lane_limits=_lane_limits(config),
                )
            elif primary_transport is not None:
                transport = primary_transport
            else:
                raise GatewayConfigurationError()
            usage_store = state.usage_store_factory(Path(config.chat_archive_path))
            gateway = Gateway(
                transport=transport,
                usage_store=usage_store,
                config=config,
                economy_mode_enabled=state.economy_mode_enabled,
            )
        except BaseException:
            state.failed = True
            try:
                await _cleanup_failed_initialization(
                    owned_transports,
                    unowned_clients,
                    drain_timeout=state.drain_timeout,
                    close_timeout=state.close_timeout,
                )
            except BaseException as cleanup_error:
                logger.warning(
                    "llm gateway initialization cleanup aborted "
                    f"error_class={type(cleanup_error).__name__}"
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
    economy_mode_enabled: Callable[[], bool] | None = None,
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
        economy_mode_enabled=economy_mode_enabled or _economy_mode_enabled,
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
    for task in tuple(_pending_close_tasks):
        task.cancel()
    _pending_close_tasks.clear()
    _states = weakref.WeakKeyDictionary()


setup_lifecycle()


__all__ = ["get_gateway", "setup_lifecycle"]
