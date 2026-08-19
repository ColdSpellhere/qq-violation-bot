import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

os.environ.setdefault("TARGET_GROUP_ID", "999000111")

from plugins.chat_archive.db import ContextMessage
from plugins.member_memory.store import (
    MEMORY_SCHEMA,
    apply_candidates,
    commit_summary,
    migrate_legacy_memory,
    remember_identity,
)


def table_exists(path: Path, name: str) -> bool:
    with sqlite3.connect(path) as conn:
        return bool(conn.execute(
            "SELECT count(*) FROM sqlite_master WHERE type=? AND name=?", ("table", name)
        ).fetchone()[0])


def count_facts(path: Path) -> int:
    with sqlite3.connect(path) as conn:
        return int(conn.execute("SELECT count(*) FROM member_memory_facts").fetchone()[0])


def seed_additional_facts(path: Path, root: Path, *, count: int) -> None:
    context = [ContextMessage("当前名", "我喜欢火锅", message_id="new-message", user_id="7")]
    candidates = [
        {
            "user_id": "7",
            "trait": f"新增特性{index}",
            "evidence_message_id": "new-message",
            "quote": "我喜欢火锅",
        }
        for index in range(count)
    ]
    apply_candidates(path, root, group_id=123, context=context, candidates=candidates)


class MemberMemoryMigrationTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.db = Path(self.temporary.name) / "chat.db"
        self.root = Path(self.temporary.name) / "member_memory"
        traits = [
            {"text": f"特性{index}", "evidence_message_id": f"m{index}", "updated_at": "2026-08-19 10:00:00"}
            for index in range(8)
        ]
        with sqlite3.connect(self.db) as conn:
            conn.executescript(MEMORY_SCHEMA)
            conn.execute(
                "INSERT INTO member_memories(group_id,user_id,nickname,aliases_json,traits_json,updated_at) VALUES(?,?,?,?,?,?)",
                (123, "7", "当前名", json.dumps(["旧名1", "旧名2"], ensure_ascii=False), json.dumps(traits, ensure_ascii=False), "2026-08-19 10:00:00"),
            )

    def test_dry_run_reports_without_writing(self):
        report = migrate_legacy_memory(self.db, self.root, apply=False)

        self.assertEqual(8, report.source_facts)
        self.assertEqual(2, report.source_aliases)
        self.assertEqual(0, report.inserted_facts)
        self.assertFalse(table_exists(self.db, "member_memory_facts"))

    def test_cli_without_mode_defaults_to_zero_write_dry_run(self):
        completed = subprocess.run(
            [
                sys.executable,
                "scripts/migrate_member_memory_v2.py",
                "--database",
                str(self.db),
                "--mirror-root",
                str(self.root),
            ],
            cwd=Path(__file__).resolve().parents[1],
            env=os.environ.copy(),
            capture_output=True,
            text=True,
            check=False,
        )

        self.assertEqual(0, completed.returncode, completed.stderr)
        self.assertIn("inserted_facts=0", completed.stdout)
        self.assertFalse(table_exists(self.db, "member_memory_facts"))

    def test_apply_is_idempotent_and_preserves_counts(self):
        first = migrate_legacy_memory(self.db, self.root, apply=True)
        second = migrate_legacy_memory(self.db, self.root, apply=True)

        self.assertEqual(8, first.inserted_facts)
        self.assertEqual(2, first.inserted_aliases)
        self.assertEqual(0, second.inserted_facts)
        self.assertEqual(0, second.inserted_aliases)
        self.assertEqual(8, count_facts(self.db))

    def test_mirror_contains_complete_history_and_summary(self):
        migrate_legacy_memory(self.db, self.root, apply=True)
        seed_additional_facts(self.db, self.root, count=10)
        for index in range(10):
            remember_identity(self.db, self.root, group_id=123, user_id="7", nickname=f"名字{index}")
        commit_summary(
            self.db, self.root, group_id=123, user_id="7",
            previous_through_id=0, through_fact_id=8, summary="长期喜欢植物",
        )

        payload = json.loads((self.root / "123" / "7.json").read_text())
        self.assertEqual(18, len(payload["traits"]))
        self.assertGreaterEqual(len(payload["aliases"]), 11)
        self.assertEqual("长期喜欢植物", payload["summary"])
