from __future__ import annotations

import hashlib
import ipaddress
import os
import re
import socket
import tempfile
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Callable
from urllib.parse import urlparse

import httpx


_IMAGE_EXTENSIONS = {
    "image/jpeg": "jpg",
    "image/png": "png",
    "image/gif": "gif",
    "image/webp": "webp",
}
_MESSAGE_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*\Z")


@dataclass(frozen=True)
class DownloadedChatImage:
    content: bytes
    mime_type: str
    extension: str


def _default_resolver(host: str) -> list[str]:
    return list({item[4][0] for item in socket.getaddrinfo(host, None)})


def _is_public_address(address: str) -> bool:
    ip = ipaddress.ip_address(address)
    return not (
        ip.is_private
        or ip.is_loopback
        or ip.is_link_local
        or ip.is_reserved
        or ip.is_multicast
        or ip.is_unspecified
    )


def _valid_signature(content: bytes, mime_type: str) -> bool:
    checks = {
        "image/jpeg": content.startswith(b"\xff\xd8\xff"),
        "image/png": content.startswith(b"\x89PNG\r\n\x1a\n"),
        "image/gif": content.startswith((b"GIF87a", b"GIF89a")),
        "image/webp": len(content) >= 12 and content[:4] == b"RIFF" and content[8:12] == b"WEBP",
    }
    return checks.get(mime_type, False)


async def download_chat_image(
    url: str,
    *,
    client: httpx.AsyncClient,
    max_bytes: int,
    resolver: Callable[[str], list[str]] = _default_resolver,
) -> DownloadedChatImage:
    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise ValueError("chat image URL must be HTTP(S)")
    if max_bytes <= 0:
        raise ValueError("chat image size limit must be positive")

    addresses = resolver(parsed.hostname)
    if not addresses or any(not _is_public_address(address) for address in addresses):
        raise ValueError("chat image URL resolves to a non-public address")

    async with client.stream("GET", url, follow_redirects=False) as response:
        if response.is_redirect:
            raise ValueError("chat image redirects are not allowed")
        response.raise_for_status()
        mime_type = response.headers.get("content-type", "").split(";", 1)[0].lower()
        extension = _IMAGE_EXTENSIONS.get(mime_type)
        if extension is None:
            raise ValueError("chat image payload is not a supported image")
        content = bytearray()
        async for chunk in response.aiter_bytes():
            content.extend(chunk)
            if len(content) > max_bytes:
                raise ValueError("chat image exceeds size limit")

    payload = bytes(content)
    if not _valid_signature(payload, mime_type):
        raise ValueError("chat image payload is not a supported image")
    return DownloadedChatImage(payload, mime_type, extension)


def _create_directory(path: Path, *, parents: bool = False) -> None:
    path.mkdir(parents=parents, exist_ok=True)
    if path.is_symlink() or not path.is_dir():
        raise ValueError("chat image destination contains a symlink")


def write_chat_image(
    root: Path,
    *,
    group_id: int,
    event_time: int,
    message_id: str,
    ordinal: int,
    image: DownloadedChatImage,
) -> tuple[str, str]:
    if group_id <= 0 or ordinal <= 0:
        raise ValueError("group_id and ordinal must be positive")
    if not _MESSAGE_ID.fullmatch(message_id):
        raise ValueError("message_id contains unsafe path characters")
    expected_extension = _IMAGE_EXTENSIONS.get(image.mime_type)
    if expected_extension is None or image.extension != expected_extension:
        raise ValueError("chat image has an unsupported MIME type")
    if not _valid_signature(image.content, image.mime_type):
        raise ValueError("chat image payload is not a supported image")

    root = Path(root)
    _create_directory(root, parents=True)
    root_resolved = root.resolve()
    date_text = datetime.fromtimestamp(event_time, UTC).date().isoformat()
    group_directory = root / str(group_id)
    _create_directory(group_directory)
    destination_directory = group_directory / date_text
    _create_directory(destination_directory)
    if not destination_directory.resolve().is_relative_to(root_resolved):
        raise ValueError("chat image destination escapes root")

    filename = f"{message_id}-{ordinal}.{image.extension}"
    destination = destination_directory / filename
    if destination.is_symlink():
        raise ValueError("chat image destination is a symlink")

    descriptor, temporary_name = tempfile.mkstemp(prefix=".chat-image-", dir=destination_directory)
    temporary_path = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as temporary_file:
            temporary_file.write(image.content)
            temporary_file.flush()
            os.fsync(temporary_file.fileno())
        os.chmod(temporary_path, 0o600)
        if destination.is_symlink():
            raise ValueError("chat image destination is a symlink")
        os.replace(temporary_path, destination)
    except Exception:
        temporary_path.unlink(missing_ok=True)
        raise

    relative_path = destination.relative_to(root).as_posix()
    return relative_path, hashlib.sha256(image.content).hexdigest()
