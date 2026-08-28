import base64
from dataclasses import dataclass

import httpx

from plugins.feature_control.runtime import FEATURES
from plugins.llm_gateway import get_gateway
from plugins.llm_gateway.errors import GatewayPaymentRequiredError


class ChatVisionAIError(RuntimeError):
    def __init__(
        self,
        error_class: str,
        *,
        code: str | None = None,
        retryable: bool = True,
    ) -> None:
        super().__init__(error_class)
        self.code = code or error_class
        self.retryable = retryable


@dataclass(frozen=True)
class VisionImage:
    content: bytes
    mime_type: str
    message_id: str
    ordinal: int


def image_data_url(content: bytes, mime_type: str) -> str:
    return f"data:{mime_type};base64,{base64.b64encode(content).decode('ascii')}"


def _vision_messages(content: bytes, mime_type: str) -> tuple[dict[str, object], ...]:
    return (
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
        },
    )


async def _legacy_describe_image(
    messages: tuple[dict[str, object], ...],
    *,
    base_url: str,
    api_key: str,
    model: str,
    timeout: float,
) -> str:
    payload = {
        "model": model,
        "thinking": {"type": "disabled"},
        "messages": list(messages),
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
        if getattr(response, "status_code", None) == 402:
            raise GatewayPaymentRequiredError(status_code=402)
        response.raise_for_status()
        result = response.json()
    if not isinstance(result, dict):
        raise ValueError("response schema is invalid")
    choices = result.get("choices")
    if not isinstance(choices, list) or not choices:
        raise ValueError("response choices are missing")
    choice = choices[0]
    if not isinstance(choice, dict) or not isinstance(choice.get("message"), dict):
        raise ValueError("response message is missing")
    description = choice["message"].get("content")
    if not isinstance(description, str):
        raise TypeError("response content must be text")
    description = description.strip()
    if not description:
        raise ValueError("empty response content")
    return description


async def describe_image(
    content: bytes,
    mime_type: str,
    *,
    base_url: str,
    api_key: str,
    model: str,
    timeout: float,
    allow_in_flight: bool = False,
    use_gateway: bool | None = None,
) -> str:
    try:
        if type(allow_in_flight) is not bool:
            raise ValueError("allow_in_flight must be boolean")
        if allow_in_flight != (use_gateway is not None) or (
            use_gateway is not None and type(use_gateway) is not bool
        ):
            raise ValueError("in-flight provider policy is invalid")
        image_allowed = getattr(FEATURES, "image_understanding_allowed", lambda: True)
        if not allow_in_flight and not bool(image_allowed()):
            raise ChatVisionAIError(
                "EconomyModeEnabled",
                code="economy_mode",
                retryable=False,
            )
        if not api_key:
            raise ValueError("missing API key")
        messages = _vision_messages(content, mime_type)
        gateway_allowed = (
            use_gateway
            if use_gateway is not None
            else FEATURES.llm_gateway_allowed("vision")
        )
        if gateway_allowed:
            gateway = await get_gateway()
            description = await gateway.describe_image(
                messages,
                economy_mode=False if allow_in_flight else None,
            )
            description = description.strip()
            if not description:
                raise ValueError("empty response content")
            return description
        return await _legacy_describe_image(
            messages,
            base_url=base_url,
            api_key=api_key,
            model=model,
            timeout=timeout,
        )
    except ChatVisionAIError:
        raise
    except GatewayPaymentRequiredError as exc:
        raise ChatVisionAIError(
            type(exc).__name__,
            code="payment_required",
            retryable=False,
        ) from None
    except Exception as exc:
        raise ChatVisionAIError(type(exc).__name__) from None
