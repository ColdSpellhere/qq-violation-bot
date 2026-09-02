from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class PluginLoadingTests(unittest.TestCase):
    def test_configured_content_alert_loads_before_chat_handlers(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            env = os.environ.copy()
            env.update(
                {
                    "BOT_INSTANCE_ROOT": directory,
                    "TARGET_GROUP_ID": "123456789",
                    "LOG_LEVEL": "WARNING",
                    "CONTENT_ALERT_ENABLED": "true",
                    "CONTENT_ALERT_SOURCE_GROUP_IDS": "123456780",
                    "CONTENT_ALERT_REPORT_GROUP_ID": "123456781",
                    "MONITOR_ONLY_GROUP_IDS": "123456780",
                }
            )
            script = """
import nonebot
import bot
from nonebot.matcher import matchers

loaded_modules = {plugin.module_name for plugin in nonebot.get_loaded_plugins()}
if "plugins.content_alert.content_alert_runtime" not in loaded_modules:
    raise SystemExit(f"content alert runtime missing: {sorted(loaded_modules)}")
registered = {
    matcher.module.__name__
    for priority_matchers in matchers.values()
    for matcher in priority_matchers
}
if "plugins.content_alert.matcher" not in registered:
    raise SystemExit(f"content alert matcher missing: {sorted(registered)}")
source = open("bot.py", encoding="utf-8").read()
if source.index('"plugins.content_alert.content_alert_runtime"') >= source.index('nonebot.load_plugin("plugins.chat_archive")'):
    raise SystemExit("content alert must register before chat handlers")
"""
            completed = subprocess.run(
                [sys.executable, "-B", "-c", script],
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

    def test_unconfigured_instance_registers_no_content_alert_matcher_or_file(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            env = os.environ.copy()
            env.update(
                {
                    "BOT_INSTANCE_ROOT": directory,
                    "BOT_MODE": "chat_only",
                    "TARGET_GROUP_ID": "123456789",
                    "LOG_LEVEL": "WARNING",
                    "CONTENT_ALERT_ENABLED": "false",
                }
            )
            for key in (
                "CONTENT_ALERT_SOURCE_GROUP_IDS",
                "CONTENT_ALERT_REPORT_GROUP_ID",
            ):
                env.pop(key, None)
            script = """
import sys
from pathlib import Path
import nonebot
import bot
from nonebot.matcher import matchers

registered = {
    matcher.module.__name__
    for priority_matchers in matchers.values()
    for matcher in priority_matchers
}
if "plugins.content_alert.matcher" in registered:
    raise SystemExit("unconfigured instance registered content alert matcher")
if "plugins.content_alert.matcher" in sys.modules:
    raise SystemExit("unconfigured instance imported content alert matcher")
if (Path(__import__('os').environ['BOT_INSTANCE_ROOT']) / 'data' / 'content_alert').exists():
    raise SystemExit("unconfigured instance created content alert data")
"""
            completed = subprocess.run(
                [sys.executable, "-B", "-c", script],
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

    def test_configured_hive_monitor_loads_notice_plugin_before_chat_handlers(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            env = os.environ.copy()
            env.update(
                {
                    "BOT_INSTANCE_ROOT": directory,
                    "TARGET_GROUP_ID": "123456789",
                    "LOG_LEVEL": "WARNING",
                    "HIVE_MEMBER_MONITOR_ENABLED": "true",
                    "HIVE_MEMBER_MONITOR_GROUP_ID": "123456780",
                    "HIVE_MEMBER_REPORT_GROUP_ID": "123456789",
                    "MONITOR_ONLY_GROUP_IDS": "123456780",
                }
            )
            script = """
import nonebot
import bot
from nonebot.matcher import matchers

loaded_modules = {plugin.module_name for plugin in nonebot.get_loaded_plugins()}
if "plugins.hive_member_monitor.hive_member_monitor_runtime" not in loaded_modules:
    raise SystemExit(f"hive monitor plugin missing: {sorted(loaded_modules)}")
registered = {
    matcher.module.__name__
    for priority_matchers in matchers.values()
    for matcher in priority_matchers
}
if "plugins.hive_member_monitor.matcher" not in registered:
    raise SystemExit(f"hive notice matcher missing: {sorted(registered)}")
source = open("bot.py", encoding="utf-8").read()
if source.index('"plugins.hive_member_monitor.hive_member_monitor_runtime"') >= source.index('nonebot.load_plugin("plugins.chat_archive")'):
    raise SystemExit("hive monitor must register before chat handlers")
"""
            completed = subprocess.run(
                [sys.executable, "-B", "-c", script],
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

    def test_unconfigured_instance_registers_no_hive_notice_or_lifecycle(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            env = os.environ.copy()
            env.update(
                {
                    "BOT_INSTANCE_ROOT": directory,
                    "BOT_MODE": "chat_only",
                    "LOG_LEVEL": "WARNING",
                    "HIVE_MEMBER_MONITOR_ENABLED": "false",
                }
            )
            for key in (
                "HIVE_MEMBER_MONITOR_GROUP_ID",
                "HIVE_MEMBER_REPORT_GROUP_ID",
                "MONITOR_ONLY_GROUP_IDS",
            ):
                env.pop(key, None)
            script = """
import sys
import nonebot
import bot
from nonebot.matcher import matchers

registered = {
    matcher.module.__name__
    for priority_matchers in matchers.values()
    for matcher in priority_matchers
}
if "plugins.hive_member_monitor.matcher" in registered:
    raise SystemExit("unconfigured instance registered hive notice matcher")
if "plugins.hive_member_monitor.lifecycle" in {
    func.__module__ for func in nonebot.get_driver()._lifespan._startup_funcs
}:
    raise SystemExit("unconfigured instance registered hive lifecycle")
if "plugins.hive_member_monitor.matcher" in sys.modules:
    raise SystemExit("unconfigured instance imported hive matcher")
"""
            completed = subprocess.run(
                [sys.executable, "-B", "-c", script],
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

    def test_gateway_entrypoint_loads_after_private_schema_lifecycle_before_handlers(self) -> None:
        source = (ROOT / "bot.py").read_text(encoding="utf-8")
        private_schema = source.index('nonebot.load_plugin("plugins.private_memory")')
        gateway = source.index('nonebot.load_plugin("plugins.llm_gateway.runtime")')
        feature_handler = source.index(
            'nonebot.load_plugin("plugins.feature_control.matcher")'
        )
        private_handler = source.index('nonebot.load_plugin("plugins.private_chat")')
        web_search = source.index("import plugins.web_search.runtime")
        self.assertLess(private_schema, gateway)
        self.assertLess(gateway, feature_handler)
        self.assertLess(gateway, private_handler)
        self.assertLess(gateway, web_search)
        self.assertLess(web_search, feature_handler)

    def test_gateway_package_import_has_no_runtime_registration_side_effect(self) -> None:
        env = os.environ.copy()
        env.update({"TARGET_GROUP_ID": "123456789", "LOG_LEVEL": "WARNING"})
        script = """
import sys
import nonebot

nonebot.init()
import plugins.llm_gateway
if "plugins.llm_gateway.runtime" in sys.modules:
    raise SystemExit("package import unexpectedly imported runtime entrypoint")
"""
        completed = subprocess.run(
            [sys.executable, "-c", script], cwd=ROOT, env=env,
            capture_output=True, text=True, timeout=30, check=False,
        )
        self.assertEqual(
            0, completed.returncode,
            f"stdout:\n{completed.stdout}\nstderr:\n{completed.stderr}",
        )

    def test_governance_loads_after_feature_control_before_chat_plugins(self) -> None:
        source = (ROOT / "bot.py").read_text(encoding="utf-8")
        feature = source.index('nonebot.load_plugin("plugins.feature_control.matcher")')
        governance_package = source.index(
            'nonebot.load_plugin("plugins.memory_governance")'
        )
        governance_matcher = source.index(
            'nonebot.load_plugin("plugins.memory_governance.matcher")'
        )
        first_chat_plugin = source.index('nonebot.load_plugin("plugins.chat_archive")')
        self.assertLess(feature, governance_package)
        self.assertLess(governance_package, governance_matcher)
        self.assertLess(governance_matcher, first_chat_plugin)

    def test_governance_package_import_has_no_matcher_side_effect(self) -> None:
        env = os.environ.copy()
        env.update({"TARGET_GROUP_ID": "123456789", "LOG_LEVEL": "WARNING"})
        script = """
import sys
import nonebot

nonebot.init()
import plugins.memory_governance
if "plugins.memory_governance.matcher" in sys.modules:
    raise SystemExit("package import unexpectedly imported matcher")
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
    "memory_governance",
    "group_router",
    "runtime",
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
if "plugins.memory_governance.matcher" not in loaded_modules:
    raise SystemExit(
        "missing loaded plugin module: plugins.memory_governance.matcher; "
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
if "plugins.llm_gateway.runtime" not in loaded_modules:
    raise SystemExit(
        "missing loaded plugin module: plugins.llm_gateway.runtime; "
        f"loaded_modules={sorted(loaded_modules)}"
    )
startup_modules = [func.__module__ for func in nonebot.get_driver()._lifespan._startup_funcs]
private_index = startup_modules.index("plugins.private_memory.lifecycle")
gateway_index = startup_modules.index("plugins.llm_gateway.runtime")
if private_index >= gateway_index:
    raise SystemExit(f"schema migration must register before gateway: {startup_modules}")
registered = {
    (priority, matcher.module.__name__)
    for priority, priority_matchers in matchers.items()
    for matcher in priority_matchers
}
expected_background = {
    (0, "plugins.memory_governance.matcher"),
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
