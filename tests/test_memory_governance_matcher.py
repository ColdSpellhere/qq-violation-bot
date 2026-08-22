from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

import nonebot
from nonebot.adapters.onebot.v11 import Message, MessageSegment
from nonebot.exception import FinishedException


try:
    nonebot.get_driver()
except ValueError:
    nonebot.init()

from plugins.memory_governance.commands import MemoryCommandError
from plugins.memory_governance.service import (
    CancelResult,
    CommitResult,
    PreviewResult,
    ViewResult,
)
from plugins.memory_governance import matcher


def _event(text: str, *, user_id: int = 900, group_id: int | None = 123):
    values = {
        "user_id": user_id,
        "message": Message(text),
        "get_plaintext": lambda: text,
    }
    if group_id is not None:
        values["group_id"] = group_id
    return SimpleNamespace(**values)


def _segment_event(
    message: Message, *, user_id: int = 900, group_id: int | None = 123
):
    event = _event(
        message.extract_plain_text(), user_id=user_id, group_id=group_id
    )
    event.message = message
    return event


def _features(*, enabled: bool = True, allowed: tuple[str, ...] = ("200",)):
    return SimpleNamespace(
        snapshot=Mock(
            return_value=SimpleNamespace(
                memory_governance_enabled=enabled,
                private_chat_allowed_user_ids=allowed,
            )
        )
    )


class MemoryGovernanceRuleTests(unittest.IsolatedAsyncioTestCase):
    async def test_only_exact_first_token_is_matched(self) -> None:
        for text in ("/记忆", "/记忆 状态", "  /记忆\n状态"):
            self.assertTrue(await matcher.is_memory_governance_event(_event(text)))
        for text in ("/记忆状态", "前缀 /记忆", "x/记忆", ""):
            self.assertFalse(await matcher.is_memory_governance_event(_event(text)))

    def test_matcher_is_priority_zero_and_blocks(self) -> None:
        self.assertEqual(0, matcher.memory_governance_matcher.priority)
        self.assertTrue(matcher.memory_governance_matcher.block)


class MemoryGovernanceHandlerTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.bot = AsyncMock()
        self.service = Mock()
        self.driver = SimpleNamespace(config=SimpleNamespace(superusers={"900"}))

    def _patches(self, *, enabled: bool = True):
        return (
            patch.object(matcher, "FEATURES", _features(enabled=enabled)),
            patch.object(matcher, "get_driver", return_value=self.driver),
            patch.object(matcher, "_create_service", return_value=self.service),
            patch.object(
                matcher.memory_governance_matcher, "finish", new=AsyncMock()
            ),
        )

    async def test_non_superuser_is_refused_before_parse_or_store_creation(self) -> None:
        self.driver.config.superusers = {"901"}
        feature_patch, driver_patch, factory_patch, finish_patch = self._patches()
        with feature_patch, driver_patch, factory_patch as factory, finish_patch as finish, patch.object(
            matcher, "parse_memory_command"
        ) as parse:
            await matcher.handle_memory_governance(self.bot, _event("/记忆 添加 200 私密内容"))

        finish.assert_awaited_once_with("你没有记忆治理权限。")
        parse.assert_not_called()
        factory.assert_not_called()
        self.service.assert_not_called()
        self.bot.send_private_msg.assert_not_awaited()

    async def test_disabled_governance_does_not_parse_or_access_store(self) -> None:
        feature_patch, driver_patch, factory_patch, finish_patch = self._patches(enabled=False)
        with feature_patch, driver_patch, factory_patch as factory, finish_patch as finish, patch.object(
            matcher, "parse_memory_command"
        ) as parse:
            await matcher.handle_memory_governance(self.bot, _event("/记忆 状态"))

        finish.assert_awaited_once_with("记忆治理功能已关闭。")
        parse.assert_not_called()
        factory.assert_not_called()
        self.bot.send_private_msg.assert_not_awaited()

    async def test_content_view_is_sent_only_by_private_message(self) -> None:
        self.service.view.return_value = ViewResult("P-1 私密事实正文")
        feature_patch, driver_patch, factory_patch, finish_patch = self._patches()
        with feature_patch, driver_patch, factory_patch, finish_patch as finish:
            await matcher.handle_memory_governance(self.bot, _event("/记忆 200"))

        self.bot.send_private_msg.assert_awaited_once_with(
            user_id=900, message="P-1 私密事实正文"
        )
        finish.assert_awaited_once_with("记忆治理结果已私发。")
        self.assertNotIn("私密事实正文", finish.await_args.args[0])

    async def test_real_at_view_reaches_service_with_exact_group_target(self) -> None:
        message = Message(
            [MessageSegment.text("/记忆 "), MessageSegment.at(300)]
        )
        self.service.view.return_value = ViewResult("群记忆详情")
        feature_patch, driver_patch, factory_patch, finish_patch = self._patches()
        with feature_patch, driver_patch, factory_patch, finish_patch:
            await matcher.handle_memory_governance(
                self.bot, _segment_event(message)
            )

        command = self.service.view.call_args.args[0]
        self.assertEqual("view_facts", command.action)
        self.assertEqual(
            ("group", "300", 123),
            (command.scope.kind, command.scope.user_id, command.scope.group_id),
        )

    async def test_real_all_plus_member_at_is_rejected_before_service_access(self) -> None:
        message = Message(
            [
                MessageSegment.text("/记忆 添加 "),
                MessageSegment.at("all"),
                MessageSegment.at(300),
                MessageSegment.text(" 内容"),
            ]
        )
        feature_patch, driver_patch, factory_patch, finish_patch = self._patches()
        with feature_patch, driver_patch, factory_patch as factory, finish_patch as finish:
            await matcher.handle_memory_governance(self.bot, _segment_event(message))

        finish.assert_awaited_once_with("记忆治理命令格式错误。")
        factory.assert_not_called()
        self.bot.send_private_msg.assert_not_awaited()

    async def test_real_at_add_reaches_preview_with_exact_content(self) -> None:
        message = Message(
            [
                MessageSegment.text("/记忆 添加 "),
                MessageSegment.at(300),
                MessageSegment.text(" 喜欢养花"),
            ]
        )
        self.service.preview.return_value = PreviewResult(
            "token", "添加预览", "2026-08-23T08:10:00Z", 7
        )
        feature_patch, driver_patch, factory_patch, finish_patch = self._patches()
        with feature_patch, driver_patch, factory_patch, finish_patch:
            await matcher.handle_memory_governance(
                self.bot, _segment_event(message)
            )

        command = self.service.preview.call_args.args[0]
        self.assertEqual(("add_fact", "喜欢养花"), (command.action, command.content))
        self.assertEqual(
            ("group", "300", 123),
            (command.scope.kind, command.scope.user_id, command.scope.group_id),
        )

    async def test_real_at_relation_reaches_preview_with_exact_content(self) -> None:
        message = Message(
            [
                MessageSegment.text("/记忆 关系 "),
                MessageSegment.at(300),
                MessageSegment.text(" 交流自然"),
            ]
        )
        self.service.preview.return_value = PreviewResult(
            "token", "关系预览", "2026-08-23T08:10:00Z", 7
        )
        feature_patch, driver_patch, factory_patch, finish_patch = self._patches()
        with feature_patch, driver_patch, factory_patch, finish_patch:
            await matcher.handle_memory_governance(
                self.bot, _segment_event(message)
            )

        command = self.service.preview.call_args.args[0]
        self.assertEqual(
            ("update_relation", "交流自然"),
            (command.action, command.content),
        )
        self.assertEqual(
            ("group", "300", 123),
            (command.scope.kind, command.scope.user_id, command.scope.group_id),
        )

    async def test_trailing_real_at_cannot_masquerade_as_group_target(self) -> None:
        message = Message(
            [
                MessageSegment.text("/记忆 添加 @伪目标 喜欢养花 "),
                MessageSegment.at(300),
            ]
        )
        feature_patch, driver_patch, factory_patch, finish_patch = self._patches()
        with feature_patch, driver_patch, factory_patch as factory, finish_patch as finish:
            await matcher.handle_memory_governance(
                self.bot, _segment_event(message)
            )

        factory.assert_not_called()
        finish.assert_awaited_once_with("记忆治理命令格式错误。")

    async def test_private_view_delivery_failure_never_falls_back_to_group(self) -> None:
        self.service.view.return_value = ViewResult("不可泄露正文")
        self.bot.send_private_msg.side_effect = RuntimeError("send failed")
        feature_patch, driver_patch, factory_patch, finish_patch = self._patches()
        with feature_patch, driver_patch, factory_patch, finish_patch as finish:
            await matcher.handle_memory_governance(self.bot, _event("/记忆 200"))

        finish.assert_awaited_once_with("私聊回执发送失败，未在群内展示。")
        self.assertNotIn("不可泄露正文", finish.await_args.args[0])

    async def test_preview_with_token_and_content_is_private_only(self) -> None:
        self.service.preview.return_value = PreviewResult(
            "secret-token", "预览：私密正文", "2026-08-23T08:10:00Z", 7
        )
        feature_patch, driver_patch, factory_patch, finish_patch = self._patches()
        with feature_patch, driver_patch, factory_patch, finish_patch as finish:
            await matcher.handle_memory_governance(
                self.bot, _event("/记忆 添加 200 私密正文")
            )

        private_text = self.bot.send_private_msg.await_args.kwargs["message"]
        self.assertIn("预览：私密正文", private_text)
        self.assertIn("secret-token", private_text)
        finish.assert_awaited_once_with("记忆治理预览已私发。")
        self.assertNotIn("secret-token", finish.await_args.args[0])

    async def test_confirm_passes_actor_and_reason_and_preserves_busy_message(self) -> None:
        message = "逻辑变更已提交，但物理清理未完成，需要重试 WAL checkpoint。"
        self.service.confirm.return_value = CommitResult(
            True, message, 7, physical_cleanup_complete=False
        )
        feature_patch, driver_patch, factory_patch, finish_patch = self._patches()
        with feature_patch, driver_patch, factory_patch, finish_patch as finish:
            await matcher.handle_memory_governance(
                self.bot, _event("/记忆 确认 secret-token 管理员核实")
            )

        kwargs = self.service.confirm.call_args.kwargs
        self.assertEqual("900", kwargs["actor"])
        self.assertEqual("管理员核实", kwargs["reason"])
        self.assertEqual("secret-token", self.service.confirm.call_args.args[0])
        self.bot.send_private_msg.assert_awaited_once_with(user_id=900, message=message)
        finish.assert_awaited_once_with("记忆治理操作结果已私发。")

    async def test_commit_failure_reports_no_change(self) -> None:
        self.service.confirm.return_value = CommitResult(False, "记忆治理操作失败。")
        feature_patch, driver_patch, factory_patch, finish_patch = self._patches()
        with feature_patch, driver_patch, factory_patch, finish_patch as finish:
            await matcher.handle_memory_governance(
                self.bot, _event("/记忆 确认 secret-token 管理员核实")
            )

        self.bot.send_private_msg.assert_not_awaited()
        finish.assert_awaited_once_with("记忆治理操作失败，状态未改变。")

    async def test_committed_change_and_receipt_failure_are_distinguished(self) -> None:
        self.service.confirm.return_value = CommitResult(
            True, "记忆治理操作已提交。", operation_id=7
        )
        self.bot.send_private_msg.side_effect = RuntimeError("send failed")
        feature_patch, driver_patch, factory_patch, finish_patch = self._patches()
        with feature_patch, driver_patch, factory_patch, finish_patch as finish:
            await matcher.handle_memory_governance(
                self.bot, _event("/记忆 确认 secret-token 管理员核实")
            )

        self.service.confirm.assert_called_once()
        finish.assert_awaited_once_with("记忆治理变更已提交，但私聊回执发送失败。")

    async def test_checkpoint_pending_is_preserved_when_receipt_also_fails(self) -> None:
        message = "逻辑变更已提交，但物理清理未完成，需要重试 WAL checkpoint。"
        self.service.confirm.return_value = CommitResult(
            True, message, operation_id=7, physical_cleanup_complete=False
        )
        self.bot.send_private_msg.side_effect = RuntimeError("send failed")
        feature_patch, driver_patch, factory_patch, finish_patch = self._patches()
        with feature_patch, driver_patch, factory_patch, finish_patch as finish:
            await matcher.handle_memory_governance(
                self.bot, _event("/记忆 确认 secret-token 管理员核实")
            )

        public_message = finish.await_args.args[0]
        self.assertIn(message, public_message)
        self.assertIn("私聊回执发送失败", public_message)

    async def test_cancel_passes_actor_without_a_confirmation_reason(self) -> None:
        self.service.cancel.return_value = CancelResult(True, "已取消记忆治理操作。", 7)
        feature_patch, driver_patch, factory_patch, finish_patch = self._patches()
        with feature_patch, driver_patch, factory_patch, finish_patch as finish:
            await matcher.handle_memory_governance(
                self.bot, _event("/记忆 取消 secret-token")
            )

        kwargs = self.service.cancel.call_args.kwargs
        self.assertEqual("900", kwargs["actor"])
        self.assertEqual("secret-token", self.service.cancel.call_args.args[0])
        self.assertNotIn("reason", kwargs)
        finish.assert_awaited_once_with("已取消记忆治理操作。")

    async def test_malformed_command_never_reaches_chat_model_or_store(self) -> None:
        feature_patch, driver_patch, factory_patch, finish_patch = self._patches()
        with feature_patch, driver_patch, factory_patch as factory, finish_patch as finish, patch.object(
            matcher,
            "parse_memory_command",
            side_effect=MemoryCommandError("malformed secret-token 私密正文"),
        ), patch(
            "plugins.private_chat.matcher.generate_reply", new=AsyncMock()
        ) as generate:
            await matcher.handle_memory_governance(
                self.bot, _event("/记忆 未批准 secret-token 私密正文")
            )

        factory.assert_not_called()
        generate.assert_not_awaited()
        finish.assert_awaited_once_with("记忆治理命令格式错误。")

    async def test_service_errors_log_only_the_exception_type(self) -> None:
        self.service.view.side_effect = RuntimeError("command secret-token content 200")
        feature_patch, driver_patch, factory_patch, finish_patch = self._patches()
        with feature_patch, driver_patch, factory_patch, finish_patch as finish, patch.object(
            matcher.logger, "error"
        ) as logged:
            await matcher.handle_memory_governance(
                self.bot, _event("/记忆 200")
            )

        log_text = logged.call_args.args[0]
        self.assertIn("RuntimeError", log_text)
        for secret in ("/记忆", "secret-token", "content", "200"):
            self.assertNotIn(secret, log_text)
        finish.assert_awaited_once_with("记忆治理服务异常，状态未改变。")

    async def test_normal_nonebot_finish_is_not_misreported_as_service_error(self) -> None:
        self.service.view.return_value = ViewResult("状态详情")
        feature_patch, driver_patch, factory_patch, _ = self._patches()
        finish = AsyncMock(side_effect=FinishedException)
        with feature_patch, driver_patch, factory_patch, patch.object(
            matcher.memory_governance_matcher, "finish", new=finish
        ), patch.object(matcher.logger, "error") as logged:
            with self.assertRaises(FinishedException):
                await matcher.handle_memory_governance(self.bot, _event("/记忆 状态"))

        finish.assert_awaited_once_with("记忆治理结果已私发。")
        logged.assert_not_called()


if __name__ == "__main__":
    unittest.main()
