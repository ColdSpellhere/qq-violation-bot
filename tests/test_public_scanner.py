from __future__ import annotations

import tempfile
import subprocess
import unittest
from pathlib import Path
from unittest.mock import patch

from scripts import check_public_tree as scanner
from scripts.check_public_tree import generic_findings, runtime_findings


class PublicScannerTests(unittest.TestCase):
    def test_runtime_artifact_paths_are_rejected_in_current_and_history_scans(self) -> None:
        path_findings = getattr(scanner, "path_findings", lambda path: [])
        cases = {
            "data/chat_archive.db": "SQLite database",
            "data/runtime_features.json": "runtime feature state",
            "exports/private.csv": "export artifact",
            "data/chat_vision/images/private.jpg": "image artifact",
        }
        for path, finding_class in cases.items():
            with self.subTest(path=path):
                self.assertEqual(
                    [f"{path}: {finding_class}"], path_findings(path)
                )
        self.assertEqual([], path_findings("docs/architecture.md"))
        self.assertEqual([], path_findings("tests/test_image_contract.py"))

    def test_generic_token_and_private_key_are_detected(self) -> None:
        glm_key = ("a" * 32) + "." + ("B" * 16)
        tavily_key = "tvly-" + "dev-" + ("C" * 24)
        text = (
            "API_KEY="
            + "sk-"
            + ("a" * 26)
            + "\nGLM_API_KEY="
            + glm_key
            + "\nTAVILY_API_KEY="
            + tavily_key
            + "\nBEGIN "
            + "OPENSSH PRIVATE KEY"
        )
        findings = generic_findings("fixture.txt", text)
        self.assertEqual(
            [
                "fixture.txt: generic API token",
                "fixture.txt: GLM API token",
                "fixture.txt: Tavily API token",
                "fixture.txt: private key material",
            ],
            findings,
        )

    def test_runtime_value_is_reported_by_key_without_printing_value(self) -> None:
        findings = runtime_findings(
            "fixture.py",
            "group = 123456789",
            {"TARGET_GROUP_ID": "123456789"},
        )
        self.assertEqual(["fixture.py: runtime value for TARGET_GROUP_ID"], findings)

    def test_empty_runtime_values_are_ignored(self) -> None:
        self.assertEqual([], runtime_findings("fixture.py", "", {"AI_API_KEY": ""}))

    def test_historical_baseline_is_exact_audited_and_never_applies_to_current_tree(self) -> None:
        entries = getattr(scanner, "HISTORICAL_BASELINE", ())
        self.assertEqual(1, len(entries))
        entry = entries[0]
        self.assertEqual("tests/test_private_memory_processing.py", entry.path)
        self.assertEqual("39603b010b2c564517180ff7d15577df291707ff", entry.blob_oid)
        self.assertEqual("generic API token", entry.finding_class)
        self.assertTrue(entry.reason)
        self.assertRegex(entry.reviewed_on, r"^\d{4}-\d{2}-\d{2}$")

        finding = "tests/test_private_memory_processing.py: generic API token"
        filter_findings = getattr(
            scanner,
            "filter_historical_findings",
            lambda path, blob_oid, findings: list(findings),
        )
        self.assertEqual(
            [],
            filter_findings(entry.path, entry.blob_oid, [finding]),
        )
        self.assertEqual(
            [finding],
            filter_findings(entry.path, "0" * 40, [finding]),
        )
        self.assertEqual(
            [finding],
            scanner.scan_text(
                entry.path,
                "sk-" + ("z" * 26),
                {},
                blob_oid=entry.blob_oid,
                historical=False,
            ),
        )

    def test_binary_current_rename_delete_and_all_history_are_scanned(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)

            def git(*args: str) -> subprocess.CompletedProcess[str]:
                return subprocess.run(
                    ["git", *args], cwd=root, check=True, capture_output=True, text=True
                )

            git("init", "-q")
            git("config", "user.name", "Scanner Test")
            git("config", "user.email", "scanner@example.invalid")
            token = b"sk-" + (b"a" * 26)
            tavily_token = b"tvly-" + b"prod-" + (b"b" * 24)
            private_key = b"BEGIN " + b"OPENSSH PRIVATE KEY"
            (root / "binary.dat").write_bytes(
                b"prefix\x00API_KEY="
                + token
                + b"\x00"
                + tavily_token
                + b"\x00"
                + private_key
                + b"\x00"
            )
            git("add", "binary.dat")
            git("commit", "-qm", "add binary fixture")

            with patch.object(scanner, "ROOT", root):
                self.assertEqual(
                    [
                        "binary.dat: generic API token",
                        "binary.dat: Tavily API token",
                        "binary.dat: private key material",
                    ],
                    scanner.scan_ref(None, {}),
                )

                git("mv", "binary.dat", "renamed.bin")
                git("commit", "-qm", "rename binary fixture")
                git("rm", "-q", "renamed.bin")
                git("commit", "-qm", "delete binary fixture")
                self.assertEqual([], scanner.scan_ref(None, {}))
                revisions = tuple(scanner.revisions())
                self.assertEqual(3, len(revisions))
                seen: set[tuple[str, str]] = set()
                history = [
                    finding
                    for revision in revisions
                    for finding in scanner.scan_ref(
                        revision, {}, seen_history=seen
                    )
                ]

            self.assertTrue(any("binary.dat: generic API token" in item for item in history))
            self.assertTrue(any("renamed.bin: generic API token" in item for item in history))
            self.assertTrue(any("Tavily API token" in item for item in history))
            self.assertTrue(any("private key material" in item for item in history))

    def test_oversized_binary_fails_closed_without_decoding(self) -> None:
        scan_bytes = getattr(scanner, "scan_bytes", lambda path, raw, values: [])
        with patch.object(scanner, "MAX_SCAN_BYTES", 4, create=True):
            self.assertEqual(
                ["large.bin: file exceeds scan size limit"],
                scan_bytes("large.bin", b"12345", {}),
            )


if __name__ == "__main__":
    unittest.main()
