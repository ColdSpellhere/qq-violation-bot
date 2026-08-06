import os
import subprocess
import sys
import unittest
from types import SimpleNamespace
from unittest.mock import patch

os.environ.setdefault("TARGET_GROUP_ID", "999000111")

from plugins.random_chat.ai import RandomChatAIError, generate_reply
from plugins.member_memory.store import MemberProfile, MemoryTrait
from plugins.random_chat.policy import eligible_text, is_candidate, should_reply
from plugins.chat_archive.db import ContextMessage


class RandomChatPolicyTests(unittest.TestCase):
    def test_candidate_requires_enabled_target_group_and_human_sender(self):
        self.assertTrue(is_candidate(True, 100, 100, 20, 10))
        self.assertFalse(is_candidate(False, 100, 100, 20, 10))
        self.assertFalse(is_candidate(True, 100, 200, 20, 10))
        self.assertFalse(is_candidate(True, 100, 100, 10, 10))

    def test_configuration_defaults_are_safe(self):
        env = os.environ.copy()
        env["TARGET_GROUP_ID"] = "999000111"
        env.pop("RANDOM_CHAT_ENABLED", None)
        env.pop("RANDOM_CHAT_PROBABILITY", None)
        completed = subprocess.run(
            [
                sys.executable,
                "-c",
                (
                    "from plugins.violation_record.config import CONFIG; "
                    "assert CONFIG.random_chat_enabled is False; "
                    "assert CONFIG.random_chat_probability == 0.05"
                ),
            ],
            env=env,
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(0, completed.returncode, completed.stderr)

    def test_rejects_empty_command_and_at_bot(self):
        self.assertIsNone(eligible_text("   ", at_bot=False))
        self.assertIsNone(eligible_text("/help", at_bot=False))
        self.assertIsNone(eligible_text("你好", at_bot=True))

    def test_accepts_plain_text(self):
        self.assertEqual(eligible_text("  大家晚上好  ", at_bot=False), "大家晚上好")

    def test_probability_boundaries(self):
        self.assertFalse(should_reply(0.0, sample=0.0))
        self.assertTrue(should_reply(0.05, sample=0.049))
        self.assertFalse(should_reply(0.05, sample=0.05))
        self.assertTrue(should_reply(1.0, sample=0.999))


class _FakeResponse:
    def __init__(self, content):
        self.content = content

    def raise_for_status(self):
        return None

    def json(self):
        return {"choices": [{"message": {"content": self.content}}]}


class _FakeClient:
    response_content = "  可以吃点热乎的。  "
    posted = None
    error = None

    def __init__(self, *, timeout):
        self.timeout = timeout

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False

    async def post(self, url, *, headers, json):
        type(self).posted = (url, headers, json, self.timeout)
        if type(self).error:
            raise type(self).error
        return _FakeResponse(type(self).response_content)


class RandomChatAITests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        _FakeClient.response_content = "  可以吃点热乎的。  "
        _FakeClient.posted = None
        _FakeClient.error = None
        self.config = SimpleNamespace(
            ai_api_key="secret",
            ai_base_url="https://ai.example.com",
            ai_model="chat-model",
            ai_timeout=12,
        )

    async def test_generates_short_free_text_reply(self):
        with patch("plugins.random_chat.ai.CONFIG", self.config), patch(
            "plugins.random_chat.ai.httpx.AsyncClient", _FakeClient
        ):
            reply = await generate_reply(
                "今晚吃什么",
                context=[
                    ContextMessage("小明", "想吃火锅", message_id="1", user_id="11"),
                    ContextMessage(
                        "小红",
                        "我也想",
                        message_id="2",
                        user_id="22",
                        at_user_ids=("11",),
                    ),
                ],
                current=ContextMessage("小刚", "今晚吃什么", message_id="3", user_id="33"),
                profiles=[
                    MemberProfile(
                        group_id=100,
                        user_id="11",
                        nickname="小明",
                        aliases=(),
                        traits=(MemoryTrait("喜欢火锅", "1", "2026-08-06 00:00:00"),),
                        updated_at="2026-08-06 00:00:00",
                    )
                ],
            )

        self.assertEqual(reply, "可以吃点热乎的。")
        url, headers, payload, timeout = _FakeClient.posted
        self.assertEqual(url, "https://ai.example.com/v1/chat/completions")
        self.assertEqual(headers["Authorization"], "Bearer secret")
        self.assertEqual(payload["model"], "chat-model")
        system_prompt = payload["messages"][0]["content"]
        self.assertIn("真实的 QQ 群聊", system_prompt)
        self.assertIn("SKIP", system_prompt)
        self.assertIn("不固定使用", system_prompt)
        self.assertIn("只输出最终群消息或 SKIP", system_prompt)
        user_content = payload["messages"][1]["content"]
        self.assertLess(user_content.index("小明[QQ:11]"), user_content.index("小红[QQ:22]"))
        self.assertIn("艾特:QQ:11", user_content)
        self.assertIn("小刚[QQ:33]", user_content)
        self.assertIn("喜欢火锅", user_content)
        self.assertIn("群友之间说的话不等于对你说", system_prompt)
        self.assertEqual(timeout, 12)

    async def test_returns_none_for_missing_key_or_empty_content(self):
        self.config.ai_api_key = ""
        with patch("plugins.random_chat.ai.CONFIG", self.config):
            self.assertIsNone(await generate_reply("你好", context=[]))

        self.config.ai_api_key = "secret"
        _FakeClient.response_content = "   "
        with patch("plugins.random_chat.ai.CONFIG", self.config), patch(
            "plugins.random_chat.ai.httpx.AsyncClient", _FakeClient
        ):
            self.assertIsNone(await generate_reply("你好", context=[]))

    async def test_suppresses_skip_and_repetitive_haha_openers(self):
        for content in ("SKIP", " skip ", "哈哈，确实是这样", "哈哈, 可以试试"):
            _FakeClient.response_content = content
            with self.subTest(content=content), patch(
                "plugins.random_chat.ai.CONFIG", self.config
            ), patch("plugins.random_chat.ai.httpx.AsyncClient", _FakeClient):
                self.assertIsNone(await generate_reply("你好", context=[]))

    async def test_wraps_transport_errors(self):
        _FakeClient.error = RuntimeError("network down")
        with patch("plugins.random_chat.ai.CONFIG", self.config), patch(
            "plugins.random_chat.ai.httpx.AsyncClient", _FakeClient
        ):
            with self.assertRaisesRegex(RandomChatAIError, "network down"):
                await generate_reply("你好", context=[])


if __name__ == "__main__":
    unittest.main()
