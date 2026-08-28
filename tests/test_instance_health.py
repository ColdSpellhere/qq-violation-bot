from __future__ import annotations

import inspect
import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import Mock


ROOT = Path(__file__).resolve().parents[1]


def _runtime_state(**overrides: object) -> dict[str, object]:
    values: dict[str, object] = {
        "business_enabled": False,
        "chat_enabled": True,
        "group_chat_enabled": True,
        "private_chat_enabled": True,
        "group_chat_allowed_group_ids": [100],
        "private_chat_allowed_user_ids": ["200"],
        "llm_gateway_business_enabled": False,
        "economy_mode_enabled": False,
    }
    values.update(overrides)
    return values


class InstanceHealthTests(unittest.TestCase):
    def _repository_and_release(self, root: Path) -> tuple[Path, str, Path]:
        repo = root / "repository.git"
        repo.mkdir()
        subprocess.run(["git", "-C", str(repo), "init", "-q"], check=True)
        subprocess.run(
            ["git", "-C", str(repo), "config", "user.name", "Health Test"],
            check=True,
        )
        subprocess.run(
            ["git", "-C", str(repo), "config", "user.email", "health@example.invalid"],
            check=True,
        )
        (repo / "requirements.txt").write_text("", encoding="utf-8")
        (repo / "app.py").write_text("VALUE = 1\n", encoding="utf-8")
        subprocess.run(["git", "-C", str(repo), "add", "."], check=True)
        subprocess.run(
            ["git", "-C", str(repo), "commit", "-q", "-m", "health"],
            check=True,
        )
        sha = subprocess.run(
            ["git", "-C", str(repo), "rev-parse", "HEAD"],
            check=True,
            text=True,
            capture_output=True,
        ).stdout.strip()
        release = root / "releases" / sha
        release.mkdir(parents=True)
        (release / "requirements.txt").write_text("", encoding="utf-8")
        (release / "app.py").write_text("VALUE = 1\n", encoding="utf-8")
        instance = root / "instances/carrot"
        instance.mkdir(parents=True)
        (instance / "current").symlink_to(release)
        (instance / ".env").write_text("PORT=6199\n", encoding="utf-8")
        return repo, sha, release

    def test_log_check_is_scoped_to_current_systemd_invocation(self) -> None:
        from scripts.instance_health import current_invocation_logs

        invocation = "a" * 32
        runner = Mock(side_effect=[invocation + "\n", "current logs\n"])

        self.assertEqual(
            "current logs\n",
            current_invocation_logs("qqbot@carrot.service", run=runner),
        )
        self.assertEqual(
            ("systemctl", "show", "qqbot@carrot.service", "-p", "InvocationID", "--value"),
            runner.call_args_list[0].args,
        )
        self.assertEqual(
            ("journalctl", f"_SYSTEMD_INVOCATION_ID={invocation}", "--no-pager"),
            runner.call_args_list[1].args,
        )

    def test_log_check_rejects_missing_invocation_id(self) -> None:
        from scripts.instance_health import current_invocation_logs

        with self.assertRaisesRegex(RuntimeError, "invocation"):
            current_invocation_logs("qqbot@carrot.service", run=Mock(return_value=""))

    def test_persisted_state_is_parseable_and_kona_business_is_off(self) -> None:
        from scripts.instance_health import validate_runtime_state

        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "runtime_features.json"
            path.write_text(
                json.dumps(_runtime_state()),
                encoding="utf-8",
            )
            validate_runtime_state(
                "kona", path, environment={"BOT_MODE": "chat_only"}
            )
            path.write_text(
                json.dumps(_runtime_state(business_enabled=True)), encoding="utf-8"
            )
            with self.assertRaisesRegex(ValueError, "business"):
                validate_runtime_state(
                    "kona", path, environment={"BOT_MODE": "chat_only"}
                )

    def test_enabled_economy_mode_requires_valid_instance_glm_configuration(self) -> None:
        from scripts.instance_health import validate_runtime_state

        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "runtime_features.json"
            path.write_text(
                json.dumps(_runtime_state(economy_mode_enabled=True)),
                encoding="utf-8",
            )
            valid = {
                "GLM_BASE_URL": "https://open.bigmodel.cn/api/paas/v4",
                "GLM_MODEL": "glm-4.7-flash",
                "GLM_API_KEY": "synthetic-secret",
            }
            validate_runtime_state("carrot", path, environment=valid)
            for field, value in (
                ("GLM_BASE_URL", "https://attacker.invalid/v1"),
                ("GLM_MODEL", "paid-model"),
                ("GLM_API_KEY", ""),
            ):
                with self.subTest(field=field), self.assertRaisesRegex(
                    ValueError, "economy"
                ):
                    validate_runtime_state(
                        "carrot",
                        path,
                        environment={**valid, field: value},
                    )

    def test_environment_default_economy_mode_is_checked_when_state_file_is_missing(
        self,
    ) -> None:
        from scripts.instance_health import validate_runtime_state

        with tempfile.TemporaryDirectory() as temporary:
            missing = Path(temporary) / "runtime_features.json"
            with self.assertRaisesRegex(ValueError, "economy"):
                validate_runtime_state(
                    "carrot",
                    missing,
                    environment={"ECONOMY_MODE_ENABLED": "true"},
                )

    def test_fresh_kona_without_runtime_state_uses_chat_only_defaults(self) -> None:
        from scripts.instance_health import validate_runtime_state

        with tempfile.TemporaryDirectory() as temporary:
            missing = Path(temporary) / "runtime_features.json"
            validate_runtime_state(
                "kona",
                missing,
                environment={"BOT_MODE": "chat_only"},
            )

    def test_kona_always_rejects_full_mode_or_explicit_business_defaults(self) -> None:
        from scripts.instance_health import validate_runtime_state

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            persisted = root / "runtime_features.json"
            persisted.write_text(
                json.dumps(_runtime_state()),
                encoding="utf-8",
            )
            for path, environment in (
                (persisted, {"BOT_MODE": "full"}),
                (root / "missing.json", {"BOT_MODE": "full"}),
                (
                    root / "missing.json",
                    {"BOT_MODE": "chat_only", "BUSINESS_ENABLED": "true"},
                ),
                (
                    root / "missing.json",
                    {
                        "BOT_MODE": "chat_only",
                        "LLM_GATEWAY_BUSINESS_ENABLED": "yes",
                    },
                ),
            ):
                with self.subTest(path=path.name, environment=environment), self.assertRaisesRegex(
                    ValueError, "kona.*chat-only"
                ):
                    validate_runtime_state("kona", path, environment=environment)

    def test_semantically_invalid_main_uses_valid_backup_for_economy_check(self) -> None:
        from scripts.instance_health import validate_runtime_state

        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "runtime_features.json"
            path.write_text(
                json.dumps(
                    {
                        "business_enabled": False,
                        "llm_gateway_business_enabled": False,
                    }
                ),
                encoding="utf-8",
            )
            path.with_suffix(path.suffix + ".bak").write_text(
                json.dumps(_runtime_state(economy_mode_enabled=True)),
                encoding="utf-8",
            )

            with self.assertRaisesRegex(ValueError, "economy"):
                validate_runtime_state(
                    "kona",
                    path,
                    environment={"BOT_MODE": "chat_only"},
                )

    def test_verify_requires_repository_backed_release_manifest(self) -> None:
        from scripts import instance_health

        self.assertIn("repo", inspect.signature(instance_health.verify).parameters)
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            repo, sha, _release = self._repository_and_release(root)
            with self.assertRaisesRegex(RuntimeError, "manifest"):
                instance_health.verify("carrot", sha, root, repo=repo)

    def test_verify_rejects_manifest_sha_mismatch_before_runtime_checks(self) -> None:
        from scripts import deploy_instance, instance_health

        self.assertIn("repo", inspect.signature(instance_health.verify).parameters)
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            repo, sha, release = self._repository_and_release(root)
            tree = subprocess.run(
                ["git", "-C", str(repo), "rev-parse", f"{sha}^{{tree}}"],
                check=True,
                text=True,
                capture_output=True,
            ).stdout.strip()
            source_hash = deploy_instance.release_source_sha256(repo, release, sha)
            (release / ".release-manifest.json").write_text(
                json.dumps(
                    {
                        "format_version": 1,
                        "commit": "f" * 40,
                        "tree": tree,
                        "source_sha256": source_hash,
                    }
                ),
                encoding="utf-8",
            )

            with self.assertRaisesRegex(RuntimeError, "manifest.*commit"):
                instance_health.verify("carrot", sha, root, repo=repo)

    def test_systemd_template_is_instance_scoped(self) -> None:
        source = (ROOT / "deploy/systemd/qqbot@.service").read_text(
            encoding="utf-8"
        )
        self.assertIn("BOT_INSTANCE_ROOT=/opt/qq-bots/instances/%i", source)
        self.assertIn("EnvironmentFile=/opt/qq-bots/instances/%i/.env", source)
        self.assertIn("/opt/qq-bots/instances/%i/current", source)
        self.assertIn("Restart=on-failure", source)


if __name__ == "__main__":
    unittest.main()
