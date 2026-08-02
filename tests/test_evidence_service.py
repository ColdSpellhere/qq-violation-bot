from __future__ import annotations

import unittest
from contextlib import nullcontext
from dataclasses import replace
from unittest.mock import MagicMock, patch

from plugins.violation_record import service


OPERATOR = {"id": 1, "qq_number": "90001", "nickname": "记录员"}
HANDLER = {"id": 1, "qq_number": "90001", "nickname": "记录员"}
MEMBER = {"id": 2, "qq_number": "123456", "qq_nickname": "小明"}


def valid_create_intent(batch_id: str | None = None, count: int = 0) -> dict:
    return {
        "intent": "create_violation",
        "group_area": "蜂巢",
        "target": {"qq_number": "123456", "qq_nickname": "小明"},
        "violation": {
            "time": "2026-07-28 10:00:00",
            "judgement": "刷屏",
            "action": "禁言10分钟",
            "handler_admin_qq": "90001",
            "handler_admin_nickname": "记录员",
            "remark": None,
        },
        "operation": {"confidence": 1.0, "missing_fields": [], "ambiguous_fields": []},
        "_evidence_batch_id": batch_id,
        "_evidence_count": count,
    }


class EvidenceServiceTests(unittest.TestCase):
    def _preview(self, required: bool, batch_id: str | None = None, count: int = 0):
        with (
            patch.object(service, "CONFIG", replace(service.CONFIG, evidence_required=required)),
            patch.object(service, "_operator_or_message", return_value=OPERATOR),
            patch.object(service, "_resolve_target_for_read", return_value=("ok", MEMBER)),
            patch.object(service, "_resolve_handler_admin", return_value=("ok", HANDLER)),
            patch.object(service, "connect", return_value=nullcontext(MagicMock())),
            patch.object(service, "_state", return_value={"status": "正常", "locked": 0}),
            patch.object(service, "_set_pending") as set_pending,
        ):
            text = service.preview_create(
                valid_create_intent(batch_id, count),
                "123456789",
                "90001",
                "记录员",
                "m1",
            )
        return text, set_pending

    def test_soft_mode_allows_missing_evidence_and_adds_reminder(self) -> None:
        text, set_pending = self._preview(False)
        self.assertIn("未引用证据图片", text)
        set_pending.assert_called_once()

    def test_hard_mode_rejects_missing_evidence_before_pending(self) -> None:
        text, set_pending = self._preview(True)
        self.assertEqual("请引用至少一张证据图片后重新记录。", text)
        set_pending.assert_not_called()

    def test_binding_failure_keeps_confirmation_success_and_queues_retry(self) -> None:
        store = MagicMock()
        store.bind_batch.side_effect = OSError("fixture failure")
        inserted = service.InsertedViolation(
            detail="小明（123456）\n\n时间：2026-07-28 10:00",
            violation_id=42,
            target_qq="123456",
        )
        with (
            patch.object(
                service,
                "CONFIG",
                replace(service.CONFIG, deduction_policy_v102_enabled=False),
            ),
            patch.object(
                service,
                "_pop_pending",
                return_value=("create_violation", {"record": {}, "evidence_batch_id": "batch-1"}),
            ),
            patch.object(service, "_operator_or_message", return_value=OPERATOR),
            patch.object(service, "connect", return_value=nullcontext(MagicMock())),
            patch.object(service, "_insert_violation", return_value=inserted),
            patch.object(service, "EvidenceStore", return_value=store),
        ):
            text = service.confirm_pending("123456789", "90001", "记录员", "m2")
        self.assertIn("已记录。", text)
        store.queue_binding.assert_called_once_with("batch-1", 42, "123456")

    def test_expired_create_marks_evidence_batch_expired(self) -> None:
        store = MagicMock()
        with (
            patch.object(
                service,
                "_pop_pending",
                return_value=("expired", {"evidence_batch_id": "batch-expired"}),
            ),
            patch.object(service, "EvidenceStore", return_value=store),
        ):
            text = service.confirm_pending("123456789", "90001", "记录员", "m3")
        self.assertEqual("待确认操作已过期，请重新发起。", text)
        store.mark_batch.assert_called_once_with("batch-expired", "expired")

    def test_cancelled_create_marks_evidence_batch_cancelled(self) -> None:
        store = MagicMock()
        with (
            patch.object(
                service,
                "_pop_pending",
                return_value=("create_violation", {"evidence_batch_id": "batch-cancelled"}),
            ),
            patch.object(service, "EvidenceStore", return_value=store),
        ):
            text = service.cancel_pending("123456789", "90001")
        self.assertEqual("已取消。", text)
        store.mark_batch.assert_called_once_with("batch-cancelled", "cancelled")


if __name__ == "__main__":
    unittest.main()
