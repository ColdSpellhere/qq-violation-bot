from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from scripts import check_public_tree as scanner
from scripts.check_public_tree import generic_findings, runtime_findings


class PublicScannerTests(unittest.TestCase):
    def test_generic_token_and_private_key_are_detected(self) -> None:
        text = "API_KEY=" + "sk-" + ("a" * 26) + "\nBEGIN " + "OPENSSH PRIVATE KEY"
        findings = generic_findings("fixture.txt", text)
        self.assertEqual(
            ["fixture.txt: generic API token", "fixture.txt: private key material"],
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


if __name__ == "__main__":
    unittest.main()
