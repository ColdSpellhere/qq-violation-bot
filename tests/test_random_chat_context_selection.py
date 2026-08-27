from __future__ import annotations

import unittest

from plugins.chat_archive.db import ContextMessage
from plugins.random_chat.context import select_chat_context


class ChatContextSelectionTests(unittest.TestCase):
    def test_causal_anchors_never_expand_result_past_limit(self) -> None:
        history = [
            item
            for index in range(10)
            for item in (
                ContextMessage(
                    f"成员{index}",
                    f"问题{index}",
                    message_id=f"trigger-{index}",
                    user_id=str(100 + index),
                ),
                ContextMessage(
                    "机器人自己",
                    f"回答{index}",
                    message_id=f"bot-{index}",
                    user_id="999",
                    reply_message_id=f"trigger-{index}",
                    is_bot=True,
                ),
            )
        ]

        selected = select_chat_context(
            history,
            limit=5,
            max_self_messages=10,
        )

        selected_ids = [item.message_id for item in selected]
        self.assertEqual(5, len(selected_ids))
        self.assertIn("bot-9", selected_ids)
        self.assertIn("trigger-9", selected_ids)

    def test_spam_filter_does_not_drop_image_or_explicit_reply_turns(self) -> None:
        history = [
            ContextMessage(
                "成员",
                "@kona @kona @kona 看这个",
                message_id="image-turn",
                user_id="101",
                image_descriptions=("一朵花",),
            ),
            ContextMessage(
                "成员",
                "@kona @kona @kona 看这个",
                message_id="reply-turn",
                user_id="101",
                reply_message_id="source",
            ),
            ContextMessage(
                "成员",
                "@kona @kona @kona 看这个",
                message_id="plain-turn",
                user_id="101",
            ),
        ]

        selected = select_chat_context(history, limit=20)

        self.assertEqual(
            ["image-turn", "reply-turn", "plain-turn"],
            [item.message_id for item in selected],
        )


if __name__ == "__main__":
    unittest.main()
