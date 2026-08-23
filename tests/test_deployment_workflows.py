from __future__ import annotations

from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[1]


class DeploymentWorkflowTests(unittest.TestCase):
    def test_main_ci_runs_full_tests_compile_and_public_scans(self) -> None:
        source = (ROOT / ".github/workflows/ci.yml").read_text(encoding="utf-8")
        self.assertIn("branches: [main]", source)
        self.assertIn("unittest discover -s tests", source)
        self.assertIn("compileall", source)
        self.assertIn("check_public_tree.py --history", source)
        self.assertIn("git grep", source)

    def test_kona_promotion_is_manual_protected_and_exact_sha(self) -> None:
        source = (ROOT / ".github/workflows/promote-kona.yml").read_text(
            encoding="utf-8"
        )
        self.assertIn("workflow_dispatch:", source)
        self.assertIn("environment: kona-production", source)
        self.assertIn("github.event.inputs.sha", source)
        self.assertIn("origin/main", source)
        self.assertIn("--instance kona", source)
        self.assertNotIn("push:", source)
        for secret in (
            "KONA_DEPLOY_HOST",
            "KONA_DEPLOY_USER",
            "KONA_DEPLOY_SSH_KEY",
            "KONA_DEPLOY_HOST_KEY",
        ):
            self.assertIn(f"secrets.{secret}", source)

    def test_carrot_candidate_never_pushes_github(self) -> None:
        source = (ROOT / "scripts/deploy_carrot_candidate.sh").read_text(
            encoding="utf-8"
        )
        self.assertIn("release/carrot-candidate", source)
        self.assertIn("--instance carrot", source)
        self.assertIn("github.com", source)
        self.assertIn("refusing GitHub remote", source)
        self.assertNotIn("git push origin", source)


if __name__ == "__main__":
    unittest.main()
