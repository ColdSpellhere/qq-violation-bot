from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
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
            str(
                root
                / "data"
                / "content_alert"
                / "managed"
                / "current.json"
            ),
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

    def test_nfkc_casefold_whitespace_and_zero_width_are_normalized(self) -> None:
        from plugins.content_alert.engine import LiteralKeywordMatcher
        from plugins.content_alert.rules import KeywordRule

        matcher = LiteralKeywordMatcher(
            (KeywordRule(rule_id="K0001", pattern="Ａb 禁 词"),)
        )

        matches = matcher.match_text("前缀 aB\u200b禁\t词 后缀")

        self.assertEqual(["K0001"], [item.rule_id for item in matches])

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
                execute_keyword_command(
                    "/违禁词 状态", reloaded_manual, actor="42"
                ),
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

            text = "".join(
                str(segment.data.get("text", ""))
                for segment in _message
                if getattr(segment, "type", None) == "text"
            )
            normalized_text = normalize_literal_text(text)
            return tuple(
                match
                for match in self.matches
                if normalize_literal_text(match.term) in normalized_text
            )

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

    async def test_political_or_mixed_catalog_hit_identifies_sender_but_hides_content(
        self,
    ) -> None:
        from plugins.content_alert.rules import KeywordRuleStore
        from plugins.content_alert.service import ContentAlertService

        manual_term = "人工占位词甲"
        visible_term = "普通占位词乙"
        protected_term = "受保护占位词丙"
        internal_tokens = (
            manual_term,
            visible_term,
            protected_term,
            "普通占位分类二",
            "受保护占位分类",
            "gender_conflict",
            "political_cn",
            "synthetic-generation",
            "shards/political_cn-0001.json",
            "完整原文尾部",
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
                    term=protected_term,
                    category_ids=("political_cn",),
                    category_names=("受保护占位分类",),
                    disclosure_policy="strict_hidden",
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
            )
            bot = self.Bot()

            delivered = await service.handle_event(
                bot,
                _group_event(
                    f"{manual_term} {visible_term} {protected_term} 完整原文尾部",
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
        self.assertIn("内容已隐藏", report)
        self.assertIn("政治敏感规则命中（词条与详情已隐藏）", report)
        self.assertNotIn("昵称已隐藏", report)
        self.assertIn("消息ID：456", report)
        for token in internal_tokens:
            self.assertNotIn(token, report)
        self.assertLessEqual(len(report), 1_800)

    async def test_political_category_forces_hidden_disclosure_if_policy_is_wrong(
        self,
    ) -> None:
        from plugins.content_alert.rules import KeywordRuleStore
        from plugins.content_alert.service import ContentAlertService

        protected_term = "受保护占位词己"
        catalog = self.ManagedCatalog(
            (
                SimpleNamespace(
                    term=protected_term,
                    category_ids=("political_cn",),
                    category_names=("错误披露策略占位分类",),
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
                    f"消息中含{protected_term}且有完整原文",
                    nickname="安全测试昵称",
                ),
            )

        self.assertTrue(delivered)
        report = str(Message(bot.calls[0]["message"])[0].data["text"])
        self.assertIn("【蜂巢政治敏感告警】", report)
        self.assertIn("内容摘录：（内容已隐藏）", report)
        self.assertIn("政治敏感规则命中（词条与详情已隐藏）", report)
        self.assertNotIn(protected_term, report)
        self.assertNotIn("错误披露策略占位分类", report)
        self.assertNotIn("完整原文", report)

    async def test_political_alert_hides_protected_sender_name_but_keeps_qq(
        self,
    ) -> None:
        from plugins.content_alert.rules import KeywordRuleStore
        from plugins.content_alert.service import ContentAlertService

        protected_term = "受保护占位词丁"
        catalog = self.ManagedCatalog(
            (
                SimpleNamespace(
                    term=protected_term,
                    category_ids=("political_cn",),
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
                    nickname=f"成员-{protected_term}",
                ),
            )

        self.assertTrue(delivered)
        report = str(Message(bot.calls[0]["message"])[0].data["text"])
        self.assertIn("【蜂巢政治敏感告警】", report)
        self.assertIn(
            f"发送者：昵称含受保护内容，已隐藏（QQ：{MEMBER_USER_ID}）",
            report,
        )
        self.assertNotIn(protected_term, report)

    async def test_political_alert_scans_full_sender_name_before_display_truncation(
        self,
    ) -> None:
        from plugins.content_alert.rules import KeywordRuleStore
        from plugins.content_alert.service import ContentAlertService

        protected_term = "受保护占位词戊"
        # The matcher removes Cf characters, while report rendering also drops
        # other controls.  Put both across the display limit so the privacy
        # check must cover the full rendered form, not only raw or truncated
        # nickname text.
        split_protected_term = "\u200b\x01".join(protected_term)
        nickname = "安" * 60 + split_protected_term + "尾部"
        catalog = self.ManagedCatalog(
            (
                SimpleNamespace(
                    term=protected_term,
                    category_ids=("political_cn",),
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
                    nickname=nickname,
                ),
            )

        self.assertTrue(delivered)
        report = str(Message(bot.calls[0]["message"])[0].data["text"])
        self.assertIn(
            f"发送者：昵称含受保护内容，已隐藏（QQ：{MEMBER_USER_ID}）",
            report,
        )
        self.assertNotIn("安" * 20, report)
        self.assertNotIn(protected_term, report)

    async def test_political_sender_scan_failure_hides_name_but_keeps_qq(self) -> None:
        from plugins.content_alert.rules import KeywordRuleStore
        from plugins.content_alert.service import ContentAlertService

        protected_term = "受保护占位词庚"

        class FailingNameCatalog(self.ManagedCatalog):
            def __init__(self) -> None:
                super().__init__(
                    (
                        SimpleNamespace(
                            term=protected_term,
                            category_ids=("political_cn",),
                            category_names=("受保护占位分类",),
                            disclosure_policy="strict_hidden",
                        ),
                    )
                )
                self.calls = 0

            def match_message(self, value: object) -> tuple[SimpleNamespace, ...]:
                self.calls += 1
                if self.calls > 1:
                    raise RuntimeError("synthetic sender scan failure")
                return super().match_message(value)

        with tempfile.TemporaryDirectory() as directory:
            service = ContentAlertService(
                rule_store=KeywordRuleStore(Path(directory) / "keywords.json"),
                managed_catalog=FailingNameCatalog(),
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
                    nickname="原本安全的昵称",
                ),
            )

        self.assertTrue(delivered)
        report = str(Message(bot.calls[0]["message"])[0].data["text"])
        self.assertIn(
            f"发送者：昵称含受保护内容，已隐藏（QQ：{MEMBER_USER_ID}）",
            report,
        )
        self.assertNotIn("原本安全的昵称", report)
        self.assertNotIn(protected_term, report)

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
                await matcher.handle_keyword_command(
                    _private_event("/违禁词 列表")
                )

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
