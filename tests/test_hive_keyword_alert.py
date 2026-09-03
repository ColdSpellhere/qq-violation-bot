from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
import tempfile
import threading
import unittest
from collections.abc import Callable
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import nonebot
from nonebot.adapters.onebot.v11 import (
    GroupMessageEvent,
    Message,
    MessageSegment,
    PrivateMessageEvent,
)

PROJECT_ROOT = Path(__file__).resolve().parents[1]
SOURCE_GROUP_ID = 900_000_000_000_510_001
REPORT_GROUP_ID = 900_000_000_000_510_002
BUSINESS_GROUP_ID = 900_000_000_000_510_003
BOT_USER_ID = 900_000_000_000_510_004
MEMBER_USER_ID = 900_000_000_000_510_015

os.environ.setdefault("TARGET_GROUP_ID", str(BUSINESS_GROUP_ID))
try:
    nonebot.get_driver()
except ValueError:
    nonebot.init()


def _group_event(
    text: str,
    *,
    group_id: int = SOURCE_GROUP_ID,
    user_id: int = MEMBER_USER_ID,
    self_id: int = BOT_USER_ID,
    event_time: int = 2_000,
    addressed: bool = False,
    include_image: bool = False,
    message_id: int = 456,
    nickname: str = "测试成员",
) -> GroupMessageEvent:
    segments: list[MessageSegment] = []
    if addressed:
        segments.append(MessageSegment.at(self_id))
    if include_image:
        segments.append(MessageSegment.image("https://example.invalid/image.jpg"))
    if text:
        segments.append(MessageSegment.text(text))
    message = Message(segments)
    return GroupMessageEvent(
        time=event_time,
        self_id=self_id,
        post_type="message",
        sub_type="normal",
        user_id=user_id,
        message_type="group",
        message_id=message_id,
        group_id=group_id,
        message=message,
        original_message=message,
        raw_message=str(message),
        font=0,
        sender={
            "user_id": user_id,
            "nickname": nickname,
            "card": "",
            "role": "member",
        },
    )


def _private_event(text: str, *, user_id: int = MEMBER_USER_ID) -> PrivateMessageEvent:
    message = Message(MessageSegment.text(text))
    return PrivateMessageEvent(
        time=2_000,
        self_id=BOT_USER_ID,
        post_type="message",
        sub_type="friend",
        user_id=user_id,
        message_type="private",
        message_id=789,
        message=message,
        original_message=message,
        raw_message=text,
        font=0,
        sender={"user_id": user_id, "nickname": "测试成员"},
    )


class ContentAlertConfigTests(unittest.TestCase):
    def _probe(
        self,
        *,
        instance_root: Path,
        monitor_only: str,
    ) -> subprocess.CompletedProcess[str]:
        environment = os.environ.copy()
        environment.update(
            {
                "BOT_INSTANCE_ROOT": str(instance_root),
                "BOT_MODE": "full",
                "TARGET_GROUP_ID": str(BUSINESS_GROUP_ID),
                "CONTENT_ALERT_ENABLED": "true",
                "CONTENT_ALERT_SOURCE_GROUP_IDS": str(SOURCE_GROUP_ID),
                "CONTENT_ALERT_REPORT_GROUP_ID": str(REPORT_GROUP_ID),
                "CONTENT_ALERT_BACKGROUND_RULES_PATH": str(
                    instance_root / "env-must-not-control-background-rules.json"
                ),
                "CONTENT_ALERT_MANAGED_CATALOG_PATH": str(
                    instance_root / "env-must-not-control-managed-catalog.json"
                ),
                "MONITOR_ONLY_GROUP_IDS": monitor_only,
                "PYTHONPATH": str(PROJECT_ROOT),
            }
        )
        code = """
import json
from plugins.violation_record.config import CONFIG

print(json.dumps({
    "enabled": CONFIG.content_alert_enabled,
    "ids": CONFIG.content_alert_source_group_ids,
    "report": CONFIG.content_alert_report_group_id,
    "capable": CONFIG.content_alert_capable,
    "rule_path": str(CONFIG.content_alert_rules_path),
    "background_rule_path": str(CONFIG.content_alert_background_rules_path),
    "managed_catalog_path": str(CONFIG.content_alert_managed_catalog_path),
}))
"""
        return subprocess.run(
            [sys.executable, "-B", "-c", code],
            cwd=PROJECT_ROOT,
            env=environment,
            text=True,
            capture_output=True,
            check=False,
        )

    def test_complete_private_config_is_capable_and_instance_scoped(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "carrot"
            completed = self._probe(
                instance_root=root,
                monitor_only=str(SOURCE_GROUP_ID),
            )

        self.assertEqual(0, completed.returncode, completed.stderr)
        payload = json.loads(completed.stdout.strip().splitlines()[-1])
        self.assertTrue(payload["enabled"])
        self.assertEqual([SOURCE_GROUP_ID], payload["ids"])
        self.assertEqual(REPORT_GROUP_ID, payload["report"])
        self.assertTrue(payload["capable"])
        self.assertEqual(
            str(root / "data" / "content_alert" / "keywords.json"),
            payload["rule_path"],
        )
        self.assertEqual(
            str(root / "data" / "content_alert" / "background_keywords.json"),
            payload["background_rule_path"],
        )
        self.assertEqual(
            str(root / "data" / "content_alert" / "managed" / "current.json"),
            payload["managed_catalog_path"],
        )

    def test_enabled_source_group_must_be_monitor_only(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            completed = self._probe(
                instance_root=Path(directory),
                monitor_only="",
            )

        self.assertNotEqual(0, completed.returncode)
        self.assertIn(
            "keyword alert source groups must be monitor-only",
            completed.stdout + completed.stderr,
        )


class LiteralKeywordEngineTests(unittest.TestCase):
    def test_scalable_matcher_enforces_trie_node_budget(self) -> None:
        from plugins.content_alert.engine import ScalableLiteralMatcher

        with self.assertRaisesRegex(ValueError, "trie node limit"):
            ScalableLiteralMatcher(
                (("first", "abcd"),),
                max_patterns=1,
                max_nodes=4,
            )

        matcher = ScalableLiteralMatcher(
            (("first", "abc"),),
            max_patterns=1,
            max_nodes=4,
        )
        self.assertEqual(
            ("first",),
            tuple(item.key for item in matcher.match_text("abc")),
        )

    def test_scalable_matcher_singleton_groups_do_not_allocate_dense_owner_maps(
        self,
    ) -> None:
        import tracemalloc

        from plugins.content_alert.engine import ScalableLiteralMatcher

        patterns = tuple((f"key-{index}", f"p{index:04d}") for index in range(500))
        matcher = ScalableLiteralMatcher(
            patterns,
            max_patterns=len(patterns),
            overlap_groups={key: f"group-{key}" for key, _pattern in patterns},
        )
        text = "|".join(pattern for _key, pattern in patterns)

        tracemalloc.start()
        try:
            matches = matcher.match_text_all(text)
            _current, peak = tracemalloc.get_traced_memory()
        finally:
            tracemalloc.stop()

        self.assertEqual(len(patterns), len(matches))
        self.assertLess(peak, 4_000_000)

    def test_scalable_matcher_nested_patterns_keep_exact_longest_semantics(
        self,
    ) -> None:
        from plugins.content_alert.engine import ScalableLiteralMatcher

        patterns = tuple((f"key-{length:02d}", "a" * length) for length in range(1, 64))
        matcher = ScalableLiteralMatcher(
            patterns,
            max_patterns=len(patterns),
            overlap_groups={key: "same-group" for key, _pattern in patterns},
        )

        matches = matcher.match_text_all("a" * 3_000)

        self.assertEqual(3_000 - 63 + 1, len(matches))
        self.assertEqual({"key-63"}, {match.key for match in matches})
        self.assertEqual((0, 63), (matches[0].start, matches[0].end))
        self.assertEqual((2_937, 3_000), (matches[-1].start, matches[-1].end))

    def test_scalable_matcher_fails_closed_at_text_and_candidate_budgets(self) -> None:
        from plugins.content_alert.engine import (
            ScalableLiteralMatcher,
            ScalableLiteralScanLimitError,
        )

        text_limited = ScalableLiteralMatcher(
            (("key", "safe"),),
            max_patterns=1,
            max_text_chars=8,
        )
        with self.assertRaises(ScalableLiteralScanLimitError):
            text_limited.match_text_all("x" * 9)

        patterns = tuple((f"key-{length}", "a" * length) for length in range(1, 9))
        candidate_limited = ScalableLiteralMatcher(
            patterns,
            max_patterns=len(patterns),
            max_candidates=20,
            overlap_groups={key: f"group-{key}" for key, _term in patterns},
        )
        with self.assertRaises(ScalableLiteralScanLimitError):
            candidate_limited.match_text_all("a" * 20)

    def test_nfkc_casefold_whitespace_and_zero_width_are_normalized(self) -> None:
        from plugins.content_alert.engine import LiteralKeywordMatcher
        from plugins.content_alert.rules import KeywordRule

        matcher = LiteralKeywordMatcher(
            (KeywordRule(rule_id="K0001", pattern="Ａb 禁 词"),)
        )

        matches = matcher.match_text("前缀 aB\u200b禁\t词 后缀")

        self.assertEqual(["K0001"], [item.rule_id for item in matches])

    def test_default_ignorable_cgj_cannot_block_canonical_composition(self) -> None:
        from plugins.content_alert.engine import LiteralKeywordMatcher
        from plugins.content_alert.rules import KeywordRule

        matcher = LiteralKeywordMatcher((KeywordRule(rule_id="K0001", pattern="éx"),))

        matches = matcher.match_text("前缀 e\u034f\u0301x 后缀")

        self.assertEqual(["K0001"], [item.rule_id for item in matches])

    def test_private_use_character_is_not_removed_or_joined_for_matching(self) -> None:
        from plugins.content_alert.engine import LiteralKeywordMatcher
        from plugins.content_alert.rules import KeywordRule

        matcher = LiteralKeywordMatcher((KeywordRule(rule_id="K0001", pattern="违禁"),))

        self.assertEqual((), matcher.match_text("违\ue000禁"))

    def test_longest_overlapping_keyword_wins_and_distinct_hits_merge(self) -> None:
        from plugins.content_alert.engine import LiteralKeywordMatcher
        from plugins.content_alert.rules import KeywordRule

        matcher = LiteralKeywordMatcher(
            (
                KeywordRule(rule_id="K0001", pattern="交易"),
                KeywordRule(rule_id="K0002", pattern="账号交易"),
                KeywordRule(rule_id="K0003", pattern="另一个词"),
            )
        )

        matches = matcher.match_text("有人说账号交易，也说另一个词")

        self.assertEqual(
            ["K0002", "K0003"],
            [item.rule_id for item in matches],
        )

    def test_only_text_segments_are_scanned_without_crossing_boundaries(self) -> None:
        from plugins.content_alert.engine import (
            LiteralKeywordMatcher,
            match_message_text_segments,
        )
        from plugins.content_alert.rules import KeywordRule

        matcher = LiteralKeywordMatcher((KeywordRule(rule_id="K0001", pattern="违禁"),))
        split_message = Message(
            (
                MessageSegment.text("违"),
                MessageSegment.image("https://example.invalid/image.jpg"),
                MessageSegment.text("禁"),
            )
        )
        direct_message = Message(
            (
                MessageSegment.at(123),
                MessageSegment.text("这里有违禁内容"),
            )
        )

        self.assertEqual((), match_message_text_segments(split_message, matcher))
        self.assertEqual(
            ("K0001",),
            tuple(
                item.rule_id
                for item in match_message_text_segments(direct_message, matcher)
            ),
        )

    def test_regex_metacharacters_remain_literal(self) -> None:
        from plugins.content_alert.engine import LiteralKeywordMatcher
        from plugins.content_alert.rules import KeywordRule

        matcher = LiteralKeywordMatcher((KeywordRule(rule_id="K0001", pattern="a.*b"),))

        self.assertEqual((), matcher.match_text("axxxb"))
        self.assertEqual(
            ("K0001",),
            tuple(item.rule_id for item in matcher.match_text("a.*b")),
        )


class KeywordRuleStoreTests(unittest.TestCase):
    def test_add_list_delete_are_atomic_bounded_and_instance_private(self) -> None:
        from plugins.content_alert.rules import KeywordRuleStore

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "data" / "content_alert" / "keywords.json"
            store = KeywordRuleStore(path)

            self.assertEqual((), store.snapshot())
            first = store.add("  测试违禁词  ", actor="42")
            second = store.add("另一个词", actor="42")

            self.assertEqual("K0001", first.rule_id)
            self.assertEqual("测试违禁词", first.pattern)
            self.assertEqual("K0002", second.rule_id)
            self.assertEqual(
                ["K0001", "K0002"],
                [item.rule_id for item in store.snapshot()],
            )
            payload = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(1, payload["version"])
            self.assertEqual(2, payload["revision"])
            self.assertEqual("42", payload["updated_by"])
            self.assertEqual(0o700, path.parent.stat().st_mode & 0o777)
            self.assertEqual(0o600, path.stat().st_mode & 0o777)
            self.assertTrue(path.with_name("keywords.json.bak").is_file())

            removed = store.remove("K0001", actor="43")

            self.assertEqual(first, removed)
            self.assertEqual(["K0002"], [item.rule_id for item in store.snapshot()])

    def test_normalized_duplicates_and_invalid_patterns_are_rejected(self) -> None:
        from plugins.content_alert.rules import KeywordRuleStore

        with tempfile.TemporaryDirectory() as directory:
            store = KeywordRuleStore(Path(directory) / "keywords.json")
            store.add("ＡＢ 禁 词", actor="1")

            with self.assertRaisesRegex(ValueError, "already exists"):
                store.add("ab\u200b禁词", actor="1")
            for pattern in ("a", "\x00坏词", "x" * 65):
                with (
                    self.subTest(pattern=repr(pattern)),
                    self.assertRaises(ValueError),
                ):
                    store.add(pattern, actor="1")
            with self.assertRaisesRegex(KeyError, "K9999"):
                store.remove("K9999", actor="1")

    def test_invalid_external_replacement_keeps_last_known_good_rules(self) -> None:
        from plugins.content_alert.rules import KeywordRuleStore

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "keywords.json"
            store = KeywordRuleStore(path)
            store.add("有效词", actor="1")
            store.add("第二个有效词", actor="1")
            path.write_text("{invalid", encoding="utf-8")

            rules = store.snapshot()
            store.add("第三个有效词", actor="2")

            self.assertEqual(
                ["有效词", "第二个有效词"],
                [item.pattern for item in rules],
            )
            backup = json.loads(
                path.with_name("keywords.json.bak").read_text(encoding="utf-8")
            )
            self.assertEqual(
                ["有效词", "第二个有效词"],
                [item["pattern"] for item in backup["rules"]],
            )

    @unittest.skipUnless(hasattr(os, "symlink"), "symlink is unavailable")
    def test_symbolic_link_target_is_rejected(self) -> None:
        from plugins.content_alert.rules import KeywordRuleStore

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            real = root / "real"
            real.mkdir()
            linked = root / "linked"
            os.symlink(real, linked)
            store = KeywordRuleStore(linked / "keywords.json")

            with self.assertRaisesRegex(OSError, "symbolic link"):
                store.add("测试词", actor="1")

    @unittest.skipUnless(hasattr(os, "symlink"), "symlink is unavailable")
    def test_symbolic_link_in_data_ancestor_is_rejected(self) -> None:
        from plugins.content_alert.rules import KeywordRuleStore

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            instance_root = root / "instance"
            instance_root.mkdir()
            real_data = root / "real-data"
            (real_data / "content_alert").mkdir(parents=True)
            os.symlink(real_data, instance_root / "data")
            escaped_path = real_data / "content_alert" / "keywords.json"
            store = KeywordRuleStore(
                instance_root / "data" / "content_alert" / "keywords.json"
            )

            with self.assertRaisesRegex(OSError, "symbolic link"):
                store.add("测试词", actor="1")
            self.assertFalse(escaped_path.exists())


class KeywordCommandTests(unittest.TestCase):
    def test_superuser_command_service_adds_lists_and_deletes_by_rule_id(self) -> None:
        from plugins.content_alert.commands import execute_keyword_command
        from plugins.content_alert.rules import KeywordRuleStore

        with tempfile.TemporaryDirectory() as directory:
            store = KeywordRuleStore(Path(directory) / "keywords.json")

            self.assertEqual(
                "违禁词列表为空。",
                execute_keyword_command("/违禁词 列表", store, actor="42"),
            )
            self.assertEqual(
                "已添加违禁词 K0001：测试 词。",
                execute_keyword_command("/违禁词 添加 测试 词", store, actor="42"),
            )
            listing = execute_keyword_command("/违禁词 列表", store, actor="42")
            self.assertIn("K0001：测试 词", listing)
            self.assertEqual(
                "已删除违禁词 K0001：测试 词。",
                execute_keyword_command("/违禁词 删除 K0001", store, actor="42"),
            )

    def test_command_errors_never_report_success(self) -> None:
        from plugins.content_alert.commands import execute_keyword_command
        from plugins.content_alert.rules import KeywordRuleStore

        with tempfile.TemporaryDirectory() as directory:
            store = KeywordRuleStore(Path(directory) / "keywords.json")
            self.assertEqual(
                "用法：/违禁词 添加 <关键词>、/违禁词 删除 <编号>、/违禁词 列表。",
                execute_keyword_command("/违禁词", store, actor="42"),
            )
            self.assertIn(
                "添加失败",
                execute_keyword_command("/违禁词 添加 a", store, actor="42"),
            )
            self.assertIn(
                "删除失败",
                execute_keyword_command("/违禁词 删除 K9999", store, actor="42"),
            )

    def test_qq_commands_expose_only_manual_rules_and_never_background_rules(
        self,
    ) -> None:
        from plugins.content_alert.commands import execute_keyword_command
        from plugins.content_alert.rules import KeywordRuleStore

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            manual_path = root / "keywords.json"
            background_path = root / "background_keywords.json"
            background_store = KeywordRuleStore(background_path)
            background_store.add("后台占位词一", actor="batch-import-20260902")
            background_store.add("后台占位词二", actor="batch-import-20260902")
            background = background_store.add(
                "后台私密词", actor="batch-import-20260902"
            )
            background_store.add("后台重合词", actor="batch-import-20260902")

            # QQ command handling receives the manual store only.  The private
            # background store survives reload independently for matching and
            # backend audit, without becoming a QQ-visible existence oracle.
            reloaded_manual = KeywordRuleStore(manual_path)
            reloaded_background = KeywordRuleStore(background_path)
            self.assertIn(
                background.pattern,
                {rule.pattern for rule in reloaded_background.snapshot()},
            )
            self.assertEqual(
                "违禁词列表为空。",
                execute_keyword_command("/违禁词 列表", reloaded_manual, actor="42"),
            )
            self.assertIn(
                "已添加违禁词",
                execute_keyword_command(
                    "/违禁词 添加 后台重合词", reloaded_manual, actor="42"
                ),
            )
            manual = reloaded_manual.add("群内手动词", actor="42")

            listing = execute_keyword_command(
                "/违禁词 列表", reloaded_manual, actor="42"
            )
            self.assertIn(f"{manual.rule_id}：{manual.pattern}", listing)
            self.assertNotIn(background.rule_id, listing)
            self.assertNotIn(background.pattern, listing)

            responses = (
                execute_keyword_command("/违禁词", reloaded_manual, actor="42"),
                execute_keyword_command("/违禁词 状态", reloaded_manual, actor="42"),
                execute_keyword_command("/违禁词 添加 a", reloaded_manual, actor="42"),
                execute_keyword_command(
                    f"/违禁词 删除 {background.rule_id}",
                    reloaded_manual,
                    actor="42",
                ),
            )
            self.assertIn("删除失败", responses[-1])
            managed_private_tokens = (
                "political_cn",
                "受保护占位分类",
                "synthetic-generation-private",
                "shards/private-0001.json",
                "私有目录占位词",
            )
            for response in responses:
                self.assertNotIn(background.rule_id, response)
                self.assertNotIn(background.pattern, response)
                for token in managed_private_tokens:
                    self.assertNotIn(token, response)


class ContentAlertServiceTests(unittest.IsolatedAsyncioTestCase):
    class Bot:
        def __init__(self, *, failures: int = 0) -> None:
            self.failures = failures
            self.calls: list[dict[str, object]] = []

        async def send_group_msg(self, **kwargs: object) -> object:
            self.calls.append(kwargs)
            if self.failures:
                self.failures -= 1
                raise OSError("temporary send failure")
            return {"message_id": 9001}

    class ManagedCatalog:
        def __init__(
            self,
            matches: tuple[SimpleNamespace, ...],
            *,
            active: bool = True,
        ) -> None:
            self.matches = matches
            self._snapshot = SimpleNamespace(
                has_active_generation=active,
                generation_id="synthetic-generation" if active else "",
            )

        def snapshot(self) -> SimpleNamespace:
            return self._snapshot

        def match_message(self, _message: object) -> tuple[SimpleNamespace, ...]:
            from plugins.content_alert.rules import normalize_literal_text

            normalized_segments = tuple(
                normalize_literal_text(str(segment.data.get("text", "")))
                for segment in _message
                if getattr(segment, "type", None) == "text"
            )
            selected: list[SimpleNamespace] = []
            for match in self.matches:
                normalized_term = normalize_literal_text(match.term)
                normalized_context = normalize_literal_text(
                    str(getattr(match, "context_term", ""))
                )
                if any(
                    normalized_term in segment
                    and (not normalized_context or normalized_context in segment)
                    for segment in normalized_segments
                ):
                    selected.append(match)
            return tuple(selected)

        def scan_message(
            self,
            message: object,
        ) -> tuple[bool, tuple[SimpleNamespace, ...]]:
            active = bool(self.snapshot().has_active_generation)
            return active, self.match_message(message) if active else ()

        def match_snapshot(
            self,
            _snapshot: SimpleNamespace,
            message: object,
        ) -> tuple[SimpleNamespace, ...]:
            return self.match_message(message)

    def _service(self, directory: str, *, enabled: bool = True):
        from plugins.content_alert.rules import KeywordRuleStore
        from plugins.content_alert.service import ContentAlertService

        store = KeywordRuleStore(Path(directory) / "keywords.json")
        if not store.snapshot():
            store.add("测试违禁词", actor="1")
            store.add("另一个词", actor="1")
        service = ContentAlertService(
            rule_store=store,
            source_group_labels={SOURCE_GROUP_ID: "蜂巢"},
            report_group_id=REPORT_GROUP_ID,
            peer_bot_user_ids=(BOT_USER_ID + 1,),
            runtime_enabled=lambda: enabled,
            clock=lambda: 2_000,
            max_event_age_seconds=300,
            max_excerpt_chars=160,
        )
        return service

    async def test_managed_catalog_scan_is_offloaded_from_the_event_loop(self) -> None:
        from plugins.content_alert.rules import KeywordRuleStore
        from plugins.content_alert.service import ContentAlertService

        catalog = self.ManagedCatalog(())
        with tempfile.TemporaryDirectory() as directory:
            service = ContentAlertService(
                rule_store=KeywordRuleStore(Path(directory) / "keywords.json"),
                managed_catalog=catalog,
                source_group_labels={SOURCE_GROUP_ID: "蜂巢"},
                report_group_id=REPORT_GROUP_ID,
                peer_bot_user_ids=(),
                runtime_enabled=lambda: True,
                clock=lambda: 2_000,
            )
            offload = AsyncMock(return_value=(True, (), False))
            with patch("plugins.content_alert.service.asyncio.to_thread", offload):
                delivered = await service.handle_event(
                    self.Bot(), _group_event("安全内容")
                )

        self.assertFalse(delivered)
        offload.assert_awaited_once()
        from plugins.content_alert.service import _scan_managed_catalog

        self.assertEqual(_scan_managed_catalog, offload.await_args.args[0])
        self.assertIs(catalog, offload.await_args.args[1])

    async def test_service_fixture_never_combines_text_across_segment_boundaries(
        self,
    ) -> None:
        from plugins.content_alert.rules import KeywordRuleStore
        from plugins.content_alert.service import ContentAlertService

        match = SimpleNamespace(
            term="合成领导姓名甲",
            category_ids=("political_cn",),
            category_names=("政治占位分类",),
            disclosure_policy="management_visible",
            subject_type="leader_name",
            context_term="接受合成调查",
            context_class="case_proceeding",
        )
        catalog = self.ManagedCatalog((match,))
        with tempfile.TemporaryDirectory() as directory:
            service = ContentAlertService(
                rule_store=KeywordRuleStore(Path(directory) / "keywords.json"),
                managed_catalog=catalog,
                source_group_labels={SOURCE_GROUP_ID: "蜂巢"},
                report_group_id=REPORT_GROUP_ID,
                peer_bot_user_ids=(),
                runtime_enabled=lambda: True,
                clock=lambda: 2_000,
            )
            bot = self.Bot()
            event = _group_event("占位")
            event.message = Message(
                [
                    MessageSegment.text("合成领导姓名甲"),
                    MessageSegment.at(BOT_USER_ID),
                    MessageSegment.text("接受合成调查"),
                ]
            )

            delivered = await service.handle_event(bot, event)

        self.assertFalse(delivered)
        self.assertEqual([], bot.calls)

    async def test_visible_excerpt_is_prepared_off_the_event_loop(self) -> None:
        from plugins.content_alert.rules import KeywordRuleStore
        from plugins.content_alert.service import ContentAlertService

        event_loop_thread = threading.get_ident()
        excerpt_thread = event_loop_thread
        match = SimpleNamespace(
            term="合成领导姓名甲",
            category_ids=("political_cn",),
            category_names=("政治占位分类",),
            disclosure_policy="management_visible",
            subject_type="leader_name",
            context_term="接受合成调查",
            context_class="case_proceeding",
            segment_index=0,
            start=0,
            end=12,
        )
        catalog = self.ManagedCatalog((match,))

        def prepare_excerpt(*_args: object, **_kwargs: object) -> str:
            nonlocal excerpt_thread
            excerpt_thread = threading.get_ident()
            return "合成摘录"

        with tempfile.TemporaryDirectory() as directory:
            service = ContentAlertService(
                rule_store=KeywordRuleStore(Path(directory) / "keywords.json"),
                managed_catalog=catalog,
                source_group_labels={SOURCE_GROUP_ID: "蜂巢"},
                report_group_id=REPORT_GROUP_ID,
                peer_bot_user_ids=(),
                runtime_enabled=lambda: True,
                clock=lambda: 2_000,
            )
            bot = self.Bot()
            with patch(
                "plugins.content_alert.service._message_excerpt",
                side_effect=prepare_excerpt,
            ):
                delivered = await service.handle_event(
                    bot,
                    _group_event("合成领导姓名甲接受合成调查"),
                )

        self.assertTrue(delivered)
        self.assertNotEqual(event_loop_thread, excerpt_thread)
        self.assertIn("内容摘录：合成摘录", str(bot.calls[0]["message"]))

    async def test_managed_snapshot_is_offloaded_and_inactive_catalog_uses_fallback(
        self,
    ) -> None:
        from plugins.content_alert.rules import KeywordRuleStore
        from plugins.content_alert.service import ContentAlertService

        event_loop_thread = threading.get_ident()

        class ThreadCheckingCatalog(self.ManagedCatalog):
            snapshot_thread = event_loop_thread

            def snapshot(self) -> SimpleNamespace:
                self.snapshot_thread = threading.get_ident()
                return super().snapshot()

        catalog = ThreadCheckingCatalog((), active=False)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            background_store = KeywordRuleStore(root / "background_keywords.json")
            background_store.add("旧版后台占位词", actor="legacy-import")
            service = ContentAlertService(
                rule_store=KeywordRuleStore(root / "keywords.json"),
                background_rule_store=background_store,
                managed_catalog=catalog,
                source_group_labels={SOURCE_GROUP_ID: "蜂巢"},
                report_group_id=REPORT_GROUP_ID,
                peer_bot_user_ids=(),
                runtime_enabled=lambda: True,
                clock=lambda: 2_000,
            )
            bot = self.Bot()

            delivered = await service.handle_event(
                bot,
                _group_event("旧版后台占位词"),
            )

        self.assertTrue(delivered)
        self.assertNotEqual(event_loop_thread, catalog.snapshot_thread)
        self.assertEqual(1, len(bot.calls))

    async def test_runtime_switch_is_rechecked_after_managed_scan_before_delivery(
        self,
    ) -> None:
        from plugins.content_alert.rules import KeywordRuleStore
        from plugins.content_alert.service import ContentAlertService

        enabled = True
        match = SimpleNamespace(
            term="普通占位词甲",
            category_ids=("controversial_topics",),
            category_names=("普通占位分类三",),
            disclosure_policy="management_visible",
        )
        catalog = self.ManagedCatalog((match,))

        async def turn_off_during_scan(
            *_args: object,
        ) -> tuple[bool, tuple[SimpleNamespace, ...], bool]:
            nonlocal enabled
            enabled = False
            return True, (match,), False

        with tempfile.TemporaryDirectory() as directory:
            service = ContentAlertService(
                rule_store=KeywordRuleStore(Path(directory) / "keywords.json"),
                managed_catalog=catalog,
                source_group_labels={SOURCE_GROUP_ID: "蜂巢"},
                report_group_id=REPORT_GROUP_ID,
                peer_bot_user_ids=(),
                runtime_enabled=lambda: enabled,
                clock=lambda: 2_000,
            )
            bot = self.Bot()
            with patch(
                "plugins.content_alert.service.asyncio.to_thread",
                side_effect=turn_off_during_scan,
            ):
                delivered = await service.handle_event(
                    bot,
                    _group_event("普通占位词甲"),
                )

        self.assertFalse(delivered)
        self.assertEqual([], bot.calls)

    async def test_event_age_is_rechecked_after_managed_scan_before_delivery(
        self,
    ) -> None:
        from plugins.content_alert.rules import KeywordRuleStore
        from plugins.content_alert.service import ContentAlertService

        now = 2_000
        match = SimpleNamespace(
            term="普通占位词甲",
            category_ids=("controversial_topics",),
            category_names=("普通占位分类三",),
            disclosure_policy="management_visible",
        )
        catalog = self.ManagedCatalog((match,))

        async def age_event_during_scan(
            *_args: object,
        ) -> tuple[bool, tuple[SimpleNamespace, ...], bool]:
            nonlocal now
            now = 2_301
            return True, (match,), False

        with tempfile.TemporaryDirectory() as directory:
            service = ContentAlertService(
                rule_store=KeywordRuleStore(Path(directory) / "keywords.json"),
                managed_catalog=catalog,
                source_group_labels={SOURCE_GROUP_ID: "蜂巢"},
                report_group_id=REPORT_GROUP_ID,
                peer_bot_user_ids=(),
                runtime_enabled=lambda: True,
                clock=lambda: now,
                max_event_age_seconds=300,
            )
            bot = self.Bot()
            with patch(
                "plugins.content_alert.service.asyncio.to_thread",
                side_effect=age_event_during_scan,
            ):
                delivered = await service.handle_event(
                    bot,
                    _group_event("普通占位词甲"),
                )

        self.assertFalse(delivered)
        self.assertEqual([], bot.calls)

    async def test_managed_scans_have_a_small_per_service_concurrency_limit(
        self,
    ) -> None:
        from plugins.content_alert.rules import KeywordRuleStore
        from plugins.content_alert.service import ContentAlertService

        active = 0
        maximum_active = 0
        match = SimpleNamespace(
            term="普通占位词甲",
            category_ids=("controversial_topics",),
            category_names=("普通占位分类三",),
            disclosure_policy="management_visible",
        )
        catalog = self.ManagedCatalog((match,))

        async def controlled_offload(
            function: Callable[..., object],
            *args: object,
            **kwargs: object,
        ) -> object:
            nonlocal active, maximum_active
            active += 1
            maximum_active = max(maximum_active, active)
            await asyncio.sleep(0.01)
            active -= 1
            from plugins.content_alert.service import _scan_managed_catalog

            if function is _scan_managed_catalog:
                return True, (match,), False
            return function(*args, **kwargs)

        with tempfile.TemporaryDirectory() as directory:
            service = ContentAlertService(
                rule_store=KeywordRuleStore(Path(directory) / "keywords.json"),
                managed_catalog=catalog,
                source_group_labels={SOURCE_GROUP_ID: "蜂巢"},
                report_group_id=REPORT_GROUP_ID,
                peer_bot_user_ids=(),
                runtime_enabled=lambda: True,
                clock=lambda: 2_000,
            )
            bot = self.Bot()
            with patch(
                "plugins.content_alert.service.asyncio.to_thread",
                side_effect=controlled_offload,
            ):
                delivered = await asyncio.gather(
                    *(
                        service.handle_event(
                            bot,
                            _group_event(
                                "普通占位词甲",
                                message_id=message_id,
                            ),
                        )
                        for message_id in (501, 502, 503)
                    )
                )

        self.assertEqual([True, True, True], delivered)
        self.assertLessEqual(maximum_active, 2)
        self.assertEqual(3, len(bot.calls))

    async def test_managed_scan_limit_sends_one_hidden_protection_alert(self) -> None:
        from plugins.content_alert.engine import ScalableLiteralScanLimitError
        from plugins.content_alert.rules import KeywordRuleStore
        from plugins.content_alert.service import ContentAlertService

        class LimitedCatalog(self.ManagedCatalog):
            def match_snapshot(
                self,
                _snapshot: SimpleNamespace,
                _message: object,
            ) -> tuple[SimpleNamespace, ...]:
                raise ScalableLiteralScanLimitError("candidate_limit")

        catalog = LimitedCatalog(())
        secret_input = "合成超限输入" * 100
        with tempfile.TemporaryDirectory() as directory:
            service = ContentAlertService(
                rule_store=KeywordRuleStore(Path(directory) / "keywords.json"),
                managed_catalog=catalog,
                source_group_labels={SOURCE_GROUP_ID: "蜂巢"},
                report_group_id=REPORT_GROUP_ID,
                peer_bot_user_ids=(),
                runtime_enabled=lambda: True,
                clock=lambda: 2_000,
            )
            bot = self.Bot()

            delivered = await service.handle_event(bot, _group_event(secret_input))

        self.assertTrue(delivered)
        self.assertEqual(1, len(bot.calls))
        report = str(Message(bot.calls[0]["message"])[0].data["text"])
        self.assertIn("关键词扫描保护告警", report)
        self.assertIn("未判定具体词条", report)
        self.assertIn(f"QQ：{MEMBER_USER_ID}", report)
        self.assertIn("内容摘录：（内容已隐藏）", report)
        self.assertNotIn(secret_input, report)

    async def test_matching_message_sends_one_plain_text_alert_without_ai(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            service = self._service(directory)
            bot = self.Bot()
            event = _group_event(
                "测试违禁词和另一个词 [CQ:at,qq=123]",
                nickname="恶意[CQ:at,qq=456]昵称",
            )

            delivered = await service.handle_event(bot, event)

        self.assertTrue(delivered)
        self.assertEqual(1, len(bot.calls))
        self.assertEqual(REPORT_GROUP_ID, bot.calls[0]["group_id"])
        message = Message(bot.calls[0]["message"])
        self.assertEqual(1, len(message))
        self.assertEqual("text", message[0].type)
        report = str(message[0].data["text"])
        for expected in (
            "【蜂巢关键词违禁告警】",
            "发送者：恶意[CQ:at,qq=456]昵称",
            str(MEMBER_USER_ID),
            "K0001：测试违禁词",
            "K0002：另一个词",
            "消息ID：456",
            "keyword-literal-v1（未调用 AI）",
            "仅告警，未自动撤回、禁言或记录违规",
        ):
            self.assertIn(expected, report)
        self.assertRegex(report, r"告警编号：KA-[0-9a-f]{12}")

    async def test_background_rule_still_alerts_without_disclosing_rule_metadata(
        self,
    ) -> None:
        from plugins.content_alert.rules import KeywordRuleStore
        from plugins.content_alert.service import ContentAlertService

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            manual_path = root / "keywords.json"
            background_path = root / "background_keywords.json"
            manual_store = KeywordRuleStore(manual_path)
            background_store = KeywordRuleStore(background_path)
            manual = manual_store.add("群内手动词", actor="42")
            background = background_store.add(
                "后台私密词", actor="batch-import-20260902"
            )
            background_store.add("后台占位词", actor="batch-import-20260902")
            self.assertEqual(manual.rule_id, background.rule_id)
            service = ContentAlertService(
                rule_store=KeywordRuleStore(manual_path),
                background_rule_store=KeywordRuleStore(background_path),
                source_group_labels={SOURCE_GROUP_ID: "蜂巢"},
                report_group_id=REPORT_GROUP_ID,
                peer_bot_user_ids=(),
                runtime_enabled=lambda: True,
                clock=lambda: 2_000,
                max_event_age_seconds=300,
            )
            bot = self.Bot()

            background_delivered = await service.handle_event(
                bot,
                _group_event(
                    "有人发送了后台 私密词",
                    message_id=888,
                    nickname="后台敏感昵称",
                ),
            )
            mixed_delivered = await service.handle_event(
                bot,
                _group_event(
                    "群内手动词，以及后台 私密词的完整原文",
                    message_id=889,
                    nickname="后台敏感昵称",
                ),
            )

        self.assertTrue(background_delivered)
        self.assertTrue(mixed_delivered)
        self.assertEqual(2, len(bot.calls))
        background_report = str(Message(bot.calls[0]["message"])[0].data["text"])
        mixed_report = str(Message(bot.calls[1]["message"])[0].data["text"])
        for report in (background_report, mixed_report):
            self.assertIn("内容已隐藏", report)
            self.assertNotIn(background.pattern, report)
            self.assertNotIn("后台 私密词", report)
            self.assertNotIn("后台敏感昵称", report)
        self.assertNotIn(background.rule_id, background_report)
        self.assertIn("消息ID：888", background_report)
        self.assertNotIn(f"{manual.rule_id}：{manual.pattern}", mixed_report)
        self.assertNotIn("完整原文", mixed_report)

    async def test_management_visible_catalog_alert_shows_only_safe_bounded_details(
        self,
    ) -> None:
        from plugins.content_alert.rules import KeywordRuleStore
        from plugins.content_alert.service import ContentAlertService

        visible_term = "普通占位词甲"
        safe_category_name = "普通占位分类三"
        catalog = self.ManagedCatalog(
            (
                SimpleNamespace(
                    term=visible_term,
                    category_ids=("controversial_topics",),
                    category_names=(safe_category_name,),
                    disclosure_policy="management_visible",
                ),
                SimpleNamespace(
                    term=visible_term,
                    category_ids=("controversial_topics",),
                    category_names=(safe_category_name,),
                    disclosure_policy="management_visible",
                ),
            )
        )
        with tempfile.TemporaryDirectory() as directory:
            service = ContentAlertService(
                rule_store=KeywordRuleStore(Path(directory) / "keywords.json"),
                managed_catalog=catalog,
                source_group_labels={SOURCE_GROUP_ID: "蜂巢"},
                report_group_id=REPORT_GROUP_ID,
                peer_bot_user_ids=(),
                runtime_enabled=lambda: True,
                clock=lambda: 2_000,
                max_event_age_seconds=300,
                max_excerpt_chars=48,
            )
            bot = self.Bot()
            event = _group_event(
                f"前缀 {visible_term} " + "无害占位尾部" * 80,
                nickname="普通测试昵称",
            )

            delivered = await service.handle_event(bot, event)

        self.assertTrue(delivered)
        self.assertEqual(1, len(bot.calls))
        message = Message(bot.calls[0]["message"])
        self.assertEqual(1, len(message))
        self.assertEqual("text", message[0].type)
        report = str(message[0].data["text"])
        self.assertIn(safe_category_name, report)
        self.assertIn(visible_term, report)
        self.assertIn("普通测试昵称", report)
        self.assertNotIn("controversial_topics", report)
        match_line = next(
            line for line in report.splitlines() if line.startswith("命中规则：")
        )
        self.assertEqual(1, match_line.count(visible_term))
        excerpt_line = next(
            line for line in report.splitlines() if line.startswith("内容摘录：")
        )
        self.assertLessEqual(len(excerpt_line.removeprefix("内容摘录：")), 49)
        self.assertLessEqual(len(report), 1_800)

    async def test_political_or_mixed_catalog_hit_shows_sender_term_and_bounded_excerpt(
        self,
    ) -> None:
        from plugins.content_alert.rules import KeywordRuleStore
        from plugins.content_alert.service import ContentAlertService

        manual_term = "人工占位词甲"
        visible_term = "普通占位词乙"
        political_term = "政治完整占位词丙"
        internal_tokens = (
            "gender_conflict",
            "political_cn",
            "synthetic-generation",
            "shards/political_cn-0001.json",
        )
        catalog = self.ManagedCatalog(
            (
                SimpleNamespace(
                    term=visible_term,
                    category_ids=("gender_conflict",),
                    category_names=("普通占位分类二",),
                    disclosure_policy="management_visible",
                ),
                SimpleNamespace(
                    term=political_term,
                    category_ids=("political_cn",),
                    category_names=("政治占位分类",),
                    disclosure_policy="management_visible",
                ),
            )
        )
        with tempfile.TemporaryDirectory() as directory:
            store = KeywordRuleStore(Path(directory) / "keywords.json")
            store.add(manual_term, actor="42")
            service = ContentAlertService(
                rule_store=store,
                managed_catalog=catalog,
                source_group_labels={SOURCE_GROUP_ID: "蜂巢"},
                report_group_id=REPORT_GROUP_ID,
                peer_bot_user_ids=(),
                runtime_enabled=lambda: True,
                clock=lambda: 2_000,
                max_event_age_seconds=300,
                max_excerpt_chars=64,
            )
            bot = self.Bot()

            delivered = await service.handle_event(
                bot,
                _group_event(
                    f"{manual_term} {visible_term} {political_term} "
                    + "无害占位尾部" * 80,
                    nickname="合成敏感昵称",
                ),
            )

        self.assertTrue(delivered)
        self.assertEqual(1, len(bot.calls))
        message = Message(bot.calls[0]["message"])
        self.assertEqual(1, len(message))
        self.assertEqual("text", message[0].type)
        report = str(message[0].data["text"])
        self.assertIn("【蜂巢政治敏感告警】", report)
        self.assertIn(
            f"发送者：合成敏感昵称（QQ：{MEMBER_USER_ID}）",
            report,
        )
        self.assertIn(f"政治占位分类：{political_term}", report)
        self.assertIn(f"K0001：{manual_term}", report)
        self.assertIn(f"普通占位分类二：{visible_term}", report)
        excerpt_line = next(
            line for line in report.splitlines() if line.startswith("内容摘录：")
        )
        self.assertIn(political_term, excerpt_line)
        self.assertLessEqual(len(excerpt_line.removeprefix("内容摘录：")), 65)
        self.assertNotIn("内容已隐藏", report)
        self.assertNotIn("昵称已隐藏", report)
        self.assertIn("消息ID：456", report)
        for token in internal_tokens:
            self.assertNotIn(token, report)
        self.assertLessEqual(len(report), 1_800)

    async def test_v2_leader_match_renders_compound_reason_and_centered_excerpt(
        self,
    ) -> None:
        from plugins.content_alert.rules import KeywordRuleStore, normalize_literal_text
        from plugins.content_alert.service import ContentAlertService

        leader = "合成领导甲"
        context = "接受审查调查"
        text = f"{'无害长前缀' * 30}{leader}随后{context}{'无害长后缀' * 30}"
        normalized = normalize_literal_text(text)
        start = normalized.index(normalize_literal_text(leader))
        end = normalized.index(normalize_literal_text(context)) + len(
            normalize_literal_text(context)
        )
        catalog = self.ManagedCatalog(
            (
                SimpleNamespace(
                    term=leader,
                    category_ids=("political_cn",),
                    category_names=("政治敏感",),
                    disclosure_policy="management_visible",
                    subject_type="leader_name",
                    match_mode="same_segment_context",
                    context_term=context,
                    context_class="case_proceeding",
                    segment_index=0,
                    start=start,
                    end=end,
                ),
            )
        )
        with tempfile.TemporaryDirectory() as directory:
            service = ContentAlertService(
                rule_store=KeywordRuleStore(Path(directory) / "keywords.json"),
                managed_catalog=catalog,
                source_group_labels={SOURCE_GROUP_ID: "蜂巢"},
                report_group_id=REPORT_GROUP_ID,
                peer_bot_user_ids=(),
                runtime_enabled=lambda: True,
                clock=lambda: 2_000,
                max_event_age_seconds=300,
                max_excerpt_chars=48,
            )
            bot = self.Bot()

            delivered = await service.handle_event(bot, _group_event(text))

        self.assertTrue(delivered)
        report = str(Message(bot.calls[0]["message"])[0].data["text"])
        self.assertIn(
            f"省部级及以上姓名+案件语境：{leader} / {context}",
            report,
        )
        excerpt_line = next(
            line for line in report.splitlines() if line.startswith("内容摘录：")
        )
        excerpt = excerpt_line.removeprefix("内容摘录：")
        self.assertIn(leader, excerpt)
        self.assertIn(context, excerpt)
        self.assertTrue(excerpt.startswith("…"))
        self.assertTrue(excerpt.endswith("…"))
        self.assertLessEqual(len(excerpt), 49)
        self.assertIn("keyword-literal-context-v2（未调用 AI）", report)

    async def test_maximum_visible_compound_matches_preserve_report_structure(
        self,
    ) -> None:
        from plugins.content_alert.rules import KeywordRuleStore
        from plugins.content_alert.service import ContentAlertService

        managed_matches = tuple(
            SimpleNamespace(
                term=f"姓名{index:02d}" + "甲" * 60,
                category_ids=("political_cn",),
                category_names=("政治占位分类" + "乙" * 24,),
                disclosure_policy="management_visible",
                subject_type="leader_name",
                match_mode="same_segment_context",
                context_term=f"语境{index:02d}" + "丙" * 60,
                context_class="case_proceeding",
                segment_index=0,
                start=0,
                end=1,
            )
            for index in range(12)
        )
        with tempfile.TemporaryDirectory() as directory:
            service = ContentAlertService(
                rule_store=KeywordRuleStore(Path(directory) / "keywords.json"),
                source_group_labels={SOURCE_GROUP_ID: "蜂巢"},
                report_group_id=REPORT_GROUP_ID,
                peer_bot_user_ids=(),
                runtime_enabled=lambda: True,
                clock=lambda: 2_000,
            )
            event = _group_event("用于验证告警报告结构的占位消息")

            report = service._build_report(
                event,
                (),
                managed_matches=managed_matches,
                political_alert=True,
                prepared_excerpt="用于验证告警报告结构的占位消息",
            )

        self.assertLessEqual(len(report), 1_800)
        self.assertIn("消息时间：1970-01-01 08:33:20", report)
        self.assertIn("消息ID：456", report)
        self.assertIn(
            "内容摘录：用于验证告警报告结构的占位消息",
            report,
        )
        self.assertIn(
            "检测器：keyword-literal-context-v2（未调用 AI）",
            report,
        )
        self.assertIn(
            "处置状态：仅告警，未自动撤回、禁言或记录违规",
            report,
        )
        match_line = next(
            line for line in report.splitlines() if line.startswith("命中规则：")
        )
        displayed = 0
        for index, match in enumerate(managed_matches):
            marker = f"姓名{index:02d}"
            if marker not in match_line:
                continue
            displayed += 1
            self.assertIn(match.term, match_line)
            self.assertIn(match.context_term, match_line)
        self.assertLess(displayed, len(managed_matches))
        self.assertTrue(
            match_line.endswith(f"另有 {len(managed_matches) - displayed} 项受控规则")
        )

    async def test_v2_historical_event_direct_match_uses_v2_detector_label(
        self,
    ) -> None:
        from plugins.content_alert.rules import KeywordRuleStore
        from plugins.content_alert.service import ContentAlertService

        event_term = "合成历史事件占位词"
        catalog = self.ManagedCatalog(
            (
                SimpleNamespace(
                    term=event_term,
                    category_ids=("political_cn",),
                    category_names=("历史事件",),
                    disclosure_policy="management_visible",
                    subject_type="historical_event",
                    match_mode="direct",
                    context_term="",
                    context_class="",
                ),
            )
        )
        with tempfile.TemporaryDirectory() as directory:
            service = ContentAlertService(
                rule_store=KeywordRuleStore(Path(directory) / "keywords.json"),
                managed_catalog=catalog,
                source_group_labels={SOURCE_GROUP_ID: "蜂巢"},
                report_group_id=REPORT_GROUP_ID,
                peer_bot_user_ids=(),
                runtime_enabled=lambda: True,
                clock=lambda: 2_000,
            )
            bot = self.Bot()

            delivered = await service.handle_event(
                bot,
                _group_event(f"消息中含{event_term}"),
            )

        self.assertTrue(delivered)
        report = str(Message(bot.calls[0]["message"])[0].data["text"])
        self.assertIn("keyword-literal-v2-direct（未调用 AI）", report)
        self.assertNotIn("keyword-literal-v1（未调用 AI）", report)

    async def test_v2_direct_leader_alert_is_labeled_and_excerpt_is_focused(
        self,
    ) -> None:
        from plugins.content_alert.rules import KeywordRuleStore
        from plugins.content_alert.service import ContentAlertService

        leader_term = "合成领导姓名乙"
        prefix = "前" * 200
        message_text = f"{prefix}{leader_term}{'后' * 200}"
        catalog = self.ManagedCatalog(
            (
                SimpleNamespace(
                    term=leader_term,
                    category_ids=("political_cn",),
                    category_names=("政治姓名",),
                    disclosure_policy="management_visible",
                    subject_type="leader_name",
                    match_mode="direct",
                    context_term="",
                    context_class="",
                    segment_index=0,
                    start=len(prefix),
                    end=len(prefix) + len(leader_term),
                ),
            )
        )
        with tempfile.TemporaryDirectory() as directory:
            service = ContentAlertService(
                rule_store=KeywordRuleStore(Path(directory) / "keywords.json"),
                managed_catalog=catalog,
                source_group_labels={SOURCE_GROUP_ID: "蜂巢"},
                report_group_id=REPORT_GROUP_ID,
                peer_bot_user_ids=(),
                runtime_enabled=lambda: True,
                clock=lambda: 2_000,
                max_excerpt_chars=48,
            )
            bot = self.Bot()

            delivered = await service.handle_event(
                bot,
                _group_event(message_text),
            )

        self.assertTrue(delivered)
        report = str(Message(bot.calls[0]["message"])[0].data["text"])
        self.assertIn("检测器：keyword-literal-v2-direct（未调用 AI）", report)
        excerpt = next(
            line.removeprefix("内容摘录：")
            for line in report.splitlines()
            if line.startswith("内容摘录：")
        )
        self.assertIn(leader_term, excerpt)
        self.assertLessEqual(len(excerpt), 48)

    async def test_political_match_precedes_manual_matches_in_report_budget(
        self,
    ) -> None:
        from plugins.content_alert.engine import KeywordMatch
        from plugins.content_alert.rules import KeywordRuleStore
        from plugins.content_alert.service import ContentAlertService

        manual_matches = tuple(
            KeywordMatch(
                rule_id=f"K{index:04d}",
                pattern=f"人工{index:02d}" + "甲" * 60,
                start=0,
                end=64,
            )
            for index in range(20)
        )
        political_term = "政治" + "乙" * 62
        managed_match = SimpleNamespace(
            term=political_term,
            category_ids=("political_cn",),
            category_names=("历史事件",),
            disclosure_policy="management_visible",
            subject_type="historical_event",
            match_mode="direct",
            context_term="",
            context_class="",
        )
        label = "群" * 64
        nickname = "昵称" * 32
        excerpt = "摘" * 160
        with tempfile.TemporaryDirectory() as directory:
            service = ContentAlertService(
                rule_store=KeywordRuleStore(Path(directory) / "keywords.json"),
                source_group_labels={SOURCE_GROUP_ID: label},
                report_group_id=REPORT_GROUP_ID,
                peer_bot_user_ids=(),
                runtime_enabled=lambda: True,
                clock=lambda: 2_000,
            )

            report = service._build_report(
                _group_event("占位", nickname=nickname),
                manual_matches,
                managed_matches=(managed_match,),
                political_alert=True,
                prepared_excerpt=excerpt,
            )

        self.assertLessEqual(len(report), 1_800)
        match_line = next(
            line for line in report.splitlines() if line.startswith("命中规则：")
        )
        self.assertIn(f"历史事件：{political_term}", match_line)
        displayed_manual = sum(match.pattern in match_line for match in manual_matches)
        self.assertLess(displayed_manual, len(manual_matches))
        self.assertIn(
            f"另有 {len(manual_matches) - displayed_manual} 项人工规则",
            match_line,
        )
        self.assertIn("消息时间：1970-01-01 08:33:20", report)
        self.assertIn("消息ID：456", report)
        self.assertIn(f"内容摘录：{excerpt}", report)
        self.assertIn("检测器：keyword-literal-v2-direct（未调用 AI）", report)
        self.assertIn(
            "处置状态：仅告警，未自动撤回、禁言或记录违规",
            report,
        )

    async def test_transparent_unicode_composition_far_from_hit_keeps_excerpt_focused(
        self,
    ) -> None:
        from plugins.content_alert.rules import KeywordRuleStore, normalize_literal_text
        from plugins.content_alert.service import ContentAlertService

        leader = "合成领导乙"
        context = "进入合成程序"
        prefixes = (
            "e\u0301",
            "e \u0301",
            "e\u200d\u0301",
            "e\u00b4",
            "A\u00a0\u030a",
            "\u1100\u200b\u1161\u11a8",
            "\u1100\u2065\u1161",
            "\u1100\ufff0\u1161",
        )
        for prefix in prefixes:
            with self.subTest(prefix=prefix.encode("unicode_escape").decode("ascii")):
                text = (
                    f"{'无害长前缀' * 30}{prefix}{'安全填充' * 30}"
                    f"{leader}随后{context}{'无害长后缀' * 30}"
                )
                normalized = normalize_literal_text(text)
                start = normalized.index(normalize_literal_text(leader))
                end = normalized.index(normalize_literal_text(context)) + len(
                    normalize_literal_text(context)
                )
                catalog = self.ManagedCatalog(
                    (
                        SimpleNamespace(
                            term=leader,
                            category_ids=("political_cn",),
                            category_names=("政治占位分类",),
                            disclosure_policy="management_visible",
                            subject_type="leader_name",
                            match_mode="same_segment_context",
                            context_term=context,
                            context_class="case_proceeding",
                            segment_index=0,
                            start=start,
                            end=end,
                        ),
                    )
                )
                with tempfile.TemporaryDirectory() as directory:
                    service = ContentAlertService(
                        rule_store=KeywordRuleStore(Path(directory) / "keywords.json"),
                        managed_catalog=catalog,
                        source_group_labels={SOURCE_GROUP_ID: "蜂巢"},
                        report_group_id=REPORT_GROUP_ID,
                        peer_bot_user_ids=(),
                        runtime_enabled=lambda: True,
                        clock=lambda: 2_000,
                        max_event_age_seconds=300,
                        max_excerpt_chars=48,
                    )
                    bot = self.Bot()

                    delivered = await service.handle_event(bot, _group_event(text))

                self.assertTrue(delivered)
                report = str(Message(bot.calls[0]["message"])[0].data["text"])
                excerpt = next(
                    line.removeprefix("内容摘录：")
                    for line in report.splitlines()
                    if line.startswith("内容摘录：")
                )
                self.assertIn(leader, excerpt)
                self.assertIn(context, excerpt)
                self.assertLessEqual(len(excerpt), 49)

    def test_normalized_span_mapping_does_not_overmerge_hangul_starters(self) -> None:
        from plugins.content_alert.rules import normalize_literal_text
        from plugins.content_alert.service import _normalized_original_spans

        raw = "\u1100" * 20 + "\u1161"
        spans = _normalized_original_spans(raw)

        self.assertEqual(len(normalize_literal_text(raw)), len(spans))
        self.assertEqual((0, 1), spans[0])
        self.assertEqual((19, 21), spans[-1])
        self.assertNotIn((0, len(raw)), spans)

    def test_focused_excerpt_covers_reordered_combining_mark_raw_span(self) -> None:
        from plugins.content_alert.rules import normalize_literal_text
        from plugins.content_alert.service import _focused_message_excerpt

        raw = "a\u0315\u0300"
        self.assertEqual("à\u0315", normalize_literal_text(raw))
        excerpt = _focused_message_excerpt(
            _group_event(raw),
            focus=SimpleNamespace(segment_index=0, start=0, end=2),
            limit=3,
        )

        self.assertEqual("à\u0315", excerpt)

    def test_centered_excerpt_preserves_focus_after_nfkc_expansion(self) -> None:
        from plugins.content_alert.service import _centered_one_line

        focus = "合成领导姓名甲合成案件语境"
        raw = "\ufdfa" * 80 + focus + "尾" * 80
        excerpt = _centered_one_line(
            raw,
            raw_start=80,
            raw_end=80 + len(focus),
            limit=160,
        )

        self.assertIn(focus, excerpt)
        self.assertLessEqual(len(excerpt), 160)

    def test_centered_excerpt_preserves_whitespace_at_focus_boundaries(self) -> None:
        from plugins.content_alert.service import _centered_one_line

        raw = "前文 目标 后文"
        excerpt = _centered_one_line(
            raw,
            raw_start=3,
            raw_end=5,
            limit=32,
        )

        self.assertEqual("前文 目标 后文", excerpt)

    def test_match_rendering_never_walks_past_the_display_cap(self) -> None:
        from plugins.content_alert.service import _render_matches

        visible = tuple(
            SimpleNamespace(
                term=f"普通占位词{index}",
                category_names=("普通占位分类",),
                context_term="",
                context_class="",
                subject_type="historical_event",
            )
            for index in range(12)
        )

        class ExplodingMatch:
            @property
            def term(self) -> str:
                raise AssertionError("renderer walked beyond its display cap")

        rendered = _render_matches(
            (),
            managed_matches=(*visible, ExplodingMatch()),
            strict_hidden=False,
        )

        self.assertIn("另有 1 项受控规则", rendered)

    def test_political_match_is_selected_before_managed_display_cap(self) -> None:
        from plugins.content_alert.service import _render_matches

        ordinary = tuple(
            SimpleNamespace(
                term=f"普通占位词{index:02d}",
                category_ids=("ordinary",),
                category_names=("普通分类",),
                context_term="",
                context_class="",
                subject_type="historical_event",
            )
            for index in range(12)
        )
        political_term = "政治占位词优先"
        political = SimpleNamespace(
            term=political_term,
            category_ids=("political_cn",),
            category_names=("历史事件",),
            context_term="",
            context_class="",
            subject_type="historical_event",
        )

        rendered = _render_matches(
            (),
            managed_matches=(*ordinary, political),
            strict_hidden=False,
            political_alert=True,
        )

        self.assertIn(f"历史事件：{political_term}", rendered)
        self.assertIn("另有 1 项受控规则", rendered)

    async def test_political_category_does_not_force_hidden_disclosure(
        self,
    ) -> None:
        from plugins.content_alert.rules import KeywordRuleStore
        from plugins.content_alert.service import ContentAlertService

        political_term = "政治完整占位词己"
        catalog = self.ManagedCatalog(
            (
                SimpleNamespace(
                    term=political_term,
                    category_ids=("political_cn",),
                    category_names=("政治占位分类",),
                    disclosure_policy="management_visible",
                ),
            )
        )
        with tempfile.TemporaryDirectory() as directory:
            service = ContentAlertService(
                rule_store=KeywordRuleStore(Path(directory) / "keywords.json"),
                managed_catalog=catalog,
                source_group_labels={SOURCE_GROUP_ID: "蜂巢"},
                report_group_id=REPORT_GROUP_ID,
                peer_bot_user_ids=(),
                runtime_enabled=lambda: True,
                clock=lambda: 2_000,
                max_event_age_seconds=300,
            )
            bot = self.Bot()

            delivered = await service.handle_event(
                bot,
                _group_event(
                    f"消息中含{political_term}且有完整原文",
                    nickname="安全测试昵称",
                ),
            )

        self.assertTrue(delivered)
        report = str(Message(bot.calls[0]["message"])[0].data["text"])
        self.assertIn("【蜂巢政治敏感告警】", report)
        self.assertIn(f"政治占位分类：{political_term}", report)
        self.assertIn(f"发送者：安全测试昵称（QQ：{MEMBER_USER_ID}）", report)
        self.assertIn(f"内容摘录：消息中含{political_term}且有完整原文", report)
        self.assertNotIn("内容已隐藏", report)

    async def test_legacy_strict_hidden_political_match_keeps_details_hidden(
        self,
    ) -> None:
        from plugins.content_alert.rules import KeywordRuleStore
        from plugins.content_alert.service import ContentAlertService

        protected_term = "旧版受保护占位词"
        catalog = self.ManagedCatalog(
            (
                SimpleNamespace(
                    term=protected_term,
                    category_ids=("political_cn",),
                    category_names=("旧版受保护占位分类",),
                    disclosure_policy="strict_hidden",
                ),
            )
        )
        with tempfile.TemporaryDirectory() as directory:
            service = ContentAlertService(
                rule_store=KeywordRuleStore(Path(directory) / "keywords.json"),
                managed_catalog=catalog,
                source_group_labels={SOURCE_GROUP_ID: "蜂巢"},
                report_group_id=REPORT_GROUP_ID,
                peer_bot_user_ids=(),
                runtime_enabled=lambda: True,
                clock=lambda: 2_000,
                max_event_age_seconds=300,
            )
            bot = self.Bot()

            delivered = await service.handle_event(
                bot,
                _group_event(
                    f"消息中含{protected_term}且有完整原文",
                    nickname="普通测试昵称",
                ),
            )

        self.assertTrue(delivered)
        report = str(Message(bot.calls[0]["message"])[0].data["text"])
        self.assertIn("【蜂巢政治敏感告警】", report)
        self.assertIn(
            f"发送者：普通测试昵称（QQ：{MEMBER_USER_ID}）",
            report,
        )
        self.assertIn("内容摘录：（内容已隐藏）", report)
        self.assertIn("政治敏感规则命中（词条与详情已隐藏）", report)
        self.assertNotIn(protected_term, report)

    async def test_strict_hidden_catalog_alert_hides_sender_rule_and_excerpt(
        self,
    ) -> None:
        from plugins.content_alert.rules import KeywordRuleStore
        from plugins.content_alert.service import ContentAlertService

        protected_term = "受保护占位词丁"
        catalog = self.ManagedCatalog(
            (
                SimpleNamespace(
                    term=protected_term,
                    category_ids=("restricted_internal",),
                    category_names=("受保护占位分类",),
                    disclosure_policy="strict_hidden",
                ),
            )
        )
        with tempfile.TemporaryDirectory() as directory:
            service = ContentAlertService(
                rule_store=KeywordRuleStore(Path(directory) / "keywords.json"),
                managed_catalog=catalog,
                source_group_labels={SOURCE_GROUP_ID: "蜂巢"},
                report_group_id=REPORT_GROUP_ID,
                peer_bot_user_ids=(),
                runtime_enabled=lambda: True,
                clock=lambda: 2_000,
                max_event_age_seconds=300,
            )
            bot = self.Bot()

            delivered = await service.handle_event(
                bot,
                _group_event(
                    f"消息中含{protected_term}",
                    nickname="普通测试昵称",
                ),
            )

        self.assertTrue(delivered)
        report = str(Message(bot.calls[0]["message"])[0].data["text"])
        self.assertIn("【蜂巢关键词违禁告警】", report)
        self.assertIn(
            f"发送者：昵称已隐藏（QQ：{MEMBER_USER_ID}）",
            report,
        )
        self.assertIn("受保护规则命中（详情已隐藏）", report)
        self.assertIn("内容摘录：（内容已隐藏）", report)
        self.assertNotIn(protected_term, report)
        self.assertNotIn("受保护占位分类", report)
        self.assertNotIn("普通测试昵称", report)

    async def test_strict_hidden_match_still_hides_mixed_political_alert(
        self,
    ) -> None:
        from plugins.content_alert.rules import KeywordRuleStore
        from plugins.content_alert.service import ContentAlertService

        political_term = "政治完整占位词戊"
        protected_term = "受保护占位词辛"
        catalog = self.ManagedCatalog(
            (
                SimpleNamespace(
                    term=political_term,
                    category_ids=("political_cn",),
                    category_names=("政治占位分类",),
                    disclosure_policy="management_visible",
                ),
                SimpleNamespace(
                    term=protected_term,
                    category_ids=("restricted_internal",),
                    category_names=("受保护占位分类",),
                    disclosure_policy="strict_hidden",
                ),
            )
        )
        with tempfile.TemporaryDirectory() as directory:
            service = ContentAlertService(
                rule_store=KeywordRuleStore(Path(directory) / "keywords.json"),
                managed_catalog=catalog,
                source_group_labels={SOURCE_GROUP_ID: "蜂巢"},
                report_group_id=REPORT_GROUP_ID,
                peer_bot_user_ids=(),
                runtime_enabled=lambda: True,
                clock=lambda: 2_000,
                max_event_age_seconds=300,
            )
            bot = self.Bot()

            delivered = await service.handle_event(
                bot,
                _group_event(
                    f"消息中含{political_term}和{protected_term}",
                    nickname="普通测试昵称",
                ),
            )

        self.assertTrue(delivered)
        report = str(Message(bot.calls[0]["message"])[0].data["text"])
        self.assertIn("【蜂巢政治敏感告警】", report)
        self.assertIn(
            f"发送者：普通测试昵称（QQ：{MEMBER_USER_ID}）",
            report,
        )
        self.assertIn("内容摘录：（内容已隐藏）", report)
        self.assertNotIn(political_term, report)
        self.assertNotIn(protected_term, report)

    async def test_strict_hidden_sender_name_cannot_bypass_with_variation_marks(
        self,
    ) -> None:
        from plugins.content_alert.rules import KeywordRuleStore
        from plugins.content_alert.service import ContentAlertService

        political_term = "政治完整占位词壬"
        protected_term = "受保护占位昵称"
        catalog = self.ManagedCatalog(
            (
                SimpleNamespace(
                    term=political_term,
                    category_ids=("political_cn",),
                    category_names=("政治占位分类",),
                    disclosure_policy="management_visible",
                ),
                SimpleNamespace(
                    term=protected_term,
                    category_ids=("restricted_internal",),
                    category_names=("受保护占位分类",),
                    disclosure_policy="strict_hidden",
                ),
            )
        )
        disguised_name = "\ufe0f".join(protected_term)
        with tempfile.TemporaryDirectory() as directory:
            service = ContentAlertService(
                rule_store=KeywordRuleStore(Path(directory) / "keywords.json"),
                managed_catalog=catalog,
                source_group_labels={SOURCE_GROUP_ID: "蜂巢"},
                report_group_id=REPORT_GROUP_ID,
                peer_bot_user_ids=(),
                runtime_enabled=lambda: True,
                clock=lambda: 2_000,
                max_event_age_seconds=300,
            )
            bot = self.Bot()

            delivered = await service.handle_event(
                bot,
                _group_event(
                    f"消息中含{political_term}和{protected_term}",
                    nickname=disguised_name,
                ),
            )

        self.assertTrue(delivered)
        report = str(Message(bot.calls[0]["message"])[0].data["text"])
        self.assertIn(
            f"发送者：昵称含受保护内容，已隐藏（QQ：{MEMBER_USER_ID}）",
            report,
        )
        self.assertNotIn(disguised_name, report)

    async def test_sender_redaction_uses_same_managed_snapshot_as_message_scan(
        self,
    ) -> None:
        from plugins.content_alert.rules import KeywordRuleStore
        from plugins.content_alert.service import ContentAlertService

        political_term = "政治完整占位词快照"
        protected_term = "受保护占位昵称快照"

        class SwitchingCatalog(self.ManagedCatalog):
            def __init__(self) -> None:
                old_matches = (
                    SimpleNamespace(
                        term=political_term,
                        category_ids=("political_cn",),
                        category_names=("政治占位分类",),
                        disclosure_policy="management_visible",
                    ),
                    SimpleNamespace(
                        term=protected_term,
                        category_ids=("restricted_internal",),
                        category_names=("受保护占位分类",),
                        disclosure_policy="strict_hidden",
                    ),
                )
                super().__init__(old_matches)
                self._snapshot = SimpleNamespace(
                    has_active_generation=True,
                    generation_id="synthetic-old-generation",
                    matches=old_matches,
                )
                self.calls = 0

            def match_snapshot(
                self,
                snapshot: SimpleNamespace,
                value: object,
            ) -> tuple[SimpleNamespace, ...]:
                self.calls += 1
                current_matches = self.matches
                self.matches = tuple(snapshot.matches)
                try:
                    result = super().match_message(value)
                finally:
                    self.matches = current_matches
                if self.calls == 1:
                    # Simulate a pointer switch immediately after the source
                    # message scan. Sender redaction must stay on that snapshot.
                    self._snapshot = SimpleNamespace(
                        has_active_generation=True,
                        generation_id="synthetic-new-generation",
                        matches=(),
                    )
                return result

        catalog = SwitchingCatalog()
        with tempfile.TemporaryDirectory() as directory:
            service = ContentAlertService(
                rule_store=KeywordRuleStore(Path(directory) / "keywords.json"),
                managed_catalog=catalog,
                source_group_labels={SOURCE_GROUP_ID: "蜂巢"},
                report_group_id=REPORT_GROUP_ID,
                peer_bot_user_ids=(),
                runtime_enabled=lambda: True,
                clock=lambda: 2_000,
            )
            bot = self.Bot()

            delivered = await service.handle_event(
                bot,
                _group_event(
                    f"消息中含{political_term}和{protected_term}",
                    nickname=protected_term,
                ),
            )

        self.assertTrue(delivered)
        report = str(Message(bot.calls[0]["message"])[0].data["text"])
        self.assertIn(
            f"发送者：昵称含受保护内容，已隐藏（QQ：{MEMBER_USER_ID}）",
            report,
        )
        self.assertNotIn(protected_term, report)

    async def test_visible_political_alert_does_not_rescan_sender_name(self) -> None:
        from plugins.content_alert.rules import KeywordRuleStore
        from plugins.content_alert.service import ContentAlertService

        political_term = "政治完整占位词庚"

        class FailingNameCatalog(self.ManagedCatalog):
            def __init__(self) -> None:
                super().__init__(
                    (
                        SimpleNamespace(
                            term=political_term,
                            category_ids=("political_cn",),
                            category_names=("政治占位分类",),
                            disclosure_policy="management_visible",
                        ),
                    )
                )
                self.calls = 0

            def match_message(self, value: object) -> tuple[SimpleNamespace, ...]:
                self.calls += 1
                if self.calls > 1:
                    raise RuntimeError("synthetic sender scan failure")
                return super().match_message(value)

        catalog = FailingNameCatalog()
        with tempfile.TemporaryDirectory() as directory:
            service = ContentAlertService(
                rule_store=KeywordRuleStore(Path(directory) / "keywords.json"),
                managed_catalog=catalog,
                source_group_labels={SOURCE_GROUP_ID: "蜂巢"},
                report_group_id=REPORT_GROUP_ID,
                peer_bot_user_ids=(),
                runtime_enabled=lambda: True,
                clock=lambda: 2_000,
                max_event_age_seconds=300,
            )
            bot = self.Bot()

            delivered = await service.handle_event(
                bot,
                _group_event(
                    f"消息中含{political_term}",
                    nickname="原本安全的昵称",
                ),
            )

        self.assertTrue(delivered)
        self.assertEqual(1, catalog.calls)
        report = str(Message(bot.calls[0]["message"])[0].data["text"])
        self.assertIn(
            f"发送者：原本安全的昵称（QQ：{MEMBER_USER_ID}）",
            report,
        )
        self.assertIn(f"政治占位分类：{political_term}", report)
        self.assertIn(f"内容摘录：消息中含{political_term}", report)

    async def test_active_managed_generation_disables_legacy_background_fallback(
        self,
    ) -> None:
        from plugins.content_alert.rules import KeywordRuleStore
        from plugins.content_alert.service import ContentAlertService

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            background_store = KeywordRuleStore(root / "background_keywords.json")
            background_store.add("旧版后台占位词", actor="legacy-import")
            service = ContentAlertService(
                rule_store=KeywordRuleStore(root / "keywords.json"),
                background_rule_store=background_store,
                managed_catalog=self.ManagedCatalog(()),
                source_group_labels={SOURCE_GROUP_ID: "蜂巢"},
                report_group_id=REPORT_GROUP_ID,
                peer_bot_user_ids=(),
                runtime_enabled=lambda: True,
                clock=lambda: 2_000,
                max_event_age_seconds=300,
            )
            bot = self.Bot()

            delivered = await service.handle_event(
                bot,
                _group_event("旧版后台占位词"),
            )

        self.assertFalse(delivered)
        self.assertEqual([], bot.calls)

    async def test_scope_freshness_switch_and_non_text_boundaries_fail_closed(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            enabled_service = self._service(directory)
            disabled_service = self._service(directory, enabled=False)
            bot = self.Bot()
            cases = (
                _group_event("测试违禁词", group_id=SOURCE_GROUP_ID + 99),
                _group_event("测试违禁词", user_id=BOT_USER_ID),
                _group_event("测试违禁词", user_id=BOT_USER_ID + 1),
                _group_event("测试违禁词", event_time=1_000),
                _group_event("", include_image=True),
            )
            for event in cases:
                self.assertFalse(await enabled_service.handle_event(bot, event))
            self.assertFalse(
                await disabled_service.handle_event(
                    bot, _group_event("测试违禁词", message_id=999)
                )
            )

        self.assertEqual([], bot.calls)

    async def test_duplicate_delivery_is_suppressed_but_failure_can_retry(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            service = self._service(directory)
            event = _group_event("测试违禁词")
            bot = self.Bot()

            self.assertTrue(await service.handle_event(bot, event))
            self.assertFalse(await service.handle_event(bot, event))
            self.assertEqual(1, len(bot.calls))

        with tempfile.TemporaryDirectory() as directory:
            service = self._service(directory)
            retrying_bot = self.Bot(failures=1)
            event = _group_event("测试违禁词", message_id=777)
            with self.assertRaises(OSError):
                await service.handle_event(retrying_bot, event)
            self.assertTrue(await service.handle_event(retrying_bot, event))
            self.assertEqual(2, len(retrying_bot.calls))


class ContentAlertMatcherTests(unittest.IsolatedAsyncioTestCase):
    async def test_real_matcher_exposes_catalog_only_to_alert_service(self) -> None:
        from plugins.content_alert import matcher
        from plugins.content_alert.rules import KeywordRuleStore

        self.assertIs(
            matcher.MANAGED_CATALOG,
            matcher.ALERT_SERVICE._managed_catalog,
        )
        with tempfile.TemporaryDirectory() as directory:
            manual_store = KeywordRuleStore(Path(directory) / "keywords.json")
            manual = manual_store.add("群内人工占位词", actor="42")
            configured_driver = SimpleNamespace(
                config=SimpleNamespace(superusers={str(MEMBER_USER_ID)})
            )
            with (
                patch.object(matcher, "RULE_STORE", manual_store),
                patch.object(matcher, "get_driver", return_value=configured_driver),
                patch.object(
                    matcher.keyword_command_matcher,
                    "finish",
                    new=AsyncMock(),
                ) as finish,
            ):
                await matcher.handle_keyword_command(_private_event("/违禁词 列表"))

        finish.assert_awaited_once()
        response = finish.await_args.args[0]
        response_text = (
            str(response.data["text"])
            if isinstance(response, MessageSegment)
            else str(response)
        )
        self.assertIn(f"{manual.rule_id}：{manual.pattern}", response_text)
        for private_token in (
            "political_cn",
            "受保护占位分类",
            "synthetic-generation-private",
            "shards/private-0001.json",
            "私有目录占位词",
        ):
            self.assertNotIn(private_token, response_text)

    async def test_alert_rule_requires_configured_source_non_self_and_runtime_switch(
        self,
    ) -> None:
        from plugins.content_alert import matcher

        config = SimpleNamespace(
            content_alert_enabled=True,
            content_alert_capable=True,
            content_alert_source_group_ids=(SOURCE_GROUP_ID,),
            peer_bot_user_ids=(),
            content_alert_report_group_id=REPORT_GROUP_ID,
            monitor_only_group_ids=(SOURCE_GROUP_ID,),
        )
        features = SimpleNamespace(
            snapshot=lambda: SimpleNamespace(content_alert_enabled=True)
        )
        with (
            patch.object(matcher, "CONFIG", config),
            patch.object(matcher, "FEATURES", features),
        ):
            self.assertTrue(await matcher.is_source_alert_event(_group_event("文字")))
            self.assertFalse(
                await matcher.is_source_alert_event(
                    _group_event("文字", group_id=SOURCE_GROUP_ID + 1)
                )
            )
            self.assertFalse(
                await matcher.is_source_alert_event(
                    _group_event("文字", user_id=BOT_USER_ID)
                )
            )

    async def test_keyword_commands_are_private_or_addressed_in_management_group_only(
        self,
    ) -> None:
        from plugins.content_alert.matcher import extract_keyword_command

        self.assertEqual(
            "/违禁词 列表",
            extract_keyword_command(
                _group_event(
                    "/违禁词 列表",
                    group_id=REPORT_GROUP_ID,
                    addressed=True,
                ),
                report_group_id=REPORT_GROUP_ID,
            ),
        )
        self.assertIsNone(
            extract_keyword_command(
                _group_event(
                    "/违禁词 列表",
                    group_id=REPORT_GROUP_ID,
                    addressed=False,
                ),
                report_group_id=REPORT_GROUP_ID,
            )
        )
        self.assertIsNone(
            extract_keyword_command(
                _group_event(
                    "/违禁词 列表",
                    group_id=SOURCE_GROUP_ID,
                    addressed=True,
                ),
                report_group_id=REPORT_GROUP_ID,
            )
        )
        self.assertEqual(
            "/违禁词 列表",
            extract_keyword_command(
                _private_event("/违禁词 列表"),
                report_group_id=REPORT_GROUP_ID,
            ),
        )

    async def test_non_superuser_cannot_mutate_private_keyword_rules(self) -> None:
        from plugins.content_alert import matcher
        from plugins.content_alert.rules import KeywordRuleStore

        with tempfile.TemporaryDirectory() as directory:
            store = KeywordRuleStore(Path(directory) / "keywords.json")
            configured_driver = SimpleNamespace(
                config=SimpleNamespace(superusers={str(MEMBER_USER_ID + 1)})
            )
            event = _private_event("/违禁词 添加 测试词")
            with (
                patch.object(matcher, "RULE_STORE", store),
                patch.object(matcher, "get_driver", return_value=configured_driver),
                patch.object(
                    matcher.keyword_command_matcher, "finish", new=AsyncMock()
                ) as finish,
            ):
                await matcher.handle_keyword_command(event)

            finish.assert_awaited_once_with("你没有违禁词管理权限。")
            self.assertEqual((), store.snapshot())


if __name__ == "__main__":
    unittest.main()
