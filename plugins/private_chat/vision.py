from __future__ import annotations

import asyncio
import logging
import math
from collections.abc import Callable
from dataclasses import dataclass

from nonebot.adapters.onebot.v11 import Message

from plugins.chat_vision.client import VisionImage, describe_image
from plugins.chat_vision.concurrency import VisionGateClosed, run_while_allowed, vision_slot
from plugins.chat_vision.download import ImageByteBudget, download_chat_image
from plugins.feature_control.runtime import FEATURES


logger = logging.getLogger(__name__)
_MAX_PRIVATE_IMAGES = 4


@dataclass(frozen=True)
class PrivateVisionResult:
    had_image: bool
    # Raw images are only a fallback for unsuccessful descriptions.
    images: tuple[VisionImage, ...] = ()
    descriptions: tuple[str, ...] = ()


async def understand_private_images(
    message: Message, *, message_id: str, max_bytes: int, timeout: float,
    base_url: str, api_key: str, model: str,
    still_allowed: Callable[[], bool] | None = None,
) -> PrivateVisionResult:
    """Share one byte budget/deadline; never interpret a successful image twice."""
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
                logger.warning("private image URL missing: ordinal=%s", image_ordinal)
        image_ordinal += 1

    if not candidates or max_bytes <= 0 or not math.isfinite(timeout) or timeout <= 0:
        return PrivateVisionResult(had_image)
    images: list[VisionImage] = []
    descriptions: list[str] = []
    budget = ImageByteBudget(max_bytes)
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout

    def allowed() -> bool:
        return FEATURES.image_understanding_allowed() and (still_allowed is None or still_allowed())

    async def process() -> None:
        async with vision_slot():
            for ordinal, url in candidates:
                if not allowed():
                    raise VisionGateClosed()
                remaining_time = deadline - loop.time()
                if budget.remaining <= 0 or remaining_time <= 0:
                    return
                before = budget.remaining
                try:
                    downloaded = await download_chat_image(url, max_bytes=before,
                        timeout=remaining_time, byte_budget=budget)
                    # Validate returned bytes too, for interchangeable download adapters.
                    if len(downloaded.content) > before:
                        budget.remaining = 0
                        raise ValueError("private image exceeds total byte budget")
                    if budget.remaining == before:
                        budget.consume(len(downloaded.content))
                except Exception as exc:
                    logger.warning("private image download failed: ordinal=%s error=%s", ordinal, type(exc).__name__)
                    continue
                if not allowed():
                    raise VisionGateClosed()
                raw = VisionImage(downloaded.content, downloaded.mime_type, message_id, ordinal)
                images.append(raw)
                remaining_time = deadline - loop.time()
                if remaining_time <= 0:
                    return
                try:
                    description = await describe_image(downloaded.content, downloaded.mime_type,
                        base_url=base_url, api_key=api_key, model=model, timeout=remaining_time)
                except Exception as exc:
                    logger.warning("private image description failed: ordinal=%s error=%s", ordinal, type(exc).__name__)
                    continue
                if not allowed():
                    raise VisionGateClosed()
                normalized = description.strip()
                if normalized:
                    descriptions.append(normalized)
                    images.remove(raw)

    try:
        await run_while_allowed(process, allowed=allowed, timeout=timeout)
    except VisionGateClosed:
        return PrivateVisionResult(had_image)
    except asyncio.TimeoutError:
        logger.warning("private image total deadline exceeded")
    return PrivateVisionResult(had_image=had_image, images=tuple(images), descriptions=tuple(descriptions))
