from __future__ import annotations

import unittest

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

    def test_missing_handler_uses_copyable_handler_placeholder(self) -> None:
        intent = create_intent()
        intent["violation"]["time"] = "15:30"
        intent["violation"]["handler_admin_nickname"] = None
        text = call_formatter(
            intent, ["violation.handler_admin_nickname"]
        )
        self.assertIn("<处理人QQ号或昵称>处理", text)


if __name__ == "__main__":
    unittest.main()
