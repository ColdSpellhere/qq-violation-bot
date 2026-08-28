from __future__ import annotations

import asyncio
import json
import unittest
from dataclasses import replace
from unittest.mock import AsyncMock, call

import httpx

from plugins.llm_gateway.contracts import GatewayRequest, LLMProvider, LLMTask
from plugins.llm_gateway.errors import (
    GatewayAuthenticationError,
    GatewayClientError,
    GatewayConfigurationError,
    GatewayContractError,
    GatewayEmptyContentError,
    GatewayPaymentRequiredError,
    GatewayRateLimitError,
    GatewayServerError,
    GatewayTimeout,
    GatewayTransportError,
    is_retryable,
)
from plugins.llm_gateway.transport import LLMTransport


def gateway_request(task: LLMTask = LLMTask.CHAT_REPLY) -> GatewayRequest:
    return GatewayRequest(
        task=task,
        messages=({"role": "user", "content": "hello"},),
        model="requested-model",
        timeout=5,
        temperature=0.2,
    )


class LLMTransportTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.clients: list[httpx.AsyncClient] = []

    async def asyncTearDown(self) -> None:
        for client in self.clients:
            if not client.is_closed:
                await client.aclose()

    def make_transport(self, handler, **kwargs) -> LLMTransport:
        client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        self.clients.append(client)
        return LLMTransport(
            base_url="https://provider.example/v1",
            api_key="secret-value",
            client=client,
            total_limit=2,
            lane_limits={task.value: 1 for task in LLMTask},
            max_attempts=kwargs.pop("max_attempts", 3),
            retry_base_delay=kwargs.pop("retry_base_delay", 0.5),
            retry_delay_cap=kwargs.pop("retry_delay_cap", 4.0),
            jitter_ratio=kwargs.pop("jitter_ratio", 0),
            **kwargs,
        )

    async def wait_for_waiter(self, semaphore: asyncio.Semaphore) -> None:
        for _ in range(20):
            waiters = getattr(semaphore, "_waiters", None)
            if waiters and len(waiters) == 1:
                return
            await asyncio.sleep(0)
        self.fail("request did not reach the expected semaphore wait queue")

    async def test_success_uses_payload_and_extracts_model_content_and_usage(self) -> None:
        seen: list[httpx.Request] = []

        def handler(http_request: httpx.Request) -> httpx.Response:
            seen.append(http_request)
            return httpx.Response(200, json={
                "model": "served-model",
                "choices": [{"message": {"content": "  answer  "}}],
                "usage": {"prompt_tokens": 7, "completion_tokens": 3, "total_tokens": 10},
            })

        completion = await self.make_transport(handler).complete(gateway_request())
        self.assertEqual(("answer", "served-model", 0), (
            completion.content, completion.model, completion.retries
        ))
        self.assertEqual((7, 3, 10), (
            completion.usage.input_tokens,
            completion.usage.output_tokens,
            completion.usage.total_tokens,
        ))
        self.assertEqual("https://provider.example/v1/chat/completions", str(seen[0].url))
        self.assertEqual("Bearer secret-value", seen[0].headers["Authorization"])
        self.assertEqual(gateway_request().to_payload(), json.loads(seen[0].content))

    async def test_base_url_canonicalizes_root_and_existing_v1_without_duplication(self) -> None:
        for base_url in (
            "https://api.deepseek.com",
            "https://api.deepseek.com/v1",
            "https://api.deepseek.com/v1/",
        ):
            seen = []

            def handler(http_request: httpx.Request) -> httpx.Response:
                seen.append(str(http_request.url))
                return httpx.Response(200, json={
                    "model": "m", "choices": [{"message": {"content": "ok"}}]
                })

            client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
            self.clients.append(client)
            transport = LLMTransport(
                base_url=base_url,
                api_key="key",
                client=client,
                total_limit=1,
                lane_limits={task.value: 1 for task in LLMTask},
                max_attempts=1,
            )
            with self.subTest(base_url=base_url):
                await transport.complete(gateway_request())
                self.assertEqual(
                    ["https://api.deepseek.com/v1/chat/completions"], seen
                )

    async def test_bigmodel_base_url_uses_exact_secure_api_prefix(self) -> None:
        seen: list[str] = []

        def handler(http_request: httpx.Request) -> httpx.Response:
            seen.append(str(http_request.url))
            return httpx.Response(
                200,
                json={"model": "glm-4.7-flash", "choices": [{"message": {"content": "ok"}}]},
            )

        client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        self.clients.append(client)
        transport = LLMTransport(
            base_url="https://open.bigmodel.cn/api/paas/v4/",
            api_key="synthetic-key",
            client=client,
            total_limit=1,
            lane_limits={task.value: 1 for task in LLMTask},
            max_attempts=1,
        )

        await transport.complete(gateway_request())
        self.assertEqual(
            ["https://open.bigmodel.cn/api/paas/v4/chat/completions"], seen
        )

        for invalid in (
            "http://open.bigmodel.cn/api/paas/v4",
            "https://open.bigmodel.cn.evil.invalid/api/paas/v4",
            "https://open.bigmodel.cn:444/api/paas/v4",
            "https://open.bigmodel.cn/api/paas/v4/extra",
        ):
            with self.subTest(invalid=invalid), self.assertRaises(GatewayConfigurationError):
                LLMTransport(
                    base_url=invalid,
                    api_key="synthetic-key",
                    client=client,
                    total_limit=1,
                    lane_limits={task.value: 1 for task in LLMTask},
                )

    async def test_configuration_requires_http_url_key_and_complete_lane_limits(self) -> None:
        client = httpx.AsyncClient(transport=httpx.MockTransport(lambda _: httpx.Response(200)))
        self.clients.append(client)
        base = dict(client=client, total_limit=1, lane_limits={task.value: 1 for task in LLMTask})
        for base_url in (
            "",
            "provider.example",
            "ftp://provider.example",
            "https://u:p@provider.example",
            "https://provider.example/api",
            "https://provider.example/v1/chat/completions",
        ):
            with self.subTest(base_url=base_url), self.assertRaises(GatewayConfigurationError):
                LLMTransport(base_url=base_url, api_key="key", **base)
        for api_key in ("", "   "):
            with self.subTest(api_key=api_key), self.assertRaises(GatewayConfigurationError):
                LLMTransport(base_url="https://provider.example", api_key=api_key, **base)
        with self.assertRaises(GatewayConfigurationError):
            LLMTransport(
                base_url="https://provider.example", api_key="key", client=client,
                total_limit=1, lane_limits={LLMTask.CHAT_REPLY.value: 1},
            )

    async def test_non_retryable_http_errors_attempt_once_and_hide_body(self) -> None:
        cases = (
            (401, GatewayAuthenticationError),
            (403, GatewayAuthenticationError),
            (400, GatewayClientError),
            (409, GatewayClientError),
            (501, GatewayServerError),
        )
        for status, expected in cases:
            attempts = 0

            def handler(_request: httpx.Request) -> httpx.Response:
                nonlocal attempts
                attempts += 1
                return httpx.Response(status, text="sensitive provider body")

            with self.subTest(status=status), self.assertRaises(expected) as raised:
                await self.make_transport(handler).complete(gateway_request())
            self.assertEqual(1, attempts)
            self.assertNotIn("sensitive", str(raised.exception))
            self.assertEqual(status, raised.exception.status_code)

    async def test_payment_required_is_precise_non_retryable_and_redacted(self) -> None:
        attempts = 0

        def handler(_request: httpx.Request) -> httpx.Response:
            nonlocal attempts
            attempts += 1
            return httpx.Response(402, text="sensitive provider balance detail")

        with self.assertRaises(GatewayPaymentRequiredError) as raised:
            await self.make_transport(handler).complete(gateway_request())

        self.assertEqual("GatewayPaymentRequiredError", type(raised.exception).__name__)
        self.assertIsInstance(raised.exception, GatewayClientError)
        self.assertFalse(is_retryable(raised.exception))
        self.assertEqual(402, raised.exception.status_code)
        self.assertEqual(0, raised.exception.retries)
        self.assertEqual(1, attempts)
        self.assertNotIn("sensitive", str(raised.exception))

    async def test_retryable_statuses_have_exact_attempts_and_bounded_delays(self) -> None:
        for status, expected in (
            (408, GatewayTimeout),
            (429, GatewayRateLimitError),
            (500, GatewayServerError),
            (503, GatewayServerError),
        ):
            attempts = 0

            def handler(_request: httpx.Request) -> httpx.Response:
                nonlocal attempts
                attempts += 1
                return httpx.Response(status, headers={"Retry-After": "99"})

            sleep = AsyncMock()
            with self.subTest(status=status), self.assertRaises(expected) as raised:
                await self.make_transport(handler, sleep=sleep).complete(gateway_request())
            self.assertEqual(3, attempts)
            self.assertEqual(2, raised.exception.retries)
            self.assertEqual([call(0.5), call(1.0)], sleep.await_args_list)

    async def test_valid_retry_after_is_used(self) -> None:
        attempts = 0

        def handler(_request: httpx.Request) -> httpx.Response:
            nonlocal attempts
            attempts += 1
            if attempts == 1:
                return httpx.Response(429, headers={"Retry-After": "2"})
            return httpx.Response(200, json={
                "model": "m", "choices": [{"message": {"content": "ok"}}]
            })

        sleep = AsyncMock()
        completion = await self.make_transport(handler, sleep=sleep).complete(gateway_request())
        self.assertEqual(1, completion.retries)
        sleep.assert_awaited_once_with(2.0)

    async def test_bigmodel_429_business_codes_do_not_blindly_retry(self) -> None:
        request = replace(gateway_request(), provider=LLMProvider.ECONOMY)
        for code, expected in (
            ("1113", GatewayPaymentRequiredError),
            ("1308", GatewayClientError),
            ("1309", GatewayClientError),
            ("1321", GatewayClientError),
        ):
            attempts = 0

            def handler(_request: httpx.Request) -> httpx.Response:
                nonlocal attempts
                attempts += 1
                return httpx.Response(
                    429,
                    json={"error": {"code": code, "message": "sensitive detail"}},
                )

            with self.subTest(code=code), self.assertRaises(expected) as raised:
                await self.make_transport(handler).complete(request)
            self.assertEqual(1, attempts)
            self.assertEqual(0, raised.exception.retries)
            self.assertNotIn("sensitive", str(raised.exception))

    async def test_bigmodel_rate_and_busy_codes_retry_without_exposing_body(self) -> None:
        request = replace(gateway_request(), provider=LLMProvider.ECONOMY)
        for code in ("1302", "1305"):
            attempts = 0
            sleep = AsyncMock()

            def handler(_request: httpx.Request) -> httpx.Response:
                nonlocal attempts
                attempts += 1
                return httpx.Response(
                    429,
                    json={"error": {"code": code, "message": "sensitive detail"}},
                )

            with self.subTest(code=code), self.assertRaises(GatewayRateLimitError):
                await self.make_transport(handler, sleep=sleep).complete(request)
            self.assertEqual(3, attempts)
            self.assertEqual([call(0.5), call(1.0)], sleep.await_args_list)

    async def test_bigmodel_network_finish_reason_retries(self) -> None:
        attempts = 0
        sleep = AsyncMock()
        request = replace(gateway_request(), provider=LLMProvider.ECONOMY)

        def handler(_request: httpx.Request) -> httpx.Response:
            nonlocal attempts
            attempts += 1
            if attempts == 1:
                return httpx.Response(
                    200,
                    json={
                        "model": "glm-4.7-flash",
                        "choices": [
                            {
                                "finish_reason": "network_error",
                                "message": {"content": "partial"},
                            }
                        ],
                    },
                )
            return httpx.Response(
                200,
                json={
                    "model": "glm-4.7-flash",
                    "choices": [{"message": {"content": "ok"}}],
                },
            )

        completion = await self.make_transport(handler, sleep=sleep).complete(request)
        self.assertEqual("ok", completion.content)
        self.assertEqual(1, completion.retries)
        sleep.assert_awaited_once_with(0.5)

    async def test_timeout_and_connect_errors_retry_but_cancellation_escapes(self) -> None:
        for exc, expected in (
            (httpx.ReadTimeout("slow"), GatewayTimeout),
            (httpx.ConnectError("offline"), GatewayTransportError),
        ):
            attempts = 0

            def handler(_request: httpx.Request) -> httpx.Response:
                nonlocal attempts
                attempts += 1
                raise exc

            sleep = AsyncMock()
            with self.subTest(error=type(exc).__name__), self.assertRaises(expected):
                await self.make_transport(handler, max_attempts=2, sleep=sleep).complete(
                    gateway_request()
                )
            self.assertEqual(2, attempts)
            sleep.assert_awaited_once_with(0.5)

        async def cancelled(_request: httpx.Request) -> httpx.Response:
            raise asyncio.CancelledError()

        with self.assertRaises(asyncio.CancelledError):
            await self.make_transport(cancelled).complete(gateway_request())

    async def test_malformed_missing_and_empty_responses_attempt_once(self) -> None:
        cases = (
            (lambda: httpx.Response(200, content=b"{"), GatewayContractError),
            (lambda: httpx.Response(200, json={"model": "m"}), GatewayContractError),
            (lambda: httpx.Response(200, json={"model": "m", "choices": []}), GatewayContractError),
            (lambda: httpx.Response(200, json={"model": "m", "choices": [{"message": {}}]}), GatewayContractError),
            (lambda: httpx.Response(200, json={"model": "m", "choices": [{"message": {"content": "  "}}]}), GatewayEmptyContentError),
            (
                lambda: httpx.Response(
                    200,
                    json={
                        "model": "m",
                        "choices": [
                            {
                                "finish_reason": "length",
                                "message": {"content": '{"partial":true'},
                            }
                        ],
                    },
                ),
                GatewayContractError,
            ),
        )
        for response_factory, expected in cases:
            attempts = 0

            def handler(_request: httpx.Request) -> httpx.Response:
                nonlocal attempts
                attempts += 1
                return response_factory()

            with self.subTest(error=expected.__name__), self.assertRaises(expected):
                await self.make_transport(handler).complete(gateway_request())
            self.assertEqual(1, attempts)

    async def test_waiting_task_lane_does_not_hold_total_and_starve_other_lane(self) -> None:
        first_chat_entered = asyncio.Event()
        second_chat_entered = asyncio.Event()
        image_entered = asyncio.Event()
        release_first_chat = asyncio.Event()
        chat_calls = 0

        async def handler(http_request: httpx.Request) -> httpx.Response:
            nonlocal chat_calls
            model = json.loads(http_request.content)["model"]
            if model == "chat-model":
                chat_calls += 1
                if chat_calls == 1:
                    first_chat_entered.set()
                    await release_first_chat.wait()
                else:
                    second_chat_entered.set()
            else:
                image_entered.set()
            return httpx.Response(200, json={
                "model": model, "choices": [{"message": {"content": "ok"}}]
            })

        client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        self.clients.append(client)
        transport = LLMTransport(
            base_url="https://provider.example",
            api_key="key",
            client=client,
            total_limit=2,
            lane_limits={task.value: 1 for task in LLMTask},
            max_attempts=1,
        )
        chat_request = replace(gateway_request(LLMTask.CHAT_REPLY), model="chat-model")
        image_request = replace(
            gateway_request(LLMTask.IMAGE_DESCRIPTION), model="image-model"
        )
        first_chat = asyncio.create_task(transport.complete(chat_request))
        await first_chat_entered.wait()
        second_chat = asyncio.create_task(transport.complete(chat_request))
        await self.wait_for_waiter(transport._lanes[LLMTask.CHAT_REPLY.value])
        self.assertFalse(second_chat_entered.is_set())

        image = asyncio.create_task(transport.complete(image_request))
        for _ in range(5):
            await asyncio.sleep(0)
        try:
            self.assertTrue(
                image_entered.is_set(),
                "a request waiting for its task lane held the total permit",
            )
        finally:
            release_first_chat.set()
            await asyncio.gather(first_chat, second_chat, image)
        self.assertTrue(second_chat_entered.is_set())

    async def test_cancelling_while_waiting_for_lane_releases_total_capacity(self) -> None:
        first_entered = asyncio.Event()
        image_entered = asyncio.Event()
        release = asyncio.Event()

        async def handler(http_request: httpx.Request) -> httpx.Response:
            model = json.loads(http_request.content)["model"]
            if model == "first":
                first_entered.set()
                await release.wait()
            else:
                image_entered.set()
            return httpx.Response(200, json={
                "model": model, "choices": [{"message": {"content": "ok"}}]
            })

        client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        self.clients.append(client)
        transport = LLMTransport(
            base_url="https://provider.example", api_key="key", client=client,
            total_limit=2, lane_limits={task.value: 1 for task in LLMTask},
            max_attempts=1,
        )
        first = asyncio.create_task(transport.complete(replace(
            gateway_request(LLMTask.CHAT_REPLY), model="first"
        )))
        await first_entered.wait()
        waiting = asyncio.create_task(transport.complete(replace(
            gateway_request(LLMTask.CHAT_REPLY), model="waiting"
        )))
        await self.wait_for_waiter(transport._lanes[LLMTask.CHAT_REPLY.value])
        waiting.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await waiting

        image = asyncio.create_task(transport.complete(replace(
            gateway_request(LLMTask.IMAGE_DESCRIPTION), model="image"
        )))
        for _ in range(5):
            await asyncio.sleep(0)
        try:
            self.assertTrue(image_entered.is_set())
        finally:
            release.set()
            await asyncio.gather(first, image)

    async def test_cancelling_while_waiting_for_total_releases_task_lane(self) -> None:
        first_entered = asyncio.Event()
        replacement_entered = asyncio.Event()
        release = asyncio.Event()

        async def handler(http_request: httpx.Request) -> httpx.Response:
            model = json.loads(http_request.content)["model"]
            if model == "first":
                first_entered.set()
                await release.wait()
            else:
                replacement_entered.set()
            return httpx.Response(200, json={
                "model": model, "choices": [{"message": {"content": "ok"}}]
            })

        client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        self.clients.append(client)
        transport = LLMTransport(
            base_url="https://provider.example", api_key="key", client=client,
            total_limit=1, lane_limits={task.value: 1 for task in LLMTask},
            max_attempts=1,
        )
        first = asyncio.create_task(transport.complete(replace(
            gateway_request(LLMTask.CHAT_REPLY), model="first"
        )))
        await first_entered.wait()
        waiting = asyncio.create_task(transport.complete(replace(
            gateway_request(LLMTask.IMAGE_DESCRIPTION), model="cancelled"
        )))
        await self.wait_for_waiter(transport._total)
        waiting.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await waiting

        replacement = asyncio.create_task(transport.complete(replace(
            gateway_request(LLMTask.IMAGE_DESCRIPTION), model="replacement"
        )))
        release.set()
        await asyncio.gather(first, replacement)
        self.assertTrue(replacement_entered.is_set())

    async def test_request_exception_releases_total_and_task_lane(self) -> None:
        calls = 0

        def handler(_request: httpx.Request) -> httpx.Response:
            nonlocal calls
            calls += 1
            if calls == 1:
                raise httpx.ConnectError("offline")
            return httpx.Response(200, json={
                "model": "m", "choices": [{"message": {"content": "ok"}}]
            })

        transport = self.make_transport(handler, max_attempts=1)
        with self.assertRaises(GatewayTransportError):
            await transport.complete(gateway_request())
        completion = await transport.complete(gateway_request())
        self.assertEqual("ok", completion.content)

    async def test_total_limit_bounds_different_lanes(self) -> None:
        active = 0
        peak = 0
        two_entered = asyncio.Event()
        release = asyncio.Event()

        async def handler(_request: httpx.Request) -> httpx.Response:
            nonlocal active, peak
            active += 1
            peak = max(peak, active)
            if active == 2:
                two_entered.set()
            await release.wait()
            active -= 1
            return httpx.Response(200, json={
                "model": "m", "choices": [{"message": {"content": "ok"}}]
            })

        client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        self.clients.append(client)
        transport = LLMTransport(
            base_url="https://provider.example",
            api_key="key",
            client=client,
            total_limit=2,
            lane_limits={task.value: 2 for task in LLMTask},
            max_attempts=1,
        )
        calls = [
            asyncio.create_task(transport.complete(gateway_request(LLMTask.CHAT_REPLY))),
            asyncio.create_task(transport.complete(gateway_request(LLMTask.IMAGE_DESCRIPTION))),
            asyncio.create_task(transport.complete(gateway_request(LLMTask.PRIVATE_SUMMARY))),
        ]
        await asyncio.wait_for(two_entered.wait(), 1)
        self.assertEqual(2, peak)
        release.set()
        await asyncio.gather(*calls)
        self.assertEqual(2, peak)

    async def test_close_is_explicit_and_idempotent(self) -> None:
        transport = self.make_transport(lambda _: httpx.Response(200))
        client = transport.client
        await transport.aclose()
        await transport.aclose()
        self.assertTrue(client.is_closed)

    async def test_close_stops_intake_and_drains_an_active_call_before_client_close(self) -> None:
        entered = asyncio.Event()
        release = asyncio.Event()

        async def handler(_request: httpx.Request) -> httpx.Response:
            entered.set()
            await release.wait()
            return httpx.Response(200, json={
                "model": "m", "choices": [{"message": {"content": "ok"}}]
            })

        transport = self.make_transport(handler, max_attempts=1)
        active = asyncio.create_task(transport.complete(gateway_request()))
        await entered.wait()
        closing = asyncio.create_task(transport.aclose(drain_timeout=1.0))
        await asyncio.sleep(0)

        self.assertFalse(transport.client.is_closed)
        self.assertFalse(closing.done())
        with self.assertRaises(GatewayConfigurationError):
            await transport.complete(gateway_request())

        release.set()
        self.assertEqual("ok", (await active).content)
        await closing
        self.assertTrue(transport.client.is_closed)

    async def test_close_timeout_cancels_active_call_then_releases_permits(self) -> None:
        entered = asyncio.Event()
        cancelled = asyncio.Event()

        async def handler(_request: httpx.Request) -> httpx.Response:
            entered.set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                cancelled.set()
                raise

        transport = self.make_transport(handler, max_attempts=1)
        active = asyncio.create_task(transport.complete(gateway_request()))
        await entered.wait()
        await transport.aclose(drain_timeout=0.01)

        with self.assertRaises(asyncio.CancelledError):
            await active
        self.assertTrue(cancelled.is_set())
        self.assertTrue(transport.client.is_closed)
        self.assertEqual(2, transport._total._value)
        self.assertEqual(1, transport._lanes[LLMTask.CHAT_REPLY.value]._value)

    async def test_admission_lock_serializes_close_before_a_competing_complete(self) -> None:
        transport = self.make_transport(lambda _: httpx.Response(200))
        await transport._admission_lock.acquire()
        closing = asyncio.create_task(transport.aclose(drain_timeout=1.0))
        await asyncio.sleep(0)
        competing = asyncio.create_task(transport.complete(gateway_request()))
        await asyncio.sleep(0)
        transport._admission_lock.release()

        await closing
        with self.assertRaises(GatewayConfigurationError):
            await competing
        self.assertTrue(transport.client.is_closed)

    async def test_cancelling_close_cleans_active_call_and_client_before_propagating(self) -> None:
        entered = asyncio.Event()
        active_cancelled = asyncio.Event()

        async def handler(_request: httpx.Request) -> httpx.Response:
            entered.set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                active_cancelled.set()
                raise

        transport = self.make_transport(handler, max_attempts=1)
        active = asyncio.create_task(transport.complete(gateway_request()))
        await entered.wait()
        closing = asyncio.create_task(transport.aclose(drain_timeout=10.0))
        await asyncio.sleep(0)
        closing.cancel()

        with self.assertRaises(asyncio.CancelledError):
            await closing
        with self.assertRaises(asyncio.CancelledError):
            await asyncio.wait_for(active, timeout=0.1)
        self.assertTrue(active_cancelled.is_set())
        self.assertTrue(transport.client.is_closed)


if __name__ == "__main__":
    unittest.main()
