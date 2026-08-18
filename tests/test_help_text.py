import asyncio
import unittest

from plugins.violation_record import ai_router, service
from plugins.violation_record.formatter import HELP_TEXT
from plugins.violation_record.policy_commands import parse_policy_command


class HelpTextContractTests(unittest.TestCase):
    def test_help_is_grouped_and_documents_operational_boundaries(self) -> None:
        for section in (
            "使用前",
            "一、常用查询",
            "二、新增违规记录",
            "三、群禁言",
            "四、状态与记录维护",
            "五、减数策略",
            "六、确认与取消",
        ):
            self.assertIn(section, HELP_TEXT)
        self.assertIn("@机器人", HELP_TEXT)
        self.assertIn("多条结果会使用合并转发", HELP_TEXT)
        self.assertIn("群禁言会立即执行", HELP_TEXT)
        self.assertIn("减数命令只接受 QQ号", HELP_TEXT)

    def test_help_contains_copyable_query_and_policy_commands(self) -> None:
        for command in (
            "查询 蜂巢 小明",
            "查询 蜂巢 123456",
            "查询 蜂巢 小明最近违规记录",
            "查询 蜂巢 本月违规记录",
            "导出蜂巢本月违规记录",
            "查询减数状态 蜂巢 123456",
            "查询减数日志 蜂巢 123456",
            "查询减缓名单",
            "查询减停名单",
            "查询减停建议名单",
            "查询减数待办",
            "减停 蜂巢 123456 事由",
            "清除减停 蜂巢 123456 事由",
            "续期减停 蜂巢 123456 事由",
            "拒绝减停建议 蜂巢 123456 事由",
        ):
            self.assertIn(command, HELP_TEXT)

    def test_help_intent_returns_exact_bounded_help_text(self) -> None:
        intent = ai_router._keyword_shortcut("帮助")
        self.assertIsNotNone(intent)
        result = asyncio.run(
            service.handle_intent(intent, "123456789", "90001", "记录员", "help-1")
        )

        self.assertEqual(HELP_TEXT, result)
        self.assertLess(len(HELP_TEXT), 2500)

    def test_advertised_policy_commands_are_accepted_by_fixed_parser(self) -> None:
        commands = (
            "查询减数状态 蜂巢 123456",
            "查询减数日志 蜂巢 123456",
            "查询减缓名单",
            "查询减停名单",
            "查询减停建议名单",
            "查询减数待办",
            "减停 蜂巢 123456 事由",
            "清除减停 蜂巢 123456 事由",
            "续期减停 蜂巢 123456 事由",
            "拒绝减停建议 蜂巢 123456 事由",
        )

        for command in commands:
            with self.subTest(command=command):
                self.assertIsNotNone(parse_policy_command(command))


if __name__ == "__main__":
    unittest.main()
