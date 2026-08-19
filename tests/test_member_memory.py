import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import nonebot
from nonebot.adapters.onebot.v11 import GroupMessageEvent, Message

os.environ.setdefault("TARGET_GROUP_ID", "999000111")

from plugins.chat_archive.db import ContextMessage
from plugins.member_memory.ai import extract_memory_candidates
from plugins.member_memory.store import (
    MemberProfile,
    MemoryTrait,
    apply_candidates,
    commit_summary,
    load_profiles,
    remember_identity,
    _write_mirror,
)

try:
    nonebot.get_driver()
except ValueError:
    nonebot.init()

from plugins.member_memory import matcher as memory_matcher


def _group_event(
    *,
    group_id: int | None = None,
    user_id: int = 456791,
    self_id: int = 10000,
    text: str = "我喜欢火锅",
) -> GroupMessageEvent:
    message = Message(text)
    return GroupMessageEvent(
        time=1785168002,
        self_id=self_id,
        post_type="message",
        sub_type="normal",
        user_id=user_id,
        message_type="group",
        message_id=104,
        group_id=group_id or memory_matcher.CONFIG.target_group_id,
        message=message,
        original_message=message,
        raw_message=text,
        font=0,
        sender={"user_id": user_id, "nickname": "群友", "role": "member"},
    )


class MemberMemoryMatcherTests(unittest.IsolatedAsyncioTestCase):
    async def test_target_group_text_is_submitted_to_independent_batcher(self):
        event = _group_event()

        with patch.object(memory_matcher.BATCHER, "add") as add:
            await memory_matcher.collect_member_memory(event)

        add.assert_called_once_with(
            group_id=memory_matcher.CONFIG.target_group_id,
            user_id="456791",
            event_time=1785168002,
            callback=memory_matcher.analyze_member_memory,
        )

    async def test_commands_blank_self_and_outside_group_are_ignored(self):
        events = (
            _group_event(text="   "),
            _group_event(text="  /帮助"),
            _group_event(user_id=10000, self_id=10000),
            _group_event(group_id=memory_matcher.CONFIG.target_group_id + 1),
        )

        with patch.object(memory_matcher.BATCHER, "add") as add:
            for event in events:
                if memory_matcher._target_member_message(event):
                    await memory_matcher.collect_member_memory(event)

        add.assert_not_called()

    async def test_analysis_reads_recent_context_and_applies_only_target_member(self):
        context = [ContextMessage("小明", "我喜欢火锅", message_id="m1", user_id="7")]
        candidates = [
            {"user_id": "7", "trait": "喜欢火锅"},
            {"user_id": "8", "trait": "喜欢跑步"},
        ]
        with patch.object(
            memory_matcher, "recent_text_context", return_value=context
        ) as recent, patch.object(
            memory_matcher,
            "extract_memory_candidates",
            AsyncMock(return_value=candidates),
        ) as extract, patch.object(memory_matcher, "apply_candidates", return_value=1) as apply:
            await memory_matcher.analyze_member_memory(123, "7", 2000)

        recent.assert_called_once_with(
            memory_matcher.CONFIG.chat_archive_path,
            group_id=123,
            since_epoch=2000 - 1800,
            limit=20,
            exclude_message_id="",
            bot_user_id=str(memory_matcher.CONFIG.bot_self_id),
        )
        extract.assert_awaited_once_with(context)
        apply.assert_called_once_with(
            memory_matcher.CONFIG.chat_archive_path,
            memory_matcher.CONFIG.member_memory_root,
            group_id=123,
            context=context,
            candidates=[candidates[0]],
        )

    async def test_analysis_refreshes_summary_after_new_facts(self):
        context = [ContextMessage("小明", "我喜欢火锅", message_id="m1", user_id="7")]
        candidates = [{"user_id": "7", "trait": "喜欢火锅"}]
        config = SimpleNamespace(
            chat_archive_path=Path("/tmp/chat.db"),
            member_memory_root=Path("/tmp/member-memory"),
            bot_self_id="999",
            member_memory_summary_enabled=True,
        )
        with patch.object(memory_matcher, "CONFIG", config), patch.object(
            memory_matcher, "recent_text_context", return_value=context
        ), patch.object(
            memory_matcher, "extract_memory_candidates", AsyncMock(return_value=candidates)
        ), patch.object(
            memory_matcher, "apply_candidates", return_value=1
        ), patch.object(
            memory_matcher, "refresh_member_summary", AsyncMock(return_value=True)
        ) as refresh:
            await memory_matcher.analyze_member_memory(123, "7", 2000)

        refresh.assert_awaited_once_with(
            config.chat_archive_path, config.member_memory_root, group_id=123, user_id="7"
        )

    async def test_analysis_failure_is_caught_at_callback_boundary(self):
        with patch.object(
            memory_matcher, "recent_text_context", side_effect=RuntimeError("db failed")
        ), patch.object(memory_matcher.logger, "warning") as warning:
            await memory_matcher.analyze_member_memory(123, "7", 2000)

        warning.assert_called_once()


class MemberMemoryStoreTests(unittest.TestCase):
    def test_compact_profile_contains_summary_and_only_bounded_pending_facts(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "member_memory"
            db = Path(directory) / "chat.db"
            context = [
                ContextMessage("小明", f"我喜欢爱好{index}", message_id=f"m{index}", user_id="7")
                for index in range(10)
            ]
            candidates = [
                {
                    "user_id": "7",
                    "trait": f"爱好{index}",
                    "evidence_message_id": f"m{index}",
                    "quote": f"我喜欢爱好{index}",
                }
                for index in range(10)
            ]
            self.assertEqual(10, apply_candidates(db, root, group_id=123, context=context, candidates=candidates))
            all_traits = load_profiles(db, group_id=123, user_ids=["7"])[0].traits
            for index in range(10):
                remember_identity(db, root, group_id=123, user_id="7", nickname=f"名字{index}")
            self.assertTrue(commit_summary(
                db, root, group_id=123, user_id="7", previous_through_id=0,
                through_fact_id=all_traits[1].fact_id, summary="长期喜欢植物",
            ))

            with patch("plugins.member_memory.store._profile_row", side_effect=AssertionError):
                profile = load_profiles(db, group_id=123, user_ids=["7"], compact=True)[0]
            fallback = load_profiles(
                db, group_id=123, user_ids=["7"], compact=True, include_summary=False
            )[0]

            self.assertEqual("长期喜欢植物", profile.summary)
            self.assertEqual(8, len(profile.traits))
            self.assertEqual(5, len(profile.aliases))
            self.assertEqual("", fallback.summary)
            self.assertEqual(0, fallback.summary_through_fact_id)
            self.assertEqual(8, len(fallback.traits))
            self.assertEqual(5, len(fallback.aliases))
            self.assertTrue(commit_summary(
                db, root, group_id=123, user_id="7", previous_through_id=all_traits[1].fact_id,
                through_fact_id=all_traits[-1].fact_id, summary="长期喜欢植物",
            ))
            self.assertEqual(
                (), load_profiles(db, group_id=123, user_ids=["7"], compact=True)[0].traits
            )
    def test_store_imports_cleanly_in_fresh_process(self):
        env = os.environ.copy()
        env["TARGET_GROUP_ID"] = "975310864"
        completed = subprocess.run(
            [sys.executable, "-c", "import plugins.member_memory.store"],
            env=env,
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(0, completed.returncode, completed.stderr)

    def test_identity_keeps_all_historical_aliases(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "member_memory"
            db = Path(directory) / "chat.db"
            for index in range(10):
                remember_identity(db, root, group_id=123, user_id="7", nickname=f"名字{index}")
            profile = load_profiles(db, group_id=123, user_ids=["7"])[0]
            mirror = root / "123" / "7.json"

            self.assertEqual("名字9", profile.nickname)
            self.assertEqual(tuple(f"名字{i}" for i in range(9)), profile.aliases)
            self.assertTrue(mirror.is_file())
            self.assertEqual(0o600, mirror.stat().st_mode & 0o777)
            self.assertEqual("7", json.loads(mirror.read_text())["user_id"])

    def test_candidates_keep_every_valid_fact_beyond_legacy_limit(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "member_memory"
            db = Path(directory) / "chat.db"
            remember_identity(db, root, group_id=123, user_id="7", nickname="小明")
            context = [ContextMessage("小明", "我喜欢火锅", message_id="m1", user_id="7")]
            candidates = [
                {"user_id": "7", "trait": f"爱好{i}", "evidence_message_id": "m1", "quote": "我喜欢火锅"}
                for i in range(10)
            ]
            candidates.extend(
                [
                    {"user_id": "8", "trait": "冒充", "evidence_message_id": "m1", "quote": "我喜欢火锅"},
                    {"user_id": "7", "trait": "电话12345678901", "evidence_message_id": "m1", "quote": "我喜欢火锅"},
                    {"user_id": "7", "trait": "无证据", "evidence_message_id": "m1", "quote": "不存在"},
                ]
            )
            applied = apply_candidates(db, root, group_id=123, context=context, candidates=candidates)
            self.assertEqual(0, apply_candidates(db, root, group_id=123, context=context, candidates=candidates))
            profile = load_profiles(db, group_id=123, user_ids=["7"])[0]

            self.assertEqual(10, applied)
            self.assertEqual(10, len(profile.traits))
            self.assertNotIn("电话12345678901", [item.text for item in profile.traits])


    def test_legacy_profile_is_imported_into_append_only_ledger_before_update(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "member_memory"
            db = Path(directory) / "chat.db"
            legacy_traits = [
                {"text": "旧特性0", "evidence_message_id": "old0", "updated_at": "2026-01-01 00:00:00"},
                {"text": "旧特性1", "evidence_message_id": "old1", "updated_at": "2026-01-01 00:00:01"},
            ]
            with sqlite3.connect(db) as conn:
                conn.execute(
                    "CREATE TABLE member_memories (group_id INTEGER NOT NULL, user_id TEXT NOT NULL, "
                    "nickname TEXT NOT NULL, aliases_json TEXT NOT NULL, traits_json TEXT NOT NULL, "
                    "updated_at TEXT NOT NULL, PRIMARY KEY(group_id,user_id))"
                )
                conn.execute(
                    "INSERT INTO member_memories VALUES (?,?,?,?,?,?)",
                    (123, "7", "旧昵称", json.dumps(["更旧昵称"]), json.dumps(legacy_traits), "2026-01-01 00:00:00"),
                )
            context = [ContextMessage("新昵称", "我喜欢火锅", message_id="m1", user_id="7")]
            candidate = {"user_id": "7", "trait": "新特性", "evidence_message_id": "m1", "quote": "我喜欢火锅"}

            self.assertEqual(1, apply_candidates(db, root, group_id=123, context=context, candidates=[candidate]))
            self.assertEqual(0, apply_candidates(db, root, group_id=123, context=context, candidates=[candidate]))
            profile = load_profiles(db, group_id=123, user_ids=["7"])[0]

            self.assertEqual(("更旧昵称", "旧昵称"), profile.aliases)
            self.assertEqual(["旧特性0", "旧特性1", "新特性"], [item.text for item in profile.traits])
            with sqlite3.connect(db) as conn:
                self.assertEqual(3, conn.execute("SELECT COUNT(*) FROM member_memory_facts").fetchone()[0])
                self.assertEqual(2, conn.execute("SELECT COUNT(*) FROM member_memory_aliases").fetchone()[0])


    def test_mirror_failure_does_not_rollback_committed_memory(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "member_memory"
            db = Path(directory) / "chat.db"
            with patch("plugins.member_memory.store.os.replace", side_effect=OSError("disk full")):
                profile = remember_identity(db, root, group_id=123, user_id="7", nickname="小明")
            self.assertEqual("小明", profile.nickname)
            self.assertEqual("小明", load_profiles(db, group_id=123, user_ids=["7"])[0].nickname)


    def test_mirror_directory_failure_does_not_escape(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "member_memory"
            db = Path(directory) / "chat.db"
            profile = remember_identity(db, root, group_id=123, user_id="7", nickname="小明")
            with patch("plugins.member_memory.store.Path.mkdir", side_effect=OSError("read-only")):
                _write_mirror(db, root, profile.group_id, profile.user_id)
            self.assertEqual("小明", load_profiles(db, group_id=123, user_ids=["7"])[0].nickname)


class _Response:
    def raise_for_status(self):
        return None

    def json(self):
        return {
            "choices": [
                {
                    "message": {
                        "content": json.dumps(
                            {
                                "memories": [
                                    {
                                        "user_id": "7",
                                        "trait": "喜欢火锅",
                                        "evidence_message_id": "m1",
                                        "quote": "我喜欢火锅",
                                    }
                                ]
                            },
                            ensure_ascii=False,
                        )
                    }
                }
            ]
        }


class _Client:
    posted = None

    def __init__(self, *, timeout):
        self.timeout = timeout

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False

    async def post(self, url, *, headers, json):
        type(self).posted = json
        return _Response()


class MemberMemoryAITests(unittest.IsolatedAsyncioTestCase):
    async def test_extracts_structured_candidates_with_conservative_prompt(self):
        config = SimpleNamespace(
            ai_api_key="secret",
            ai_base_url="https://ai.example.com",
            ai_model="chat-model",
            ai_timeout=12,
        )
        context = [ContextMessage("小明", "我喜欢火锅", message_id="m1", user_id="7")]
        with patch("plugins.member_memory.ai.CONFIG", config), patch(
            "plugins.member_memory.ai.httpx.AsyncClient", _Client
        ):
            result = await extract_memory_candidates(context)

        self.assertEqual("喜欢火锅", result[0]["trait"])
        prompt = _Client.posted["messages"][0]["content"]
        self.assertIn("只记录说话者本人明确表达", prompt)
        self.assertIn("不要记录敏感信息", prompt)

    async def test_malformed_output_returns_empty(self):
        class BadResponse(_Response):
            def json(self):
                return {"choices": [{"message": {"content": "not-json"}}]}

        class BadClient(_Client):
            async def post(self, url, *, headers, json):
                return BadResponse()

        config = SimpleNamespace(
            ai_api_key="secret",
            ai_base_url="https://ai.example.com",
            ai_model="chat-model",
            ai_timeout=12,
        )
        with patch("plugins.member_memory.ai.CONFIG", config), patch(
            "plugins.member_memory.ai.httpx.AsyncClient", BadClient
        ):
            self.assertEqual([], await extract_memory_candidates([]))


if __name__ == "__main__":
    unittest.main()
