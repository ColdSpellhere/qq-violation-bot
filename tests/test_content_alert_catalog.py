from __future__ import annotations

import hashlib
import json
import os
import tempfile
import unittest
from pathlib import Path
from typing import Any

from nonebot.adapters.onebot.v11 import Message, MessageSegment

CATEGORY_SPECS = (
    ("political_cn", "政治占位分类", "management_visible"),
    ("sexual_explicit", "普通占位分类一", "management_visible"),
    ("gender_conflict", "普通占位分类二", "management_visible"),
    ("controversial_topics", "普通占位分类三", "management_visible"),
    ("anime_game_controversy", "普通占位分类四", "management_visible"),
    ("graphic_violence", "普通占位分类五", "management_visible"),
    ("terrorism", "普通占位分类六", "management_visible"),
)
CATEGORY_IDS = tuple(item[0] for item in CATEGORY_SPECS)
STRICT_CATEGORY_SPEC = (
    "restricted_internal",
    "受保护占位分类",
    "strict_hidden",
)
V2_EVENT_TERM = "合成历史事件甲"
V2_LEADER_TERM = "合成领导姓名甲"
V2_STRONG_CONTEXT_TERM = "接受合成调查"
V2_WEAK_CONTEXT_TERM = "曾经合成任职"
V2_OVERLAPPING_WEAK_CONTEXT_TERM = f"{V2_STRONG_CONTEXT_TERM}后任职"


def _json_bytes(value: object) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def _write_json(path: Path, value: object) -> bytes:
    payload = _json_bytes(value)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(payload)
    path.chmod(0o600)
    return payload


def _term(category_id: str, generation_id: str, index: int) -> str:
    return f"占位词-{category_id}-{generation_id}-{index:05d}"


def _write_generation(
    root: Path,
    generation_id: str,
    *,
    terms_by_category: dict[str, list[str]] | None = None,
    entry_fields_by_term: dict[str, dict[str, object]] | None = None,
    policy_overrides: dict[str, str] | None = None,
    enabled_overrides: dict[str, bool] | None = None,
    category_specs: tuple[tuple[str, str, str], ...] = CATEGORY_SPECS,
    shard_size: int = 1_000,
    catalog_version: int = 1,
) -> dict[str, Any]:
    generation_root = root / "generations" / generation_id
    terms_by_category = terms_by_category or {
        category_id: [_term(category_id, generation_id, 1)]
        for category_id, _name_zh, _policy in category_specs
    }
    entry_fields_by_term = entry_fields_by_term or {}
    policy_overrides = policy_overrides or {}
    enabled_overrides = enabled_overrides or {}
    categories: list[dict[str, Any]] = []
    shard_paths: list[Path] = []

    for category_id, name_zh, default_policy in category_specs:
        category_terms = terms_by_category.get(category_id, [])
        shards: list[dict[str, Any]] = []
        for shard_index, offset in enumerate(
            range(0, len(category_terms), shard_size),
            start=1,
        ):
            terms = category_terms[offset : offset + shard_size]
            entries = [
                {
                    "id": f"{category_id}-{offset + index:05d}",
                    "term": term,
                    "aliases": [],
                    "status": "active",
                    "source_ref": "synthetic-fixture",
                }
                | entry_fields_by_term.get(term, {})
                for index, term in enumerate(terms, start=1)
            ]
            shard_path = (
                generation_root / "shards" / f"{category_id}-{shard_index:04d}.json"
            )
            payload = _write_json(
                shard_path,
                {
                    "version": catalog_version,
                    "generation_id": generation_id,
                    "category_id": category_id,
                    "entries": entries,
                },
            )
            shard_paths.append(shard_path)
            shards.append(
                {
                    "path": str(shard_path.relative_to(generation_root)),
                    "sha256": hashlib.sha256(payload).hexdigest(),
                    "entry_count": len(entries),
                }
            )

        source = {
            "source_id": "synthetic-fixture",
            "reference": "synthetic://fixture",
            "license": "synthetic-test-only",
            "retrieved_at": "2026-09-02T00:00:00Z",
        }
        if catalog_version >= 2:
            source.update(
                {
                    "revision": "synthetic-v2-source-r1",
                    "sha256": hashlib.sha256(b"synthetic-v2-source-r1").hexdigest(),
                }
            )
        categories.append(
            {
                "id": category_id,
                "name_zh": name_zh,
                "description": f"{name_zh}的合成测试说明",
                "severity": "high" if category_id == "political_cn" else "medium",
                "disclosure_policy": policy_overrides.get(
                    category_id,
                    default_policy,
                ),
                "enabled": enabled_overrides.get(category_id, True),
                "version": "2026.09.02.1",
                "sources": [source],
                "shards": shards,
            }
        )

    manifest_path = generation_root / "manifest.json"
    _write_json(
        manifest_path,
        {
            "version": catalog_version,
            "generation_id": generation_id,
            "generated_at": "2026-09-02T00:00:00Z",
            "categories": categories,
        },
    )
    return {
        "generation_root": generation_root,
        "manifest_path": manifest_path,
        "shard_paths": shard_paths,
    }


def _activate(root: Path, generation_id: str, *, catalog_version: int = 1) -> Path:
    current_path = root / "current.json"
    temporary = root / f".current-{generation_id}.tmp"
    _write_json(
        temporary,
        {
            "version": catalog_version,
            "generation_id": generation_id,
            "manifest": f"generations/{generation_id}/manifest.json",
        },
    )
    os.replace(temporary, current_path)
    return current_path


def _write_v2_political_generation(
    root: Path,
    generation_id: str,
    *,
    leader_fields: dict[str, object] | None = None,
    strong_context_fields: dict[str, object] | None = None,
    extra_entry_fields_by_term: dict[str, dict[str, object]] | None = None,
    policy_overrides: dict[str, str] | None = None,
) -> dict[str, Any]:
    terms_by_category = {
        category_id: [_term(category_id, generation_id, 1)]
        for category_id in CATEGORY_IDS
    }
    terms_by_category["political_cn"] = [
        V2_EVENT_TERM,
        V2_LEADER_TERM,
        V2_STRONG_CONTEXT_TERM,
        V2_WEAK_CONTEXT_TERM,
        V2_OVERLAPPING_WEAK_CONTEXT_TERM,
    ]
    entry_fields_by_term: dict[str, dict[str, object]] = {
        V2_EVENT_TERM: {
            "subject_type": "historical_event",
            "match_mode": "direct",
        },
        V2_LEADER_TERM: {
            "subject_type": "leader_name",
            "match_mode": "same_segment_context",
            "entity_ref": "synthetic-leader-0001",
            "rank_level": "省部级副职",
            "rank_basis": "合成测试中的官方任免依据",
        },
        V2_STRONG_CONTEXT_TERM: {
            "subject_type": "political_context",
            "match_mode": "support_only",
            "context_class": "case_proceeding",
            "strength": "strong",
        },
        V2_WEAK_CONTEXT_TERM: {
            "subject_type": "political_context",
            "match_mode": "support_only",
            "context_class": "office_title",
            "strength": "weak",
        },
        V2_OVERLAPPING_WEAK_CONTEXT_TERM: {
            "subject_type": "political_context",
            "match_mode": "support_only",
            "context_class": "office_title",
            "strength": "weak",
        },
    }
    if leader_fields is not None:
        entry_fields_by_term[V2_LEADER_TERM] = leader_fields
    if strong_context_fields is not None:
        entry_fields_by_term[V2_STRONG_CONTEXT_TERM] = strong_context_fields
    if extra_entry_fields_by_term:
        terms_by_category["political_cn"].extend(extra_entry_fields_by_term)
        entry_fields_by_term.update(extra_entry_fields_by_term)
    default_subjects = {
        V2_EVENT_TERM: "historical_event",
        V2_LEADER_TERM: "leader_name",
        V2_STRONG_CONTEXT_TERM: "political_context",
        V2_WEAK_CONTEXT_TERM: "political_context",
        V2_OVERLAPPING_WEAK_CONTEXT_TERM: "political_context",
    }
    for term, fields in entry_fields_by_term.items():
        subject_type = str(
            fields.get("subject_type", default_subjects.get(term, "political_context"))
        )
        fields.update(
            {
                "verification_status": (
                    "research_candidate"
                    if subject_type == "leader_name"
                    else "operator_curated"
                ),
                "confidence": 0.85 if subject_type == "leader_name" else 0.95,
                "tags": [
                    "human-reviewed-political-scope",
                    f"subject:{subject_type}",
                ],
                "source_refs": ["synthetic-fixture"],
                "last_reviewed": "2026-09-03",
            }
        )
    return _write_generation(
        root,
        generation_id,
        terms_by_category=terms_by_category,
        entry_fields_by_term=entry_fields_by_term,
        policy_overrides=policy_overrides,
        enabled_overrides={
            category_id: category_id == "political_cn" for category_id in CATEGORY_IDS
        },
        catalog_version=2,
    )


def _rewrite_manifest_shard(
    manifest_path: Path,
    *,
    category_index: int = 0,
    shard_index: int = 0,
    **updates: object,
) -> None:
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["categories"][category_index]["shards"][shard_index].update(updates)
    _write_json(manifest_path, manifest)


def _rewrite_first_shard(
    generation: dict[str, Any],
    payload: bytes,
) -> None:
    shard_path = generation["shard_paths"][0]
    shard_path.write_bytes(payload)
    _rewrite_manifest_shard(
        generation["manifest_path"],
        sha256=hashlib.sha256(payload).hexdigest(),
    )


class ManagedKeywordCatalogTests(unittest.TestCase):
    def test_valid_generation_loads_all_categories_and_pointer_switches_atomically(
        self,
    ) -> None:
        from plugins.content_alert.catalog import ManagedKeywordCatalog

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "managed"
            _write_generation(root, "generation-0001")
            current = _activate(root, "generation-0001")
            catalog = ManagedKeywordCatalog(current)

            first = catalog.snapshot()

            self.assertTrue(first.has_active_generation)
            self.assertEqual("generation-0001", first.generation_id)
            self.assertEqual(
                set(CATEGORY_IDS),
                {category.category_id for category in first.categories},
            )
            self.assertEqual(7, len(first.entries))
            self.assertEqual(
                {"management_visible"},
                {category.disclosure_policy for category in first.categories},
            )
            self.assertIsNone(catalog.last_error)

            _write_generation(root, "generation-0002")
            _activate(root, "generation-0002")
            second = catalog.snapshot()

        self.assertTrue(second.has_active_generation)
        self.assertEqual("generation-0002", second.generation_id)
        self.assertIsNot(first, second)
        self.assertEqual("generation-0001", first.generation_id)

    def test_legacy_hidden_and_new_visible_political_generations_both_load(
        self,
    ) -> None:
        from plugins.content_alert.catalog import ManagedKeywordCatalog

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "managed"
            _write_generation(root, "generation-good")
            current = _activate(root, "generation-good")
            catalog = ManagedKeywordCatalog(current)
            visible = catalog.snapshot()

            _write_generation(
                root,
                "generation-hidden",
                policy_overrides={"political_cn": "strict_hidden"},
            )
            _activate(root, "generation-hidden")
            hidden = catalog.snapshot()

        self.assertEqual("generation-good", visible.generation_id)
        self.assertEqual(
            "management_visible",
            next(
                category.disclosure_policy
                for category in visible.categories
                if category.category_id == "political_cn"
            ),
        )
        self.assertEqual("generation-hidden", hidden.generation_id)
        self.assertEqual(
            "strict_hidden",
            next(
                category.disclosure_policy
                for category in hidden.categories
                if category.category_id == "political_cn"
            ),
        )

    def test_v2_hidden_political_generation_keeps_lkg_and_cold_start_empty(
        self,
    ) -> None:
        from plugins.content_alert.catalog import ManagedKeywordCatalog

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "managed"
            _write_v2_political_generation(root, "generation-v2-good")
            current = _activate(
                root,
                "generation-v2-good",
                catalog_version=2,
            )
            catalog = ManagedKeywordCatalog(current)
            good = catalog.snapshot()

            _write_v2_political_generation(
                root,
                "generation-v2-hidden",
                policy_overrides={"political_cn": "strict_hidden"},
            )
            _activate(
                root,
                "generation-v2-hidden",
                catalog_version=2,
            )

            retained = catalog.snapshot()
            cold = ManagedKeywordCatalog(current).snapshot()

        self.assertIs(good, retained)
        self.assertFalse(cold.has_active_generation)
        self.assertIsNotNone(catalog.last_error)

    def test_v1_generation_without_match_metadata_remains_direct(self) -> None:
        from plugins.content_alert.catalog import ManagedKeywordCatalog

        political_term = "合成旧版政治词条"
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "managed"
            terms = {
                category_id: [_term(category_id, "generation-v1-direct", 1)]
                for category_id in CATEGORY_IDS
            }
            terms["political_cn"] = [political_term]
            _write_generation(
                root,
                "generation-v1-direct",
                terms_by_category=terms,
            )
            current = _activate(root, "generation-v1-direct")
            catalog = ManagedKeywordCatalog(current)

            matches = catalog.match_message(
                Message(MessageSegment.text(political_term))
            )

        self.assertEqual([political_term], [match.term for match in matches])
        self.assertEqual(0, matches[0].segment_index)
        self.assertEqual("", matches[0].context_term)
        self.assertEqual("", matches[0].context_class)

    def test_v2_historical_event_is_direct_but_context_is_support_only(
        self,
    ) -> None:
        from plugins.content_alert.catalog import ManagedKeywordCatalog

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "managed"
            _write_v2_political_generation(root, "generation-v2-direct")
            current = _activate(
                root,
                "generation-v2-direct",
                catalog_version=2,
            )
            catalog = ManagedKeywordCatalog(current)

            event_matches = catalog.match_message(
                Message(
                    [
                        MessageSegment.at(10001),
                        MessageSegment.text(V2_EVENT_TERM),
                    ]
                )
            )
            context_matches = catalog.match_message(
                Message(MessageSegment.text(V2_STRONG_CONTEXT_TERM))
            )
            leader_matches = catalog.match_message(
                Message(MessageSegment.text(V2_LEADER_TERM))
            )

        self.assertEqual([V2_EVENT_TERM], [match.term for match in event_matches])
        self.assertEqual(1, getattr(event_matches[0], "segment_index", None))
        self.assertEqual((), context_matches)
        self.assertEqual((), leader_matches)

    def test_v2_direct_leader_matches_without_context(self) -> None:
        from plugins.content_alert.catalog import ManagedKeywordCatalog

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "managed"
            _write_v2_political_generation(
                root,
                "generation-v2-direct-leader",
                leader_fields={
                    "subject_type": "leader_name",
                    "match_mode": "direct",
                    "entity_ref": "synthetic-leader-0001",
                    "rank_level": "省部级副职",
                    "rank_basis": "合成测试中的官方任免依据",
                },
            )
            current = _activate(
                root,
                "generation-v2-direct-leader",
                catalog_version=2,
            )
            catalog = ManagedKeywordCatalog(current)

            matches = catalog.match_message(
                Message(MessageSegment.text(V2_LEADER_TERM))
            )

        self.assertEqual([V2_LEADER_TERM], [match.term for match in matches])
        self.assertEqual("leader_name", matches[0].subject_type)
        self.assertEqual("direct", matches[0].match_mode)
        self.assertEqual("", matches[0].context_term)

    def test_v2_matching_ignores_unicode_default_ignorable_marks(self) -> None:
        from plugins.content_alert.catalog import ManagedKeywordCatalog

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "managed"
            _write_v2_political_generation(root, "generation-v2-ignorables")
            current = _activate(
                root,
                "generation-v2-ignorables",
                catalog_version=2,
            )
            catalog = ManagedKeywordCatalog(current)

            for marker in ("\ufe0f", "\u034f", "\u2065", "\ufff0"):
                with self.subTest(marker=ord(marker)):
                    event_term = marker.join(V2_EVENT_TERM)
                    leader_term = marker.join(V2_LEADER_TERM)
                    context_term = marker.join(V2_STRONG_CONTEXT_TERM)

                    event_matches = catalog.match_message(
                        Message(MessageSegment.text(event_term))
                    )
                    compound_matches = catalog.match_message(
                        Message(MessageSegment.text(f"{leader_term}随后{context_term}"))
                    )

                    self.assertEqual(
                        [V2_EVENT_TERM],
                        [match.term for match in event_matches],
                    )
                    self.assertEqual(
                        [V2_LEADER_TERM],
                        [match.term for match in compound_matches],
                    )

    def test_v2_leader_requires_strong_context_with_normalized_gap_at_most_twelve(
        self,
    ) -> None:
        from plugins.content_alert.catalog import ManagedKeywordCatalog

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "managed"
            _write_v2_political_generation(root, "generation-v2-distance")
            current = _activate(
                root,
                "generation-v2-distance",
                catalog_version=2,
            )
            catalog = ManagedKeywordCatalog(current)

            at_boundary = catalog.match_message(
                Message(
                    MessageSegment.text(
                        f"{V2_LEADER_TERM}{'甲' * 12}{V2_STRONG_CONTEXT_TERM}"
                    )
                )
            )
            beyond_boundary = catalog.match_message(
                Message(
                    MessageSegment.text(
                        f"{V2_LEADER_TERM}{'甲' * 13}{V2_STRONG_CONTEXT_TERM}"
                    )
                )
            )
            weak_context = catalog.match_message(
                Message(MessageSegment.text(f"{V2_LEADER_TERM}{V2_WEAK_CONTEXT_TERM}"))
            )

        self.assertEqual([V2_LEADER_TERM], [match.term for match in at_boundary])
        self.assertEqual(
            V2_STRONG_CONTEXT_TERM,
            getattr(at_boundary[0], "context_term", None),
        )
        self.assertEqual("case_proceeding", at_boundary[0].context_class)
        self.assertEqual(0, at_boundary[0].start)
        self.assertEqual(0, getattr(at_boundary[0], "segment_index", None))
        self.assertEqual(
            len(V2_LEADER_TERM) + 12 + len(V2_STRONG_CONTEXT_TERM),
            at_boundary[0].end,
        )
        self.assertEqual((), beyond_boundary)
        self.assertEqual((), weak_context)

    def test_v2_entity_scoped_context_only_qualifies_its_named_leader(self) -> None:
        from plugins.content_alert.catalog import ManagedKeywordCatalog

        other_leader = "合成领导姓名乙"
        other_context = "进入另一合成程序"
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "managed"
            _write_v2_political_generation(
                root,
                "generation-v2-scope",
                strong_context_fields={
                    "subject_type": "political_context",
                    "match_mode": "support_only",
                    "context_class": "case_proceeding",
                    "strength": "strong",
                    "entity_refs": ["synthetic-leader-0001"],
                },
                extra_entry_fields_by_term={
                    other_leader: {
                        "subject_type": "leader_name",
                        "match_mode": "same_segment_context",
                        "entity_ref": "synthetic-leader-0002",
                        "rank_level": "省部级副职",
                        "rank_basis": "另一条合成测试任职依据",
                    },
                    other_context: {
                        "subject_type": "political_context",
                        "match_mode": "support_only",
                        "context_class": "case_proceeding",
                        "strength": "strong",
                        "entity_refs": ["synthetic-leader-0002"],
                    },
                },
            )
            current = _activate(
                root,
                "generation-v2-scope",
                catalog_version=2,
            )
            catalog = ManagedKeywordCatalog(current)

            intended = catalog.match_message(
                Message(
                    MessageSegment.text(f"{V2_LEADER_TERM}{V2_STRONG_CONTEXT_TERM}")
                )
            )
            unrelated = catalog.match_message(
                Message(MessageSegment.text(f"{other_leader}{V2_STRONG_CONTEXT_TERM}"))
            )

        self.assertEqual([V2_LEADER_TERM], [match.term for match in intended])
        self.assertEqual((), unrelated)

    def test_v2_applicable_short_context_survives_overlapping_other_entity_context(
        self,
    ) -> None:
        from plugins.content_alert.catalog import ManagedKeywordCatalog

        other_leader = "合成领导姓名乙"
        longer_other_context = f"正在{V2_STRONG_CONTEXT_TERM}"
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "managed"
            _write_v2_political_generation(
                root,
                "generation-v2-ctx-scope",
                strong_context_fields={
                    "subject_type": "political_context",
                    "match_mode": "support_only",
                    "context_class": "case_proceeding",
                    "strength": "strong",
                    "entity_refs": ["synthetic-leader-0001"],
                },
                extra_entry_fields_by_term={
                    other_leader: {
                        "subject_type": "leader_name",
                        "match_mode": "same_segment_context",
                        "entity_ref": "synthetic-leader-0002",
                        "rank_level": "省部级副职",
                        "rank_basis": "另一条合成测试任职依据",
                    },
                    longer_other_context: {
                        "subject_type": "political_context",
                        "match_mode": "support_only",
                        "context_class": "case_proceeding",
                        "strength": "strong",
                        "entity_refs": ["synthetic-leader-0002"],
                    },
                },
            )
            current = _activate(
                root,
                "generation-v2-ctx-scope",
                catalog_version=2,
            )
            catalog = ManagedKeywordCatalog(current)

            matches = catalog.match_message(
                Message(MessageSegment.text(f"{V2_LEADER_TERM}{longer_other_context}"))
            )

        self.assertEqual([V2_LEADER_TERM], [match.term for match in matches])
        self.assertEqual(V2_STRONG_CONTEXT_TERM, matches[0].context_term)

    def test_v2_context_before_leader_expands_the_normalized_match_range(
        self,
    ) -> None:
        from plugins.content_alert.catalog import ManagedKeywordCatalog

        prefix = "合成前缀"
        text = f"{prefix}{V2_STRONG_CONTEXT_TERM}甲乙{V2_LEADER_TERM}合成后缀"
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "managed"
            _write_v2_political_generation(root, "generation-v2-union-range")
            current = _activate(
                root,
                "generation-v2-union-range",
                catalog_version=2,
            )
            catalog = ManagedKeywordCatalog(current)

            matches = catalog.match_message(Message(MessageSegment.text(text)))

        self.assertEqual(1, len(matches))
        self.assertEqual(V2_LEADER_TERM, matches[0].term)
        self.assertEqual(
            V2_STRONG_CONTEXT_TERM,
            getattr(matches[0], "context_term", None),
        )
        self.assertEqual("case_proceeding", matches[0].context_class)
        self.assertEqual(len(prefix), matches[0].start)
        self.assertEqual(
            len(prefix) + len(V2_STRONG_CONTEXT_TERM) + 2 + len(V2_LEADER_TERM),
            matches[0].end,
        )

    def test_v2_leader_uses_later_occurrence_when_first_lacks_nearby_context(
        self,
    ) -> None:
        from plugins.content_alert.catalog import ManagedKeywordCatalog

        text = f"{V2_LEADER_TERM}{'甲' * 20}{V2_LEADER_TERM}{V2_STRONG_CONTEXT_TERM}"
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "managed"
            _write_v2_political_generation(root, "generation-v2-occurrences")
            current = _activate(
                root,
                "generation-v2-occurrences",
                catalog_version=2,
            )
            catalog = ManagedKeywordCatalog(current)

            matches = catalog.match_message(Message(MessageSegment.text(text)))

        self.assertEqual(1, len(matches))
        self.assertEqual(V2_LEADER_TERM, matches[0].term)
        self.assertEqual(len(V2_LEADER_TERM) + 20, matches[0].start)

    def test_v2_repeated_leader_uses_globally_nearest_context_pair(self) -> None:
        from plugins.content_alert.catalog import ManagedKeywordCatalog

        text = (
            f"{V2_LEADER_TERM}{'甲' * 12}{V2_STRONG_CONTEXT_TERM}"
            f"{'乙' * 20}{V2_LEADER_TERM}{V2_STRONG_CONTEXT_TERM}"
        )
        expected_start = text.rindex(V2_LEADER_TERM)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "managed"
            _write_v2_political_generation(root, "generation-v2-nearest-pair")
            current = _activate(
                root,
                "generation-v2-nearest-pair",
                catalog_version=2,
            )
            catalog = ManagedKeywordCatalog(current)

            matches = catalog.match_message(Message(MessageSegment.text(text)))

        self.assertEqual(1, len(matches))
        self.assertEqual(V2_LEADER_TERM, matches[0].term)
        self.assertEqual(expected_start, matches[0].start)
        self.assertEqual(
            expected_start + len(V2_LEADER_TERM) + len(V2_STRONG_CONTEXT_TERM),
            matches[0].end,
        )

    def test_v2_overlapping_same_name_uses_the_later_qualified_occurrence(
        self,
    ) -> None:
        from plugins.content_alert.catalog import ManagedKeywordCatalog

        overlapping_leader = "人人"
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "managed"
            _write_v2_political_generation(
                root,
                "generation-v2-repeat",
                extra_entry_fields_by_term={
                    overlapping_leader: {
                        "subject_type": "leader_name",
                        "match_mode": "same_segment_context",
                        "entity_ref": "synthetic-leader-repeat",
                        "rank_level": "省部级副职",
                        "rank_basis": "重叠出现位置的合成测试任职依据",
                    }
                },
            )
            current = _activate(
                root,
                "generation-v2-repeat",
                catalog_version=2,
            )
            catalog = ManagedKeywordCatalog(current)
            text = f"人人人{'甲' * 12}{V2_STRONG_CONTEXT_TERM}"

            matches = catalog.match_message(Message(MessageSegment.text(text)))

        self.assertEqual([overlapping_leader], [match.term for match in matches])
        self.assertEqual(1, matches[0].start)

    def test_v2_strong_context_is_not_suppressed_by_overlapping_weak_context(
        self,
    ) -> None:
        from plugins.content_alert.catalog import ManagedKeywordCatalog

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "managed"
            _write_v2_political_generation(root, "generation-v2-context-overlap")
            current = _activate(
                root,
                "generation-v2-context-overlap",
                catalog_version=2,
            )
            catalog = ManagedKeywordCatalog(current)

            matches = catalog.match_message(
                Message(
                    MessageSegment.text(
                        f"{V2_LEADER_TERM}{V2_OVERLAPPING_WEAK_CONTEXT_TERM}"
                    )
                )
            )

        self.assertEqual([V2_LEADER_TERM], [match.term for match in matches])

    def test_v2_context_inside_leader_span_does_not_qualify_the_name(self) -> None:
        from plugins.content_alert.catalog import ManagedKeywordCatalog

        overlapping_context = "领导姓名"
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "managed"
            _write_v2_political_generation(
                root,
                "generation-v2-span",
                extra_entry_fields_by_term={
                    overlapping_context: {
                        "subject_type": "political_context",
                        "match_mode": "support_only",
                        "context_class": "case_proceeding",
                        "strength": "strong",
                    }
                },
            )
            current = _activate(
                root,
                "generation-v2-span",
                catalog_version=2,
            )
            catalog = ManagedKeywordCatalog(current)

            matches = catalog.match_message(
                Message(MessageSegment.text(V2_LEADER_TERM))
            )

        self.assertEqual((), matches)

    def test_v2_leader_context_never_crosses_message_segment_boundary(self) -> None:
        from plugins.content_alert.catalog import ManagedKeywordCatalog

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "managed"
            _write_v2_political_generation(root, "generation-v2-segments")
            current = _activate(
                root,
                "generation-v2-segments",
                catalog_version=2,
            )
            catalog = ManagedKeywordCatalog(current)

            split_messages = (
                Message(
                    [
                        MessageSegment.text(V2_LEADER_TERM),
                        MessageSegment.at(10001),
                        MessageSegment.text(V2_STRONG_CONTEXT_TERM),
                    ]
                ),
                Message(
                    [
                        MessageSegment.text(V2_LEADER_TERM),
                        MessageSegment.text(V2_STRONG_CONTEXT_TERM),
                    ]
                ),
            )
            matches = tuple(
                catalog.match_message(message) for message in split_messages
            )

        self.assertEqual(((), ()), matches)

    def test_v2_nontext_prefix_preserves_matching_text_segment_index(self) -> None:
        from plugins.content_alert.catalog import ManagedKeywordCatalog

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "managed"
            _write_v2_political_generation(root, "generation-v2-segment-index")
            current = _activate(
                root,
                "generation-v2-segment-index",
                catalog_version=2,
            )
            catalog = ManagedKeywordCatalog(current)

            prefixes = (
                MessageSegment.at(10001),
                MessageSegment.image("https://example.invalid/synthetic.jpg"),
                MessageSegment.reply(10002),
            )
            matches = tuple(
                catalog.match_message(
                    Message(
                        [
                            prefix,
                            MessageSegment.text(
                                f"{V2_LEADER_TERM}{V2_STRONG_CONTEXT_TERM}"
                            ),
                        ]
                    )
                )
                for prefix in prefixes
            )

        self.assertTrue(all(len(result) == 1 for result in matches))
        self.assertEqual(
            (1, 1, 1), tuple(result[0].segment_index for result in matches)
        )

    def test_v2_context_pairing_has_a_fail_closed_comparison_budget(self) -> None:
        from unittest.mock import patch

        import plugins.content_alert.catalog as catalog_module
        from plugins.content_alert.engine import ScalableLiteralScanLimitError

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "managed"
            _write_v2_political_generation(root, "generation-v2-pair-budget")
            current = _activate(
                root,
                "generation-v2-pair-budget",
                catalog_version=2,
            )
            catalog = catalog_module.ManagedKeywordCatalog(current)

            with (
                patch.object(
                    catalog_module,
                    "MAX_MANAGED_CONTEXT_COMPARISONS",
                    0,
                ),
                self.assertRaises(ScalableLiteralScanLimitError),
            ):
                catalog.match_message(
                    Message(
                        MessageSegment.text(f"{V2_LEADER_TERM}{V2_STRONG_CONTEXT_TERM}")
                    )
                )

    def test_managed_scan_has_cumulative_per_message_budgets(self) -> None:
        from unittest.mock import patch

        import plugins.content_alert.catalog as catalog_module
        from plugins.content_alert.engine import ScalableLiteralScanLimitError

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "managed"
            _write_v2_political_generation(root, "generation-v2-message-budget")
            current = _activate(
                root,
                "generation-v2-message-budget",
                catalog_version=2,
            )
            catalog = catalog_module.ManagedKeywordCatalog(current)

            cases = (
                (
                    "segment-count",
                    "MAX_MANAGED_MESSAGE_SEGMENTS",
                    2,
                    Message(
                        [
                            MessageSegment.at(10001),
                            MessageSegment.at(10002),
                            MessageSegment.at(10003),
                        ]
                    ),
                    "message_segment_limit",
                ),
                (
                    "total-text",
                    "MAX_MANAGED_MESSAGE_TEXT_CHARS",
                    5,
                    Message(
                        [
                            MessageSegment.text("甲乙丙"),
                            MessageSegment.at(10001),
                            MessageSegment.text("丁戊己"),
                        ]
                    ),
                    "message_text_limit",
                ),
                (
                    "total-matches",
                    "MAX_MANAGED_MESSAGE_MATCHES",
                    1,
                    Message(
                        [
                            MessageSegment.text(V2_EVENT_TERM),
                            MessageSegment.at(10001),
                            MessageSegment.text(V2_EVENT_TERM),
                        ]
                    ),
                    "message_match_limit",
                ),
            )
            for name, constant, value, message, reason in cases:
                with (
                    self.subTest(case=name),
                    patch.object(catalog_module, constant, value),
                    self.assertRaises(ScalableLiteralScanLimitError) as captured,
                ):
                    catalog.match_message(message)
                self.assertEqual(reason, captured.exception.reason)

    def test_invalid_v2_political_semantics_keep_last_known_good(self) -> None:
        from plugins.content_alert.catalog import ManagedKeywordCatalog

        invalid_variants = {
            "missing-subject": {
                "match_mode": "same_segment_context",
            },
            "leader-direct-missing-required-metadata": {
                "subject_type": "leader_name",
                "match_mode": "direct",
            },
            "unknown-subject": {
                "subject_type": "synthetic_unknown",
                "match_mode": "same_segment_context",
            },
        }
        invalid_context_variants = {
            "missing-strength": {
                "subject_type": "political_context",
                "match_mode": "support_only",
                "context_class": "case_proceeding",
            },
            "invalid-strength": {
                "subject_type": "political_context",
                "match_mode": "support_only",
                "context_class": "case_proceeding",
                "strength": "synthetic_invalid",
            },
            "invalid-context-class": {
                "subject_type": "political_context",
                "match_mode": "support_only",
                "context_class": "synthetic_invalid",
                "strength": "strong",
            },
            "unknown-entity-ref": {
                "subject_type": "political_context",
                "match_mode": "support_only",
                "context_class": "case_proceeding",
                "strength": "strong",
                "entity_refs": ["synthetic-leader-missing"],
            },
            "duplicate-entity-ref": {
                "subject_type": "political_context",
                "match_mode": "support_only",
                "context_class": "case_proceeding",
                "strength": "strong",
                "entity_refs": [
                    "synthetic-leader-0001",
                    "synthetic-leader-0001",
                ],
            },
        }

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "managed"
            _write_v2_political_generation(root, "generation-v2-good")
            current = _activate(
                root,
                "generation-v2-good",
                catalog_version=2,
            )
            catalog = ManagedKeywordCatalog(current)
            good = catalog.snapshot()

            variants = [
                *((name, fields, None) for name, fields in invalid_variants.items()),
                *(
                    (name, None, fields)
                    for name, fields in invalid_context_variants.items()
                ),
            ]
            for variant_index, (name, leader_fields, context_fields) in enumerate(
                variants,
                start=1,
            ):
                with self.subTest(variant=name):
                    # Keep fixture terms below the runtime literal limit so a
                    # semantic failure cannot pass for the wrong reason.
                    generation_id = f"generation-v2-bad-{variant_index}"
                    _write_v2_political_generation(
                        root,
                        generation_id,
                        leader_fields=leader_fields,
                        strong_context_fields=context_fields,
                    )
                    _activate(
                        root,
                        generation_id,
                        catalog_version=2,
                    )

                    retained = catalog.snapshot()
                    cold = ManagedKeywordCatalog(current).snapshot()

                    self.assertIs(good, retained)
                    self.assertFalse(cold.has_active_generation)
                    self.assertIsNotNone(catalog.last_error)

                    _activate(
                        root,
                        "generation-v2-good",
                        catalog_version=2,
                    )
                    self.assertIs(good, catalog.snapshot())

    def test_missing_malformed_hash_traversal_duplicate_and_over_limit_shards_keep_lkg(
        self,
    ) -> None:
        from plugins.content_alert.catalog import ManagedKeywordCatalog

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "managed"
            _write_generation(root, "generation-good")
            current = _activate(root, "generation-good")
            catalog = ManagedKeywordCatalog(current)
            good = catalog.snapshot()

            variants: list[tuple[str, str, Any]] = []

            missing = _write_generation(root, "generation-missing")
            missing["shard_paths"][0].unlink()
            variants.append(("missing", "generation-missing", missing))

            malformed = _write_generation(root, "generation-malformed")
            _rewrite_first_shard(malformed, b"{not-json")
            variants.append(("malformed", "generation-malformed", malformed))

            hash_mismatch = _write_generation(root, "generation-hash")
            _rewrite_manifest_shard(
                hash_mismatch["manifest_path"],
                sha256="0" * 64,
            )
            variants.append(("hash-mismatch", "generation-hash", hash_mismatch))

            traversal = _write_generation(root, "generation-traversal")
            outside = root / "outside.json"
            outside_payload = _json_bytes({"version": 1, "entries": []})
            outside.write_bytes(outside_payload)
            _rewrite_manifest_shard(
                traversal["manifest_path"],
                path="../../outside.json",
                sha256=hashlib.sha256(outside_payload).hexdigest(),
                entry_count=0,
            )
            variants.append(("path-traversal", "generation-traversal", traversal))

            duplicate = _write_generation(root, "generation-duplicate")
            duplicate_payload = json.loads(
                duplicate["shard_paths"][0].read_text(encoding="utf-8")
            )
            duplicate_payload["entries"].append(dict(duplicate_payload["entries"][0]))
            duplicate_bytes = _json_bytes(duplicate_payload)
            _rewrite_first_shard(duplicate, duplicate_bytes)
            _rewrite_manifest_shard(
                duplicate["manifest_path"],
                entry_count=2,
            )
            variants.append(("duplicate-id", "generation-duplicate", duplicate))

            over_limit = _write_generation(root, "generation-over-limit")
            _rewrite_manifest_shard(
                over_limit["manifest_path"],
                entry_count=50_001,
            )
            variants.append(
                ("declared-over-limit", "generation-over-limit", over_limit)
            )

            for variant_name, generation_id, _generation in variants:
                with self.subTest(variant=variant_name):
                    _activate(root, generation_id)
                    retained = catalog.snapshot()
                    cold = ManagedKeywordCatalog(current).snapshot()

                    self.assertIs(good, retained)
                    self.assertEqual("generation-good", retained.generation_id)
                    self.assertFalse(cold.has_active_generation)
                    self.assertEqual((), cold.entries)
                    self.assertIsNotNone(catalog.last_error)
                    _activate(root, "generation-good")
                    self.assertEqual(
                        "generation-good",
                        catalog.snapshot().generation_id,
                    )

    def test_boolean_pointer_manifest_and_shard_versions_are_rejected(self) -> None:
        from plugins.content_alert.catalog import ManagedKeywordCatalog

        for variant in ("pointer", "manifest", "shard"):
            with (
                self.subTest(variant=variant),
                tempfile.TemporaryDirectory() as directory,
            ):
                root = Path(directory) / "managed"
                _write_generation(root, "generation-good")
                current = _activate(root, "generation-good")
                catalog = ManagedKeywordCatalog(current)
                good = catalog.snapshot()

                bad = _write_generation(root, "generation-bool")
                if variant == "pointer":
                    _activate(root, "generation-bool", catalog_version=True)
                elif variant == "manifest":
                    manifest = json.loads(
                        bad["manifest_path"].read_text(encoding="utf-8")
                    )
                    manifest["version"] = True
                    _write_json(bad["manifest_path"], manifest)
                    _activate(root, "generation-bool")
                else:
                    shard_path = bad["shard_paths"][0]
                    shard = json.loads(shard_path.read_text(encoding="utf-8"))
                    shard["version"] = True
                    _rewrite_first_shard(bad, _json_bytes(shard))
                    _activate(root, "generation-bool")

                retained = catalog.snapshot()
                cold = ManagedKeywordCatalog(current).snapshot()

                self.assertIs(good, retained)
                self.assertFalse(cold.has_active_generation)
                self.assertIsNotNone(catalog.last_error)

    def test_deep_json_nesting_keeps_lkg_and_cold_start_empty(self) -> None:
        from plugins.content_alert.catalog import ManagedKeywordCatalog

        nested_payload = b'{"nested":' + b"[" * 2_000 + b"0" + b"]" * 2_000 + b"}"
        for variant in ("pointer", "manifest", "shard"):
            with (
                self.subTest(variant=variant),
                tempfile.TemporaryDirectory() as directory,
            ):
                root = Path(directory) / "managed"
                _write_v2_political_generation(root, "g-good")
                current = _activate(root, "g-good", catalog_version=2)
                catalog = ManagedKeywordCatalog(current)
                good = catalog.snapshot()

                generation_id = f"g-deep-{variant}"
                malformed = _write_v2_political_generation(root, generation_id)
                if variant == "pointer":
                    current.write_bytes(nested_payload)
                    current.chmod(0o600)
                elif variant == "manifest":
                    malformed["manifest_path"].write_bytes(nested_payload)
                    malformed["manifest_path"].chmod(0o600)
                    _activate(root, generation_id, catalog_version=2)
                else:
                    _rewrite_first_shard(malformed, nested_payload)
                    _activate(root, generation_id, catalog_version=2)

                retained = catalog.snapshot()
                cold_catalog = ManagedKeywordCatalog(current)
                cold = cold_catalog.snapshot()

                self.assertIs(good, retained)
                self.assertFalse(cold.has_active_generation)
                self.assertIsNotNone(catalog.last_error)
                self.assertIsNotNone(cold_catalog.last_error)

    def test_invalid_v2_review_and_provenance_contract_keeps_lkg_and_cold_empty(
        self,
    ) -> None:
        from plugins.content_alert.catalog import ManagedKeywordCatalog

        def add_alias(entry: dict[str, Any]) -> None:
            entry["aliases"] = ["合成别名"]

        def remove_rank(entry: dict[str, Any]) -> None:
            entry.pop("rank_level")

        def remove_review_tag(entry: dict[str, Any]) -> None:
            entry["tags"] = ["subject:leader_name"]

        def invalid_confidence(entry: dict[str, Any]) -> None:
            entry["confidence"] = True

        def invalid_verification(entry: dict[str, Any]) -> None:
            entry["verification_status"] = "synthetic-invalid"

        def remove_source_refs(entry: dict[str, Any]) -> None:
            entry.pop("source_refs")

        def remove_review_date(entry: dict[str, Any]) -> None:
            entry.pop("last_reviewed")

        def noncanonical_term(entry: dict[str, Any]) -> None:
            entry["term"] = "合成\ufe0f领导姓名甲"

        variants = {
            "alias": add_alias,
            "rank": remove_rank,
            "review-tag": remove_review_tag,
            "confidence": invalid_confidence,
            "verification": invalid_verification,
            "source-refs": remove_source_refs,
            "review-date": remove_review_date,
            "canonical-term": noncanonical_term,
        }

        for index, (variant, mutate) in enumerate(variants.items(), start=1):
            with (
                self.subTest(variant=variant),
                tempfile.TemporaryDirectory() as directory,
            ):
                root = Path(directory) / "managed"
                _write_v2_political_generation(root, "g-good")
                current = _activate(root, "g-good", catalog_version=2)
                catalog = ManagedKeywordCatalog(current)
                good = catalog.snapshot()

                generation_id = f"g-bad-{index}"
                malformed = _write_v2_political_generation(root, generation_id)
                shard = json.loads(
                    malformed["shard_paths"][0].read_text(encoding="utf-8")
                )
                mutate(shard["entries"][1])
                _rewrite_first_shard(malformed, _json_bytes(shard))
                _activate(root, generation_id, catalog_version=2)

                retained = catalog.snapshot()
                cold_catalog = ManagedKeywordCatalog(current)
                cold = cold_catalog.snapshot()

                self.assertIs(good, retained)
                self.assertFalse(cold.has_active_generation)
                self.assertIsNotNone(catalog.last_error)
                self.assertIsNotNone(cold_catalog.last_error)

    def test_v2_edge_whitespace_keeps_lkg_and_cold_start_empty(self) -> None:
        from plugins.content_alert.catalog import ManagedKeywordCatalog

        for entry_index in range(3):
            for edge in ("leading", "trailing"):
                with (
                    self.subTest(entry_index=entry_index, edge=edge),
                    tempfile.TemporaryDirectory() as directory,
                ):
                    root = Path(directory) / "managed"
                    _write_v2_political_generation(root, "g-good")
                    current = _activate(root, "g-good", catalog_version=2)
                    catalog = ManagedKeywordCatalog(current)
                    good = catalog.snapshot()

                    generation_id = f"g-edge-{entry_index}-{edge}"
                    malformed = _write_v2_political_generation(root, generation_id)
                    shard = json.loads(
                        malformed["shard_paths"][0].read_text(encoding="utf-8")
                    )
                    original = shard["entries"][entry_index]["term"]
                    shard["entries"][entry_index]["term"] = (
                        f" {original}" if edge == "leading" else f"{original} "
                    )
                    _rewrite_first_shard(malformed, _json_bytes(shard))
                    _activate(root, generation_id, catalog_version=2)

                    retained = catalog.snapshot()
                    cold_catalog = ManagedKeywordCatalog(current)
                    cold = cold_catalog.snapshot()

                    self.assertIs(good, retained)
                    self.assertFalse(cold.has_active_generation)
                    self.assertIsNotNone(catalog.last_error)
                    self.assertIsNotNone(cold_catalog.last_error)

    def test_v2_dead_political_generation_keeps_lkg_and_cold_start_empty(
        self,
    ) -> None:
        from plugins.content_alert.catalog import ManagedKeywordCatalog

        def disable_political(
            generation: dict[str, Any],
        ) -> None:
            manifest = json.loads(
                generation["manifest_path"].read_text(encoding="utf-8")
            )
            manifest["categories"][0]["enabled"] = False
            _write_json(generation["manifest_path"], manifest)

        def mutate_entries(
            generation: dict[str, Any],
            predicate: object,
        ) -> None:
            shard = json.loads(generation["shard_paths"][0].read_text(encoding="utf-8"))
            for entry in shard["entries"]:
                if predicate(entry):
                    entry["status"] = "disabled"
            _rewrite_first_shard(generation, _json_bytes(shard))

        variants = {
            "political-disabled": disable_political,
            "all-disabled": lambda generation: mutate_entries(
                generation,
                lambda _entry: True,
            ),
            "support-only": lambda generation: mutate_entries(
                generation,
                lambda entry: entry["subject_type"] != "political_context",
            ),
            "leader-without-strong-context": lambda generation: mutate_entries(
                generation,
                lambda entry: (
                    entry["subject_type"] == "historical_event"
                    or (
                        entry["subject_type"] == "political_context"
                        and entry["strength"] == "strong"
                    )
                ),
            ),
        }

        for index, (variant, mutate) in enumerate(variants.items(), start=1):
            with (
                self.subTest(variant=variant),
                tempfile.TemporaryDirectory() as directory,
            ):
                root = Path(directory) / "managed"
                _write_v2_political_generation(root, "g-good")
                current = _activate(root, "g-good", catalog_version=2)
                catalog = ManagedKeywordCatalog(current)
                good = catalog.snapshot()

                generation_id = f"g-dead-{index}"
                malformed = _write_v2_political_generation(root, generation_id)
                mutate(malformed)
                _activate(root, generation_id, catalog_version=2)

                retained = catalog.snapshot()
                cold_catalog = ManagedKeywordCatalog(current)
                cold = cold_catalog.snapshot()

                self.assertIs(good, retained)
                self.assertFalse(cold.has_active_generation)
                self.assertIsNotNone(catalog.last_error)
                self.assertIsNotNone(cold_catalog.last_error)

    def test_v2_unpinned_source_keeps_lkg_and_cold_start_empty(self) -> None:
        from plugins.content_alert.catalog import ManagedKeywordCatalog

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "managed"
            _write_v2_political_generation(root, "g-good")
            current = _activate(root, "g-good", catalog_version=2)
            catalog = ManagedKeywordCatalog(current)
            good = catalog.snapshot()

            malformed = _write_v2_political_generation(root, "g-unpinned")
            manifest = json.loads(
                malformed["manifest_path"].read_text(encoding="utf-8")
            )
            source = manifest["categories"][0]["sources"][0]
            source.pop("revision")
            source.pop("sha256")
            _write_json(malformed["manifest_path"], manifest)
            _activate(root, "g-unpinned", catalog_version=2)

            retained = catalog.snapshot()
            cold_catalog = ManagedKeywordCatalog(current)
            cold = cold_catalog.snapshot()

        self.assertIs(good, retained)
        self.assertFalse(cold.has_active_generation)
        self.assertIsNotNone(catalog.last_error)
        self.assertIsNotNone(cold_catalog.last_error)

    def test_v2_enabled_nonpolitical_active_entry_is_rejected(self) -> None:
        from plugins.content_alert.catalog import ManagedKeywordCatalog

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "managed"
            malformed = _write_v2_political_generation(root, "g-nonpolitical")
            manifest = json.loads(
                malformed["manifest_path"].read_text(encoding="utf-8")
            )
            manifest["categories"][1]["enabled"] = True
            _write_json(malformed["manifest_path"], manifest)
            current = _activate(root, "g-nonpolitical", catalog_version=2)
            catalog = ManagedKeywordCatalog(current)

            snapshot = catalog.snapshot()

        self.assertFalse(snapshot.has_active_generation)
        self.assertIsNotNone(catalog.last_error)

    def test_v2_enabled_category_rejects_nonalerting_entry_statuses(self) -> None:
        from plugins.content_alert.catalog import ManagedKeywordCatalog

        for status in ("shadow", "disabled"):
            with (
                self.subTest(status=status),
                tempfile.TemporaryDirectory() as directory,
            ):
                root = Path(directory) / "managed"
                malformed = _write_v2_political_generation(
                    root,
                    f"g-nonalerting-{status}",
                )
                shard_path = malformed["shard_paths"][0]
                shard = json.loads(shard_path.read_text(encoding="utf-8"))
                leader = next(
                    entry
                    for entry in shard["entries"]
                    if entry.get("subject_type") == "leader_name"
                )
                leader["status"] = status
                _rewrite_first_shard(malformed, _json_bytes(shard))
                current = _activate(
                    root,
                    f"g-nonalerting-{status}",
                    catalog_version=2,
                )
                catalog = ManagedKeywordCatalog(current)

                snapshot = catalog.snapshot()

            self.assertFalse(snapshot.has_active_generation)
            self.assertIsNotNone(catalog.last_error)

    @unittest.skipUnless(hasattr(os, "symlink"), "symlink is unavailable")
    def test_symlinked_shard_keeps_lkg_and_cold_start_is_empty(self) -> None:
        from plugins.content_alert.catalog import ManagedKeywordCatalog

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "managed"
            _write_generation(root, "generation-good")
            current = _activate(root, "generation-good")
            catalog = ManagedKeywordCatalog(current)
            good = catalog.snapshot()

            linked = _write_generation(root, "generation-symlink")
            shard_path = linked["shard_paths"][0]
            outside = root / "outside-shard.json"
            payload = shard_path.read_bytes()
            outside.write_bytes(payload)
            shard_path.unlink()
            os.symlink(outside, shard_path)
            _activate(root, "generation-symlink")

            retained = catalog.snapshot()
            cold = ManagedKeywordCatalog(current).snapshot()

        self.assertIs(good, retained)
        self.assertFalse(cold.has_active_generation)
        self.assertEqual((), cold.entries)

    def test_group_readable_managed_file_keeps_lkg_and_cold_start_is_empty(
        self,
    ) -> None:
        from plugins.content_alert.catalog import ManagedKeywordCatalog

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "managed"
            _write_generation(root, "generation-good")
            current = _activate(root, "generation-good")
            catalog = ManagedKeywordCatalog(current)
            good = catalog.snapshot()

            exposed = _write_generation(root, "generation-exposed")
            exposed["shard_paths"][0].chmod(0o640)
            _activate(root, "generation-exposed")

            retained = catalog.snapshot()
            cold = ManagedKeywordCatalog(current).snapshot()

        self.assertIs(good, retained)
        self.assertFalse(cold.has_active_generation)
        self.assertEqual((), cold.entries)

    def test_normalized_duplicates_merge_provenance_and_strictest_policy_wins(
        self,
    ) -> None:
        from plugins.content_alert.catalog import ManagedKeywordCatalog

        category_specs = (*CATEGORY_SPECS, STRICT_CATEGORY_SPEC)
        duplicate_terms = {
            category_id: [_term(category_id, "generation-merged", 1)]
            for category_id, _name_zh, _policy in category_specs
        }
        duplicate_terms["restricted_internal"] = ["ＡＢ 保护占位"]
        duplicate_terms["controversial_topics"] = ["ab\u200b保护占位"]

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "managed"
            _write_generation(
                root,
                "generation-merged",
                terms_by_category=duplicate_terms,
                category_specs=category_specs,
            )
            current = _activate(root, "generation-merged")
            catalog = ManagedKeywordCatalog(current)

            matches = catalog.match_message(
                Message(MessageSegment.text("前缀 ab保护占位 后缀"))
            )

        self.assertEqual(1, len(matches))
        match = matches[0]
        self.assertEqual(
            {"restricted_internal", "controversial_topics"},
            set(match.category_ids),
        )
        self.assertEqual(
            {"受保护占位分类", "普通占位分类三"},
            set(match.category_names),
        )
        self.assertEqual("strict_hidden", match.disclosure_policy)

    def test_political_alias_variants_match_as_management_visible(self) -> None:
        from plugins.content_alert.catalog import ManagedKeywordCatalog

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "managed"
            generation = _write_generation(root, "generation-aliases")
            shard_path = generation["shard_paths"][0]
            shard = json.loads(shard_path.read_text(encoding="utf-8"))
            shard["entries"][0]["aliases"] = [
                "保护 代称",
                "BHD C",
                "保护\u200b拆写",
            ]
            payload = _json_bytes(shard)
            _rewrite_first_shard(generation, payload)
            current = _activate(root, "generation-aliases")
            catalog = ManagedKeywordCatalog(current)

            for text in ("保护代称", "bhdc", "保护拆写"):
                with self.subTest(text=text):
                    matches = catalog.match_message(Message(MessageSegment.text(text)))
                    self.assertEqual(1, len(matches))
                    self.assertEqual(
                        "management_visible",
                        matches[0].disclosure_policy,
                    )
                    self.assertIn("political_cn", matches[0].category_ids)

    def test_visible_longer_match_cannot_suppress_overlapping_hidden_match(
        self,
    ) -> None:
        from plugins.content_alert.catalog import ManagedKeywordCatalog

        category_specs = (*CATEGORY_SPECS, STRICT_CATEGORY_SPEC)
        terms_by_category = {
            category_id: [_term(category_id, "generation-overlap-policy", 1)]
            for category_id, _name_zh, _policy in category_specs
        }
        terms_by_category["restricted_internal"] = ["保护短词"]
        terms_by_category["controversial_topics"] = ["前保护短词后"]

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "managed"
            _write_generation(
                root,
                "generation-overlap-policy",
                terms_by_category=terms_by_category,
                category_specs=category_specs,
            )
            current = _activate(root, "generation-overlap-policy")
            catalog = ManagedKeywordCatalog(current)

            matches = catalog.match_message(
                Message(MessageSegment.text("前保护短词后"))
            )

        self.assertIn("strict_hidden", {match.disclosure_policy for match in matches})
        self.assertIn(
            "restricted_internal",
            {category_id for match in matches for category_id in match.category_ids},
        )

    def test_ten_thousand_entries_match_tail_and_reuse_compiled_snapshot(self) -> None:
        from plugins.content_alert.catalog import ManagedKeywordCatalog

        counts = (1_429, 1_429, 1_429, 1_429, 1_428, 1_428, 1_428)
        terms_by_category = {
            category_id: [
                _term(category_id, "generation-scale", index) for index in range(count)
            ]
            for category_id, count in zip(CATEGORY_IDS, counts, strict=True)
        }
        tail_term = terms_by_category["terrorism"][-1]

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "managed"
            generation = _write_generation(
                root,
                "generation-scale",
                terms_by_category=terms_by_category,
                shard_size=250,
            )
            current = _activate(root, "generation-scale")
            catalog = ManagedKeywordCatalog(current)

            first = catalog.snapshot()
            initial_matches = catalog.match_message(
                Message(MessageSegment.text(f"头部 {tail_term} 尾部"))
            )
            for shard_path in generation["shard_paths"]:
                shard_path.unlink()
            second = catalog.snapshot()
            cached_matches = catalog.match_message(
                Message(MessageSegment.text(f"再次 {tail_term}"))
            )

        self.assertEqual(10_000, len(first.entries))
        self.assertEqual([tail_term], [match.term for match in initial_matches])
        self.assertIs(first, second)
        self.assertEqual([tail_term], [match.term for match in cached_matches])
        self.assertIsNone(catalog.last_error)


if __name__ == "__main__":
    unittest.main()
