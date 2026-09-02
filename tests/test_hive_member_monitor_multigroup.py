from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

from openpyxl import load_workbook


PROJECT_ROOT = Path(__file__).resolve().parents[1]
LEGACY_GROUP_ID = 900_000_000_000_300_001
SECOND_GROUP_ID = 900_000_000_000_300_002
BUSINESS_GROUP_ID = 900_000_000_000_300_003
REPORT_GROUP_ID = 900_000_000_000_300_004
USER_ID = 900_000_000_000_400_001
FIXED_TIME = datetime(2026, 9, 2, 14, 30, 0)


class MultiGroupConfigTests(unittest.TestCase):
    def _probe(self, *, monitor_ids: str, target_group_id: int) -> subprocess.CompletedProcess[str]:
        with tempfile.TemporaryDirectory() as directory:
            environment = os.environ.copy()
            environment.update(
                {
                    "BOT_INSTANCE_ROOT": directory,
                    "BOT_MODE": "full",
                    "TARGET_GROUP_ID": str(target_group_id),
                    "HIVE_MEMBER_MONITOR_ENABLED": "true",
                    "HIVE_MEMBER_MONITOR_GROUP_ID": str(LEGACY_GROUP_ID),
                    "HIVE_MEMBER_MONITOR_GROUP_IDS": monitor_ids,
                    "HIVE_MEMBER_MONITOR_GROUP_LABELS_JSON": json.dumps(
                        {
                            str(LEGACY_GROUP_ID): "蜂巢",
                            str(SECOND_GROUP_ID): "蜂窝",
                        },
                        ensure_ascii=False,
                    ),
                    "HIVE_MEMBER_REPORT_GROUP_ID": str(REPORT_GROUP_ID),
                    "MONITOR_ONLY_GROUP_IDS": str(REPORT_GROUP_ID + 1),
                    "PYTHONPATH": str(PROJECT_ROOT),
                }
            )
            code = """
import json
from plugins.violation_record.config import CONFIG

print(json.dumps({
    "ids": CONFIG.hive_member_monitor_group_ids,
    "monitor_only": CONFIG.monitor_only_group_ids,
    "capable": CONFIG.hive_member_monitor_capable,
    "labels": {
        str(group_id): CONFIG.hive_member_monitor_group_label(group_id)
        for group_id in CONFIG.hive_member_monitor_group_ids
    },
}, ensure_ascii=False))
"""
            return subprocess.run(
                [sys.executable, "-B", "-c", code],
                cwd=PROJECT_ROOT,
                env=environment,
                text=True,
                capture_output=True,
                check=False,
            )

    def test_legacy_and_list_config_merge_into_isolated_monitor_groups(self) -> None:
        completed = self._probe(
            monitor_ids=f"{SECOND_GROUP_ID},{LEGACY_GROUP_ID}",
            target_group_id=BUSINESS_GROUP_ID,
        )

        self.assertEqual(0, completed.returncode, completed.stderr)
        payload = json.loads(completed.stdout.strip().splitlines()[-1])
        self.assertEqual(
            [LEGACY_GROUP_ID, SECOND_GROUP_ID],
            payload["ids"],
        )
        self.assertTrue(payload["capable"])
        self.assertEqual("蜂巢", payload["labels"][str(LEGACY_GROUP_ID)])
        self.assertEqual("蜂窝", payload["labels"][str(SECOND_GROUP_ID)])
        self.assertTrue(
            {LEGACY_GROUP_ID, SECOND_GROUP_ID}.issubset(payload["monitor_only"])
        )

    def test_any_monitor_group_colliding_with_business_group_fails_closed(self) -> None:
        completed = self._probe(
            monitor_ids=f"{LEGACY_GROUP_ID},{SECOND_GROUP_ID}",
            target_group_id=SECOND_GROUP_ID,
        )

        self.assertNotEqual(0, completed.returncode)
        self.assertIn(
            "monitor groups must differ from TARGET_GROUP_ID",
            completed.stdout + completed.stderr,
        )


class MultiGroupExportAndServiceTests(unittest.IsolatedAsyncioTestCase):
    async def test_service_scopes_sync_and_export_to_its_explicit_group(self) -> None:
        from plugins.hive_member_monitor.service import HiveMemberMonitorService
        from plugins.hive_member_monitor.store import MemberSnapshotStore

        class Bot:
            def __init__(self) -> None:
                self.calls: list[tuple[str, dict[str, object]]] = []

            async def call_api(self, api: str, **kwargs: object) -> object:
                self.calls.append((api, kwargs))
                if api == "get_group_member_list":
                    return [
                        {
                            "group_id": SECOND_GROUP_ID,
                            "user_id": USER_ID,
                            "nickname": "测试成员",
                            "card": "",
                            "role": "member",
                        }
                    ]
                if api == "get_group_info":
                    return {
                        "group_id": SECOND_GROUP_ID,
                        "member_count": 1,
                    }
                if api == "upload_group_file":
                    return {"status": "ok"}
                raise AssertionError(f"unexpected api: {api}")

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            store = MemberSnapshotStore(root / "members.sqlite3")
            config = SimpleNamespace(
                hive_member_monitor_enabled=True,
                hive_member_monitor_group_id=LEGACY_GROUP_ID,
                hive_member_report_group_id=REPORT_GROUP_ID,
            )
            service = HiveMemberMonitorService(
                config=config,
                store=store,
                output_dir=root / "exports",
                monitor_group_id=SECOND_GROUP_ID,
                group_label="蜂窝",
                clock=lambda: FIXED_TIME,
            )
            bot = Bot()

            count = await service.sync_once(bot)

            self.assertEqual(1, count)
            self.assertEqual(0, store.member_count(LEGACY_GROUP_ID))
            self.assertEqual(1, store.member_count(SECOND_GROUP_ID))
            list_call = next(call for call in bot.calls if call[0] == "get_group_member_list")
            self.assertEqual(str(SECOND_GROUP_ID), list_call[1]["group_id"])
            upload_call = next(call for call in bot.calls if call[0] == "upload_group_file")
            self.assertEqual(str(REPORT_GROUP_ID), upload_call[1]["group_id"])
            self.assertEqual(
                "蜂窝群员名单_2026-09-02_14-30-00.xlsx",
                upload_call[1]["name"],
            )

    async def test_exporter_uses_group_label_for_file_and_sheet_names(self) -> None:
        from plugins.hive_member_monitor.exporter import export_member_list
        from plugins.hive_member_monitor.store import MemberSnapshot

        with tempfile.TemporaryDirectory() as directory:
            path = export_member_list(
                [MemberSnapshot(user_id=str(USER_ID), qq_name="测试成员")],
                output_dir=Path(directory),
                group_label="蜂箱",
                now=FIXED_TIME,
            )

            self.assertEqual(
                "蜂箱群员名单_2026-09-02_14-30-00.xlsx",
                path.name,
            )
            workbook = load_workbook(path, read_only=True, data_only=True)
            self.addCleanup(workbook.close)
            self.assertEqual("蜂箱群员名单", workbook.active.title)


class MultiGroupLifecycleTests(unittest.TestCase):
    def test_service_factory_builds_one_isolated_service_per_group(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            environment = os.environ.copy()
            environment.update(
                {
                    "BOT_INSTANCE_ROOT": str(root / "instance"),
                    "BOT_MODE": "chat_only",
                    "PYTHONPATH": str(PROJECT_ROOT),
                }
            )
            code = f"""
import json
from pathlib import Path
from types import SimpleNamespace

from plugins.hive_member_monitor.lifecycle import build_services
from plugins.hive_member_monitor.store import MemberSnapshotStore

legacy = {LEGACY_GROUP_ID}
second = {SECOND_GROUP_ID}
root = Path({str(root)!r})
config = SimpleNamespace(
    hive_member_monitor_enabled=True,
    hive_member_monitor_group_ids=(legacy, second),
    hive_member_report_group_id={REPORT_GROUP_ID},
    hive_member_monitor_database_path=root / "members.sqlite3",
    hive_member_monitor_export_dir=root / "exports",
    hive_member_monitor_group_label=lambda group_id: {{
        legacy: "蜂巢",
        second: "蜂窝",
    }}[group_id],
)
store = MemberSnapshotStore(config.hive_member_monitor_database_path)
services = build_services(
    config=config,
    store=store,
    runtime_enabled=lambda: True,
)
print(json.dumps({{
    "ids": sorted(services),
    "labels": {{str(key): value.group_label for key, value in services.items()}},
    "shared_store": len({{id(value.store) for value in services.values()}}) == 1,
}}))
"""
            completed = subprocess.run(
                [sys.executable, "-B", "-c", code],
                cwd=PROJECT_ROOT,
                env=environment,
                text=True,
                capture_output=True,
                check=False,
            )

            self.assertEqual(0, completed.returncode, completed.stderr)
            payload = json.loads(completed.stdout.strip().splitlines()[-1])
            self.assertEqual(
                [LEGACY_GROUP_ID, SECOND_GROUP_ID],
                payload["ids"],
            )
            self.assertEqual("蜂巢", payload["labels"][str(LEGACY_GROUP_ID)])
            self.assertEqual("蜂窝", payload["labels"][str(SECOND_GROUP_ID)])
            self.assertTrue(payload["shared_store"])

    def test_one_group_sync_failure_does_not_block_other_groups(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            environment = os.environ.copy()
            environment.update(
                {
                    "BOT_INSTANCE_ROOT": str(Path(directory) / "instance"),
                    "BOT_MODE": "chat_only",
                    "PYTHONPATH": str(PROJECT_ROOT),
                }
            )
            code = """
import asyncio
import json
from types import SimpleNamespace

from plugins.hive_member_monitor import lifecycle

calls = []

class Service:
    def __init__(self, group_id, fail=False):
        self.monitor_group_id = group_id
        self.group_label = str(group_id)
        self.fail = fail

    async def sync_once(self, bot):
        calls.append(self.monitor_group_id)
        if self.fail:
            raise RuntimeError("synthetic group failure")
        return 1

lifecycle._runtime_enabled = lambda: True
lifecycle._services.clear()
lifecycle._services.update({
    1: Service(1, fail=True),
    2: Service(2),
})
asyncio.run(lifecycle._sync_safely(SimpleNamespace()))
print(json.dumps(calls))
"""
            completed = subprocess.run(
                [sys.executable, "-B", "-c", code],
                cwd=PROJECT_ROOT,
                env=environment,
                text=True,
                capture_output=True,
                check=False,
            )

            self.assertEqual(0, completed.returncode, completed.stderr)
            self.assertEqual([1, 2], json.loads(completed.stdout.strip().splitlines()[-1]))


class MultiGroupMatcherTests(unittest.TestCase):
    def test_secondary_group_notice_is_routed_to_monitor_matcher(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            environment = os.environ.copy()
            environment.update(
                {
                    "BOT_INSTANCE_ROOT": directory,
                    "BOT_MODE": "full",
                    "TARGET_GROUP_ID": str(BUSINESS_GROUP_ID),
                    "HIVE_MEMBER_MONITOR_ENABLED": "true",
                    "HIVE_MEMBER_MONITOR_GROUP_ID": str(LEGACY_GROUP_ID),
                    "HIVE_MEMBER_MONITOR_GROUP_IDS": (
                        f"{LEGACY_GROUP_ID},{SECOND_GROUP_ID}"
                    ),
                    "HIVE_MEMBER_REPORT_GROUP_ID": str(REPORT_GROUP_ID),
                    "PYTHONPATH": str(PROJECT_ROOT),
                }
            )
            code = f"""
import nonebot
from nonebot.adapters.onebot.v11 import GroupDecreaseNoticeEvent

nonebot.init()
from plugins.hive_member_monitor import matcher

event = GroupDecreaseNoticeEvent(
    time=1,
    self_id=123456789,
    post_type="notice",
    notice_type="group_decrease",
    sub_type="leave",
    group_id={SECOND_GROUP_ID},
    operator_id=123456789,
    user_id={USER_ID},
)
print("yes" if matcher._target_group_decrease(event) else "no")
"""
            completed = subprocess.run(
                [sys.executable, "-B", "-c", code],
                cwd=PROJECT_ROOT,
                env=environment,
                text=True,
                capture_output=True,
                check=False,
            )

            self.assertEqual(0, completed.returncode, completed.stderr)
            self.assertEqual("yes", completed.stdout.strip().splitlines()[-1])


if __name__ == "__main__":
    unittest.main()
