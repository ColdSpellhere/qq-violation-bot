from __future__ import annotations

import json
import os
import re
import stat
import tempfile
import unicodedata
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from threading import RLock
from typing import Any

RULE_FILE_VERSION = 1
MAX_RULES = 200
MIN_NORMALIZED_PATTERN_LENGTH = 2
MAX_NORMALIZED_PATTERN_LENGTH = 64

_RULE_ID_RE = re.compile(r"K([0-9]{4,})\Z")


def normalize_literal_text(value: str) -> str:
    """Return the canonical form used for literal rule matching.

    NFKC handles full-width compatibility characters, ``casefold`` provides a
    Unicode-aware case-insensitive comparison, and formatting/whitespace
    characters are ignored so common visual separators cannot evade a rule.
    """

    if not isinstance(value, str):
        raise TypeError("literal text must be a string")
    normalized = unicodedata.normalize("NFKC", value).casefold()
    return "".join(
        character
        for character in normalized
        if not character.isspace() and unicodedata.category(character) != "Cf"
    )


def _validate_pattern(value: str) -> tuple[str, str]:
    if not isinstance(value, str):
        raise TypeError("keyword pattern must be a string")
    pattern = value.strip()
    if not pattern:
        raise ValueError("keyword pattern must not be empty")

    # Whitespace controls are intentionally accepted because matching removes
    # whitespace.  Other control/surrogate/private-use/unassigned characters
    # do not belong in an operator-authored literal rule.  Cf is explicitly
    # permitted here and removed by normalize_literal_text for duplicate checks.
    for character in pattern:
        category = unicodedata.category(character)
        if category.startswith("C") and category != "Cf" and not character.isspace():
            raise ValueError("keyword pattern contains a control character")

    normalized = normalize_literal_text(pattern)
    if not (
        MIN_NORMALIZED_PATTERN_LENGTH
        <= len(normalized)
        <= MAX_NORMALIZED_PATTERN_LENGTH
    ):
        raise ValueError(
            "normalized keyword pattern length must be between "
            f"{MIN_NORMALIZED_PATTERN_LENGTH} and {MAX_NORMALIZED_PATTERN_LENGTH}"
        )
    return pattern, normalized


def _validate_actor(value: object) -> str:
    actor = str(value).strip()
    if not actor:
        raise ValueError("actor must not be empty")
    if any(unicodedata.category(character).startswith("C") for character in actor):
        raise ValueError("actor contains a control character")
    return actor[:128]


@dataclass(frozen=True)
class KeywordRule:
    rule_id: str
    pattern: str

    def __post_init__(self) -> None:
        match = (
            _RULE_ID_RE.fullmatch(self.rule_id)
            if isinstance(self.rule_id, str)
            else None
        )
        if match is None or int(match.group(1)) <= 0:
            raise ValueError(f"invalid keyword rule id: {self.rule_id!r}")
        pattern, _ = _validate_pattern(self.pattern)
        object.__setattr__(self, "pattern", pattern)


@dataclass(frozen=True)
class _RuleDocument:
    revision: int
    updated_at: str
    updated_by: str
    next_rule_number: int
    rules: tuple[KeywordRule, ...]


class KeywordRuleStore:
    """Atomically persisted literal rules with a last-known-good snapshot.

    The JSON file is instance-local.  Every successful mutation writes a
    backup of the previous valid file and publishes the new document through a
    same-directory atomic replacement.  A malformed manual replacement never
    displaces the in-memory last-known-good rules.
    """

    def __init__(self, path: Path):
        self._path = Path(path)
        self._trusted_root = self._default_trusted_root(
            Path(os.path.abspath(self._path))
        )
        self._lock = RLock()
        self._document = _RuleDocument(
            revision=0,
            updated_at="",
            updated_by="",
            next_rule_number=1,
            rules=(),
        )
        with self._lock:
            loaded = self._load_document_if_valid()
            if loaded is not None:
                self._document = loaded

    @property
    def path(self) -> Path:
        return self._path

    @property
    def _backup_path(self) -> Path:
        return self._path.with_name(f"{self._path.name}.bak")

    def snapshot(self) -> tuple[KeywordRule, ...]:
        with self._lock:
            loaded = self._load_document_if_valid()
            if loaded is not None:
                self._document = loaded
            return self._document.rules

    def add(self, pattern: str, *, actor: object) -> KeywordRule:
        display_pattern, normalized = _validate_pattern(pattern)
        updated_by = _validate_actor(actor)

        with self._lock:
            self._assert_safe_path()
            self._refresh_from_valid_file()
            if len(self._document.rules) >= MAX_RULES:
                raise ValueError(f"keyword rule limit is {MAX_RULES}")

            for existing in self._document.rules:
                if normalize_literal_text(existing.pattern) == normalized:
                    raise ValueError(
                        f"keyword pattern already exists as {existing.rule_id}"
                    )

            number = self._document.next_rule_number
            rule = KeywordRule(rule_id=f"K{number:04d}", pattern=display_pattern)
            document = _RuleDocument(
                revision=self._document.revision + 1,
                updated_at=_utc_now(),
                updated_by=updated_by,
                next_rule_number=number + 1,
                rules=(*self._document.rules, rule),
            )
            self._persist(document)
            self._document = document
            return rule

    def remove(self, rule_id: str, *, actor: object) -> KeywordRule:
        updated_by = _validate_actor(actor)
        if not isinstance(rule_id, str):
            raise KeyError(rule_id)
        requested_id = rule_id.strip()

        with self._lock:
            self._assert_safe_path()
            self._refresh_from_valid_file()
            removed = next(
                (rule for rule in self._document.rules if rule.rule_id == requested_id),
                None,
            )
            if removed is None:
                raise KeyError(requested_id)

            document = _RuleDocument(
                revision=self._document.revision + 1,
                updated_at=_utc_now(),
                updated_by=updated_by,
                next_rule_number=self._document.next_rule_number,
                rules=tuple(
                    rule
                    for rule in self._document.rules
                    if rule.rule_id != requested_id
                ),
            )
            self._persist(document)
            self._document = document
            return removed

    def _refresh_from_valid_file(self) -> None:
        loaded = self._load_document_if_valid()
        if loaded is not None:
            self._document = loaded

    def _load_document_if_valid(self) -> _RuleDocument | None:
        try:
            self._assert_safe_path()
            raw_bytes = self._path.read_bytes()
            raw = json.loads(raw_bytes.decode("utf-8"))
            return self._decode_document(raw)
        except FileNotFoundError:
            return None
        except (
            OSError,
            UnicodeError,
            json.JSONDecodeError,
            KeyError,
            TypeError,
            ValueError,
        ):
            return None

    @staticmethod
    def _decode_document(raw: Any) -> _RuleDocument:
        if not isinstance(raw, dict) or raw.get("version") != RULE_FILE_VERSION:
            raise ValueError("unsupported keyword rule document")

        revision = raw.get("revision")
        if isinstance(revision, bool) or not isinstance(revision, int) or revision < 0:
            raise ValueError("invalid keyword rule revision")
        updated_at = raw.get("updated_at")
        updated_by = raw.get("updated_by")
        if not isinstance(updated_at, str) or not isinstance(updated_by, str):
            raise TypeError("invalid keyword rule audit metadata")

        entries = raw.get("rules")
        if not isinstance(entries, list) or len(entries) > MAX_RULES:
            raise ValueError("invalid keyword rules")

        rules: list[KeywordRule] = []
        ids: set[str] = set()
        normalized_patterns: set[str] = set()
        highest_number = 0
        for entry in entries:
            if not isinstance(entry, dict) or set(entry) != {"id", "pattern"}:
                raise ValueError("invalid keyword rule entry")
            rule = KeywordRule(rule_id=entry["id"], pattern=entry["pattern"])
            _, normalized = _validate_pattern(rule.pattern)
            if rule.rule_id in ids or normalized in normalized_patterns:
                raise ValueError("duplicate keyword rule")
            ids.add(rule.rule_id)
            normalized_patterns.add(normalized)
            number_match = _RULE_ID_RE.fullmatch(rule.rule_id)
            assert number_match is not None
            highest_number = max(highest_number, int(number_match.group(1)))
            rules.append(rule)

        next_rule_number = raw.get("next_rule_number", highest_number + 1)
        if (
            isinstance(next_rule_number, bool)
            or not isinstance(next_rule_number, int)
            or next_rule_number <= highest_number
        ):
            raise ValueError("invalid next keyword rule number")
        return _RuleDocument(
            revision=revision,
            updated_at=updated_at,
            updated_by=updated_by,
            next_rule_number=next_rule_number,
            rules=tuple(rules),
        )

    def _persist(self, document: _RuleDocument) -> None:
        self._assert_safe_path()
        self._path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(self._path.parent, 0o700)
        self._assert_safe_path()

        previous: bytes | None = None
        try:
            candidate = self._path.read_bytes()
            self._decode_document(json.loads(candidate.decode("utf-8")))
            previous = candidate
        except FileNotFoundError:
            pass
        except (
            UnicodeError,
            json.JSONDecodeError,
            KeyError,
            TypeError,
            ValueError,
        ):
            # Back up the in-memory last-known-good document when an external
            # writer has replaced the primary file with malformed content.
            if self._document.revision > 0:
                previous = self._encode_document(self._document)

        if previous is not None:
            self._atomic_write(self._backup_path, previous)
        self._atomic_write(self._path, self._encode_document(document))

    @staticmethod
    def _encode_document(document: _RuleDocument) -> bytes:
        payload = {
            "version": RULE_FILE_VERSION,
            "revision": document.revision,
            "updated_at": document.updated_at,
            "updated_by": document.updated_by,
            "next_rule_number": document.next_rule_number,
            "rules": [
                {"id": rule.rule_id, "pattern": rule.pattern} for rule in document.rules
            ],
        }
        return (
            json.dumps(
                payload,
                ensure_ascii=False,
                indent=2,
                sort_keys=True,
            )
            + "\n"
        ).encode("utf-8")

    def _atomic_write(self, destination: Path, content: bytes) -> None:
        self._assert_safe_path(destination)
        descriptor, temporary_name = tempfile.mkstemp(
            dir=destination.parent,
            prefix=f".{destination.name}.",
            suffix=".tmp",
        )
        try:
            os.fchmod(descriptor, 0o600)
            with os.fdopen(descriptor, "wb") as handle:
                handle.write(content)
                handle.flush()
                os.fsync(handle.fileno())
            self._assert_safe_path(destination)
            os.replace(temporary_name, destination)
            os.chmod(destination, 0o600)
            self._fsync_directory(destination.parent)
        except BaseException:
            try:
                os.unlink(temporary_name)
            except FileNotFoundError:
                pass
            raise

    def _assert_safe_path(self, candidate: Path | None = None) -> None:
        path = Path(os.path.abspath(self._path if candidate is None else candidate))
        try:
            relative = path.relative_to(self._trusted_root)
        except ValueError as exc:
            raise OSError(f"path is outside the trusted instance root: {path}") from exc

        current = self._trusted_root
        components = [current]
        for part in relative.parts:
            current /= part
            components.append(current)
        for component in components:
            try:
                mode = os.lstat(component).st_mode
            except FileNotFoundError:
                continue
            if stat.S_ISLNK(mode):
                raise OSError(
                    f"symbolic link path component is not allowed: {component}"
                )

    @staticmethod
    def _default_trusted_root(path: Path) -> Path:
        if path.parent.name == "content_alert" and path.parent.parent.name == "data":
            return path.parent.parent.parent
        return path.parent

    @staticmethod
    def _fsync_directory(directory: Path) -> None:
        descriptor = os.open(directory, os.O_RDONLY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
