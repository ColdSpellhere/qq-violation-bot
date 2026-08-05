import httpx

from plugins.violation_record.config import CONFIG


class RandomChatAIError(RuntimeError):
    pass


async def generate_reply(message: str) -> str | None:
    if not CONFIG.ai_api_key:
        return None
    payload = {
        "model": CONFIG.ai_model,
        "messages": [
            {
                "role": "system",
                "content": (
                    "你在 QQ 群里自然聊天。用中文简短回复，不超过两句话；"
                    "不执行群管理操作，也不要声称自己做过现实动作。"
                ),
            },
            {"role": "user", "content": message},
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
