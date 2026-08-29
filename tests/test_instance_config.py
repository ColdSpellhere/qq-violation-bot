from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


PROJECT_ROOT = Path(__file__).resolve().parents[1]


class InstanceConfigTests(unittest.TestCase):
    def test_production_entrypoint_disables_source_bytecode_writes(self) -> None:
        script = (PROJECT_ROOT / "scripts" / "start_bot.sh").read_text(
            encoding="utf-8"
        )

        self.assertIn("exec .venv/bin/python -B bot.py", script)

    def test_enabled_hive_monitor_rejects_business_target_as_monitor_group(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            environment = os.environ.copy()
            environment.update(
                {
                    "BOT_INSTANCE_ROOT": temporary,
                    "BOT_MODE": "full",
                    "TARGET_GROUP_ID": "123456789",
                    "HIVE_MEMBER_MONITOR_ENABLED": "true",
                    "HIVE_MEMBER_MONITOR_GROUP_ID": "123456789",
                    "HIVE_MEMBER_REPORT_GROUP_ID": "987654321",
                    "PYTHONPATH": str(PROJECT_ROOT),
                }
            )
            result = subprocess.run(
                [
                    sys.executable,
                    "-B",
                    "-c",
                    "from plugins.violation_record.config import CONFIG",
                ],
                cwd=PROJECT_ROOT,
                env=environment,
                text=True,
                capture_output=True,
                check=False,
            )

        self.assertNotEqual(0, result.returncode)
        self.assertIn(
            "HIVE_MEMBER_MONITOR_GROUP_ID must differ from TARGET_GROUP_ID",
            result.stdout + result.stderr,
        )

    def _probe(self, instance_root: Path) -> dict[str, object]:
        environment = os.environ.copy()
        for name in (
            "DATABASE_URL",
            "CHAT_VISION_IMAGE_ROOT",
            "PRIVATE_CHAT_ALLOWED_USER_ID",
            "PRIVATE_CHAT_ALLOWED_USER_IDS",
        ):
            environment.pop(name, None)
        environment.update(
            {
                "BOT_INSTANCE_ROOT": str(instance_root),
                "TARGET_GROUP_ID": str(10**8 + 73_185_296),
                "CHAT_CONTEXT_MESSAGES": "40",
                "CHAT_CONTEXT_MINUTES": "90",
                "CHAT_CONTEXT_SELF_MESSAGES": "3",
                "PYTHONPATH": str(PROJECT_ROOT),
            }
        )
        code = r'''
import json
from plugins.random_chat.persona import CHARACTER_FILE
from plugins.violation_record import config

print(json.dumps({
    "instance_root": str(config.INSTANCE_ROOT),
    "data_dir": str(config.DATA_DIR),
    "backup_dir": str(config.BACKUP_DIR),
    "character_file": str(CHARACTER_FILE),
    "config_character_file": str(config.CONFIG.character_file),
    "chat_archive_path": str(config.CONFIG.chat_archive_path),
    "runtime_features_path": str(config.CONFIG.runtime_features_path),
    "sticker_root": str(config.CONFIG.random_chat_sticker_root),
    "hive_monitor_database_path": str(config.CONFIG.hive_member_monitor_database_path),
    "hive_monitor_export_dir": str(config.CONFIG.hive_member_monitor_export_dir),
    "chat_context_messages": config.CONFIG.chat_context_messages,
    "chat_context_minutes": config.CONFIG.chat_context_minutes,
    "chat_context_self_messages": config.CONFIG.chat_context_self_messages,
    "mutable_paths": [
        str(config.CONFIG.database_path),
        str(config.CONFIG.chat_archive_path),
        str(config.CONFIG.member_memory_root),
        str(config.CONFIG.evidence_database_path),
        str(config.CONFIG.evidence_root),
        str(config.CONFIG.chat_vision_root),
        str(config.CONFIG.random_chat_sticker_root),
        str(config.CONFIG.runtime_features_path),
        str(config.CONFIG.hive_member_monitor_database_path),
        str(config.CONFIG.hive_member_monitor_export_dir),
    ],
}, ensure_ascii=False))
'''
        result = subprocess.run(
            [sys.executable, "-c", code],
            cwd=PROJECT_ROOT,
            env=environment,
            text=True,
            capture_output=True,
            check=False,
        )
        self.assertEqual(0, result.returncode, result.stderr)
        return json.loads(result.stdout.strip().splitlines()[-1])

    def test_instance_root_owns_every_mutable_path(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve() / "carrot"
            root.mkdir()

            payload = self._probe(root)

            self.assertEqual(str(root), payload["instance_root"])
            self.assertEqual(str(root / "data"), payload["data_dir"])
            self.assertEqual(str(root / "backups"), payload["backup_dir"])
            self.assertEqual(str(root / "character.md"), payload["character_file"])
            self.assertEqual(40, payload["chat_context_messages"])
            self.assertEqual(90, payload["chat_context_minutes"])
            self.assertEqual(3, payload["chat_context_self_messages"])
            self.assertEqual(
                str(root / "character.md"), payload["config_character_file"]
            )
            for value in payload["mutable_paths"]:
                self.assertTrue(Path(value).is_relative_to(root), value)

    def test_two_instance_roots_never_resolve_same_runtime_file(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            carrot_root = root / "carrot"
            kona_root = root / "kona"
            carrot_root.mkdir()
            kona_root.mkdir()

            carrot = self._probe(carrot_root)
            kona = self._probe(kona_root)

            for key in (
                "chat_archive_path",
                "runtime_features_path",
                "sticker_root",
                "character_file",
                "hive_monitor_database_path",
                "hive_monitor_export_dir",
            ):
                self.assertNotEqual(carrot[key], kona[key], key)

    def test_instance_env_is_loaded_before_application_config(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            (root / ".env").write_text(
                "TARGET_GROUP_ID=" + str(10**8 + 26_481_359) + "\n"
                "BOT_SELF_ID=" + str(10**9 + 23_456_789) + "\n",
                encoding="utf-8",
            )
            environment = os.environ.copy()
            environment.pop("TARGET_GROUP_ID", None)
            environment.pop("BOT_SELF_ID", None)
            environment["BOT_INSTANCE_ROOT"] = str(root)
            environment["PYTHONPATH"] = str(PROJECT_ROOT)
            code = r'''
from plugins.runtime_paths import load_instance_env
load_instance_env()
from plugins.violation_record.config import CONFIG
print(CONFIG.target_group_id)
print(CONFIG.bot_self_id)
'''

            result = subprocess.run(
                [sys.executable, "-c", code],
                cwd=PROJECT_ROOT,
                env=environment,
                text=True,
                capture_output=True,
                check=False,
            )

            self.assertEqual(0, result.returncode, result.stderr)
            self.assertEqual(
                [str(10**8 + 26_481_359), str(10**9 + 23_456_789)],
                result.stdout.strip().splitlines()[-2:],
            )


if __name__ == "__main__":
    unittest.main()
