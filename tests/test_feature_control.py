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


if __name__ == "__main__":
    unittest.main()
