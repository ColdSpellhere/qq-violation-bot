from __future__ import annotations

import asyncio
import unittest
from types import SimpleNamespace

import httpx


class WebSearchPolicyTests(unittest.TestCase):
    def test_only_addressed_or_private_chat_can_search(self):
        from plugins.web_search.policy import build_search_query

        self.assertEqual("DeepSeek 最新模型", build_search_query("帮我搜一下 DeepSeek 最新模型", addressed=True, private=False))
        self.assertEqual("今天北京天气怎么样", build_search_query("今天北京天气怎么样", addressed=False, private=True))
        self.assertIsNone(build_search_query("搜一下 DeepSeek 最新模型", addressed=False, private=False))
        self.assertIsNone(build_search_query("今天有点累", addressed=True, private=False))
        self.assertIsNone(build_search_query("目前我有点累", addressed=True, private=False))

    def test_query_is_current_text_only_and_bounded(self):
        from plugins.web_search.policy import build_search_query

        query = build_search_query("搜一下 " + "甲" * 400, addressed=True, private=False)
        self.assertEqual(200, len(query or ""))


class WebSearchClientTests(unittest.IsolatedAsyncioTestCase):
    async def test_fixed_endpoint_bearer_and_bounded_results(self):
        from plugins.web_search.client import TavilySearchClient

        seen = {}

        async def handler(request: httpx.Request):
            seen["url"] = str(request.url)
            seen["auth"] = request.headers.get("Authorization")
            return httpx.Response(200, json={"results": [
                {"title": f"t{i}", "url": f"https://example.com/{i}", "content": "x" * 100}
                for i in range(8)
            ]})

        client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        search = TavilySearchClient(api_key="secret", client=client, max_results=5, max_context_chars=500)
        result = await search.search("query")
        self.assertEqual("https://api.tavily.com/search", seen["url"])
        self.assertEqual("Bearer secret", seen["auth"])
        self.assertLessEqual(len(result.results), 5)
        self.assertLessEqual(sum(len(item.title) + len(item.url) + len(item.content) for item in result.results), 500)
        await client.aclose()

    async def test_cancellation_propagates(self):
        from plugins.web_search.client import TavilySearchClient

        async def handler(_request):
            raise asyncio.CancelledError

        client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        with self.assertRaises(asyncio.CancelledError):
            await TavilySearchClient(api_key="secret", client=client).search("query")
        await client.aclose()

    async def test_retries_one_retryable_server_failure(self):
        from plugins.web_search.client import TavilySearchClient

        calls = 0
        async def handler(_request):
            nonlocal calls
            calls += 1
            if calls == 1:
                return httpx.Response(503)
            return httpx.Response(200, json={"results": []})
        client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        result = await TavilySearchClient(api_key="secret", client=client).search("query")
        self.assertEqual(2, calls)
        self.assertEqual((), result.results)
        await client.aclose()


class MultiReplyContractTests(unittest.TestCase):
    def test_strict_json_and_plain_text_fallback(self):
        from plugins.random_chat.ai import parse_chat_replies

        self.assertEqual(("一", "二", "三"), parse_chat_replies('{"messages":["一","二","三"]}', max_messages=3))
        self.assertEqual(("一",), parse_chat_replies('{"messages":["一","二"]}', max_messages=1))
        self.assertEqual(("普通回复",), parse_chat_replies("普通回复", max_messages=3))
        self.assertEqual((), parse_chat_replies("SKIP", max_messages=3))
        self.assertEqual((), parse_chat_replies('{"messages":[]}', max_messages=3))
        self.assertEqual((), parse_chat_replies('{"messages":["重复","重复"]}', max_messages=3))

    def test_rejects_overlong_or_unknown_json(self):
        from plugins.random_chat.ai import parse_chat_replies

        self.assertEqual((), parse_chat_replies('{"messages":["' + "x" * 1201 + '"]}', max_messages=3))
        self.assertEqual((), parse_chat_replies('{"messages":["ok"],"extra":1}', max_messages=3))


if __name__ == "__main__":
    unittest.main()
