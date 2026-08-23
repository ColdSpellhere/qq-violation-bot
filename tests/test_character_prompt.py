import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from plugins.random_chat.persona import DEFAULT_CHARACTER_PROMPT, load_character_prompt


class CharacterPromptTests(unittest.TestCase):
    def test_default_path_is_reloaded_from_current_instance_root(self):
        from plugins.random_chat import persona

        with tempfile.TemporaryDirectory() as temp_dir:
            instance_file = Path(temp_dir) / "character.md"
            instance_file.write_text("kona 第一版", encoding="utf-8")
            with patch.object(persona, "CHARACTER_FILE", instance_file):
                self.assertEqual("kona 第一版", persona.load_character_prompt())
                instance_file.write_text("kona 第二版", encoding="utf-8")
                self.assertEqual("kona 第二版", persona.load_character_prompt())

    def test_reads_utf8_markdown_and_strips_outer_whitespace(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "character.md"
            path.write_text("\n# 新角色\n\n喜欢薄荷。\n", encoding="utf-8")

            self.assertEqual("# 新角色\n\n喜欢薄荷。", load_character_prompt(path))

    def test_reads_file_again_on_every_call(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "character.md"
            path.write_text("第一版", encoding="utf-8")
            self.assertEqual("第一版", load_character_prompt(path))

            path.write_text("第二版", encoding="utf-8")
            self.assertEqual("第二版", load_character_prompt(path))

    def test_missing_empty_and_invalid_utf8_use_default(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "character.md"
            self.assertEqual(DEFAULT_CHARACTER_PROMPT, load_character_prompt(path))

            path.write_text(" \n\t", encoding="utf-8")
            self.assertEqual(DEFAULT_CHARACTER_PROMPT, load_character_prompt(path))

            path.write_bytes(b"\xff\xfe")
            self.assertEqual(DEFAULT_CHARACTER_PROMPT, load_character_prompt(path))


if __name__ == "__main__":
    unittest.main()
