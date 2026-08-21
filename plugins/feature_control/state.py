import json
import os
import shutil
import tempfile
from dataclasses import asdict, dataclass, replace
from datetime import datetime, timezone
from pathlib import Path
from threading import RLock
from typing import Any


SWITCH_NAMES = {
    "business_enabled",
    "chat_enabled",
    "group_chat_enabled",
    "private_chat_enabled",
}
ALLOWLIST_KINDS = {"group_chat", "private_chat"}


@dataclass(frozen=True)
class FeatureState:
    business_enabled: bool
    chat_enabled: bool
    group_chat_enabled: bool
    private_chat_enabled: bool
    group_chat_allowed_group_ids: tuple[int, ...]
    private_chat_allowed_user_ids: tuple[str, ...]
    updated_at: str = ""
    updated_by: str = ""


class FeatureController:
    def __init__(self, path: Path, defaults: FeatureState):
        self._path = Path(path)
        self._lock = RLock()
        self._state = self._load_state(self._path) or self._load_state(
            self._backup_path
        ) or defaults

    @property
    def _backup_path(self) -> Path:
        return self._path.with_suffix(self._path.suffix + ".bak")

    def snapshot(self) -> FeatureState:
        with self._lock:
            return self._state

    def set_switch(self, name: str, enabled: bool, actor: str) -> FeatureState:
        if name not in SWITCH_NAMES:
            raise ValueError(f"unknown feature switch: {name}")
        if not isinstance(enabled, bool):
            raise ValueError("feature switch values must be boolean")

        with self._lock:
            return self._replace_state(**{name: enabled}, updated_by=str(actor))

    def add_allowed(self, kind: str, value: str, actor: str) -> FeatureState:
        normalized = self._validate_allowed_value(kind, value)
        with self._lock:
            field_name = self._allowlist_field(kind)
            existing = getattr(self._state, field_name)
            if normalized in existing:
                return self._state
            values = self._sorted_allowlist(kind, (*existing, normalized))
            return self._replace_state(**{field_name: values}, updated_by=str(actor))

    def remove_allowed(self, kind: str, value: str, actor: str) -> FeatureState:
        normalized = self._validate_allowed_value(kind, value)
        with self._lock:
            field_name = self._allowlist_field(kind)
            existing = getattr(self._state, field_name)
            if normalized not in existing:
                return self._state
            values = tuple(item for item in existing if item != normalized)
            return self._replace_state(**{field_name: values}, updated_by=str(actor))

    def business_allowed(self, group_id: int, target_group_id: int) -> bool:
        return (
            self.snapshot().business_enabled
            and self._as_positive_int(group_id) == self._as_positive_int(target_group_id)
            and self._as_positive_int(group_id) is not None
        )

    def group_chat_allowed(self, group_id: int) -> bool:
        state = self.snapshot()
        normalized = self._as_positive_int(group_id)
        return (
            state.chat_enabled
            and state.group_chat_enabled
            and normalized is not None
            and normalized in state.group_chat_allowed_group_ids
        )

    def private_chat_allowed(self, user_id: str) -> bool:
        state = self.snapshot()
        normalized = self._as_positive_string_id(user_id)
        return (
            state.chat_enabled
            and state.private_chat_enabled
            and normalized is not None
            and normalized in state.private_chat_allowed_user_ids
        )

    def _replace_state(self, **changes: Any) -> FeatureState:
        candidate = replace(
            self._state,
            **changes,
            updated_at=datetime.now(timezone.utc).isoformat(),
        )
        self._persist(candidate)
        self._state = candidate
        return candidate

    def _persist(self, state: FeatureState) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        if self._path.is_file():
            shutil.copy2(self._path, self._backup_path)
        else:
            self._write_json(self._backup_path, self._state)
        self._write_json(self._path, state)

    @staticmethod
    def _write_json(path: Path, state: FeatureState) -> None:
        serialized = json.dumps(asdict(state), ensure_ascii=False, sort_keys=True)
        descriptor, temporary_name = tempfile.mkstemp(
            dir=path.parent,
            prefix=f".{path.name}.",
            suffix=".tmp",
        )
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                handle.write(serialized)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary_name, path)
        except BaseException:
            try:
                os.unlink(temporary_name)
            except FileNotFoundError:
                pass
            raise

    @classmethod
    def _load_state(cls, path: Path) -> FeatureState | None:
        try:
            with path.open(encoding="utf-8") as handle:
                raw = json.load(handle)
        except (OSError, json.JSONDecodeError):
            return None

        try:
            return FeatureState(
                business_enabled=cls._strict_bool(raw["business_enabled"]),
                chat_enabled=cls._strict_bool(raw["chat_enabled"]),
                group_chat_enabled=cls._strict_bool(raw["group_chat_enabled"]),
                private_chat_enabled=cls._strict_bool(raw["private_chat_enabled"]),
                group_chat_allowed_group_ids=cls._load_allowlist(
                    "group_chat", raw["group_chat_allowed_group_ids"]
                ),
                private_chat_allowed_user_ids=cls._load_allowlist(
                    "private_chat", raw["private_chat_allowed_user_ids"]
                ),
                updated_at=str(raw.get("updated_at", "")),
                updated_by=str(raw.get("updated_by", "")),
            )
        except (KeyError, TypeError, ValueError):
            return None

    @staticmethod
    def _strict_bool(value: Any) -> bool:
        if not isinstance(value, bool):
            raise ValueError("feature switch values must be boolean")
        return value

    @classmethod
    def _validate_allowed_value(cls, kind: str, value: str) -> int | str:
        if kind not in ALLOWLIST_KINDS:
            raise ValueError(f"unknown allowlist kind: {kind}")
        if kind == "group_chat":
            normalized = cls._as_positive_int(value)
        else:
            normalized = cls._as_positive_string_id(value)
        if normalized is None:
            raise ValueError("allowlist IDs must be positive numeric values")
        return normalized

    @staticmethod
    def _allowlist_field(kind: str) -> str:
        return (
            "group_chat_allowed_group_ids"
            if kind == "group_chat"
            else "private_chat_allowed_user_ids"
        )

    @classmethod
    def _load_allowlist(
        cls, kind: str, values: Any
    ) -> tuple[int, ...] | tuple[str, ...]:
        if not isinstance(values, (list, tuple)):
            raise ValueError("persisted allowlists must be arrays")
        return cls._sorted_allowlist(kind, values)

    @classmethod
    def _sorted_allowlist(cls, kind: str, values: Any) -> tuple[int, ...] | tuple[str, ...]:
        normalized = {cls._validate_allowed_value(kind, value) for value in values}
        return tuple(sorted(normalized, key=int))

    @staticmethod
    def _as_positive_int(value: Any) -> int | None:
        text = str(value).strip()
        if not text.isdigit():
            return None
        normalized = int(text)
        return normalized if normalized > 0 else None

    @classmethod
    def _as_positive_string_id(cls, value: Any) -> str | None:
        normalized = cls._as_positive_int(value)
        return str(normalized) if normalized is not None else None
