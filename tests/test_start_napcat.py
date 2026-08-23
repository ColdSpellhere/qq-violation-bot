from __future__ import annotations

import os
from pathlib import Path
import subprocess
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]


class StartNapCatTests(unittest.TestCase):
    def test_env_parser_uses_portable_awk_quote_class(self) -> None:
        source = (ROOT / "scripts/start_napcat.sh").read_text(encoding="utf-8")
        self.assertNotIn(r"\"", source)

    def _run(self, instance: str, *, port: int, bot_id: str) -> subprocess.CompletedProcess[str]:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name).resolve()
        instance_root = root / "instances" / instance
        instance_root.mkdir(parents=True)
        (instance_root / ".env").write_text(
            f"BOT_SELF_ID={bot_id}\nPORT={port}\nNAPCAT_ACCESS_TOKEN=token-{instance}-123456\n",
            encoding="utf-8",
        )
        capture = root / "capture.txt"
        fake = root / "fake-xvfb"
        fake.write_text(
            "#!/usr/bin/env bash\n"
            "set -euo pipefail\n"
            "printf '%s\\n' \"$HOME\" \"$XDG_CONFIG_HOME\" \"$XDG_DATA_HOME\" "
            "\"$NAPCAT_ACCESS_TOKEN\" \"$NAPCAT_REVERSE_WS_PORT\" \"$*\" > \"$CAPTURE\"\n",
            encoding="utf-8",
        )
        fake.chmod(0o755)
        env = os.environ.copy()
        env.update(
            {
                "QQ_BOTS_ROOT": str(root),
                "XVFB_RUN": str(fake),
                "NAPCAT_INSTALL_ROOT": str(root),
                "NAPCAT_QQ_BINARY": "/opt/NapCat/qq",
                "CAPTURE": str(capture),
            }
        )
        result = subprocess.run(
            ["bash", str(ROOT / "scripts/start_napcat.sh"), instance],
            cwd=ROOT,
            env=env,
            text=True,
            capture_output=True,
            check=False,
        )
        result.capture = capture.read_text(encoding="utf-8") if capture.exists() else ""
        return result

    def test_instances_use_separate_home_config_token_port_and_qq_data(self) -> None:
        carrot = self._run("carrot", port=6199, bot_id="1234567890")
        kona = self._run("kona", port=6299, bot_id="2345678901")

        self.assertEqual(0, carrot.returncode, carrot.stderr)
        self.assertEqual(0, kona.returncode, kona.stderr)
        carrot_lines = carrot.capture.splitlines()
        kona_lines = kona.capture.splitlines()
        for index in range(5):
            self.assertNotEqual(carrot_lines[index], kona_lines[index])
        self.assertNotIn("--user-data-dir=", carrot_lines[-1])
        self.assertTrue(carrot_lines[1].endswith("/instances/carrot/napcat/config"))
        self.assertTrue(kona_lines[1].endswith("/instances/kona/napcat/config"))
        self.assertIn("-q 1234567890", carrot_lines[-1])
        self.assertIn("-q 2345678901", kona_lines[-1])

    def test_rejects_unsafe_instance_and_wrong_fixed_port(self) -> None:
        unsafe = subprocess.run(
            ["bash", str(ROOT / "scripts/start_napcat.sh"), "../kona"],
            cwd=ROOT,
            text=True,
            capture_output=True,
            check=False,
        )
        self.assertNotEqual(0, unsafe.returncode)
        wrong = self._run("kona", port=6199, bot_id="2345678901")
        self.assertNotEqual(0, wrong.returncode)
        self.assertIn("6299", wrong.stderr)


if __name__ == "__main__":
    unittest.main()
