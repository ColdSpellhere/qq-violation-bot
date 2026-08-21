import unittest
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


if __name__ == "__main__":
    unittest.main()
