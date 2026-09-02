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
    ("political_cn", "受保护占位分类", "strict_hidden"),
    ("sexual_explicit", "普通占位分类一", "management_visible"),
    ("gender_conflict", "普通占位分类二", "management_visible"),
    ("controversial_topics", "普通占位分类三", "management_visible"),
    ("anime_game_controversy", "普通占位分类四", "management_visible"),
    ("graphic_violence", "普通占位分类五", "management_visible"),
    ("terrorism", "普通占位分类六", "management_visible"),
)
CATEGORY_IDS = tuple(item[0] for item in CATEGORY_SPECS)


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
    policy_overrides: dict[str, str] | None = None,
    shard_size: int = 1_000,
) -> dict[str, Any]:
    generation_root = root / "generations" / generation_id
    terms_by_category = terms_by_category or {
        category_id: [_term(category_id, generation_id, 1)]
        for category_id in CATEGORY_IDS
    }
    policy_overrides = policy_overrides or {}
    categories: list[dict[str, Any]] = []
    shard_paths: list[Path] = []

    for category_id, name_zh, default_policy in CATEGORY_SPECS:
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
                for index, term in enumerate(terms, start=1)
            ]
            shard_path = (
                generation_root
                / "shards"
                / f"{category_id}-{shard_index:04d}.json"
            )
            payload = _write_json(
                shard_path,
                {
                    "version": 1,
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
                "enabled": True,
                "version": "2026.09.02.1",
                "sources": [
                    {
                        "source_id": "synthetic-fixture",
                        "reference": "synthetic://fixture",
                        "license": "synthetic-test-only",
                        "retrieved_at": "2026-09-02T00:00:00Z",
                    }
                ],
                "shards": shards,
            }
        )

    manifest_path = generation_root / "manifest.json"
    _write_json(
        manifest_path,
        {
            "version": 1,
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


def _activate(root: Path, generation_id: str) -> Path:
    current_path = root / "current.json"
    temporary = root / f".current-{generation_id}.tmp"
    _write_json(
        temporary,
        {
            "version": 1,
            "generation_id": generation_id,
            "manifest": f"generations/{generation_id}/manifest.json",
        },
    )
    os.replace(temporary, current_path)
    return current_path


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
                {"strict_hidden", "management_visible"},
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

    def test_political_policy_cannot_be_downgraded_and_keeps_last_known_good(
        self,
    ) -> None:
        from plugins.content_alert.catalog import ManagedKeywordCatalog

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "managed"
            _write_generation(root, "generation-good")
            current = _activate(root, "generation-good")
            catalog = ManagedKeywordCatalog(current)
            good = catalog.snapshot()

            _write_generation(
                root,
                "generation-downgraded",
                policy_overrides={"political_cn": "management_visible"},
            )
            _activate(root, "generation-downgraded")
            retained = catalog.snapshot()
            cold = ManagedKeywordCatalog(current).snapshot()

        self.assertIs(good, retained)
        self.assertEqual("generation-good", retained.generation_id)
        self.assertFalse(cold.has_active_generation)
        self.assertEqual("", cold.generation_id)
        self.assertEqual((), cold.categories)
        self.assertEqual((), cold.entries)

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
            variants.append(
                ("path-traversal", "generation-traversal", traversal)
            )

            duplicate = _write_generation(root, "generation-duplicate")
            duplicate_payload = json.loads(
                duplicate["shard_paths"][0].read_text(encoding="utf-8")
            )
            duplicate_payload["entries"].append(
                dict(duplicate_payload["entries"][0])
            )
            duplicate_bytes = _json_bytes(duplicate_payload)
            _rewrite_first_shard(duplicate, duplicate_bytes)
            _rewrite_manifest_shard(
                duplicate["manifest_path"],
                entry_count=2,
            )
            variants.append(
                ("duplicate-id", "generation-duplicate", duplicate)
            )

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

        duplicate_terms = {
            category_id: [_term(category_id, "generation-merged", 1)]
            for category_id in CATEGORY_IDS
        }
        duplicate_terms["political_cn"] = ["ＡＢ 保护占位"]
        duplicate_terms["controversial_topics"] = ["ab\u200b保护占位"]

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "managed"
            _write_generation(
                root,
                "generation-merged",
                terms_by_category=duplicate_terms,
            )
            current = _activate(root, "generation-merged")
            catalog = ManagedKeywordCatalog(current)

            matches = catalog.match_message(
                Message(MessageSegment.text("前缀 ab保护占位 后缀"))
            )

        self.assertEqual(1, len(matches))
        match = matches[0]
        self.assertEqual(
            {"political_cn", "controversial_topics"},
            set(match.category_ids),
        )
        self.assertEqual(
            {"受保护占位分类", "普通占位分类三"},
            set(match.category_names),
        )
        self.assertEqual("strict_hidden", match.disclosure_policy)

    def test_political_alias_variants_match_as_strict_hidden(self) -> None:
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
                    matches = catalog.match_message(
                        Message(MessageSegment.text(text))
                    )
                    self.assertEqual(1, len(matches))
                    self.assertEqual("strict_hidden", matches[0].disclosure_policy)
                    self.assertIn("political_cn", matches[0].category_ids)

    def test_visible_longer_match_cannot_suppress_overlapping_political_match(
        self,
    ) -> None:
        from plugins.content_alert.catalog import ManagedKeywordCatalog

        terms_by_category = {
            category_id: [_term(category_id, "generation-overlap-policy", 1)]
            for category_id in CATEGORY_IDS
        }
        terms_by_category["political_cn"] = ["保护短词"]
        terms_by_category["controversial_topics"] = ["前保护短词后"]

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "managed"
            _write_generation(
                root,
                "generation-overlap-policy",
                terms_by_category=terms_by_category,
            )
            current = _activate(root, "generation-overlap-policy")
            catalog = ManagedKeywordCatalog(current)

            matches = catalog.match_message(
                Message(MessageSegment.text("前保护短词后"))
            )

        self.assertIn("strict_hidden", {match.disclosure_policy for match in matches})
        self.assertIn(
            "political_cn",
            {
                category_id
                for match in matches
                for category_id in match.category_ids
            },
        )

    def test_ten_thousand_entries_match_tail_and_reuse_compiled_snapshot(self) -> None:
        from plugins.content_alert.catalog import ManagedKeywordCatalog

        counts = (1_429, 1_429, 1_429, 1_429, 1_428, 1_428, 1_428)
        terms_by_category = {
            category_id: [
                _term(category_id, "generation-scale", index)
                for index in range(count)
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
