from __future__ import annotations

import asyncio
import json
from collections.abc import Callable, Sequence

import httpx

from plugins.chat_archive.db import ContextMessage
from plugins.feature_control.runtime import FEATURES
from plugins.llm_gateway import get_gateway
from plugins.llm_gateway.errors import (
    GatewayError, GatewayAuthenticationError, GatewayClientError,
    GatewayConfigurationError, GatewayEmptyContentError, GatewayContractError,
    GatewayPaymentRequiredError, GatewayRateLimitError, GatewayServerError,
    GatewayTimeout, GatewayTransportError,
)
from .errors import MemberMemoryError, MemberSummaryError
from .safety import contains_secret
from plugins.member_memory.store import MemoryTrait
from plugins.violation_record.config import CONFIG


def _extraction_messages(
    context: Sequence[ContextMessage],
) -> tuple[dict[str, object], dict[str, object]]:
    messages = "\n".join(
        f"[{item.message_id}] {item.nickname}[QQ:{item.user_id}]：{item.text}" for item in context
    )
    return (
        {
            "role": "system",
            "content": (
                "你负责保守地提取QQ群成员长期记忆。只记录说话者本人明确表达、未来仍可能有用的"
                "稳定爱好、习惯、偏好或非敏感背景。不要记录别人对他的评价、玩笑、推测、临时情绪，"
                "不要记录敏感信息。若不确定就不记录。只输出JSON对象："
                '{"memories":[{"user_id":"QQ号","trait":"简短特性",'
                '"evidence_message_id":"消息ID","quote":"原文中的连续短句"}]}。'
                "quote必须逐字来自该成员对应的证据消息。"
            ),
        },
        {"role": "user", "content": messages},
    )


def _summary_messages(
    existing: str, facts: Sequence[MemoryTrait]
) -> tuple[dict[str, object], dict[str, object]]:
    return (
        {
            "role": "system",
            "content": (
                "将已有摘要和新增的本人记忆合并为不超过300字的中文摘要。"
                "目标为180～220个字符，总字符数必须≤300；汉字、字母、数字、标点和空白"
                "（包括空格、换行）都计入字符数。不要添加首尾空白。"
                "只保留输入中的明确非敏感事实，不推测、不扩写、不评价，只输出摘要正文。"
            ),
        },
        {
            "role": "user",
            "content": "已有摘要：\n" + (existing or "（无）") + "\n新增记忆：\n"
            + "\n".join(f"- {item.text}" for item in facts),
        },
    )


async def _legacy_complete(messages: tuple[dict[str, object], ...]) -> object:
    payload = {
        "model": CONFIG.ai_model,
        "messages": list(messages),
        "temperature": 0.1,
    }
    async with httpx.AsyncClient(timeout=CONFIG.ai_timeout) as client:
        response = await client.post(
            f"{CONFIG.ai_base_url}/v1/chat/completions",
            headers={
                "Authorization": f"Bearer {CONFIG.ai_api_key}",
                "Content-Type": "application/json",
            },
            json=payload,
        )
        response.raise_for_status()
        return response.json()["choices"][0]["message"]["content"]


async def _complete(
    task: str,
    messages: tuple[dict[str, object], ...],
    *,
    use_gateway: bool,
    economy_mode: bool,
) -> object:
    if not use_gateway:
        return await _legacy_complete(messages)
    gateway = await get_gateway()
    if task == "extract":
        return await gateway.extract_member_memories(
            messages, economy_mode=economy_mode
        )
    if task == "summary":
        return await gateway.summarize_member_memory(
            messages, economy_mode=economy_mode
        )
    raise ValueError("unknown member memory task")


def _request_policy() -> tuple[bool, bool]:
    state = FEATURES.snapshot()
    use_gateway = bool(
        getattr(state, "llm_gateway_enabled", False)
        and getattr(state, "llm_gateway_member_memory_enabled", False)
    )
    return use_gateway, False


async def extract_memory_candidates(context: Sequence[ContextMessage], *, strict: bool = False) -> list[dict[str, object]]:
    use_gateway, economy_mode = _request_policy()
    api_available = bool(
        getattr(CONFIG, "glm_api_key", "") if economy_mode else CONFIG.ai_api_key
    )
    if not context:
        return []
    if not api_available:
        if strict:
            raise MemberMemoryError("member memory model unavailable")
        return []
    try:
        content = str(
            await _complete(
                "extract",
                _extraction_messages(context),
                use_gateway=use_gateway,
                economy_mode=economy_mode,
            )
        ).strip()
        if content.startswith("```"):
            content = content.strip("`").removeprefix("json").strip()
        parsed = json.loads(content)
        if strict and (not isinstance(parsed, dict) or not isinstance(parsed.get("memories"), list)):
            raise ValueError("invalid member memory response contract")
        memories = parsed.get("memories", []) if isinstance(parsed, dict) else []
        if strict and (len(memories) > 20 or any(not isinstance(item, dict) for item in memories)):
            raise ValueError("invalid member memory candidate count or type")
        return [item for item in memories if isinstance(item, dict)]
    except (OSError, ValueError, KeyError, TypeError, httpx.HTTPError, GatewayError) as exc:
        if strict:
            raise MemberMemoryError("member memory request failed") from exc
        return []


def _summary_request_error(error: Exception) -> MemberSummaryError:
    if isinstance(error, (GatewayTimeout, httpx.TimeoutException)):
        code = "request_timeout"
    elif isinstance(error, GatewayConfigurationError):
        code = "configuration_error"
    elif isinstance(error, GatewayPaymentRequiredError):
        code = "payment_required"
    elif isinstance(error, GatewayAuthenticationError):
        code = "auth_error"
    elif isinstance(error, GatewayRateLimitError):
        code = "rate_limited"
    elif isinstance(error, GatewayServerError):
        code = "server_error"
    elif isinstance(error, GatewayEmptyContentError):
        code = "empty_response"
    elif isinstance(error, (GatewayContractError, ValueError, KeyError, TypeError, IndexError)):
        code = "invalid_response"
    elif isinstance(error, httpx.HTTPStatusError):
        status = error.response.status_code
        code = ({401: "auth_error", 403: "auth_error", 402: "payment_required",
                 408: "request_timeout", 429: "rate_limited"}.get(status)
                or ("server_error" if status >= 500 else "client_error"))
    elif isinstance(error, (GatewayTransportError, httpx.TransportError, OSError)):
        code = "transport_error"
    elif isinstance(error, (GatewayClientError, GatewayError, httpx.HTTPError)):
        code = "client_error"
    else:
        code = "generation_failed"
    return MemberSummaryError("member_summary_" + code)


async def generate_memory_summary(
    existing: str, facts: Sequence[MemoryTrait], *, strict: bool = False,
    still_allowed: Callable[[], bool] | None = None,
) -> str | None:
    use_gateway, economy_mode = _request_policy()
    api_available = bool(
        getattr(CONFIG, "glm_api_key", "") if economy_mode else CONFIG.ai_api_key
    )
    if not facts:
        return None
    if not api_available:
        if strict:
            raise MemberSummaryError("member_summary_configuration_error")
        return None
    # Snapshot the original inputs once. A correction never includes rejected output.
    original_messages = _summary_messages(existing, facts)
    messages = original_messages
    for attempt in range(2):
        if still_allowed is not None and not still_allowed():
            return None
        try:
            content = await _complete(
                "summary", messages,
                use_gateway=use_gateway, economy_mode=economy_mode,
            )
        except (OSError, ValueError, KeyError, TypeError, IndexError, httpx.HTTPError, GatewayError) as exc:
            if strict:
                raise _summary_request_error(exc) from None
            return None
        if not isinstance(content, str):
            rejection = "member_summary_invalid_response"
        else:
            text = content.strip()
            if not text:
                rejection = "member_summary_empty_response"
            elif len(text) <= 300:
                return text  # The existing commit boundary also checks secrets and CAS.
            elif contains_secret(text):
                rejection = "member_summary_secret_blocked"
            else:
                rejection = "member_summary_too_long"
                if attempt == 0:
                    messages = (
                        {**original_messages[0], "content": str(original_messages[0]["content"])
                         + "上一次生成超过字符上限。请仅根据下方原始输入重新合并、精简措辞，"
                         "优先保留重要的明确事实，控制在180～220个字符，绝不能超过300个字符。"},
                        original_messages[1],
                    )
                    del content, text
                    # Honor pending cancellation before spending the single correction.
                    await asyncio.sleep(0)
                    continue
        break
    if strict:
        raise MemberSummaryError(rejection)
    return None
