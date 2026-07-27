from __future__ import annotations

import ipaddress
import socket
from dataclasses import dataclass
from typing import Any, Callable
from urllib.parse import urlparse

import httpx
from nonebot import logger

from .config import CONFIG
from .evidence_store import EvidenceStore


@dataclass(frozen=True)
class DownloadedImage:
    content: bytes
    mime_type: str


def _default_resolver(host: str) -> list[str]:
    return list({item[4][0] for item in socket.getaddrinfo(host, None)})


def _valid_signature(content: bytes, mime_type: str) -> bool:
    checks = {
        "image/jpeg": content.startswith(b"\xff\xd8\xff"),
        "image/png": content.startswith(b"\x89PNG\r\n\x1a\n"),
        "image/gif": content.startswith((b"GIF87a", b"GIF89a")),
        "image/webp": len(content) >= 12 and content[:4] == b"RIFF" and content[8:12] == b"WEBP",
    }
    return checks.get(mime_type, False)


async def download_image(
    url: str,
    *,
    client: httpx.AsyncClient,
    resolver: Callable[[str], list[str]] = _default_resolver,
    max_bytes: int,
) -> DownloadedImage:
    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise ValueError("evidence URL must be HTTP(S)")
    for address in resolver(parsed.hostname):
        ip = ipaddress.ip_address(address)
        if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved:
            raise ValueError("evidence URL resolves to a non-public address")
    async with client.stream("GET", url, follow_redirects=False) as response:
        response.raise_for_status()
        mime_type = response.headers.get("content-type", "").split(";", 1)[0].lower()
        content = bytearray()
        async for chunk in response.aiter_bytes():
            content.extend(chunk)
            if len(content) > max_bytes:
                raise ValueError("evidence image exceeds size limit")
    payload = bytes(content)
    if not _valid_signature(payload, mime_type):
        raise ValueError("evidence payload is not a supported image")
    return DownloadedImage(payload, mime_type)


def _segment_type_data(segment: Any) -> tuple[str, dict[str, Any]]:
    if isinstance(segment, dict):
        return str(segment.get("type") or ""), dict(segment.get("data") or {})
    return str(getattr(segment, "type", "")), dict(getattr(segment, "data", {}) or {})


def _image_urls(message: Any) -> list[str]:
    urls: list[str] = []
    for segment in message or []:
        segment_type, data = _segment_type_data(segment)
        url = str(data.get("url") or "").strip()
        if segment_type == "image" and url.startswith(("http://", "https://")):
            urls.append(url)
    return urls


def _reply_message_id(event: Any) -> str | None:
    for segment in getattr(event, "message", []) or []:
        segment_type, data = _segment_type_data(segment)
        if segment_type == "reply":
            value = data.get("id") or data.get("message_id")
            return str(value) if value is not None else None
    return None


async def referenced_image_urls(bot: Any, event: Any) -> tuple[list[str], str | None]:
    reply = getattr(event, "reply", None)
    if reply is not None and getattr(reply, "message", None) is not None:
        source_id = str(getattr(reply, "message_id", "") or _reply_message_id(event) or "") or None
        return _image_urls(reply.message), source_id
    source_id = _reply_message_id(event)
    if not source_id:
        return [], None
    data = await bot.call_api("get_msg", message_id=source_id)
    if not isinstance(data, dict):
        return [], source_id
    return _image_urls(data.get("message") or []), source_id


async def capture_referenced_images(
    bot: Any,
    event: Any,
    store: EvidenceStore,
    *,
    operator_qq: str,
    command_message_id: str,
    client: httpx.AsyncClient | None = None,
    resolver: Callable[[str], list[str]] = _default_resolver,
) -> tuple[str | None, int]:
    urls, source_message_id = await referenced_image_urls(bot, event)
    if not urls or not source_message_id:
        return None, 0
    batch_id = store.create_batch(
        CONFIG.target_group_id,
        operator_qq,
        command_message_id,
    )
    owned_client = client is None
    active_client = client or httpx.AsyncClient(timeout=20.0)
    stored = 0
    try:
        for ordinal, url in enumerate(urls, 1):
            try:
                image = await download_image(
                    url,
                    client=active_client,
                    resolver=resolver,
                    max_bytes=CONFIG.evidence_max_bytes,
                )
                store.add_bytes(
                    batch_id,
                    image.content,
                    image.mime_type,
                    CONFIG.target_group_id,
                    source_message_id,
                    ordinal,
                )
                stored += 1
            except Exception as exc:
                logger.warning(
                    f"证据图片暂存失败 stage=download message_id={source_message_id} error={type(exc).__name__}"
                )
    finally:
        if owned_client:
            await active_client.aclose()
    if not stored:
        store.mark_batch(batch_id, "error")
        return None, 0
    return batch_id, stored
