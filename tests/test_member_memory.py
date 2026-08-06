import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

os.environ.setdefault("TARGET_GROUP_ID", "999000111")

from plugins.chat_archive.db import ContextMessage
from plugins.member_memory.ai import extract_memory_candidates
from plugins.member_memory.store import (
    MemberProfile,
    MemoryTrait,
    apply_candidates,
    load_profiles,
    remember_identity,
)


class MemberMemoryStoreTests(unittest.TestCase):
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

    def test_identity_keeps_bounded_aliases_and_writes_private_mirror(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "member_memory"
            db = Path(directory) / "chat.db"
            remember_identity(db, root, group_id=123, user_id="7", nickname="旧名")
            remember_identity(db, root, group_id=123, user_id="7", nickname="新名")
            profile = load_profiles(db, group_id=123, user_ids=["7"])[0]
            mirror = root / "123" / "7.json"

            self.assertEqual("新名", profile.nickname)
            self.assertIn("旧名", profile.aliases)
            self.assertTrue(mirror.is_file())
            self.assertEqual(0o600, mirror.stat().st_mode & 0o777)
            self.assertEqual("7", json.loads(mirror.read_text())["user_id"])

    def test_candidates_require_matching_first_party_evidence_and_are_bounded(self):
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
            profile = load_profiles(db, group_id=123, user_ids=["7"])[0]

            self.assertEqual(8, applied)
            self.assertEqual(8, len(profile.traits))
            self.assertNotIn("电话12345678901", [item.text for item in profile.traits])


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
