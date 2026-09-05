from __future__ import annotations

import fcntl
import json
import os
import subprocess
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import Mock, patch


class DeployInstanceTests(unittest.TestCase):
    def setUp(self) -> None:
        try:
            from scripts.deploy_instance import (
                DeploymentError,
                deploy_existing_release,
                prepare_release,
                prune_unreferenced_releases,
            )
        except ImportError as exc:
            self.fail(str(exc))
        self.DeploymentError = DeploymentError
        self.deploy = deploy_existing_release
        self.prepare = prepare_release
        self.prune = prune_unreferenced_releases
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

    def _repository(self) -> tuple[Path, str]:
        repo = self.root / "repository"
        repo.mkdir()
        subprocess.run(["git", "-C", str(repo), "init", "-q"], check=True)
        subprocess.run(
            ["git", "-C", str(repo), "config", "user.name", "Release Test"],
            check=True,
        )
        subprocess.run(
            ["git", "-C", str(repo), "config", "user.email", "release@example.invalid"],
            check=True,
        )
        (repo / "requirements.txt").write_text("", encoding="utf-8")
        (repo / "app.py").write_text("VALUE = 1\n", encoding="utf-8")
        subprocess.run(["git", "-C", str(repo), "add", "."], check=True)
        subprocess.run(
            ["git", "-C", str(repo), "commit", "-q", "-m", "release"],
            check=True,
        )
        sha = subprocess.run(
            ["git", "-C", str(repo), "rev-parse", "HEAD"],
            check=True,
            text=True,
            capture_output=True,
        ).stdout.strip()
        return repo, sha

    def _prepare_without_dependencies(self, repo: Path, sha: str) -> Path:
        # Empty requirements exercise a real local venv/entrypoint build.
        # No dependencies are fetched and pip's version network check is off.
        return self.prepare(repo, self.root, sha)

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
        self.assertEqual(
            self.releases / self.old_sha,
            (self.instances / "carrot" / "previous").resolve(),
        )
        restart.assert_called_once_with("carrot")
        health.assert_called_once_with("carrot", self.new_sha)

    def test_same_sha_retry_preserves_existing_previous_pointer(self) -> None:
        previous = self.instances / "carrot" / "previous"
        previous.symlink_to(self.releases / self.new_sha)

        result = self.deploy(
            "carrot",
            self.old_sha,
            self.root,
            restart=Mock(),
            health=Mock(return_value=True),
        )

        self.assertEqual(self.old_sha, result)
        self.assertEqual(
            self.releases / self.old_sha,
            (self.instances / "carrot" / "current").resolve(),
        )
        self.assertEqual(self.releases / self.new_sha, previous.resolve())

    def test_failed_health_rolls_back_only_requested_instance(self) -> None:
        restart = Mock()
        health = Mock(return_value=False)
        preserved_previous = self.releases / self.kona_sha
        (self.instances / "carrot" / "previous").symlink_to(preserved_previous)

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
        self.assertEqual(
            preserved_previous,
            (self.instances / "carrot" / "previous").resolve(),
        )
        self.assertEqual(
            [("carrot",), ("carrot",)],
            [call.args for call in restart.call_args_list],
        )

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

    def test_health_wait_allows_bounded_service_startup(self) -> None:
        try:
            from scripts.deploy_instance import wait_for_health
        except ImportError as exc:
            self.fail(str(exc))
        probe = Mock(side_effect=[False, False, True])
        clock = iter((0.0, 1.0, 2.0))
        sleeper = Mock()

        self.assertTrue(
            wait_for_health(
                probe,
                timeout_seconds=10,
                interval_seconds=1,
                monotonic=lambda: next(clock),
                sleep=sleeper,
            )
        )
        self.assertEqual(3, probe.call_count)
        self.assertEqual(2, sleeper.call_count)

    def test_health_wait_times_out_instead_of_waiting_forever(self) -> None:
        try:
            from scripts.deploy_instance import wait_for_health
        except ImportError as exc:
            self.fail(str(exc))
        probe = Mock(return_value=False)
        clock = iter((0.0, 1.0, 3.0))

        self.assertFalse(
            wait_for_health(
                probe,
                timeout_seconds=2,
                interval_seconds=1,
                monotonic=lambda: next(clock),
                sleep=Mock(),
            )
        )

    def test_prepare_release_writes_redacted_source_manifest(self) -> None:
        repo, sha = self._repository()

        release = self._prepare_without_dependencies(repo, sha)

        manifest_path = release / ".release-manifest.json"
        self.assertTrue(manifest_path.is_file())
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        self.assertEqual(
            {"format_version", "commit", "tree", "source_sha256", 'build'},
            set(manifest),
        )
        self.assertEqual(2, manifest["format_version"])
        self.assertEqual(sha, manifest["commit"])
        self.assertRegex(manifest["tree"], r"^[0-9a-f]{40}$")
        self.assertRegex(manifest["source_sha256"], r"^[0-9a-f]{64}$")
        self.assertRegex(manifest['build']['pip_freeze_sha256'], r'^[0-9a-f]{64}$')
        self.assertEqual('requirements.txt', manifest['build']['requirements_file'])
        self.assertNotIn("key", manifest_path.read_text(encoding="utf-8").lower())

    def test_prepare_release_rejects_unmanifested_existing_release(self) -> None:
        repo, sha = self._repository()
        release = self.releases / sha
        release.mkdir()
        (release / "requirements.txt").write_text("", encoding="utf-8")
        (release / "app.py").write_text("VALUE = 1\n", encoding="utf-8")

        with self.assertRaisesRegex(
            self.DeploymentError,
            "cannot be safely reused.*manifest",
        ):
            self.prepare(repo, self.root, sha)

    def test_environment_drift_is_detected_on_release_reuse(self) -> None:
        from scripts.deploy_instance import verify_release

        repo, sha = self._repository()
        release = self._prepare_without_dependencies(repo, sha)
        manifest_path = release/'.release-manifest.json'
        manifest = json.loads(manifest_path.read_text())
        manifest['build']['pip_freeze_sha256'] = 'f'*64
        manifest_path.write_text(json.dumps(manifest))
        with self.assertRaisesRegex(self.DeploymentError, 'environment drift'):
            verify_release(repo, release, sha, verify_environment=True)

    def test_prepare_release_rejects_tampered_or_injected_existing_release(self) -> None:
        repo, sha = self._repository()
        release = self._prepare_without_dependencies(repo, sha)

        (release / "app.py").write_text("VALUE = 2\n", encoding="utf-8")
        with self.assertRaisesRegex(self.DeploymentError, "tracked source"):
            self.prepare(repo, self.root, sha)

        (release / "app.py").write_text("VALUE = 1\n", encoding="utf-8")
        self.assertEqual(release, self.prepare(repo, self.root, sha))
        (release / "injected.py").write_text("raise SystemExit\n", encoding="utf-8")
        with self.assertRaisesRegex(self.DeploymentError, "unexpected release file"):
            self.prepare(repo, self.root, sha)

    def test_prune_protects_all_current_and_previous_releases(self) -> None:
        extra_shas = tuple(str(value) * 40 for value in range(4, 9))
        for index, sha in enumerate(extra_shas, start=4):
            release = self.releases / sha
            release.mkdir()
            os.utime(release, (index, index))
        for index, sha in enumerate(
            (self.old_sha, self.new_sha, self.kona_sha), start=1
        ):
            os.utime(self.releases / sha, (index, index))
        carrot_previous = self.releases / extra_shas[0]
        kona_previous = self.releases / extra_shas[1]
        (self.instances / "carrot" / "previous").symlink_to(carrot_previous)
        (self.instances / "kona" / "previous").symlink_to(kona_previous)

        self.prune(self.root, keep=1)

        protected = {
            self.releases / self.old_sha,
            self.releases / self.kona_sha,
            carrot_previous,
            kona_previous,
            self.releases / extra_shas[-1],
        }
        self.assertTrue(all(path.is_dir() for path in protected))
        self.assertFalse((self.releases / self.new_sha).exists())

    def test_prune_waits_for_in_flight_deployment_lock(self) -> None:
        lock_path = self.root / "deploy.lock"
        lock_path.touch()
        holder = subprocess.Popen(
            [
                sys.executable,
                "-c",
                (
                    "import fcntl, pathlib, sys; "
                    "handle = pathlib.Path(sys.argv[1]).open('r+'); "
                    "fcntl.flock(handle.fileno(), fcntl.LOCK_EX); "
                    "print('locked', flush=True); "
                    "sys.stdin.read(1)"
                ),
                str(lock_path),
            ],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            text=True,
        )
        self.assertEqual("locked", holder.stdout.readline().strip())
        attempted = threading.Event()
        completed = threading.Event()
        errors: list[BaseException] = []
        real_flock = fcntl.flock

        def observed_flock(file_descriptor: int, operation: int) -> None:
            attempted.set()
            real_flock(file_descriptor, operation)

        def run_prune() -> None:
            try:
                self.prune(self.root, keep=0)
            except BaseException as exc:
                errors.append(exc)
            finally:
                completed.set()

        worker = threading.Thread(target=run_prune)
        try:
            with patch(
                "scripts.deploy_instance.fcntl.flock",
                side_effect=observed_flock,
            ):
                worker.start()
                self.assertTrue(attempted.wait(2), "prune did not acquire deploy.lock")
                self.assertFalse(completed.wait(0.05))
                self.assertTrue((self.releases / self.new_sha).is_dir())
                holder.stdin.write("x")
                holder.stdin.flush()
                holder.wait(timeout=2)
                worker.join(timeout=2)
        finally:
            if holder.poll() is None:
                holder.kill()
                holder.wait(timeout=2)
            worker.join(timeout=2)
            if holder.stdin is not None:
                holder.stdin.close()
            if holder.stdout is not None:
                holder.stdout.close()
        self.assertFalse(worker.is_alive())
        self.assertEqual([], errors)
        self.assertFalse((self.releases / self.new_sha).exists())


if __name__ == "__main__":
    unittest.main()
