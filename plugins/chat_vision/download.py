from __future__ import annotations

import asyncio
import math
import hashlib
import ipaddress
import os
import re
import socket
import ssl
import tempfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urlparse

import httpcore

from .paths import ensure_private_managed_root


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


@dataclass
class ImageByteBudget:
    remaining: int

    def consume(self, amount: int) -> None:
        if amount > self.remaining:
            self.remaining = 0
            raise ValueError("chat image exceeds total byte budget")
        self.remaining -= amount


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


class PinnedNetworkBackend(httpcore.AsyncNetworkBackend):
    """Connect only to the public addresses approved before the request."""

    def __init__(
        self,
        expected_host: str,
        addresses: tuple[str, ...],
        *,
        backend: httpcore.AsyncNetworkBackend | Any | None = None,
    ) -> None:
        self.expected_host = expected_host.casefold()
        self.addresses = addresses
        self.backend = backend or httpcore.AnyIOBackend()

    async def connect_tcp(
        self,
        host: str,
        port: int,
        timeout: float | None = None,
        local_address: str | None = None,
        socket_options=None,
    ):
        if host.casefold() != self.expected_host:
            raise ValueError("chat image connection host changed")
        last_error: Exception | None = None
        for address in self.addresses:
            try:
                stream = await self.backend.connect_tcp(
                    address,
                    port,
                    timeout=timeout,
                    local_address=local_address,
                    socket_options=socket_options,
                )
            except Exception as exc:
                last_error = exc
                continue
            peer = stream.get_extra_info("server_addr")
            peer_address = str(peer[0]) if isinstance(peer, tuple) and peer else ""
            if (
                not peer_address
                or not _is_public_address(peer_address)
                or ipaddress.ip_address(peer_address) != ipaddress.ip_address(address)
            ):
                await stream.aclose()
                raise ValueError("chat image connection peer was not the pinned public address")
            return stream
        if last_error is not None:
            raise last_error
        raise ValueError("chat image URL has no usable public address")

    async def connect_unix_socket(self, *args, **kwargs):
        raise ValueError("chat image Unix sockets are not allowed")

    async def sleep(self, seconds: float) -> None:
        await self.backend.sleep(seconds)


async def download_chat_image(
    url: str, *, max_bytes: int, timeout: float,
    resolver: Callable[[str], list[str]] = _default_resolver,
    network_backend: httpcore.AsyncNetworkBackend | Any | None = None,
    byte_budget: ImageByteBudget | None = None,
) -> DownloadedChatImage:
    """Apply one total deadline, including DNS, to a pinned HTTP download."""
    if not math.isfinite(timeout) or timeout <= 0:
        raise ValueError("chat image timeout must be positive")
    try:
        return await asyncio.wait_for(_download_chat_image(url, max_bytes=max_bytes, timeout=timeout,
            resolver=resolver, network_backend=network_backend, byte_budget=byte_budget), timeout=timeout)
    except asyncio.TimeoutError:
        raise ValueError("chat image request timed out") from None


async def _download_chat_image(
    url: str,
    *,
    max_bytes: int,
    timeout: float,
    resolver: Callable[[str], list[str]] = _default_resolver,
    network_backend: httpcore.AsyncNetworkBackend | Any | None = None,
    byte_budget: ImageByteBudget | None = None,
) -> DownloadedChatImage:
    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise ValueError("chat image URL must be HTTP(S)")
    if max_bytes <= 0:
        raise ValueError("chat image size limit must be positive")

    addresses = tuple(dict.fromkeys(await asyncio.to_thread(resolver, parsed.hostname)))
    if not addresses or any(not _is_public_address(address) for address in addresses):
        raise ValueError("chat image URL resolves to a non-public address")

    pinned_backend = PinnedNetworkBackend(
        parsed.hostname,
        addresses,
        backend=network_backend,
    )
    pool = httpcore.AsyncConnectionPool(
        ssl_context=ssl.create_default_context(),
        max_connections=1,
        max_keepalive_connections=0,
        http1=True,
        http2=False,
        retries=0,
        network_backend=pinned_backend,
    )
    default_port = 443 if parsed.scheme == "https" else 80
    host_header = parsed.hostname
    if parsed.port is not None and parsed.port != default_port:
        host_header = f"{host_header}:{parsed.port}"
    request_timeout = {
        "connect": timeout,
        "read": timeout,
        "write": timeout,
        "pool": timeout,
    }
    try:
        async with pool:
            async with pool.stream(
                "GET",
                url,
                headers=[(b"Host", host_header.encode("ascii")), (b"Accept", b"image/*")],
                extensions={"timeout": request_timeout},
            ) as response:
                if 300 <= response.status < 400:
                    raise ValueError("chat image redirects are not allowed")
                if not 200 <= response.status < 300:
                    raise ValueError("chat image HTTP status error")
                headers = {
                    key.decode("latin-1").casefold(): value.decode("latin-1")
                    for key, value in response.headers
                }
                mime_type = headers.get("content-type", "").split(";", 1)[0].lower()
                extension = _IMAGE_EXTENSIONS.get(mime_type)
                if extension is None:
                    raise ValueError("chat image payload is not a supported image")
                declared_size = headers.get("content-length")
                if declared_size is not None and (not declared_size.isdigit() or int(declared_size) > max_bytes):
                    raise ValueError("chat image exceeds size limit")
                content = bytearray()
                async for chunk in response.aiter_stream():
                    if byte_budget is not None:
                        byte_budget.consume(len(chunk))
                    if len(content) + len(chunk) > max_bytes:
                        raise ValueError("chat image exceeds size limit")
                    content.extend(chunk)
    except ValueError:
        raise
    except (httpcore.NetworkError, httpcore.TimeoutException, httpcore.ProtocolError):
        raise ValueError("chat image request failed") from None
    except Exception:
        raise ValueError("chat image request failed") from None

    payload = bytes(content)
    if not _valid_signature(payload, mime_type):
        raise ValueError("chat image payload is not a supported image")
    return DownloadedChatImage(payload, mime_type, extension)


def _create_directory(path: Path) -> None:
    path.mkdir(mode=0o700, exist_ok=True)
    if path.is_symlink() or not path.is_dir():
        raise ValueError("chat image destination contains a symlink")
    os.chmod(path, 0o700)


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

    root = ensure_private_managed_root(Path(root))
    root_resolved = root.resolve()
    date_text = datetime.fromtimestamp(event_time, timezone.utc).date().isoformat()
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
