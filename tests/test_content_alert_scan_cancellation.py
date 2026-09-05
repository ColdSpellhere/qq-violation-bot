import asyncio
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import AsyncMock, patch

from plugins.content_alert.rules import KeywordRuleStore
from plugins.content_alert.service import ContentAlertService
from tests.test_hive_keyword_alert import _group_event


class ScanCancellationTests(unittest.IsolatedAsyncioTestCase):
    async def test_cancelled_scan_keeps_admission_until_thread_finishes(self):
        with tempfile.TemporaryDirectory() as directory:
            store = KeywordRuleStore(Path(directory).resolve() / "rules.json")
            store.add("合成", actor="synthetic")
            event = _group_event("合成")
            service = ContentAlertService(rule_store=store, source_group_labels={int(event.group_id): "synthetic"},
                report_group_id=int(event.group_id)+1, peer_bot_user_ids=(), runtime_enabled=lambda: True,
                clock=lambda: float(event.time))
            entered, release = threading.Event(), threading.Event()
            def slow(*args):
                entered.set()
                release.wait(timeout=2)
                return (), ()
            with patch("plugins.content_alert.service._scan_literal_sources", side_effect=slow):
                task = asyncio.create_task(service.handle_event(AsyncMock(), event))
                try:
                    self.assertTrue(await asyncio.to_thread(entered.wait, 1))
                    task.cancel()
                    await asyncio.sleep(0.02)
                    self.assertEqual(service._admitted_scans, 1)
                    self.assertFalse(task.done())
                finally:
                    release.set()
                    await asyncio.gather(task, return_exceptions=True)
                self.assertEqual(service._admitted_scans, 0)
