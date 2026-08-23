from __future__ import annotations

import os
from pathlib import Path

from dotenv import load_dotenv


CODE_ROOT = Path(__file__).resolve().parents[1]


def _configured_instance_root() -> Path:
    raw = str(os.getenv("BOT_INSTANCE_ROOT") or "").strip()
    if not raw:
        return CODE_ROOT
    configured = Path(raw)
    if not configured.is_absolute():
        raise RuntimeError("BOT_INSTANCE_ROOT must be an absolute path")
    absolute = Path(os.path.abspath(configured))
    if absolute.is_symlink():
        raise RuntimeError("BOT_INSTANCE_ROOT must not be a symbolic link")
    return absolute


INSTANCE_ROOT = _configured_instance_root()
DATA_DIR = INSTANCE_ROOT / "data"
EXPORT_DIR = INSTANCE_ROOT / "exports"
BACKUP_DIR = INSTANCE_ROOT / "backups"
LOG_DIR = INSTANCE_ROOT / "logs"
CHARACTER_FILE = INSTANCE_ROOT / "character.md"


def load_instance_env() -> bool:
    return load_dotenv(INSTANCE_ROOT / ".env")
