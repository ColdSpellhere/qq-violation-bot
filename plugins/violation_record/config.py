import json
import os
from dataclasses import dataclass
from pathlib import Path


BASE_DIR = Path(__file__).resolve().parents[2]
DATA_DIR = BASE_DIR / "data"
EXPORT_DIR = BASE_DIR / "exports"
BACKUP_DIR = BASE_DIR / "backups"
LOG_DIR = BASE_DIR / "logs"


def _int_env(name: str, default: int) -> int:
    value = os.getenv(name)
    if not value:
        return default
    try:
        return int(value)
    except ValueError:
        return default


def _bool_env(name: str, default: bool = False) -> bool:
    raw = str(os.getenv(name) or "").strip().lower()
    if not raw:
        return default
    return raw in {"1", "true", "yes", "on"}


def _target_group_id_env() -> int:
    raw = str(os.getenv("TARGET_GROUP_ID") or "").strip()
    if not raw.isdigit():
        raise RuntimeError("TARGET_GROUP_ID must be one numeric QQ group ID")
    group_id = int(raw)
    if group_id <= 0:
        raise RuntimeError("TARGET_GROUP_ID must be a positive QQ group ID")
    return group_id


def _database_path(url: str) -> Path:
    if url.startswith("sqlite:///"):
        return Path(url.removeprefix("sqlite:///"))
    if url.startswith("sqlite://"):
        return Path(url.removeprefix("sqlite://"))
    return DATA_DIR / "violation_records.db"


_TARGET_GROUP_ID = _target_group_id_env()


@dataclass(frozen=True)
class AppConfig:
    allowed_group_ids: tuple[int, ...] = (_TARGET_GROUP_ID,)
    target_group_id: int = _TARGET_GROUP_ID
    bot_self_id: str = os.getenv("BOT_SELF_ID", "")
    napcat_access_token: str = os.getenv("NAPCAT_ACCESS_TOKEN", "")
    database_url: str = os.getenv("DATABASE_URL", f"sqlite:///{DATA_DIR / 'violation_records.db'}")
    database_path: Path = _database_path(os.getenv("DATABASE_URL", f"sqlite:///{DATA_DIR / 'violation_records.db'}"))
    chat_archive_path: Path = DATA_DIR / "chat_archive.db"
    evidence_database_path: Path = DATA_DIR / "evidence.db"
    evidence_root: Path = BASE_DIR / "evidence"
    evidence_required: bool = _bool_env("EVIDENCE_REQUIRED", False)
    mute_enabled: bool = _bool_env("MUTE_ENABLED", False)
    deduction_policy_v102_enabled: bool = _bool_env(
        "DEDUCTION_POLICY_V102_ENABLED", False
    )
    deduction_policy_rule_version: str = os.getenv(
        "DEDUCTION_POLICY_RULE_VERSION", "v1.0.2beta"
    )
    evidence_max_bytes: int = _int_env("EVIDENCE_MAX_BYTES", 20 * 1024 * 1024)
    ai_base_url: str = os.getenv("AI_BASE_URL", "https://api.deepseek.com").rstrip("/")
    ai_api_key: str = os.getenv("AI_API_KEY", "")
    ai_model: str = os.getenv("AI_MODEL", "deepseek-chat")
    ai_timeout: int = _int_env("AI_TIMEOUT", 30)
    admin_seed: str = os.getenv("ADMIN_SEED", "")


CONFIG = AppConfig()

GROUP_AREAS = {"蜂巢", "蜂窝", "蜂箱"}
LOCKED_STATUSES = {"已移出", "已拉黑", "已退群"}
ALL_STATUSES = {"正常", "已质询", "最后警告", "已移出", "已拉黑", "已退群"}
CONFIRM_WORDS = {"确认", "确定", "yes", "YES", "y", "Y"}
CANCEL_WORDS = {"取消", "放弃", "算了", "no", "NO", "n", "N"}


def ensure_dirs() -> None:
    for path in (DATA_DIR, EXPORT_DIR, BACKUP_DIR, LOG_DIR):
        path.mkdir(parents=True, exist_ok=True)


def parse_admin_seed(seed: str) -> list[dict]:
    admins: list[dict] = []
    for item in filter(None, [p.strip() for p in seed.split(";")]):
        parts = item.split(":")
        if len(parts) < 2:
            continue
        admins.append(
            {
                "qq_number": parts[0].strip(),
                "nickname": parts[1].strip(),
                "aliases": json.dumps([p for p in (parts[2].split("|") if len(parts) > 2 else []) if p], ensure_ascii=False),
            }
        )
    return admins
