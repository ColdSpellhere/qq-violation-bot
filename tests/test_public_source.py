from __future__ import annotations

import os
import re
import subprocess
import unittest
import zipfile
from pathlib import Path

from dotenv import dotenv_values


ROOT = Path(__file__).resolve().parents[1]
FALLBACK_PUBLIC_FILES = (
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
CHAT_VISION_EXAMPLE_DEFAULTS = {
    "CHAT_VISION_ENABLED": "false",
    "CHAT_VISION_MODEL": "deepseek-v4-flash-vision-exp",
    "CHAT_VISION_IMAGE_ROOT": "data/chat_vision/images",
    "CHAT_VISION_RETENTION_DAYS": "7",
    "CHAT_VISION_MAX_BYTES": "10485760",
    "CHAT_VISION_TIMEOUT": "60",
    "CHAT_VISION_MAX_RETRIES": "3",
}


def _tracked_paths() -> list[Path]:
    result = subprocess.run(
        ["git", "ls-files", "-z"],
        cwd=ROOT,
        check=False,
        capture_output=True,
    )
    if result.returncode != 0:
        return list(FALLBACK_PUBLIC_FILES)
    return [ROOT / item for item in result.stdout.decode().split("\0") if item]


def _public_text() -> str:
    chunks: list[str] = []
    for path in _tracked_paths():
        try:
            chunks.append(path.read_text(encoding="utf-8"))
            continue
        except (UnicodeDecodeError, OSError):
            pass
        if path.suffix.lower() not in {".docx", ".xlsx", ".pptx"}:
            continue
        try:
            with zipfile.ZipFile(path) as archive:
                for name in archive.namelist():
                    if name.endswith((".xml", ".rels")):
                        chunks.append(archive.read(name).decode("utf-8"))
        except (OSError, UnicodeDecodeError, zipfile.BadZipFile):
            continue
    return "\n".join(chunks)


class PublicSourceBoundaryTests(unittest.TestCase):
    def test_live_runtime_values_are_absent_from_public_files(self) -> None:
        env_path = ROOT / ".env"
        public_text = _public_text()
        values = dotenv_values(env_path) if env_path.exists() else {}
        runtime_values = {
            key: str(os.getenv(key) or values.get(key) or "").strip()
            for key in SENSITIVE_KEYS
        }
        if not any(runtime_values.values()):
            self.skipTest("runtime values are not available")
        leaked = [
            key
            for key in SENSITIVE_KEYS
            if runtime_values[key] and runtime_values[key] in public_text
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

        target_group_id = str(os.getenv("TARGET_GROUP_ID") or "").strip()
        if target_group_id and target_group_id != "123456789":
            self.assertNotIn(f"TARGET_GROUP_ID={target_group_id}", text)

    def test_chat_vision_example_has_safe_defaults(self) -> None:
        values = dotenv_values(ROOT / ".env.example")
        self.assertEqual(CHAT_VISION_EXAMPLE_DEFAULTS, {
            key: values.get(key) for key in CHAT_VISION_EXAMPLE_DEFAULTS
        })

    def test_private_memory_migration_docs_export_project_dotenv(self) -> None:
        text = (ROOT / "README.md").read_text(encoding="utf-8")
        migration = text[text.index("#### 迁移、启用与回滚"):]
        self.assertIn("必须从项目根目录执行", migration)
        self.assertIn("set -a\n. ./.env\nset +a", migration)
        self.assertIn("TARGET_GROUP_ID", migration)

    def test_private_memory_changelog_distinguishes_checkpoint_outcomes(self) -> None:
        text = (ROOT / "CHANGELOG.md").read_text(encoding="utf-8")
        section = text[text.index("### 私聊连续性记忆与治理"):text.index("### 群聊图片理解")]
        self.assertIn("治理清空", section)
        self.assertIn("持久审计", section)
        self.assertIn("每日保留清理", section)
        self.assertIn("仅记录脱敏日志", section)

    def test_private_memory_plan_requires_external_synthetic_group_id(self) -> None:
        plan = (
            ROOT / "docs/superpowers/plans/2026-08-22-private-continuity-memory-governance.md"
        ).read_text(encoding="utf-8")
        self.assertIsNone(re.search(r"TARGET_GROUP_ID=\d{5,}", plan))
        self.assertIn("${TARGET_GROUP_ID:?", plan)


if __name__ == "__main__":
    unittest.main()
