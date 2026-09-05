from __future__ import annotations

import re
import ipaddress
import unicodedata
from urllib.parse import unquote, urlparse


_EXPLICIT = re.compile(r"(?:帮我)?(?:联网|上网)?(?:搜一下|搜索|查一下|查询一下|查查)\s*")
_TIME_SENSITIVE = re.compile(
    r"(?:最新|实时|刚刚发布|(?:目前|当前|现在).*(?:版本|价格|汇率|天气|新闻|赛程|比分|政策|规定)|"
    r"今天.*(?:天气|新闻)|价格|汇率|比分|赛程)"
)

# Search is an external disclosure boundary: refuse sensitive input before
# truncation, including when callers bypass build_search_query.
_SENSITIVE = re.compile(
    r"(?:password|passwd|api[_ -]?key|access[_ -]?token|secret|密码|口令|密钥)[\"'’]?\s*[:=：]|"
    r"(?:token|令牌)[\"'’]?\s*(?:值|[:=：])|\bBearer\s+\S+|-----BEGIN .*PRIVATE KEY|"
    r"\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}\b|(?<!\d)1[3-9]\d{9}(?!\d)|"
    r"(?:公司|企业|客户|组织)?内部(?:系统|网络|服务|文档|接口)|"
    r"\b(?:ssh|scp)\s+|\b(?:localhost|[\w.-]+\.(?:local|internal|lan))\b",
    re.IGNORECASE,
)
_IPV4 = re.compile(r"(?<![\d.])(?:\d{1,3}\.){3}\d{1,3}(?![\d.])")
_IPV6 = re.compile(r"(?<![0-9A-Za-z:])(?:[0-9a-fA-F]{0,4}:){2,}[0-9a-fA-F:.]*(?:%[A-Za-z0-9_.-]+)?")
_ASSIGNED_SECRET = re.compile(
    r"(?:password|passwd|api[ _-]*key|access[ _-]*token|secret|token|密码|口令|密钥|令牌)"
    r"\s*(?:的?值?\s*(?:是|为)|(?:is|equals)\b)\s*(\S+)", re.I,
)
_BARE_SECRET = re.compile(
    r"\b(?:password|passwd|api[ _-]*key|access[ _-]*token|secret|token)\s+([A-Za-z0-9_./+=-]{6,})", re.I,
)
_EXPLANATORY = re.compile(r"(?:什么|如何|怎么|哪|否|what\b|how\b|why\b|which\b)", re.I)
_URL = re.compile(r"https?://[^\s<>]+", re.I)


def _normalized_privacy_text(text: str) -> str:
    # Check the entire source before truncation and before caching/network I/O.
    for _ in range(2):
        text = unicodedata.normalize("NFKC", unquote(text))
    return text


def _contains_disclosed_secret(text: str) -> bool:
    for match in _ASSIGNED_SECRET.finditer(text):
        value = match.group(1)
        if not _EXPLANATORY.match(value):
            return True
    for match in _BARE_SECRET.finditer(text):
        value = match.group(1)
        # Unlabelled natural-language discussion remains searchable. A value
        # containing credential-like separators, digits or case is not public.
        if any(char.isdigit() or char in "_./+=-" for char in value) or (
            len(value) >= 12 and any(char.isupper() for char in value)
            and any(char.islower() for char in value)
        ):
            return True
    for value in _URL.findall(text):
        try:
            parsed = urlparse(value)
        except ValueError:
            return True
        if parsed.username is not None or parsed.password is not None:
            return True
    return False


def query_is_public(text: str) -> bool:
    if type(text) is not str or not text.strip():
        return False
    text = _normalized_privacy_text(text)
    if _SENSITIVE.search(text) or _contains_disclosed_secret(text):
        return False
    for candidate in [*_IPV4.findall(text), *_IPV6.findall(text)]:
        try:
            if not ipaddress.ip_address(candidate.rstrip(".").split("%", 1)[0]).is_global:
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
