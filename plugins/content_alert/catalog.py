from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
import stat
import unicodedata
from bisect import bisect_left, bisect_right
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from threading import RLock
from types import MappingProxyType
from typing import Any

from .engine import (
    ScalableLiteralMatch,
    ScalableLiteralMatcher,
    ScalableLiteralScanLimitError,
)
from .rules import (
    MAX_NORMALIZED_PATTERN_LENGTH,
    MIN_NORMALIZED_PATTERN_LENGTH,
    is_ignored_literal_character,
    normalize_literal_text,
)

CATALOG_VERSION = 2
SUPPORTED_CATALOG_VERSIONS = frozenset({1, CATALOG_VERSION})
STRICT_HIDDEN = "strict_hidden"
MANAGEMENT_VISIBLE = "management_visible"
DIRECT_MATCH = "direct"
SAME_SEGMENT_CONTEXT_MATCH = "same_segment_context"
SUPPORT_ONLY_MATCH = "support_only"
STRONG_CONTEXT = "strong"
MAX_CONTEXT_GAP = 12
MAX_MANAGED_PATTERNS = 50_000
MAX_STORED_PATTERNS = 100_000
MAX_MANAGED_TRIE_NODES = 500_000
MAX_MANAGED_SCAN_TEXT_CHARS = 16_384
MAX_MANAGED_SCAN_CANDIDATES = 20_000
MAX_MANAGED_MESSAGE_SEGMENTS = 256
MAX_MANAGED_MESSAGE_TEXT_CHARS = MAX_MANAGED_SCAN_TEXT_CHARS
MAX_MANAGED_MESSAGE_MATCHES = MAX_MANAGED_SCAN_CANDIDATES
MAX_MANAGED_CONTEXT_COMPARISONS = 100_000
MAX_POINTER_BYTES = 64 * 1024
MAX_MANIFEST_BYTES = 2 * 1024 * 1024
MAX_SHARD_BYTES = 32 * 1024 * 1024

_MAX_CATEGORIES = 64
_MAX_SOURCES_PER_CATEGORY = 64
_MAX_SHARDS = 512
_MAX_ALIASES_PER_ENTRY = 32
_MAX_MANAGED_ENTRIES = 100_000
_IDENTIFIER_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}\Z")
_SHA256_RE = re.compile(r"[0-9a-f]{64}\Z")
# ``political_cn`` is intentionally omitted: the runtime loader accepts the
# previous hidden policy and the new management-visible policy across an
# atomic rollout.  The catalog importer owns the policy for new generations.
_KNOWN_CATEGORY_POLICIES = {
    "sexual_explicit": MANAGEMENT_VISIBLE,
    "gender_conflict": MANAGEMENT_VISIBLE,
    "controversial_topics": MANAGEMENT_VISIBLE,
    "anime_game_controversy": MANAGEMENT_VISIBLE,
    "graphic_violence": MANAGEMENT_VISIBLE,
    "terrorism": MANAGEMENT_VISIBLE,
}
_DISCLOSURE_POLICIES = frozenset({STRICT_HIDDEN, MANAGEMENT_VISIBLE})
_SEVERITIES = frozenset({"low", "medium", "high", "critical"})
_ENTRY_STATUSES = frozenset({"active", "shadow", "disabled"})
_POLITICAL_SUBJECT_MATCH_MODES = {
    "historical_event": DIRECT_MATCH,
    "leader_name": SAME_SEGMENT_CONTEXT_MATCH,
    "political_context": SUPPORT_ONLY_MATCH,
}
_CONTEXT_STRENGTHS = frozenset({STRONG_CONTEXT, "weak"})
_CONTEXT_CLASSES = frozenset(
    {
        "office_title",
        "case_proceeding",
        "political_institution",
        "historical_reference",
    }
)
_REQUIRED_V2_CATEGORY_IDS = frozenset(
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
_POLITICAL_REVIEW_TAG = "human-reviewed-political-scope"
_POLITICAL_SOURCE_SCREENED_TAG = "source-screened-political-scope"
_POLITICAL_SUBJECT_TAGS = frozenset(
    {
        "subject:leader_name",
        "subject:historical_event",
        "subject:political_context",
    }
)
_POLITICAL_RANK_LEVELS = frozenset(
    {"国家级正职", "国家级副职", "省部级正职", "省部级副职"}
)
_POLITICAL_VERIFICATION_STATUSES = frozenset(
    {"official_verified", "research_candidate", "operator_curated"}
)


@dataclass(frozen=True)
class ManagedKeywordSource:
    source_id: str
    reference: str
    license: str
    retrieved_at: str
    revision: str = ""
    sha256: str = ""


@dataclass(frozen=True)
class ManagedKeywordCategory:
    category_id: str
    name_zh: str
    description: str
    severity: str
    disclosure_policy: str
    version: str
    sources: tuple[ManagedKeywordSource, ...]


@dataclass(frozen=True)
class ManagedKeywordEntry:
    entry_ids: tuple[str, ...]
    term: str
    aliases: tuple[str, ...]
    category_ids: tuple[str, ...]
    category_names: tuple[str, ...]
    disclosure_policy: str
    source_refs: tuple[str, ...]
    subject_type: str
    match_mode: str
    context_class: str
    context_strength: str
    entity_ref: str
    entity_refs: tuple[str, ...]


@dataclass(frozen=True)
class ManagedKeywordMatch:
    term: str
    category_ids: tuple[str, ...]
    category_names: tuple[str, ...]
    disclosure_policy: str
    entry_ids: tuple[str, ...]
    source_refs: tuple[str, ...]
    start: int
    end: int
    subject_type: str
    match_mode: str
    context_class: str
    context_strength: str
    context_term: str
    segment_index: int
    entity_ref: str
    entity_refs: tuple[str, ...]


@dataclass(frozen=True)
class ManagedCatalogError:
    """Sanitized refresh error safe for server-side status reporting."""

    error_type: str
    generation_id: str = ""

    def __str__(self) -> str:
        generation = self.generation_id or "unknown"
        return f"managed catalog error={self.error_type} generation={generation}"


@dataclass(frozen=True)
class ManagedCatalogSnapshot:
    catalog_version: int
    generation_id: str
    generated_at: str
    categories: tuple[ManagedKeywordCategory, ...]
    entries: tuple[ManagedKeywordEntry, ...]
    _matcher: ScalableLiteralMatcher | None = field(
        default=None,
        repr=False,
        compare=False,
    )
    _match_metadata: Mapping[str, ManagedKeywordMatch] = field(
        default_factory=lambda: MappingProxyType({}),
        repr=False,
        compare=False,
    )

    @property
    def has_active_generation(self) -> bool:
        return bool(self.generation_id)


@dataclass
class _EntryBuilder:
    term: str
    entry_ids: list[str]
    aliases: list[str]
    category_ids: list[str]
    category_names: list[str]
    source_refs: list[str]
    disclosure_policy: str
    subject_type: str
    match_mode: str
    context_class: str
    context_strength: str
    entity_ref: str
    entity_refs: tuple[str, ...]


@dataclass
class _MatchBuilder:
    term: str
    entry_ids: list[str]
    category_ids: list[str]
    category_names: list[str]
    source_refs: list[str]
    disclosure_policy: str
    subject_type: str
    match_mode: str
    context_class: str
    context_strength: str
    entity_ref: str
    entity_refs: tuple[str, ...]


@dataclass(frozen=True)
class _DecodedEntry:
    entry_id: str
    term: str
    aliases: tuple[str, ...]
    status: str
    source_ref: str
    source_refs: tuple[str, ...]
    subject_type: str
    match_mode: str
    context_class: str
    context_strength: str
    entity_ref: str
    entity_refs: tuple[str, ...]


@dataclass(frozen=True)
class _CatalogValidationError(Exception):
    code: str
    generation_id: str = ""


_EMPTY_SNAPSHOT = ManagedCatalogSnapshot(
    catalog_version=0,
    generation_id="",
    generated_at="",
    categories=(),
    entries=(),
)


class ManagedKeywordCatalog:
    """Read an atomic, immutable managed-keyword generation.

    A pointer change is validated completely before publication.  Any failure
    leaves the previous in-memory generation untouched, and failures expose
    only a bounded error class and already-validated generation identifier.
    """

    def __init__(self, current_path: Path):
        self._current_path = _absolute_path(Path(current_path))
        self._managed_root = self._current_path.parent
        self._trusted_root = _default_trusted_root(self._current_path)
        self._lock = RLock()
        self._snapshot = _EMPTY_SNAPSHOT
        self._manifest_reference = ""
        self._loaded_fingerprint: tuple[object, ...] | None = None
        self._failed_fingerprint: tuple[object, ...] | None = None
        self._last_error: ManagedCatalogError | None = None

    @property
    def path(self) -> Path:
        return self._current_path

    @property
    def last_error(self) -> ManagedCatalogError | None:
        with self._lock:
            return self._last_error

    def snapshot(self) -> ManagedCatalogSnapshot:
        with self._lock:
            fingerprint, probe_error = self._probe_pointer()
            if fingerprint == self._loaded_fingerprint:
                return self._snapshot
            if fingerprint == self._failed_fingerprint:
                return self._snapshot
            if probe_error is not None:
                self._record_failure(probe_error, fingerprint)
                return self._snapshot

            try:
                loaded, manifest_reference = self._load_generation(fingerprint)
            except _CatalogValidationError as exc:
                error = ManagedCatalogError(exc.code, exc.generation_id)
                self._record_failure(error, fingerprint)
                return self._snapshot
            except (
                OSError,
                UnicodeError,
                json.JSONDecodeError,
                RecursionError,
                TypeError,
                ValueError,
            ):
                self._record_failure(
                    ManagedCatalogError("invalid_generation"),
                    fingerprint,
                )
                return self._snapshot

            self._snapshot = loaded
            self._manifest_reference = manifest_reference
            self._loaded_fingerprint = fingerprint
            self._failed_fingerprint = None
            self._last_error = None
            return self._snapshot

    def match_message(self, message: object) -> tuple[ManagedKeywordMatch, ...]:
        snapshot = self.snapshot()
        return self.match_snapshot(snapshot, message)

    def scan_message(
        self,
        message: object,
    ) -> tuple[bool, tuple[ManagedKeywordMatch, ...]]:
        """Atomically select one immutable snapshot and scan against it."""

        snapshot = self.snapshot()
        return (
            snapshot.has_active_generation,
            self.match_snapshot(snapshot, message),
        )

    def match_snapshot(
        self,
        snapshot: ManagedCatalogSnapshot,
        message: object,
    ) -> tuple[ManagedKeywordMatch, ...]:
        matcher = snapshot._matcher
        if matcher is None:
            return ()

        selected: dict[
            str,
            tuple[
                tuple[int, ...],
                ScalableLiteralMatch,
                ScalableLiteralMatch | None,
                int,
            ],
        ] = {}
        context_comparisons = 0
        try:
            segments = iter(message)  # type: ignore[arg-type]
        except TypeError:
            return ()
        message_text_chars = 0
        message_match_count = 0
        for segment_index, segment in enumerate(segments):
            if segment_index >= MAX_MANAGED_MESSAGE_SEGMENTS:
                raise ScalableLiteralScanLimitError("message_segment_limit")
            if getattr(segment, "type", None) != "text":
                continue
            data = getattr(segment, "data", None)
            if not isinstance(data, dict):
                continue
            text = data.get("text")
            if not isinstance(text, str):
                continue
            message_text_chars += len(text)
            if message_text_chars > MAX_MANAGED_MESSAGE_TEXT_CHARS:
                raise ScalableLiteralScanLimitError("message_text_limit")
            literal_matches = matcher.match_text_all(text)
            message_match_count += len(literal_matches)
            if message_match_count > MAX_MANAGED_MESSAGE_MATCHES:
                raise ScalableLiteralScanLimitError("message_match_limit")
            strong_contexts = tuple(
                literal_match
                for literal_match in literal_matches
                if (
                    snapshot._match_metadata[literal_match.key].match_mode
                    == SUPPORT_ONLY_MATCH
                    and snapshot._match_metadata[literal_match.key].context_strength
                    == STRONG_CONTEXT
                )
            )
            strong_context_starts = tuple(context.start for context in strong_contexts)
            for literal_match in literal_matches:
                metadata = snapshot._match_metadata[literal_match.key]
                if metadata.match_mode == SUPPORT_ONLY_MATCH:
                    continue
                context_match: ScalableLiteralMatch | None = None
                if metadata.match_mode == SAME_SEGMENT_CONTEXT_MATCH:
                    context_left = bisect_left(
                        strong_context_starts,
                        max(
                            0,
                            literal_match.start
                            - MAX_CONTEXT_GAP
                            - MAX_NORMALIZED_PATTERN_LENGTH,
                        ),
                    )
                    context_right = bisect_right(
                        strong_context_starts,
                        literal_match.end + MAX_CONTEXT_GAP,
                    )
                    context_comparisons += context_right - context_left
                    if context_comparisons > MAX_MANAGED_CONTEXT_COMPARISONS:
                        raise ScalableLiteralScanLimitError("context_comparison_limit")
                    context_match = _nearest_strong_context(
                        literal_match,
                        (
                            candidate
                            for candidate in strong_contexts[context_left:context_right]
                            if _context_applies_to_subject(
                                subject=metadata,
                                context=snapshot._match_metadata[candidate.key],
                            )
                        ),
                    )
                    if context_match is None:
                        continue
                    selection_rank = (
                        _normalized_gap(literal_match, context_match),
                        segment_index,
                        literal_match.start,
                        context_match.start,
                        -(context_match.end - context_match.start),
                    )
                else:
                    selection_rank = (
                        segment_index,
                        literal_match.start,
                        -(literal_match.end - literal_match.start),
                    )
                previous = selected.get(literal_match.key)
                if previous is None or selection_rank < previous[0]:
                    selected[literal_match.key] = (
                        selection_rank,
                        literal_match,
                        context_match,
                        segment_index,
                    )

        matches: list[ManagedKeywordMatch] = []
        ordered_selected = sorted(
            selected.values(),
            key=lambda item: (
                item[3],
                min(
                    item[1].start,
                    item[2].start if item[2] is not None else item[1].start,
                ),
                item[1].key,
            ),
        )
        for _rank, literal_match, context_match, segment_index in ordered_selected:
            metadata = snapshot._match_metadata[literal_match.key]
            context_metadata = (
                snapshot._match_metadata[context_match.key]
                if context_match is not None
                else None
            )
            matches.append(
                ManagedKeywordMatch(
                    term=metadata.term,
                    category_ids=metadata.category_ids,
                    category_names=metadata.category_names,
                    disclosure_policy=metadata.disclosure_policy,
                    entry_ids=metadata.entry_ids,
                    source_refs=metadata.source_refs,
                    start=(
                        min(literal_match.start, context_match.start)
                        if context_match is not None
                        else literal_match.start
                    ),
                    end=(
                        max(literal_match.end, context_match.end)
                        if context_match is not None
                        else literal_match.end
                    ),
                    subject_type=metadata.subject_type,
                    match_mode=metadata.match_mode,
                    context_class=(
                        context_metadata.context_class
                        if context_metadata is not None
                        else ""
                    ),
                    context_strength=(
                        context_metadata.context_strength
                        if context_metadata is not None
                        else ""
                    ),
                    context_term=(
                        context_metadata.term if context_metadata is not None else ""
                    ),
                    segment_index=segment_index,
                    entity_ref=metadata.entity_ref,
                    entity_refs=(
                        context_metadata.entity_refs
                        if context_metadata is not None
                        else ()
                    ),
                )
            )
        return tuple(matches)

    def _probe_pointer(
        self,
    ) -> tuple[tuple[object, ...], ManagedCatalogError | None]:
        try:
            _assert_safe_path(self._current_path, self._trusted_root)
            metadata = os.lstat(self._current_path)
            if not stat.S_ISREG(metadata.st_mode):
                return _raw_fingerprint(metadata, "unsafe"), ManagedCatalogError(
                    "unsafe_pointer"
                )
            return _raw_fingerprint(metadata, "file"), None
        except FileNotFoundError:
            return ("missing",), ManagedCatalogError("missing_pointer")
        except OSError:
            try:
                metadata = os.lstat(self._current_path)
            except OSError:
                return ("unsafe",), ManagedCatalogError("unsafe_pointer")
            return _raw_fingerprint(metadata, "unsafe"), ManagedCatalogError(
                "unsafe_pointer"
            )

    def _record_failure(
        self,
        error: ManagedCatalogError,
        fingerprint: tuple[object, ...],
    ) -> None:
        self._last_error = error
        self._failed_fingerprint = fingerprint

    def _load_generation(
        self,
        expected_pointer_fingerprint: tuple[object, ...],
    ) -> tuple[ManagedCatalogSnapshot, str]:
        pointer_raw, pointer_fingerprint = _read_regular_bytes(
            self._current_path,
            trusted_root=self._trusted_root,
            max_bytes=MAX_POINTER_BYTES,
        )
        if pointer_fingerprint != expected_pointer_fingerprint:
            raise _CatalogValidationError("pointer_changed")
        pointer = _decode_json_object(pointer_raw, code="invalid_pointer_json")
        catalog_version = pointer.get("version")
        if (
            isinstance(catalog_version, bool)
            or not isinstance(catalog_version, int)
            or catalog_version not in SUPPORTED_CATALOG_VERSIONS
        ):
            raise _CatalogValidationError("unsupported_pointer_version")

        generation_id = _identifier(
            pointer.get("generation_id"),
            code="invalid_generation_id",
        )
        manifest_reference = _relative_path(
            pointer.get("manifest"),
            code="invalid_manifest_reference",
        )
        expected_manifest = (
            Path("generations") / generation_id / "manifest.json"
        ).as_posix()
        if manifest_reference != expected_manifest:
            raise _CatalogValidationError(
                "invalid_manifest_reference",
                generation_id,
            )

        # Returning to an already validated immutable generation does not
        # reread or recompile its shards.  The new pointer itself was still
        # safely opened and validated above.
        if (
            self._snapshot.has_active_generation
            and catalog_version == self._snapshot.catalog_version
            and generation_id == self._snapshot.generation_id
            and manifest_reference == self._manifest_reference
        ):
            return self._snapshot, manifest_reference

        manifest_path = _join_relative(
            self._managed_root,
            manifest_reference,
            trusted_root=self._trusted_root,
            code="invalid_manifest_reference",
            generation_id=generation_id,
        )
        manifest_raw, _ = _read_regular_bytes(
            manifest_path,
            trusted_root=self._trusted_root,
            max_bytes=MAX_MANIFEST_BYTES,
            generation_id=generation_id,
        )
        manifest = _decode_json_object(
            manifest_raw,
            code="invalid_manifest_json",
            generation_id=generation_id,
        )
        return (
            self._decode_manifest(
                manifest,
                catalog_version=int(catalog_version),
                generation_id=generation_id,
                generation_root=manifest_path.parent,
            ),
            manifest_reference,
        )

    def _decode_manifest(
        self,
        manifest: Mapping[str, Any],
        *,
        catalog_version: int,
        generation_id: str,
        generation_root: Path,
    ) -> ManagedCatalogSnapshot:
        manifest_version = manifest.get("version")
        if (
            isinstance(manifest_version, bool)
            or not isinstance(manifest_version, int)
            or manifest_version != catalog_version
        ):
            raise _CatalogValidationError(
                "unsupported_manifest_version",
                generation_id,
            )
        if manifest.get("generation_id") != generation_id:
            raise _CatalogValidationError("generation_mismatch", generation_id)
        generated_at = _timestamp(
            manifest.get("generated_at"),
            code="invalid_generated_at",
            generation_id=generation_id,
        )
        raw_categories = manifest.get("categories")
        if (
            not isinstance(raw_categories, list)
            or not raw_categories
            or len(raw_categories) > _MAX_CATEGORIES
        ):
            raise _CatalogValidationError("invalid_categories", generation_id)

        categories: list[ManagedKeywordCategory] = []
        category_order: dict[str, int] = {}
        decoded_categories: list[
            tuple[Mapping[str, Any], ManagedKeywordCategory, bool]
        ] = []
        category_ids: set[str] = set()
        shard_references: set[str] = set()
        declared_entry_count = 0
        political_category_enabled: bool | None = None

        for raw_category in raw_categories:
            if not isinstance(raw_category, dict):
                raise _CatalogValidationError("invalid_category", generation_id)
            category = _decode_category(
                raw_category,
                catalog_version=catalog_version,
                generation_id=generation_id,
            )
            if category.category_id in category_ids:
                raise _CatalogValidationError(
                    "duplicate_category_id",
                    generation_id,
                )
            category_ids.add(category.category_id)
            enabled = raw_category.get("enabled")
            if not isinstance(enabled, bool):
                raise _CatalogValidationError("invalid_category", generation_id)
            if category.category_id == "political_cn":
                political_category_enabled = enabled
            shards = raw_category.get("shards")
            if not isinstance(shards, list) or len(shards) > _MAX_SHARDS:
                raise _CatalogValidationError("invalid_shards", generation_id)
            if not shards:
                raise _CatalogValidationError("invalid_shards", generation_id)

            for descriptor in shards:
                reference, entry_count, _digest = _decode_shard_descriptor(
                    descriptor,
                    generation_id=generation_id,
                )
                if not reference.startswith("shards/"):
                    raise _CatalogValidationError(
                        "invalid_shard_reference",
                        generation_id,
                    )
                if reference in shard_references:
                    raise _CatalogValidationError(
                        "duplicate_shard_reference",
                        generation_id,
                    )
                shard_references.add(reference)
                declared_entry_count += entry_count
                if declared_entry_count > _MAX_MANAGED_ENTRIES:
                    raise _CatalogValidationError(
                        "managed_entry_limit",
                        generation_id,
                    )

            if enabled:
                category_order[category.category_id] = len(categories)
                categories.append(category)
            decoded_categories.append((raw_category, category, enabled))

        if catalog_version >= 2 and not _REQUIRED_V2_CATEGORY_IDS.issubset(
            category_ids
        ):
            raise _CatalogValidationError(
                "missing_required_categories",
                generation_id,
            )
        if catalog_version >= 2 and political_category_enabled is not True:
            raise _CatalogValidationError(
                "dead_political_generation",
                generation_id,
            )

        entry_builders: dict[str, _EntryBuilder] = {}
        match_builders: dict[str, _MatchBuilder] = {}
        entry_ids: set[str] = set()
        leader_entity_refs: set[str] = set()
        referenced_leader_entities: set[str] = set()
        strong_context_entity_refs: set[str] = set()
        has_global_strong_context = False
        active_event_count = 0
        active_pattern_count = 0
        stored_pattern_count = 0

        for raw_category, category, enabled in decoded_categories:
            sources_by_id = {source.source_id: source for source in category.sources}
            for descriptor in raw_category["shards"]:
                reference, entry_count, expected_digest = _decode_shard_descriptor(
                    descriptor,
                    generation_id=generation_id,
                )
                shard_path = _join_relative(
                    generation_root,
                    reference,
                    trusted_root=self._trusted_root,
                    code="invalid_shard_reference",
                    generation_id=generation_id,
                )
                shard_raw, _ = _read_regular_bytes(
                    shard_path,
                    trusted_root=self._trusted_root,
                    max_bytes=MAX_SHARD_BYTES,
                    generation_id=generation_id,
                )
                actual_digest = hashlib.sha256(shard_raw).hexdigest()
                if not hmac.compare_digest(actual_digest, expected_digest):
                    raise _CatalogValidationError(
                        "shard_hash_mismatch",
                        generation_id,
                    )
                shard = _decode_json_object(
                    shard_raw,
                    code="invalid_shard_json",
                    generation_id=generation_id,
                )
                shard_version = shard.get("version")
                if (
                    isinstance(shard_version, bool)
                    or not isinstance(shard_version, int)
                    or shard_version != catalog_version
                    or shard.get("generation_id") != generation_id
                    or shard.get("category_id") != category.category_id
                ):
                    raise _CatalogValidationError(
                        "shard_metadata_mismatch",
                        generation_id,
                    )
                raw_entries = shard.get("entries")
                if not isinstance(raw_entries, list) or len(raw_entries) != entry_count:
                    raise _CatalogValidationError(
                        "shard_entry_count_mismatch",
                        generation_id,
                    )

                for raw_entry in raw_entries:
                    decoded = _decode_entry(
                        raw_entry,
                        catalog_version=catalog_version,
                        category_id=category.category_id,
                        generation_id=generation_id,
                        sources_by_id=sources_by_id,
                        category_enabled=enabled,
                    )
                    entry_id = decoded.entry_id
                    term = decoded.term
                    aliases = decoded.aliases
                    status = decoded.status
                    if entry_id in entry_ids:
                        raise _CatalogValidationError(
                            "duplicate_entry_id",
                            generation_id,
                        )
                    entry_ids.add(entry_id)
                    stored_pattern_count += 1 + len(aliases)
                    if stored_pattern_count > MAX_STORED_PATTERNS:
                        raise _CatalogValidationError(
                            "stored_pattern_limit",
                            generation_id,
                        )
                    if not enabled or status != "active":
                        continue

                    if decoded.entity_ref:
                        if decoded.entity_ref in leader_entity_refs:
                            raise _CatalogValidationError(
                                "duplicate_political_entity_reference",
                                generation_id,
                            )
                        leader_entity_refs.add(decoded.entity_ref)
                    referenced_leader_entities.update(decoded.entity_refs)
                    if decoded.subject_type == "historical_event":
                        active_event_count += 1
                    elif (
                        decoded.match_mode == SUPPORT_ONLY_MATCH
                        and decoded.context_strength == STRONG_CONTEXT
                    ):
                        if decoded.entity_refs:
                            strong_context_entity_refs.update(decoded.entity_refs)
                        else:
                            has_global_strong_context = True

                    normalized_term = normalize_literal_text(term)
                    entry_builder = entry_builders.get(normalized_term)
                    if entry_builder is None:
                        entry_builder = _EntryBuilder(
                            term=term,
                            entry_ids=[],
                            aliases=[],
                            category_ids=[],
                            category_names=[],
                            source_refs=[],
                            disclosure_policy=MANAGEMENT_VISIBLE,
                            subject_type=decoded.subject_type,
                            match_mode=decoded.match_mode,
                            context_class=decoded.context_class,
                            context_strength=decoded.context_strength,
                            entity_ref=decoded.entity_ref,
                            entity_refs=decoded.entity_refs,
                        )
                        entry_builders[normalized_term] = entry_builder
                    _require_compatible_semantics(
                        entry_builder,
                        decoded,
                        generation_id=generation_id,
                    )
                    _merge_builder_metadata(
                        entry_builder,
                        entry_id=entry_id,
                        category=category,
                        source_refs=decoded.source_refs,
                    )
                    for alias in aliases:
                        if alias not in entry_builder.aliases:
                            entry_builder.aliases.append(alias)

                    for pattern in (term, *aliases):
                        normalized_pattern = normalize_literal_text(pattern)
                        match_builder = match_builders.get(normalized_pattern)
                        if match_builder is None:
                            active_pattern_count += 1
                            if active_pattern_count > MAX_MANAGED_PATTERNS:
                                raise _CatalogValidationError(
                                    "managed_pattern_limit",
                                    generation_id,
                                )
                            match_builder = _MatchBuilder(
                                term=pattern,
                                entry_ids=[],
                                category_ids=[],
                                category_names=[],
                                source_refs=[],
                                disclosure_policy=MANAGEMENT_VISIBLE,
                                subject_type=decoded.subject_type,
                                match_mode=decoded.match_mode,
                                context_class=decoded.context_class,
                                context_strength=decoded.context_strength,
                                entity_ref=decoded.entity_ref,
                                entity_refs=decoded.entity_refs,
                            )
                            match_builders[normalized_pattern] = match_builder
                        _require_compatible_semantics(
                            match_builder,
                            decoded,
                            generation_id=generation_id,
                        )
                        _merge_builder_metadata(
                            match_builder,
                            entry_id=entry_id,
                            category=category,
                            source_refs=decoded.source_refs,
                        )

        if not referenced_leader_entities.issubset(leader_entity_refs):
            raise _CatalogValidationError(
                "unknown_political_entity_reference",
                generation_id,
            )
        if catalog_version >= 2 and (
            not (active_event_count or leader_entity_refs)
            or (
                leader_entity_refs
                and not has_global_strong_context
                and not leader_entity_refs.issubset(strong_context_entity_refs)
            )
        ):
            raise _CatalogValidationError(
                "dead_political_generation",
                generation_id,
            )

        entries = tuple(
            ManagedKeywordEntry(
                entry_ids=tuple(builder.entry_ids),
                term=builder.term,
                aliases=tuple(builder.aliases),
                category_ids=_ordered_categories(
                    builder.category_ids,
                    category_order,
                ),
                category_names=_ordered_names(
                    builder.category_ids,
                    builder.category_names,
                    category_order,
                ),
                disclosure_policy=builder.disclosure_policy,
                source_refs=tuple(builder.source_refs),
                subject_type=builder.subject_type,
                match_mode=builder.match_mode,
                context_class=builder.context_class,
                context_strength=builder.context_strength,
                entity_ref=builder.entity_ref,
                entity_refs=builder.entity_refs,
            )
            for builder in entry_builders.values()
        )
        metadata = {
            normalized: ManagedKeywordMatch(
                term=builder.term,
                category_ids=_ordered_categories(
                    builder.category_ids,
                    category_order,
                ),
                category_names=_ordered_names(
                    builder.category_ids,
                    builder.category_names,
                    category_order,
                ),
                disclosure_policy=builder.disclosure_policy,
                entry_ids=tuple(builder.entry_ids),
                source_refs=tuple(builder.source_refs),
                start=0,
                end=0,
                subject_type=builder.subject_type,
                match_mode=builder.match_mode,
                context_class=builder.context_class,
                context_strength=builder.context_strength,
                context_term="",
                segment_index=-1,
                entity_ref=builder.entity_ref,
                entity_refs=builder.entity_refs,
            )
            for normalized, builder in match_builders.items()
        }
        matcher = ScalableLiteralMatcher(
            (
                (normalized, builder.term)
                for normalized, builder in match_builders.items()
            ),
            max_patterns=MAX_MANAGED_PATTERNS,
            max_nodes=MAX_MANAGED_TRIE_NODES,
            overlap_groups={
                normalized: _overlap_group(builder, normalized=normalized)
                for normalized, builder in match_builders.items()
            },
            max_text_chars=MAX_MANAGED_SCAN_TEXT_CHARS,
            max_candidates=MAX_MANAGED_SCAN_CANDIDATES,
        )
        return ManagedCatalogSnapshot(
            catalog_version=catalog_version,
            generation_id=generation_id,
            generated_at=generated_at,
            categories=tuple(categories),
            entries=entries,
            _matcher=matcher,
            _match_metadata=MappingProxyType(metadata),
        )


def _decode_category(
    raw: Mapping[str, Any],
    *,
    catalog_version: int,
    generation_id: str,
) -> ManagedKeywordCategory:
    category_id = _identifier(
        raw.get("id"),
        code="invalid_category_id",
        generation_id=generation_id,
    )
    name_zh = _bounded_text(
        raw.get("name_zh"),
        limit=64,
        code="invalid_category_name",
        generation_id=generation_id,
    )
    description = _bounded_text(
        raw.get("description"),
        limit=512,
        code="invalid_category_description",
        generation_id=generation_id,
    )
    severity = raw.get("severity")
    if severity not in _SEVERITIES:
        raise _CatalogValidationError("invalid_category_severity", generation_id)
    disclosure_policy = raw.get("disclosure_policy")
    if disclosure_policy not in _DISCLOSURE_POLICIES:
        raise _CatalogValidationError("invalid_disclosure_policy", generation_id)
    required_policy = _KNOWN_CATEGORY_POLICIES.get(category_id)
    if category_id == "political_cn" and catalog_version >= 2:
        required_policy = MANAGEMENT_VISIBLE
    if required_policy is not None and disclosure_policy != required_policy:
        raise _CatalogValidationError("forbidden_policy_downgrade", generation_id)
    version = _bounded_text(
        raw.get("version"),
        limit=64,
        code="invalid_category_version",
        generation_id=generation_id,
    )
    raw_sources = raw.get("sources")
    if (
        not isinstance(raw_sources, list)
        or not raw_sources
        or len(raw_sources) > _MAX_SOURCES_PER_CATEGORY
    ):
        raise _CatalogValidationError("invalid_sources", generation_id)
    sources: list[ManagedKeywordSource] = []
    source_ids: set[str] = set()
    for raw_source in raw_sources:
        if not isinstance(raw_source, dict):
            raise _CatalogValidationError("invalid_source", generation_id)
        source_id = _identifier(
            raw_source.get("source_id"),
            code="invalid_source_id",
            generation_id=generation_id,
        )
        if source_id in source_ids:
            raise _CatalogValidationError("duplicate_source_id", generation_id)
        source_ids.add(source_id)
        revision = raw_source.get("revision", "")
        if revision:
            revision = _bounded_text(
                revision,
                limit=512,
                code="invalid_source_revision",
                generation_id=generation_id,
            )
        elif not isinstance(revision, str):
            raise _CatalogValidationError("invalid_source_revision", generation_id)
        digest = raw_source.get("sha256", "")
        if digest and (
            not isinstance(digest, str) or _SHA256_RE.fullmatch(digest) is None
        ):
            raise _CatalogValidationError("invalid_source_hash", generation_id)
        if not isinstance(digest, str):
            raise _CatalogValidationError("invalid_source_hash", generation_id)
        sources.append(
            ManagedKeywordSource(
                source_id=source_id,
                reference=_bounded_text(
                    raw_source.get("reference"),
                    limit=1_024,
                    code="invalid_source_reference",
                    generation_id=generation_id,
                ),
                license=_bounded_text(
                    raw_source.get("license"),
                    limit=128,
                    code="invalid_source_license",
                    generation_id=generation_id,
                ),
                retrieved_at=_timestamp(
                    raw_source.get("retrieved_at"),
                    code="invalid_source_timestamp",
                    generation_id=generation_id,
                ),
                revision=revision,
                sha256=digest,
            )
        )
    return ManagedKeywordCategory(
        category_id=category_id,
        name_zh=name_zh,
        description=description,
        severity=str(severity),
        disclosure_policy=str(disclosure_policy),
        version=version,
        sources=tuple(sources),
    )


def _decode_shard_descriptor(
    raw: object,
    *,
    generation_id: str,
) -> tuple[str, int, str]:
    if not isinstance(raw, dict):
        raise _CatalogValidationError("invalid_shard_descriptor", generation_id)
    reference = _relative_path(
        raw.get("path"),
        code="invalid_shard_reference",
        generation_id=generation_id,
    )
    digest = raw.get("sha256")
    if not isinstance(digest, str) or _SHA256_RE.fullmatch(digest) is None:
        raise _CatalogValidationError("invalid_shard_hash", generation_id)
    entry_count = raw.get("entry_count")
    if (
        isinstance(entry_count, bool)
        or not isinstance(entry_count, int)
        or entry_count <= 0
        or entry_count > _MAX_MANAGED_ENTRIES
    ):
        raise _CatalogValidationError("managed_pattern_limit", generation_id)
    return reference, entry_count, digest


def _decode_entry(
    raw: object,
    *,
    catalog_version: int,
    category_id: str,
    generation_id: str,
    sources_by_id: Mapping[str, ManagedKeywordSource],
    category_enabled: bool,
) -> _DecodedEntry:
    if not isinstance(raw, dict):
        raise _CatalogValidationError("invalid_entry", generation_id)
    entry_id = _identifier(
        raw.get("id"),
        code="invalid_entry_id",
        generation_id=generation_id,
    )
    term, _ = _literal(
        raw.get("term"),
        code="invalid_entry_term",
        generation_id=generation_id,
    )
    raw_aliases = raw.get("aliases")
    if not isinstance(raw_aliases, list) or len(raw_aliases) > _MAX_ALIASES_PER_ENTRY:
        raise _CatalogValidationError("invalid_entry_aliases", generation_id)
    aliases: list[str] = []
    alias_normalized: set[str] = {normalize_literal_text(term)}
    for raw_alias in raw_aliases:
        alias, normalized = _literal(
            raw_alias,
            code="invalid_entry_alias",
            generation_id=generation_id,
        )
        if normalized in alias_normalized:
            continue
        alias_normalized.add(normalized)
        aliases.append(alias)
    status = raw.get("status")
    if status not in _ENTRY_STATUSES:
        raise _CatalogValidationError("invalid_entry_status", generation_id)
    if catalog_version >= 2 and category_enabled and status != "active":
        raise _CatalogValidationError(
            "nonalerting_entry_in_enabled_v2_category",
            generation_id,
        )
    source_ids = set(sources_by_id)
    source_ref = raw.get("source_ref")
    if source_ref is None:
        if len(source_ids) != 1:
            raise _CatalogValidationError("missing_entry_source", generation_id)
        source_ref = next(iter(source_ids))
    if not isinstance(source_ref, str) or source_ref not in source_ids:
        raise _CatalogValidationError("invalid_entry_source", generation_id)
    source_refs: tuple[str, ...] = (source_ref,)

    if (
        catalog_version >= 2
        and category_enabled
        and category_id != "political_cn"
        and status == "active"
    ):
        raise _CatalogValidationError(
            "active_v2_nonpolitical_entry",
            generation_id,
        )

    subject_type = ""
    match_mode = DIRECT_MATCH
    context_class = ""
    context_strength = ""
    entity_ref = ""
    entity_refs: tuple[str, ...] = ()
    if catalog_version >= 2 and category_id == "political_cn" and status == "active":
        if raw.get("term") != term:
            raise _CatalogValidationError(
                "invalid_political_canonical_term",
                generation_id,
            )
        raw_subject_type = raw.get("subject_type")
        raw_match_mode = raw.get("match_mode")
        expected_match_mode = _POLITICAL_SUBJECT_MATCH_MODES.get(raw_subject_type)
        if expected_match_mode is None or raw_match_mode != expected_match_mode:
            raise _CatalogValidationError(
                "invalid_political_match_semantics",
                generation_id,
            )
        subject_type = str(raw_subject_type)
        match_mode = str(raw_match_mode)
        if raw_aliases:
            raise _CatalogValidationError(
                "invalid_political_aliases",
                generation_id,
            )

        raw_tags = raw.get("tags")
        if not isinstance(raw_tags, list) or not raw_tags or len(raw_tags) > 64:
            raise _CatalogValidationError("invalid_political_review", generation_id)
        tags = tuple(
            _bounded_text(
                tag,
                limit=128,
                code="invalid_political_review",
                generation_id=generation_id,
            )
            for tag in raw_tags
        )
        subject_tags = {tag for tag in tags if tag.startswith("subject:")}
        verification_status = raw.get("verification_status")
        source_screened_candidate = (
            verification_status == "research_candidate"
            and _POLITICAL_SOURCE_SCREENED_TAG in tags
        )
        if (
            len(tags) != len(set(tags))
            or (_POLITICAL_REVIEW_TAG not in tags and not source_screened_candidate)
            or subject_tags != {f"subject:{subject_type}"}
            or not subject_tags.issubset(_POLITICAL_SUBJECT_TAGS)
        ):
            raise _CatalogValidationError("invalid_political_review", generation_id)

        confidence = raw.get("confidence")
        if verification_status not in _POLITICAL_VERIFICATION_STATUSES:
            raise _CatalogValidationError(
                "invalid_political_verification",
                generation_id,
            )
        if (
            isinstance(confidence, bool)
            or not isinstance(confidence, (int, float))
            or not 0 <= confidence <= 1
        ):
            raise _CatalogValidationError(
                "invalid_political_confidence",
                generation_id,
            )
        _date_or_timestamp(
            raw.get("last_reviewed"),
            code="invalid_political_review_date",
            generation_id=generation_id,
        )

        raw_source_refs = raw.get("source_refs")
        if (
            not isinstance(raw_source_refs, list)
            or not raw_source_refs
            or len(raw_source_refs) > 64
        ):
            raise _CatalogValidationError(
                "invalid_political_provenance",
                generation_id,
            )
        source_refs = tuple(
            _identifier(
                candidate,
                code="invalid_political_provenance",
                generation_id=generation_id,
            )
            for candidate in raw_source_refs
        )
        if (
            len(source_refs) != len(set(source_refs))
            or source_ref not in source_refs
            or any(reference not in source_ids for reference in source_refs)
            or any(
                not sources_by_id[reference].revision
                or not sources_by_id[reference].sha256
                for reference in source_refs
            )
        ):
            raise _CatalogValidationError(
                "invalid_political_provenance",
                generation_id,
            )

        if subject_type == "leader_name":
            if not _is_canonical_person_name(term):
                raise _CatalogValidationError(
                    "invalid_political_leader_name",
                    generation_id,
                )
            if not _is_canonical_political_term(term):
                raise _CatalogValidationError(
                    "invalid_political_canonical_term",
                    generation_id,
                )
            entity_ref = _identifier(
                raw.get("entity_ref"),
                code="invalid_political_entity_reference",
                generation_id=generation_id,
            )
            if "entity_refs" in raw:
                raise _CatalogValidationError(
                    "invalid_political_entity_reference",
                    generation_id,
                )
            if raw.get("rank_level") not in _POLITICAL_RANK_LEVELS:
                raise _CatalogValidationError(
                    "invalid_political_rank",
                    generation_id,
                )
            _bounded_text(
                raw.get("rank_basis"),
                limit=512,
                code="invalid_political_rank",
                generation_id=generation_id,
            )
            if "context_class" in raw or "strength" in raw:
                raise _CatalogValidationError(
                    "invalid_political_context",
                    generation_id,
                )
        elif match_mode == SUPPORT_ONLY_MATCH:
            if not _is_canonical_political_term(term):
                raise _CatalogValidationError(
                    "invalid_political_canonical_term",
                    generation_id,
                )
            if "entity_ref" in raw:
                raise _CatalogValidationError(
                    "invalid_political_entity_reference",
                    generation_id,
                )
            raw_context_class = raw.get("context_class")
            raw_context_strength = raw.get("strength")
            if (
                raw_context_class not in _CONTEXT_CLASSES
                or raw_context_strength not in _CONTEXT_STRENGTHS
            ):
                raise _CatalogValidationError(
                    "invalid_political_context",
                    generation_id,
                )
            context_class = str(raw_context_class)
            context_strength = str(raw_context_strength)
            if "entity_refs" in raw:
                raw_entity_refs = raw["entity_refs"]
                if (
                    not isinstance(raw_entity_refs, list)
                    or not raw_entity_refs
                    or len(raw_entity_refs) > 64
                ):
                    raise _CatalogValidationError(
                        "invalid_political_entity_reference",
                        generation_id,
                    )
                entity_refs = tuple(
                    _identifier(
                        candidate,
                        code="invalid_political_entity_reference",
                        generation_id=generation_id,
                    )
                    for candidate in raw_entity_refs
                )
                if len(entity_refs) != len(set(entity_refs)):
                    raise _CatalogValidationError(
                        "invalid_political_entity_reference",
                        generation_id,
                    )
            if "rank_level" in raw or "rank_basis" in raw:
                raise _CatalogValidationError(
                    "invalid_political_rank",
                    generation_id,
                )
        else:
            if not _is_canonical_political_term(term):
                raise _CatalogValidationError(
                    "invalid_political_canonical_term",
                    generation_id,
                )
            if "context_class" in raw or "strength" in raw:
                raise _CatalogValidationError(
                    "invalid_political_context",
                    generation_id,
                )
            if "entity_ref" in raw or "entity_refs" in raw:
                raise _CatalogValidationError(
                    "invalid_political_entity_reference",
                    generation_id,
                )
            if "rank_level" in raw or "rank_basis" in raw:
                raise _CatalogValidationError(
                    "invalid_political_rank",
                    generation_id,
                )
    return _DecodedEntry(
        entry_id=entry_id,
        term=term,
        aliases=tuple(aliases),
        status=str(status),
        source_ref=source_ref,
        source_refs=source_refs,
        subject_type=subject_type,
        match_mode=match_mode,
        context_class=context_class,
        context_strength=context_strength,
        entity_ref=entity_ref,
        entity_refs=entity_refs,
    )


def _merge_builder_metadata(
    builder: _EntryBuilder | _MatchBuilder,
    *,
    entry_id: str,
    category: ManagedKeywordCategory,
    source_refs: Iterable[str],
) -> None:
    if entry_id not in builder.entry_ids:
        builder.entry_ids.append(entry_id)
    if category.category_id not in builder.category_ids:
        builder.category_ids.append(category.category_id)
        builder.category_names.append(category.name_zh)
    for source_ref in source_refs:
        if source_ref not in builder.source_refs:
            builder.source_refs.append(source_ref)
    if category.disclosure_policy == STRICT_HIDDEN:
        builder.disclosure_policy = STRICT_HIDDEN


def _require_compatible_semantics(
    builder: _EntryBuilder | _MatchBuilder,
    decoded: _DecodedEntry,
    *,
    generation_id: str,
) -> None:
    if (
        builder.subject_type != decoded.subject_type
        or builder.match_mode != decoded.match_mode
        or builder.context_class != decoded.context_class
        or builder.context_strength != decoded.context_strength
        or builder.entity_ref != decoded.entity_ref
        or builder.entity_refs != decoded.entity_refs
    ):
        raise _CatalogValidationError(
            "conflicting_match_semantics",
            generation_id,
        )


def _normalized_gap(
    left: ScalableLiteralMatch,
    right: ScalableLiteralMatch,
) -> int:
    if left.end <= right.start:
        return right.start - left.end
    if right.end <= left.start:
        return left.start - right.end
    return 0


def _nearest_strong_context(
    subject: ScalableLiteralMatch,
    contexts: Iterable[ScalableLiteralMatch],
) -> ScalableLiteralMatch | None:
    nearby = (
        context
        for context in contexts
        if (
            (subject.end <= context.start or context.end <= subject.start)
            and _normalized_gap(subject, context) <= MAX_CONTEXT_GAP
        )
    )
    return min(
        nearby,
        key=lambda context: (
            _normalized_gap(subject, context),
            context.start,
            -(context.end - context.start),
            context.key,
        ),
        default=None,
    )


def _context_applies_to_subject(
    *,
    subject: ManagedKeywordMatch,
    context: ManagedKeywordMatch,
) -> bool:
    return not context.entity_refs or subject.entity_ref in context.entity_refs


def _overlap_group(builder: _MatchBuilder, *, normalized: str) -> str:
    semantic_group = builder.match_mode
    if builder.match_mode == SUPPORT_ONLY_MATCH:
        # Contexts are filtered for strength and entity scope only after the
        # literal scan.  Preserve every distinct overlapping context here so
        # a longer inapplicable term cannot hide a shorter applicable one.
        semantic_group = f"{semantic_group}:{normalized}"
    return f"{builder.disclosure_policy}:{semantic_group}"


def _ordered_categories(
    values: Sequence[str],
    order: Mapping[str, int],
) -> tuple[str, ...]:
    return tuple(sorted(values, key=order.__getitem__))


def _ordered_names(
    category_ids: Sequence[str],
    names: Sequence[str],
    order: Mapping[str, int],
) -> tuple[str, ...]:
    pairs = sorted(zip(category_ids, names), key=lambda item: order[item[0]])
    return tuple(name for _category_id, name in pairs)


def _identifier(
    value: object,
    *,
    code: str,
    generation_id: str = "",
) -> str:
    if not isinstance(value, str) or _IDENTIFIER_RE.fullmatch(value) is None:
        raise _CatalogValidationError(code, generation_id)
    return value


def _bounded_text(
    value: object,
    *,
    limit: int,
    code: str,
    generation_id: str,
) -> str:
    if not isinstance(value, str):
        raise _CatalogValidationError(code, generation_id)
    text = value.strip()
    if not text or len(text) > limit:
        raise _CatalogValidationError(code, generation_id)
    if any(unicodedata.category(character).startswith("C") for character in text):
        raise _CatalogValidationError(code, generation_id)
    return text


def _timestamp(
    value: object,
    *,
    code: str,
    generation_id: str = "",
) -> str:
    text = _bounded_text(
        value,
        limit=64,
        code=code,
        generation_id=generation_id,
    )
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as exc:
        raise _CatalogValidationError(code, generation_id) from exc
    if parsed.tzinfo is None:
        raise _CatalogValidationError(code, generation_id)
    return text


def _date_or_timestamp(
    value: object,
    *,
    code: str,
    generation_id: str = "",
) -> str:
    text = _bounded_text(
        value,
        limit=128,
        code=code,
        generation_id=generation_id,
    )
    try:
        datetime.fromisoformat(text[:-1] + "+00:00" if text.endswith("Z") else text)
    except ValueError as exc:
        raise _CatalogValidationError(code, generation_id) from exc
    return text


def _is_canonical_political_term(value: str) -> bool:
    return value == unicodedata.normalize("NFKC", value) and not any(
        character.isspace() or is_ignored_literal_character(character)
        for character in value
    )


def _is_canonical_person_name(value: str) -> bool:
    parts = value.split("·")
    if not 1 <= len(parts) <= 4 or sum(map(len, parts)) < 2:
        return False
    if any(not 1 <= len(part) <= 16 for part in parts):
        return False
    return all(
        unicodedata.name(character, "").startswith(
            ("CJK UNIFIED IDEOGRAPH-", "CJK COMPATIBILITY IDEOGRAPH-")
        )
        for part in parts
        for character in part
    )


def _literal(
    value: object,
    *,
    code: str,
    generation_id: str,
) -> tuple[str, str]:
    if not isinstance(value, str):
        raise _CatalogValidationError(code, generation_id)
    pattern = value.strip()
    if not pattern:
        raise _CatalogValidationError(code, generation_id)
    for character in pattern:
        category = unicodedata.category(character)
        if category.startswith("C") and category != "Cf" and not character.isspace():
            raise _CatalogValidationError(code, generation_id)
    normalized = normalize_literal_text(pattern)
    if not (
        MIN_NORMALIZED_PATTERN_LENGTH
        <= len(normalized)
        <= MAX_NORMALIZED_PATTERN_LENGTH
    ):
        raise _CatalogValidationError(code, generation_id)
    return pattern, normalized


def _relative_path(
    value: object,
    *,
    code: str,
    generation_id: str = "",
) -> str:
    if not isinstance(value, str) or not value or "\\" in value:
        raise _CatalogValidationError(code, generation_id)
    path = Path(value)
    if path.is_absolute() or any(part in {"", ".", ".."} for part in path.parts):
        raise _CatalogValidationError(code, generation_id)
    return path.as_posix()


def _join_relative(
    base: Path,
    reference: str,
    *,
    trusted_root: Path,
    code: str,
    generation_id: str,
) -> Path:
    relative = _relative_path(
        reference,
        code=code,
        generation_id=generation_id,
    )
    candidate = _absolute_path(base / relative)
    try:
        candidate.relative_to(_absolute_path(base))
    except ValueError as exc:
        raise _CatalogValidationError(code, generation_id) from exc
    try:
        candidate.relative_to(trusted_root)
    except ValueError as exc:
        raise _CatalogValidationError(code, generation_id) from exc
    return candidate


def _decode_json_object(
    raw: bytes,
    *,
    code: str,
    generation_id: str = "",
) -> Mapping[str, Any]:
    try:
        value = json.loads(
            raw.decode("utf-8"),
            object_pairs_hook=_object_without_duplicate_keys,
        )
    except (UnicodeError, json.JSONDecodeError, RecursionError, ValueError) as exc:
        raise _CatalogValidationError(code, generation_id) from exc
    if not isinstance(value, dict):
        raise _CatalogValidationError(code, generation_id)
    return value


def _object_without_duplicate_keys(
    pairs: list[tuple[str, Any]],
) -> dict[str, Any]:
    decoded: dict[str, Any] = {}
    for key, value in pairs:
        if key in decoded:
            raise ValueError("duplicate JSON key")
        decoded[key] = value
    return decoded


def _read_regular_bytes(
    path: Path,
    *,
    trusted_root: Path,
    max_bytes: int,
    generation_id: str = "",
) -> tuple[bytes, tuple[object, ...]]:
    try:
        _assert_safe_path(path, trusted_root)
        flags = os.O_RDONLY
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        descriptor = os.open(path, flags)
    except FileNotFoundError as exc:
        raise _CatalogValidationError("missing_catalog_file", generation_id) from exc
    except OSError as exc:
        raise _CatalogValidationError("unsafe_catalog_path", generation_id) from exc
    try:
        before = os.fstat(descriptor)
        if not stat.S_ISREG(before.st_mode) or before.st_size > max_bytes:
            raise _CatalogValidationError("invalid_catalog_file", generation_id)
        if before.st_uid != os.geteuid() or stat.S_IMODE(before.st_mode) & 0o077:
            raise _CatalogValidationError(
                "unsafe_catalog_permissions",
                generation_id,
            )
        chunks: list[bytes] = []
        remaining = max_bytes + 1
        while remaining > 0:
            chunk = os.read(descriptor, min(1024 * 1024, remaining))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        payload = b"".join(chunks)
        after = os.fstat(descriptor)
        if len(payload) > max_bytes or _raw_fingerprint(
            before, "file"
        ) != _raw_fingerprint(after, "file"):
            raise _CatalogValidationError("catalog_file_changed", generation_id)
    finally:
        os.close(descriptor)
    try:
        _assert_safe_path(path, trusted_root)
        final = os.lstat(path)
    except OSError as exc:
        raise _CatalogValidationError("unsafe_catalog_path", generation_id) from exc
    fingerprint = _raw_fingerprint(final, "file")
    if fingerprint != _raw_fingerprint(after, "file"):
        raise _CatalogValidationError("catalog_file_changed", generation_id)
    return payload, fingerprint


def _assert_safe_path(path: Path, trusted_root: Path) -> None:
    absolute = _absolute_path(path)
    root = _absolute_path(trusted_root)
    try:
        relative = absolute.relative_to(root)
    except ValueError as exc:
        raise OSError("path outside trusted root") from exc
    current = root
    # Check the trusted root explicitly without resolving it through a
    # potentially attacker-controlled symbolic link.
    _assert_not_symlink(root)
    for part in relative.parts:
        current /= part
        _assert_not_symlink(current)


def _assert_not_symlink(path: Path) -> None:
    try:
        metadata = os.lstat(path)
    except FileNotFoundError:
        return
    if stat.S_ISLNK(metadata.st_mode):
        raise OSError("symbolic link path component is not allowed")


def _raw_fingerprint(
    metadata: os.stat_result,
    kind: str,
) -> tuple[object, ...]:
    return (
        kind,
        metadata.st_dev,
        metadata.st_ino,
        metadata.st_size,
        metadata.st_mtime_ns,
        metadata.st_ctime_ns,
    )


def _absolute_path(path: Path) -> Path:
    return Path(os.path.abspath(os.fspath(path)))


def _default_trusted_root(path: Path) -> Path:
    parent = path.parent
    if (
        parent.name == "managed"
        and parent.parent.name == "content_alert"
        and parent.parent.parent.name == "data"
    ):
        return parent.parent.parent.parent
    return parent


__all__ = (
    "CATALOG_VERSION",
    "MANAGEMENT_VISIBLE",
    "MAX_MANAGED_PATTERNS",
    "MAX_MANAGED_TRIE_NODES",
    "MAX_MANIFEST_BYTES",
    "MAX_POINTER_BYTES",
    "MAX_SHARD_BYTES",
    "MAX_STORED_PATTERNS",
    "STRICT_HIDDEN",
    "ManagedCatalogError",
    "ManagedCatalogSnapshot",
    "ManagedKeywordCatalog",
    "ManagedKeywordCategory",
    "ManagedKeywordEntry",
    "ManagedKeywordMatch",
    "ManagedKeywordSource",
)
