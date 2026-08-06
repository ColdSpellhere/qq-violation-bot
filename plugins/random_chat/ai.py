import httpx
from collections.abc import Sequence

from plugins.chat_archive.db import ContextMessage
from plugins.violation_record.config import CONFIG


class RandomChatAIError(RuntimeError):
    pass


def _clean_reply(content: object) -> str | None:
    cleaned = str(content).strip()
    if not cleaned or cleaned.casefold() == "skip":
        return None
    if cleaned.startswith(("哈哈，", "哈哈,")):
        return None
    return cleaned


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
                    "你正在参与一个真实的 QQ 群聊。阅读最近的聊天记录，只写机器人此刻最自然的一条群消息。\n"
                    "先判断普通群成员现在会不会接话：没有自然接话点、话题已经结束或只能重复别人时，"
                    "输出且只输出 SKIP；有自然接话点才回复。\n"
                    "接住最近正在聊的具体内容，不要泛泛评价；像熟悉的群成员随手发消息，"
                    "不像客服、助手或主持人。通常只写一句，允许短句、省略和口语，不强求完整语法。\n"
                    "可以有态度、疑问或轻微调侃，但不要强行搞笑。直接说内容，不寒暄、不总结、"
                    "不解释为何回复。不固定使用“哈哈”“确实”“听起来”“感觉”“原来如此”等开场，"
                    "也不要为了像人而刻意添加语气词。不要复述上一条消息或换个说法重复。\n"
                    "不执行群管理操作，不编造身份、现实经历、群内事实或已完成的动作。"
                    "只输出最终群消息或 SKIP，不输出分析、引号、昵称前缀。"
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
    return _clean_reply(content)
