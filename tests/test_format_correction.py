from __future__ import annotations

import unittest
from contextlib import nullcontext
from dataclasses import replace
from unittest.mock import MagicMock, patch

from plugins.violation_record import service
from plugins.violation_record import formatter


def call_formatter(intent: dict, missing_fields: list[str]) -> str:
    function = getattr(formatter, "format_create_correction", None)
    assert function is not None, "format_create_correction is missing"
    return function(intent, missing_fields)


def create_intent() -> dict:
    return {
        "intent": "create_violation",
        "group_area": "蜂巢",
        "target": {"qq_number": None, "qq_nickname": "小明"},
        "violation": {
            "time": None,
            "judgement": "刷屏",
            "action": "禁言",
            "handler_admin_qq": None,
            "handler_admin_nickname": "企鹅",
            "remark": None,
        },
        "operation": {
            "confidence": 0.9,
            "missing_fields": [],
            "ambiguous_fields": [],
        },
    }


OPERATOR = {"id": 1, "qq_number": "90001", "nickname": "记录员"}
MEMBER = {"id": 2, "qq_number": "123456", "qq_nickname": "小明"}
HANDLER = {"id": 1, "qq_number": "90001", "nickname": "记录员"}


def complete_create_intent() -> dict:
    intent = create_intent()
    intent["target"] = {"qq_number": "123456", "qq_nickname": "小明"}
    intent["violation"]["time"] = "2026-07-31 15:30:00"
    intent["violation"]["action"] = "禁言10分钟"
    intent["violation"]["handler_admin_nickname"] = None
    intent["operation"]["confidence"] = 1.0
    return intent


class FormatCreateCorrectionTests(unittest.TestCase):
    def test_missing_qq_and_time_produces_copy_ready_line(self) -> None:
        text = call_formatter(
            create_intent(), ["target.qq_number", "violation.time"]
        )
        self.assertIn("格式缺少：QQ号、时间", text)
        self.assertIn(
            "蜂巢 小明（<QQ号>） <时间，24小时制，如03:30或15:30> 刷屏，禁言，企鹅处理",
            text,
        )
        self.assertIn("记录人：自动取当前发送者", text)
        self.assertIn("未写处理人时：默认等于记录人", text)
        self.assertIn("备注：未写时默认为“无”", text)

    def test_missing_area_and_action_keeps_recognized_values(self) -> None:
        intent = create_intent()
        intent["group_area"] = None
        intent["target"] = {"qq_number": "123456", "qq_nickname": "小明"}
        intent["violation"]["time"] = "15:30"
        intent["violation"]["action"] = None
        intent["violation"]["handler_admin_nickname"] = None
        text = call_formatter(
            intent, ["group_area", "violation.action"]
        )
        self.assertIn("格式缺少：分区、处理措施", text)
        self.assertIn(
            "<分区：蜂巢/蜂窝/蜂箱> 小明（123456） 15:30 刷屏，<处理措施>",
            text,
        )
        self.assertNotIn("<QQ号>", text)

    def test_reply_time_avoids_time_placeholder(self) -> None:
        intent = create_intent()
        intent["_reply_time"] = "2026-07-31 15:30:00"
        text = call_formatter(intent, ["target.qq_number"])
        self.assertIn("2026-07-31 15:30:00", text)
        self.assertNotIn("<时间，24小时制", text)

    def test_unparseable_reply_time_does_not_fill_missing_time(self) -> None:
        intent = create_intent()
        intent["_reply_time"] = "昨天晚上"
        text = call_formatter(intent, ["violation.time"])
        self.assertIn("<时间，24小时制，如03:30或15:30>", text)
        self.assertNotIn("昨天晚上", text)

    def test_missing_handler_uses_copyable_handler_placeholder(self) -> None:
        intent = create_intent()
        intent["violation"]["time"] = "15:30"
        intent["violation"]["handler_admin_nickname"] = None
        text = call_formatter(
            intent, ["violation.handler_admin_nickname"]
        )
        self.assertIn("<处理人QQ号或昵称>处理", text)

    def test_unclear_handler_replaces_the_untrusted_parsed_name(self) -> None:
        intent = create_intent()
        intent["violation"]["time"] = "15:30"
        text = call_formatter(
            intent, ["violation.handler_admin_nickname"]
        )
        self.assertIn("<处理人QQ号或昵称>处理", text)
        self.assertNotIn("企鹅处理", text)

    def test_flagged_nonempty_values_are_replaced_with_placeholders(self) -> None:
        intent = complete_create_intent()
        intent["group_area"] = "未知分区"
        intent["violation"]["time"] = "昨天晚上"
        intent["violation"]["judgement"] = "可能违规"
        intent["violation"]["action"] = "可能禁言"
        text = call_formatter(
            intent,
            [
                "group_area",
                "violation.time",
                "violation.judgement",
                "violation.action",
            ],
        )
        self.assertIn("<分区：蜂巢/蜂窝/蜂箱>", text)
        self.assertIn("<时间，24小时制，如03:30或15:30>", text)
        self.assertIn("<违规行为>", text)
        self.assertIn("<处理措施>", text)
        self.assertNotIn("未知分区", text)
        self.assertNotIn("昨天晚上", text)
        self.assertNotIn("可能违规", text)
        self.assertNotIn("可能禁言", text)


class CreateCorrectionServiceTests(unittest.TestCase):
    def test_preview_missing_fields_returns_template_without_pending(self) -> None:
        intent = create_intent()
        intent["violation"]["action"] = None
        with (
            patch.object(service, "_operator_or_message") as operator,
            patch.object(service, "_resolve_target_for_read") as read_resolver,
            patch.object(
                service,
                "_resolve_target_for_write",
                return_value=("ambiguous", [MEMBER, {**MEMBER, "id": 3}]),
            ) as write_resolver,
            patch.object(service, "connect") as connect,
            patch.object(service, "_set_pending") as set_pending,
        ):
            text = service.preview_create(
                intent, "123456789", "90001", "记录员", "m1"
            )
        self.assertIn("格式缺少：时间、处理措施", text)
        self.assertIn("<时间，24小时制，如03:30或15:30>", text)
        self.assertIn("<处理措施>", text)
        operator.assert_not_called()
        read_resolver.assert_not_called()
        write_resolver.assert_not_called()
        connect.assert_not_called()
        set_pending.assert_not_called()

    def test_ambiguous_nickname_requires_qq_without_changing_matcher(self) -> None:
        intent = complete_create_intent()
        intent["target"] = {"qq_number": None, "qq_nickname": "小明"}
        with (
            patch.object(service, "_operator_or_message") as operator,
            patch.object(
                service,
                "_resolve_target_for_read",
                return_value=("ambiguous", [MEMBER, {**MEMBER, "id": 3}]),
            ) as read_resolver,
            patch.object(
                service,
                "_resolve_target_for_write",
                return_value=("ambiguous", [MEMBER, {**MEMBER, "id": 3}]),
            ) as write_resolver,
            patch.object(service, "_set_pending") as set_pending,
        ):
            text = service.preview_create(
                intent, "123456789", "90001", "记录员", "m2"
            )
        self.assertIn("格式缺少：QQ号", text)
        self.assertIn("小明（<QQ号>）", text)
        operator.assert_not_called()
        read_resolver.assert_called_once_with(intent)
        write_resolver.assert_not_called()
        set_pending.assert_not_called()

    def test_nlp_ambiguous_action_returns_template_without_writes(self) -> None:
        intent = complete_create_intent()
        intent["violation"]["action"] = "可能禁言"
        intent["operation"]["ambiguous_fields"] = ["violation.action"]
        with (
            patch.object(
                service, "_operator_or_message", return_value=OPERATOR
            ) as operator,
            patch.object(service, "_resolve_target_for_read") as read_resolver,
            patch.object(
                service, "_resolve_target_for_write", return_value=("ok", MEMBER)
            ) as write_resolver,
            patch.object(
                service, "_resolve_handler_admin", return_value=("ok", HANDLER)
            ) as handler_resolver,
            patch.object(
                service, "connect", return_value=nullcontext(MagicMock())
            ) as connect,
            patch.object(
                service, "_state", return_value={"status": "正常", "locked": 0}
            ),
            patch.object(service, "_set_pending") as set_pending,
        ):
            text = service.preview_create(
                intent, "123456789", "90001", "记录员", "m-action"
            )
        self.assertIn("格式缺少：处理措施", text)
        self.assertIn("<处理措施>", text)
        self.assertNotIn("可能禁言", text)
        operator.assert_not_called()
        read_resolver.assert_not_called()
        write_resolver.assert_not_called()
        handler_resolver.assert_not_called()
        connect.assert_not_called()
        set_pending.assert_not_called()

    def test_nlp_ambiguous_handler_returns_placeholder_before_resolution(self) -> None:
        intent = complete_create_intent()
        intent["violation"]["handler_admin_nickname"] = "某管理员"
        intent["operation"]["ambiguous_fields"] = [
            "violation.handler_admin_nickname"
        ]
        with (
            patch.object(
                service, "_operator_or_message", return_value=OPERATOR
            ) as operator,
            patch.object(service, "_resolve_target_for_read") as read_resolver,
            patch.object(
                service, "_resolve_target_for_write", return_value=("ok", MEMBER)
            ) as write_resolver,
            patch.object(
                service, "_resolve_handler_admin", return_value=("ok", HANDLER)
            ) as handler_resolver,
            patch.object(
                service, "connect", return_value=nullcontext(MagicMock())
            ) as connect,
            patch.object(
                service, "_state", return_value={"status": "正常", "locked": 0}
            ),
            patch.object(service, "_set_pending") as set_pending,
        ):
            text = service.preview_create(
                intent, "123456789", "90001", "记录员", "m-handler"
            )
        self.assertIn("格式缺少：处理人", text)
        self.assertIn("<处理人QQ号或昵称>处理", text)
        self.assertNotIn("某管理员处理", text)
        operator.assert_not_called()
        read_resolver.assert_not_called()
        write_resolver.assert_not_called()
        handler_resolver.assert_not_called()
        connect.assert_not_called()
        set_pending.assert_not_called()

    def test_resolved_handler_ambiguity_does_not_create_unknown_member(self) -> None:
        intent = complete_create_intent()
        intent["target"] = {"qq_number": "654321", "qq_nickname": "新成员"}
        intent["violation"]["handler_admin_nickname"] = "小管"
        with (
            patch.object(
                service, "_operator_or_message", return_value=OPERATOR
            ) as operator,
            patch.object(
                service, "_resolve_target_for_read", return_value=("need_member_info", None)
            ),
            patch.object(
                service, "_resolve_target_for_write", return_value=("ok", MEMBER)
            ) as write_resolver,
            patch.object(
                service,
                "_resolve_handler_admin",
                return_value=("ambiguous", [{"nickname": "小管"}]),
            ),
            patch.object(service, "connect") as connect,
            patch.object(service, "_set_pending") as set_pending,
        ):
            text = service.preview_create(
                intent, "123456789", "90001", "记录员", "m-new-handler"
            )
        self.assertIn("格式缺少：处理人", text)
        operator.assert_not_called()
        write_resolver.assert_not_called()
        connect.assert_not_called()
        set_pending.assert_not_called()

    def test_unique_nickname_still_reaches_existing_preview(self) -> None:
        intent = complete_create_intent()
        intent["target"] = {"qq_number": None, "qq_nickname": "小明"}
        with (
            patch.object(
                service,
                "CONFIG",
                replace(service.CONFIG, evidence_required=False),
            ),
            patch.object(
                service, "_resolve_target_for_read", return_value=("ok", MEMBER)
            ),
            patch.object(service, "_operator_or_message", return_value=OPERATOR),
            patch.object(
                service, "_resolve_target_for_write", return_value=("ok", MEMBER)
            ),
            patch.object(
                service, "_resolve_handler_admin", return_value=("ok", HANDLER)
            ),
            patch.object(service, "connect", return_value=nullcontext(MagicMock())),
            patch.object(
                service, "_state", return_value={"status": "正常", "locked": 0}
            ),
            patch.object(service, "_set_pending") as set_pending,
        ):
            text = service.preview_create(
                intent, "123456789", "90001", "记录员", "m3"
            )
        self.assertNotIn("格式缺少", text)
        set_pending.assert_called_once()

    def test_low_confidence_missing_qq_allows_unique_nickname_preview(self) -> None:
        intent = complete_create_intent()
        intent["target"] = {"qq_number": None, "qq_nickname": "小明"}
        intent["operation"]["confidence"] = 0.4
        intent["operation"]["missing_fields"] = ["target.qq_number"]
        with (
            patch.object(
                service,
                "CONFIG",
                replace(service.CONFIG, evidence_required=False),
            ),
            patch.object(
                service, "_resolve_target_for_read", return_value=("ok", MEMBER)
            ) as read_resolver,
            patch.object(service, "_operator_or_message", return_value=OPERATOR),
            patch.object(service, "_resolve_target_for_write") as write_resolver,
            patch.object(
                service, "_resolve_handler_admin", return_value=("ok", HANDLER)
            ),
            patch.object(service, "connect", return_value=nullcontext(MagicMock())),
            patch.object(
                service, "_state", return_value={"status": "正常", "locked": 0}
            ),
            patch.object(service, "_set_pending") as set_pending,
        ):
            text = service.preview_create(
                intent, "123456789", "90001", "记录员", "m-low-confidence"
            )
        self.assertNotIn("格式缺少", text)
        self.assertNotIn("请补充 QQ号", text)
        read_resolver.assert_called_once_with(intent)
        write_resolver.assert_not_called()
        set_pending.assert_called_once()

    def test_complete_unknown_qq_and_nickname_still_creates_preview(self) -> None:
        intent = complete_create_intent()
        intent["target"] = {"qq_number": "654321", "qq_nickname": "新成员"}
        new_member = {"id": 4, "qq_number": "654321", "qq_nickname": "新成员"}
        with (
            patch.object(
                service,
                "CONFIG",
                replace(service.CONFIG, evidence_required=False),
            ),
            patch.object(
                service, "_resolve_target_for_read", return_value=("need_member_info", None)
            ),
            patch.object(service, "_operator_or_message", return_value=OPERATOR),
            patch.object(
                service, "_resolve_target_for_write", return_value=("ok", new_member)
            ) as write_resolver,
            patch.object(
                service, "_resolve_handler_admin", return_value=("ok", HANDLER)
            ),
            patch.object(service, "connect", return_value=nullcontext(MagicMock())),
            patch.object(
                service, "_state", return_value={"status": "正常", "locked": 0}
            ),
            patch.object(service, "_set_pending") as set_pending,
        ):
            text = service.preview_create(
                intent, "123456789", "90001", "记录员", "m-new"
            )
        self.assertNotIn("格式缺少", text)
        write_resolver.assert_called_once_with(intent)
        set_pending.assert_called_once()

    def test_format_failure_falls_back_to_existing_short_message(self) -> None:
        adapter = getattr(service, "_format_create_problem", None)
        self.assertIsNotNone(adapter, "_format_create_problem is missing")
        with (
            patch.object(
                service,
                "format_create_correction",
                side_effect=ValueError("fixture"),
                create=True,
            ),
            patch.object(service.logger, "warning") as warning,
        ):
            text = adapter(create_intent(), ["violation.time"])
        self.assertEqual("缺少必要信息：违规时间。", text)
        warning.assert_called_once_with(
            "新增记录纠正模板降级 stage=create error=ValueError"
        )


class CreateCorrectionRoutingTests(unittest.IsolatedAsyncioTestCase):
    async def test_missing_area_is_formatted_only_for_create_intent(self) -> None:
        create = complete_create_intent()
        create["group_area"] = None
        with patch.object(
            service, "preview_create", return_value="formatted-create"
        ) as preview:
            result = await service.handle_intent(
                create, "123456789", "90001", "记录员", "m4"
            )
        self.assertEqual("formatted-create", result)
        preview.assert_called_once()

        query = {
            "intent": "query_member",
            "group_area": None,
            "target": {"qq_number": "123456", "qq_nickname": None},
        }
        result = await service.handle_intent(
            query, "123456789", "90001", "记录员", "m5"
        )
        self.assertEqual("请标明群聊：蜂巢 / 蜂窝 / 蜂箱。", result)


if __name__ == "__main__":
    unittest.main()
