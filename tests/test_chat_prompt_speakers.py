from __future__ import annotations

import unittest
import re

from plugins.chat_archive.db import ContextMessage
from plugins.member_memory.store import MemberProfile, MemoryTrait


def turn(
    user_id: str,
    nickname: str,
    text: str,
    *,
    message_id: str,
    at_user_ids: tuple[str, ...] = (),
    replied_to_user_id: str | None = None,
) -> ContextMessage:
    return ContextMessage(
        nickname=nickname,
        text=text,
        message_id=message_id,
        user_id=user_id,
        at_user_ids=at_user_ids,
        replied_to_user_id=replied_to_user_id,
    )


def profile(user_id: str, nickname: str) -> MemberProfile:
    return MemberProfile(
        group_id=1,
        user_id=user_id,
        nickname=nickname,
        aliases=(),
        traits=(),
        updated_at="now",
    )


class SpeakerDirectoryTests(unittest.TestCase):
    def test_current_sender_is_s1_and_known_users_follow_deterministically(self) -> None:
        from plugins.chat_prompt.speakers import build_speaker_directory

        current = turn(
            "200",
            "乙",
            "当前",
            message_id="m-current",
            at_user_ids=("300",),
            replied_to_user_id="100",
        )
        directory = build_speaker_directory(
            current=current,
            context=(turn("100", "甲", "历史", message_id="m-old"),),
            profiles=(profile("400", "丁"),),
        )

        self.assertEqual("S1", directory.ref_for_user("200"))
        self.assertEqual("S2", directory.ref_for_user("100"))
        self.assertEqual("S3", directory.ref_for_user("300"))
        self.assertEqual("S4", directory.ref_for_user("400"))
        self.assertTrue(directory.identities[0].current)

    def test_same_qq_reuses_ref_across_nickname_change(self) -> None:
        from plugins.chat_prompt.speakers import build_speaker_directory

        directory = build_speaker_directory(
            current=turn("200", "新昵称", "现在", message_id="m2"),
            context=(turn("200", "旧昵称", "之前", message_id="m1"),),
            profiles=(),
        )

        self.assertEqual("S1", directory.ref_for_user("200"))
        self.assertEqual(
            1, len([item for item in directory.identities if item.user_id == "200"])
        )
        self.assertEqual("新昵称", directory.identities[0].nickname)

    def test_same_nickname_different_qq_gets_distinct_refs(self) -> None:
        from plugins.chat_prompt.speakers import build_speaker_directory

        directory = build_speaker_directory(
            current=turn("200", "同名", "当前", message_id="m2"),
            context=(turn("100", "同名", "历史", message_id="m1"),),
            profiles=(),
        )

        self.assertNotEqual(
            directory.ref_for_user("200"), directory.ref_for_user("100")
        )

    def test_unknown_history_never_merges_with_current_or_other_unknown(self) -> None:
        from plugins.chat_prompt.speakers import build_speaker_directory

        first = turn("", "未知", "第一条", message_id="u1")
        second = turn("", "未知", "第二条", message_id="u2")
        directory = build_speaker_directory(
            current=turn("200", "乙", "当前", message_id="m2"),
            context=(first, second),
            profiles=(),
        )

        self.assertEqual("S1", directory.ref_for_user("200"))
        self.assertEqual("U1", directory.ref_for_message("u1"))
        self.assertEqual("U2", directory.ref_for_message("u2"))
        self.assertNotEqual(
            directory.ref_for_message("u1"), directory.ref_for_message("u2")
        )


class SpeakerPromptRenderingTests(unittest.TestCase):
    def build_input(
        self,
        *,
        current: ContextMessage,
        context: tuple[ContextMessage, ...] = (),
        profiles: tuple[MemberProfile, ...] = (),
    ):
        from plugins.chat_prompt.models import ChatPromptInput

        return ChatPromptInput(
            mode="group",
            now_text="2026-08-23T08:30+08:00",
            persona="萝卜猫",
            context=context,
            profiles=profiles,
            relationship=None,
            open_topics=(),
            image_descriptions=(),
            current=current,
            addressed=True,
        )

    def test_first_person_stays_with_history_author(self) -> None:
        from plugins.chat_prompt.builder import build_chat_prompt

        prompt = build_chat_prompt(
            self.build_input(
                current=turn("200", "乙", "他说的花是什么？", message_id="m2"),
                context=(turn("100", "甲", "我喜欢养花", message_id="m1"),),
            )
        )

        system = str(prompt.messages[0]["content"])
        user = str(prompt.messages[1]["content"])
        self.assertIn("第一人称", system)
        self.assertIn("不同 speaker_ref", system)
        self.assertIn("S1|qq=200|nickname=乙|current=true", user)
        self.assertIn("S2|qq=100|nickname=甲", user)
        self.assertIn('"speaker_ref":"S2"', user)
        self.assertIn("我喜欢养花", user)
        self.assertIn('"current_speaker_ref":"S1"', user)

    def test_reply_author_does_not_replace_current_sender(self) -> None:
        from plugins.chat_prompt.builder import build_chat_prompt

        prompt = build_chat_prompt(
            self.build_input(
                current=turn(
                    "200",
                    "乙",
                    "这句话呢",
                    message_id="m2",
                    replied_to_user_id="100",
                ),
                context=(turn("100", "甲", "我喜欢养花", message_id="m1"),),
            )
        )

        user = str(prompt.messages[1]["content"])
        self.assertIn('"current_speaker_ref":"S1"', user)
        self.assertIn('"reply_author_ref":"S2"', user)

    def test_member_fact_uses_same_ref_as_its_history_author(self) -> None:
        from plugins.chat_prompt.builder import build_chat_prompt

        remembered = MemberProfile(
            group_id=1,
            user_id="100",
            nickname="甲",
            aliases=(),
            traits=(MemoryTrait("喜欢花", "m1", "now"),),
            updated_at="now",
        )
        prompt = build_chat_prompt(
            self.build_input(
                current=turn("200", "乙", "继续", message_id="m2"),
                context=(turn("100", "甲", "我喜欢养花", message_id="m1"),),
                profiles=(remembered,),
            )
        )

        user = str(prompt.messages[1]["content"])
        self.assertRegex(
            user,
            r'<member_memory_data>[^<]*"speaker_ref":"S2"[^<]*喜欢花',
        )
        self.assertNotRegex(
            user,
            r'<member_memory_data>[^<]*"speaker_ref":"S1"[^<]*喜欢花',
        )

    def test_escape_heavy_budget_has_no_dangling_speaker_refs(self) -> None:
        from plugins.chat_prompt.builder import build_chat_prompt

        context = tuple(
            turn(str(100 + index), f"成员<{index}", "<&" * 500, message_id=f"m{index}")
            for index in range(20)
        )
        prompt = build_chat_prompt(
            self.build_input(
                current=turn("999", "当前<&", "当前消息", message_id="current"),
                context=context,
            )
        )
        user = str(prompt.messages[1]["content"])
        directory_match = re.search(
            r"<speaker_directory_data>(.*?)</speaker_directory_data>", user, re.S
        )
        self.assertIsNotNone(directory_match)
        directory_refs = set(re.findall(r"(?:S|U)\d+", directory_match.group(1)))
        referenced = set(re.findall(r'"speaker_ref":"((?:S|U)\d+)"', user))
        current_refs = set(
            re.findall(r'"current_speaker_ref":"((?:S|U)\d+)"', user)
        )
        self.assertTrue(referenced | current_refs)
        self.assertEqual(referenced | current_refs, directory_refs)
        self.assertLessEqual(prompt.total_chars, 12_000)
        self.assertIn("当前消息", user)


if __name__ == "__main__":
    unittest.main()
