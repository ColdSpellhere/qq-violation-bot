from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import os
import re
import secrets
import shutil
import stat
import sys
import unicodedata
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Iterator


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from plugins.content_alert.catalog import (  # noqa: E402
    MAX_MANIFEST_BYTES,
    MAX_MANAGED_TRIE_NODES,
    MAX_POINTER_BYTES,
    MAX_SHARD_BYTES,
    ManagedKeywordCatalog,
)


DOCUMENT_VERSION = 1
PRIVATE_DIRECTORY_MODE = 0o700
PRIVATE_FILE_MODE = 0o600
MAX_BUILD_BYTES = 64 * 1024 * 1024
# Runtime matching is bounded by unique enabled+active normalized patterns.
# Shadow/disabled material stays available for later review without consuming
# that active budget, while separate storage bounds still prevent oversized
# catalogs from exhausting memory during validation.
MAX_TOTAL_PATTERNS = 50_000
MAX_STORED_PATTERNS = 100_000
MAX_TOTAL_ENTRIES = 100_000
MAX_CATEGORIES = 64
MAX_SOURCES_PER_CATEGORY = 64
MAX_SHARD_ENTRIES = 1_000
MAX_TERM_LENGTH = 256
MAX_NORMALIZED_TERM_LENGTH = 64

REQUIRED_CATEGORY_IDS = frozenset(
    {
        "political_cn",
        "sexual_explicit",
        "gender_conflict",
        "controversial_topics",
        "anime_game_controversy",
        "graphic_violence",
        "terrorism",
    }
)
DISCLOSURE_POLICIES = frozenset({"strict_hidden", "management_visible"})
SEVERITIES = frozenset({"low", "medium", "high", "critical"})
ENTRY_STATUSES = frozenset({"active", "shadow", "disabled"})
KNOWN_CATEGORY_POLICIES = {
    "political_cn": "strict_hidden",
    "sexual_explicit": "management_visible",
    "gender_conflict": "management_visible",
    "controversial_topics": "management_visible",
    "anime_game_controversy": "management_visible",
    "graphic_violence": "management_visible",
    "terrorism": "management_visible",
}
GENDER_ACTIVE_REVIEW_TAG = "human-reviewed-gender-antagonism"

_IDENTIFIER_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}\Z")
_CATEGORY_ID_RE = re.compile(r"[a-z][a-z0-9_]{1,63}\Z")
_SHA256_RE = re.compile(r"[0-9a-f]{64}\Z")


class CatalogImportError(RuntimeError):
    """A fail-closed catalog import or rollback error.

    Error messages deliberately describe only the failing field or operation;
    they never interpolate a rule term or alias.
    """


@dataclass(frozen=True)
class PreparedGeneration:
    generation_id: str
    catalog_sha256: str
    pointer_bytes: bytes
    files: dict[Path, bytes]
    category_count: int
    entry_count: int


def _json_bytes(value: object) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def _reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise CatalogImportError("JSON contains a duplicate object key")
        result[key] = value
    return result


def _reject_non_finite(_value: str) -> None:
    raise CatalogImportError("JSON contains a non-finite number")


def _decode_json(payload: bytes, *, label: str) -> Any:
    try:
        return json.loads(
            payload.decode("utf-8"),
            object_pairs_hook=_reject_duplicate_keys,
            parse_constant=_reject_non_finite,
        )
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise CatalogImportError(f"{label} is not valid UTF-8 JSON") from exc


def _assert_absolute_clean_path(path: Path, *, label: str) -> Path:
    candidate = Path(path)
    if not candidate.is_absolute() or ".." in candidate.parts:
        raise CatalogImportError(f"{label} must be an absolute normalized path")
    return candidate


def _assert_existing_path_chain(path: Path, *, leaf_kind: str) -> None:
    current = Path(path.anchor)
    parts = path.parts[1:]
    for index, part in enumerate(parts):
        current = current / part
        try:
            metadata = os.lstat(current)
        except FileNotFoundError as exc:
            raise CatalogImportError("required path does not exist") from exc
        if stat.S_ISLNK(metadata.st_mode):
            raise CatalogImportError("symbolic links are not allowed")
        is_leaf = index == len(parts) - 1
        if not is_leaf and not stat.S_ISDIR(metadata.st_mode):
            raise CatalogImportError("path ancestor is not a directory")
        if is_leaf:
            expected = stat.S_ISDIR if leaf_kind == "directory" else stat.S_ISREG
            if not expected(metadata.st_mode):
                raise CatalogImportError(f"path is not a regular {leaf_kind}")


def _assert_instance_root(path: Path) -> Path:
    root = _assert_absolute_clean_path(path, label="instance root")
    if root == Path(root.anchor):
        raise CatalogImportError("instance root must not be a filesystem root")
    _assert_existing_path_chain(root, leaf_kind="directory")
    return root


def _assert_regular_or_missing(path: Path) -> None:
    try:
        metadata = os.lstat(path)
    except FileNotFoundError:
        return
    if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISREG(metadata.st_mode):
        raise CatalogImportError("managed file must be regular or absent")


def _assert_directory_or_missing(path: Path) -> None:
    try:
        metadata = os.lstat(path)
    except FileNotFoundError:
        return
    if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(metadata.st_mode):
        raise CatalogImportError("managed directory must be regular or absent")


def _read_regular_file(
    path: Path,
    *,
    label: str,
    max_bytes: int = MAX_BUILD_BYTES,
) -> bytes:
    path = _assert_absolute_clean_path(path, label=label)
    _assert_existing_path_chain(path, leaf_kind="file")
    flags = os.O_RDONLY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        descriptor = os.open(path, flags)
    except OSError as exc:
        raise CatalogImportError(f"unable to open {label}") from exc
    try:
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode):
            raise CatalogImportError(f"{label} is not a regular file")
        if metadata.st_uid != os.geteuid() or stat.S_IMODE(metadata.st_mode) & 0o077:
            raise CatalogImportError(f"{label} has unsafe permissions")
        if metadata.st_size > max_bytes:
            raise CatalogImportError(f"{label} is too large")
        chunks: list[bytes] = []
        remaining = max_bytes + 1
        while remaining > 0:
            chunk = os.read(descriptor, min(1024 * 1024, remaining))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        payload = b"".join(chunks)
        if len(payload) > max_bytes:
            raise CatalogImportError(f"{label} is too large")
        return payload
    finally:
        os.close(descriptor)


def _ensure_directory(
    path: Path,
    *,
    trusted_root: Path,
    enforce_private_mode: bool,
) -> None:
    try:
        relative = path.relative_to(trusted_root)
    except ValueError as exc:
        raise CatalogImportError("directory escapes the instance root") from exc
    current = trusted_root
    for index, part in enumerate(relative.parts):
        current = current / part
        is_leaf = index == len(relative.parts) - 1
        try:
            metadata = os.lstat(current)
        except FileNotFoundError:
            try:
                os.mkdir(current, PRIVATE_DIRECTORY_MODE)
            except OSError as exc:
                raise CatalogImportError("unable to create private directory") from exc
            os.chmod(current, PRIVATE_DIRECTORY_MODE, follow_symlinks=False)
            _fsync_directory(current.parent)
            continue
        if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(metadata.st_mode):
            raise CatalogImportError("directory path contains a symbolic link")
        if is_leaf and enforce_private_mode:
            os.chmod(current, PRIVATE_DIRECTORY_MODE, follow_symlinks=False)


def _fsync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _write_new_private_file(path: Path, payload: bytes) -> None:
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    descriptor = os.open(path, flags, PRIVATE_FILE_MODE)
    try:
        os.fchmod(descriptor, PRIVATE_FILE_MODE)
        view = memoryview(payload)
        written = 0
        while written < len(view):
            written += os.write(descriptor, view[written:])
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _atomic_write_private(path: Path, payload: bytes) -> None:
    _assert_regular_or_missing(path)
    temporary = path.with_name(f".{path.name}.{secrets.token_hex(8)}.tmp")
    try:
        _write_new_private_file(temporary, payload)
        os.replace(temporary, path)
        _fsync_directory(path.parent)
    finally:
        try:
            if temporary.exists() and not temporary.is_symlink():
                temporary.unlink()
        except OSError:
            pass


def _validate_display_text(
    value: object,
    *,
    label: str,
    minimum: int = 1,
    maximum: int = MAX_TERM_LENGTH,
    allow_format: bool = False,
) -> str:
    if not isinstance(value, str):
        raise CatalogImportError(f"{label} must be a string")
    result = value.strip()
    if not minimum <= len(result) <= maximum:
        raise CatalogImportError(f"{label} has an invalid length")
    for character in result:
        category = unicodedata.category(character)
        if category.startswith("C") and not (
            allow_format and (category == "Cf" or character.isspace())
        ):
            raise CatalogImportError(f"{label} contains a control character")
    return result


def _normalize_term(value: str) -> str:
    normalized = unicodedata.normalize("NFKC", value).casefold()
    return "".join(
        character
        for character in normalized
        if not character.isspace() and unicodedata.category(character) != "Cf"
    )


def _validate_identifier(value: object, *, label: str) -> str:
    if not isinstance(value, str) or _IDENTIFIER_RE.fullmatch(value) is None:
        raise CatalogImportError(f"{label} is invalid")
    return value


def _copy_source(source: object) -> dict[str, Any]:
    if not isinstance(source, dict):
        raise CatalogImportError("category source must be an object")
    source_id = _validate_identifier(source.get("source_id"), label="source id")
    try:
        encoded = _json_bytes(source)
    except (TypeError, ValueError) as exc:
        raise CatalogImportError("category source is not JSON-safe") from exc
    if len(encoded) > 16 * 1024:
        raise CatalogImportError("category source metadata is too large")
    copied = _decode_json(encoded, label="category source")
    copied["source_id"] = source_id
    if "reference" not in copied:
        reference = copied.get("url", "private-build")
        copied["reference"] = _validate_display_text(
            reference,
            label="source reference",
            maximum=2_048,
        )
    if "license" not in copied or "retrieved_at" not in copied:
        raise CatalogImportError("category source is missing required metadata")
    copied["license"] = _validate_display_text(
        copied["license"],
        label="source license",
        maximum=128,
    )
    copied["retrieved_at"] = _validate_date_or_timestamp(
        copied["retrieved_at"],
        label="source retrieved_at",
        timezone_required=True,
    )
    for field in ("url", "revision", "reference"):
        if field in copied:
            copied[field] = _validate_display_text(
                copied[field],
                label=f"source {field}",
                maximum=(
                    2_048 if field == "url" else 1_024 if field == "reference" else 512
                ),
            )
    if "sha256" in copied:
        digest = copied["sha256"]
        if not isinstance(digest, str) or _SHA256_RE.fullmatch(digest) is None:
            raise CatalogImportError("source sha256 is invalid")
    return copied


def _validate_date_or_timestamp(
    value: object,
    *,
    label: str,
    timezone_required: bool = False,
) -> str:
    result = _validate_display_text(value, label=label, maximum=128)
    try:
        parsed = datetime.fromisoformat(
            result[:-1] + "+00:00" if result.endswith("Z") else result
        )
    except ValueError as exc:
        raise CatalogImportError(f"{label} is invalid") from exc
    if timezone_required and parsed.tzinfo is None:
        raise CatalogImportError(f"{label} must include a timezone")
    return result


def _validate_optional_entry_metadata(
    raw_entry: dict[str, Any],
    *,
    source_ids: list[str],
) -> dict[str, Any]:
    metadata: dict[str, Any] = {}
    if "tags" in raw_entry:
        raw_tags = raw_entry["tags"]
        if not isinstance(raw_tags, list) or len(raw_tags) > 64:
            raise CatalogImportError("entry tags are invalid")
        tags = [
            _validate_display_text(tag, label="entry tag", maximum=128)
            for tag in raw_tags
        ]
        if len(tags) != len(set(tags)):
            raise CatalogImportError("entry tag is duplicated")
        metadata["tags"] = tags

    if "severity" in raw_entry:
        severity = raw_entry["severity"]
        if severity not in SEVERITIES:
            raise CatalogImportError("entry severity is invalid")
        metadata["severity"] = severity

    if "confidence" in raw_entry:
        confidence = raw_entry["confidence"]
        if (
            isinstance(confidence, bool)
            or not isinstance(confidence, (int, float))
            or not 0 <= confidence <= 1
        ):
            raise CatalogImportError("entry confidence is invalid")
        metadata["confidence"] = confidence

    for field in ("first_seen", "last_reviewed"):
        if field in raw_entry:
            metadata[field] = _validate_date_or_timestamp(
                raw_entry[field],
                label=f"entry {field}",
            )

    if "source_refs" in raw_entry:
        raw_refs = raw_entry["source_refs"]
        if not isinstance(raw_refs, list) or not raw_refs or len(raw_refs) > 64:
            raise CatalogImportError("entry source_refs are invalid")
        refs = [
            _validate_identifier(reference, label="entry source_ref")
            for reference in raw_refs
        ]
        if len(refs) != len(set(refs)) or any(
            reference not in source_ids for reference in refs
        ):
            raise CatalogImportError("entry source_refs are invalid")
        metadata["source_refs"] = refs
    return metadata


def _validate_build(raw: object) -> tuple[str, list[dict[str, Any]]]:
    if not isinstance(raw, dict) or raw.get("version") != DOCUMENT_VERSION:
        raise CatalogImportError("unsupported private build document")
    generated_at = _validate_date_or_timestamp(
        raw.get("generated_at"),
        label="generated_at",
        timezone_required=True,
    )
    raw_categories = raw.get("categories")
    if not isinstance(raw_categories, list) or not raw_categories:
        raise CatalogImportError("categories must be a non-empty list")
    if len(raw_categories) > MAX_CATEGORIES:
        raise CatalogImportError("too many categories")

    categories: list[dict[str, Any]] = []
    seen_category_ids: set[str] = set()
    seen_entry_ids: set[str] = set()
    active_patterns: set[str] = set()
    stored_pattern_count = 0
    total_entries = 0
    for raw_category in raw_categories:
        if not isinstance(raw_category, dict):
            raise CatalogImportError("category must be an object")
        category_id = raw_category.get("id")
        if not isinstance(category_id, str) or _CATEGORY_ID_RE.fullmatch(category_id) is None:
            raise CatalogImportError("category id is invalid")
        if category_id in seen_category_ids:
            raise CatalogImportError("category id is duplicated")
        seen_category_ids.add(category_id)

        policy = raw_category.get("disclosure_policy")
        if policy not in DISCLOSURE_POLICIES:
            raise CatalogImportError("category disclosure policy is invalid")
        required_policy = KNOWN_CATEGORY_POLICIES.get(category_id)
        if required_policy is not None and policy != required_policy:
            raise CatalogImportError("known category disclosure policy is invalid")
        severity = raw_category.get("severity")
        if severity not in SEVERITIES:
            raise CatalogImportError("category severity is invalid")
        enabled = raw_category.get("enabled")
        if not isinstance(enabled, bool):
            raise CatalogImportError("category enabled must be boolean")
        category_version = raw_category.get("version")
        if isinstance(category_version, bool) or not isinstance(
            category_version, (int, str)
        ):
            raise CatalogImportError("category version is invalid")
        if isinstance(category_version, int) and category_version < 1:
            raise CatalogImportError("category version is invalid")
        if isinstance(category_version, int):
            category_version = str(category_version)
        else:
            category_version = _validate_display_text(
                category_version,
                label="category version",
                maximum=64,
            )

        raw_sources = raw_category.get("sources")
        if (
            not isinstance(raw_sources, list)
            or not raw_sources
            or len(raw_sources) > MAX_SOURCES_PER_CATEGORY
        ):
            raise CatalogImportError("category sources must be a non-empty list")
        sources = [_copy_source(source) for source in raw_sources]
        source_ids = [source["source_id"] for source in sources]
        if len(source_ids) != len(set(source_ids)):
            raise CatalogImportError("category source id is duplicated")

        raw_entries = raw_category.get("entries")
        if not isinstance(raw_entries, list):
            raise CatalogImportError("category entries must be a list")
        entries: list[dict[str, Any]] = []
        entry_ids: set[str] = set()
        normalized_patterns: set[str] = set()
        for raw_entry in raw_entries:
            if not isinstance(raw_entry, dict):
                raise CatalogImportError("catalog entry must be an object")
            entry_id = _validate_identifier(raw_entry.get("id"), label="entry id")
            if entry_id in entry_ids:
                raise CatalogImportError("entry id is duplicated")
            entry_ids.add(entry_id)
            if entry_id in seen_entry_ids:
                raise CatalogImportError("entry id is duplicated across categories")
            seen_entry_ids.add(entry_id)
            term = _validate_display_text(
                raw_entry.get("term"),
                label="entry term",
                minimum=2,
                allow_format=True,
            )
            normalized_term = _normalize_term(term)
            if not 2 <= len(normalized_term) <= MAX_NORMALIZED_TERM_LENGTH:
                raise CatalogImportError("entry term normalizes to an invalid length")
            if normalized_term in normalized_patterns:
                raise CatalogImportError("entry term is duplicated within its category")
            normalized_patterns.add(normalized_term)

            raw_aliases = raw_entry.get("aliases", [])
            if not isinstance(raw_aliases, list) or len(raw_aliases) > 32:
                raise CatalogImportError("entry aliases are invalid")
            aliases: list[str] = []
            for raw_alias in raw_aliases:
                alias = _validate_display_text(
                    raw_alias,
                    label="entry alias",
                    minimum=2,
                    allow_format=True,
                )
                normalized_alias = _normalize_term(alias)
                if not 2 <= len(normalized_alias) <= MAX_NORMALIZED_TERM_LENGTH:
                    raise CatalogImportError("entry alias normalizes to an invalid length")
                if normalized_alias in normalized_patterns:
                    raise CatalogImportError("entry alias is duplicated within its category")
                normalized_patterns.add(normalized_alias)
                aliases.append(alias)

            status = raw_entry.get("status", "active")
            if status not in ENTRY_STATUSES:
                raise CatalogImportError("entry status is invalid")
            optional_metadata = _validate_optional_entry_metadata(
                raw_entry,
                source_ids=source_ids,
            )
            if (
                category_id == "gender_conflict"
                and status == "active"
                and GENDER_ACTIVE_REVIEW_TAG
                not in optional_metadata.get("tags", ())
            ):
                raise CatalogImportError(
                    "active gender conflict entry is missing human review"
                )
            default_source_ref = optional_metadata.get("source_refs", source_ids)[0]
            source_ref = raw_entry.get("source_ref", default_source_ref)
            source_ref = _validate_identifier(source_ref, label="entry source_ref")
            if source_ref not in source_ids:
                raise CatalogImportError("entry source_ref is unknown")
            entries.append(
                {
                    "id": entry_id,
                    "term": term,
                    "aliases": aliases,
                    "status": status,
                    "source_ref": source_ref,
                }
                | optional_metadata
            )
            total_entries += 1
            if total_entries > MAX_TOTAL_ENTRIES:
                raise CatalogImportError("catalog entry limit exceeded")
            stored_pattern_count += 1 + len(aliases)
            if stored_pattern_count > MAX_STORED_PATTERNS:
                raise CatalogImportError("catalog stored pattern limit exceeded")
            if enabled and status == "active":
                active_patterns.update(
                    (_normalize_term(term), *(_normalize_term(alias) for alias in aliases))
                )
            if len(active_patterns) > MAX_TOTAL_PATTERNS:
                raise CatalogImportError("catalog pattern limit exceeded")

        categories.append(
            {
                "id": category_id,
                "name_zh": _validate_display_text(
                    raw_category.get("name_zh"),
                    label="category name",
                    maximum=64,
                ),
                "description": _validate_display_text(
                    raw_category.get("description"),
                    label="category description",
                    maximum=512,
                ),
                "severity": severity,
                "disclosure_policy": policy,
                "enabled": enabled,
                "version": category_version,
                "sources": sources,
                "entries": sorted(entries, key=lambda entry: entry["id"]),
            }
        )

        if not entries:
            raise CatalogImportError("category must contain an entry")

    if not REQUIRED_CATEGORY_IDS.issubset(seen_category_ids):
        raise CatalogImportError("private build is missing required categories")
    _assert_trie_node_limit(active_patterns)
    return generated_at, sorted(categories, key=lambda item: item["id"])


def _load_legacy_rules(instance_root: Path) -> tuple[bytes | None, list[dict[str, str]]]:
    path = instance_root / "data" / "content_alert" / "background_keywords.json"
    try:
        os.lstat(path)
    except FileNotFoundError:
        return None, []
    payload = _read_regular_file(path, label="legacy rules")
    raw = _decode_json(payload, label="legacy rules")
    if not isinstance(raw, dict) or raw.get("version") != DOCUMENT_VERSION:
        raise CatalogImportError("legacy rule document version is invalid")
    raw_rules = raw.get("rules")
    if not isinstance(raw_rules, list):
        raise CatalogImportError("legacy rule document is invalid")
    rules: list[dict[str, str]] = []
    ids: set[str] = set()
    normalized: set[str] = set()
    for raw_rule in raw_rules:
        if not isinstance(raw_rule, dict):
            raise CatalogImportError("legacy rule entry is invalid")
        rule_id = _validate_identifier(raw_rule.get("id"), label="legacy rule id")
        if rule_id in ids:
            raise CatalogImportError("legacy rule id is duplicated")
        ids.add(rule_id)
        pattern = _validate_display_text(
            raw_rule.get("pattern"),
            label="legacy rule term",
            minimum=2,
            allow_format=True,
        )
        normalized_pattern = _normalize_term(pattern)
        if (
            not 2 <= len(normalized_pattern) <= MAX_NORMALIZED_TERM_LENGTH
            or normalized_pattern in normalized
        ):
            raise CatalogImportError("legacy rule term is invalid or duplicated")
        normalized.add(normalized_pattern)
        rules.append({"id": rule_id, "term": pattern})
    return payload, rules


def _merge_legacy_rules(
    categories: list[dict[str, Any]],
    legacy_rules: list[dict[str, str]],
    *,
    generated_at: str,
) -> list[dict[str, Any]]:
    if not legacy_rules:
        return categories
    copied = _decode_json(_json_bytes(categories), label="validated categories")
    political = next(
        category for category in copied if category["id"] == "political_cn"
    )
    source_id = "legacy-background-keywords"
    existing_source_ids = {source["source_id"] for source in political["sources"]}
    if len(existing_source_ids) >= MAX_SOURCES_PER_CATEGORY:
        raise CatalogImportError("political category source limit exceeded")
    suffix = 1
    while source_id in existing_source_ids:
        suffix += 1
        source_id = f"legacy-background-keywords-{suffix}"
    political["sources"].append(
        {
            "source_id": source_id,
            "reference": "instance-private:background_keywords.json",
            "license": "private-legacy",
            "retrieved_at": generated_at,
        }
    )

    entry_ids = {
        entry["id"]
        for category in copied
        for entry in category["entries"]
    }
    normalized_patterns = {
        _normalize_term(value)
        for entry in political["entries"]
        for value in (entry["term"], *entry["aliases"])
    }
    for legacy in legacy_rules:
        normalized = _normalize_term(legacy["term"])
        if normalized in normalized_patterns:
            continue
        entry_id = _allocate_legacy_entry_id(legacy["id"], entry_ids)
        entry_ids.add(entry_id)
        normalized_patterns.add(normalized)
        political["entries"].append(
            {
                "id": entry_id,
                "term": legacy["term"],
                "aliases": [],
                "status": "active",
                "source_ref": source_id,
            }
        )
    political["entries"].sort(key=lambda entry: entry["id"])
    return copied


def _allocate_legacy_entry_id(rule_id: str, existing_ids: set[str]) -> str:
    direct = f"legacy-{rule_id}"
    if _IDENTIFIER_RE.fullmatch(direct) is not None and direct not in existing_ids:
        return direct

    # A legacy identifier may already occupy the full 128-character runtime
    # limit, and a build may deliberately use the obvious derived hash.  Keep
    # the mapping deterministic while trying bounded, collision-safe digests.
    for nonce in range(1_024):
        digest_input = f"{rule_id}\0{nonce}".encode("utf-8")
        digest = hashlib.sha256(digest_input).hexdigest()[:24]
        candidate = f"legacy-{digest}"
        if candidate not in existing_ids:
            return _validate_identifier(candidate, label="legacy mapped rule id")
    raise CatalogImportError("legacy rule id conflicts with build entries")


def _assert_trie_node_limit(active_patterns: set[str]) -> None:
    trie_prefixes: set[str] = set()
    for pattern in active_patterns:
        for length in range(1, len(pattern) + 1):
            trie_prefixes.add(pattern[:length])
            if len(trie_prefixes) + 1 > MAX_MANAGED_TRIE_NODES:
                raise CatalogImportError("catalog trie node limit exceeded")


def _assert_merged_pattern_limit(categories: list[dict[str, Any]]) -> None:
    entries = [
        entry
        for category in categories
        for entry in category["entries"]
    ]
    if len(entries) > MAX_TOTAL_ENTRIES:
        raise CatalogImportError("catalog entry limit exceeded after legacy import")
    stored_pattern_count = sum(1 + len(entry["aliases"]) for entry in entries)
    if stored_pattern_count > MAX_STORED_PATTERNS:
        raise CatalogImportError(
            "catalog stored pattern limit exceeded after legacy import"
        )
    active_patterns = {
        _normalize_term(pattern)
        for category in categories
        if category["enabled"]
        for entry in category["entries"]
        if entry["status"] == "active"
        for pattern in (entry["term"], *entry["aliases"])
    }
    if len(active_patterns) > MAX_TOTAL_PATTERNS:
        raise CatalogImportError("catalog pattern limit exceeded after legacy import")
    _assert_trie_node_limit(active_patterns)


def _prepare_generation(
    generated_at: str,
    categories: list[dict[str, Any]],
) -> PreparedGeneration:
    seed = _json_bytes(
        {
            "version": DOCUMENT_VERSION,
            "generated_at": generated_at,
            "categories": categories,
        }
    )
    digest = hashlib.sha256(b"content-alert-catalog-v1\0" + seed).hexdigest()
    generation_id = f"generation-{digest}"
    files: dict[Path, bytes] = {}
    manifest_categories: list[dict[str, Any]] = []
    entry_count = 0

    for category in categories:
        category_id = category["id"]
        entries = category["entries"]
        shards: list[dict[str, Any]] = []
        for shard_number, offset in enumerate(
            range(0, len(entries), MAX_SHARD_ENTRIES),
            start=1,
        ):
            shard_entries = entries[offset : offset + MAX_SHARD_ENTRIES]
            relative = Path("shards") / f"{category_id}-{shard_number:04d}.json"
            payload = _json_bytes(
                {
                    "version": DOCUMENT_VERSION,
                    "generation_id": generation_id,
                    "category_id": category_id,
                    "entries": shard_entries,
                }
            )
            files[relative] = payload
            shards.append(
                {
                    "path": str(relative),
                    "sha256": hashlib.sha256(payload).hexdigest(),
                    "entry_count": len(shard_entries),
                }
            )
            entry_count += len(shard_entries)
        manifest_categories.append(
            {
                key: category[key]
                for key in (
                    "id",
                    "name_zh",
                    "description",
                    "severity",
                    "disclosure_policy",
                    "enabled",
                    "version",
                    "sources",
                )
            }
            | {"shards": shards}
        )

    manifest = {
        "version": DOCUMENT_VERSION,
        "generation_id": generation_id,
        "generated_at": generated_at,
        "categories": manifest_categories,
    }
    files[Path("manifest.json")] = _json_bytes(manifest)
    pointer = {
        "version": DOCUMENT_VERSION,
        "generation_id": generation_id,
        "manifest": f"generations/{generation_id}/manifest.json",
    }
    return PreparedGeneration(
        generation_id=generation_id,
        catalog_sha256=digest,
        pointer_bytes=_json_bytes(pointer),
        files=files,
        category_count=len(categories),
        entry_count=entry_count,
    )


def _assert_prepared_runtime_limits(prepared: PreparedGeneration) -> None:
    if len(prepared.pointer_bytes) > MAX_POINTER_BYTES:
        raise CatalogImportError("catalog pointer exceeds runtime size limit")
    for relative, payload in prepared.files.items():
        maximum = (
            MAX_MANIFEST_BYTES
            if relative == Path("manifest.json")
            else MAX_SHARD_BYTES
        )
        if len(payload) > maximum:
            raise CatalogImportError("catalog file exceeds runtime size limit")


def _assert_safe_relative_path(value: object, *, label: str) -> Path:
    if not isinstance(value, str):
        raise CatalogImportError(f"{label} must be a string")
    relative = Path(value)
    if (
        relative.is_absolute()
        or not relative.parts
        or ".." in relative.parts
        or "." in relative.parts
    ):
        raise CatalogImportError(f"{label} is unsafe")
    return relative


def _verify_expected_generation(
    generation_root: Path,
    expected_files: dict[Path, bytes],
) -> None:
    metadata = os.lstat(generation_root)
    if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(metadata.st_mode):
        raise CatalogImportError("generation path is not a regular directory")
    actual_files: set[Path] = set()
    for path in generation_root.rglob("*"):
        relative = path.relative_to(generation_root)
        metadata = os.lstat(path)
        if stat.S_ISLNK(metadata.st_mode):
            raise CatalogImportError("generation contains a symbolic link")
        if stat.S_ISREG(metadata.st_mode):
            actual_files.add(relative)
        elif not stat.S_ISDIR(metadata.st_mode):
            raise CatalogImportError("generation contains a special file")
    if actual_files != set(expected_files):
        raise CatalogImportError("generation contents conflict with deterministic build")
    for relative, expected in expected_files.items():
        path = generation_root / relative
        if _read_regular_file(path, label="generation file") != expected:
            raise CatalogImportError("generation contents conflict with deterministic build")
        if stat.S_IMODE(os.lstat(path).st_mode) != PRIVATE_FILE_MODE:
            raise CatalogImportError("generation file permissions are invalid")


def _decode_pointer(payload: bytes) -> dict[str, Any]:
    raw = _decode_json(payload, label="catalog pointer")
    if not isinstance(raw, dict) or set(raw) != {
        "version",
        "generation_id",
        "manifest",
    }:
        raise CatalogImportError("catalog pointer schema is invalid")
    if raw.get("version") != DOCUMENT_VERSION:
        raise CatalogImportError("catalog pointer version is invalid")
    generation_id = raw.get("generation_id")
    if not isinstance(generation_id, str) or _IDENTIFIER_RE.fullmatch(generation_id) is None:
        raise CatalogImportError("catalog pointer generation id is invalid")
    expected_manifest = f"generations/{generation_id}/manifest.json"
    if raw.get("manifest") != expected_manifest:
        raise CatalogImportError("catalog pointer manifest path is invalid")
    return raw


def _verify_pointer_catalog(
    managed_root: Path,
    pointer_bytes: bytes,
    *,
    require_gender_active_review: bool = False,
) -> str:
    pointer = _decode_pointer(pointer_bytes)
    generation_id = pointer["generation_id"]
    manifest_path = managed_root / pointer["manifest"]
    manifest_bytes = _read_regular_file(manifest_path, label="catalog manifest")
    manifest = _decode_json(manifest_bytes, label="catalog manifest")
    if (
        not isinstance(manifest, dict)
        or manifest.get("version") != DOCUMENT_VERSION
        or manifest.get("generation_id") != generation_id
        or not isinstance(manifest.get("categories"), list)
    ):
        raise CatalogImportError("catalog manifest is invalid")
    seen_categories: set[str] = set()
    for category in manifest["categories"]:
        if not isinstance(category, dict):
            raise CatalogImportError("catalog category descriptor is invalid")
        category_id = category.get("id")
        if not isinstance(category_id, str) or _CATEGORY_ID_RE.fullmatch(category_id) is None:
            raise CatalogImportError("catalog category id is invalid")
        if category_id in seen_categories:
            raise CatalogImportError("catalog category id is duplicated")
        seen_categories.add(category_id)
        policy = category.get("disclosure_policy")
        if policy not in DISCLOSURE_POLICIES:
            raise CatalogImportError("catalog disclosure policy is invalid")
        required_policy = KNOWN_CATEGORY_POLICIES.get(category_id)
        if required_policy is not None and policy != required_policy:
            raise CatalogImportError("known category disclosure policy is invalid")
        shards = category.get("shards")
        if not isinstance(shards, list):
            raise CatalogImportError("catalog shard descriptors are invalid")
        for descriptor in shards:
            if not isinstance(descriptor, dict):
                raise CatalogImportError("catalog shard descriptor is invalid")
            relative = _assert_safe_relative_path(
                descriptor.get("path"),
                label="catalog shard path",
            )
            if not relative.parts or relative.parts[0] != "shards":
                raise CatalogImportError("catalog shard path is outside shard directory")
            expected_hash = descriptor.get("sha256")
            if not isinstance(expected_hash, str) or _SHA256_RE.fullmatch(expected_hash) is None:
                raise CatalogImportError("catalog shard hash is invalid")
            shard_bytes = _read_regular_file(
                manifest_path.parent / relative,
                label="catalog shard",
            )
            if hashlib.sha256(shard_bytes).hexdigest() != expected_hash:
                raise CatalogImportError("catalog shard hash mismatch")
            shard = _decode_json(shard_bytes, label="catalog shard")
            if (
                not isinstance(shard, dict)
                or shard.get("version") != DOCUMENT_VERSION
                or shard.get("generation_id") != generation_id
                or shard.get("category_id") != category_id
                or not isinstance(shard.get("entries"), list)
                or descriptor.get("entry_count") != len(shard["entries"])
            ):
                raise CatalogImportError("catalog shard schema is invalid")
            if require_gender_active_review and category_id == "gender_conflict":
                for entry in shard["entries"]:
                    if not isinstance(entry, dict):
                        raise CatalogImportError("catalog shard entry is invalid")
                    if entry.get("status") != "active":
                        continue
                    tags = entry.get("tags")
                    if (
                        not isinstance(tags, list)
                        or GENDER_ACTIVE_REVIEW_TAG not in tags
                    ):
                        raise CatalogImportError(
                            "rollback target contains unreviewed active gender entry"
                        )
    if not REQUIRED_CATEGORY_IDS.issubset(seen_categories):
        raise CatalogImportError("catalog is missing required categories")
    return generation_id


def _verify_runtime_pointer(
    managed_root: Path,
    pointer_bytes: bytes,
    *,
    expected_generation_id: str,
) -> None:
    """Load a staged pointer through the real runtime validator.

    The temporary pointer lives beside ``current.json`` so relative manifest
    paths and trust boundaries are identical to production.  Only a complete
    active snapshot for the expected immutable generation is accepted.
    """

    temporary = managed_root / f".runtime-verify-{secrets.token_hex(8)}.json"
    try:
        _write_new_private_file(temporary, pointer_bytes)
        catalog = ManagedKeywordCatalog(temporary)
        snapshot = catalog.snapshot()
        if (
            not snapshot.has_active_generation
            or snapshot.generation_id != expected_generation_id
            or catalog.last_error is not None
        ):
            raise CatalogImportError("runtime catalog verification failed")
    finally:
        try:
            metadata = os.lstat(temporary)
        except FileNotFoundError:
            pass
        else:
            if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISREG(metadata.st_mode):
                raise CatalogImportError("runtime verification file became unsafe")
            temporary.unlink()


def _restore_pointer(
    current_path: Path,
    previous_bytes: bytes | None,
) -> None:
    if previous_bytes is not None:
        _atomic_write_private(current_path, previous_bytes)
        restored = _read_regular_file(current_path, label="restored catalog pointer")
        if restored != previous_bytes:
            raise CatalogImportError("catalog pointer restoration failed")
        return
    _assert_regular_or_missing(current_path)
    try:
        current_path.unlink()
    except FileNotFoundError:
        return
    _fsync_directory(current_path.parent)


def _publish_pointer(
    managed_root: Path,
    current_path: Path,
    pointer_bytes: bytes,
    *,
    previous_bytes: bytes | None,
    expected_generation_id: str,
    require_gender_active_review: bool = False,
) -> None:
    try:
        _atomic_write_private(current_path, pointer_bytes)
        published = _read_regular_file(current_path, label="catalog pointer")
        if published != pointer_bytes:
            raise CatalogImportError(
                "catalog pointer publication verification failed"
            )
        published_generation = _verify_pointer_catalog(
            managed_root,
            published,
            require_gender_active_review=require_gender_active_review,
        )
        if published_generation != expected_generation_id:
            raise CatalogImportError(
                "catalog pointer publication verification failed"
            )
    except Exception as exc:
        try:
            _restore_pointer(current_path, previous_bytes)
        except Exception as restore_exc:
            raise CatalogImportError(
                "catalog pointer publication and restoration failed"
            ) from restore_exc
        raise CatalogImportError(
            "catalog pointer publication failed; previous pointer restored"
        ) from exc


def _write_generation(
    generations_root: Path,
    prepared: PreparedGeneration,
) -> Path:
    destination = generations_root / prepared.generation_id
    _assert_directory_or_missing(destination)
    if destination.exists():
        _verify_expected_generation(destination, prepared.files)
        return destination

    temporary = generations_root / (
        f".{prepared.generation_id}.tmp-{os.getpid()}-{secrets.token_hex(6)}"
    )
    os.mkdir(temporary, PRIVATE_DIRECTORY_MODE)
    try:
        shards = temporary / "shards"
        os.mkdir(shards, PRIVATE_DIRECTORY_MODE)
        os.chmod(temporary, PRIVATE_DIRECTORY_MODE, follow_symlinks=False)
        os.chmod(shards, PRIVATE_DIRECTORY_MODE, follow_symlinks=False)
        for relative, payload in prepared.files.items():
            target = temporary / relative
            _write_new_private_file(target, payload)
        _fsync_directory(shards)
        _fsync_directory(temporary)
        _verify_expected_generation(temporary, prepared.files)
        os.replace(temporary, destination)
        _fsync_directory(generations_root)
    finally:
        try:
            if temporary.exists() and not temporary.is_symlink():
                shutil.rmtree(temporary)
        except OSError:
            pass
    _verify_expected_generation(destination, prepared.files)
    return destination


def _write_backup_file(path: Path, payload: bytes) -> None:
    _assert_regular_or_missing(path)
    if path.exists():
        if _read_regular_file(path, label="catalog backup") != payload:
            raise CatalogImportError("existing catalog backup conflicts")
        if stat.S_IMODE(os.lstat(path).st_mode) != PRIVATE_FILE_MODE:
            raise CatalogImportError("catalog backup permissions are invalid")
        return
    _write_new_private_file(path, payload)


def _backup_before_activation(
    instance_root: Path,
    prepared: PreparedGeneration,
    *,
    current_bytes: bytes | None,
    legacy_bytes: bytes | None,
) -> Path:
    backups_parent = instance_root / "backups"
    backup_root = backups_parent / "content-alert"
    transaction = backup_root / f"before-{prepared.generation_id}"
    _ensure_directory(
        backups_parent,
        trusted_root=instance_root,
        enforce_private_mode=False,
    )
    _ensure_directory(
        backup_root,
        trusted_root=instance_root,
        enforce_private_mode=True,
    )
    _ensure_directory(
        transaction,
        trusted_root=instance_root,
        enforce_private_mode=True,
    )
    if current_bytes is None:
        _write_backup_file(transaction / "current.absent", b"absent\n")
    else:
        _write_backup_file(transaction / "current.json", current_bytes)
    if legacy_bytes is not None:
        _write_backup_file(transaction / "background_keywords.json", legacy_bytes)
    _fsync_directory(transaction)
    _fsync_directory(backup_root)
    return transaction


@contextmanager
def _catalog_lock(managed_root: Path) -> Iterator[None]:
    lock_path = managed_root / "import.lock"
    _assert_regular_or_missing(lock_path)
    flags = os.O_RDWR | os.O_CREAT
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    descriptor = os.open(lock_path, flags, PRIVATE_FILE_MODE)
    try:
        os.fchmod(descriptor, PRIVATE_FILE_MODE)
        fcntl.flock(descriptor, fcntl.LOCK_EX)
        yield
    finally:
        try:
            fcntl.flock(descriptor, fcntl.LOCK_UN)
        finally:
            os.close(descriptor)


def import_catalog(*, instance_root: Path, build_path: Path) -> tuple[str, PreparedGeneration]:
    root = _assert_instance_root(Path(instance_root))
    build_bytes = _read_regular_file(Path(build_path), label="private build")
    generated_at, categories = _validate_build(
        _decode_json(build_bytes, label="private build")
    )
    legacy_bytes, legacy_rules = _load_legacy_rules(root)
    categories = _merge_legacy_rules(
        categories,
        legacy_rules,
        generated_at=generated_at,
    )
    _assert_merged_pattern_limit(categories)
    prepared = _prepare_generation(generated_at, categories)
    _assert_prepared_runtime_limits(prepared)

    data_root = root / "data"
    content_alert_root = data_root / "content_alert"
    managed_root = content_alert_root / "managed"
    generations_root = managed_root / "generations"
    _ensure_directory(data_root, trusted_root=root, enforce_private_mode=False)
    _ensure_directory(
        content_alert_root,
        trusted_root=root,
        enforce_private_mode=False,
    )
    _ensure_directory(
        managed_root,
        trusted_root=root,
        enforce_private_mode=True,
    )
    _ensure_directory(
        generations_root,
        trusted_root=root,
        enforce_private_mode=True,
    )
    current_path = managed_root / "current.json"

    with _catalog_lock(managed_root):
        _assert_regular_or_missing(current_path)
        current_bytes = (
            _read_regular_file(current_path, label="catalog pointer")
            if current_path.exists()
            else None
        )
        if current_bytes is not None:
            current_generation = _verify_pointer_catalog(managed_root, current_bytes)
            _verify_runtime_pointer(
                managed_root,
                current_bytes,
                expected_generation_id=current_generation,
            )

        destination = generations_root / prepared.generation_id
        if destination.exists():
            _verify_expected_generation(destination, prepared.files)
            if current_bytes == prepared.pointer_bytes:
                return "unchanged", prepared
        else:
            _write_generation(generations_root, prepared)

        _verify_expected_generation(destination, prepared.files)
        _verify_runtime_pointer(
            managed_root,
            prepared.pointer_bytes,
            expected_generation_id=prepared.generation_id,
        )
        _backup_before_activation(
            root,
            prepared,
            current_bytes=current_bytes,
            legacy_bytes=legacy_bytes,
        )
        _publish_pointer(
            managed_root,
            current_path,
            prepared.pointer_bytes,
            previous_bytes=current_bytes,
            expected_generation_id=prepared.generation_id,
        )
        return "activated", prepared


def rollback_catalog(*, instance_root: Path) -> str:
    root = _assert_instance_root(Path(instance_root))
    managed_root = root / "data" / "content_alert" / "managed"
    _assert_existing_path_chain(managed_root, leaf_kind="directory")
    current_path = managed_root / "current.json"
    with _catalog_lock(managed_root):
        current_bytes = _read_regular_file(current_path, label="catalog pointer")
        current_generation = _verify_pointer_catalog(managed_root, current_bytes)
        transaction = (
            root
            / "backups"
            / "content-alert"
            / f"before-{current_generation}"
        )
        _assert_existing_path_chain(transaction, leaf_kind="directory")
        previous = transaction / "current.json"
        absent = transaction / "current.absent"
        _assert_regular_or_missing(previous)
        _assert_regular_or_missing(absent)
        if previous.exists() == absent.exists():
            raise CatalogImportError("rollback backup state is invalid")
        if previous.exists():
            previous_bytes = _read_regular_file(previous, label="previous pointer")
            previous_generation = _verify_pointer_catalog(
                managed_root,
                previous_bytes,
                require_gender_active_review=True,
            )
            _verify_runtime_pointer(
                managed_root,
                previous_bytes,
                expected_generation_id=previous_generation,
            )
            _publish_pointer(
                managed_root,
                current_path,
                previous_bytes,
                previous_bytes=current_bytes,
                expected_generation_id=previous_generation,
                require_gender_active_review=True,
            )
            return previous_generation
        _restore_pointer(current_path, None)
        return "absent"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Safely import an instance-private content-alert catalog"
    )
    parser.add_argument("--instance-root", required=True, type=Path)
    parser.add_argument("--build", type=Path)
    modes = parser.add_mutually_exclusive_group(required=True)
    modes.add_argument("--apply", action="store_true")
    modes.add_argument("--rollback", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        if args.apply:
            if args.build is None:
                raise CatalogImportError("apply requires a private build document")
            status, prepared = import_catalog(
                instance_root=args.instance_root,
                build_path=args.build,
            )
            print(
                f"catalog_import={status} generation_id={prepared.generation_id} "
                f"category_count={prepared.category_count} "
                f"entry_count={prepared.entry_count} "
                f"catalog_sha256={prepared.catalog_sha256}"
            )
        else:
            generation_id = rollback_catalog(instance_root=args.instance_root)
            print(f"catalog_rollback=ok generation_id={generation_id}")
        return 0
    except (CatalogImportError, OSError, TypeError, ValueError) as exc:
        print(
            f"catalog_import=failed error={type(exc).__name__}",
            file=sys.stderr,
        )
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
