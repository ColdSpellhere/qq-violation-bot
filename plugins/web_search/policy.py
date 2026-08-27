from __future__ import annotations

import re


_EXPLICIT = re.compile(r"(?:帮我)?(?:联网|上网)?(?:搜一下|搜索|查一下|查询一下|查查)\s*")
_TIME_SENSITIVE = re.compile(
    r"(?:最新|实时|刚刚发布|(?:目前|当前|现在).*(?:版本|价格|汇率|天气|新闻|赛程|比分|政策|规定)|"
    r"今天.*(?:天气|新闻)|价格|汇率|比分|赛程)"
)


def build_search_query(text: str, *, addressed: bool, private: bool) -> str | None:
    if not (addressed or private) or type(text) is not str:
        return None
    cleaned = " ".join(text.strip().split())
    if not cleaned:
        return None
    explicit = _EXPLICIT.search(cleaned)
    if explicit:
        cleaned = (cleaned[: explicit.start()] + cleaned[explicit.end() :]).strip()
    elif not _TIME_SENSITIVE.search(cleaned):
        return None
    return cleaned[:200] or None


__all__ = ["build_search_query"]
