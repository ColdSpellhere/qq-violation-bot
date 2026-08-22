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


def _float_env(name: str, default: float) -> float:
    value = os.getenv(name)
    if not value:
        return default
    try:
        return float(value)
    except ValueError:
        return default


def _bool_env(name: str, default: bool = False) -> bool:
    raw = str(os.getenv(name) or "").strip().lower()
    if not raw:
        return default
    return raw in {"1", "true", "yes", "on"}


def _id_tuple_env(name: str, default: tuple[int, ...]) -> tuple[int, ...]:
    raw = str(os.getenv(name) or "").strip()
    if not raw:
        return default
    values = [item.strip() for item in raw.split(",")]
    if not values or any(not item.isdigit() or int(item) <= 0 for item in values):
        return default
    return tuple(sorted({int(item) for item in values}))


def _string_id_tuple_env(name: str, default: tuple[str, ...]) -> tuple[str, ...]:
    raw = str(os.getenv(name) or "").strip()
    if not raw:
        return default
    values = [item.strip() for item in raw.split(",")]
    if not values or any(not item.isdigit() or int(item) <= 0 for item in values):
        return default
    return tuple(str(item) for item in sorted({int(item) for item in values}))


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


def _chat_vision_root_env() -> Path:
    allowed_root = Path(os.path.abspath(DATA_DIR / "chat_vision"))
    raw = Path(os.getenv("CHAT_VISION_IMAGE_ROOT", "data/chat_vision/images"))
    configured = Path(os.path.abspath(raw if raw.is_absolute() else BASE_DIR / raw))
    if not configured.is_relative_to(allowed_root):
        raise RuntimeError("CHAT_VISION_IMAGE_ROOT must stay under data/chat_vision")
    current = DATA_DIR
    for component in configured.relative_to(DATA_DIR).parts:
        try:
            if current.is_symlink():
                raise RuntimeError("CHAT_VISION_IMAGE_ROOT ancestors must not be symlinks")
        except OSError as exc:
            raise RuntimeError("CHAT_VISION_IMAGE_ROOT ancestors are unavailable") from exc
        current /= component
    if current.is_symlink():
        raise RuntimeError("CHAT_VISION_IMAGE_ROOT ancestors must not be symlinks")
    return configured


_TARGET_GROUP_ID = _target_group_id_env()
legacy_private_ids = _string_id_tuple_env("PRIVATE_CHAT_ALLOWED_USER_ID", ())
legacy_group_chat_config = (
    "CHAT_ENABLED" not in os.environ and "GROUP_CHAT_ENABLED" not in os.environ
)


@dataclass(frozen=True)
class AppConfig:
    allowed_group_ids: tuple[int, ...] = (_TARGET_GROUP_ID,)
    target_group_id: int = _TARGET_GROUP_ID
    bot_self_id: str = os.getenv("BOT_SELF_ID", "")
    napcat_access_token: str = os.getenv("NAPCAT_ACCESS_TOKEN", "")
    database_url: str = os.getenv("DATABASE_URL", f"sqlite:///{DATA_DIR / 'violation_records.db'}")
    database_path: Path = _database_path(os.getenv("DATABASE_URL", f"sqlite:///{DATA_DIR / 'violation_records.db'}"))
    chat_archive_path: Path = DATA_DIR / "chat_archive.db"
    member_memory_root: Path = DATA_DIR / "member_memory"
    evidence_database_path: Path = DATA_DIR / "evidence.db"
    evidence_root: Path = BASE_DIR / "evidence"
    evidence_required: bool = _bool_env("EVIDENCE_REQUIRED", False)
    chat_vision_enabled: bool = _bool_env("CHAT_VISION_ENABLED", False)
    chat_vision_model: str = os.getenv(
        "CHAT_VISION_MODEL", "deepseek-v4-flash-vision-exp"
    ).strip()
    chat_vision_root: Path = _chat_vision_root_env()
    chat_vision_retention_days: int = max(1, _int_env("CHAT_VISION_RETENTION_DAYS", 7))
    chat_vision_max_bytes: int = max(1, _int_env("CHAT_VISION_MAX_BYTES", 10 * 1024 * 1024))
    chat_vision_timeout: int = max(1, _int_env("CHAT_VISION_TIMEOUT", 60))
    chat_vision_max_retries: int = max(1, _int_env("CHAT_VISION_MAX_RETRIES", 3))
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
    random_chat_enabled: bool = _bool_env("RANDOM_CHAT_ENABLED", False)
    member_memory_summary_enabled: bool = _bool_env(
        "MEMBER_MEMORY_SUMMARY_ENABLED", False
    )
    random_chat_probability: float = min(
        1.0, max(0.0, _float_env("RANDOM_CHAT_PROBABILITY", 0.05))
    )
    random_chat_direct_fallback_enabled: bool = _bool_env(
        "RANDOM_CHAT_DIRECT_FALLBACK_ENABLED", False
    )
    random_chat_sticker_probability: float = min(
        1.0, max(0.0, _float_env("RANDOM_CHAT_STICKER_PROBABILITY", 0.20))
    )
    random_chat_sticker_root: Path = (
        DATA_DIR / "random_chat" / "stickers" / "incoming"
    )
    random_chat_special_sticker: str = os.getenv(
        "RANDOM_CHAT_SPECIAL_STICKER",
        "5df2a91f55ea6257a768b6bcfe6a10b1.gif",
    )
    private_chat_enabled: bool = _bool_env("PRIVATE_CHAT_ENABLED", False)
    private_chat_allowed_user_id: str = str(
        os.getenv("PRIVATE_CHAT_ALLOWED_USER_ID") or ""
    ).strip()
    business_enabled: bool = _bool_env("BUSINESS_ENABLED", True)
    chat_enabled: bool = _bool_env(
        "CHAT_ENABLED",
        True if legacy_group_chat_config else random_chat_enabled or private_chat_enabled,
    )
    group_chat_enabled: bool = _bool_env(
        "GROUP_CHAT_ENABLED",
        True if legacy_group_chat_config else random_chat_enabled,
    )
    group_chat_allowed_group_ids: tuple[int, ...] = _id_tuple_env(
        "GROUP_CHAT_ALLOWED_GROUP_IDS", (_TARGET_GROUP_ID,)
    )
    private_chat_allowed_user_ids: tuple[str, ...] = _string_id_tuple_env(
        "PRIVATE_CHAT_ALLOWED_USER_IDS", legacy_private_ids
    )
    private_memory_enabled: bool = _bool_env("PRIVATE_MEMORY_ENABLED", False)
    relationship_state_enabled: bool = _bool_env(
        "RELATIONSHIP_STATE_ENABLED", False
    )
    memory_governance_enabled: bool = _bool_env("MEMORY_GOVERNANCE_ENABLED", False)
    private_memory_retention_days: int = max(
        1, _int_env("PRIVATE_MEMORY_RETENTION_DAYS", 30)
    )
    private_memory_max_messages: int = max(
        1, _int_env("PRIVATE_MEMORY_MAX_MESSAGES", 500)
    )
    private_memory_shutdown_timeout: float = max(
        0.1, _float_env("PRIVATE_MEMORY_SHUTDOWN_TIMEOUT", 10.0)
    )
    runtime_features_path: Path = DATA_DIR / "runtime_features.json"
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
