from __future__ import annotations

import asyncio
import unittest
from unittest.mock import patch

from tests import test_policy_scheduler as scheduler_tests
from tests import test_policy_reliability as reliability_tests
from plugins.violation_record import db, scheduler

FakeBot = scheduler_tests.FakeBot


class SchedulerBusinessScopeTests(unittest.TestCase):
    setUp = scheduler_tests.PolicySchedulerTests.setUp
    tearDown = scheduler_tests.PolicySchedulerTests.tearDown

    def _weekly(self, count: int, text: str = "合成周报") -> None:
        for index in range(count):
            scheduler._record_missed_business_notification(
                idempotency_key=f"scope-weekly:{index}", message_type="weekly",
                message_text=f"{text}:{index}", reason="bot_offline",
                as_of="2026-08-02 12:00:00",
            )

    def _deliver(self, bot: FakeBot) -> int:
        with patch.object(scheduler, "_business_allowed", return_value=True):
            return asyncio.run(scheduler.deliver_missed_policy_summary(
                bot, as_of="2026-08-02 12:02:00"
            ))

    def test_weekly_backlog_keeps_all_rows_and_one_original_forward(self) -> None:
        self._weekly(37)
        bot = FakeBot()
        self.assertEqual(self._deliver(bot), 37)
        self.assertEqual(len(bot.forwarded), 1)
        contents = [node["data"]["content"] for node in bot.forwarded[0]["messages"]]
        self.assertEqual(len(contents), 2)
        self.assertIn("涉及提醒：37 条", contents[0])
        self.assertEqual(contents[1], "weekly 未发送通知（37 条）\n\n" + "\n\n".join(
            f"2026-08-02 12:00:00｜QQ离线\n合成周报:{index}" for index in range(37)
        ))
        with db.connect() as conn:
            self.assertEqual(conn.execute(
                "SELECT COUNT(*) FROM business_notification_outbox WHERE status='sent'"
            ).fetchone()[0], 37)

    def test_weekly_long_message_keeps_original_node_without_policy_splitting(self) -> None:
        self._weekly(1, "合成周报" * 4000)
        bot = FakeBot()
        self.assertEqual(self._deliver(bot), 1)
        self.assertEqual(len(bot.forwarded), 1)
        contents = [node["data"]["content"] for node in bot.forwarded[0]["messages"]]
        self.assertEqual(len(contents), 2)
        self.assertEqual(contents[1], "weekly 未发送通知（1 条）\n\n"
                         "2026-08-02 12:00:00｜QQ离线\n" + "合成周报" * 4000 + ":0")

    def test_mixed_summary_preserves_legacy_combined_weekly_delivery(self) -> None:
        self._weekly(1)
        reliability_tests.PolicyNotificationReliabilityTests._backlog(self, 1, 8000)
        bot = FakeBot()
        self.assertEqual(self._deliver(bot), 2)
        self.assertEqual(len(bot.forwarded), 1)
        contents = [node["data"]["content"] for node in bot.forwarded[0]["messages"]]
        self.assertIn("涉及提醒：2 条", contents[0])
        self.assertIn("policy_event 1 / weekly 1", contents[0])
        self.assertEqual(contents[-1], "weekly 未发送通知（1 条）\n\n"
                         "2026-08-02 12:00:00｜QQ离线\n合成周报:0")


if __name__ == "__main__":
    unittest.main()
