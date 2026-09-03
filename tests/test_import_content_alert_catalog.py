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
    (
        "political_cn",
        "测试类别甲",
        "合成测试类别甲",
        "critical",
        "management_visible",
    ),
    ("sexual_explicit", "测试类别乙", "合成测试类别乙", "high", "management_visible"),
    ("gender_conflict", "测试类别丙", "合成测试类别丙", "medium", "management_visible"),
    (
        "controversial_topics",
        "测试类别丁",
        "合成测试类别丁",
        "medium",
        "management_visible",
    ),
    (
        "anime_game_controversy",
        "测试类别戊",
        "合成测试类别戊",
        "medium",
        "management_visible",
    ),
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
                    "enabled": category_id == "political_cn",
                    "version": 1,
                    "sources": [self._source(revision)],
                    "entries": [
                        {
                            "id": f"T{index:04d}",
                            "term": f"占位规则{index:02d}-{revision}",
                            "aliases": (
                                []
                                if category_id == "political_cn"
                                else [f"占位别名{index:02d}-{revision}"]
                            ),
                            "status": (
                                "active"
                                if category_id == "political_cn"
                                else "disabled"
                            ),
                        }
                        | (
                            {
                                "tags": [
                                    "human-reviewed-political-scope",
                                    "subject:leader_name",
                                ],
                                "last_reviewed": "2026-09-03",
                            }
                            if category_id == "political_cn"
                            else {"tags": ["human-reviewed-gender-antagonism"]}
                            if category_id == "gender_conflict"
                            else {}
                        )
                    ],
                }
            )
        return {
            "version": 1,
            "generated_at": "2026-09-02T00:00:00Z",
            "categories": categories,
        }

    def _build_v2_document(self, *, revision: str) -> dict[str, object]:
        document = self._build_document(revision=revision)
        document["version"] = 2
        political = next(
            category
            for category in document["categories"]
            if category["id"] == "political_cn"
        )
        source_ref = political["sources"][0]["source_id"]
        political["version"] = 2
        political["entries"] = [
            {
                "id": "P2001",
                "term": "合成领导甲",
                "aliases": [],
                "status": "active",
                "subject_type": "leader_name",
                "match_mode": "direct",
                "entity_ref": "leader.synthetic-a",
                "rank_level": "省部级正职",
                "rank_basis": "合成测试任职依据",
                "verification_status": "research_candidate",
                "confidence": 0.85,
                "tags": [
                    "human-reviewed-political-scope",
                    "subject:leader_name",
                ],
                "source_refs": [source_ref],
                "last_reviewed": "2026-09-03",
            },
            {
                "id": "P2002",
                "term": "合成历史事件",
                "aliases": [],
                "status": "active",
                "subject_type": "historical_event",
                "match_mode": "direct",
                "verification_status": "operator_curated",
                "confidence": 0.95,
                "tags": [
                    "human-reviewed-political-scope",
                    "subject:historical_event",
                ],
                "source_refs": [source_ref],
                "last_reviewed": "2026-09-03",
            },
        ]
        return document

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
        include_legacy_background: bool = False,
        allow_legacy_v1_build: bool = True,
    ) -> tuple[int, str]:
        arguments = [
            "--instance-root",
            str(root or self.instance_root),
            "--build",
            str(build or self.build_path),
            "--apply",
        ]
        if include_legacy_background:
            arguments.append("--include-legacy-background")
        if allow_legacy_v1_build:
            arguments.append("--allow-legacy-v1-build")
        return self._invoke(*arguments)

    def _rollback(self, *, root: Path | None = None) -> tuple[int, str]:
        return self._invoke(
            "--instance-root",
            str(root or self.instance_root),
            "--rollback",
        )

    def test_active_gender_conflict_entry_requires_human_review_tag(self) -> None:
        document = self._build_document(revision="fixture-unreviewed-gender")
        gender = next(
            category
            for category in document["categories"]
            if category["id"] == "gender_conflict"
        )
        gender["entries"][0]["status"] = "active"
        del gender["entries"][0]["tags"]
        self.build_path.write_text(
            json.dumps(document, ensure_ascii=False, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        self.build_path.chmod(0o600)

        code, output = self._apply()

        self.assertNotEqual(0, code, output)
        self.assertFalse(self._current_path(self.instance_root).exists())
        self.assertNotIn(str(gender["entries"][0]["term"]), output)

    def test_political_category_requires_management_visible_policy(self) -> None:
        document = self._build_document(revision="fixture-political-policy")

        _document_version, _generated_at, categories = self.importer._validate_build(
            document
        )

        political = next(
            category for category in categories if category["id"] == "political_cn"
        )
        self.assertEqual("management_visible", political["disclosure_policy"])

    def test_cli_exposes_explicit_legacy_background_opt_in(self) -> None:
        option_strings = {
            option
            for action in self.importer.build_parser()._actions
            for option in action.option_strings
        }

        self.assertIn("--include-legacy-background", option_strings)
        self.assertIn("--allow-legacy-v1-build", option_strings)

    def test_cli_rejects_new_v1_apply_without_explicit_legacy_opt_in(self) -> None:
        code, output = self._invoke(
            "--instance-root",
            str(self.instance_root),
            "--build",
            str(self.build_path),
            "--apply",
        )

        self.assertNotEqual(0, code, output)
        self.assertFalse(self._current_path(self.instance_root).exists())

    def test_cli_rejects_legacy_background_option_with_rollback(self) -> None:
        with patch.object(
            self.importer,
            "rollback_catalog",
            return_value="synthetic-should-not-run",
        ) as rollback:
            code, output = self._invoke(
                "--instance-root",
                str(self.instance_root),
                "--rollback",
                "--include-legacy-background",
            )

        self.assertNotEqual(0, code, output)
        rollback.assert_not_called()

    def test_active_political_entry_requires_review_single_subject_and_no_aliases(
        self,
    ) -> None:
        variants = {
            "missing-review": ["subject:leader_name"],
            "missing-subject": ["human-reviewed-political-scope"],
            "both-subjects": [
                "human-reviewed-political-scope",
                "subject:leader_name",
                "subject:historical_event",
            ],
            "allowed-plus-unknown-subject": [
                "human-reviewed-political-scope",
                "subject:leader_name",
                "subject:synthetic_unknown",
            ],
            "non-empty-aliases": [
                "human-reviewed-political-scope",
                "subject:leader_name",
            ],
        }

        for index, (variant, tags) in enumerate(variants.items(), start=1):
            with self.subTest(variant=variant):
                document = self._build_document(revision=f"fixture-{variant}")
                political = next(
                    category
                    for category in document["categories"]
                    if category["id"] == "political_cn"
                )
                entry = political["entries"][0]
                entry["tags"] = tags
                if variant == "non-empty-aliases":
                    entry["aliases"] = [f"政治占位别名-{index}"]

                with self.assertRaises(self.importer.CatalogImportError) as captured:
                    self.importer._validate_build(document)

                message = str(captured.exception)
                self.assertNotIn(entry["term"], message)
                for alias in entry["aliases"]:
                    self.assertNotIn(alias, message)

    def test_active_political_entry_accepts_historical_event_subject(self) -> None:
        document = self._build_document(revision="fixture-historical-subject")
        political = next(
            category
            for category in document["categories"]
            if category["id"] == "political_cn"
        )
        political["entries"][0]["tags"] = [
            "human-reviewed-political-scope",
            "subject:historical_event",
        ]

        self.importer._validate_build(document)

    def test_active_political_entry_requires_last_reviewed(self) -> None:
        document = self._build_document(revision="fixture-missing-last-reviewed")
        political = next(
            category
            for category in document["categories"]
            if category["id"] == "political_cn"
        )
        entry = political["entries"][0]
        del entry["last_reviewed"]

        with self.assertRaises(self.importer.CatalogImportError) as captured:
            self.importer._validate_build(document)

        self.assertNotIn(entry["term"], str(captured.exception))

    def test_active_political_entry_requires_pinned_referenced_sources(self) -> None:
        variants = ("missing-revision", "missing-sha256", "private-manual")

        for variant in variants:
            with self.subTest(variant=variant):
                document = self._build_document(revision=f"fixture-{variant}")
                political = next(
                    category
                    for category in document["categories"]
                    if category["id"] == "political_cn"
                )
                entry = political["entries"][0]
                source = political["sources"][0]
                if variant == "missing-revision":
                    del source["revision"]
                elif variant == "missing-sha256":
                    del source["sha256"]
                else:
                    source["license"] = "private-manual"
                    source["url"] = "instance-private:review-batch"
                    del source["revision"]
                    del source["sha256"]

                with self.assertRaises(self.importer.CatalogImportError) as captured:
                    self.importer._validate_build(document)

                self.assertNotIn(entry["term"], str(captured.exception))

    def test_v2_political_schema_is_preserved_in_v2_generation(self) -> None:
        document = self._build_v2_document(revision="fixture-v2-schema")

        document_version, generated_at, categories = self.importer._validate_build(
            document
        )
        prepared = self.importer._prepare_generation(
            document_version,
            generated_at,
            categories,
        )

        self.assertEqual(2, document_version)
        pointer = json.loads(prepared.pointer_bytes)
        self.assertEqual(2, pointer["version"])
        manifest = json.loads(prepared.files[Path("manifest.json")])
        self.assertEqual(2, manifest["version"])
        political = next(
            category
            for category in manifest["categories"]
            if category["id"] == "political_cn"
        )
        shard = json.loads(prepared.files[Path(political["shards"][0]["path"])])
        self.assertEqual(2, shard["version"])
        entries = {entry["id"]: entry for entry in shard["entries"]}
        self.assertEqual("leader_name", entries["P2001"]["subject_type"])
        self.assertEqual(
            "direct",
            entries["P2001"]["match_mode"],
        )
        self.assertEqual("leader.synthetic-a", entries["P2001"]["entity_ref"])
        self.assertEqual("省部级正职", entries["P2001"]["rank_level"])
        self.assertEqual("合成测试任职依据", entries["P2001"]["rank_basis"])
        self.assertEqual("direct", entries["P2002"]["match_mode"])
        self.assertEqual({"P2001", "P2002"}, set(entries))

    def test_v2_leader_requires_direct_mode_canonical_name_and_rank_metadata(
        self,
    ) -> None:
        variants: dict[str, tuple[str, object, str]] = {
            "missing-subject-type": ("subject_type", None, "subject type"),
            "wrong-match-mode": (
                "match_mode",
                "same_segment_context",
                "match mode",
            ),
            "noncanonical-name": ("term", "合成 领导甲", "canonical name"),
            "missing-entity-ref": ("entity_ref", None, "entity_ref"),
            "invalid-rank-level": ("rank_level", "厅局级正职", "rank_level"),
            "missing-rank-basis": ("rank_basis", None, "rank_basis"),
            "missing-verification-status": (
                "verification_status",
                None,
                "verification_status",
            ),
            "invalid-verification-status": (
                "verification_status",
                "official-ish",
                "verification_status",
            ),
            "missing-confidence": ("confidence", None, "confidence"),
            "invalid-confidence": ("confidence", 1.1, "confidence"),
            "missing-source-refs": ("source_refs", None, "source_refs"),
            "missing-last-reviewed": ("last_reviewed", None, "last_reviewed"),
        }

        for variant, (field, replacement, expected_error) in variants.items():
            with self.subTest(variant=variant):
                document = self._build_v2_document(revision=f"fixture-{variant}")
                political = next(
                    category
                    for category in document["categories"]
                    if category["id"] == "political_cn"
                )
                entry = political["entries"][0]
                if replacement is None:
                    entry.pop(field)
                else:
                    entry[field] = replacement

                with self.assertRaises(self.importer.CatalogImportError) as captured:
                    self.importer._validate_build(document)

                self.assertIn(expected_error, str(captured.exception))
                self.assertNotIn(entry["term"], str(captured.exception))

    def test_v2_canonical_leader_name_supports_han_extensions_and_middle_dot(
        self,
    ) -> None:
        for index, canonical_name in enumerate(("𠮷合成", "阿·合成"), start=1):
            with self.subTest(name_index=index):
                document = self._build_v2_document(
                    revision=f"fixture-canonical-name-{index}"
                )
                political = next(
                    category
                    for category in document["categories"]
                    if category["id"] == "political_cn"
                )
                political["entries"][0]["term"] = canonical_name

                self.importer._validate_build(document)

    def test_v2_direct_leader_without_context_is_a_live_catalog(self) -> None:
        document = self._build_v2_document(revision="fixture-direct-leader")
        political = next(
            category
            for category in document["categories"]
            if category["id"] == "political_cn"
        )
        political["entries"][0]["match_mode"] = "direct"
        political["entries"] = [
            entry
            for entry in political["entries"]
            if entry["subject_type"] != "political_context"
        ]

        document_version, generated_at, categories = self.importer._validate_build(
            document
        )
        prepared = self.importer._prepare_generation(
            document_version,
            generated_at,
            categories,
        )

        political_manifest = next(
            category
            for category in json.loads(prepared.files[Path("manifest.json")])[
                "categories"
            ]
            if category["id"] == "political_cn"
        )
        shard = json.loads(
            prepared.files[Path(political_manifest["shards"][0]["path"])]
        )
        leader = next(
            entry
            for entry in shard["entries"]
            if entry["subject_type"] == "leader_name"
        )
        self.assertEqual("direct", leader["match_mode"])

    def test_v2_historical_event_requires_direct_match_mode(self) -> None:
        document = self._build_v2_document(revision="fixture-event-mode")
        political = next(
            category
            for category in document["categories"]
            if category["id"] == "political_cn"
        )
        event = political["entries"][1]
        event["match_mode"] = "same_segment_context"

        with self.assertRaises(self.importer.CatalogImportError) as captured:
            self.importer._validate_build(document)

        self.assertIn("match mode", str(captured.exception))
        self.assertNotIn(event["term"], str(captured.exception))

    def test_v2_new_build_rejects_support_only_context_entries(self) -> None:
        document = self._build_v2_document(revision="fixture-no-support-only")
        political = next(
            category
            for category in document["categories"]
            if category["id"] == "political_cn"
        )
        source_ref = political["sources"][0]["source_id"]
        context = {
            "id": "P2003",
            "term": "合成辅助语境",
            "aliases": [],
            "status": "active",
            "subject_type": "political_context",
            "match_mode": "support_only",
            "context_class": "case_proceeding",
            "strength": "strong",
            "verification_status": "operator_curated",
            "confidence": 0.95,
            "tags": [
                "human-reviewed-political-scope",
                "subject:political_context",
            ],
            "source_refs": [source_ref],
            "last_reviewed": "2026-09-03",
        }
        political["entries"].append(context)

        with self.assertRaises(self.importer.CatalogImportError) as captured:
            self.importer._validate_build(document)

        self.assertIn("support-only", str(captured.exception))
        self.assertNotIn(context["term"], str(captured.exception))

    def test_same_name_leaders_must_be_preclustered_as_one_match_subject(
        self,
    ) -> None:
        document = self._build_v2_document(revision="fixture-same-name-group")
        political = next(
            category
            for category in document["categories"]
            if category["id"] == "political_cn"
        )
        leader = political["entries"][0]
        duplicate = json.loads(json.dumps(leader))
        duplicate["id"] = "T0099"
        duplicate["entity_ref"] = "leader.synthetic-same-name-group-b"
        political["entries"].append(duplicate)

        with self.assertRaises(self.importer.CatalogImportError) as captured:
            self.importer._validate_build(document)

        self.assertIn("canonical name is duplicated", str(captured.exception))
        self.assertNotIn(leader["term"], str(captured.exception))

    def test_v2_enabled_category_cannot_silence_a_referenced_leader(self) -> None:
        document = self._build_v2_document(revision="fixture-inactive-leader-reference")
        political = next(
            category
            for category in document["categories"]
            if category["id"] == "political_cn"
        )
        leader = political["entries"][0]
        leader["status"] = "shadow"

        with self.assertRaises(self.importer.CatalogImportError) as captured:
            self.importer._validate_build(document)

        self.assertIn("enabled v2 category", str(captured.exception))
        self.assertNotIn(leader["term"], str(captured.exception))

        self.build_path.write_text(
            json.dumps(document, ensure_ascii=False, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        self.build_path.chmod(0o600)
        code, output = self._apply(allow_legacy_v1_build=False)

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

    def test_v2_subject_type_must_match_exactly_one_subject_tag(self) -> None:
        document = self._build_v2_document(revision="fixture-subject-consistency")
        political = next(
            category
            for category in document["categories"]
            if category["id"] == "political_cn"
        )
        leader = political["entries"][0]
        leader["tags"] = [
            "human-reviewed-political-scope",
            "subject:historical_event",
        ]

        with self.assertRaises(self.importer.CatalogImportError) as captured:
            self.importer._validate_build(document)

        self.assertIn("subject tag", str(captured.exception))
        self.assertNotIn(leader["term"], str(captured.exception))

    def test_v2_research_candidate_accepts_source_screened_scope_tag(self) -> None:
        document = self._build_v2_document(revision="fixture-source-screened")
        political = next(
            category
            for category in document["categories"]
            if category["id"] == "political_cn"
        )
        leader = political["entries"][0]
        leader["tags"] = [
            "source-screened-political-scope",
            "subject:leader_name",
        ]

        self.importer._validate_build(document)

    def test_v2_source_screened_candidate_applies_and_cold_loads(self) -> None:
        from plugins.content_alert.catalog import ManagedKeywordCatalog

        document = self._build_v2_document(revision="fixture-source-screened-cold-load")
        political = next(
            category
            for category in document["categories"]
            if category["id"] == "political_cn"
        )
        leader = political["entries"][0]
        leader["tags"] = [
            "source-screened-political-scope",
            "subject:leader_name",
        ]
        self.build_path.write_text(
            json.dumps(document, ensure_ascii=False, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        self.build_path.chmod(0o600)

        code, output = self._apply(allow_legacy_v1_build=False)

        self.assertEqual(0, code, output)
        cold_catalog = ManagedKeywordCatalog(self._current_path(self.instance_root))
        snapshot = cold_catalog.snapshot()
        self.assertTrue(snapshot.has_active_generation)
        self.assertEqual(2, len(snapshot.entries))
        self.assertIsNone(cold_catalog.last_error)

    def test_v2_operator_curated_entry_rejects_source_screened_only_tag(self) -> None:
        document = self._build_v2_document(revision="fixture-source-screened-operator")
        political = next(
            category
            for category in document["categories"]
            if category["id"] == "political_cn"
        )
        event = political["entries"][1]
        event["tags"] = [
            "source-screened-political-scope",
            "subject:historical_event",
        ]

        with self.assertRaises(self.importer.CatalogImportError) as captured:
            self.importer._validate_build(document)

        self.assertIn("review", str(captured.exception))
        self.assertNotIn(event["term"], str(captured.exception))

    def test_v2_enabled_category_rejects_nonalerting_entry_statuses(self) -> None:
        for status in ("shadow", "disabled"):
            with self.subTest(status=status):
                document = self._build_v2_document(revision=f"fixture-enabled-{status}")
                political = next(
                    category
                    for category in document["categories"]
                    if category["id"] == "political_cn"
                )
                entry = political["entries"][0]
                entry["status"] = status

                with self.assertRaises(self.importer.CatalogImportError) as captured:
                    self.importer._validate_build(document)

                self.assertIn("enabled v2 category", str(captured.exception))
                self.assertNotIn(entry["term"], str(captured.exception))

    def test_v2_rejects_legacy_shadow_merge_before_writing(self) -> None:
        document = self._build_v2_document(revision="fixture-v2-no-shadow-merge")
        self.build_path.write_text(
            json.dumps(document, ensure_ascii=False, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        self.build_path.chmod(0o600)
        self._write_legacy_rules([{"id": "K0001", "pattern": "合成旧候选规则甲"}])

        with self.assertRaisesRegex(
            self.importer.CatalogImportError,
            "v2 catalog does not support shadow entries",
        ):
            self.importer.import_catalog(
                instance_root=self.instance_root,
                build_path=self.build_path,
                include_legacy_background=True,
            )

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

    def test_active_v2_political_terms_must_be_canonical_visible_text(self) -> None:
        variants = {
            "zero-width": (1, "合成\u200b历史事件"),
            "variation-selector": (0, "合成\ufe0f领导姓名"),
            "whitespace": (0, "合成 领导姓名"),
            "non-nfkc": (1, "合成历史事件Ａ"),
        }

        for variant, (entry_index, term) in variants.items():
            with self.subTest(variant=variant):
                document = self._build_v2_document(
                    revision=f"fixture-canonical-term-{variant}"
                )
                political = next(
                    category
                    for category in document["categories"]
                    if category["id"] == "political_cn"
                )
                political["entries"][entry_index]["term"] = term

                with self.assertRaises(self.importer.CatalogImportError) as captured:
                    self.importer._validate_build(document)

                self.assertIn("canonical", str(captured.exception))
                self.assertNotIn(term, str(captured.exception))

    def test_active_v2_political_terms_reject_edge_whitespace_without_trimming(
        self,
    ) -> None:
        for entry_index in range(2):
            for edge in ("leading", "trailing"):
                with self.subTest(entry_index=entry_index, edge=edge):
                    document = self._build_v2_document(
                        revision=f"fixture-edge-space-{entry_index}-{edge}"
                    )
                    political = next(
                        category
                        for category in document["categories"]
                        if category["id"] == "political_cn"
                    )
                    entry = political["entries"][entry_index]
                    original = entry["term"]
                    entry["term"] = (
                        f" {original}" if edge == "leading" else f"{original} "
                    )

                    with self.assertRaises(
                        self.importer.CatalogImportError
                    ) as captured:
                        self.importer._validate_build(document)

                    self.assertIn("canonical", str(captured.exception))
                    self.assertNotIn(str(original), str(captured.exception))

    def test_v2_political_generation_requires_live_alerting_semantics(self) -> None:
        variants = {
            "political-disabled": (
                lambda political: political.update({"enabled": False}),
                "alerting semantics",
            ),
            "all-disabled": (
                lambda political: [
                    entry.update({"status": "disabled"})
                    for entry in political["entries"]
                ],
                "enabled v2 category",
            ),
        }

        for variant, (mutate, expected_error) in variants.items():
            with self.subTest(variant=variant):
                document = self._build_v2_document(
                    revision=f"fixture-live-semantics-{variant}"
                )
                political = next(
                    category
                    for category in document["categories"]
                    if category["id"] == "political_cn"
                )
                mutate(political)

                with self.assertRaises(self.importer.CatalogImportError) as captured:
                    self.importer._validate_build(document)

                self.assertIn(expected_error, str(captured.exception))

    def test_v1_build_remains_valid_and_prepares_v1_generation(self) -> None:
        document = self._build_document(revision="fixture-v1-compatibility")

        document_version, generated_at, categories = self.importer._validate_build(
            document
        )
        prepared = self.importer._prepare_generation(
            document_version,
            generated_at,
            categories,
        )

        self.assertEqual(1, document_version)
        self.assertEqual(1, json.loads(prepared.pointer_bytes)["version"])
        self.assertEqual(
            1,
            json.loads(prepared.files[Path("manifest.json")])["version"],
        )

    def test_private_build_version_must_be_a_supported_integer(self) -> None:
        for invalid_version in (True, 2.0, [], 3):
            with self.subTest(version=invalid_version):
                document = self._build_document(revision="fixture-invalid-version")
                document["version"] = invalid_version

                with self.assertRaises(self.importer.CatalogImportError):
                    self.importer._validate_build(document)

    def test_deep_json_nesting_is_reported_as_catalog_error(self) -> None:
        payload = b'{"nested":' + b"[" * 2_000 + b"0" + b"]" * 2_000 + b"}"

        with self.assertRaises(self.importer.CatalogImportError) as captured:
            self.importer._decode_json(payload, label="private build")

        self.assertIn("private build", str(captured.exception))
        self.assertNotIn("nested", str(captured.exception))

    def test_generation_version_must_be_a_supported_integer(self) -> None:
        _version, generated_at, categories = self.importer._validate_build(
            self._build_document(revision="fixture-generation-version")
        )

        for invalid_version in (True, 2.0, [], 3):
            with (
                self.subTest(version=invalid_version),
                self.assertRaises(self.importer.CatalogImportError),
            ):
                self.importer._prepare_generation(
                    invalid_version,
                    generated_at,
                    categories,
                )

    def test_pointer_version_must_be_a_supported_integer(self) -> None:
        for invalid_version in (True, 2.0, [], 3):
            with self.subTest(version=invalid_version):
                pointer = {
                    "version": invalid_version,
                    "generation_id": "generation-synthetic",
                    "manifest": "generations/generation-synthetic/manifest.json",
                }
                with self.assertRaises(self.importer.CatalogImportError):
                    self.importer._decode_pointer(json.dumps(pointer).encode("utf-8"))

    def test_all_referenced_political_sources_require_pinned_provenance(self) -> None:
        document = self._build_document(revision="fixture-multiple-source-refs")
        political = next(
            category
            for category in document["categories"]
            if category["id"] == "political_cn"
        )
        primary = political["sources"][0]
        secondary = dict(primary, source_id="secondary-source")
        secondary.pop("revision")
        secondary.pop("sha256")
        political["sources"].append(secondary)
        entry = political["entries"][0]
        entry["source_refs"] = [primary["source_id"], secondary["source_id"]]

        with self.assertRaises(self.importer.CatalogImportError) as captured:
            self.importer._validate_build(document)

        self.assertNotIn(entry["term"], str(captured.exception))

    def test_v2_primary_source_must_be_present_in_complete_source_refs(self) -> None:
        document = self._build_v2_document(revision="fixture-source-ref-mismatch")
        political = next(
            category
            for category in document["categories"]
            if category["id"] == "political_cn"
        )
        primary = political["sources"][0]
        secondary = dict(primary, source_id="secondary-source")
        political["sources"].append(secondary)
        entry = political["entries"][0]
        entry["source_ref"] = secondary["source_id"]
        entry["source_refs"] = [primary["source_id"]]

        with self.assertRaises(self.importer.CatalogImportError) as captured:
            self.importer._validate_build(document)

        self.assertNotIn(entry["term"], str(captured.exception))

    def test_enabled_nonpolitical_category_cannot_have_active_entries(self) -> None:
        document = self._build_document(revision="fixture-nonpolitical-active")
        category = next(
            category
            for category in document["categories"]
            if category["id"] == "sexual_explicit"
        )
        category["enabled"] = True
        category["entries"][0]["status"] = "active"

        with self.assertRaises(self.importer.CatalogImportError) as captured:
            self.importer._validate_build(document)

        self.assertNotIn(category["entries"][0]["term"], str(captured.exception))

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

    def _write_legacy_rules(
        self,
        rules: list[dict[str, str]],
    ) -> tuple[Path, bytes]:
        legacy_path = (
            self.instance_root / "data" / "content_alert" / "background_keywords.json"
        )
        legacy_path.parent.mkdir(parents=True, mode=0o700)
        legacy_document = {
            "version": 1,
            "revision": 1,
            "updated_at": "2026-09-02T00:00:00Z",
            "updated_by": "synthetic-fixture",
            "next_rule_number": len(rules) + 1,
            "rules": rules,
        }
        legacy_path.write_text(
            json.dumps(legacy_document, ensure_ascii=False, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        legacy_path.chmod(0o600)
        return legacy_path, legacy_path.read_bytes()

    def _managed_category_entries(self, category_id: str) -> list[dict[str, object]]:
        _, manifest_path, manifest = self._current_and_manifest()
        category = next(
            category
            for category in manifest["categories"]
            if category["id"] == category_id
        )
        entries: list[dict[str, object]] = []
        for descriptor in category["shards"]:
            shard = json.loads(
                (manifest_path.parent / descriptor["path"]).read_text(encoding="utf-8")
            )
            entries.extend(shard["entries"])
        return entries

    def _install_current_generation(self, prepared: object) -> Path:
        managed_root = self.instance_root / "data" / "content_alert" / "managed"
        generations_root = managed_root / "generations"
        current = self._current_path(self.instance_root)
        for directory in (
            self.instance_root / "data",
            self.instance_root / "data" / "content_alert",
            managed_root,
            generations_root,
        ):
            directory.mkdir(mode=0o700, exist_ok=True)
            directory.chmod(0o700)
        self.importer._write_generation(generations_root, prepared)
        self.importer._atomic_write_private(current, prepared.pointer_bytes)
        return current

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
        duplicate_id["categories"][1]["entries"][0]["id"] = duplicate_id["categories"][
            0
        ]["entries"][0]["id"]
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
            political = second_unique["categories"][0]
            political["entries"].append(
                dict(
                    political["entries"][0],
                    id="T0008",
                    term="第二个唯一占位词",
                )
            )
            with self.assertRaises(self.importer.CatalogImportError):
                self.importer._validate_build(second_unique)

            all_shadow = self._build_document(revision="fixture-shadow-limit")
            for category in all_shadow["categories"]:
                category["entries"][0]["status"] = "shadow"
                category["entries"][0]["aliases"] = [f"影子别名-{category['id']}"]
            self.importer._validate_build(all_shadow)

        with (
            patch.object(self.importer, "MAX_MANAGED_TRIE_NODES", 3),
            self.assertRaisesRegex(
                self.importer.CatalogImportError,
                "trie node limit",
            ),
        ):
            self.importer._validate_build(duplicate_active)

    def test_runtime_file_size_limits_are_checked_before_generation_write(self) -> None:
        document_version, generated_at, categories = self.importer._validate_build(
            self._build_document(revision="fixture-v1")
        )
        prepared = self.importer._prepare_generation(
            document_version,
            generated_at,
            categories,
        )
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
                        root / "data" / "content_alert" / "managed" / "generations"
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
        first_shard = (
            manifest_path.parent / manifest["categories"][0]["shards"][0]["path"]
        )
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
                for alias in entry["aliases"]:
                    self.assertNotIn(alias, conflict_output)

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
        invalid["categories"][0]["disclosure_policy"] = "strict_hidden"
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

    def test_visible_upgrade_and_rollback_accept_legacy_hidden_generation(self) -> None:
        document_version, generated_at, old_categories = self.importer._validate_build(
            self._build_document(revision="fixture-legacy-hidden")
        )
        old_categories = json.loads(json.dumps(old_categories))
        old_political = next(
            category for category in old_categories if category["id"] == "political_cn"
        )
        old_political["disclosure_policy"] = "strict_hidden"
        old_prepared = self.importer._prepare_generation(
            document_version,
            generated_at,
            old_categories,
        )
        current_path = self._install_current_generation(old_prepared)
        old_pointer = current_path.read_bytes()
        self._write_build(self.build_path, revision="fixture-visible-upgrade")

        apply_code, apply_output = self._apply()

        self.assertEqual(0, apply_code, apply_output)
        self.assertNotEqual(old_pointer, current_path.read_bytes())

        rollback_code, rollback_output = self._rollback()

        self.assertEqual(0, rollback_code, rollback_output)
        self.assertEqual(old_pointer, current_path.read_bytes())

    def test_v2_upgrade_and_rollback_restore_existing_v1_generation(self) -> None:
        document_version, generated_at, old_categories = self.importer._validate_build(
            self._build_document(revision="fixture-v1-before-v2")
        )
        old_prepared = self.importer._prepare_generation(
            document_version,
            generated_at,
            old_categories,
        )
        current_path = self._install_current_generation(old_prepared)
        old_pointer = current_path.read_bytes()
        self.build_path.write_text(
            json.dumps(
                self._build_v2_document(revision="fixture-v2-upgrade"),
                ensure_ascii=False,
                sort_keys=True,
            )
            + "\n",
            encoding="utf-8",
        )
        self.build_path.chmod(0o600)

        apply_code, apply_output = self._apply()

        self.assertEqual(0, apply_code, apply_output)
        self.assertEqual(
            2,
            json.loads(current_path.read_text(encoding="utf-8"))["version"],
        )

        rollback_code, rollback_output = self._rollback()

        self.assertEqual(0, rollback_code, rollback_output)
        self.assertEqual(old_pointer, current_path.read_bytes())

    def test_legacy_hidden_policy_exception_does_not_apply_to_v2(self) -> None:
        document_version, generated_at, categories = self.importer._validate_build(
            self._build_v2_document(revision="fixture-v2-hidden-policy")
        )
        political = next(
            category for category in categories if category["id"] == "political_cn"
        )
        political["disclosure_policy"] = "strict_hidden"
        prepared = self.importer._prepare_generation(
            document_version,
            generated_at,
            categories,
        )
        current_path = self._install_current_generation(prepared)

        with self.assertRaises(self.importer.CatalogImportError):
            self.importer._verify_pointer_catalog(
                current_path.parent,
                current_path.read_bytes(),
                allow_legacy_political_hidden=True,
            )

    def test_rollback_rejects_unreviewed_active_gender_generation(self) -> None:
        first_document = self._build_document(revision="fixture-v1")
        first_gender = next(
            category
            for category in first_document["categories"]
            if category["id"] == "gender_conflict"
        )
        first_gender["entries"][0]["status"] = "active"
        self.build_path.write_text(
            json.dumps(first_document, ensure_ascii=False, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        self.build_path.chmod(0o600)
        first_code, first_output = self._apply()
        self.assertEqual(0, first_code, first_output)
        first_pointer, first_manifest_path, first_manifest = (
            self._current_and_manifest()
        )

        second_path = self.base / "reviewed-second-build.json"
        self._write_build(second_path, revision="fixture-reviewed-v2")
        second_code, second_output = self._apply(build=second_path)
        self.assertEqual(0, second_code, second_output)
        current_path = self._current_path(self.instance_root)
        second_pointer_bytes = current_path.read_bytes()

        gender = next(
            category
            for category in first_manifest["categories"]
            if category["id"] == "gender_conflict"
        )
        descriptor = gender["shards"][0]
        shard_path = first_manifest_path.parent / descriptor["path"]
        shard = json.loads(shard_path.read_text(encoding="utf-8"))
        active = next(
            entry
            for entry in shard["entries"]
            if entry.get("status", "active") == "active"
        )
        active.pop("tags", None)
        shard_bytes = self.importer._json_bytes(shard)
        shard_path.write_bytes(shard_bytes)
        shard_path.chmod(0o600)
        descriptor["sha256"] = hashlib.sha256(shard_bytes).hexdigest()
        first_manifest_path.write_bytes(self.importer._json_bytes(first_manifest))
        first_manifest_path.chmod(0o600)

        rollback_code, rollback_output = self._rollback()

        self.assertNotEqual(0, rollback_code, rollback_output)
        self.assertEqual(second_pointer_bytes, current_path.read_bytes())
        self.assertNotEqual(first_pointer, json.loads(current_path.read_text()))
        self.assertNotIn(active["term"], rollback_output)

    def test_rollback_rejects_v2_generation_with_invalid_runtime_provenance(
        self,
    ) -> None:
        self.build_path.write_text(
            json.dumps(
                self._build_v2_document(revision="fixture-v2-first"),
                ensure_ascii=False,
                sort_keys=True,
            )
            + "\n",
            encoding="utf-8",
        )
        self.build_path.chmod(0o600)
        first_code, first_output = self._apply()
        self.assertEqual(0, first_code, first_output)
        _first_pointer, first_manifest_path, first_manifest = (
            self._current_and_manifest()
        )

        second_path = self.base / "v2-second-build.json"
        second_path.write_text(
            json.dumps(
                self._build_v2_document(revision="fixture-v2-second"),
                ensure_ascii=False,
                sort_keys=True,
            )
            + "\n",
            encoding="utf-8",
        )
        second_path.chmod(0o600)
        second_code, second_output = self._apply(build=second_path)
        self.assertEqual(0, second_code, second_output)
        current_path = self._current_path(self.instance_root)
        second_pointer_bytes = current_path.read_bytes()

        political = next(
            category
            for category in first_manifest["categories"]
            if category["id"] == "political_cn"
        )
        descriptor = political["shards"][0]
        shard_path = first_manifest_path.parent / descriptor["path"]
        shard = json.loads(shard_path.read_text(encoding="utf-8"))
        active = next(
            entry for entry in shard["entries"] if entry["status"] == "active"
        )
        active["source_refs"] = []
        shard_bytes = self.importer._json_bytes(shard)
        shard_path.write_bytes(shard_bytes)
        shard_path.chmod(0o600)
        descriptor["sha256"] = hashlib.sha256(shard_bytes).hexdigest()
        first_manifest_path.write_bytes(self.importer._json_bytes(first_manifest))
        first_manifest_path.chmod(0o600)

        rollback_code, rollback_output = self._rollback()

        self.assertNotEqual(0, rollback_code, rollback_output)
        self.assertEqual(second_pointer_bytes, current_path.read_bytes())
        self.assertNotIn(active["term"], rollback_output)

    def test_legacy_rules_are_not_merged_by_default_but_are_backed_up(self) -> None:
        legacy_rules = [
            {"id": "K0001", "pattern": "默认不纳入占位规则甲"},
            {"id": "K0002", "pattern": "默认不纳入占位规则乙"},
        ]
        legacy_path, legacy_before = self._write_legacy_rules(legacy_rules)

        code, output = self._apply()

        self.assertEqual(0, code, output)
        self.assertEqual(legacy_before, legacy_path.read_bytes())
        terms = {
            entry["term"] for entry in self._managed_category_entries("political_cn")
        }
        self.assertTrue({rule["pattern"] for rule in legacy_rules}.isdisjoint(terms))
        backup_root = self.instance_root / "backups" / "content-alert"
        backup_files = [path for path in backup_root.rglob("*") if path.is_file()]
        self.assertTrue(
            any(path.read_bytes() == legacy_before for path in backup_files),
            backup_files,
        )
        for rule in legacy_rules:
            self.assertNotIn(rule["pattern"], output)

    def test_explicit_legacy_import_adds_shadow_entries_and_preserves_backup(
        self,
    ) -> None:
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

        code, output = self._apply(include_legacy_background=True)

        self.assertEqual(0, code, output)
        self.assertEqual(legacy_before, legacy_path.read_bytes())
        self.assertEqual(legacy_mode, self._mode(legacy_path))
        _, manifest_path, manifest = self._current_and_manifest()
        political = next(
            category
            for category in manifest["categories"]
            if category["id"] == "political_cn"
        )
        self.assertEqual("management_visible", political["disclosure_policy"])
        entries: list[dict[str, object]] = []
        for descriptor in political["shards"]:
            shard = json.loads(
                (manifest_path.parent / descriptor["path"]).read_text(encoding="utf-8")
            )
            entries.extend(shard["entries"])
        imported = [
            entry
            for entry in entries
            if entry["source_ref"].startswith("legacy-background-keywords")
        ]
        terms = {entry["term"] for entry in imported}
        self.assertTrue(
            {f"历史占位规则{index:02d}" for index in range(1, 61)}.issubset(terms)
        )
        self.assertEqual(60, len(imported))
        self.assertTrue(all(entry["status"] == "shadow" for entry in imported))
        self.assertTrue(all(not entry.get("tags") for entry in imported))

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

    def test_legacy_merge_rechecks_storage_budget_before_writing_generation(
        self,
    ) -> None:
        document = self._build_document(revision="fixture-trie-merge")
        self.build_path.write_text(
            json.dumps(document, ensure_ascii=False, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        self.build_path.chmod(0o600)
        base_stored_pattern_count = sum(
            1 + len(entry["aliases"])
            for category in document["categories"]
            for entry in category["entries"]
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
            "MAX_STORED_PATTERNS",
            base_stored_pattern_count,
        ):
            code, output = self._apply(include_legacy_background=True)

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

        code, output = self._apply(include_legacy_background=True)

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
                (manifest_path.parent / descriptor["path"]).read_text(encoding="utf-8")
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
