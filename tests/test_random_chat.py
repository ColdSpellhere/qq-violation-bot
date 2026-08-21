import os
import subprocess
import sys
import unittest
from types import SimpleNamespace
from unittest.mock import patch

os.environ.setdefault("TARGET_GROUP_ID", "999000111")

from plugins.chat_archive.db import ContextMessage
from plugins.chat_vision.client import VisionImage
from plugins.member_memory.store import MemberProfile, MemoryTrait
from plugins.random_chat.ai import RandomChatAIError, generate_reply
from plugins.random_chat.policy import eligible_text, is_candidate, should_reply


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
        env.pop("RANDOM_CHAT_DIRECT_FALLBACK_ENABLED", None)
        completed = subprocess.run(
            [
                sys.executable,
                "-c",
                (
                    "from plugins.violation_record.config import CONFIG; "
                    "assert CONFIG.random_chat_enabled is False; "
                    "assert CONFIG.member_memory_summary_enabled is False; "
                    "assert CONFIG.random_chat_probability == 0.05; "
                    "assert CONFIG.random_chat_direct_fallback_enabled is False"
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
            chat_vision_model="vision-model",
            chat_vision_timeout=29,
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
                        summary="长期喜欢植物",
                    )
                ],
            )

        self.assertEqual(reply, "可以吃点热乎的。")
        url, headers, payload, timeout = _FakeClient.posted
        self.assertEqual(url, "https://ai.example.com/v1/chat/completions")
        self.assertEqual(headers["Authorization"], "Bearer secret")
        self.assertEqual(payload["model"], "chat-model")
        self.assertEqual(0.8, payload["temperature"])
        self.assertNotIn("thinking", payload)
        system_prompt = payload["messages"][0]["content"]
        self.assertIn("真实的 QQ 群聊", system_prompt)
        self.assertIn("SKIP", system_prompt)
        self.assertIn("不固定使用", system_prompt)
        self.assertIn("只输出最终群消息或 SKIP", system_prompt)
        user_content = payload["messages"][1]["content"]
        self.assertIsInstance(user_content, str)
        self.assertLess(user_content.index("小明[QQ:11]"), user_content.index("小红[QQ:22]"))
        self.assertIn("艾特:QQ:11", user_content)
        self.assertIn("小刚[QQ:33]", user_content)
        self.assertIn("喜欢火锅", user_content)
        self.assertIn("记忆摘要:长期喜欢植物", user_content)
        self.assertIn("新增特性:喜欢火锅", user_content)
        self.assertIn("群友之间说的话不等于对你说", system_prompt)
        self.assertIn("萝卜猫", system_prompt)
        self.assertIn("花和植物", system_prompt)
        self.assertIn("反二梦女", system_prompt)
        self.assertIn("萝卜猫只是你的名字", system_prompt)
        self.assertIn("你不是猫", system_prompt)
        self.assertIn("不要使用“喵”", system_prompt)
        self.assertIn("反二梦女是你认可的兴趣和自我标签", system_prompt)
        self.assertIn("不是另一个名字", system_prompt)
        self.assertIn("不要每句话都卖萌", system_prompt)
        self.assertEqual(timeout, 12)

    async def test_images_use_vision_model_and_openai_multimodal_content(self) -> None:
        images = (
            VisionImage(b"first-image", "image/jpeg", "current", 1),
            VisionImage(b"second-image", "image/png", "quoted", 2),
        )
        with patch("plugins.random_chat.ai.CONFIG", self.config), patch(
            "plugins.random_chat.ai.httpx.AsyncClient", _FakeClient
        ):
            try:
                await generate_reply("看看图片", context=[], images=images)
            except TypeError as exc:
                self.fail(f"generate_reply must accept raw images: {exc}")

        payload = _FakeClient.posted[2]
        self.assertEqual(29, _FakeClient.posted[3])
        self.assertEqual("vision-model", payload["model"])
        self.assertEqual({"type": "disabled"}, payload["thinking"])
        self.assertNotIn("temperature", payload)
        user_content = payload["messages"][1]["content"]
        self.assertEqual("text", user_content[0]["type"])
        self.assertIn("当前消息：看看图片", user_content[0]["text"])
        self.assertEqual(
            [
                {
                    "type": "image_url",
                    "image_url": {
                        "url": "data:image/jpeg;base64,Zmlyc3QtaW1hZ2U="
                    },
                },
                {
                    "type": "image_url",
                    "image_url": {
                        "url": "data:image/png;base64,c2Vjb25kLWltYWdl"
                    },
                },
            ],
            user_content[1:],
        )

    def test_vision_image_is_an_immutable_raw_image_value(self) -> None:
        image = VisionImage(b"bytes", "image/jpeg", "m1", 3)
        self.assertEqual(b"bytes", image.content)
        self.assertEqual("image/jpeg", image.mime_type)
        self.assertEqual("m1", image.message_id)
        self.assertEqual(3, image.ordinal)
        with self.assertRaises((AttributeError, TypeError)):
            image.ordinal = 4

    async def test_addressed_mode_requires_a_natural_answer(self):
        with patch("plugins.random_chat.ai.CONFIG", self.config), patch(
            "plugins.random_chat.ai.httpx.AsyncClient", _FakeClient
        ):
            await generate_reply("你叫什么", context=[], addressed=True)

        system_prompt = _FakeClient.posted[2]["messages"][0]["content"]
        self.assertIn("这条消息明确在对你说", system_prompt)
        self.assertIn("不要输出 SKIP", system_prompt)
        self.assertNotIn("无法确定时输出 SKIP", system_prompt)
        self.assertNotIn("最终群消息或 SKIP", system_prompt)

    async def test_private_mode_uses_one_to_one_prompt_without_group_language(self):
        with patch("plugins.random_chat.ai.CONFIG", self.config), patch(
            "plugins.random_chat.ai.httpx.AsyncClient", _FakeClient
        ):
            await generate_reply(
                "在吗",
                context=[],
                current=ContextMessage("测试者", "在吗", message_id="p1", user_id="123"),
                addressed=True,
                chat_mode="private",
            )

        payload = _FakeClient.posted[2]
        system_prompt = payload["messages"][0]["content"]
        user_prompt = payload["messages"][1]["content"]
        self.assertIn("一对一 QQ 私聊", system_prompt)
        self.assertNotIn("QQ 群聊", system_prompt)
        self.assertNotIn("群友", system_prompt)
        self.assertIn("不要输出 SKIP", system_prompt)
        self.assertIn("萝卜猫只是你的名字", system_prompt)
        self.assertIn("你不是猫", system_prompt)
        self.assertIn("不要使用“喵”", system_prompt)
        self.assertIn("反二梦女是你认可的兴趣和自我标签", system_prompt)
        self.assertIn("不是另一个名字", system_prompt)
        self.assertIn("近期私聊", user_prompt)
        self.assertNotIn("近期群聊", user_prompt)

    async def test_loads_character_prompt_for_every_ai_request(self):
        with patch("plugins.random_chat.ai.CONFIG", self.config), patch(
            "plugins.random_chat.ai.httpx.AsyncClient", _FakeClient
        ), patch(
            "plugins.random_chat.ai.load_character_prompt",
            side_effect=["角色版本一", "角色版本二"],
        ) as loader:
            await generate_reply("第一条", context=[])
            first_prompt = _FakeClient.posted[2]["messages"][0]["content"]
            await generate_reply("第二条", context=[])
            second_prompt = _FakeClient.posted[2]["messages"][0]["content"]

        self.assertIn("角色版本一", first_prompt)
        self.assertNotIn("角色版本二", first_prompt)
        self.assertIn("角色版本二", second_prompt)
        self.assertNotIn("角色版本一", second_prompt)
        self.assertEqual(2, loader.call_count)

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
