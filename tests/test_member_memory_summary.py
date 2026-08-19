import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

os.environ.setdefault("TARGET_GROUP_ID", "999000111")

from plugins.chat_archive.db import ContextMessage
from plugins.member_memory.store import apply_candidates, commit_summary, load_profiles
from plugins.member_memory.summary import refresh_member_summary


def seed_facts(path: Path, root: Path, *, count: int) -> None:
    context = [ContextMessage("小明", "我喜欢火锅", message_id="m1", user_id="7")]
    candidates = [
        {"user_id": "7", "trait": f"特性{index}", "evidence_message_id": "m1", "quote": "我喜欢火锅"}
        for index in range(count)
    ]
    apply_candidates(path, root, group_id=123, context=context, candidates=candidates)


class MemberMemorySummaryTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.db = Path(self.temporary.name) / "chat.db"
        self.root = Path(self.temporary.name) / "member_memory"

    async def test_five_pending_facts_refresh_summary_advance_cursor_and_mirror(self):
        seed_facts(self.db, self.root, count=5)
        with patch("plugins.member_memory.summary.generate_memory_summary", AsyncMock(return_value="喜欢火锅，也常养花")):
            self.assertTrue(await refresh_member_summary(self.db, self.root, group_id=123, user_id="7"))
        profile = load_profiles(self.db, group_id=123, user_ids=["7"])[0]
        mirror = json.loads((self.root / "123" / "7.json").read_text(encoding="utf-8"))
        self.assertEqual("喜欢火锅，也常养花", profile.summary)
        self.assertEqual(profile.traits[-1].fact_id, profile.summary_through_fact_id)
        self.assertEqual(profile.summary, mirror["summary"])
        self.assertEqual(profile.summary_through_fact_id, mirror["summary_through_fact_id"])
        self.assertEqual(5, len(mirror["traits"]))

    async def test_summary_failure_keeps_cursor_and_raw_facts(self):
        seed_facts(self.db, self.root, count=5)
        with patch("plugins.member_memory.summary.generate_memory_summary", AsyncMock(return_value=None)):
            self.assertFalse(await refresh_member_summary(self.db, self.root, group_id=123, user_id="7"))
        profile = load_profiles(self.db, group_id=123, user_ids=["7"])[0]
        self.assertEqual("", profile.summary)
        self.assertEqual(0, profile.summary_through_fact_id)
        self.assertEqual(5, len(profile.traits))

    async def test_four_pending_facts_do_not_call_ai(self):
        seed_facts(self.db, self.root, count=4)
        generate = AsyncMock(return_value="不应生成")
        with patch("plugins.member_memory.summary.generate_memory_summary", generate):
            self.assertFalse(await refresh_member_summary(self.db, self.root, group_id=123, user_id="7"))
        generate.assert_not_awaited()

    async def test_twenty_five_pending_facts_are_summarized_in_two_batches(self):
        seed_facts(self.db, self.root, count=25)
        generate = AsyncMock(side_effect=["第一批摘要", "最终摘要"])
        with patch("plugins.member_memory.summary.generate_memory_summary", generate):
            self.assertTrue(await refresh_member_summary(self.db, self.root, group_id=123, user_id="7"))
        self.assertEqual(2, generate.await_count)
        profile = load_profiles(self.db, group_id=123, user_ids=["7"])[0]
        self.assertEqual("最终摘要", profile.summary)
        self.assertEqual(profile.traits[-1].fact_id, profile.summary_through_fact_id)

    async def test_stale_cursor_cannot_overwrite_newer_summary(self):
        seed_facts(self.db, self.root, count=5)
        self.assertTrue(commit_summary(self.db, self.root, group_id=123, user_id="7", previous_through_id=0, through_fact_id=5, summary="新摘要"))
        self.assertFalse(commit_summary(self.db, self.root, group_id=123, user_id="7", previous_through_id=0, through_fact_id=4, summary="过期摘要"))


if __name__ == "__main__":
    unittest.main()
