from __future__ import annotations

import os
import subprocess
import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class PluginLoadingTests(unittest.TestCase):
    def test_bot_import_registers_business_and_archive_plugins(self) -> None:
        env = os.environ.copy()
        env.update(
            {
                "TARGET_GROUP_ID": "123456789",
                "LOG_LEVEL": "WARNING",
            }
        )
        script = """
import nonebot
import bot

loaded = {plugin.name for plugin in nonebot.get_loaded_plugins()}
required = {"violation_record", "chat_archive"}
missing = sorted(required - loaded)
if missing:
    raise SystemExit(f"missing loaded plugins: {missing}; loaded={sorted(loaded)}")
"""

        completed = subprocess.run(
            [sys.executable, "-c", script],
            cwd=ROOT,
            env=env,
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )

        self.assertEqual(
            0,
            completed.returncode,
            f"stdout:\n{completed.stdout}\nstderr:\n{completed.stderr}",
        )


if __name__ == "__main__":
    unittest.main()
