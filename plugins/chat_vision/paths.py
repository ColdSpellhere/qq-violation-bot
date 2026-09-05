from __future__ import annotations

import os
import stat
from pathlib import Path


def lexical_absolute(path: Path) -> Path:
    """Normalize dots without following filesystem links."""
    return Path(os.path.abspath(os.fspath(path)))


def _managed_chain(root: Path) -> tuple[Path, ...] | None:
    root = lexical_absolute(root)
    parts = root.parts
    indexes = [index for index, part in enumerate(parts) if part == "chat_vision"]
    if not indexes:
        return None
    chat_index = indexes[-1]
    if chat_index == 0:
        return None
    anchor = Path(*parts[:chat_index])
    return tuple(
        Path(*parts[: index + 1])
        for index in range(chat_index - 1, len(parts))
    ) or (anchor,)


def validate_existing_managed_root(root: Path, *, allow_missing: bool = False) -> Path | None:
    root = lexical_absolute(root)
    chain = _managed_chain(root)
    if chain is None:
        return None
    for component in chain:
        try:
            mode = component.lstat().st_mode
        except FileNotFoundError:
            if allow_missing:
                continue
            return None
        except (OSError, RuntimeError):
            return None
        if stat.S_ISLNK(mode) or not stat.S_ISDIR(mode):
            return None
    return root


def ensure_private_managed_root(root: Path) -> Path:
    root = lexical_absolute(root)
    chain = _managed_chain(root)
    if chain is None:
        raise ValueError("chat image root must be below chat_vision")
    for component in chain:
        try:
            mode = component.lstat().st_mode
        except FileNotFoundError:
            component.mkdir(mode=0o700)
            mode = component.lstat().st_mode
        except OSError as exc:
            raise ValueError("chat image root is unavailable") from exc
        if stat.S_ISLNK(mode) or not stat.S_ISDIR(mode):
            raise ValueError("chat image destination contains a symlink")
        os.chmod(component, 0o700)
    return root


def exact_configured_root(root: Path, configured_root: Path, *, allow_missing: bool = False) -> Path | None:
    root = lexical_absolute(root)
    if root != lexical_absolute(configured_root):
        return None
    return validate_existing_managed_root(root, allow_missing=allow_missing)
