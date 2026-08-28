from __future__ import annotations

import asyncio
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from plugins.feature_control.commands import execute_control_command, is_control_command
from plugins.feature_control.state import FeatureController, FeatureState
from plugins.llm_gateway.contracts import (
    GatewayCompletion,
    GatewayRequest,
    LLMProvider,
    LLMTask,
)
from plugins.llm_gateway.errors import (
    GatewayConfigurationError,
    GatewayTransportError,
)
from plugins.llm_gateway.gateway import Gateway
from plugins.llm_gateway.providers import ProviderRouterTransport
from plugins.violation_record.config import AppConfig


MESSAGES = ({"role": "user", "content": "只处理文字"},)


def _state(**overrides: object) -> FeatureState:
    values: dict[str, object] = {
        "business_enabled": True,
        "chat_enabled": True,
        "group_chat_enabled": True,
        "private_chat_enabled": True,
        "group_chat_allowed_group_ids": (100,),
        "private_chat_allowed_user_ids": ("200",),
        "llm_gateway_enabled": False,
        "llm_gateway_vision_enabled": True,
        "llm_gateway_private_memory_enabled": False,
        "llm_gateway_member_memory_enabled": False,
        "llm_gateway_chat_enabled": False,
        "llm_gateway_business_enabled": False,
    }
    values.update(overrides)
    return FeatureState(**values)


class _Transport:
    def __init__(self, *, error: BaseException | None = None) -> None:
        self.requests: list[GatewayRequest] = []
        self.error = error
        self.closed_with: list[float] = []

    async def complete(self, request: GatewayRequest) -> GatewayCompletion:
        self.requests.append(request)
        if self.error is not None:
            raise self.error
        return GatewayCompletion(content="ok", model=request.model)

    async def aclose(self, *, drain_timeout: float = 10.0) -> None:
        self.closed_with.append(drain_timeout)


class _BlockingTransport(_Transport):
    def __init__(self) -> None:
        super().__init__()
        self.entered = asyncio.Event()
        self.release = asyncio.Event()

    async def complete(self, request: GatewayRequest) -> GatewayCompletion:
        self.requests.append(request)
        self.entered.set()
        await self.release.wait()
        return GatewayCompletion(content="ok", model=request.model)


class _BlockingCloseTransport(_Transport):
    def __init__(self) -> None:
        super().__init__()
        self.close_started = asyncio.Event()
        self.close_release = asyncio.Event()

    async def aclose(self, *, drain_timeout: float = 10.0) -> None:
        self.closed_with.append(drain_timeout)
        self.close_started.set()
        await self.close_release.wait()


class _StubbornCloseTransport(_Transport):
    def __init__(self) -> None:
        super().__init__()
        self.close_started = asyncio.Event()
        self.cancelled = asyncio.Event()
        self.close_release = asyncio.Event()

    async def aclose(self, *, drain_timeout: float = 10.0) -> None:
        self.closed_with.append(drain_timeout)
        self.close_started.set()
        try:
            await self.close_release.wait()
        except asyncio.CancelledError:
            self.cancelled.set()
            await self.close_release.wait()


class _CountingClient:
    def __init__(self) -> None:
        self.close_count = 0

    async def aclose(self) -> None:
        self.close_count += 1


class _OwnedTransport(_Transport):
    def __init__(
        self,
        client: _CountingClient,
        *,
        close_error: BaseException | None = None,
    ) -> None:
        super().__init__()
        self.client = client
        self.close_error = close_error

    async def aclose(self, *, drain_timeout: float = 10.0) -> None:
        self.closed_with.append(drain_timeout)
        if self.close_error is not None:
            raise self.close_error
        await self.client.aclose()


class _FlakyCloseTransport(_Transport):
    def __init__(self) -> None:
        super().__init__()
        self.close_calls = 0

    async def aclose(self, *, drain_timeout: float = 10.0) -> None:
        self.close_calls += 1
        if self.close_calls == 1:
            raise RuntimeError("first close failed")
        self.closed_with.append(drain_timeout)


class _Usage:
    def record_success(self, request: GatewayRequest, completion: GatewayCompletion) -> None:
        return None

    def record_failure(self, request: GatewayRequest, **kwargs: object) -> None:
        return None


def _config() -> SimpleNamespace:
    return SimpleNamespace(
        ai_model="deepseek-chat",
        ai_timeout=30,
        chat_vision_model="vision-model",
        chat_vision_timeout=60,
        glm_model="glm-4.7-flash",
        glm_timeout=30,
    )


def _lane_limits(limit: int = 4) -> dict[str, int]:
    return {task.value: limit for task in LLMTask}


def _router(
    *,
    primary: _Transport | None,
    economy: _Transport | None,
    total_limit: int = 4,
    lane_limit: int = 4,
) -> ProviderRouterTransport:
    return ProviderRouterTransport(
        primary=primary,
        economy=economy,
        total_limit=total_limit,
        lane_limits=_lane_limits(lane_limit),
    )


class EconomyFeatureControlTests(unittest.TestCase):
    def test_provider_availability_is_exact_and_config_repr_hides_glm_secret(self) -> None:
        valid = AppConfig(glm_api_key="synthetic-economy-secret")
        self.assertTrue(valid.economy_provider_available)
        self.assertNotIn("synthetic-economy-secret", repr(valid))
        self.assertFalse(
            AppConfig(
                glm_api_key="synthetic-economy-secret",
                glm_base_url="https://attacker.invalid/v1",
            ).economy_provider_available
        )
        self.assertFalse(
            AppConfig(
                glm_api_key="synthetic-economy-secret",
                glm_model="paid-model",
            ).economy_provider_available
        )

    def test_superuser_command_persists_mode_and_overrides_effective_gateway_domains(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "runtime_features.json"
            controller = FeatureController(
                path,
                _state(),
                economy_provider_available=True,
            )

            self.assertTrue(is_control_command("/穷鬼模式 开"))
            self.assertEqual(
                "穷鬼模式已开启：文字模型切换为 glm-4.7-flash，图片理解已停用。",
                execute_control_command("/穷鬼模式 开", controller, "1"),
            )
            self.assertTrue(controller.snapshot().economy_mode_enabled)
            for domain in ("business", "chat", "member_memory", "private_memory"):
                self.assertTrue(controller.llm_gateway_allowed(domain), domain)
            self.assertFalse(controller.llm_gateway_allowed("vision"))
            self.assertFalse(controller.image_understanding_allowed())

            reloaded = FeatureController(
                path,
                _state(),
                economy_provider_available=True,
            )
            self.assertTrue(reloaded.snapshot().economy_mode_enabled)
            self.assertIn(
                "穷鬼模式：开（文字：glm-4.7-flash；图片理解：关）",
                execute_control_command("/模块状态", reloaded, "1"),
            )

            self.assertEqual(
                "穷鬼模式已关闭：已恢复原文字模型和图片理解配置。",
                execute_control_command("/穷鬼模式 关", reloaded, "1"),
            )
            self.assertFalse(reloaded.snapshot().economy_mode_enabled)
            self.assertFalse(reloaded.llm_gateway_allowed("chat"))
            self.assertTrue(reloaded.image_understanding_allowed())

    def test_missing_provider_rejects_enable_without_mutating_state(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            controller = FeatureController(
                Path(directory) / "runtime_features.json",
                _state(),
                economy_provider_available=False,
            )
            self.assertEqual(
                "穷鬼模式不可用：当前实例未完整配置 GLM 网关。",
                execute_control_command("/穷鬼模式 开", controller, "1"),
            )
            self.assertFalse(controller.snapshot().economy_mode_enabled)

    def test_enabled_mode_stays_fail_closed_when_provider_configuration_disappears(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "runtime_features.json"
            configured = FeatureController(
                path,
                _state(),
                economy_provider_available=True,
            )
            configured.set_switch("economy_mode_enabled", True, "1")

            unavailable = FeatureController(
                path,
                _state(),
                economy_provider_available=False,
            )

            self.assertTrue(unavailable.snapshot().economy_mode_enabled)
            self.assertTrue(unavailable.llm_gateway_allowed("chat"))
            self.assertFalse(unavailable.image_understanding_allowed())
            self.assertIn(
                "穷鬼模式：开（GLM 配置不可用；文字调用已阻断；图片理解：关）",
                execute_control_command("/模块状态", unavailable, "1"),
            )

            self.assertEqual(
                "穷鬼模式已关闭：已恢复原文字模型和图片理解配置。",
                execute_control_command("/穷鬼模式 关", unavailable, "1"),
            )
            self.assertFalse(unavailable.snapshot().economy_mode_enabled)

    def test_enabled_environment_default_stays_fail_closed_without_provider(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            controller = FeatureController(
                Path(directory) / "runtime_features.json",
                _state(economy_mode_enabled=True),
                economy_provider_available=False,
            )
            self.assertTrue(controller.snapshot().economy_mode_enabled)

    def test_legacy_state_without_mode_field_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "runtime_features.json"
            state = _state()
            FeatureController(path, state).set_switch("chat_enabled", False, "1")
            legacy = json.loads(path.read_text(encoding="utf-8"))
            legacy.pop("economy_mode_enabled", None)
            path.write_text(json.dumps(legacy), encoding="utf-8")

            controller = FeatureController(
                path,
                _state(economy_mode_enabled=True),
                economy_provider_available=True,
            )
            self.assertFalse(controller.snapshot().economy_mode_enabled)

    def test_master_gateway_off_also_stops_economy_mode(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            controller = FeatureController(
                Path(directory) / "runtime_features.json",
                _state(),
                economy_provider_available=True,
            )
            execute_control_command("/穷鬼模式 开", controller, "1")
            execute_control_command("/模型网关 关", controller, "1")
            self.assertFalse(controller.snapshot().llm_gateway_enabled)
            self.assertFalse(controller.snapshot().economy_mode_enabled)

    def test_economy_only_instance_refuses_to_restore_missing_primary_provider(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            controller = FeatureController(
                Path(directory) / "runtime_features.json",
                _state(economy_mode_enabled=True),
                economy_provider_available=True,
                primary_provider_available=False,
            )

            self.assertEqual(
                "穷鬼模式无法关闭：当前实例未配置原文字模型；"
                "请先恢复原模型配置；如需停止当前纯 GLM 实例的模型调用，"
                "可使用 /模型网关 关。",
                execute_control_command("/穷鬼模式 关", controller, "1"),
            )
            self.assertTrue(controller.snapshot().economy_mode_enabled)
            with self.assertRaisesRegex(ValueError, "primary provider"):
                controller.set_switch("economy_mode_enabled", False, "1")

            self.assertEqual(
                "模型网关已关闭。",
                execute_control_command("/模型网关 关", controller, "1"),
            )
            self.assertFalse(controller.snapshot().economy_mode_enabled)


class EconomyGatewayRoutingTests(unittest.IsolatedAsyncioTestCase):
    async def test_explicit_request_mode_overrides_later_runtime_switch(self) -> None:
        enabled = True
        transport = _Transport()
        gateway = Gateway(
            transport=transport,
            usage_store=_Usage(),
            config=_config(),
            economy_mode_enabled=lambda: enabled,
        )

        await gateway.generate_chat_reply(
            MESSAGES,
            images=False,
            economy_mode=False,
        )
        self.assertEqual(LLMProvider.PRIMARY, transport.requests[-1].provider)

        enabled = False
        await gateway.generate_chat_reply(
            MESSAGES,
            images=False,
            economy_mode=True,
        )
        self.assertEqual(LLMProvider.ECONOMY, transport.requests[-1].provider)

    async def test_hot_mode_switch_routes_every_text_task_without_rebuilding_gateway(self) -> None:
        enabled = False
        transport = _Transport()
        gateway = Gateway(
            transport=transport,
            usage_store=_Usage(),
            config=_config(),
            economy_mode_enabled=lambda: enabled,
        )

        self.assertEqual("ok", await gateway.generate_chat_reply(MESSAGES, images=False))
        self.assertEqual(LLMProvider.PRIMARY, transport.requests[-1].provider)
        self.assertEqual("deepseek-chat", transport.requests[-1].model)

        enabled = True
        calls = (
            gateway.parse_business_intent(MESSAGES),
            gateway.generate_chat_reply(MESSAGES, images=False),
            gateway.extract_member_memories(MESSAGES),
            gateway.summarize_member_memory(MESSAGES),
            gateway.extract_private_facts(MESSAGES),
            gateway.summarize_private_conversation(MESSAGES),
            gateway.update_relationship_state(MESSAGES),
        )
        for call in calls:
            self.assertEqual("ok", await call)

        economy_requests = transport.requests[-len(calls) :]
        self.assertTrue(economy_requests)
        for request in economy_requests:
            self.assertEqual(LLMProvider.ECONOMY, request.provider)
            self.assertEqual("glm-4.7-flash", request.model)
            self.assertEqual(30, request.timeout)
            self.assertTrue(request.thinking_disabled)
            self.assertTrue(all(isinstance(message["content"], str) for message in request.messages))
        self.assertEqual(0.1, economy_requests[0].temperature)
        member_extraction = next(
            request
            for request in economy_requests
            if request.task is LLMTask.MEMBER_EXTRACTION
        )
        self.assertEqual({"type": "json_object"}, member_extraction.response_format)

    async def test_economy_mode_defensively_rejects_every_vision_request(self) -> None:
        transport = _Transport()
        gateway = Gateway(
            transport=transport,
            usage_store=_Usage(),
            config=_config(),
            economy_mode_enabled=lambda: True,
        )
        with self.assertRaises(GatewayConfigurationError):
            await gateway.generate_chat_reply(MESSAGES, images=True)
        with self.assertRaises(GatewayConfigurationError):
            await gateway.describe_image(
                (
                    {
                        "role": "user",
                        "content": [
                            {"type": "text", "text": "看图"},
                            {
                                "type": "image_url",
                                "image_url": {"url": "data:image/png;base64,AAAA"},
                            },
                        ],
                    },
                )
            )
        self.assertEqual([], transport.requests)

    async def test_claimed_vision_request_can_finish_with_its_frozen_primary_policy(
        self,
    ) -> None:
        transport = _Transport()
        gateway = Gateway(
            transport=transport,
            usage_store=_Usage(),
            config=_config(),
            economy_mode_enabled=lambda: True,
        )
        messages = (
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": "看图"},
                    {
                        "type": "image_url",
                        "image_url": {"url": "data:image/png;base64,AAAA"},
                    },
                ],
            },
        )

        self.assertEqual(
            "ok",
            await gateway.describe_image(messages, economy_mode=False),
        )
        self.assertEqual(LLMProvider.PRIMARY, transport.requests[0].provider)
        self.assertEqual("vision-model", transport.requests[0].model)


class ProviderRouterTests(unittest.IsolatedAsyncioTestCase):
    async def test_economy_only_router_fails_closed_for_primary_requests(self) -> None:
        economy = _Transport()
        router = _router(primary=None, economy=economy)
        primary_request = GatewayRequest(
            task=LLMTask.CHAT_REPLY,
            messages=MESSAGES,
            model="deepseek-chat",
            timeout=30,
            provider=LLMProvider.PRIMARY,
        )
        with self.assertRaises(GatewayConfigurationError):
            await router.complete(primary_request)
        self.assertEqual([], economy.requests)

    async def test_routes_explicitly_and_never_falls_back_to_paid_provider(self) -> None:
        primary = _Transport()
        failure = GatewayTransportError(task=LLMTask.CHAT_REPLY)
        economy = _Transport(error=failure)
        router = _router(primary=primary, economy=economy)
        request = GatewayRequest(
            task=LLMTask.CHAT_REPLY,
            messages=MESSAGES,
            model="glm-4.7-flash",
            timeout=30,
            provider=LLMProvider.ECONOMY,
            thinking_disabled=True,
        )

        with self.assertRaises(GatewayTransportError) as raised:
            await router.complete(request)
        self.assertIs(failure, raised.exception)
        self.assertEqual([], primary.requests)
        self.assertEqual([request], economy.requests)

    async def test_rejects_multimodal_economy_payload_and_closes_both_transports(self) -> None:
        primary = _Transport()
        economy = _Transport()
        router = _router(primary=primary, economy=economy)
        request = GatewayRequest(
            task=LLMTask.CHAT_REPLY,
            messages=(
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": "看图"},
                        {
                            "type": "image_url",
                            "image_url": {"url": "data:image/png;base64,AAAA"},
                        },
                    ],
                },
            ),
            model="glm-4.7-flash",
            timeout=30,
            provider=LLMProvider.ECONOMY,
        )

        with self.assertRaises(GatewayConfigurationError):
            await router.complete(request)
        self.assertEqual([], economy.requests)

        await router.aclose(drain_timeout=1.5)
        self.assertEqual([1.5], primary.closed_with)
        self.assertEqual([1.5], economy.closed_with)

    async def test_rejects_non_flash_model_unsupported_json_schema_and_high_temperature(self) -> None:
        router = _router(primary=_Transport(), economy=_Transport())
        base = GatewayRequest(
            task=LLMTask.CHAT_REPLY,
            messages=MESSAGES,
            model="glm-4.7-flash",
            timeout=30,
            provider=LLMProvider.ECONOMY,
            thinking_disabled=True,
        )
        invalid = (
            GatewayRequest(
                task=base.task,
                messages=base.messages,
                model="paid-model",
                timeout=base.timeout,
                provider=base.provider,
                thinking_disabled=True,
            ),
            GatewayRequest(
                task=base.task,
                messages=base.messages,
                model=base.model,
                timeout=base.timeout,
                provider=base.provider,
                thinking_disabled=True,
                response_format={
                    "type": "json_schema",
                    "json_schema": {
                        "name": "future_contract",
                        "strict": True,
                        "schema": {"type": "object"},
                    },
                },
            ),
            GatewayRequest(
                task=base.task,
                messages=base.messages,
                model=base.model,
                timeout=base.timeout,
                provider=base.provider,
                thinking_disabled=True,
                temperature=1.1,
            ),
        )
        for request in invalid:
            with self.subTest(request=request), self.assertRaises(GatewayConfigurationError):
                await router.complete(request)

    async def test_total_concurrency_is_shared_across_primary_and_economy(self) -> None:
        primary = _BlockingTransport()
        economy = _BlockingTransport()
        router = _router(
            primary=primary,
            economy=economy,
            total_limit=1,
            lane_limit=2,
        )
        primary_request = GatewayRequest(
            task=LLMTask.CHAT_REPLY,
            messages=MESSAGES,
            model="deepseek-chat",
            timeout=30,
            provider=LLMProvider.PRIMARY,
        )
        economy_request = GatewayRequest(
            task=LLMTask.BUSINESS_INTENT,
            messages=MESSAGES,
            model="glm-4.7-flash",
            timeout=30,
            provider=LLMProvider.ECONOMY,
            thinking_disabled=True,
        )

        primary_task = asyncio.create_task(router.complete(primary_request))
        await primary.entered.wait()
        economy_task = asyncio.create_task(router.complete(economy_request))
        await asyncio.sleep(0)
        self.assertFalse(economy.entered.is_set())

        primary.release.set()
        await economy.entered.wait()
        economy.release.set()
        await asyncio.gather(primary_task, economy_task)

    async def test_lane_concurrency_is_shared_across_primary_and_economy(self) -> None:
        primary = _BlockingTransport()
        economy = _BlockingTransport()
        router = _router(
            primary=primary,
            economy=economy,
            total_limit=2,
            lane_limit=1,
        )
        primary_request = GatewayRequest(
            task=LLMTask.CHAT_REPLY,
            messages=MESSAGES,
            model="deepseek-chat",
            timeout=30,
            provider=LLMProvider.PRIMARY,
        )
        economy_request = GatewayRequest(
            task=LLMTask.CHAT_REPLY,
            messages=MESSAGES,
            model="glm-4.7-flash",
            timeout=30,
            provider=LLMProvider.ECONOMY,
            thinking_disabled=True,
        )

        primary_task = asyncio.create_task(router.complete(primary_request))
        await primary.entered.wait()
        economy_task = asyncio.create_task(router.complete(economy_request))
        await asyncio.sleep(0)
        self.assertFalse(economy.entered.is_set())

        primary.release.set()
        await economy.entered.wait()
        economy.release.set()
        await asyncio.gather(primary_task, economy_task)

    async def test_close_starts_both_provider_drains_concurrently(self) -> None:
        primary = _BlockingCloseTransport()
        economy = _BlockingCloseTransport()
        router = _router(primary=primary, economy=economy)

        close_task = asyncio.create_task(router.aclose(drain_timeout=1.5))
        await primary.close_started.wait()
        await asyncio.sleep(0)
        try:
            self.assertTrue(economy.close_started.is_set())
        finally:
            primary.close_release.set()
            economy.close_release.set()
            await close_task

        self.assertEqual([1.5], primary.closed_with)
        self.assertEqual([1.5], economy.closed_with)

    async def test_close_drains_router_admitted_request_before_closing_provider(self) -> None:
        primary = _BlockingTransport()
        economy = _Transport()
        router = _router(primary=primary, economy=economy)
        request = GatewayRequest(
            task=LLMTask.CHAT_REPLY,
            messages=MESSAGES,
            model="deepseek-chat",
            timeout=30,
            provider=LLMProvider.PRIMARY,
        )

        request_task = asyncio.create_task(router.complete(request))
        await primary.entered.wait()
        close_task = asyncio.create_task(router.aclose(drain_timeout=1.0))
        await asyncio.sleep(0)
        self.assertEqual([], primary.closed_with)
        with self.assertRaises(GatewayConfigurationError):
            await router.complete(request)

        primary.release.set()
        self.assertEqual("ok", (await request_task).content)
        await close_task
        self.assertEqual(1, len(primary.closed_with))
        self.assertEqual(1, len(economy.closed_with))

    async def test_zero_drain_timeout_cancels_admitted_request_and_closes_both(self) -> None:
        primary = _BlockingTransport()
        economy = _Transport()
        router = _router(primary=primary, economy=economy)
        request = GatewayRequest(
            task=LLMTask.CHAT_REPLY,
            messages=MESSAGES,
            model="deepseek-chat",
            timeout=30,
            provider=LLMProvider.PRIMARY,
        )

        request_task = asyncio.create_task(router.complete(request))
        await primary.entered.wait()
        await router.aclose(drain_timeout=0)

        with self.assertRaises(asyncio.CancelledError):
            await request_task
        self.assertEqual([0.0], primary.closed_with)
        self.assertEqual([0.0], economy.closed_with)

    async def test_failed_child_close_can_be_retried(self) -> None:
        primary = _FlakyCloseTransport()
        economy = _Transport()
        router = _router(primary=primary, economy=economy)

        with self.assertRaisesRegex(RuntimeError, "first close"):
            await router.aclose(drain_timeout=1.0)
        await router.aclose(drain_timeout=1.0)

        self.assertEqual(2, primary.close_calls)
        self.assertEqual([1.0], primary.closed_with)


class _Driver:
    def __init__(self) -> None:
        self.startup: list[object] = []
        self.shutdown: list[object] = []

    def on_startup(self, callback):
        self.startup.append(callback)
        return callback

    def on_shutdown(self, callback):
        self.shutdown.append(callback)
        return callback


class EconomyGatewayLifecycleTests(unittest.IsolatedAsyncioTestCase):
    async def test_runtime_close_timeout_is_hard_even_when_provider_ignores_cancel(
        self,
    ) -> None:
        from plugins.llm_gateway import runtime

        primary = _StubbornCloseTransport()
        economy = _StubbornCloseTransport()
        router = _router(primary=primary, economy=economy)
        loop = asyncio.get_running_loop()
        started = loop.time()

        try:
            with self.assertRaises(asyncio.TimeoutError):
                await runtime._close(
                    router,
                    drain_timeout=1.0,
                    close_timeout=0.01,
                )
            self.assertLess(loop.time() - started, 0.1)
            await asyncio.wait_for(primary.cancelled.wait(), timeout=0.1)
            await asyncio.wait_for(economy.cancelled.wait(), timeout=0.1)
            self.assertEqual(1, len(primary.closed_with))
            self.assertEqual(1, len(economy.closed_with))
        finally:
            primary.close_release.set()
            economy.close_release.set()
            for _ in range(10):
                if not runtime._pending_close_tasks:
                    break
                await asyncio.sleep(0)
            runtime._reset_for_tests()

    async def test_valid_economy_mode_starts_without_primary_api_key(self) -> None:
        from plugins.llm_gateway import runtime

        runtime._reset_for_tests()
        driver = _Driver()
        transports: list[_Transport] = []

        def transport_factory(**kwargs: object) -> _Transport:
            transport = _Transport()
            transports.append(transport)
            return transport

        config = SimpleNamespace(
            **vars(_config()),
            ai_base_url="https://api.deepseek.com",
            ai_api_key="",
            glm_base_url="https://open.bigmodel.cn/api/paas/v4",
            glm_api_key="economy-secret",
            economy_provider_available=True,
            chat_archive_path=Path("unused.db"),
            llm_gateway_max_connections=4,
            llm_gateway_max_retries=1,
            llm_gateway_total_concurrency=4,
            llm_gateway_business_concurrency=1,
            llm_gateway_chat_concurrency=2,
            llm_gateway_vision_concurrency=1,
            llm_gateway_memory_concurrency=1,
        )
        runtime.setup_lifecycle(
            driver=driver,
            config=config,
            enabled=lambda: True,
            economy_mode_enabled=lambda: True,
            client_factory=lambda **_kwargs: object(),
            transport_factory=transport_factory,
            usage_store_factory=lambda _path: _Usage(),
        )

        await driver.startup[0]()
        gateway = await runtime.get_gateway(driver=driver)
        self.assertEqual("ok", await gateway.generate_chat_reply(MESSAGES, images=False))
        self.assertEqual(1, len(transports))
        self.assertEqual(LLMProvider.ECONOMY, transports[0].requests[0].provider)
        await driver.shutdown[0]()

    async def test_runtime_builds_and_closes_isolated_provider_transports(self) -> None:
        from plugins.llm_gateway import runtime

        runtime._reset_for_tests()
        driver = _Driver()
        economy_enabled = False
        transports: list[_Transport] = []
        clients: list[object] = []

        def client_factory(**kwargs: object) -> object:
            client = object()
            clients.append(client)
            return client

        def transport_factory(**kwargs: object) -> _Transport:
            transport = _Transport()
            transports.append(transport)
            return transport

        config = SimpleNamespace(
            **vars(_config()),
            ai_base_url="https://api.deepseek.com",
            ai_api_key="primary-secret",
            glm_base_url="https://open.bigmodel.cn/api/paas/v4",
            glm_api_key="economy-secret",
            economy_provider_available=True,
            chat_archive_path=Path("unused.db"),
            llm_gateway_max_connections=4,
            llm_gateway_max_retries=1,
            llm_gateway_total_concurrency=4,
            llm_gateway_business_concurrency=1,
            llm_gateway_chat_concurrency=2,
            llm_gateway_vision_concurrency=1,
            llm_gateway_memory_concurrency=1,
        )
        runtime.setup_lifecycle(
            driver=driver,
            config=config,
            enabled=lambda: True,
            economy_mode_enabled=lambda: economy_enabled,
            client_factory=client_factory,
            transport_factory=transport_factory,
            usage_store_factory=lambda _path: _Usage(),
        )

        await driver.startup[0]()
        gateway = await runtime.get_gateway(driver=driver)
        await gateway.generate_chat_reply(MESSAGES, images=False)
        economy_enabled = True
        await gateway.generate_chat_reply(MESSAGES, images=False)

        self.assertEqual(2, len(clients))
        self.assertEqual(2, len(transports))
        self.assertEqual(LLMProvider.PRIMARY, transports[0].requests[0].provider)
        self.assertEqual(LLMProvider.ECONOMY, transports[1].requests[0].provider)

        await driver.shutdown[0]()
        self.assertEqual([[10.0], [10.0]], [item.closed_with for item in transports])

    async def test_runtime_rejects_nonofficial_economy_endpoint_before_transport_creation(self) -> None:
        from plugins.llm_gateway import runtime

        runtime._reset_for_tests()
        driver = _Driver()
        transports: list[_Transport] = []

        def transport_factory(**kwargs: object) -> _Transport:
            transport = _Transport()
            transports.append(transport)
            return transport

        config = SimpleNamespace(
            **vars(_config()),
            ai_base_url="https://api.deepseek.com",
            ai_api_key="primary-secret",
            glm_base_url="https://attacker.invalid/v1",
            glm_api_key="economy-secret",
            economy_provider_available=True,
            chat_archive_path=Path("unused.db"),
            llm_gateway_max_connections=4,
            llm_gateway_max_retries=1,
            llm_gateway_total_concurrency=4,
            llm_gateway_business_concurrency=1,
            llm_gateway_chat_concurrency=2,
            llm_gateway_vision_concurrency=1,
            llm_gateway_memory_concurrency=1,
        )
        runtime.setup_lifecycle(
            driver=driver,
            config=config,
            enabled=lambda: True,
            economy_mode_enabled=lambda: True,
            client_factory=lambda **_kwargs: object(),
            transport_factory=transport_factory,
            usage_store_factory=lambda _path: _Usage(),
        )

        with self.assertRaises(GatewayConfigurationError):
            await driver.startup[0]()
        self.assertEqual([], transports)

    async def test_runtime_never_falls_back_to_primary_when_enabled_mode_is_unavailable(
        self,
    ) -> None:
        from plugins.llm_gateway import runtime

        runtime._reset_for_tests()
        driver = _Driver()
        transports: list[_Transport] = []
        clients: list[object] = []

        config = SimpleNamespace(
            **vars(_config()),
            ai_base_url="https://api.deepseek.com",
            ai_api_key="primary-secret",
            glm_base_url="https://open.bigmodel.cn/api/paas/v4",
            glm_api_key="",
            economy_provider_available=False,
            chat_archive_path=Path("unused.db"),
            llm_gateway_max_connections=4,
            llm_gateway_max_retries=1,
            llm_gateway_total_concurrency=4,
            llm_gateway_business_concurrency=1,
            llm_gateway_chat_concurrency=2,
            llm_gateway_vision_concurrency=1,
            llm_gateway_memory_concurrency=1,
        )
        runtime.setup_lifecycle(
            driver=driver,
            config=config,
            enabled=lambda: True,
            economy_mode_enabled=lambda: True,
            client_factory=lambda **_kwargs: clients.append(object()) or clients[-1],
            transport_factory=lambda **_kwargs: transports.append(_Transport())
            or transports[-1],
            usage_store_factory=lambda _path: _Usage(),
        )

        with self.assertRaises(GatewayConfigurationError):
            await driver.startup[0]()
        self.assertEqual([], clients)
        self.assertEqual([], transports)

    async def test_partial_provider_initialization_closes_each_client_once(self) -> None:
        from plugins.llm_gateway import runtime

        runtime._reset_for_tests()
        driver = _Driver()
        clients: list[_CountingClient] = []
        transports: list[_OwnedTransport] = []

        def client_factory(**_kwargs: object) -> _CountingClient:
            client = _CountingClient()
            clients.append(client)
            return client

        def transport_factory(**kwargs: object) -> _OwnedTransport:
            if transports:
                raise ValueError("economy transport initialization failed")
            transport = _OwnedTransport(kwargs["client"])
            transports.append(transport)
            return transport

        config = SimpleNamespace(
            **vars(_config()),
            ai_base_url="https://api.deepseek.com",
            ai_api_key="primary-secret",
            glm_base_url="https://open.bigmodel.cn/api/paas/v4",
            glm_api_key="economy-secret",
            economy_provider_available=True,
            chat_archive_path=Path("unused.db"),
            llm_gateway_max_connections=4,
            llm_gateway_max_retries=1,
            llm_gateway_total_concurrency=4,
            llm_gateway_business_concurrency=1,
            llm_gateway_chat_concurrency=2,
            llm_gateway_vision_concurrency=1,
            llm_gateway_memory_concurrency=1,
        )
        runtime.setup_lifecycle(
            driver=driver,
            config=config,
            enabled=lambda: True,
            economy_mode_enabled=lambda: False,
            client_factory=client_factory,
            transport_factory=transport_factory,
            usage_store_factory=lambda _path: _Usage(),
        )

        with self.assertRaisesRegex(ValueError, "economy transport"):
            await driver.startup[0]()
        self.assertEqual([1, 1], [client.close_count for client in clients])

    async def test_cleanup_failure_never_replaces_gateway_initialization_error(self) -> None:
        from plugins.llm_gateway import runtime

        runtime._reset_for_tests()
        driver = _Driver()
        clients: list[_CountingClient] = []
        transports: list[_OwnedTransport] = []

        def client_factory(**_kwargs: object) -> _CountingClient:
            client = _CountingClient()
            clients.append(client)
            return client

        def transport_factory(**kwargs: object) -> _OwnedTransport:
            transport = _OwnedTransport(
                kwargs["client"],
                close_error=(
                    RuntimeError("transport close failed")
                    if not transports
                    else None
                ),
            )
            transports.append(transport)
            return transport

        config = SimpleNamespace(
            **vars(_config()),
            ai_base_url="https://api.deepseek.com",
            ai_api_key="primary-secret",
            glm_base_url="https://open.bigmodel.cn/api/paas/v4",
            glm_api_key="economy-secret",
            economy_provider_available=True,
            chat_archive_path=Path("unused.db"),
            llm_gateway_max_connections=4,
            llm_gateway_max_retries=1,
            llm_gateway_total_concurrency=4,
            llm_gateway_business_concurrency=1,
            llm_gateway_chat_concurrency=2,
            llm_gateway_vision_concurrency=1,
            llm_gateway_memory_concurrency=1,
        )
        runtime.setup_lifecycle(
            driver=driver,
            config=config,
            enabled=lambda: True,
            economy_mode_enabled=lambda: False,
            client_factory=client_factory,
            transport_factory=transport_factory,
            usage_store_factory=lambda _path: (_ for _ in ()).throw(
                ValueError("usage store initialization failed")
            ),
        )

        with self.assertRaisesRegex(ValueError, "usage store"):
            await driver.startup[0]()
        self.assertEqual([1, 1], [client.close_count for client in clients])


if __name__ == "__main__":
    unittest.main()
