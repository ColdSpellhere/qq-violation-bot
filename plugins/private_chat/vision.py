from __future__ import annotations

import logging
from dataclasses import dataclass

from nonebot.adapters.onebot.v11 import Message

from plugins.chat_vision.client import VisionImage, describe_image
from plugins.chat_vision.download import download_chat_image


logger = logging.getLogger(__name__)

_MAX_PRIVATE_IMAGES = 4


@dataclass(frozen=True)
class PrivateVisionResult:
    had_image: bool
    images: tuple[VisionImage, ...] = ()
    descriptions: tuple[str, ...] = ()


async def understand_private_images(
    message: Message,
    *,
    message_id: str,
    max_bytes: int,
    timeout: float,
    base_url: str,
    api_key: str,
    model: str,
) -> PrivateVisionResult:
    """Understand bounded private-message images without persisting raw bytes."""
    candidates: list[tuple[int, str]] = []
    image_ordinal = 0
    had_image = False
    for segment in message:
        if segment.type != "image":
            continue
        had_image = True
        if image_ordinal < _MAX_PRIVATE_IMAGES:
            url = segment.data.get("url")
            if isinstance(url, str) and url.strip():
                candidates.append((image_ordinal, url.strip()))
            else:
                logger.warning(
                    "private image URL missing: ordinal=%s", image_ordinal
                )
        image_ordinal += 1

    images: list[VisionImage] = []
    descriptions: list[str] = []
    for ordinal, url in candidates:
        try:
            downloaded = await download_chat_image(
                url,
                max_bytes=max_bytes,
                timeout=timeout,
            )
        except Exception as exc:
            logger.warning(
                "private image download failed: ordinal=%s error=%s",
                ordinal,
                type(exc).__name__,
            )
            continue

        images.append(
            VisionImage(
                content=downloaded.content,
                mime_type=downloaded.mime_type,
                message_id=message_id,
                ordinal=ordinal,
            )
        )
        try:
            description = await describe_image(
                downloaded.content,
                downloaded.mime_type,
                base_url=base_url,
                api_key=api_key,
                model=model,
                timeout=timeout,
            )
        except Exception as exc:
            logger.warning(
                "private image description failed: ordinal=%s error=%s",
                ordinal,
                type(exc).__name__,
            )
            continue
        normalized = description.strip()
        if normalized:
            descriptions.append(normalized)

    if sum(len(item.content) for item in images) > max_bytes:
        images.clear()
        logger.warning("private image raw payload exceeded total byte budget")

    return PrivateVisionResult(
        had_image=had_image,
        images=tuple(images),
        descriptions=tuple(descriptions),
    )
