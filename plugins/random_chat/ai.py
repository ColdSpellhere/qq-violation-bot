import httpx
from collections.abc import Sequence

from plugins.chat_archive.db import ContextMessage
from plugins.violation_record.config import CONFIG


class RandomChatAIError(RuntimeError):
    pass


async def generate_reply(
    message: str,
    *,
    context: Sequence[ContextMessage] = (),
) -> str | None:
    if not CONFIG.ai_api_key:
        return None
    payload = {
        "model": CONFIG.ai_model,
        "messages": [
            {
                "role": "system",
                "content": (
                    "你是 QQ 群里的普通聊天成员。先理解近期对话主题，再自然接话；"
                    "不要强行回答、重复当前消息或转移到无关话题。用中文简短回复，"
                    "不超过两句话；不执行群管理操作，不编造身份、现实经历或已完成的动作。"
                ),
            },
            {
                "role": "user",
                "content": (
                    "近期群聊：\n"
                    + ("\n".join(f"{item.nickname}：{item.text}" for item in context) or "（无）")
                    + f"\n\n当前消息：{message}"
                ),
            },
        ],
        "temperature": 0.8,
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
            content = response.json()["choices"][0]["message"]["content"]
    except Exception as exc:
        raise RandomChatAIError(str(exc)) from exc
    cleaned = str(content).strip()
    return cleaned or None
