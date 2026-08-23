from __future__ import annotations

import os
from pathlib import Path
import subprocess
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]


class ProvisionSwapTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()
        self.fstab = self.root / "fstab"
        self.sysctl = self.root / "99-qq-bots-swap.conf"
        self.swap = self.root / "swapfile"
        self.fstab.write_text("# existing\n", encoding="utf-8")

    def _run(self, action: str) -> subprocess.CompletedProcess[str]:
        env = os.environ.copy()
        env.update(
            {
                "QQ_SWAP_TEST_MODE": "1",
                "QQ_SWAP_FILE": str(self.swap),
                "QQ_SWAP_FSTAB": str(self.fstab),
                "QQ_SWAP_SYSCTL": str(self.sysctl),
                "QQ_SWAP_SIZE_MIB": "2",
            }
        )
        return subprocess.run(
            ["bash", str(ROOT / "scripts/provision_swap.sh"), action],
            cwd=ROOT,
            env=env,
            text=True,
            capture_output=True,
            check=False,
        )

    def test_apply_is_idempotent_and_writes_exact_managed_config(self) -> None:
        first = self._run("apply")
        second = self._run("apply")

        self.assertEqual(0, first.returncode, first.stderr)
        self.assertEqual(0, second.returncode, second.stderr)
        self.assertTrue(self.swap.is_file())
        self.assertEqual(0o600, self.swap.stat().st_mode & 0o777)
        fstab = self.fstab.read_text(encoding="utf-8")
        self.assertEqual(1, fstab.count("# BEGIN qq-bots managed swap"))
        self.assertIn(f"{self.swap} none swap sw 0 0", fstab)
        self.assertEqual("vm.swappiness=10\n", self.sysctl.read_text(encoding="utf-8"))

    def test_remove_deletes_only_managed_swap_state(self) -> None:
        self.assertEqual(0, self._run("apply").returncode)

        removed = self._run("remove")

        self.assertEqual(0, removed.returncode, removed.stderr)
        self.assertFalse(self.swap.exists())
        self.assertFalse(self.sysctl.exists())
        self.assertEqual("# existing\n", self.fstab.read_text(encoding="utf-8"))

    def test_refuses_wrong_existing_swapfile_type(self) -> None:
        self.swap.mkdir()

        result = self._run("apply")

        self.assertNotEqual(0, result.returncode)
        self.assertIn("regular file", result.stderr)

    def test_refuses_unmanaged_existing_regular_file(self) -> None:
        self.swap.write_text("user data", encoding="utf-8")

        result = self._run("apply")

        self.assertNotEqual(0, result.returncode)
        self.assertIn("not managed", result.stderr)
        self.assertEqual("user data", self.swap.read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
