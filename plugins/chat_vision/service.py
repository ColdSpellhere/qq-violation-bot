from __future__ import annotations

import asyncio
import hashlib
import stat
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING

from nonebot import logger
from nonebot.adapters.onebot.v11 import GroupMessageEvent

from plugins.violation_record.config import CONFIG

from .client import describe_image
from .download import download_chat_image, write_chat_image
from .paths import exact_configured_root, validate_existing_managed_root
from .store import ChatImageAsset, ChatVisionStore

if TYPE_CHECKING:
    from typing import Any


STORE: ChatVisionStore | None = None
_PROCESS_CONCURRENCY = 3
_RECOVERY_BATCH_SIZE = 50


def set_store(store: ChatVisionStore) -> None:
    global STORE
    STORE = store


def _active_store() -> ChatVisionStore:
    if STORE is None:
        raise RuntimeError("chat vision store is not initialized")
    return STORE


def _safe_root(root: Path) -> tuple[Path, Path] | None:
    root = validate_existing_managed_root(root)
    if root is None:
        return None
    try:
        return root, root.resolve(strict=True)
    except (OSError, RuntimeError):
        return None


def _has_symlink_component(root: Path, relative_path: Path) -> bool:
    current = root
    for component in relative_path.parts:
        if component in {"", ".", ".."}:
            return True
        current /= component
        try:
            mode = current.lstat().st_mode
        except FileNotFoundError:
            return False
        except OSError:
            return True
        if stat.S_ISLNK(mode):
            return True
    return False


def _remove_written_file(relative_path_text: str) -> None:
    safe_root = _safe_root(CONFIG.chat_vision_root)
    if safe_root is None:
        return
    root, root_resolved = safe_root
    relative_path = Path(relative_path_text)
    if relative_path.is_absolute() or _has_symlink_component(root, relative_path):
        return
    candidate = root / relative_path
    try:
        mode = candidate.lstat().st_mode
        resolved = candidate.resolve(strict=True)
    except (OSError, RuntimeError):
        return
    if not stat.S_ISREG(mode) or not resolved.is_relative_to(root_resolved):
        return
    try:
        candidate.unlink()
    except OSError:
        return


async def cleanup_expired(store: ChatVisionStore, root: Path, *, now_text: str) -> None:
    root = exact_configured_root(root, CONFIG.chat_vision_root)
    if root is None:
        return
    safe_root = _safe_root(root)
    if safe_root is None:
        return
    root, root_resolved = safe_root
    for asset in store.expired(now_text):
        if asset.relative_path is None:
            continue
        relative_path = Path(asset.relative_path)
        if relative_path.is_absolute():
            continue
        if _has_symlink_component(root, relative_path):
            continue
        candidate = root / relative_path
        try:
            if not candidate.resolve().is_relative_to(root_resolved):
                continue
        except (OSError, RuntimeError):
            continue

        try:
            mode = candidate.lstat().st_mode
        except FileNotFoundError:
            continue
        except OSError:
            continue
        if not stat.S_ISREG(mode):
            continue

        try:
            candidate.unlink()
        except FileNotFoundError:
            pass
        except OSError:
            continue
        store.mark_deleted(asset.id, now_text)


def _image_segments(event: GroupMessageEvent) -> list[tuple[int, str]]:
    images: list[tuple[int, str]] = []
    ordinal = 0
    for segment in event.message:
        if segment.type != "image":
            continue
        ordinal += 1
        url = str(segment.data.get("url") or "").strip()
        if url:
            images.append((ordinal, url))
    return images


def _expiry_text(event_time: int) -> str:
    expires_at = datetime.fromtimestamp(event_time, UTC) + timedelta(
        days=CONFIG.chat_vision_retention_days
    )
    return expires_at.strftime("%Y-%m-%d %H:%M:%S")


def _read_valid_stored_file(asset: ChatImageAsset) -> bytes | None:
    if (
        asset.relative_path is None
        or asset.mime_type is None
        or asset.byte_size is None
        or asset.sha256 is None
    ):
        return None
    safe_root = _safe_root(CONFIG.chat_vision_root)
    if safe_root is None:
        return None
    root, root_resolved = safe_root
    relative_path = Path(asset.relative_path)
    if relative_path.is_absolute() or _has_symlink_component(root, relative_path):
        return None
    candidate = root / relative_path
    try:
        mode = candidate.lstat().st_mode
        resolved = candidate.resolve(strict=True)
    except (OSError, RuntimeError):
        return None
    if (
        not stat.S_ISREG(mode)
        or not resolved.is_relative_to(root_resolved)
    ):
        return None
    try:
        content = candidate.read_bytes()
    except OSError:
        return None
    if len(content) != asset.byte_size:
        return None
    if hashlib.sha256(content).hexdigest() != asset.sha256:
        return None
    return content


async def _finish_claim(store: ChatVisionStore, asset: ChatImageAsset) -> None:
    try:
        content = _read_valid_stored_file(asset)
        mime_type = asset.mime_type
        if content is None or mime_type is None:
            image = await download_chat_image(
                asset.source_url,
                max_bytes=CONFIG.chat_vision_max_bytes,
                timeout=CONFIG.chat_vision_timeout,
            )
            relative_path, digest = write_chat_image(
                CONFIG.chat_vision_root,
                group_id=asset.group_id,
                event_time=asset.event_time,
                message_id=asset.message_id,
                ordinal=asset.ordinal,
                image=image,
            )
            try:
                store.mark_downloaded(
                    asset.id,
                    relative_path,
                    image.mime_type,
                    len(image.content),
                    digest,
                    _expiry_text(asset.event_time),
                )
            except BaseException:
                _remove_written_file(relative_path)
                raise
            content = image.content
            mime_type = image.mime_type

        description = await describe_image(
            content,
            mime_type,
            base_url=CONFIG.ai_base_url,
            api_key=CONFIG.ai_api_key,
            model=CONFIG.chat_vision_model,
            timeout=CONFIG.chat_vision_timeout,
        )
        store.mark_ready(asset.id, description)
    except Exception as exc:
        error_type = type(exc).__name__
        try:
            store.mark_failed(asset.id, error_type)
        except Exception as mark_exc:
            logger.warning(
                "群聊图片失败状态写入失败 "
                f"group_id={asset.group_id} message_id={asset.message_id} "
                f"ordinal={asset.ordinal} error={type(mark_exc).__name__}"
            )
        logger.warning(
            "群聊图片处理失败 "
            f"group_id={asset.group_id} message_id={asset.message_id} "
            f"ordinal={asset.ordinal} error={error_type}"
        )


async def process_pending_asset(
    asset: ChatImageAsset, *, store: ChatVisionStore | None = None
) -> None:
    active_store = store or _active_store()
    claimed = active_store.claim(asset.id, CONFIG.chat_vision_max_retries)
    if claimed is not None:
        await _finish_claim(active_store, claimed)


async def process_image_event(event: GroupMessageEvent) -> list[ChatImageAsset]:
    """Create, claim, and process every image segment from this live event."""
    store = _active_store()
    group_id = int(event.group_id)
    message_id = str(event.message_id)
    pending: list[ChatImageAsset] = []
    for ordinal, source_url in _image_segments(event):
        try:
            asset = store.ensure_pending(
                group_id,
                message_id,
                ordinal,
                source_url,
                int(event.time),
            )
            pending.append(asset)
        except Exception as exc:
            logger.warning(
                "群聊图片任务创建失败 "
                f"group_id={group_id} message_id={message_id} ordinal={ordinal} "
                f"error={type(exc).__name__}"
            )
    semaphore = asyncio.Semaphore(_PROCESS_CONCURRENCY)

    async def process(asset: ChatImageAsset) -> None:
        async with semaphore:
            await process_pending_asset(asset, store=store)

    if pending:
        await asyncio.gather(*(process(asset) for asset in pending))
    return store.for_message(group_id, message_id)


async def recover_pending(
    store: ChatVisionStore,
    processor: Callable[[ChatImageAsset], Awaitable[Any]],
    *,
    max_retries: int,
    batch_size: int = _RECOVERY_BATCH_SIZE,
) -> None:
    after_id = 0
    while True:
        assets = store.claimable(
            max_retries,
            after_id=after_id,
            limit=batch_size,
        )
        if not assets:
            return
        semaphore = asyncio.Semaphore(_PROCESS_CONCURRENCY)

        async def process(asset: ChatImageAsset) -> None:
            async with semaphore:
                try:
                    await processor(asset)
                except Exception as exc:
                    logger.warning(
                        "群聊图片恢复失败 "
                        f"group_id={asset.group_id} message_id={asset.message_id} "
                        f"ordinal={asset.ordinal} error={type(exc).__name__}"
                    )

        await asyncio.gather(*(process(asset) for asset in assets))
        after_id = max(asset.id for asset in assets)
