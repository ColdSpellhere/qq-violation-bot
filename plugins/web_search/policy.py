from __future__ import annotations

import re
import ipaddress


_EXPLICIT = re.compile(r"(?:帮我)?(?:联网|上网)?(?:搜一下|搜索|查一下|查询一下|查查)\s*")
_TIME_SENSITIVE = re.compile(
    r"(?:最新|实时|刚刚发布|(?:目前|当前|现在).*(?:版本|价格|汇率|天气|新闻|赛程|比分|政策|规定)|"
    r"今天.*(?:天气|新闻)|价格|汇率|比分|赛程)"
)

# Search is an external disclosure boundary: refuse sensitive input before
# truncation, including when callers bypass build_search_query.
_SENSITIVE = re.compile(
    r"(?:password|passwd|api[_ -]?key|access[_ -]?token|secret|密码|口令|密钥)\s*[:=：]|"
    r"(?:token|令牌)\s*(?:值|[:=：])|\bBearer\s+\S+|-----BEGIN .*PRIVATE KEY|"
    r"\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}\b|(?<!\d)1[3-9]\d{9}(?!\d)|"
    r"(?:公司|企业|客户|组织)?内部(?:系统|网络|服务|文档|接口)|"
    r"\b(?:ssh|scp)\s+|\b(?:localhost|[\w.-]+\.(?:local|internal|lan))\b",
    re.IGNORECASE,
)
_IPV4 = re.compile(r"(?<![\d.])(?:\d{1,3}\.){3}\d{1,3}(?![\d.])")
_IPV6 = re.compile(r"\[([0-9a-fA-F:]+)\]")


def query_is_public(text: str) -> bool:
    if type(text) is not str or not text.strip() or _SENSITIVE.search(text):
        return False
    for candidate in [*_IPV4.findall(text), *_IPV6.findall(text)]:
        try:
            if not ipaddress.ip_address(candidate).is_global:
                return False
        except ValueError:
            continue
    return True


def build_search_query(text: str, *, addressed: bool, private: bool) -> str | None:
    if not (addressed or private) or not query_is_public(text):
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


__all__ = ["build_search_query", "query_is_public"]
