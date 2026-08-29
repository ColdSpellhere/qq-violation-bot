from __future__ import annotations

import json
import unittest
from dataclasses import replace
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from plugins.llm_gateway.errors import GatewayAuthenticationError
from plugins.violation_record import ai_router


class _Gateway:
    def __init__(self, output: str) -> None:
        self.parse_business_intent = AsyncMock(return_value=output)
        self.generate_chat_reply = AsyncMock(
            side_effect=AssertionError("chat model must not parse business intent")
        )


class _Features:
    def __init__(
        self, *, enabled: bool, economy: bool, business_capable: bool = True
    ) -> None:
        self.enabled = enabled
        self.economy = economy
        self.business_capable = business_capable

    def snapshot(self):
        return SimpleNamespace(
            economy_mode_enabled=self.economy,
            llm_gateway_enabled=self.enabled,
            llm_gateway_business_enabled=self.enabled,
        )

    def llm_gateway_allowed(self, domain: str) -> bool:
        if domain != "business":
            raise AssertionError(domain)
        return self.enabled


class _FixedDateTime(datetime):
    @classmethod
    def now(cls, tz=None):
        value = cls(2026, 8, 27, 12, 34, 56)
        return value if tz is None else value.replace(tzinfo=tz)


def _output(intent: str, **overrides: object) -> str:
    payload: dict[str, object] = {"intent": intent}
    payload.update(overrides)
    return json.dumps(payload, ensure_ascii=False)


class BusinessGatewayMigrationTests(unittest.IsolatedAsyncioTestCase):
    async def test_chat_model_switch_does_not_force_business_into_gateway(
        self,
    ) -> None:
        features = _Features(
            enabled=False,
            economy=True,
            business_capable=False,
        )
        gateway = _Gateway(_output("unknown"))
        legacy = AsyncMock(return_value=_output("unknown"))
        config = replace(
            ai_router.CONFIG,
            ai_api_key="synthetic-primary-key",
            glm_api_key="synthetic-economy-key",
        )

        with (
            patch.object(ai_router, "CONFIG", config),
            patch("plugins.feature_control.runtime.FEATURES", features),
            patch.object(ai_router, "get_gateway", AsyncMock(return_value=gateway)),
            patch.object(ai_router, "_legacy_complete", legacy),
        ):
            await ai_router.parse_intent("请解析这条业务消息")

        legacy.assert_awaited_once()
        gateway.parse_business_intent.assert_not_awaited()

    async def test_gateway_business_stays_on_primary_when_chat_uses_glm(
        self,
    ) -> None:
        features = _Features(enabled=True, economy=True)
        gateway = _Gateway(_output("unknown"))

        async def delayed_gateway():
            features.economy = False
            return gateway

        config = replace(
            ai_router.CONFIG,
            ai_api_key="synthetic-primary-key",
            glm_api_key="synthetic-economy-key",
        )
        with (
            patch.object(ai_router, "CONFIG", config),
            patch("plugins.feature_control.runtime.FEATURES", features),
            patch.object(
                ai_router, "get_gateway", AsyncMock(side_effect=delayed_gateway)
            ),
        ):
            await ai_router.parse_intent("请解析这条业务消息")

        gateway.parse_business_intent.assert_awaited_once()
        self.assertIs(
            False,
            gateway.parse_business_intent.await_args.kwargs["economy_mode"],
        )

    async def test_fixed_business_corpus_is_normalized_identically(self) -> None:
        corpus = (
            ("记录小明刚刚刷屏", _output("create_violation")),
            ("查一下小明的情况", _output("query_member")),
            ("看小明最近两周", _output("query_recent")),
            ("这个分区最近怎么样", _output("query_area_records")),
            ("这种情况怎么处理", _output("consultation")),
            ("给他最后一次警告", _output("final_warning")),
            ("撤回上一条操作", _output("withdraw_latest")),
            ("把他的状态改正常", _output("update_status")),
            ("解除他的锁定", _output("unlock_member")),
            (
                "现在让被艾特的人安静半小时",
                _output(
                    "mute_member",
                    target={"qq_number": None, "qq_nickname": None},
                    moderation={
                        "duration_seconds": 1800,
                        "duration_text": "半小时",
                        "reason": None,
                    },
                ),
            ),
            ("我只是问能不能让他安静，不要真操作", _output("unknown")),
            ("把当前待办确认掉", _output("confirm")),
            ("放弃当前待办吧", _output("cancel")),
            ("告诉我可用操作", _output("help")),
            ("生成一份分区记录文件", _output("export")),
            ("完全无法判断的内容", _output("unknown")),
            (
                "几分钟前似乎有人刷屏",
                _output(
                    "create_violation",
                    violation={"time": "几分钟前"},
                    operation={
                        "confidence": 0.3,
                        "missing_fields": ["violation.time"],
                        "ambiguous_fields": ["violation.time"],
                    },
                ),
            ),
        )
        config = replace(ai_router.CONFIG, ai_api_key="synthetic-test-key")

        for message, output in corpus:
            with self.subTest(message=message), patch.object(
                ai_router, "datetime", _FixedDateTime
            ):
                legacy = AsyncMock(return_value=output)
                with (
                    patch.object(ai_router, "CONFIG", config),
                    patch.object(ai_router, "_gateway_enabled", return_value=False),
                    patch.object(
                        ai_router, "_legacy_complete", legacy, create=True
                    ),
                ):
                    legacy_result = await ai_router.parse_intent(
                        message, referenced_time="2026-08-23 04:00:00"
                    )

                gateway = _Gateway(output)
                with (
                    patch.object(ai_router, "CONFIG", config),
                    patch.object(ai_router, "_gateway_enabled", return_value=True),
                    patch.object(
                        ai_router,
                        "get_gateway",
                        AsyncMock(return_value=gateway),
                        create=True,
                    ),
                    patch.object(
                        ai_router,
                        "_legacy_complete",
                        AsyncMock(side_effect=AssertionError("legacy path used")),
                        create=True,
                    ),
                ):
                    gateway_result = await ai_router.parse_intent(
                        message, referenced_time="2026-08-23 04:00:00"
                    )

                self.assertEqual(legacy_result, gateway_result)
                legacy_messages = legacy.await_args.args[0]
                gateway_messages = gateway.parse_business_intent.await_args.args[0]
                self.assertEqual(legacy_messages, gateway_messages)
                gateway.generate_chat_reply.assert_not_awaited()

    async def test_business_request_contains_only_fixed_business_context(self) -> None:
        config = replace(ai_router.CONFIG, ai_api_key="synthetic-test-key")
        gateway = _Gateway(_output("unknown"))
        with (
            patch.object(ai_router, "CONFIG", config),
            patch.object(ai_router, "_gateway_enabled", return_value=True),
            patch.object(
                ai_router, "get_gateway", AsyncMock(return_value=gateway), create=True
            ),
        ):
            await ai_router.parse_intent("请判断这个业务请求")

        messages = gateway.parse_business_intent.await_args.args[0]
        rendered = json.dumps(messages, ensure_ascii=False)
        self.assertIn("违规记录机器人的意图解析器", rendered)
        self.assertNotIn("character.md", rendered)
        self.assertNotIn("relationship_state", rendered)
        self.assertNotIn("member_memory", rendered)
        self.assertNotIn("image_description", rendered)
        gateway.generate_chat_reply.assert_not_awaited()

    async def test_gateway_error_is_mapped_without_detail(self) -> None:
        config = replace(ai_router.CONFIG, ai_api_key="synthetic-test-key")
        gateway = _Gateway(_output("unknown"))
        gateway.parse_business_intent.side_effect = GatewayAuthenticationError(
            "private credential"
        )
        with (
            patch.object(ai_router, "CONFIG", config),
            patch.object(ai_router, "_gateway_enabled", return_value=True),
            patch.object(
                ai_router, "get_gateway", AsyncMock(return_value=gateway), create=True
            ),
            self.assertRaises(ai_router.AIRouterError) as raised,
        ):
            await ai_router.parse_intent("请解析这条业务消息")

        self.assertNotIn("private credential", str(raised.exception))
        self.assertIn("GatewayAuthenticationError", str(raised.exception))

    async def test_malformed_json_has_same_safe_failure_on_both_paths(self) -> None:
        config = replace(ai_router.CONFIG, ai_api_key="synthetic-test-key")
        for enabled in (False, True):
            gateway = _Gateway("not-json")
            with (
                self.subTest(enabled=enabled),
                patch.object(ai_router, "CONFIG", config),
                patch.object(ai_router, "_gateway_enabled", return_value=enabled),
                patch.object(
                    ai_router, "get_gateway", AsyncMock(return_value=gateway), create=True
                ),
                patch.object(
                    ai_router,
                    "_legacy_complete",
                    AsyncMock(return_value="not-json"),
                    create=True,
                ),
                self.assertRaises(ai_router.AIRouterError) as raised,
            ):
                await ai_router.parse_intent("请解析一个不匹配快捷规则的请求")
            self.assertIn("不是合法 JSON", str(raised.exception))


if __name__ == "__main__":
    unittest.main()
