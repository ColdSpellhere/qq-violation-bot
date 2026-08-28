from __future__ import annotations

import asyncio
import os
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import httpx

from plugins.llm_gateway.contracts import GatewayCompletion, LLMTask
from plugins.llm_gateway.errors import GatewayConfigurationError, GatewayTimeout


MESSAGES = ({"role": "user", "content": "already built"},)


def config(**overrides):
    values = {
        "ai_base_url": "https://provider.example/v1",
        "ai_api_key": "secret",
        "ai_model": "chat-model",
        "ai_timeout": 31,
        "chat_vision_model": "vision-model",
        "chat_vision_timeout": 61,
        "chat_archive_path": Path("unused.db"),
        "llm_gateway_enabled": True,
        "llm_gateway_max_connections": 8,
        "llm_gateway_max_retries": 2,
        "llm_gateway_total_concurrency": 8,
        "llm_gateway_business_concurrency": 2,
        "llm_gateway_chat_concurrency": 3,
        "llm_gateway_vision_concurrency": 3,
        "llm_gateway_memory_concurrency": 2,
    }
    values.update(overrides)
    return SimpleNamespace(**values)


class RecordingTransport:
    def __init__(self, *, failure: BaseException | None = None) -> None:
        self.requests = []
        self.failure = failure

    async def complete(self, request):
        self.requests.append(request)
        if self.failure is not None:
            raise self.failure
        return GatewayCompletion(
            content="answer", model="served-model", latency_ms=12, retries=1
        )


class RecordingUsage:
    def __init__(self, *, failure: BaseException | None = None) -> None:
        self.successes = []
        self.failures = []
        self.failure = failure

    def record_success(self, request, completion) -> None:
        self.successes.append((request, completion))
        if self.failure is not None:
            raise self.failure

    def record_failure(self, request, *, latency_ms, retries, error) -> None:
        self.failures.append((request, latency_ms, retries, error))
        if self.failure is not None:
            raise self.failure


class GatewayRoutingTests(unittest.IsolatedAsyncioTestCase):
    async def test_gateway_module_does_not_import_business_or_store_packages(self) -> None:
        script = """
import sys
import plugins.llm_gateway.gateway
for forbidden in (
    "plugins.violation_record",
    "plugins.private_memory",
    "plugins.chat_archive",
    "plugins.member_memory",
):
    if forbidden in sys.modules:
        raise SystemExit(f"gateway imported forbidden package: {forbidden}")
"""
        completed = subprocess.run(
            [sys.executable, "-c", script],
            cwd=Path(__file__).resolve().parents[1],
            env={**os.environ, "TARGET_GROUP_ID": "918273645"},
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
        self.assertEqual(
            0,
            completed.returncode,
            f"stdout:\n{completed.stdout}\nstderr:\n{completed.stderr}",
        )

    async def test_named_methods_preserve_existing_models_options_and_task_lanes(self) -> None:
        from plugins.llm_gateway.gateway import Gateway

        transport = RecordingTransport()
        gateway = Gateway(
            transport=transport, usage_store=RecordingUsage(), config=config()
        )

        calls = (
            (gateway.parse_business_intent(MESSAGES), LLMTask.BUSINESS_INTENT, "chat-model", 31, 0, {"type": "json_object"}, False),
            (gateway.generate_chat_reply(MESSAGES, images=False), LLMTask.CHAT_REPLY, "chat-model", 31, 0.8, None, False),
            (gateway.generate_chat_reply(MESSAGES, images=True), LLMTask.CHAT_REPLY, "vision-model", 61, None, None, True),
            (gateway.extract_member_memories(MESSAGES), LLMTask.MEMBER_EXTRACTION, "chat-model", 31, 0.1, None, False),
            (gateway.summarize_member_memory(MESSAGES), LLMTask.MEMBER_SUMMARY, "chat-model", 31, 0.1, None, False),
            (gateway.extract_private_facts(MESSAGES), LLMTask.PRIVATE_FACT_EXTRACTION, "chat-model", 31, 0.1, {"type": "json_object"}, False),
            (gateway.summarize_private_conversation(MESSAGES), LLMTask.PRIVATE_SUMMARY, "chat-model", 31, 0.1, {"type": "json_object"}, False),
            (gateway.update_relationship_state(MESSAGES), LLMTask.RELATIONSHIP_UPDATE, "chat-model", 31, 0.1, {"type": "json_object"}, False),
            (gateway.describe_image(MESSAGES), LLMTask.IMAGE_DESCRIPTION, "vision-model", 61, None, None, True),
        )
        for awaitable, *_ in calls:
            self.assertEqual("answer", await awaitable)

        self.assertEqual(len(calls), len(transport.requests))
        for request, expected in zip(transport.requests, calls):
            _, task, model, timeout, temperature, response_format, thinking = expected
            with self.subTest(task=task, model=model):
                self.assertEqual(task, request.task)
                self.assertEqual(model, request.model)
                self.assertEqual(timeout, request.timeout)
                self.assertEqual(temperature, request.temperature)
                self.assertEqual(response_format, request.response_format)
                self.assertEqual(thinking, request.thinking_disabled)
                self.assertEqual(MESSAGES, request.messages)

    async def test_usage_is_recorded_for_success_and_failure(self) -> None:
        from plugins.llm_gateway.gateway import Gateway

        success_usage = RecordingUsage()
        gateway = Gateway(
            transport=RecordingTransport(), usage_store=success_usage, config=config()
        )
        self.assertEqual("answer", await gateway.describe_image(MESSAGES))
        self.assertEqual(1, len(success_usage.successes))

        timeout = GatewayTimeout(task=LLMTask.PRIVATE_SUMMARY)
        timeout.retries = 2
        failure_usage = RecordingUsage()
        gateway = Gateway(
            transport=RecordingTransport(failure=timeout),
            usage_store=failure_usage,
            config=config(),
            clock=iter((10.0, 10.125)).__next__,
        )
        with self.assertRaises(GatewayTimeout) as raised:
            await gateway.summarize_private_conversation(MESSAGES)
        self.assertIs(timeout, raised.exception)
        request, latency_ms, retries, error = failure_usage.failures[0]
        self.assertEqual(LLMTask.PRIVATE_SUMMARY, request.task)
        self.assertEqual(125, latency_ms)
        self.assertEqual(2, retries)
        self.assertIs(timeout, error)

    async def test_usage_write_failure_never_changes_model_result_or_error(self) -> None:
        from plugins.llm_gateway.gateway import Gateway

        logger = Mock()
        gateway = Gateway(
            transport=RecordingTransport(),
            usage_store=RecordingUsage(failure=sqlite3.OperationalError("private")),
            config=config(),
            logger=logger,
        )
        self.assertEqual("answer", await gateway.generate_chat_reply(MESSAGES, images=False))
        logger.warning.assert_called_once()
        self.assertNotIn("private", " ".join(map(str, logger.warning.call_args.args)))

        timeout = GatewayTimeout(task=LLMTask.CHAT_REPLY)
        gateway = Gateway(
            transport=RecordingTransport(failure=timeout),
            usage_store=RecordingUsage(failure=sqlite3.OperationalError("private")),
            config=config(),
            logger=Mock(),
        )
        with self.assertRaises(GatewayTimeout) as raised:
            await gateway.generate_chat_reply(MESSAGES, images=False)
        self.assertIs(timeout, raised.exception)


class FakeDriver:
    def __init__(self) -> None:
        self.startup = []
        self.shutdown = []

    def on_startup(self, function):
        self.startup.append(function)
        return function

    def on_shutdown(self, function):
        self.shutdown.append(function)
        return function


class ClosingTransport(RecordingTransport):
    def __init__(self, *, close_error: BaseException | None = None) -> None:
        super().__init__()
        self.close_count = 0
        self.close_error = close_error

    async def aclose(self) -> None:
        self.close_count += 1
        if self.close_error is not None:
            raise self.close_error


class GatewayLifecycleTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        from plugins.llm_gateway import runtime

        runtime._reset_for_tests()
        self.runtime = runtime

    async def test_setup_is_per_driver_idempotent_and_uses_one_shared_runtime(self) -> None:
        driver = FakeDriver()
        transport = ClosingTransport()
        clients = []
        transport_calls = []
        usage_stores = []

        def client_factory(**kwargs):
            client = object()
            clients.append((client, kwargs))
            return client

        def transport_factory(**kwargs):
            transport_calls.append(kwargs)
            return transport

        def usage_store_factory(_path):
            store = RecordingUsage()
            usage_stores.append(store)
            return store

        self.runtime.setup_lifecycle(
            driver=driver,
            config=config(),
            enabled=lambda: True,
            client_factory=client_factory,
            transport_factory=transport_factory,
            usage_store_factory=usage_store_factory,
        )
        self.runtime.setup_lifecycle(driver=driver, config=config())
        self.assertEqual((1, 1), (len(driver.startup), len(driver.shutdown)))
        with self.assertRaises(GatewayConfigurationError):
            await self.runtime.get_gateway(driver=driver)

        await driver.startup[0]()
        first = await self.runtime.get_gateway(driver=driver)
        self.assertIs(first, await self.runtime.get_gateway(driver=driver))
        self.assertEqual(1, len(clients))
        self.assertEqual(1, len(transport_calls))
        self.assertEqual(1, len(usage_stores))
        self.assertEqual(3, transport_calls[0]["max_attempts"])
        self.assertEqual(
            {
                LLMTask.BUSINESS_INTENT.value: 2,
                LLMTask.CHAT_REPLY.value: 3,
                LLMTask.MEMBER_EXTRACTION.value: 2,
                LLMTask.MEMBER_SUMMARY.value: 2,
                LLMTask.PRIVATE_SUMMARY.value: 2,
                LLMTask.PRIVATE_FACT_EXTRACTION.value: 2,
                LLMTask.RELATIONSHIP_UPDATE.value: 2,
                LLMTask.IMAGE_DESCRIPTION.value: 3,
            },
            transport_calls[0]["lane_limits"],
        )

        await driver.shutdown[0]()
        self.assertEqual(1, transport.close_count)
        with self.assertRaises(GatewayConfigurationError):
            await self.runtime.get_gateway(driver=driver)

    async def test_runtime_hot_enable_is_single_flight_disable_fails_closed_and_reenable_reuses(self) -> None:
        driver = FakeDriver()
        enabled = False
        clients = []
        transports = []
        usages = []

        def is_enabled() -> bool:
            return enabled

        def client_factory(**_kwargs):
            client = object()
            clients.append(client)
            return client

        def transport_factory(**_kwargs):
            transport = ClosingTransport()
            transports.append(transport)
            return transport

        def usage_store_factory(_path):
            usage = RecordingUsage()
            usages.append(usage)
            return usage

        self.runtime.setup_lifecycle(
            driver=driver,
            config=config(),
            enabled=is_enabled,
            client_factory=client_factory,
            transport_factory=transport_factory,
            usage_store_factory=usage_store_factory,
        )
        await driver.startup[0]()
        self.assertEqual([], clients)
        with self.assertRaises(GatewayConfigurationError):
            await self.runtime.get_gateway(driver=driver)

        enabled = True
        first, second, third = await asyncio.gather(
            *(self.runtime.get_gateway(driver=driver) for _ in range(3))
        )
        self.assertIs(first, second)
        self.assertIs(second, third)
        self.assertEqual((1, 1, 1), (len(clients), len(transports), len(usages)))

        enabled = False
        with self.assertRaises(GatewayConfigurationError):
            await self.runtime.get_gateway(driver=driver)

        enabled = True
        self.assertIs(first, await self.runtime.get_gateway(driver=driver))
        self.assertEqual((1, 1, 1), (len(clients), len(transports), len(usages)))

        await driver.shutdown[0]()
        with self.assertRaises(GatewayConfigurationError):
            await self.runtime.get_gateway(driver=driver)
        self.assertEqual(1, transports[0].close_count)

    async def test_schema_v1_fails_startup_and_closes_the_created_client(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        database = Path(temporary.name) / "chat.db"
        with closing(sqlite3.connect(database)) as connection:
            connection.execute(
                "CREATE TABLE private_memory_schema_meta("
                "singleton INTEGER PRIMARY KEY, schema_version INTEGER NOT NULL, updated_at TEXT NOT NULL)"
            )
            connection.execute(
                "INSERT INTO private_memory_schema_meta VALUES(1,1,'legacy')"
            )
            connection.commit()
        client = httpx.AsyncClient(transport=httpx.MockTransport(lambda _: httpx.Response(200)))
        driver = FakeDriver()
        self.runtime.setup_lifecycle(
            driver=driver,
            config=config(chat_archive_path=database),
            enabled=lambda: True,
            client_factory=lambda **_kwargs: client,
        )
        with self.assertRaisesRegex(RuntimeError, "schema"):
            await driver.startup[0]()
        self.assertTrue(client.is_closed)
        with self.assertRaises(GatewayConfigurationError):
            await self.runtime.get_gateway(driver=driver)

    async def test_invalid_enabled_configuration_closes_client_and_fails_startup(self) -> None:
        client = httpx.AsyncClient(transport=httpx.MockTransport(lambda _: httpx.Response(200)))
        driver = FakeDriver()
        self.runtime.setup_lifecycle(
            driver=driver,
            config=config(ai_model=""),
            enabled=lambda: True,
            client_factory=lambda **_kwargs: client,
            usage_store_factory=lambda _path: RecordingUsage(),
        )
        with self.assertRaises(GatewayConfigurationError):
            await driver.startup[0]()
        self.assertTrue(client.is_closed)

    async def test_shutdown_cancelled_error_propagates_and_getter_stays_closed(self) -> None:
        driver = FakeDriver()
        transport = ClosingTransport(close_error=asyncio.CancelledError())
        self.runtime.setup_lifecycle(
            driver=driver,
            config=config(),
            enabled=lambda: True,
            client_factory=lambda **_kwargs: object(),
            transport_factory=lambda **_kwargs: transport,
            usage_store_factory=lambda _path: RecordingUsage(),
        )
        await driver.startup[0]()
        with self.assertRaises(asyncio.CancelledError):
            await driver.shutdown[0]()
        with self.assertRaises(GatewayConfigurationError):
            await self.runtime.get_gateway(driver=driver)

    async def test_runtime_outer_timeout_cancels_active_transport_and_closes_client(self) -> None:
        from plugins.llm_gateway.transport import LLMTransport

        entered = asyncio.Event()
        cancelled = asyncio.Event()

        async def handler(_request: httpx.Request) -> httpx.Response:
            entered.set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                cancelled.set()
                raise

        client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        driver = FakeDriver()
        self.runtime.setup_lifecycle(
            driver=driver,
            config=config(),
            enabled=lambda: True,
            client_factory=lambda **_kwargs: client,
            transport_factory=lambda **kwargs: LLMTransport(**kwargs),
            usage_store_factory=lambda _path: RecordingUsage(),
            drain_timeout=1.0,
            close_timeout=0.01,
        )
        await driver.startup[0]()
        gateway = await self.runtime.get_gateway(driver=driver)
        active = asyncio.create_task(
            gateway.generate_chat_reply(MESSAGES, images=False)
        )
        await entered.wait()

        await driver.shutdown[0]()

        with self.assertRaises(asyncio.CancelledError):
            await active
        self.assertTrue(cancelled.is_set())
        self.assertTrue(client.is_closed)
        with self.assertRaises(GatewayConfigurationError):
            await self.runtime.get_gateway(driver=driver)


if __name__ == "__main__":
    unittest.main()
