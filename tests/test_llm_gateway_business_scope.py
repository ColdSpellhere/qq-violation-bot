"""Chat resource controls must preserve the existing business request contract."""
from __future__ import annotations

import asyncio
import json
import unittest

import httpx

from plugins.llm_gateway.contracts import GatewayCompletion, GatewayRequest, LLMTask
from plugins.llm_gateway.errors import GatewayRateLimitError
from plugins.llm_gateway.providers import ProviderRouterTransport
from plugins.llm_gateway.transport import LLMTransport


def _request(task: LLMTask, label: str, *, timeout: float) -> GatewayRequest:
    return GatewayRequest(
        task=task,
        messages=({"role": "user", "content": label},),
        model="synthetic-business-scope-model",
        timeout=timeout,
        temperature=0,
        response_format={"type": "json_object"},
    )


class BusinessScopeTests(unittest.IsolatedAsyncioTestCase):
    async def _run_with_full_chat_admission(self, transport, entered, release, business):
        """One chat runs and one queues, filling total_limit=1/max_pending=1."""
        running = asyncio.create_task(transport.complete(
            _request(LLMTask.CHAT_REPLY, "running-chat", timeout=2)
        ))
        calls = [running]
        try:
            await asyncio.wait_for(entered.wait(), 1)
            calls.append(asyncio.create_task(transport.complete(
                _request(LLMTask.CHAT_REPLY, "queued-chat", timeout=2)
            )))
            async def both_admitted():
                while len(transport._active_tasks) < 2:
                    await asyncio.sleep(0)
            await asyncio.wait_for(both_admitted(), 1)
            # Prove chat admission really is full, rather than weakening its cap.
            with self.assertRaises(GatewayRateLimitError):
                await transport.complete(_request(LLMTask.CHAT_REPLY, "overflow-chat", timeout=2))
            business_call = asyncio.create_task(transport.complete(business))
            calls.append(business_call)
            await asyncio.sleep(business.timeout * 3)
            self.assertFalse(
                business_call.done(),
                "business must remain queued beyond its per-request timeout when chat admission is full",
            )
            release.set()
            completion = await asyncio.wait_for(business_call, 1)
            await asyncio.wait_for(asyncio.gather(running, calls[1]), 1)
            self.assertEqual("business-answer", completion.content)
        finally:
            release.set()
            for call in calls:
                if not call.done():
                    call.cancel()
            await asyncio.gather(*calls, return_exceptions=True)

    async def test_transport_preserves_business_admission_and_per_attempt_timeout(self):
        entered, release = asyncio.Event(), asyncio.Event()
        business = _request(LLMTask.BUSINESS_INTENT, "business", timeout=0.02)
        original_payload = business.to_payload()
        business_http_calls = []

        async def handler(request: httpx.Request) -> httpx.Response:
            payload = json.loads(request.content)
            label = payload["messages"][0]["content"]
            if label == "running-chat":
                entered.set()
                await release.wait()
            if label == "business":
                business_http_calls.append((payload, request.extensions["timeout"]))
            return httpx.Response(200, json={
                "model": "synthetic-business-scope-model",
                "choices": [{"message": {"content": "business-answer"}}],
            })

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            transport = LLMTransport(
                base_url="https://provider.invalid/v1", api_key="synthetic-test-only",
                client=client, total_limit=1, max_pending=1, max_attempts=1,
                lane_limits={task.value: 1 for task in LLMTask},
            )
            try:
                await self._run_with_full_chat_admission(transport, entered, release, business)
                self.assertEqual([(original_payload, {
                    "connect": business.timeout, "read": business.timeout,
                    "write": business.timeout, "pool": business.timeout,
                })], business_http_calls)
                self.assertEqual(original_payload, business.to_payload())
                self.assertEqual(0.02, business.timeout)
            finally:
                await transport.aclose()

    async def test_router_preserves_business_admission_and_original_request(self):
        entered, release = asyncio.Event(), asyncio.Event()
        business = _request(LLMTask.BUSINESS_INTENT, "business", timeout=0.02)
        observed = []

        class Resource:
            async def complete(self, request):
                observed.append(request)
                if request.messages[0]["content"] == "running-chat":
                    entered.set()
                    await release.wait()
                return GatewayCompletion(
                    content="business-answer", model=request.model, latency_ms=0, retries=0
                )

            async def aclose(self, *, drain_timeout=10):
                return None

        router = ProviderRouterTransport(
            primary=Resource(), economy=None, total_limit=1, max_pending=1,
            lane_limits={task.value: 1 for task in LLMTask},
        )
        try:
            await self._run_with_full_chat_admission(router, entered, release, business)
            business_calls = [request for request in observed if request.task is LLMTask.BUSINESS_INTENT]
            self.assertEqual(1, len(business_calls))
            self.assertIs(business, business_calls[0])
            self.assertEqual(0.02, business_calls[0].timeout)
        finally:
            await router.aclose()
