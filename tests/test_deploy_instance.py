from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock


class DeployInstanceTests(unittest.TestCase):
    def setUp(self) -> None:
        try:
            from scripts.deploy_instance import DeploymentError, deploy_existing_release
        except ImportError as exc:
            self.fail(str(exc))
        self.DeploymentError = DeploymentError
        self.deploy = deploy_existing_release
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()
        self.releases = self.root / "releases"
        self.instances = self.root / "instances"
        self.releases.mkdir()
        self.instances.mkdir()
        self.old_sha = "1" * 40
        self.new_sha = "2" * 40
        self.kona_sha = "3" * 40
        for sha in (self.old_sha, self.new_sha, self.kona_sha):
            (self.releases / sha).mkdir()
        for name, sha in (("carrot", self.old_sha), ("kona", self.kona_sha)):
            root = self.instances / name
            root.mkdir()
            (root / "current").symlink_to(self.releases / sha)

    def test_rejects_unsafe_instance_and_non_full_sha(self) -> None:
        restart = Mock()
        health = Mock(return_value=True)
        for instance in ("Carrot", "../kona", "other"):
            with self.assertRaises(self.DeploymentError):
                self.deploy(
                    instance, self.new_sha, self.root, restart=restart, health=health
                )
        with self.assertRaises(self.DeploymentError):
            self.deploy(
                "carrot", "abc", self.root, restart=restart, health=health
            )

    def test_success_switches_only_requested_instance(self) -> None:
        restart = Mock()
        health = Mock(return_value=True)

        result = self.deploy(
            "carrot", self.new_sha, self.root, restart=restart, health=health
        )

        self.assertEqual(self.new_sha, result)
        self.assertEqual(
            self.releases / self.new_sha,
            (self.instances / "carrot" / "current").resolve(),
        )
        self.assertEqual(
            self.releases / self.kona_sha,
            (self.instances / "kona" / "current").resolve(),
        )
        restart.assert_called_once_with("carrot")
        health.assert_called_once_with("carrot", self.new_sha)

    def test_failed_health_rolls_back_only_requested_instance(self) -> None:
        restart = Mock()
        health = Mock(return_value=False)

        with self.assertRaisesRegex(self.DeploymentError, "health"):
            self.deploy(
                "carrot", self.new_sha, self.root, restart=restart, health=health
            )

        self.assertEqual(
            self.releases / self.old_sha,
            (self.instances / "carrot" / "current").resolve(),
        )
        self.assertEqual(
            self.releases / self.kona_sha,
            (self.instances / "kona" / "current").resolve(),
        )
        self.assertEqual([("carrot",), ("carrot",)], [call.args for call in restart.call_args_list])

    def test_failed_first_deploy_does_not_leave_failed_current_pointer(self) -> None:
        (self.instances / "kona" / "current").unlink()

        with self.assertRaisesRegex(self.DeploymentError, "health"):
            self.deploy(
                "kona",
                self.new_sha,
                self.root,
                restart=Mock(),
                health=Mock(return_value=False),
            )

        self.assertFalse((self.instances / "kona" / "current").exists())


if __name__ == "__main__":
    unittest.main()
