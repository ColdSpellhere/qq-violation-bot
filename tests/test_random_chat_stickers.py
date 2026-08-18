import os
import tempfile
import unittest
from pathlib import Path

os.environ.setdefault("TARGET_GROUP_ID", "999000111")

from plugins.random_chat.stickers import choose_sticker


class StickerSelectionTests(unittest.TestCase):
    def _touch(self, root: Path, name: str) -> Path:
        path = root / name
        path.write_bytes(b"image")
        return path

    def test_attachment_gate_is_twenty_percent(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            special = self._touch(root, "special.gif")
            self.assertEqual(
                special,
                choose_sticker(
                    root,
                    special_filename="special.gif",
                    attachment_probability=0.20,
                    attachment_sample=0.199,
                    weight_sample=0.0,
                ),
            )
            self.assertIsNone(
                choose_sticker(
                    root,
                    special_filename="special.gif",
                    attachment_probability=0.20,
                    attachment_sample=0.20,
                    weight_sample=0.0,
                )
            )

    def test_special_owns_first_ten_percent_of_conditional_pool(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            special = self._touch(root, "special.gif")
            normal = self._touch(root, "normal.jpg")
            common = dict(
                root=root,
                special_filename="special.gif",
                attachment_probability=1.0,
                attachment_sample=0.0,
            )
            self.assertEqual(special, choose_sticker(**common, weight_sample=0.099999))
            self.assertEqual(normal, choose_sticker(**common, weight_sample=0.10))

    def test_normal_images_evenly_split_the_remaining_ninety_percent(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self._touch(root, "special.gif")
            first = self._touch(root, "a.jpg")
            second = self._touch(root, "b.webp")
            common = dict(
                root=root,
                special_filename="special.gif",
                attachment_probability=1.0,
                attachment_sample=0.0,
            )
            self.assertEqual(first, choose_sticker(**common, weight_sample=0.10))
            self.assertEqual(first, choose_sticker(**common, weight_sample=0.549999))
            self.assertEqual(second, choose_sticker(**common, weight_sample=0.55))
            self.assertEqual(second, choose_sticker(**common, weight_sample=0.999999))

    def test_missing_or_unsupported_pool_disables_attachment(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self._touch(root, "note.txt")
            (root / "nested").mkdir()
            self._touch(root / "nested", "hidden.jpg")
            self.assertIsNone(
                choose_sticker(
                    root,
                    special_filename="special.gif",
                    attachment_probability=1.0,
                    attachment_sample=0.0,
                    weight_sample=0.0,
                )
            )


if __name__ == "__main__":
    unittest.main()
