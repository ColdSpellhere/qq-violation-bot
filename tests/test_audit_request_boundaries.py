from __future__ import annotations

import asyncio
import unittest
from dataclasses import replace

import httpx

from plugins.llm_gateway.contracts import GatewayCompletion, GatewayRequest, LLMTask
from plugins.llm_gateway.errors import GatewayRateLimitError, GatewayTimeout
from plugins.llm_gateway.providers import ProviderRouterTransport
from plugins.llm_gateway.transport import LLMTransport
from plugins.web_search.client import TavilySearchClient, WebSearchError
from plugins.web_search.policy import build_search_query


def request(timeout=0.03):
    return GatewayRequest(LLMTask.CHAT_REPLY, ({"role": "user", "content": "synthetic"},), "model", timeout)


class RequestBoundaryTests(unittest.IsolatedAsyncioTestCase):
    async def test_transport_queue_wait_is_inside_request_deadline(self):
        started = asyncio.Event()
        release = asyncio.Event()
        async def handler(_):
            started.set()
            await release.wait()
            return httpx.Response(200, json={"model": "model", "choices": [{"message": {"content": "ok"}}]})
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            transport = LLMTransport(base_url="https://model.invalid", api_key="synthetic", client=client,
                total_limit=1, lane_limits={task.value: 1 for task in LLMTask}, max_attempts=1)
            first = asyncio.create_task(transport.complete(request(1)))
            await started.wait()
            try:
                with self.assertRaises(GatewayTimeout):
                    await asyncio.wait_for(transport.complete(request()), 0.2)
            finally:
                release.set()
                await first
            self.assertFalse(transport._active_tasks)

    async def test_provider_queue_has_same_overall_deadline(self):
        started = asyncio.Event()
        release = asyncio.Event()
        class Resource:
            async def complete(self, req):
                started.set()
                await release.wait()
                return GatewayCompletion("ok", req.model)
            async def aclose(self, **kwargs):
                pass
        router = ProviderRouterTransport(primary=Resource(), economy=None, total_limit=1,
            lane_limits={task.value: 1 for task in LLMTask})
        first = asyncio.create_task(router.complete(request(1)))
        await started.wait()
        try:
            with self.assertRaises(GatewayTimeout):
                await asyncio.wait_for(router.complete(request()), 0.2)
        finally:
            release.set()
            await first

    async def test_full_admission_rejects_without_another_network_call(self):
        started, release = asyncio.Event(), asyncio.Event()
        calls = 0
        async def handler(_):
            nonlocal calls
            calls += 1
            started.set()
            await release.wait()
            return httpx.Response(200, json={"model": "model", "choices": [{"message": {"content": "ok"}}]})
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            transport = LLMTransport(base_url="https://model.invalid", api_key="synthetic", client=client,
                total_limit=1, max_pending=1, lane_limits={task.value: 1 for task in LLMTask})
            first = asyncio.create_task(transport.complete(request(1)))
            await started.wait()
            second = asyncio.create_task(transport.complete(request(1)))
            await asyncio.sleep(0)
            try:
                with self.assertRaises(GatewayRateLimitError):
                    await transport.complete(request())
                self.assertEqual(calls, 1)
            finally:
                release.set()
                await asyncio.gather(first, second)

    def test_chat_has_output_budget_without_changing_business_payload(self):
        self.assertGreater(request().to_payload()["max_tokens"], 0)
        self.assertNotIn("max_tokens", replace(request(), task=LLMTask.BUSINESS_INTENT).to_payload())


class SearchPrivacyTests(unittest.IsolatedAsyncioTestCase):
    def test_secrets_internal_addresses_and_personal_identifiers_never_become_queries(self):
        for text in ("查一下 密码=synthetic-secret", "查一下 api_key:opaque-value", "查一下 http://10.0.0.8/status",
                     "查一下 账户 user@example.invalid 的情况", "查一下 ssh 203.0.113.10 root", "查一下公司内部系统故障",
                     "查一下手机13800138000", "查一下 token 值 opaque", "查一下 https://service.local"):
            with self.subTest(text=text):
                self.assertIsNone(build_search_query(text, addressed=True, private=True))
        self.assertEqual(build_search_query("搜索 北京今天的天气", addressed=True, private=False), "北京今天的天气")

    async def test_client_is_final_privacy_gate_and_caches_public_query(self):
        calls = 0
        def handler(_):
            nonlocal calls
            calls += 1
            return httpx.Response(200, json={"results": []})
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
            client = TavilySearchClient(api_key="synthetic", client=http)
            with self.assertRaises(WebSearchError):
                await client.search("password=synthetic-value")
            self.assertEqual(calls, 0)
            await client.search("北京天气")
            await client.search("北京天气")
            self.assertEqual(calls, 1)

    async def test_search_deadline_applies_to_injected_client_and_budget_counts_retries(self):
        async def slow(_):
            await asyncio.sleep(1)
            return httpx.Response(200, json={"results": []})
        async with httpx.AsyncClient(transport=httpx.MockTransport(slow)) as http:
            client = TavilySearchClient(api_key="synthetic", client=http, timeout=0.01)
            with self.assertRaisesRegex(WebSearchError, "deadline_exceeded"):
                await client.search("公开天气")
            self.assertEqual(client._pending, 0)
        calls = 0
        def failed(_):
            nonlocal calls
            calls += 1
            return httpx.Response(503)
        async with httpx.AsyncClient(transport=httpx.MockTransport(failed)) as http:
            client = TavilySearchClient(api_key="synthetic", client=http, daily_request_limit=1)
            with self.assertRaisesRegex(WebSearchError, "budget_exhausted"):
                await client.search("公开天气")
            self.assertEqual(calls, 1)


class ChatTurnBudgetTests(unittest.IsolatedAsyncioTestCase):
    async def test_conversation_queue_is_bounded_and_cancelled_on_deadline(self):
        from plugins.random_chat.admission import run_chat_turn, _pending
        gate = asyncio.Event()
        entered = 0
        async def operation():
            nonlocal entered
            entered += 1
            await gate.wait()
        tasks = [asyncio.create_task(run_chat_turn("same", operation, timeout=0.03)) for _ in range(8)]
        await asyncio.sleep(0.01)
        self.assertEqual(_pending["same"], 4)
        self.assertEqual(entered, 4)
        await asyncio.gather(*tasks)
        self.assertNotIn("same", _pending)

    def test_legacy_prompt_obeys_budget_and_keeps_current_message(self):
        from plugins.random_chat.ai import _legacy_messages
        from plugins.chat_archive.db import ContextMessage
        result = _legacy_messages("current-marker", context=tuple(ContextMessage("name", "x" * 2000) for _ in range(50)),
            current=None, profiles=(), addressed=True, required_reply=False, chat_mode="private", persona="p" * 10000,
            web_search_data=("s" * 15000,))
        self.assertLessEqual(sum(len(str(item["content"])) for item in result), 12000)
        self.assertIn("current-marker", result[-1]["content"])


if __name__ == "__main__":
    unittest.main()
