import unittest

from nonebot.adapters.onebot.v11 import Message, MessageSegment

from plugins.memory_governance.commands import (
    MemoryCommandError,
    is_memory_command,
    parse_memory_command,
)


class MemoryGovernanceCommandTests(unittest.TestCase):
    def parse(self, text: str, message: Message | None = None):
        return parse_memory_command(
            text,
            message or Message(text),
            group_id=123,
            private_allowed_user_ids=("200",),
        )

    def test_only_exact_first_token_is_recognized(self):
        self.assertTrue(is_memory_command("/记忆 帮助"))
        self.assertTrue(is_memory_command("  /记忆\n状态"))
        for text in ("前缀 /记忆", "/记忆状态", "x/记忆", ""):
            self.assertFalse(is_memory_command(text), text)
            self.assertIsNone(self.parse(text))

    def test_help_and_status_are_typed(self):
        self.assertEqual("help", self.parse("/记忆").action)
        self.assertEqual("help", self.parse("/记忆 帮助").action)
        self.assertEqual("status", self.parse("/记忆 状态").action)

    def test_private_view_add_relation_and_clear_forms(self):
        view = self.parse("/记忆 200")
        self.assertEqual(("view_facts", "private", "200"), (view.action, view.scope.kind, view.scope.user_id))
        relation = self.parse("/记忆 关系 200")
        self.assertEqual("view_relation", relation.action)
        add = self.parse("/记忆 添加 200 喜欢清淡口味")
        self.assertEqual(("add_fact", "喜欢清淡口味"), (add.action, add.content))
        update = self.parse("/记忆 关系 200 最近交流自然")
        self.assertEqual(("update_relation", "最近交流自然"), (update.action, update.content))
        self.assertEqual("clear_private", self.parse("/记忆 清空 200").action)

    def test_group_target_requires_a_real_onebot_at_segment(self):
        message = Message(
            [
                MessageSegment.text("/记忆 添加 "),
                MessageSegment.at(300),
                MessageSegment.text(" 喜欢养花"),
            ]
        )
        command = self.parse("/记忆 添加 @群友 喜欢养花", message)
        self.assertEqual(("add_fact", "group", "300", 123), (
            command.action, command.scope.kind, command.scope.user_id, command.scope.group_id,
        ))
        with self.assertRaises(MemoryCommandError):
            self.parse("/记忆 添加 @群友 喜欢养花", Message("/记忆 添加 @群友 喜欢养花"))

    def test_real_at_must_occupy_the_target_argument_position(self):
        malicious = Message(
            [
                MessageSegment.text("/记忆 添加 @伪目标 喜欢养花 "),
                MessageSegment.at(300),
            ]
        )
        with self.assertRaises(MemoryCommandError):
            self.parse("/记忆 添加 @伪目标 喜欢养花 @真实目标", malicious)

    def test_all_multiple_and_invalid_real_at_segments_are_always_rejected(self):
        invalid_messages = (
            Message(
                [
                    MessageSegment.text("/记忆 添加 "),
                    MessageSegment.at("all"),
                    MessageSegment.at(300),
                    MessageSegment.text(" 内容"),
                ]
            ),
            Message(
                [
                    MessageSegment.text("/记忆 关系 "),
                    MessageSegment.at(300),
                    MessageSegment.at(301),
                    MessageSegment.text(" 新状态"),
                ]
            ),
            Message(
                [
                    MessageSegment.text("/记忆 添加 "),
                    MessageSegment("at", {"qq": "invalid"}),
                    MessageSegment.text(" 内容"),
                ]
            ),
            Message(
                [
                    MessageSegment.text("/记忆 修改 G-1 内容 "),
                    MessageSegment.at(300),
                ]
            ),
            Message(
                [
                    MessageSegment.text("/记忆 状态"),
                    MessageSegment.at(300),
                ]
            ),
            Message(
                [
                    MessageSegment.text("/记忆 添加 200 内容 "),
                    MessageSegment.at(300),
                ]
            ),
        )
        for message in invalid_messages:
            with self.subTest(message=message), self.assertRaises(MemoryCommandError):
                self.parse(message.extract_plain_text(), message)

    def test_group_relation_view_and_update_use_at_target(self):
        message = Message([MessageSegment.text("/记忆 关系 "), MessageSegment.at(300)])
        self.assertEqual("view_relation", self.parse("/记忆 关系 @群友", message).action)
        message += MessageSegment.text(" 关系不错")
        self.assertEqual("update_relation", self.parse("/记忆 关系 @群友 关系不错", message).action)

    def test_fact_ids_are_strictly_scoped(self):
        group = self.parse("/记忆 修改 G-12 新内容")
        private = self.parse("/记忆 删除 P-9")
        self.assertEqual(("group", 12), (group.fact_kind, group.memory_id))
        self.assertEqual(("private", 9), (private.fact_kind, private.memory_id))
        for bad in ("G-0", "P-０１", "g-1", "12", "G--1"):
            with self.assertRaises(MemoryCommandError, msg=bad):
                self.parse(f"/记忆 删除 {bad}")

    def test_confirm_and_cancel_are_typed_and_reason_is_mandatory(self):
        confirmed = self.parse("/记忆 确认 opaque-token 修正错误事实")
        self.assertEqual(("confirm", "opaque-token", "修正错误事实"), (
            confirmed.action, confirmed.token, confirmed.reason,
        ))
        cancelled = self.parse("/记忆 取消 opaque-token")
        self.assertEqual(("cancel", "opaque-token"), (cancelled.action, cancelled.token))
        with self.assertRaises(MemoryCommandError):
            self.parse("/记忆 确认 opaque-token")

    def test_private_ids_are_ascii_positive_and_must_be_allowed(self):
        for target in ("0", "-1", "２００", "201"):
            with self.assertRaises(MemoryCommandError, msg=target):
                self.parse(f"/记忆 {target}")

    def test_malformed_and_overlong_content_are_rejected(self):
        for text in (
            "/记忆 未批准",
            "/记忆 添加 200",
            "/记忆 删除 G-1 多余",
            "/记忆 清空 G-1",
            "/记忆 状态 多余",
        ):
            with self.assertRaises(MemoryCommandError, msg=text):
                self.parse(text)
        with self.assertRaises(MemoryCommandError):
            self.parse("/记忆 添加 200 " + "字" * 81)
        with self.assertRaises(MemoryCommandError):
            self.parse("/记忆 关系 200 " + "字" * 601)


if __name__ == "__main__":
    unittest.main()
