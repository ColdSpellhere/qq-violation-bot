from __future__ import annotations

import asyncio
import json
from urllib.parse import urlparse

import httpx

from .models import SearchBundle, SearchResult


_ENDPOINT = "https://api.tavily.com/search"


class WebSearchError(RuntimeError):
    pass


class TavilySearchClient:
    def __init__(self, *, api_key: str, client: httpx.AsyncClient | None = None,
                 timeout: float = 8, max_results: int = 5, max_context_chars: int = 4000):
        if not api_key.strip():
            raise ValueError("Tavily API key is required")
        self._api_key = api_key.strip()
        self._client = client or httpx.AsyncClient(timeout=timeout, trust_env=False)
        self._owns_client = client is None
        self._max_results = min(5, max(1, int(max_results)))
        self._max_chars = min(8000, max(500, int(max_context_chars)))
        self._closed = False

    async def search(self, query: str) -> SearchBundle:
        raw = None
        for attempt in range(2):
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
            if not title or parsed.scheme not in {"http", "https"} or not parsed.netloc:
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
