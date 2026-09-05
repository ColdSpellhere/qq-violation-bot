from __future__ import annotations

import asyncio
import json
import time
from collections import OrderedDict
from datetime import datetime, timezone
from urllib.parse import urlparse

import httpx

from .models import SearchBundle, SearchResult
from .policy import query_is_public


_ENDPOINT = "https://api.tavily.com/search"


class WebSearchError(RuntimeError):
    pass


class TavilySearchClient:
    def __init__(self, *, api_key: str, client: httpx.AsyncClient | None = None,
                 timeout: float = 8, max_results: int = 5, max_context_chars: int = 4000,
                 daily_request_limit: int = 200, cache_ttl: float = 300):
        if not api_key.strip():
            raise ValueError("Tavily API key is required")
        self._api_key = api_key.strip()
        self._client = client or httpx.AsyncClient(timeout=timeout, trust_env=False)
        self._owns_client = client is None
        self._max_results = min(5, max(1, int(max_results)))
        self._max_chars = min(8000, max(500, int(max_context_chars)))
        self._closed = False
        self._timeout = max(0.01, float(timeout))
        self._cache_ttl = max(0, float(cache_ttl))
        self._cache: OrderedDict[str, tuple[float, SearchBundle]] = OrderedDict()
        self._gate = asyncio.Semaphore(2)
        self._pending = 0
        self._day = ""
        self._daily_requests = 0
        self._daily_limit = max(1, int(daily_request_limit))

    async def search(self, query: str) -> SearchBundle:
        if self._closed:
            raise WebSearchError("client_closed")
        if not query_is_public(query):
            raise WebSearchError("query_privacy_blocked")
        query = " ".join(query.split())[:200]
        cached = self._cached(query)
        if cached is not None:
            return cached
        if self._pending >= 10:
            raise WebSearchError("queue_full")
        self._pending += 1
        try:
            return await asyncio.wait_for(self._search_admitted(query), self._timeout)
        except asyncio.TimeoutError:
            raise WebSearchError("deadline_exceeded") from None
        finally:
            self._pending -= 1

    def _cached(self, query: str) -> SearchBundle | None:
        cached = self._cache.get(query)
        if cached and cached[0] > time.monotonic():
            self._cache.move_to_end(query)
            return cached[1]
        self._cache.pop(query, None)
        return None

    async def _search_admitted(self, query: str) -> SearchBundle:
        async with self._gate:
            cached = self._cached(query)
            if cached is not None:
                return cached
            bundle = await self._fetch(query)
            self._cache[query] = (time.monotonic() + self._cache_ttl, bundle)
            self._cache.move_to_end(query)
            while len(self._cache) > 128:
                self._cache.popitem(last=False)
            return bundle

    async def _fetch(self, query: str) -> SearchBundle:
        raw = None
        for attempt in range(2):
            day = datetime.now(timezone.utc).date().isoformat()
            if day != self._day:
                self._day, self._daily_requests = day, 0
            if self._daily_requests >= self._daily_limit:
                raise WebSearchError("process_daily_budget_exhausted")
            self._daily_requests += 1
            try:
                response = await self._client.post(
                    _ENDPOINT,
                    headers={"Authorization": f"Bearer {self._api_key}", "Content-Type": "application/json"},
                    json={"query": query[:200], "search_depth": "basic", "max_results": self._max_results,
                          "include_answer": False, "include_raw_content": False, "include_images": False},
                )
                response.raise_for_status()
                raw = response.json()
                break
            except asyncio.CancelledError:
                raise
            except httpx.HTTPStatusError as exc:
                if attempt == 0 and (exc.response.status_code == 429 or exc.response.status_code >= 500):
                    continue
                raise WebSearchError(type(exc).__name__) from None
            except (httpx.TransportError, httpx.TimeoutException) as exc:
                if attempt == 0:
                    continue
                raise WebSearchError(type(exc).__name__) from None
            except (json.JSONDecodeError, ValueError) as exc:
                raise WebSearchError(type(exc).__name__) from None
        items = raw.get("results") if isinstance(raw, dict) else None
        if not isinstance(items, list):
            raise WebSearchError("invalid_response")
        results: list[SearchResult] = []
        remaining = self._max_chars
        for item in items[: self._max_results]:
            if not isinstance(item, dict):
                continue
            title, url, content = (str(item.get(k) or "").strip() for k in ("title", "url", "content"))
            parsed = urlparse(url)
            if (not title or parsed.scheme not in {"http", "https"} or not parsed.netloc
                    or parsed.username or parsed.password or not query_is_public(url)):
                continue
            fixed = len(title) + len(url)
            if fixed >= remaining:
                break
            content = content[: remaining - fixed]
            results.append(SearchResult(title, url, content))
            remaining -= fixed + len(content)
        return SearchBundle(query=query[:200], results=tuple(results))

    async def aclose(self) -> None:
        if self._closed:
            return
        self._closed = True
        if self._owns_client:
            await self._client.aclose()


__all__ = ["TavilySearchClient", "WebSearchError"]
