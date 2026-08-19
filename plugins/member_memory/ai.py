from __future__ import annotations

import json
from collections.abc import Sequence

import httpx

from plugins.chat_archive.db import ContextMessage
from plugins.member_memory.store import MemoryTrait
from plugins.violation_record.config import CONFIG


async def extract_memory_candidates(context: Sequence[ContextMessage]) -> list[dict[str, object]]:
    if not CONFIG.ai_api_key or not context:
        return []
    messages = "\n".join(
        f"[{item.message_id}] {item.nickname}[QQ:{item.user_id}]：{item.text}" for item in context
    )
    payload = {
        "model": CONFIG.ai_model,
        "messages": [
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
        ],
        "temperature": 0.1,
    }
    try:
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
            content = str(response.json()["choices"][0]["message"]["content"]).strip()
        if content.startswith("```"):
            content = content.strip("`").removeprefix("json").strip()
        parsed = json.loads(content)
        memories = parsed.get("memories", []) if isinstance(parsed, dict) else []
        return [item for item in memories if isinstance(item, dict)]
    except (OSError, ValueError, KeyError, TypeError, httpx.HTTPError):
        return []


async def generate_memory_summary(existing: str, facts: Sequence[MemoryTrait]) -> str | None:
    if not CONFIG.ai_api_key or not facts:
        return None
    payload = {
        "model": CONFIG.ai_model,
        "messages": [
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
        ],
        "temperature": 0.1,
    }
    try:
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
            text = str(response.json()["choices"][0]["message"]["content"]).strip()
    except (OSError, ValueError, KeyError, TypeError, httpx.HTTPError):
        return None
    return text if text and len(text) <= 300 else None
