from __future__ import annotations

import stat
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .store import ChatVisionStore


def _safe_root(root: Path) -> tuple[Path, Path] | None:
    root = Path(root)
    try:
        mode = root.lstat().st_mode
        root_resolved = root.resolve(strict=True)
    except (OSError, RuntimeError):
        return None
    if stat.S_ISLNK(mode) or not stat.S_ISDIR(mode):
        return None
    return root, root_resolved


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


async def cleanup_expired(store: ChatVisionStore, root: Path, *, now_text: str) -> None:
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
