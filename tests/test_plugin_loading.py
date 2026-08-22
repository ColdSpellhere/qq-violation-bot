from __future__ import annotations

import os
import subprocess
import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class PluginLoadingTests(unittest.TestCase):
    def test_bot_import_registers_control_router_and_background_plugins(self) -> None:
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
from nonebot.matcher import matchers

loaded = {plugin.name for plugin in nonebot.get_loaded_plugins()}
loaded_modules = {plugin.module_name for plugin in nonebot.get_loaded_plugins()}
required = {
    "violation_record",
    "chat_archive",
    "chat_vision",
    "random_chat",
    "private_chat",
    "feature_control",
    "group_router",
}
missing = sorted(required - loaded)
if missing:
    raise SystemExit(f"missing loaded plugins: {missing}; loaded={sorted(loaded)}")
if "plugins.member_memory.matcher" not in loaded_modules:
    raise SystemExit(
        "missing loaded plugin module: plugins.member_memory.matcher; "
        f"loaded_modules={sorted(loaded_modules)}"
    )
if "plugins.feature_control.matcher" not in loaded_modules:
    raise SystemExit(
        "missing loaded plugin module: plugins.feature_control.matcher; "
        f"loaded_modules={sorted(loaded_modules)}"
    )
if "plugins.chat_vision" not in loaded_modules:
    raise SystemExit(
        "missing loaded plugin module: plugins.chat_vision; "
        f"loaded_modules={sorted(loaded_modules)}"
    )
if "plugins.private_memory" not in loaded_modules:
    raise SystemExit(
        "missing loaded plugin module: plugins.private_memory; "
        f"loaded_modules={sorted(loaded_modules)}"
    )
registered = {
    (priority, matcher.module.__name__)
    for priority, priority_matchers in matchers.items()
    for matcher in priority_matchers
}
expected_background = {
    (1, "plugins.chat_archive.matcher"),
    (2, "plugins.member_memory.matcher"),
    (2, "plugins.chat_vision.matcher"),
}
if not expected_background.issubset(registered):
    raise SystemExit(f"missing background matcher priorities: {sorted(registered)}")
group_response_modules = {
    module
    for _, module in registered
    if module in {
        "plugins.group_router.matcher",
        "plugins.random_chat.matcher",
        "plugins.violation_record.matcher",
    }
}
if group_response_modules != {"plugins.group_router.matcher"}:
    raise SystemExit(
        f"expected one group response matcher, got {sorted(group_response_modules)}"
    )
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
