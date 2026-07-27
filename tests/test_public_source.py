from __future__ import annotations

import re
import unittest
from pathlib import Path

from dotenv import dotenv_values


ROOT = Path(__file__).resolve().parents[1]
PUBLIC_FILES = (
    ROOT / ".env.example",
    ROOT / "README.md",
    ROOT / "plugins/violation_record/config.py",
    ROOT / "scripts/start_napcat.sh",
)
SENSITIVE_KEYS = (
    "TARGET_GROUP_ID",
    "BOT_SELF_ID",
    "NAPCAT_ACCESS_TOKEN",
    "AI_API_KEY",
    "ADMIN_SEED",
)


def _public_text() -> str:
    return "\n".join(path.read_text(encoding="utf-8") for path in PUBLIC_FILES)


class PublicSourceBoundaryTests(unittest.TestCase):
    def test_live_runtime_values_are_absent_from_public_files(self) -> None:
        env_path = ROOT / ".env"
        if not env_path.exists():
            self.skipTest("production .env is not present")
        public_text = _public_text()
        values = dotenv_values(env_path)
        leaked = [
            key
            for key in SENSITIVE_KEYS
            if str(values.get(key) or "").strip()
            and str(values[key]).strip() in public_text
        ]
        self.assertEqual([], leaked, f"runtime values leaked for keys: {leaked}")

    def test_napcat_launcher_has_no_literal_bot_qq(self) -> None:
        text = (ROOT / "scripts/start_napcat.sh").read_text(encoding="utf-8")
        self.assertIsNone(re.search(r"(?:^|\s)-q\s+\d{5,12}(?:\s|$)", text))

    def test_config_has_no_numeric_target_group_fallback(self) -> None:
        text = (ROOT / "plugins/violation_record/config.py").read_text(
            encoding="utf-8"
        )
        self.assertIsNone(re.search(r"values\s*=\s*\[\d{5,12}\]", text))

    def test_public_example_uses_synthetic_values(self) -> None:
        text = (ROOT / ".env.example").read_text(encoding="utf-8")
        self.assertIn("TARGET_GROUP_ID=123456789", text)
        self.assertIn("NAPCAT_ACCESS_TOKEN=replace-with-random-token", text)


if __name__ == "__main__":
    unittest.main()
