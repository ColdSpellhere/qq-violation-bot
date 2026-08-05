import unittest

from plugins.random_chat.policy import eligible_text, should_reply


class RandomChatPolicyTests(unittest.TestCase):
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


if __name__ == "__main__":
    unittest.main()
