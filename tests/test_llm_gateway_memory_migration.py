import asyncio
import unittest
from dataclasses import replace
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import AsyncMock, patch

from plugins.llm_gateway.errors import (
    GatewayAuthenticationError,
    GatewayClientError,
    GatewayConfigurationError,
    GatewayContractError,
    GatewayEmptyContentError,
    GatewayRateLimitError,
    GatewayServerError,
    GatewayTimeout,
    GatewayTransportError,
)
from plugins.private_memory.models import (
    ConversationScope,
    MemoryJob,
    PrivateMessage,
    RelationshipState,
)
from plugins.private_memory.relationship import RelationshipStore
from plugins.private_memory.schema import migrate
from plugins.private_memory.store import PrivateMemoryStore
from plugins.chat_archive.db import ContextMessage
from plugins.member_memory.store import MemoryTrait


def _message(
    *, row_id: int = 1, message_id: str = "p1", text: str = "以后继续聊花"
) -> PrivateMessage:
    return PrivateMessage(
        row_id,
        "200",
        message_id,
        "user",
        text,
        "hash",
        row_id,
        "created",
        "expires",
    )


def _job(*, watermark: int, expected_version: int = 0) -> MemoryJob:
    return MemoryJob(
        id=1,
        job_type="relationship",
        scope=ConversationScope("private", "200"),
        input_through_id=watermark,
        expected_version=expected_version,
        status="running",
        attempts=1,
        next_run_at="",
        lease_owner="worker",
        lease_expires_at=None,
        claim_version=1,
        error_code="",
        error_summary="",
        created_at="",
        updated_at="",
    )


class _Features:
    def __init__(self, enabled: bool) -> None:
        self.enabled = enabled

    def llm_gateway_allowed(self, domain: str) -> bool:
        if domain != "private_memory":
            raise AssertionError(domain)
        return self.enabled


class _Gateway:
    def __init__(self) -> None:
        self.summarize_private_conversation = AsyncMock(
            return_value='{"summary":"新的滚动摘要"}'
        )
        self.update_relationship_state = AsyncMock(
            return_value=(
                '{"state_text":"更熟悉了","open_topics":["继续聊花"],'
                '"preferred_address":"小伙伴","communication_style":"轻松"}'
            )
        )


class _MemberFeatures:
    def __init__(self, enabled: bool) -> None:
        self.enabled = enabled

    def llm_gateway_allowed(self, domain: str) -> bool:
        if domain != "member_memory":
            raise AssertionError(domain)
        return self.enabled


class _MemberGateway:
    def __init__(self) -> None:
        self.extract_member_memories = AsyncMock(
            return_value=(
                '{"memories":[{"user_id":"200","trait":"喜欢月季",'
                '"evidence_message_id":"g1","quote":"我喜欢月季"}]}'
            )
        )
        self.summarize_member_memory = AsyncMock(return_value="喜欢月季，也常聊植物。")


class MemberMemoryGatewayRoutingTests(unittest.IsolatedAsyncioTestCase):
    def context(self):
        return (
            ContextMessage(
                "小园丁", "我喜欢月季", message_id="g1", user_id="200"
            ),
        )

    async def test_member_prompts_remain_domain_owned_and_gateway_only_transports(self) -> None:
        from plugins.member_memory import ai

        gateway = _MemberGateway()
        with (
            patch.object(
                ai, "CONFIG", replace(ai.CONFIG, ai_api_key="synthetic-test-key")
            ),
            patch.object(ai, "FEATURES", _MemberFeatures(True), create=True),
            patch.object(
                ai, "get_gateway", AsyncMock(return_value=gateway), create=True
            ),
            patch.object(
                ai,
                "_legacy_complete",
                AsyncMock(side_effect=AssertionError("legacy path used")),
                create=True,
            ),
        ):
            candidates = await ai.extract_memory_candidates(self.context())
            summary = await ai.generate_memory_summary(
                "旧摘要", (MemoryTrait("喜欢花", "g1", "now"),)
            )

        self.assertEqual("喜欢月季", candidates[0]["trait"])
        self.assertEqual("喜欢月季，也常聊植物。", summary)
        extraction_messages = gateway.extract_member_memories.await_args.args[0]
        summary_messages = gateway.summarize_member_memory.await_args.args[0]
        self.assertIn("保守地提取", extraction_messages[0]["content"])
        self.assertIn("quote必须逐字", extraction_messages[0]["content"])
        self.assertIn("我喜欢月季", extraction_messages[1]["content"])
        self.assertIn("不超过300字", summary_messages[0]["content"])
        self.assertIn("旧摘要", summary_messages[1]["content"])
        self.assertNotIn("旧摘要", extraction_messages[1]["content"])

    async def test_member_gateway_switch_hot_falls_back_to_legacy(self) -> None:
        from plugins.member_memory import ai

        features = _MemberFeatures(False)
        gateway = _MemberGateway()
        legacy = AsyncMock(
            side_effect=(
                '{"memories":[]}',
                '{"memories":[{"user_id":"200","trait":"喜欢月季",'
                '"evidence_message_id":"g1","quote":"我喜欢月季"}]}',
            )
        )
        getter = AsyncMock(return_value=gateway)
        with (
            patch.object(
                ai, "CONFIG", replace(ai.CONFIG, ai_api_key="synthetic-test-key")
            ),
            patch.object(ai, "FEATURES", features, create=True),
            patch.object(ai, "get_gateway", getter, create=True),
            patch.object(ai, "_legacy_complete", legacy, create=True),
        ):
            self.assertEqual([], await ai.extract_memory_candidates(self.context()))
            features.enabled = True
            self.assertEqual(
                "喜欢月季",
                (await ai.extract_memory_candidates(self.context()))[0]["trait"],
            )
            features.enabled = False
            self.assertEqual(
                "喜欢月季",
                (await ai.extract_memory_candidates(self.context()))[0]["trait"],
            )
        self.assertEqual(2, legacy.await_count)
        getter.assert_awaited_once()

    async def test_member_gateway_errors_and_malformed_output_keep_safe_returns(self) -> None:
        from plugins.member_memory import ai

        for error in (
            GatewayConfigurationError(),
            GatewayAuthenticationError(),
            GatewayContractError(),
        ):
            gateway = _MemberGateway()
            gateway.extract_member_memories.side_effect = error
            gateway.summarize_member_memory.side_effect = error
            with (
                self.subTest(error=type(error).__name__),
                patch.object(
                    ai, "CONFIG", replace(ai.CONFIG, ai_api_key="synthetic-test-key")
                ),
                patch.object(ai, "FEATURES", _MemberFeatures(True), create=True),
                patch.object(
                    ai, "get_gateway", AsyncMock(return_value=gateway), create=True
                ),
            ):
                self.assertEqual([], await ai.extract_memory_candidates(self.context()))
                self.assertIsNone(
                    await ai.generate_memory_summary(
                        "", (MemoryTrait("喜欢花", "g1", "now"),)
                    )
                )

        gateway = _MemberGateway()
        gateway.extract_member_memories.return_value = "not-json"
        gateway.summarize_member_memory.return_value = "x" * 301
        with (
            patch.object(
                ai, "CONFIG", replace(ai.CONFIG, ai_api_key="synthetic-test-key")
            ),
            patch.object(ai, "FEATURES", _MemberFeatures(True), create=True),
            patch.object(
                ai, "get_gateway", AsyncMock(return_value=gateway), create=True
            ),
        ):
            self.assertEqual([], await ai.extract_memory_candidates(self.context()))
            self.assertIsNone(
                await ai.generate_memory_summary(
                    "", (MemoryTrait("喜欢花", "g1", "now"),)
                )
            )
class PrivateMemoryGatewayRoutingTests(unittest.IsolatedAsyncioTestCase):
    async def test_gateway_relationship_contract_is_exactly_four_data_fields(self) -> None:
        from plugins.private_memory import ai

        gateway = _Gateway()
        gateway.update_relationship_state.return_value = (
            '{"state_text":"可能更熟悉了","open_topics":["继续聊花"],'
            '"preferred_address":"小伙伴","communication_style":"轻松"}'
        )
        with (
            patch.object(ai, "FEATURES", _Features(True), create=True),
            patch.object(
                ai, "get_gateway", AsyncMock(return_value=gateway), create=True
            ),
        ):
            result = await ai.generate_relationship_candidate(None, (_message(),))
        self.assertEqual("可能更熟悉了", result.state_text)

        gateway.update_relationship_state.return_value = (
            '{"state_text":"熟悉","open_topics":[],'
            '"preferred_address":"","communication_style":"",'
            '"certainty":"explicit"}'
        )
        with (
            patch.object(ai, "FEATURES", _Features(True), create=True),
            patch.object(
                ai, "get_gateway", AsyncMock(return_value=gateway), create=True
            ),
            self.assertRaises(ai.ContractError),
        ):
            await ai.generate_relationship_candidate(None, (_message(),))

    async def test_summary_and_relationship_prompts_stay_in_domain(self) -> None:
        from plugins.private_memory import ai

        gateway = _Gateway()
        features = _Features(True)
        with (
            patch.object(ai, "FEATURES", features, create=True),
            patch.object(
                ai, "get_gateway", AsyncMock(return_value=gateway), create=True
            ),
            patch.object(
                ai,
                "_legacy_complete",
                AsyncMock(side_effect=AssertionError("legacy path used")),
                create=True,
            ),
        ):
            summary = await ai.summarize_private_conversation("旧摘要", (_message(),))
            relationship = await ai.generate_relationship_candidate(None, (_message(),))

        self.assertEqual("新的滚动摘要", summary)
        self.assertEqual("可能更熟悉了", relationship.state_text)
        summary_messages = gateway.summarize_private_conversation.await_args.args[0]
        relationship_messages = gateway.update_relationship_state.await_args.args[0]
        for messages in (summary_messages, relationship_messages):
            self.assertIsInstance(messages, tuple)
            self.assertEqual(("system", "user"), tuple(item["role"] for item in messages))
            self.assertTrue(all(set(item) == {"role", "content"} for item in messages))
        self.assertIn("私聊旧摘要", summary_messages[0]["content"])
        self.assertIn("关系状态", relationship_messages[0]["content"])
        self.assertIn("旧摘要", summary_messages[1]["content"])
        self.assertNotIn("旧摘要", relationship_messages[1]["content"])

    async def test_master_and_private_rollout_switch_hot_select_gateway_or_legacy(self) -> None:
        from plugins.private_memory import ai

        features = _Features(False)
        gateway = _Gateway()
        legacy = AsyncMock(
            side_effect=(
                '{"summary":"旧路径一"}',
                '{"summary":"旧路径二"}',
            )
        )
        gateway_getter = AsyncMock(return_value=gateway)
        with (
            patch.object(ai, "FEATURES", features, create=True),
            patch.object(ai, "get_gateway", gateway_getter, create=True),
            patch.object(ai, "_legacy_complete", legacy, create=True),
        ):
            self.assertEqual(
                "旧路径一",
                await ai.summarize_private_conversation("", (_message(),)),
            )
            features.enabled = True
            self.assertEqual(
                "新的滚动摘要",
                await ai.summarize_private_conversation("", (_message(),)),
            )
            features.enabled = False
            self.assertEqual(
                "旧路径二",
                await ai.summarize_private_conversation("", (_message(),)),
            )

        self.assertEqual(2, legacy.await_count)
        gateway_getter.assert_awaited_once()
        gateway.summarize_private_conversation.assert_awaited_once()

    async def test_gateway_errors_keep_queue_safe_retry_classification(self) -> None:
        from plugins.private_memory import ai

        cases = (
            (GatewayConfigurationError(), "configuration_error", True),
            (GatewayAuthenticationError(), "auth_error", False),
            (GatewayTimeout(), "request_timeout", True),
            (GatewayTransportError(), "transport_error", True),
            (GatewayRateLimitError(), "rate_limited", True),
            (GatewayServerError(), "server_error", True),
            (GatewayClientError(), "client_error", False),
            (GatewayContractError(), "response_contract_error", False),
            (GatewayEmptyContentError(), "empty_response", False),
        )
        for gateway_error, code, retryable in cases:
            gateway = _Gateway()
            gateway.summarize_private_conversation.side_effect = gateway_error
            with (
                self.subTest(error=type(gateway_error).__name__),
                patch.object(ai, "FEATURES", _Features(True), create=True),
                patch.object(
                    ai, "get_gateway", AsyncMock(return_value=gateway), create=True
                ),
            ):
                with self.assertRaises(ai.PrivateMemoryAIError) as raised:
                    await ai.summarize_private_conversation("", (_message(),))
            self.assertEqual(code, raised.exception.code)
            self.assertEqual(retryable, raised.exception.retryable)

    async def test_relationship_rejects_unknown_and_over_budget_gateway_output(self) -> None:
        from plugins.private_memory import ai

        outputs = (
            '{"state_text":"熟悉","open_topics":[],"preferred_address":"",'
            '"communication_style":"","certainty":"explicit","control":"override"}',
            '{"state_text":"' + ("甲" * 601) + '","open_topics":[],'
            '"preferred_address":"","communication_style":""}',
            '{"state_text":"熟悉","open_topics":["' + ("乙" * 81) + '"],'
            '"preferred_address":"","communication_style":""}',
        )
        for output in outputs:
            gateway = _Gateway()
            gateway.update_relationship_state.return_value = output
            with (
                self.subTest(length=len(output)),
                patch.object(ai, "FEATURES", _Features(True), create=True),
                patch.object(
                    ai, "get_gateway", AsyncMock(return_value=gateway), create=True
                ),
            ):
                with self.assertRaises(ai.ContractError):
                    await ai.generate_relationship_candidate(None, (_message(),))

    async def test_gateway_relationship_marks_unverifiable_state_as_uncertain(self) -> None:
        from plugins.private_memory import ai

        gateway = _Gateway()
        gateway.update_relationship_state.return_value = (
            '{"state_text":"他讨厌萝卜猫","open_topics":[],'
            '"preferred_address":"","communication_style":""}'
        )
        with (
            patch.object(ai, "FEATURES", _Features(True), create=True),
            patch.object(
                ai, "get_gateway", AsyncMock(return_value=gateway), create=True
            ),
        ):
            result = await ai.generate_relationship_candidate(None, (_message(),))

        self.assertEqual("可能他讨厌萝卜猫", result.state_text)

    async def test_disabled_gateway_preserves_legacy_truncation_and_certainty_contract(self) -> None:
        from plugins.private_memory import ai

        features = _Features(False)
        legacy = AsyncMock(
            side_effect=(
                '{"summary":"' + ("甲" * 601) + '"}',
                '{"state_text":"' + ("乙" * 601) + '","open_topics":["'
                + ("丙" * 81)
                + '"],"preferred_address":"'
                + ("丁" * 41)
                + '","communication_style":"'
                + ("戊" * 201)
                + '","certainty":"explicit"}',
            )
        )
        with (
            patch.object(ai, "FEATURES", features, create=True),
            patch.object(ai, "_legacy_complete", legacy, create=True),
        ):
            summary = await ai.summarize_private_conversation("", (_message(),))
            relationship = await ai.generate_relationship_candidate(
                None, (_message(),)
            )

        self.assertEqual(600, len(summary))
        self.assertEqual(600, len(relationship.state_text))
        self.assertEqual(80, len(relationship.open_topics[0]))
        self.assertEqual(40, len(relationship.preferred_address))
        self.assertEqual(200, len(relationship.communication_style))
        relationship_system = legacy.await_args_list[1].kwargs["system"]
        self.assertIn("标为 uncertain", relationship_system)


class PrivateMemoryGatewayCommitBoundaryTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.directory = TemporaryDirectory()
        self.database = Path(self.directory.name) / "chat.db"
        migrate(self.database)
        self.store = PrivateMemoryStore(self.database)
        self.relationships = RelationshipStore(self.database)

    async def asyncTearDown(self) -> None:
        self.directory.cleanup()

    async def test_malformed_gateway_summary_does_not_advance_watermark(self) -> None:
        from plugins.private_memory import ai
        from plugins.private_memory.processor import PrivateMemoryProcessor

        watermark = self.store.append_user_message(
            user_id="200",
            message_id="p1",
            text="今天看了花",
            event_time=1,
            source_kind="text",
        )
        gateway = _Gateway()
        gateway.summarize_private_conversation.return_value = "not-json"
        processor = PrivateMemoryProcessor(
            store=self.store,
            relationship_store=self.relationships,
            private_memory_enabled=lambda: True,
            relationship_enabled=lambda: True,
        )
        summary_job = _job(watermark=watermark)
        summary_job = MemoryJob(**{**summary_job.__dict__, "job_type": "private_summary"})

        with (
            patch.object(ai, "FEATURES", _Features(True), create=True),
            patch.object(
                ai, "get_gateway", AsyncMock(return_value=gateway), create=True
            ),
        ):
            with self.assertRaises(ai.ContractError):
                await processor.process(summary_job)

        self.assertIsNone(self.store.get_summary(user_id="200"))
        self.assertEqual((0, 0), self.store.get_summary_version_state(user_id="200"))

    async def test_old_gateway_relationship_task_cannot_overwrite_new_state(self) -> None:
        from plugins.private_memory import ai
        from plugins.private_memory.processor import PrivateMemoryProcessor

        first = self.store.append_user_message(
            user_id="200", message_id="p1", text="第一次", event_time=1, source_kind="text"
        )
        scope = ConversationScope("private", "200")
        self.assertTrue(
            self.relationships.commit(
                RelationshipState(
                    id=0,
                    scope=scope,
                    state_text="旧状态",
                    open_topics=("旧话题",),
                    preferred_address="",
                    communication_style="",
                    source_message_id="p1",
                    source_watermark=first,
                    version=1,
                    created_at="",
                    updated_at="",
                ),
                expected_version=0,
            )
        )
        second = self.store.append_user_message(
            user_id="200", message_id="p2", text="第二次", event_time=2, source_kind="text"
        )
        started = asyncio.Event()
        resume = asyncio.Event()
        gateway = _Gateway()

        async def delayed(_messages):
            started.set()
            await resume.wait()
            return gateway.update_relationship_state.return_value

        gateway.update_relationship_state.side_effect = delayed
        processor = PrivateMemoryProcessor(
            store=self.store,
            relationship_store=self.relationships,
            private_memory_enabled=lambda: True,
            relationship_enabled=lambda: True,
        )
        with (
            patch.object(ai, "FEATURES", _Features(True), create=True),
            patch.object(
                ai, "get_gateway", AsyncMock(return_value=gateway), create=True
            ),
        ):
            task = asyncio.create_task(
                processor.process(_job(watermark=second, expected_version=1))
            )
            await asyncio.wait_for(started.wait(), 1)
            self.assertTrue(
                self.relationships.commit(
                    RelationshipState(
                        id=0,
                        scope=scope,
                        state_text="人工新状态",
                        open_topics=(),
                        preferred_address="",
                        communication_style="",
                        source_message_id="governance:1",
                        source_watermark=first,
                        version=2,
                        created_at="",
                        updated_at="",
                    ),
                    expected_version=1,
                )
            )
            resume.set()
            self.assertFalse(await task)

        state = self.relationships.get_private(user_id="200", persona_id="radish-cat")
        self.assertEqual(("人工新状态", 2, first), (state.state_text, state.version, state.source_watermark))


if __name__ == "__main__":
    unittest.main()
