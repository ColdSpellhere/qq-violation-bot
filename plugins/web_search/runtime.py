from __future__ import annotations

import asyncio

from plugins.violation_record.config import CONFIG

from .client import TavilySearchClient


_client: TavilySearchClient | None = None
_lock = asyncio.Lock()


async def get_search_client() -> TavilySearchClient:
    global _client
    if _client is not None:
        return _client
    async with _lock:
        if _client is None:
            _client = TavilySearchClient(
                api_key=CONFIG.tavily_api_key,
                timeout=CONFIG.web_search_timeout,
                max_results=CONFIG.web_search_max_results,
                max_context_chars=CONFIG.web_search_max_context_chars,
            )
        return _client


async def close_search_client() -> None:
    global _client
    async with _lock:
        client, _client = _client, None
    if client is not None:
        await client.aclose()


try:
    from nonebot import get_driver
    get_driver().on_shutdown(close_search_client)
except (ImportError, ValueError):
    pass


__all__ = ["close_search_client", "get_search_client"]
