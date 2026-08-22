import json
import os
import subprocess
import sys
import unittest
from dataclasses import asdict, replace
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from plugins.feature_control.state import FeatureController, FeatureState


class FeatureControllerTests(unittest.TestCase):
    def setUp(self):
        self.temporary_directory = TemporaryDirectory()
        self.path = Path(self.temporary_directory.name) / "runtime_features.json"
        self.defaults = FeatureState(
            business_enabled=True,
            chat_enabled=True,
            group_chat_enabled=True,
            private_chat_enabled=True,
            group_chat_allowed_group_ids=(100,),
            private_chat_allowed_user_ids=("200",),
            private_memory_enabled=False,
            relationship_state_enabled=False,
            memory_governance_enabled=False,
        )

    def tearDown(self):
        self.temporary_directory.cleanup()

    def test_parent_and_child_gates_are_both_required(self):
        controller = FeatureController(self.path, self.defaults)

        self.assertTrue(controller.group_chat_allowed(100))
        self.assertTrue(controller.private_chat_allowed("200"))
        controller.set_switch("chat_enabled", False, actor="1")

        self.assertFalse(controller.group_chat_allowed(100))
        self.assertFalse(controller.private_chat_allowed("200"))
        self.assertTrue(controller.business_allowed(999, 999))

    def test_new_memory_switches_default_to_disabled(self):
        state = FeatureController(self.path, self.defaults).snapshot()

        self.assertFalse(state.private_memory_enabled)
        self.assertFalse(state.relationship_state_enabled)
        self.assertFalse(state.memory_governance_enabled)

    def test_v104_runtime_state_uses_configured_defaults_for_new_switches(self):
        self.path.write_text(
            json.dumps(
                {
                    "business_enabled": True,
                    "chat_enabled": True,
                    "group_chat_enabled": True,
                    "private_chat_enabled": True,
                    "group_chat_allowed_group_ids": [100],
                    "private_chat_allowed_user_ids": ["200"],
                }
            ),
            encoding="utf-8",
        )

        loaded = FeatureController(self.path, self.defaults).snapshot()

        self.assertFalse(loaded.private_memory_enabled)
        self.assertFalse(loaded.relationship_state_enabled)
        self.assertFalse(loaded.memory_governance_enabled)

    def test_present_new_switches_still_require_strict_booleans(self):
        backup = replace(self.defaults, private_memory_enabled=True)
        self.path.write_text(
            json.dumps(
                {
                    **asdict(self.defaults),
                    "private_memory_enabled": "false",
                }
            ),
            encoding="utf-8",
        )
        self.path.with_suffix(self.path.suffix + ".bak").write_text(
            json.dumps(asdict(backup)), encoding="utf-8"
        )

        loaded = FeatureController(self.path, self.defaults).snapshot()

        self.assertTrue(loaded.private_memory_enabled)

    def test_state_survives_restart_and_keeps_backup(self):
        first = FeatureController(self.path, self.defaults)

        first.add_allowed("group_chat", "101", actor="1")
        second = FeatureController(self.path, self.defaults)

        self.assertIn(101, second.snapshot().group_chat_allowed_group_ids)
        self.assertTrue(self.path.with_suffix(self.path.suffix + ".bak").is_file())

    def test_invalid_write_keeps_old_in_memory_state(self):
        controller = FeatureController(self.path, self.defaults)

        with patch.object(controller, "_persist", side_effect=OSError("disk full")):
            with self.assertRaises(OSError):
                controller.set_switch("chat_enabled", False, actor="1")

        self.assertTrue(controller.snapshot().chat_enabled)

    def test_semantically_invalid_primary_recovers_from_valid_backup(self):
        backup = replace(self.defaults, group_chat_allowed_group_ids=(101,))
        self.path.write_text(
            json.dumps(
                {
                    **asdict(self.defaults),
                    "group_chat_allowed_group_ids": "12",
                }
            ),
            encoding="utf-8",
        )
        self.path.with_suffix(self.path.suffix + ".bak").write_text(
            json.dumps(asdict(backup)), encoding="utf-8"
        )

        controller = FeatureController(self.path, self.defaults)

        self.assertEqual((101,), controller.snapshot().group_chat_allowed_group_ids)

    def test_mutation_after_backup_recovery_preserves_validated_backup(self):
        recovered = replace(
            self.defaults,
            group_chat_allowed_group_ids=(101,),
            updated_by="recovered",
        )
        backup_path = self.path.with_suffix(self.path.suffix + ".bak")
        self.path.write_text("{corrupt", encoding="utf-8")
        backup_path.write_text(json.dumps(asdict(recovered)), encoding="utf-8")
        controller = FeatureController(self.path, self.defaults)

        controller.set_switch("chat_enabled", False, actor="1")
        self.path.write_text("{corrupt again", encoding="utf-8")

        restarted = FeatureController(self.path, self.defaults)
        self.assertEqual(recovered, restarted.snapshot())

    def test_failed_primary_replace_keeps_memory_and_valid_backup(self):
        controller = FeatureController(self.path, self.defaults)
        self.path.write_text("{corrupt", encoding="utf-8")
        backup_path = self.path.with_suffix(self.path.suffix + ".bak")
        real_replace = os.replace

        def fail_primary_replace(source, destination):
            if Path(destination) == self.path:
                raise OSError("replace failed")
            return real_replace(source, destination)

        with patch(
            "plugins.feature_control.state.os.replace",
            side_effect=fail_primary_replace,
        ):
            with self.assertRaises(OSError):
                controller.set_switch("chat_enabled", False, actor="1")

        self.assertEqual(self.defaults, controller.snapshot())
        self.assertEqual(
            self.defaults,
            FeatureController._load_state(backup_path),
        )

    def test_legacy_private_allowlist_migrates_to_new_tuple(self):
        environment = os.environ.copy()
        environment.update(
            {
                "TARGET_GROUP_ID": "999000111",
                "PRIVATE_CHAT_ALLOWED_USER_ID": "101, 202",
            }
        )
        environment.pop("PRIVATE_CHAT_ALLOWED_USER_IDS", None)

        completed = subprocess.run(
            [
                sys.executable,
                "-c",
                (
                    "from plugins.violation_record.config import CONFIG; "
                    "assert CONFIG.private_chat_allowed_user_ids == ('101', '202')"
                ),
            ],
            env=environment,
            capture_output=True,
            text=True,
            check=False,
        )

        self.assertEqual(0, completed.returncode, completed.stderr)

    def test_legacy_environment_keeps_historical_target_group_chat_enabled(self):
        environment = os.environ.copy()
        environment["TARGET_GROUP_ID"] = "999000111"
        environment["RANDOM_CHAT_ENABLED"] = "false"
        for key in (
            "BUSINESS_ENABLED",
            "CHAT_ENABLED",
            "GROUP_CHAT_ENABLED",
            "GROUP_CHAT_ALLOWED_GROUP_IDS",
            "PRIVATE_CHAT_ENABLED",
            "PRIVATE_CHAT_ALLOWED_USER_IDS",
        ):
            environment.pop(key, None)

        completed = subprocess.run(
            [
                sys.executable,
                "-c",
                (
                    "from plugins.violation_record.config import CONFIG; "
                    "assert CONFIG.chat_enabled is True; "
                    "assert CONFIG.group_chat_enabled is True; "
                    "assert CONFIG.group_chat_allowed_group_ids == (999000111,)"
                ),
            ],
            env=environment,
            capture_output=True,
            text=True,
            check=False,
        )

        self.assertEqual(0, completed.returncode, completed.stderr)

    def test_explicit_module_keys_override_legacy_chat_defaults(self):
        environment = os.environ.copy()
        environment.update(
            {
                "TARGET_GROUP_ID": "999000111",
                "RANDOM_CHAT_ENABLED": "true",
                "BUSINESS_ENABLED": "false",
                "CHAT_ENABLED": "false",
                "GROUP_CHAT_ENABLED": "false",
                "GROUP_CHAT_ALLOWED_GROUP_IDS": "222333444",
                "PRIVATE_CHAT_ENABLED": "false",
                "PRIVATE_CHAT_ALLOWED_USER_IDS": "333444555",
            }
        )

        completed = subprocess.run(
            [
                sys.executable,
                "-c",
                (
                    "from plugins.violation_record.config import CONFIG; "
                    "assert CONFIG.business_enabled is False; "
                    "assert CONFIG.chat_enabled is False; "
                    "assert CONFIG.group_chat_enabled is False; "
                    "assert CONFIG.group_chat_allowed_group_ids == (222333444,); "
                    "assert CONFIG.private_chat_enabled is False; "
                    "assert CONFIG.private_chat_allowed_user_ids == ('333444555',)"
                ),
            ],
            env=environment,
            capture_output=True,
            text=True,
            check=False,
        )

        self.assertEqual(0, completed.returncode, completed.stderr)

    def test_chat_vision_configuration_defaults_are_isolated_from_evidence(self):
        environment = os.environ.copy()
        environment["TARGET_GROUP_ID"] = "999000111"
        for key in (
            "CHAT_VISION_ENABLED",
            "CHAT_VISION_MODEL",
            "CHAT_VISION_IMAGE_ROOT",
            "CHAT_VISION_RETENTION_DAYS",
            "CHAT_VISION_MAX_BYTES",
            "CHAT_VISION_TIMEOUT",
            "CHAT_VISION_MAX_RETRIES",
        ):
            environment.pop(key, None)

        completed = subprocess.run(
            [
                sys.executable,
                "-c",
                (
                    "from plugins.violation_record.config import CONFIG; "
                    "assert CONFIG.chat_vision_enabled is False; "
                    "assert CONFIG.chat_vision_model == 'deepseek-v4-flash-vision-exp'; "
                    "assert CONFIG.chat_vision_retention_days == 7; "
                    "assert CONFIG.chat_vision_max_bytes == 10 * 1024 * 1024; "
                    "assert CONFIG.chat_vision_root.name == 'images'; "
                    "assert CONFIG.chat_vision_root.parent.name == 'chat_vision'; "
                    "assert CONFIG.chat_vision_root != CONFIG.evidence_root"
                ),
            ],
            env=environment,
            capture_output=True,
            text=True,
            check=False,
        )

        self.assertEqual(0, completed.returncode, completed.stderr)

    def test_private_memory_configuration_has_safe_bounded_defaults(self):
        environment = os.environ.copy()
        environment["TARGET_GROUP_ID"] = "999000111"
        for key in (
            "PRIVATE_MEMORY_ENABLED",
            "RELATIONSHIP_STATE_ENABLED",
            "MEMORY_GOVERNANCE_ENABLED",
            "PRIVATE_MEMORY_RETENTION_DAYS",
            "PRIVATE_MEMORY_MAX_MESSAGES",
            "PRIVATE_MEMORY_SHUTDOWN_TIMEOUT",
        ):
            environment.pop(key, None)

        completed = subprocess.run(
            [
                sys.executable,
                "-c",
                (
                    "from plugins.violation_record.config import CONFIG; "
                    "assert CONFIG.private_memory_enabled is False; "
                    "assert CONFIG.relationship_state_enabled is False; "
                    "assert CONFIG.memory_governance_enabled is False; "
                    "assert CONFIG.private_memory_retention_days == 30; "
                    "assert CONFIG.private_memory_max_messages == 500; "
                    "assert CONFIG.private_memory_shutdown_timeout == 10.0"
                ),
            ],
            env=environment,
            capture_output=True,
            text=True,
            check=False,
        )

        self.assertEqual(0, completed.returncode, completed.stderr)

    def test_runtime_uses_private_memory_environment_defaults(self):
        runtime_path = Path(self.temporary_directory.name) / "fresh-runtime.json"
        environment = os.environ.copy()
        environment.update(
            {
                "TARGET_GROUP_ID": "999000111",
                "PRIVATE_MEMORY_ENABLED": "true",
                "RELATIONSHIP_STATE_ENABLED": "true",
                "MEMORY_GOVERNANCE_ENABLED": "true",
            }
        )
        completed = subprocess.run(
            [
                sys.executable,
                "-c",
                (
                    "from dataclasses import replace; "
                    "from pathlib import Path; "
                    "import plugins.violation_record.config as config_module; "
                    f"config_module.CONFIG = replace(config_module.CONFIG, runtime_features_path=Path({str(runtime_path)!r})); "
                    "from plugins.feature_control.runtime import FEATURES; "
                    "state = FEATURES.snapshot(); "
                    "assert state.private_memory_enabled is True; "
                    "assert state.relationship_state_enabled is True; "
                    "assert state.memory_governance_enabled is True"
                ),
            ],
            env=environment,
            capture_output=True,
            text=True,
            check=False,
        )

        self.assertEqual(0, completed.returncode, completed.stderr)

    def test_persisted_runtime_memory_switches_override_environment_defaults(self):
        runtime_path = Path(self.temporary_directory.name) / "persisted-runtime.json"
        runtime_path.write_text(
            json.dumps(
                {
                    **asdict(self.defaults),
                    "private_memory_enabled": False,
                    "relationship_state_enabled": False,
                    "memory_governance_enabled": False,
                }
            ),
            encoding="utf-8",
        )
        environment = os.environ.copy()
        environment.update(
            {
                "TARGET_GROUP_ID": "999000111",
                "PRIVATE_MEMORY_ENABLED": "true",
                "RELATIONSHIP_STATE_ENABLED": "true",
                "MEMORY_GOVERNANCE_ENABLED": "true",
            }
        )
        completed = subprocess.run(
            [
                sys.executable,
                "-c",
                (
                    "from dataclasses import replace; "
                    "from pathlib import Path; "
                    "import plugins.violation_record.config as config_module; "
                    f"config_module.CONFIG = replace(config_module.CONFIG, runtime_features_path=Path({str(runtime_path)!r})); "
                    "from plugins.feature_control.runtime import FEATURES; "
                    "state = FEATURES.snapshot(); "
                    "assert state.private_memory_enabled is False; "
                    "assert state.relationship_state_enabled is False; "
                    "assert state.memory_governance_enabled is False"
                ),
            ],
            env=environment,
            capture_output=True,
            text=True,
            check=False,
        )

        self.assertEqual(0, completed.returncode, completed.stderr)


if __name__ == "__main__":
    unittest.main()
