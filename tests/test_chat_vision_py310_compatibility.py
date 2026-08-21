from __future__ import annotations

import unittest
from pathlib import Path


class ChatVisionPython310CompatibilityTests(unittest.TestCase):
    def test_modules_do_not_import_datetime_utc(self) -> None:
        root = Path(__file__).resolve().parents[1]
        modules = (
            "plugins/chat_vision/lifecycle.py",
            "plugins/chat_vision/download.py",
            "plugins/chat_vision/service.py",
            "plugins/chat_vision/store.py",
        )

        for relative_path in modules:
            with self.subTest(module=relative_path):
                source = (root / relative_path).read_text(encoding="utf-8")
                self.assertNotIn("from datetime import UTC", source)
                self.assertNotIn("datetime.now(UTC)", source)
                self.assertNotIn("datetime.fromtimestamp(event_time, UTC)", source)
