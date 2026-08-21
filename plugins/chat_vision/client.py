import base64
from dataclasses import dataclass

import httpx


class ChatVisionAIError(RuntimeError):
    pass


@dataclass(frozen=True)
class VisionImage:
    content: bytes
    mime_type: str
    message_id: str
    ordinal: int


def image_data_url(content: bytes, mime_type: str) -> str:
    return f"data:{mime_type};base64,{base64.b64encode(content).decode('ascii')}"


async def describe_image(
    content: bytes,
    mime_type: str,
    *,
    base_url: str,
    api_key: str,
    model: str,
    timeout: float,
) -> str:
    try:
        if not api_key:
            raise ValueError("missing API key")
        payload = {
            "model": model,
            "thinking": {"type": "disabled"},
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "text",
                            "text": (
                                "请用简洁、事实性的中文描述图片。包括可见主体、动作、场景、表情和"
                                "重要的可见文字（OCR）；不要臆测人物身份、图片外的事实或不可见内容。"
                            ),
                        },
                        {
                            "type": "image_url",
                            "image_url": {"url": image_data_url(content, mime_type)},
                        },
                    ],
                }
            ],
        }
        async with httpx.AsyncClient(timeout=timeout) as client:
            response = await client.post(
                f"{base_url.rstrip('/')}/v1/chat/completions",
                headers={
                    "Authorization": f"Bearer {api_key}",
                    "Content-Type": "application/json",
                },
                json=payload,
            )
            response.raise_for_status()
            result = response.json()
        description = result["choices"][0]["message"]["content"]
        if not isinstance(description, str):
            raise TypeError("response content must be text")
        description = description.strip()
        if not description:
            raise ValueError("empty response content")
        return description
    except Exception as exc:
        raise ChatVisionAIError(type(exc).__name__) from None
