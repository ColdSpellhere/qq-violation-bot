from __future__ import annotations

import stat
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .store import ChatVisionStore


async def cleanup_expired(store: ChatVisionStore, root: Path, *, now_text: str) -> None:
    root = Path(root)
    root_resolved = root.resolve()
    for asset in store.expired(now_text):
        if asset.relative_path is None:
            continue
        relative_path = Path(asset.relative_path)
        if relative_path.is_absolute():
            continue
        candidate = root / relative_path
        if candidate.is_symlink():
            continue
        try:
            if not candidate.resolve().is_relative_to(root_resolved):
                continue
        except OSError:
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
