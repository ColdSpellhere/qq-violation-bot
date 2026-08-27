from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class SearchResult:
    title: str
    url: str
    content: str


@dataclass(frozen=True)
class SearchBundle:
    query: str
    results: tuple[SearchResult, ...] = ()
    failed: bool = False


__all__ = ["SearchBundle", "SearchResult"]
