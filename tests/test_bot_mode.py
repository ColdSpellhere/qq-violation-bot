from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]


class BotModeTests(unittest.TestCase):
    def _run(self, code: str, *, mode: str) -> subprocess.CompletedProcess[str]:
        environment = os.environ.copy()
        environment.pop("TARGET_GROUP_ID", None)
        environment["BOT_MODE"] = mode
        environment["PYTHONPATH"] = str(ROOT)
        environment["LOG_LEVEL"] = "WARNING"
        return subprocess.run(
            [sys.executable, "-c", code],
            cwd=ROOT,
            env=environment,
            text=True,
            capture_output=True,
            check=False,
        )

    def test_chat_only_does_not_require_a_business_target(self) -> None:
        result = self._run(
            """
import json
from plugins.violation_record.config import CONFIG
print(json.dumps({
    "mode": CONFIG.bot_mode,
    "target": CONFIG.target_group_id,
    "allowed": CONFIG.allowed_group_ids,
    "business": CONFIG.business_enabled,
    "gateway_business": CONFIG.llm_gateway_business_enabled,
}))
""",
            mode="chat_only",
        )

        self.assertEqual(0, result.returncode, result.stderr)
        self.assertEqual(
            {
                "mode": "chat_only",
                "target": 0,
                "allowed": [],
                "business": False,
                "gateway_business": False,
            },
            json.loads(result.stdout.strip().splitlines()[-1]),
        )

    def test_invalid_mode_fails_closed(self) -> None:
        result = self._run(
            "from plugins.violation_record.config import CONFIG",
            mode="unknown",
        )

        self.assertNotEqual(0, result.returncode)
        self.assertIn("BOT_MODE must be full or chat_only", result.stderr)

    def test_chat_only_never_registers_business_scheduler(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            result = self._run(
                f"""
import os
os.environ["BOT_INSTANCE_ROOT"] = {temporary!r}
import nonebot
nonebot.init()
import plugins.violation_record
modules = [func.__module__ for func in nonebot.get_driver()._lifespan._startup_funcs]
if "plugins.violation_record.scheduler" in modules:
    raise SystemExit(f"business scheduler registered: {{modules}}")
""",
                mode="chat_only",
            )

        self.assertEqual(0, result.returncode, result.stderr)


if __name__ == "__main__":
    unittest.main()
