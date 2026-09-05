from __future__ import annotations

import asyncio
import hashlib
import stat
from collections.abc import Awaitable, Callable
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import TYPE_CHECKING

from nonebot import logger
from nonebot.adapters.onebot.v11 import GroupMessageEvent

from plugins.feature_control.runtime import FEATURES
from plugins.violation_record.config import CONFIG

from .concurrency import VisionGateClosed, run_while_allowed, vision_slot
from .client import describe_image
from .download import download_chat_image, write_chat_image
from .paths import exact_configured_root, validate_existing_managed_root
from .store import ChatImageAsset, ChatVisionStore

if TYPE_CHECKING:
    from typing import Any


STORE: ChatVisionStore | None = None
_PROCESS_CONCURRENCY = 3
_MAX_IMAGES_PER_MESSAGE = 4
_MAX_PENDING_ASSETS = 256
_RETRY_BASE_SECONDS = 5.0
_WORKER_POLL_SECONDS = 1.0
_workers: list[asyncio.Task[None]] = []
_wake: asyncio.Event | None = None
_RECOVERY_BATCH_SIZE = 50
_LIVE_EVENT_FUTURE_SKEW_SECONDS = 5 * 60


def _now_timestamp() -> int:
    return int(datetime.now(timezone.utc).timestamp())


def live_event_time_allowed(event_time: int) -> bool:
    now = _now_timestamp()
    return (
        now - CONFIG.chat_vision_recovery_window_seconds
        <= event_time
        <= now + _LIVE_EVENT_FUTURE_SKEW_SECONDS
    )


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
    root = exact_configured_root(root, CONFIG.chat_vision_root, allow_missing=True)
    if root is None:
        return
    if not root.exists():
        for asset in store.expired(now_text):
            relative_path = Path(asset.relative_path or "")
            if not relative_path.is_absolute() and not any(part in {"", ".", ".."} for part in relative_path.parts):
                store.mark_deleted(asset.id, now_text)
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
            store.mark_deleted(asset.id, now_text)
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
        if ordinal > _MAX_IMAGES_PER_MESSAGE:
            break
        url = str(segment.data.get("url") or "").strip()
        if url:
            images.append((ordinal, url))
    return images


def _expiry_text(event_time: int) -> str:
    expires_at = datetime.fromtimestamp(event_time, timezone.utc) + timedelta(
        days=CONFIG.chat_vision_retention_days
    )
    return expires_at.strftime("%Y-%m-%d %H:%M:%S")


def _read_valid_stored_file(asset: ChatImageAsset) -> bytes | None:
    if (
        asset.relative_path is None
        or asset.mime_type is None
        or asset.byte_size is None
        or asset.sha256 is None
        or asset.byte_size > CONFIG.chat_vision_max_bytes
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
        if candidate.stat().st_size != asset.byte_size:
            return None
        with candidate.open("rb") as source:
            content = source.read(CONFIG.chat_vision_max_bytes + 1)
    except OSError:
        return None
    if len(content) != asset.byte_size:
        return None
    if hashlib.sha256(content).hexdigest() != asset.sha256:
        return None
    return content


async def _finish_claim(
    store: ChatVisionStore,
    asset: ChatImageAsset,
    *,
    use_gateway: bool,
) -> None:
    content = _read_valid_stored_file(asset)
    mime_type = asset.mime_type
    if content is None or mime_type is None:
        image = await download_chat_image(
            asset.source_url,
            max_bytes=CONFIG.chat_vision_max_bytes,
            timeout=CONFIG.chat_vision_timeout,
        )
        if not _allowed(asset.group_id):
            raise VisionGateClosed()
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
        allow_in_flight=True,
        use_gateway=use_gateway,
    )
    if not _allowed(asset.group_id):
        raise VisionGateClosed()
    store.mark_ready(asset.id, description)


def _allowed(group_id: int) -> bool:
    return (bool(getattr(CONFIG, "chat_vision_enabled", True))
        and FEATURES.image_understanding_allowed()
        and bool(getattr(FEATURES, "group_chat_allowed", lambda _group: True)(group_id)))


async def process_pending_asset(
    asset: ChatImageAsset, *, store: ChatVisionStore | None = None
) -> None:
    active_store = store or _active_store()
    if not _allowed(asset.group_id) or not live_event_time_allowed(asset.event_time):
        return
    gateway_allowed = getattr(FEATURES, "llm_gateway_allowed", lambda _domain: False)
    use_gateway = bool(gateway_allowed("vision"))
    # All entry points share the same slot, including explicit inline helpers.
    async def process() -> None:
        async with vision_slot():
            if not _allowed(asset.group_id):
                raise VisionGateClosed()
            await _finish_claim(active_store, claimed, use_gateway=use_gateway)
    claimed = active_store.claim(asset.id, CONFIG.chat_vision_max_retries)
    if claimed is None:
        return
    try:
        await run_while_allowed(process, allowed=lambda: _allowed(asset.group_id),
            timeout=2 * CONFIG.chat_vision_timeout)
    except (asyncio.CancelledError, VisionGateClosed) as exc:
        active_store.release_claim(asset.id)
        if isinstance(exc, asyncio.CancelledError):
            raise
    except Exception as exc:
        error_type = type(exc).__name__
        if getattr(exc, "retryable", True) is False:
            error_type = str(getattr(exc, "code", "") or error_type)
        try:
            active_store.mark_failed(asset.id, error_type,
                retry_delay=min(60, _RETRY_BASE_SECONDS * 2 ** max(0, claimed.attempts-1)))
        except Exception as mark_exc:
            logger.warning("群聊图片失败状态写入失败 "
                f"asset_id={asset.id} error={type(mark_exc).__name__}")
        logger.warning(f"群聊图片处理失败 asset_id={asset.id} error={error_type}")


async def enqueue_image_event(event: GroupMessageEvent) -> list[ChatImageAsset]:
    """Only persist bounded work; the message matcher never waits for a model."""
    store = _active_store()
    group_id, message_id = int(event.group_id), str(event.message_id)
    if not _allowed(group_id) or not live_event_time_allowed(int(event.time)):
        return store.for_message(group_id, message_id)
    for ordinal, source_url in _image_segments(event):
        try:
            asset = store.admit_pending(group_id, message_id, ordinal, source_url, int(event.time),
                max_pending=_MAX_PENDING_ASSETS, max_retries=CONFIG.chat_vision_max_retries,
                min_event_time=_now_timestamp()-CONFIG.chat_vision_recovery_window_seconds)
            if asset is None:
                logger.warning(f"群聊图片队列已满 group_id={group_id} message_id={message_id}")
                break
        except Exception as exc:
            logger.warning(f"群聊图片任务创建失败 group_id={group_id} message_id={message_id} "
                f"ordinal={ordinal} error={type(exc).__name__}")
    if _wake is not None:
        _wake.set()
    return store.for_message(group_id, message_id)


async def wait_for_message_assets(
    group_id: int, message_id: str, *, timeout: float = 8.0,
) -> list[ChatImageAsset]:
    """Wait only for this message, for at most 30 seconds; cancellation is local."""
    store = _active_store()
    deadline = asyncio.get_running_loop().time() + max(0.0, min(float(timeout), 30.0))
    if _wake is not None:
        _wake.set()
    while True:
        assets = store.for_message(int(group_id), str(message_id))
        unfinished = any(asset.status in {"pending", "processing", "failed"}
            and asset.attempts < CONFIG.chat_vision_max_retries
            and asset.error_type not in {"payment_required", "GatewayPaymentRequiredError"}
            for asset in assets)
        remaining = deadline - asyncio.get_running_loop().time()
        if not unfinished or remaining <= 0 or not _allowed(int(group_id)) or not _workers:
            return assets
        await asyncio.sleep(min(.05, remaining))


async def _worker_loop(store: ChatVisionStore) -> None:
    while True:
        try:
            if bool(getattr(CONFIG, "chat_vision_enabled", True)) and FEATURES.image_understanding_allowed():
                candidates = store.claimable(CONFIG.chat_vision_max_retries,
                    min_event_time=_now_timestamp()-CONFIG.chat_vision_recovery_window_seconds,
                    limit=_MAX_PENDING_ASSETS)
                asset = next((item for item in candidates if _allowed(item.group_id) and live_event_time_allowed(item.event_time)), None)
                if asset is not None:
                    await process_pending_asset(asset, store=store)
                    continue
        except Exception as exc:
            logger.warning(f"群聊图片工作单元失败 error={type(exc).__name__}")
        try:
            if _wake is not None:
                await asyncio.wait_for(_wake.wait(), timeout=_WORKER_POLL_SECONDS)
                _wake.clear()
            else:
                await asyncio.sleep(_WORKER_POLL_SECONDS)
        except asyncio.TimeoutError:
            pass


def start_workers(store: ChatVisionStore) -> None:
    global _wake
    if _workers:
        return
    set_store(store)
    store.recover_interrupted_claims()
    _wake = asyncio.Event()
    for index in range(_PROCESS_CONCURRENCY):
        _workers.append(asyncio.create_task(_worker_loop(store), name=f"chat-vision-worker-{index}"))


async def stop_workers() -> None:
    global _wake
    tasks = list(_workers)
    for task in tasks:
        task.cancel()
    if tasks:
        await asyncio.gather(*tasks, return_exceptions=True)
    _workers.clear()
    _wake = None


async def process_image_event(event: GroupMessageEvent) -> list[ChatImageAsset]:
    """Explicit inline helper for tools/tests; production ingestion uses enqueue only."""
    assets = await enqueue_image_event(event)
    await asyncio.gather(*(process_pending_asset(asset) for asset in assets))
    return _active_store().for_message(int(event.group_id), str(event.message_id))


async def recover_pending(
    store: ChatVisionStore,
    processor: Callable[[ChatImageAsset], Awaitable[Any]],
    *,
    max_retries: int,
    batch_size: int = _RECOVERY_BATCH_SIZE,
    min_event_time: int | None = None,
    max_assets: int | None = None,
) -> None:
    after_id = 0
    processed = 0
    while True:
        if max_assets is not None and processed >= max_assets:
            return
        limit = batch_size
        if max_assets is not None:
            limit = min(limit, max_assets - processed)
        assets = store.claimable(
            max_retries,
            after_id=after_id,
            limit=limit,
            min_event_time=min_event_time,
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
        processed += len(assets)
        after_id = max(asset.id for asset in assets)
