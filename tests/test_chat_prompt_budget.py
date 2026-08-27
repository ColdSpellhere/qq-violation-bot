from __future__ import annotations

import unittest

from plugins.chat_archive.db import ContextMessage
from plugins.member_memory.store import MemberProfile, MemoryTrait
from plugins.private_memory.models import ConversationScope, RelationshipState


def message(index: int, text: str) -> ContextMessage:
    return ContextMessage(
        nickname=f"member-{index}",
        user_id=str(1000 + index),
        message_id=f"m-{index}",
        text=text,
        at_user_ids=("2000",),
        reply_message_id=f"r-{index}",
        replied_to_user_id="3000",
    )


def profile(index: int, text: str) -> MemberProfile:
    return MemberProfile(
        group_id=1,
        user_id=str(1000 + index),
        nickname=f"member-{index}",
        aliases=(),
        traits=(MemoryTrait(text, f"m-{index}", "now"),),
        updated_at="now",
    )


def relationship(text: str) -> RelationshipState:
    return RelationshipState(
        id=1,
        scope=ConversationScope("group", "1001", group_id=1),
        state_text=text,
        open_topics=(),
        preferred_address="小花",
        communication_style="自然聊天",
        source_message_id="m-1",
        source_watermark=1,
        version=1,
        created_at="now",
        updated_at="now",
    )


class ChatPromptBudgetTests(unittest.TestCase):
    def build_input(self, **overrides):
        from plugins.chat_prompt.models import ChatPromptInput

        values = {
            "mode": "group",
            "now_text": "2026-08-23 04:00:00 +08:00",
            "persona": "萝卜猫",
            "context": (message(1, "你好"),),
            "profiles": (profile(1, "喜欢花"),),
            "relationship": relationship("刚刚聊过植物"),
            "open_topics": ("下次继续聊月季",),
            "image_descriptions": ("一盆开花的月季",),
            "current": message(99, "这朵花叫什么"),
            "addressed": True,
        }
        values.update(overrides)
        return ChatPromptInput(**values)

    def test_category_caps_are_exact_and_inputs_are_not_mutated(self) -> None:
        from plugins.chat_prompt.budget import apply_prompt_budget, prompt_data_chars
        from plugins.chat_prompt.models import PromptBudget

        original = self.build_input(
            persona="p" * 2100,
            context=tuple(message(i, "c" * 350) for i in range(25)),
            profiles=tuple(profile(i, "f" * 500) for i in range(5)),
            relationship=relationship("r" * 800),
            open_topics=tuple("t" * 100 for _ in range(8)),
            image_descriptions=tuple("i" * 800 for _ in range(4)),
            current=message(99, "u" * 2200),
        )
        budget = PromptBudget(total_chars=20_000)

        result = apply_prompt_budget(original, budget)

        self.assertEqual(2000, len(result.persona))
        self.assertLessEqual(len(result.context), 20)
        self.assertLessEqual(sum(len(item) for item in result.context), 6000)
        self.assertLessEqual(sum(len(item) for item in result.facts), 1200)
        self.assertLessEqual(len(result.relationship), 600)
        self.assertLessEqual(len(result.open_topics), 5)
        self.assertLessEqual(sum(map(len, result.open_topics)), 400)
        self.assertLessEqual(sum(map(len, result.image_descriptions)), 2000)
        self.assertEqual(2000, len(result.current))
        self.assertLessEqual(prompt_data_chars(result), 20_000)
        self.assertEqual(2100, len(original.persona))
        self.assertEqual(25, len(original.context))

    def test_context_drops_oldest_first_and_keeps_newest_deterministically(self) -> None:
        from plugins.chat_prompt.budget import apply_prompt_budget
        from plugins.chat_prompt.models import PromptBudget

        source = self.build_input(
            context=tuple(message(i, f"marker-{i}-" + "x" * 300) for i in range(24)),
        )
        budget = PromptBudget(context_messages=3, context_chars=10_000)

        first = apply_prompt_budget(source, budget)
        second = apply_prompt_budget(source, budget)

        self.assertEqual(first, second)
        self.assertEqual(("m-21", "m-22", "m-23"), first.context_message_ids)
        self.assertEqual(21, first.truncation.context_messages_removed)

    def test_total_budget_removes_optional_data_before_recent_context(self) -> None:
        from plugins.chat_prompt.budget import apply_prompt_budget, prompt_data_chars
        from plugins.chat_prompt.models import PromptBudget

        source = self.build_input(
            persona="p" * 100,
            context=tuple(message(i, "c" * 220) for i in range(5)),
            profiles=(profile(1, "f" * 100),),
            relationship=relationship("r" * 100),
            open_topics=("t" * 80,),
            image_descriptions=("i" * 100,),
            current=message(99, "CURRENT-MUST-STAY"),
        )
        without_context = self.build_input(
            persona=source.persona,
            context=(),
            profiles=source.profiles,
            relationship=source.relationship,
            open_topics=source.open_topics,
            image_descriptions=source.image_descriptions,
            current=source.current,
        )
        baseline = apply_prompt_budget(without_context, PromptBudget(total_chars=20_000))
        budget = PromptBudget(total_chars=prompt_data_chars(baseline) + 5)

        result = apply_prompt_budget(source, budget)

        self.assertTrue(result.context)
        self.assertGreater(result.truncation.persona_chars_removed, 0)
        self.assertFalse(result.image_descriptions)
        self.assertEqual("CURRENT-MUST-STAY", result.current_text)
        self.assertLessEqual(prompt_data_chars(result), budget.total_chars)

    def test_tiny_total_never_removes_direction_safety_contract_or_current(self) -> None:
        from plugins.chat_prompt.budget import apply_prompt_budget, prompt_data_chars
        from plugins.chat_prompt.models import PromptBudget

        source = self.build_input(
            persona="persona",
            context=(message(1, "context"),),
            profiles=(profile(1, "fact"),),
            relationship=relationship("relationship"),
            open_topics=("topic",),
            image_descriptions=("image",),
            current=message(99, "CURRENT"),
            addressed=False,
        )
        result = apply_prompt_budget(source, PromptBudget(total_chars=7))

        self.assertEqual("CURRENT", result.current_text)
        self.assertEqual("group", result.mode)
        self.assertFalse(result.addressed)
        self.assertTrue(result.safety_required)
        self.assertTrue(result.direction_required)
        self.assertTrue(result.output_contract_required)
        self.assertLessEqual(prompt_data_chars(result), 7)

    def test_models_reject_invalid_modes_and_non_positive_budgets(self) -> None:
        from plugins.chat_prompt.models import ChatPromptInput, PromptBudget

        with self.assertRaises(ValueError):
            ChatPromptInput(**{**self.build_input().__dict__, "mode": "business"})
        with self.assertRaises(ValueError):
            PromptBudget(context_messages=0)
        with self.assertRaises(ValueError):
            PromptBudget(total_chars=True)


if __name__ == "__main__":
    unittest.main()
