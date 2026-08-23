from __future__ import annotations

import unittest

from plugins.chat_archive.db import ContextMessage
from plugins.member_memory.store import MemberProfile


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


if __name__ == "__main__":
    unittest.main()
