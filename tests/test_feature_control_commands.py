from __future__ import annotations

import os
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

os.environ.setdefault("TARGET_GROUP_ID", "999000111")

import nonebot
from nonebot.adapters.onebot.v11 import GroupMessageEvent, Message

try:
    nonebot.get_driver()
except ValueError:
    nonebot.init()

from plugins.feature_control.commands import (
    execute_control_command,
    is_control_command,
)
from plugins.feature_control.matcher import handle_control_command
from plugins.feature_control.state import FeatureController, FeatureState


def _event(text: str, *, user_id: int = 1) -> GroupMessageEvent:
    message = Message(text)
    return GroupMessageEvent(
        time=2000,
        self_id=999999,
        post_type="message",
        sub_type="normal",
        user_id=user_id,
        message_type="group",
        message_id=456,
        group_id=123456,
        message=message,
        original_message=message,
        raw_message=text,
        font=0,
        sender={"user_id": user_id, "nickname": "测试者", "role": "member"},
    )


class FeatureControlCommandTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = TemporaryDirectory()
        self.controller = FeatureController(
            Path(self.temporary_directory.name) / "runtime_features.json",
            FeatureState(
                business_enabled=True,
                chat_enabled=True,
                group_chat_enabled=True,
                private_chat_enabled=True,
                group_chat_allowed_group_ids=(100,),
                private_chat_allowed_user_ids=("200",),
                private_memory_enabled=False,
                relationship_state_enabled=False,
                memory_governance_enabled=False,
                llm_gateway_enabled=False,
                prompt_builder_enabled=False,
                llm_gateway_vision_enabled=False,
                llm_gateway_private_memory_enabled=False,
                llm_gateway_member_memory_enabled=False,
                llm_gateway_chat_enabled=False,
                llm_gateway_business_enabled=False,
            ),
        )

    def tearDown(self) -> None:
        self.temporary_directory.cleanup()

    def test_recognizes_only_control_command_prefixes(self) -> None:
        for text in (
            "/模块状态",
            "/业务 开",
            "/聊天 关",
            "/群聊 开",
            "/群聊群 添加 123",
            "/私聊 关",
            "/私聊用户 列表",
            "/私聊记忆 开",
            "/关系状态 关",
            "/记忆治理 开",
            "/模型网关 开",
            "/模型网关 视觉 开",
            "/模型网关 私聊记忆 关",
            "/模型网关 成员记忆 开",
            "/模型网关 聊天 关",
            "/模型网关 业务 开",
            "/提示构建 开",
        ):
            self.assertTrue(is_control_command(text), text)
        for text in ("业务 开", "/未知 开", "/聊天会 开", "普通聊天"):
            self.assertFalse(is_control_command(text), text)

    def test_executes_all_switch_commands(self) -> None:
        self.assertEqual(
            "业务功能已关闭。",
            execute_control_command("/业务 关", self.controller, "1"),
        )
        self.assertEqual(
            "聊天总开关已关闭。",
            execute_control_command("/聊天 关", self.controller, "1"),
        )
        self.assertEqual(
            "群聊功能已关闭。",
            execute_control_command("/群聊 关", self.controller, "1"),
        )
        self.assertEqual(
            "私聊功能已关闭。",
            execute_control_command("/私聊 关", self.controller, "1"),
        )
        self.assertEqual(
            "私聊持久记忆已开启。",
            execute_control_command("/私聊记忆 开", self.controller, "1"),
        )
        self.assertEqual(
            "关系状态已开启。",
            execute_control_command("/关系状态 开", self.controller, "1"),
        )
        self.assertEqual(
            "记忆治理已开启。",
            execute_control_command("/记忆治理 开", self.controller, "1"),
        )
        self.assertEqual(
            "模型网关已开启。",
            execute_control_command("/模型网关 开", self.controller, "1"),
        )
        for domain, label in (
            ("视觉", "模型网关视觉调用"),
            ("私聊记忆", "模型网关私聊记忆调用"),
            ("成员记忆", "模型网关成员记忆调用"),
            ("聊天", "模型网关聊天调用"),
            ("业务", "模型网关业务调用"),
        ):
            self.assertEqual(
                f"{label}已开启。",
                execute_control_command(
                    f"/模型网关 {domain} 开", self.controller, "1"
                ),
            )
        self.assertEqual(
            "提示构建已开启。",
            execute_control_command("/提示构建 开", self.controller, "1"),
        )
        self.assertFalse(self.controller.snapshot().business_enabled)
        self.assertFalse(self.controller.snapshot().chat_enabled)
        self.assertFalse(self.controller.snapshot().group_chat_enabled)
        self.assertFalse(self.controller.snapshot().private_chat_enabled)
        self.assertTrue(self.controller.snapshot().private_memory_enabled)
        self.assertTrue(self.controller.snapshot().relationship_state_enabled)
        self.assertTrue(self.controller.snapshot().memory_governance_enabled)
        self.assertTrue(self.controller.snapshot().llm_gateway_enabled)
        self.assertTrue(self.controller.snapshot().prompt_builder_enabled)
        self.assertTrue(self.controller.snapshot().llm_gateway_vision_enabled)
        self.assertTrue(self.controller.snapshot().llm_gateway_private_memory_enabled)
        self.assertTrue(self.controller.snapshot().llm_gateway_member_memory_enabled)
        self.assertTrue(self.controller.snapshot().llm_gateway_chat_enabled)
        self.assertTrue(self.controller.snapshot().llm_gateway_business_enabled)

    def test_executes_all_allowlist_commands(self) -> None:
        self.assertEqual(
            "已添加群聊群：123。",
            execute_control_command("/群聊群 添加 123", self.controller, "1"),
        )
        self.assertEqual(
            "群聊群允许列表：100、123。",
            execute_control_command("/群聊群 列表", self.controller, "1"),
        )
        self.assertEqual(
            "已删除群聊群：123。",
            execute_control_command("/群聊群 删除 123", self.controller, "1"),
        )
        self.assertEqual(
            "已添加私聊用户：456。",
            execute_control_command("/私聊用户 添加 456", self.controller, "1"),
        )
        self.assertEqual(
            "私聊用户允许列表：200、456。",
            execute_control_command("/私聊用户 列表", self.controller, "1"),
        )
        self.assertEqual(
            "已删除私聊用户：456。",
            execute_control_command("/私聊用户 删除 456", self.controller, "1"),
        )

    def test_status_does_not_include_allowlist_ids(self) -> None:
        status = execute_control_command("/模块状态", self.controller, "1")

        self.assertIn("业务功能：开", status)
        self.assertIn("聊天总开关：开", status)
        self.assertIn("群聊功能：开（允许群数：1）", status)
        self.assertIn("私聊功能：开（允许用户数：1）", status)
        self.assertIn("私聊持久记忆：关", status)
        self.assertIn("关系状态：关", status)
        self.assertIn("记忆治理：关", status)
        self.assertIn("模型网关：关", status)
        self.assertIn("模型网关视觉调用：关", status)
        self.assertIn("模型网关私聊记忆调用：关", status)
        self.assertIn("模型网关成员记忆调用：关", status)
        self.assertIn("模型网关聊天调用：关", status)
        self.assertIn("模型网关业务调用：关", status)
        self.assertIn("提示构建：关", status)
        self.assertNotIn("100", status)
        self.assertNotIn("200", status)

    def test_reports_duplicate_missing_invalid_and_usage_errors(self) -> None:
        self.assertEqual(
            "群聊群：100 已在允许列表中。",
            execute_control_command("/群聊群 添加 100", self.controller, "1"),
        )
        self.assertEqual(
            "私聊用户：999 不在允许列表中。",
            execute_control_command("/私聊用户 删除 999", self.controller, "1"),
        )
        self.assertEqual(
            "群号必须为正整数。",
            execute_control_command("/群聊群 添加 abc", self.controller, "1"),
        )
        self.assertEqual(
            "用法：/业务 开|关。",
            execute_control_command("/业务", self.controller, "1"),
        )
        self.assertEqual(
            "用法：/模型网关 开|关，或 /模型网关 视觉|私聊记忆|成员记忆|聊天|业务 开|关。",
            execute_control_command("/模型网关 视觉", self.controller, "1"),
        )
        self.assertEqual(
            "不支持的模块管理命令。",
            execute_control_command("/未知 开", self.controller, "1"),
        )

    def test_rejects_non_ascii_allowlist_ids_without_mutating_state(self) -> None:
        invalid_commands = (
            ("/群聊群 添加 ²", "群号必须为正整数。"),
            ("/群聊群 添加 ٣", "群号必须为正整数。"),
            ("/私聊用户 添加 ²", "QQ号必须为正整数。"),
            ("/私聊用户 添加 ٣", "QQ号必须为正整数。"),
        )

        for command, response in invalid_commands:
            self.assertEqual(
                response,
                execute_control_command(command, self.controller, "1"),
            )
        self.assertEqual((100,), self.controller.snapshot().group_chat_allowed_group_ids)
        self.assertEqual(("200",), self.controller.snapshot().private_chat_allowed_user_ids)


class FeatureControlMatcherTests(unittest.IsolatedAsyncioTestCase):
    async def test_non_superuser_is_rejected_without_mutating_state(self) -> None:
        with TemporaryDirectory() as directory:
            controller = FeatureController(
                Path(directory) / "runtime_features.json",
                FeatureState(True, True, True, True, (), ()),
            )
            configured_driver = SimpleNamespace(
                config=SimpleNamespace(superusers={"2"})
            )
            with patch(
                "plugins.feature_control.matcher.FEATURES", controller
            ), patch(
                "plugins.feature_control.matcher.get_driver",
                return_value=configured_driver,
            ), patch(
                "plugins.feature_control.matcher.control_matcher.finish",
                new=AsyncMock(),
            ) as finish:
                await handle_control_command(_event("/业务 关", user_id=1))

            finish.assert_awaited_once_with("你没有模块管理权限。")
            self.assertTrue(controller.snapshot().business_enabled)

    async def test_persistence_failure_replies_without_reporting_success(self) -> None:
        with TemporaryDirectory() as directory:
            controller = FeatureController(
                Path(directory) / "runtime_features.json",
                FeatureState(True, True, True, True, (), ()),
            )
            configured_driver = SimpleNamespace(
                config=SimpleNamespace(superusers={"1"})
            )
            with patch(
                "plugins.feature_control.matcher.FEATURES", controller
            ), patch(
                "plugins.feature_control.matcher.get_driver",
                return_value=configured_driver,
            ), patch.object(
                controller, "_persist", side_effect=OSError("disk full")
            ), patch(
                "plugins.feature_control.matcher.control_matcher.finish",
                new=AsyncMock(),
            ) as finish:
                try:
                    await handle_control_command(_event("/业务 关", user_id=1))
                except OSError as exc:
                    self.fail(f"persistence error escaped matcher: {exc}")

            finish.assert_awaited_once_with("写入失败，状态未改变。")
            self.assertTrue(controller.snapshot().business_enabled)


if __name__ == "__main__":
    unittest.main()
