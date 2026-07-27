from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

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


if __name__ == "__main__":
    unittest.main()
