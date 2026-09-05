from __future__ import annotations

import json
from collections.abc import Sequence

import httpx

from plugins.chat_archive.db import ContextMessage
from plugins.feature_control.runtime import FEATURES
from plugins.llm_gateway import get_gateway
from plugins.llm_gateway.errors import GatewayError
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


class MemberMemoryError(RuntimeError):
    code = "member_memory_processing_error"
    retryable = True


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


async def generate_memory_summary(existing: str, facts: Sequence[MemoryTrait]) -> str | None:
    use_gateway, economy_mode = _request_policy()
    api_available = bool(
        getattr(CONFIG, "glm_api_key", "") if economy_mode else CONFIG.ai_api_key
    )
    if not api_available or not facts:
        return None
    try:
        content = await _complete(
            "summary",
            _summary_messages(existing, facts),
            use_gateway=use_gateway,
            economy_mode=economy_mode,
        )
        if not isinstance(content, str):
            return None
        text = content.strip()
    except (OSError, ValueError, KeyError, TypeError, httpx.HTTPError, GatewayError):
        return None
    return text if text and len(text) <= 300 else None
