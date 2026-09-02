from __future__ import annotations

import hashlib
import io
import json
import os
import stat
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import patch


CATEGORY_SPECS = (
    ("political_cn", "测试类别甲", "合成测试类别甲", "critical", "strict_hidden"),
    ("sexual_explicit", "测试类别乙", "合成测试类别乙", "high", "management_visible"),
    ("gender_conflict", "测试类别丙", "合成测试类别丙", "medium", "management_visible"),
    ("controversial_topics", "测试类别丁", "合成测试类别丁", "medium", "management_visible"),
    ("anime_game_controversy", "测试类别戊", "合成测试类别戊", "medium", "management_visible"),
    ("graphic_violence", "测试类别己", "合成测试类别己", "high", "management_visible"),
    ("terrorism", "测试类别庚", "合成测试类别庚", "critical", "management_visible"),
)


class ContentAlertCatalogImporterTests(unittest.TestCase):
    def setUp(self) -> None:
        try:
            from scripts import import_content_alert_catalog
        except ImportError as exc:
            self.fail(str(exc))
        self.importer = import_content_alert_catalog
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.base = Path(self.temporary.name).resolve()
        self.instance_root = self.base / "instance"
        self.instance_root.mkdir(mode=0o700)
        self.build_path = self.base / "private-build.json"
        self._write_build(self.build_path, revision="fixture-v1")

    @staticmethod
    def _source(revision: str) -> dict[str, object]:
        return {
            "source_id": "synthetic-fixture",
            "title": "Synthetic test fixture",
            "url": "https://example.invalid/content-alert-fixture",
            "license": "test-only",
            "retrieved_at": "2026-09-02T00:00:00Z",
            "revision": revision,
            "sha256": hashlib.sha256(revision.encode("utf-8")).hexdigest(),
        }

    def _build_document(self, *, revision: str) -> dict[str, object]:
        categories: list[dict[str, object]] = []
        for index, (category_id, name, description, severity, policy) in enumerate(
            CATEGORY_SPECS,
            start=1,
        ):
            categories.append(
                {
                    "id": category_id,
                    "name_zh": name,
                    "description": description,
                    "severity": severity,
                    "disclosure_policy": policy,
                    "enabled": True,
                    "version": 1,
                    "sources": [self._source(revision)],
                    "entries": [
                        {
                            "id": f"T{index:04d}",
                            "term": f"占位规则{index:02d}-{revision}",
                            "aliases": [f"占位别名{index:02d}-{revision}"],
                            "status": "active",
                        }
                    ],
                }
            )
        return {
            "version": 1,
            "generated_at": "2026-09-02T00:00:00Z",
            "categories": categories,
        }

    def _write_build(self, path: Path, *, revision: str) -> None:
        path.write_text(
            json.dumps(
                self._build_document(revision=revision),
                ensure_ascii=False,
                indent=2,
                sort_keys=True,
            )
            + "\n",
            encoding="utf-8",
        )
        path.chmod(0o600)

    def _invoke(self, *arguments: str) -> tuple[int, str]:
        stdout = io.StringIO()
        stderr = io.StringIO()
        with redirect_stdout(stdout), redirect_stderr(stderr):
            result = self.importer.main(list(arguments))
        self.assertIsInstance(result, int)
        return result, stdout.getvalue() + stderr.getvalue()

    def _apply(
        self,
        *,
        root: Path | None = None,
        build: Path | None = None,
    ) -> tuple[int, str]:
        return self._invoke(
            "--instance-root",
            str(root or self.instance_root),
            "--build",
            str(build or self.build_path),
            "--apply",
        )

    def _rollback(self, *, root: Path | None = None) -> tuple[int, str]:
        return self._invoke(
            "--instance-root",
            str(root or self.instance_root),
            "--rollback",
        )

    @staticmethod
    def _current_path(root: Path) -> Path:
        return root / "data" / "content_alert" / "managed" / "current.json"

    def _current_and_manifest(
        self,
        root: Path | None = None,
    ) -> tuple[dict[str, object], Path, dict[str, object]]:
        instance_root = root or self.instance_root
        current_path = self._current_path(instance_root)
        current = json.loads(current_path.read_text(encoding="utf-8"))
        manifest_relative = Path(current["manifest"])
        self.assertFalse(manifest_relative.is_absolute())
        self.assertNotIn("..", manifest_relative.parts)
        manifest_path = current_path.parent / manifest_relative
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        return current, manifest_path, manifest

    @staticmethod
    def _mode(path: Path) -> int:
        return stat.S_IMODE(path.stat(follow_symlinks=False).st_mode)

    @staticmethod
    def _tree_snapshot(root: Path) -> dict[str, tuple[int, int, bytes]]:
        snapshot: dict[str, tuple[int, int, bytes]] = {}
        for path in sorted(root.rglob("*")):
            if path.name.endswith(".lock"):
                continue
            relative = str(path.relative_to(root))
            mode = stat.S_IMODE(path.stat(follow_symlinks=False).st_mode)
            if path.is_file() and not path.is_symlink():
                snapshot[relative] = (mode, path.stat().st_mtime_ns, path.read_bytes())
            else:
                snapshot[relative] = (mode, path.stat().st_mtime_ns, b"")
        return snapshot

    def test_apply_writes_hashed_generation_with_private_permissions_and_atomic_pointer(
        self,
    ) -> None:
        real_replace = os.replace
        with patch.object(self.importer.os, "replace", wraps=real_replace) as replace:
            code, output = self._apply()

        self.assertEqual(0, code, output)
        current, manifest_path, manifest = self._current_and_manifest()
        current_path = self._current_path(self.instance_root)
        generation_id = current["generation_id"]
        self.assertEqual(generation_id, manifest["generation_id"])
        self.assertEqual(1, current["version"])

        generation_root = manifest_path.parent
        for directory in (
            current_path.parent,
            generation_root.parent,
            generation_root,
            generation_root / "shards",
        ):
            self.assertTrue(directory.is_dir(), directory)
            self.assertFalse(directory.is_symlink(), directory)
            self.assertEqual(0o700, self._mode(directory), directory)

        self.assertEqual(0o600, self._mode(current_path))
        self.assertEqual(0o600, self._mode(manifest_path))
        self.assertFalse(current_path.is_symlink())
        self.assertFalse(manifest_path.is_symlink())

        seen_categories: set[str] = set()
        for category in manifest["categories"]:
            category_id = category["id"]
            seen_categories.add(category_id)
            for descriptor in category["shards"]:
                shard_relative = Path(descriptor["path"])
                self.assertFalse(shard_relative.is_absolute())
                self.assertNotIn("..", shard_relative.parts)
                shard_path = generation_root / shard_relative
                payload = shard_path.read_bytes()
                self.assertEqual(
                    descriptor["sha256"],
                    hashlib.sha256(payload).hexdigest(),
                )
                self.assertEqual(0o600, self._mode(shard_path))
                self.assertFalse(shard_path.is_symlink())
                shard = json.loads(payload.decode("utf-8"))
                self.assertEqual(generation_id, shard["generation_id"])
                self.assertEqual(category_id, shard["category_id"])
                self.assertEqual(descriptor["entry_count"], len(shard["entries"]))
        self.assertEqual({item[0] for item in CATEGORY_SPECS}, seen_categories)

        pointer_replaces = [
            call
            for call in replace.call_args_list
            if Path(call.args[1]) == current_path
        ]
        self.assertEqual(1, len(pointer_replaces), replace.call_args_list)
        temporary_pointer = Path(pointer_replaces[0].args[0])
        self.assertEqual(current_path.parent, temporary_pointer.parent)
        self.assertNotEqual(current_path, temporary_pointer)

        from plugins.content_alert.catalog import ManagedKeywordCatalog

        runtime_catalog = ManagedKeywordCatalog(current_path)
        runtime_snapshot = runtime_catalog.snapshot()
        self.assertTrue(runtime_snapshot.has_active_generation)
        self.assertEqual(generation_id, runtime_snapshot.generation_id)
        self.assertIsNone(runtime_catalog.last_error)

    def test_importer_rejects_documents_the_runtime_would_reject(self) -> None:
        variants: list[tuple[str, dict[str, object]]] = []

        empty_disabled = self._build_document(revision="fixture-empty-disabled")
        empty_disabled["categories"][0]["enabled"] = False
        empty_disabled["categories"][0]["entries"] = []
        variants.append(("empty-disabled-category", empty_disabled))

        duplicate_id = self._build_document(revision="fixture-global-id")
        duplicate_id["categories"][1]["entries"][0]["id"] = duplicate_id[
            "categories"
        ][0]["entries"][0]["id"]
        variants.append(("global-duplicate-entry-id", duplicate_id))

        too_many_sources = self._build_document(revision="fixture-sources")
        original_source = too_many_sources["categories"][0]["sources"][0]
        too_many_sources["categories"][0]["sources"] = [
            dict(original_source, source_id=f"source-{index:03d}")
            for index in range(65)
        ]
        variants.append(("too-many-sources", too_many_sources))

        long_reference = self._build_document(revision="fixture-reference")
        long_reference["categories"][0]["sources"][0]["reference"] = "x" * 1025
        variants.append(("long-source-reference", long_reference))

        multiline_label = self._build_document(revision="fixture-multiline")
        multiline_label["categories"][0]["name_zh"] = "测试\n类别"
        variants.append(("multiline-category-label", multiline_label))

        for index, (name, document) in enumerate(variants, start=1):
            with self.subTest(variant=name):
                build_path = self.base / f"invalid-runtime-{index}.json"
                build_path.write_text(
                    json.dumps(document, ensure_ascii=False, sort_keys=True),
                    encoding="utf-8",
                )
                build_path.chmod(0o600)
                root = self.base / f"invalid-runtime-instance-{index}"
                root.mkdir(mode=0o700)

                code, output = self._apply(root=root, build=build_path)

                self.assertNotEqual(0, code, output)
                self.assertFalse(self._current_path(root).exists())

    def test_active_pattern_limit_ignores_shadow_and_global_duplicates(self) -> None:
        duplicate_active = self._build_document(revision="fixture-active-limit")
        for category in duplicate_active["categories"]:
            category["entries"][0]["term"] = "全局相同占位词"
            category["entries"][0]["aliases"] = []

        with patch.object(self.importer, "MAX_TOTAL_PATTERNS", 1):
            self.importer._validate_build(duplicate_active)

            second_unique = json.loads(json.dumps(duplicate_active))
            second_unique["categories"][0]["entries"][0]["term"] = (
                "第二个唯一占位词"
            )
            with self.assertRaises(self.importer.CatalogImportError):
                self.importer._validate_build(second_unique)

            all_shadow = self._build_document(revision="fixture-shadow-limit")
            for category in all_shadow["categories"]:
                category["entries"][0]["status"] = "shadow"
                category["entries"][0]["aliases"] = [
                    f"影子别名-{category['id']}"
                ]
            self.importer._validate_build(all_shadow)

        with patch.object(self.importer, "MAX_MANAGED_TRIE_NODES", 3):
            with self.assertRaisesRegex(
                self.importer.CatalogImportError,
                "trie node limit",
            ):
                self.importer._validate_build(duplicate_active)

    def test_runtime_file_size_limits_are_checked_before_generation_write(self) -> None:
        generated_at, categories = self.importer._validate_build(
            self._build_document(revision="fixture-v1")
        )
        prepared = self.importer._prepare_generation(generated_at, categories)
        manifest_size = len(prepared.files[Path("manifest.json")])
        shard_size = max(
            len(payload)
            for relative, payload in prepared.files.items()
            if relative != Path("manifest.json")
        )
        cases = (
            ("MAX_POINTER_BYTES", len(prepared.pointer_bytes) - 1),
            ("MAX_MANIFEST_BYTES", manifest_size - 1),
            ("MAX_SHARD_BYTES", shard_size - 1),
        )

        for index, (constant, limit) in enumerate(cases, start=1):
            with self.subTest(constant=constant):
                root = self.base / f"runtime-limit-instance-{index}"
                root.mkdir(mode=0o700)
                with patch.object(self.importer, constant, limit):
                    code, output = self._apply(root=root)

                self.assertNotEqual(0, code, output)
                self.assertFalse(self._current_path(root).exists())
                self.assertFalse(
                    (
                        root
                        / "data"
                        / "content_alert"
                        / "managed"
                        / "generations"
                    ).exists()
                )

    def test_same_input_is_a_true_no_op_and_conflicting_generation_aborts(self) -> None:
        first_code, first_output = self._apply()
        self.assertEqual(0, first_code, first_output)
        managed_root = self._current_path(self.instance_root).parent
        before = self._tree_snapshot(managed_root)

        second_code, second_output = self._apply()

        self.assertEqual(0, second_code, second_output)
        self.assertEqual(before, self._tree_snapshot(managed_root))

        current_before = self._current_path(self.instance_root).read_bytes()
        _, manifest_path, manifest = self._current_and_manifest()
        first_shard = manifest_path.parent / manifest["categories"][0]["shards"][0][
            "path"
        ]
        first_shard.write_bytes(first_shard.read_bytes() + b"\n")
        first_shard.chmod(0o600)

        conflict_code, conflict_output = self._apply()

        self.assertNotEqual(0, conflict_code, conflict_output)
        self.assertEqual(
            current_before,
            self._current_path(self.instance_root).read_bytes(),
        )
        for category in self._build_document(revision="fixture-v1")["categories"]:
            for entry in category["entries"]:
                self.assertNotIn(entry["term"], conflict_output)
                self.assertNotIn(entry["aliases"][0], conflict_output)

    def test_validation_and_pointer_publication_fail_closed(self) -> None:
        first_code, first_output = self._apply()
        self.assertEqual(0, first_code, first_output)
        current_path = self._current_path(self.instance_root)
        current_before = current_path.read_bytes()
        generations_before = {
            path.name
            for path in (current_path.parent / "generations").iterdir()
            if path.is_dir()
        }

        invalid_path = self.base / "invalid-build.json"
        invalid = self._build_document(revision="fixture-invalid")
        invalid["categories"][0]["disclosure_policy"] = "management_visible"
        invalid_path.write_text(
            json.dumps(invalid, ensure_ascii=False, sort_keys=True),
            encoding="utf-8",
        )
        invalid_path.chmod(0o600)

        invalid_code, invalid_output = self._apply(build=invalid_path)

        self.assertNotEqual(0, invalid_code, invalid_output)
        self.assertEqual(current_before, current_path.read_bytes())
        self.assertEqual(
            generations_before,
            {
                path.name
                for path in (current_path.parent / "generations").iterdir()
                if path.is_dir()
            },
        )

        second_path = self.base / "second-build.json"
        self._write_build(second_path, revision="fixture-v2")
        real_replace = os.replace

        def fail_current_pointer(source: object, destination: object) -> None:
            if Path(destination) == current_path:
                raise OSError("synthetic pointer publication failure")
            real_replace(source, destination)

        with patch.object(
            self.importer.os,
            "replace",
            side_effect=fail_current_pointer,
        ):
            failure_code, failure_output = self._apply(build=second_path)

        self.assertNotEqual(0, failure_code, failure_output)
        self.assertEqual(current_before, current_path.read_bytes())

        third_path = self.base / "third-build.json"
        self._write_build(third_path, revision="fixture-v3")
        real_atomic_write = self.importer._atomic_write_private
        pointer_writes = 0

        def publish_then_fail(path: Path, payload: bytes) -> None:
            nonlocal pointer_writes
            real_atomic_write(path, payload)
            if path == current_path:
                pointer_writes += 1
                if pointer_writes == 1:
                    raise OSError("synthetic post-publication failure")

        with patch.object(
            self.importer,
            "_atomic_write_private",
            side_effect=publish_then_fail,
        ):
            post_code, post_output = self._apply(build=third_path)

        self.assertNotEqual(0, post_code, post_output)
        self.assertEqual(current_before, current_path.read_bytes())
        self.assertGreaterEqual(pointer_writes, 2)

    def test_rollback_restores_previous_pointer_or_removes_first_pointer(self) -> None:
        first_code, first_output = self._apply()
        self.assertEqual(0, first_code, first_output)
        current_path = self._current_path(self.instance_root)
        first_pointer = current_path.read_bytes()

        second_path = self.base / "second-build.json"
        self._write_build(second_path, revision="fixture-v2")
        second_code, second_output = self._apply(build=second_path)
        self.assertEqual(0, second_code, second_output)
        second_pointer = current_path.read_bytes()
        self.assertNotEqual(first_pointer, second_pointer)
        generation_names = {
            path.name
            for path in (current_path.parent / "generations").iterdir()
            if path.is_dir()
        }

        rollback_code, rollback_output = self._rollback()

        self.assertEqual(0, rollback_code, rollback_output)
        self.assertEqual(first_pointer, current_path.read_bytes())
        self.assertEqual(
            generation_names,
            {
                path.name
                for path in (current_path.parent / "generations").iterdir()
                if path.is_dir()
            },
        )

        fresh_root = self.base / "fresh-instance"
        fresh_root.mkdir(mode=0o700)
        fresh_code, fresh_output = self._apply(root=fresh_root)
        self.assertEqual(0, fresh_code, fresh_output)
        fresh_current = self._current_path(fresh_root)
        self.assertTrue(fresh_current.is_file())

        remove_code, remove_output = self._rollback(root=fresh_root)

        self.assertEqual(0, remove_code, remove_output)
        self.assertFalse(fresh_current.exists())
        self.assertTrue((fresh_current.parent / "generations").is_dir())

    def test_legacy_rules_are_imported_into_strict_category_without_mutation(self) -> None:
        legacy_path = (
            self.instance_root / "data" / "content_alert" / "background_keywords.json"
        )
        legacy_path.parent.mkdir(parents=True, mode=0o700)
        legacy_document = {
            "version": 1,
            "revision": 1,
            "updated_at": "2026-09-02T00:00:00Z",
            "updated_by": "synthetic-fixture",
            "next_rule_number": 61,
            "rules": [
                {"id": f"K{index:04d}", "pattern": f"历史占位规则{index:02d}"}
                for index in range(1, 61)
            ],
        }
        legacy_path.write_text(
            json.dumps(legacy_document, ensure_ascii=False, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        legacy_path.chmod(0o600)
        legacy_before = legacy_path.read_bytes()
        legacy_mode = self._mode(legacy_path)

        code, output = self._apply()

        self.assertEqual(0, code, output)
        self.assertEqual(legacy_before, legacy_path.read_bytes())
        self.assertEqual(legacy_mode, self._mode(legacy_path))
        _, manifest_path, manifest = self._current_and_manifest()
        political = next(
            category
            for category in manifest["categories"]
            if category["id"] == "political_cn"
        )
        self.assertEqual("strict_hidden", political["disclosure_policy"])
        entries: list[dict[str, object]] = []
        for descriptor in political["shards"]:
            shard = json.loads(
                (manifest_path.parent / descriptor["path"]).read_text(
                    encoding="utf-8"
                )
            )
            entries.extend(shard["entries"])
        terms = {entry["term"] for entry in entries}
        self.assertTrue(
            {f"历史占位规则{index:02d}" for index in range(1, 61)}.issubset(terms)
        )

        backup_root = self.instance_root / "backups" / "content-alert"
        self.assertTrue(backup_root.is_dir())
        self.assertEqual(0o700, self._mode(backup_root))
        backup_files = [path for path in backup_root.rglob("*") if path.is_file()]
        self.assertTrue(
            any(path.read_bytes() == legacy_before for path in backup_files),
            backup_files,
        )
        for backup in backup_files:
            self.assertEqual(0o600, self._mode(backup), backup)
        for rule in legacy_document["rules"]:
            self.assertNotIn(rule["pattern"], output)

    def test_legacy_merge_rechecks_trie_budget_before_writing_generation(self) -> None:
        document = self._build_document(revision="fixture-trie-merge")
        self.build_path.write_text(
            json.dumps(document, ensure_ascii=False, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        self.build_path.chmod(0o600)
        active_patterns = {
            self.importer._normalize_term(pattern)
            for category in document["categories"]
            if category["enabled"]
            for entry in category["entries"]
            if entry["status"] == "active"
            for pattern in (entry["term"], *entry["aliases"])
        }
        base_node_count = 1 + len(
            {
                pattern[:length]
                for pattern in active_patterns
                for length in range(1, len(pattern) + 1)
            }
        )
        legacy_path = (
            self.instance_root / "data" / "content_alert" / "background_keywords.json"
        )
        legacy_path.parent.mkdir(parents=True, mode=0o700)
        legacy_term = "容量边界额外占位规则"
        legacy_path.write_text(
            json.dumps(
                {
                    "version": 1,
                    "revision": 1,
                    "updated_at": "2026-09-02T00:00:00Z",
                    "updated_by": "synthetic-fixture",
                    "next_rule_number": 2,
                    "rules": [{"id": "K0001", "pattern": legacy_term}],
                },
                ensure_ascii=False,
                sort_keys=True,
            )
            + "\n",
            encoding="utf-8",
        )
        legacy_path.chmod(0o600)

        with patch.object(
            self.importer,
            "MAX_MANAGED_TRIE_NODES",
            base_node_count,
        ):
            code, output = self._apply()

        self.assertNotEqual(0, code, output)
        self.assertFalse(self._current_path(self.instance_root).exists())
        self.assertFalse(
            (
                self.instance_root
                / "data"
                / "content_alert"
                / "managed"
                / "generations"
            ).exists()
        )
        self.assertNotIn(legacy_term, output)

    def test_legacy_merge_maps_maximum_length_identifier_safely(self) -> None:
        legacy_path = (
            self.instance_root / "data" / "content_alert" / "background_keywords.json"
        )
        legacy_path.parent.mkdir(parents=True, mode=0o700)
        legacy_term = "超长编号占位规则"
        legacy_path.write_text(
            json.dumps(
                {
                    "version": 1,
                    "revision": 1,
                    "updated_at": "2026-09-02T00:00:00Z",
                    "updated_by": "synthetic-fixture",
                    "next_rule_number": 2,
                    "rules": [{"id": "L" * 128, "pattern": legacy_term}],
                },
                ensure_ascii=False,
                sort_keys=True,
            )
            + "\n",
            encoding="utf-8",
        )
        legacy_path.chmod(0o600)

        code, output = self._apply()

        self.assertEqual(0, code, output)
        _, manifest_path, manifest = self._current_and_manifest()
        political = next(
            category
            for category in manifest["categories"]
            if category["id"] == "political_cn"
        )
        imported_ids: list[str] = []
        for descriptor in political["shards"]:
            shard = json.loads(
                (manifest_path.parent / descriptor["path"]).read_text(
                    encoding="utf-8"
                )
            )
            imported_ids.extend(
                entry["id"]
                for entry in shard["entries"]
                if entry["source_ref"].startswith("legacy-background-keywords")
            )
        self.assertEqual(1, len(imported_ids))
        self.assertLessEqual(len(imported_ids[0]), 128)
        self.assertRegex(imported_ids[0], r"\A[A-Za-z0-9][A-Za-z0-9_.:-]*\Z")
        self.assertNotIn(legacy_term, output)

    def test_rejects_relative_paths_traversal_and_symlinks_without_external_writes(
        self,
    ) -> None:
        relative_code, relative_output = self._invoke(
            "--instance-root",
            "relative-instance",
            "--build",
            str(self.build_path),
            "--apply",
        )
        self.assertNotEqual(0, relative_code, relative_output)

        exposed_build = self.base / "group-readable-build.json"
        exposed_build.write_bytes(self.build_path.read_bytes())
        exposed_build.chmod(0o640)
        exposed_root = self.base / "group-readable-build-instance"
        exposed_root.mkdir(mode=0o700)
        exposed_code, exposed_output = self._apply(
            root=exposed_root,
            build=exposed_build,
        )
        self.assertNotEqual(0, exposed_code, exposed_output)
        self.assertFalse(self._current_path(exposed_root).exists())

        invalid_id_path = self.base / "traversing-build.json"
        invalid = self._build_document(revision="fixture-traversal")
        invalid["categories"][1]["entries"][0]["id"] = "../outside"
        invalid_id_path.write_text(
            json.dumps(invalid, ensure_ascii=False, sort_keys=True),
            encoding="utf-8",
        )
        invalid_id_path.chmod(0o600)
        traversal_root = self.base / "traversal-instance"
        traversal_root.mkdir(mode=0o700)
        traversal_code, traversal_output = self._apply(
            root=traversal_root,
            build=invalid_id_path,
        )
        self.assertNotEqual(0, traversal_code, traversal_output)
        self.assertFalse(self._current_path(traversal_root).exists())

        linked_build = self.base / "linked-build.json"
        linked_build.symlink_to(self.build_path)
        linked_build_root = self.base / "linked-build-instance"
        linked_build_root.mkdir(mode=0o700)
        linked_code, linked_output = self._apply(
            root=linked_build_root,
            build=linked_build,
        )
        self.assertNotEqual(0, linked_code, linked_output)
        self.assertFalse(self._current_path(linked_build_root).exists())

        real_root = self.base / "real-instance"
        real_root.mkdir(mode=0o700)
        linked_root = self.base / "linked-instance"
        linked_root.symlink_to(real_root, target_is_directory=True)
        root_code, root_output = self._apply(root=linked_root)
        self.assertNotEqual(0, root_code, root_output)
        self.assertFalse(self._current_path(real_root).exists())

        managed_link_root = self.base / "managed-link-instance"
        content_alert = managed_link_root / "data" / "content_alert"
        content_alert.mkdir(parents=True, mode=0o700)
        outside = self.base / "outside-managed"
        outside.mkdir(mode=0o700)
        (content_alert / "managed").symlink_to(outside, target_is_directory=True)
        sentinel = outside / "sentinel"
        sentinel.write_text("unchanged", encoding="utf-8")
        managed_code, managed_output = self._apply(root=managed_link_root)
        self.assertNotEqual(0, managed_code, managed_output)
        self.assertEqual("unchanged", sentinel.read_text(encoding="utf-8"))
        self.assertEqual([sentinel], list(outside.iterdir()))

        pointer_link_root = self.base / "pointer-link-instance"
        managed = pointer_link_root / "data" / "content_alert" / "managed"
        managed.mkdir(parents=True, mode=0o700)
        outside_pointer = self.base / "outside-pointer.json"
        outside_pointer.write_text('{"unchanged":true}\n', encoding="utf-8")
        outside_before = outside_pointer.read_bytes()
        (managed / "current.json").symlink_to(outside_pointer)
        pointer_code, pointer_output = self._apply(root=pointer_link_root)
        self.assertNotEqual(0, pointer_code, pointer_output)
        self.assertEqual(outside_before, outside_pointer.read_bytes())


if __name__ == "__main__":
    unittest.main()
