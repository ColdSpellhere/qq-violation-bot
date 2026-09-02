import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal, cast

from plugins.runtime_paths import (
    BACKUP_DIR,
    CHARACTER_FILE,
    DATA_DIR,
    EXPORT_DIR,
    INSTANCE_ROOT,
    LOG_DIR,
)


BASE_DIR = INSTANCE_ROOT
_CHAT_VISION_RECOVERY_WINDOW_SECONDS_LIMIT = 30 * 60
_CHAT_VISION_RECOVERY_MAX_ASSETS_LIMIT = 100


def _int_env(name: str, default: int) -> int:
    value = os.getenv(name)
    if not value:
        return default
    try:
        return int(value)
    except ValueError:
        return default


def _bounded_positive_int_env(name: str, default: int, maximum: int) -> int:
    value = _int_env(name, default)
    return min(value, maximum) if value > 0 else default


def _float_env(name: str, default: float) -> float:
    value = os.getenv(name)
    if not value:
        return default
    try:
        return float(value)
    except ValueError:
        return default


def _bounded_int_env(name: str, default: int, minimum: int, maximum: int) -> int:
    return min(maximum, max(minimum, _int_env(name, default)))


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


def _group_labels_env(name: str) -> tuple[tuple[int, str], ...]:
    raw = str(os.getenv(name) or "").strip()
    if not raw:
        return ()
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"{name} must be a JSON object") from exc
    if not isinstance(payload, dict):
        raise RuntimeError(f"{name} must be a JSON object")

    labels: dict[int, str] = {}
    for raw_group_id, raw_label in payload.items():
        group_id_text = str(raw_group_id).strip()
        label = str(raw_label).strip() if isinstance(raw_label, str) else ""
        if not group_id_text.isdigit() or int(group_id_text) <= 0:
            raise RuntimeError(f"{name} keys must be positive QQ group IDs")
        if not label or len(label) > 20 or any(ord(character) < 32 for character in label):
            raise RuntimeError(f"{name} labels must be 1-20 printable characters")
        if label in labels.values():
            raise RuntimeError(f"{name} labels must be unique per monitor group")
        labels[int(group_id_text)] = label
    return tuple(sorted(labels.items()))


def _string_id_tuple_env(name: str, default: tuple[str, ...]) -> tuple[str, ...]:
    raw = str(os.getenv(name) or "").strip()
    if not raw:
        return default
    values = [item.strip() for item in raw.split(",")]
    if not values or any(not item.isdigit() or int(item) <= 0 for item in values):
        return default
    return tuple(str(item) for item in sorted({int(item) for item in values}))


def _text_tuple_env(name: str, default: tuple[str, ...]) -> tuple[str, ...]:
    raw = str(os.getenv(name) or "").strip()
    if not raw:
        return default
    values = tuple(item.strip() for item in raw.split(",") if item.strip())
    return tuple(dict.fromkeys(values)) or default


def _target_group_id_env() -> int:
    raw = str(os.getenv("TARGET_GROUP_ID") or "").strip()
    if not raw.isdigit():
        raise RuntimeError("TARGET_GROUP_ID must be one numeric QQ group ID")
    group_id = int(raw)
    if group_id <= 0:
        raise RuntimeError("TARGET_GROUP_ID must be a positive QQ group ID")
    return group_id


def _bot_mode_env() -> Literal["full", "chat_only"]:
    value = str(os.getenv("BOT_MODE") or "full").strip().lower()
    if value not in {"full", "chat_only"}:
        raise RuntimeError("BOT_MODE must be full or chat_only")
    return cast(Literal["full", "chat_only"], value)


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


_BOT_MODE = _bot_mode_env()
_TARGET_GROUP_ID = 0 if _BOT_MODE == "chat_only" else _target_group_id_env()
_DEFAULT_BUSINESS_GROUPS = () if _TARGET_GROUP_ID == 0 else (_TARGET_GROUP_ID,)
legacy_private_ids = _string_id_tuple_env("PRIVATE_CHAT_ALLOWED_USER_ID", ())
legacy_group_chat_config = (
    "CHAT_ENABLED" not in os.environ and "GROUP_CHAT_ENABLED" not in os.environ
)


@dataclass(frozen=True)
class AppConfig:
    character_file: Path = CHARACTER_FILE
    bot_mode: Literal["full", "chat_only"] = _BOT_MODE
    business_capable: bool = _BOT_MODE == "full"
    allowed_group_ids: tuple[int, ...] = _DEFAULT_BUSINESS_GROUPS
    target_group_id: int = _TARGET_GROUP_ID
    bot_self_id: str = os.getenv("BOT_SELF_ID", "")
    napcat_access_token: str = os.getenv("NAPCAT_ACCESS_TOKEN", "")
    database_url: str = os.getenv("DATABASE_URL", f"sqlite:///{DATA_DIR / 'violation_records.db'}")
    database_path: Path = _database_path(os.getenv("DATABASE_URL", f"sqlite:///{DATA_DIR / 'violation_records.db'}"))
    chat_archive_path: Path = DATA_DIR / "chat_archive.db"
    member_memory_root: Path = DATA_DIR / "member_memory"
    evidence_database_path: Path = DATA_DIR / "evidence.db"
    evidence_root: Path = INSTANCE_ROOT / "evidence"
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
    chat_vision_recovery_window_seconds: int = _bounded_positive_int_env(
        "CHAT_VISION_RECOVERY_WINDOW_SECONDS",
        900,
        _CHAT_VISION_RECOVERY_WINDOW_SECONDS_LIMIT,
    )
    chat_vision_recovery_max_assets: int = _bounded_positive_int_env(
        "CHAT_VISION_RECOVERY_MAX_ASSETS",
        20,
        _CHAT_VISION_RECOVERY_MAX_ASSETS_LIMIT,
    )
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
    glm_base_url: str = os.getenv(
        "GLM_BASE_URL", "https://open.bigmodel.cn/api/paas/v4"
    ).rstrip("/")
    glm_api_key: str = field(
        default=os.getenv("GLM_API_KEY", "").strip(),
        repr=False,
    )
    glm_model: str = os.getenv("GLM_MODEL", "glm-4.7-flash").strip()
    glm_timeout: int = _bounded_int_env("GLM_TIMEOUT", 30, 1, 120)
    random_chat_enabled: bool = _bool_env("RANDOM_CHAT_ENABLED", False)
    member_memory_summary_enabled: bool = _bool_env(
        "MEMBER_MEMORY_SUMMARY_ENABLED", False
    )
    random_chat_probability: float = min(
        1.0, max(0.0, _float_env("RANDOM_CHAT_PROBABILITY", 0.05))
    )
    chat_context_messages: int = _bounded_int_env(
        "CHAT_CONTEXT_MESSAGES", 20, 5, 60
    )
    chat_context_minutes: int = _bounded_int_env(
        "CHAT_CONTEXT_MINUTES", 30, 5, 180
    )
    chat_context_self_messages: int = _bounded_int_env(
        "CHAT_CONTEXT_SELF_MESSAGES", 3, 0, 10
    )
    random_chat_direct_fallback_enabled: bool = _bool_env(
        "RANDOM_CHAT_DIRECT_FALLBACK_ENABLED", False
    )
    protected_chat_user_ids: tuple[str, ...] = _string_id_tuple_env(
        "PROTECTED_CHAT_USER_IDS", ()
    )
    protected_chat_aliases: tuple[str, ...] = _text_tuple_env(
        "PROTECTED_CHAT_ALIASES", ()
    )
    peer_bot_user_ids: tuple[str, ...] = _string_id_tuple_env(
        "PEER_BOT_USER_IDS", ()
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
    business_enabled: bool = (
        _bool_env("BUSINESS_ENABLED", True) if _BOT_MODE == "full" else False
    )
    chat_enabled: bool = _bool_env(
        "CHAT_ENABLED",
        True if legacy_group_chat_config else random_chat_enabled or private_chat_enabled,
    )
    group_chat_enabled: bool = _bool_env(
        "GROUP_CHAT_ENABLED",
        True if legacy_group_chat_config else random_chat_enabled,
    )
    group_chat_allowed_group_ids: tuple[int, ...] = _id_tuple_env(
        "GROUP_CHAT_ALLOWED_GROUP_IDS", _DEFAULT_BUSINESS_GROUPS
    )
    private_chat_allowed_user_ids: tuple[str, ...] = _string_id_tuple_env(
        "PRIVATE_CHAT_ALLOWED_USER_IDS", legacy_private_ids
    )
    private_memory_enabled: bool = _bool_env("PRIVATE_MEMORY_ENABLED", False)
    relationship_state_enabled: bool = _bool_env(
        "RELATIONSHIP_STATE_ENABLED", False
    )
    memory_governance_enabled: bool = _bool_env("MEMORY_GOVERNANCE_ENABLED", False)
    llm_gateway_enabled: bool = _bool_env("LLM_GATEWAY_ENABLED", False)
    economy_mode_enabled: bool = _bool_env("ECONOMY_MODE_ENABLED", False)
    prompt_builder_enabled: bool = _bool_env("PROMPT_BUILDER_ENABLED", False)
    web_search_enabled: bool = _bool_env("WEB_SEARCH_ENABLED", False)
    tavily_api_key: str = os.getenv("TAVILY_API_KEY", "").strip()
    web_search_timeout: int = _bounded_int_env("WEB_SEARCH_TIMEOUT", 8, 1, 30)
    web_search_max_results: int = _bounded_int_env("WEB_SEARCH_MAX_RESULTS", 5, 1, 5)
    web_search_max_context_chars: int = _bounded_int_env(
        "WEB_SEARCH_MAX_CONTEXT_CHARS", 4000, 500, 8000
    )
    llm_gateway_vision_enabled: bool = _bool_env(
        "LLM_GATEWAY_VISION_ENABLED", False
    )
    llm_gateway_private_memory_enabled: bool = _bool_env(
        "LLM_GATEWAY_PRIVATE_MEMORY_ENABLED", False
    )
    llm_gateway_member_memory_enabled: bool = _bool_env(
        "LLM_GATEWAY_MEMBER_MEMORY_ENABLED", False
    )
    llm_gateway_chat_enabled: bool = _bool_env("LLM_GATEWAY_CHAT_ENABLED", False)
    llm_gateway_business_enabled: bool = (
        _bool_env("LLM_GATEWAY_BUSINESS_ENABLED", False)
        if _BOT_MODE == "full"
        else False
    )
    llm_gateway_max_connections: int = _bounded_int_env(
        "LLM_GATEWAY_MAX_CONNECTIONS", 8, 1, 64
    )
    llm_gateway_max_retries: int = _bounded_int_env(
        "LLM_GATEWAY_MAX_RETRIES", 2, 0, 10
    )
    llm_gateway_total_concurrency: int = _bounded_int_env(
        "LLM_GATEWAY_TOTAL_CONCURRENCY", 8, 1, 64
    )
    llm_gateway_business_concurrency: int = _bounded_int_env(
        "LLM_GATEWAY_BUSINESS_CONCURRENCY", 2, 1, 16
    )
    llm_gateway_chat_concurrency: int = _bounded_int_env(
        "LLM_GATEWAY_CHAT_CONCURRENCY", 3, 1, 16
    )
    llm_gateway_vision_concurrency: int = _bounded_int_env(
        "LLM_GATEWAY_VISION_CONCURRENCY", 3, 1, 16
    )
    llm_gateway_memory_concurrency: int = _bounded_int_env(
        "LLM_GATEWAY_MEMORY_CONCURRENCY", 2, 1, 16
    )
    private_memory_retention_days: int = max(
        1, _int_env("PRIVATE_MEMORY_RETENTION_DAYS", 30)
    )
    private_memory_max_messages: int = max(
        1, _int_env("PRIVATE_MEMORY_MAX_MESSAGES", 500)
    )
    private_memory_shutdown_timeout: float = max(
        0.1, _float_env("PRIVATE_MEMORY_SHUTDOWN_TIMEOUT", 10.0)
    )
    hive_member_monitor_enabled: bool = _bool_env(
        "HIVE_MEMBER_MONITOR_ENABLED", False
    )
    hive_member_monitor_group_id: int = max(
        0, _int_env("HIVE_MEMBER_MONITOR_GROUP_ID", 0)
    )
    configured_hive_member_monitor_group_ids: tuple[int, ...] = _id_tuple_env(
        "HIVE_MEMBER_MONITOR_GROUP_IDS", ()
    )
    configured_hive_member_monitor_group_labels: tuple[tuple[int, str], ...] = (
        _group_labels_env("HIVE_MEMBER_MONITOR_GROUP_LABELS_JSON")
    )
    hive_member_report_group_id: int = max(
        0, _int_env("HIVE_MEMBER_REPORT_GROUP_ID", 0)
    )
    hive_member_monitor_reconcile_seconds: int = _bounded_int_env(
        "HIVE_MEMBER_MONITOR_RECONCILE_SECONDS", 300, 60, 3600
    )
    configured_monitor_only_group_ids: tuple[int, ...] = _id_tuple_env(
        "MONITOR_ONLY_GROUP_IDS", ()
    )
    hive_member_monitor_database_path: Path = (
        DATA_DIR / "hive_member_monitor.sqlite3"
    )
    hive_member_monitor_export_dir: Path = EXPORT_DIR / "hive_member_monitor"
    content_alert_enabled: bool = _bool_env("CONTENT_ALERT_ENABLED", False)
    content_alert_source_group_ids: tuple[int, ...] = _id_tuple_env(
        "CONTENT_ALERT_SOURCE_GROUP_IDS", ()
    )
    content_alert_report_group_id: int = max(
        0, _int_env("CONTENT_ALERT_REPORT_GROUP_ID", 0)
    )
    content_alert_rules_path: Path = (
        DATA_DIR / "content_alert" / "keywords.json"
    )
    runtime_features_path: Path = DATA_DIR / "runtime_features.json"
    admin_seed: str = os.getenv("ADMIN_SEED", "")

    @property
    def hive_member_monitor_group_ids(self) -> tuple[int, ...]:
        values = set(self.configured_hive_member_monitor_group_ids)
        if self.hive_member_monitor_group_id > 0:
            values.add(self.hive_member_monitor_group_id)
        return tuple(sorted(values))

    def hive_member_monitor_group_label(self, group_id: int) -> str:
        normalized = int(group_id)
        configured = dict(self.configured_hive_member_monitor_group_labels)
        if normalized in configured:
            return configured[normalized]
        if normalized == self.hive_member_monitor_group_id:
            return "蜂巢"
        return f"群{normalized}"

    @property
    def hive_member_monitor_capable(self) -> bool:
        return (
            bool(self.hive_member_monitor_group_ids)
            and self.hive_member_report_group_id > 0
            and self.hive_member_report_group_id
            not in self.hive_member_monitor_group_ids
            and self.target_group_id not in self.hive_member_monitor_group_ids
        )

    @property
    def monitor_only_group_ids(self) -> tuple[int, ...]:
        values = set(self.configured_monitor_only_group_ids)
        values.update(self.hive_member_monitor_group_ids)
        return tuple(sorted(values))

    @property
    def content_alert_capable(self) -> bool:
        source_groups = set(self.content_alert_source_group_ids)
        return (
            bool(source_groups)
            and self.content_alert_report_group_id > 0
            and self.content_alert_report_group_id not in source_groups
            and self.target_group_id not in source_groups
            and source_groups.issubset(self.monitor_only_group_ids)
        )

    @property
    def economy_provider_available(self) -> bool:
        return (
            type(self.glm_base_url) is str
            and self.glm_base_url.strip().rstrip("/")
            == "https://open.bigmodel.cn/api/paas/v4"
            and type(self.glm_api_key) is str
            and bool(self.glm_api_key.strip())
            and type(self.glm_model) is str
            and self.glm_model.strip() == "glm-4.7-flash"
            and self.glm_timeout > 0
        )


CONFIG = AppConfig()
if (
    CONFIG.hive_member_monitor_enabled
    and CONFIG.hive_member_monitor_group_id == CONFIG.target_group_id
):
    raise RuntimeError(
        "HIVE_MEMBER_MONITOR_GROUP_ID must differ from TARGET_GROUP_ID"
    )
if (
    CONFIG.hive_member_monitor_enabled
    and CONFIG.target_group_id in CONFIG.hive_member_monitor_group_ids
):
    raise RuntimeError(
        "HIVE_MEMBER_MONITOR_GROUP_IDS monitor groups must differ from TARGET_GROUP_ID"
    )
if CONFIG.hive_member_monitor_enabled and not CONFIG.hive_member_monitor_capable:
    raise RuntimeError(
        "HIVE_MEMBER_MONITOR_ENABLED requires distinct positive monitor and report group IDs"
    )
if (
    CONFIG.content_alert_enabled
    and not set(CONFIG.content_alert_source_group_ids).issubset(
        CONFIG.monitor_only_group_ids
    )
):
    raise RuntimeError("keyword alert source groups must be monitor-only")
if CONFIG.content_alert_enabled and not CONFIG.content_alert_capable:
    raise RuntimeError(
        "CONTENT_ALERT_ENABLED requires distinct positive source and report group IDs"
    )

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
