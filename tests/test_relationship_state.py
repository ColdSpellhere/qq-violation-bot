from __future__ import annotations

import sqlite3
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from pathlib import Path

from plugins.private_memory.models import ConversationScope, RelationshipState
from plugins.private_memory.schema import migrate
from plugins.private_memory.relationship import RelationshipStore


PROJECT_ROOT = Path(__file__).resolve().parents[1]
CHAT_ARCHIVE_SCHEMA = """
CREATE TABLE IF NOT EXISTS chat_messages (
    message_id TEXT PRIMARY KEY,
    group_id INTEGER NOT NULL,
    event_time INTEGER NOT NULL,
    user_id TEXT NOT NULL,
    sender_json TEXT NOT NULL,
    message_json TEXT NOT NULL,
    plaintext TEXT NOT NULL,
    reply_message_id TEXT,
    created_at TEXT NOT NULL
);
"""


class RelationshipStateTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.database = Path(temporary.name) / "chat_archive.db"
        migrate(self.database)
        self.store = RelationshipStore(self.database)

    @staticmethod
    def candidate(
        *,
        kind: str = "private",
        group_id: int | None = None,
        user_id: str = "200",
        persona_id: str = "radish-cat",
        state_text: str = "刚认识，聊天气氛轻松",
        open_topics: tuple[str, ...] = ("下次继续聊阳台上的花",),
        preferred_address: str = "小园丁",
        communication_style: str = "自然、简短，不要过度撒娇",
        source_message_id: str | None = None,
        source_watermark: int = 0,
        version: int = 1,
    ) -> RelationshipState:
        return RelationshipState(
            id=0,
            scope=ConversationScope(
                conversation_kind=kind,
                group_id=group_id,
                user_id=user_id,
                persona_id=persona_id,
            ),
            state_text=state_text,
            open_topics=open_topics,
            preferred_address=preferred_address,
            communication_style=communication_style,
            source_message_id=(
                source_message_id
                if source_message_id is not None
                else "governance:1"
            ),
            source_watermark=source_watermark,
            version=version,
            created_at="",
            updated_at="",
        )

    def add_private_source(
        self, *, user_id: str = "200", message_id: str = "private-source"
    ) -> int:
        with closing(sqlite3.connect(self.database)) as connection:
            cursor = connection.execute(
                """
                INSERT INTO private_chat_messages(
                    user_id,message_id,direction,text,content_hash,event_time,
                    created_at,expires_at,purged_at,source_kind,source_message_id
                ) VALUES(?,?,'user','source','hash',100,
                         '2026-08-22T00:00:00Z','2026-09-21T00:00:00Z',NULL,'text',NULL)
                """,
                (user_id, message_id),
            )
            connection.commit()
            return int(cursor.lastrowid)

    def add_group_source(
        self,
        *,
        group_id: int = 100,
        user_id: str = "200",
        message_id: str = "group-source",
        event_time: int = 123,
    ) -> int:
        with closing(sqlite3.connect(self.database)) as connection:
            connection.executescript(CHAT_ARCHIVE_SCHEMA)
            cursor = connection.execute(
                """
                INSERT INTO chat_messages(
                    message_id,group_id,event_time,user_id,sender_json,message_json,
                    plaintext,reply_message_id,created_at
                ) VALUES(?,?,?,?,'{}','[]','source',NULL,'2026-08-22T00:00:00Z')
                """,
                (message_id, group_id, event_time, user_id),
            )
            connection.commit()
            return int(cursor.lastrowid)

    def test_store_requires_an_explicit_current_schema(self) -> None:
        missing = self.database.with_name("missing.db")
        with self.assertRaisesRegex(RuntimeError, "schema version"):
            RelationshipStore(missing)
        self.assertFalse(missing.exists())

        legacy = self.database.with_name("legacy.db")
        with closing(sqlite3.connect(legacy)) as connection:
            connection.execute("CREATE TABLE marker(value TEXT)")
            connection.commit()
        with self.assertRaisesRegex(RuntimeError, "schema version"):
            RelationshipStore(legacy)
        with closing(sqlite3.connect(legacy)) as connection:
            self.assertEqual(
                [("marker",)],
                connection.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                ).fetchall(),
            )

    def test_private_insert_and_read_return_structured_topics(self) -> None:
        candidate = self.candidate(open_topics=("继续聊花", "问考试结果"))

        self.assertTrue(self.store.commit(candidate, expected_version=0))
        stored = self.store.get_private(user_id="200", persona_id="radish-cat")

        self.assertIsNotNone(stored)
        assert stored is not None
        self.assertGreater(stored.id, 0)
        self.assertEqual("private", stored.scope.conversation_kind)
        self.assertIsNone(stored.scope.group_id)
        self.assertEqual(("继续聊花", "问考试结果"), stored.open_topics)
        self.assertEqual(1, stored.version)
        self.assertTrue(stored.created_at.endswith("Z"))

    def test_group_private_user_group_and_persona_scopes_are_isolated(self) -> None:
        candidates = (
            self.candidate(state_text="private 200"),
            self.candidate(
                user_id="201", state_text="private 201"
            ),
            self.candidate(
                persona_id="other", state_text="private other"
            ),
            self.candidate(
                kind="group",
                group_id=100,
                state_text="group 100",
            ),
            self.candidate(
                kind="group",
                group_id=101,
                state_text="group 101",
            ),
        )
        for candidate in candidates:
            self.assertTrue(self.store.commit(candidate, expected_version=0))

        self.assertEqual(
            "private 200",
            self.store.get_private(user_id="200", persona_id="radish-cat").state_text,
        )
        self.assertEqual(
            "private 201",
            self.store.get_private(user_id="201", persona_id="radish-cat").state_text,
        )
        self.assertEqual(
            "private other",
            self.store.get_private(user_id="200", persona_id="other").state_text,
        )
        self.assertEqual(
            "group 100",
            self.store.get_group(
                group_id=100, user_id="200", persona_id="radish-cat"
            ).state_text,
        )
        self.assertEqual(
            "group 101",
            self.store.get_group(
                group_id=101, user_id="200", persona_id="radish-cat"
            ).state_text,
        )

    def test_compare_and_swap_rejects_stale_version_and_preserves_new_state(self) -> None:
        self.assertTrue(self.store.commit(self.candidate(), expected_version=0))
        current = self.store.get_private(user_id="200", persona_id="radish-cat")
        assert current is not None
        newest = self.candidate(
            state_text="已经熟悉一些",
            source_message_id="governance:20",
            version=current.version + 1,
        )
        stale = self.candidate(
            state_text="旧任务生成的内容",
            source_message_id="governance:15",
            version=current.version + 1,
        )

        self.assertTrue(self.store.commit(newest, expected_version=current.version))
        self.assertFalse(self.store.commit(stale, expected_version=current.version))
        stored = self.store.get_private(user_id="200", persona_id="radish-cat")
        assert stored is not None
        self.assertEqual("已经熟悉一些", stored.state_text)
        self.assertEqual(2, stored.version)

    def test_watermark_must_strictly_advance_and_failure_preserves_old_state(self) -> None:
        self.assertTrue(self.store.commit(self.candidate(), expected_version=0))
        first = self.add_private_source(message_id="source-1")
        second = self.add_private_source(message_id="source-2")
        self.assertTrue(
            self.store.commit(
                self.candidate(
                    source_message_id="source-1",
                    source_watermark=first,
                    version=2,
                ),
                expected_version=1,
            )
        )
        self.assertTrue(
            self.store.commit(
                self.candidate(
                    source_message_id="source-2",
                    source_watermark=second,
                    version=3,
                ),
                expected_version=2,
            )
        )
        for message_id, watermark in (("source-1", first), ("source-2", second)):
            with self.subTest(message_id=message_id):
                invalid = self.candidate(
                    state_text="不应写入",
                    source_message_id=message_id,
                    source_watermark=watermark,
                    version=4,
                )
                with self.assertRaisesRegex(ValueError, "watermark must advance"):
                    self.store.commit(invalid, expected_version=3)
        stored = self.store.get_private(user_id="200", persona_id="radish-cat")
        assert stored is not None
        self.assertEqual(second, stored.source_watermark)
        self.assertEqual(3, stored.version)

    def test_two_store_instances_racing_version_zero_have_one_winner(self) -> None:
        barrier = threading.Barrier(2)

        def commit(state_text: str) -> bool:
            store = RelationshipStore(self.database)
            barrier.wait()
            return store.commit(
                self.candidate(state_text=state_text),
                expected_version=0,
            )

        with ThreadPoolExecutor(max_workers=2) as executor:
            results = tuple(
                executor.map(commit, ("first candidate", "second candidate"))
            )
        self.assertEqual([False, True], sorted(results))

    def test_two_store_instances_racing_same_update_version_have_one_winner(self) -> None:
        self.assertTrue(
            self.store.commit(
                self.candidate(), expected_version=0
            )
        )
        barrier = threading.Barrier(2)

        def commit(watermark: int) -> bool:
            store = RelationshipStore(self.database)
            barrier.wait()
            return store.commit(
                self.candidate(
                    state_text=f"candidate {watermark}",
                    source_message_id=f"governance:{watermark}",
                    version=2,
                ),
                expected_version=1,
            )

        with ThreadPoolExecutor(max_workers=2) as executor:
            results = tuple(executor.map(commit, (2, 3)))
        self.assertEqual([False, True], sorted(results))

    def test_all_scope_and_content_boundaries_are_validated(self) -> None:
        invalid_cases = (
            self.candidate(user_id="０１２"),
            self.candidate(user_id="0"),
            self.candidate(persona_id="   "),
            self.candidate(persona_id="Radish-Cat"),
            self.candidate(persona_id="萝卜猫"),
            self.candidate(persona_id="radish_cat"),
            self.candidate(persona_id="-radish"),
            self.candidate(persona_id="radish-"),
            self.candidate(persona_id="a" * 65),
            self.candidate(kind="group", group_id=0),
            self.candidate(kind="group", group_id=None),
            self.candidate(kind="private", group_id=100),
            self.candidate(kind="room", group_id=100),
            self.candidate(state_text="x" * 601),
            self.candidate(open_topics=tuple(str(index) for index in range(6))),
            self.candidate(open_topics=("x" * 81,)),
            self.candidate(preferred_address="x" * 41),
            self.candidate(communication_style="x" * 201),
            self.candidate(source_message_id=""),
            self.candidate(source_message_id="   "),
            self.candidate(source_message_id=" padded "),
            self.candidate(source_message_id="line\nbreak"),
            self.candidate(source_message_id="消息-1"),
            self.candidate(source_message_id="x" * 129),
            self.candidate(source_watermark=-1),
            self.candidate(version=2),
        )
        for candidate in invalid_cases:
            with self.subTest(candidate=candidate):
                with self.assertRaises((TypeError, ValueError)):
                    self.store.commit(candidate, expected_version=0)

        invalid_reads = (
            lambda: self.store.get_private(user_id="１２", persona_id="radish-cat"),
            lambda: self.store.get_private(user_id="1", persona_id=""),
            lambda: self.store.get_group(
                group_id=-1, user_id="1", persona_id="radish-cat"
            ),
            lambda: self.store.get_group(
                group_id=True, user_id="1", persona_id="radish-cat"
            ),
        )
        for read in invalid_reads:
            with self.assertRaises((TypeError, ValueError)):
                read()

    def test_transaction_error_rolls_back_and_preserves_old_state(self) -> None:
        self.assertTrue(self.store.commit(self.candidate(), expected_version=0))
        with closing(sqlite3.connect(self.database)) as connection:
            connection.execute(
                """
                CREATE TRIGGER reject_relationship_update
                BEFORE UPDATE ON relationship_states
                BEGIN
                    SELECT RAISE(ABORT, 'injected failure');
                END
                """
            )
            connection.commit()

        with self.assertRaisesRegex(sqlite3.IntegrityError, "injected failure"):
            self.store.commit(
                self.candidate(
                    state_text="事务失败的新内容",
                    source_message_id="governance:20",
                    version=2,
                ),
                expected_version=1,
            )

        stored = self.store.get_private(user_id="200", persona_id="radish-cat")
        assert stored is not None
        self.assertEqual("刚认识，聊天气氛轻松", stored.state_text)
        self.assertEqual(1, stored.version)

    def test_automatic_private_source_must_match_live_user_message_and_row_id(self) -> None:
        watermark = self.add_private_source(message_id="private-source")
        valid = self.candidate(
            source_message_id="private-source", source_watermark=watermark
        )
        self.assertTrue(self.store.commit(valid, expected_version=0))

        other_watermark = self.add_private_source(
            user_id="201", message_id="other-private-source"
        )
        for candidate in (
            self.candidate(
                user_id="202",
                source_message_id="missing",
                source_watermark=other_watermark,
            ),
            self.candidate(
                user_id="200",
                persona_id="other",
                source_message_id="private-source",
                source_watermark=watermark + 99,
            ),
            self.candidate(
                user_id="200",
                persona_id="third",
                source_message_id="other-private-source",
                source_watermark=other_watermark,
            ),
        ):
            with self.subTest(candidate=candidate):
                with self.assertRaisesRegex(ValueError, "source message"):
                    self.store.commit(candidate, expected_version=0)

        with closing(sqlite3.connect(self.database)) as connection:
            connection.execute(
                "UPDATE private_chat_messages SET purged_at='2026-08-22T01:00:00Z' "
                "WHERE message_id='private-source'"
            )
            connection.commit()
        with self.assertRaisesRegex(ValueError, "source message"):
            self.store.commit(
                self.candidate(
                    persona_id="purged",
                    source_message_id="private-source",
                    source_watermark=watermark,
                ),
                expected_version=0,
            )

    def test_automatic_group_source_must_match_complete_scope_and_rowid(self) -> None:
        watermark = self.add_group_source()
        valid = self.candidate(
            kind="group",
            group_id=100,
            source_message_id="group-source",
            source_watermark=watermark,
        )
        self.assertTrue(self.store.commit(valid, expected_version=0))

        invalid = (
            self.candidate(
                kind="group",
                group_id=101,
                source_message_id="group-source",
                source_watermark=watermark,
            ),
            self.candidate(
                kind="group",
                group_id=100,
                user_id="201",
                source_message_id="group-source",
                source_watermark=watermark,
            ),
            self.candidate(
                kind="group",
                group_id=100,
                persona_id="other",
                source_message_id="group-source",
                source_watermark=watermark + 1,
            ),
        )
        for candidate in invalid:
            with self.subTest(candidate=candidate):
                with self.assertRaisesRegex(ValueError, "source message"):
                    self.store.commit(candidate, expected_version=0)

    def test_governance_operation_id_does_not_advance_message_watermark(self) -> None:
        self.assertTrue(
            self.store.commit(
                self.candidate(source_message_id="governance:100"),
                expected_version=0,
            )
        )
        self.assertTrue(
            self.store.commit(
                self.candidate(
                    source_message_id="governance:1", version=2
                ),
                expected_version=1,
            )
        )
        stored = self.store.get_private(user_id="200", persona_id="radish-cat")
        assert stored is not None
        self.assertEqual(0, stored.source_watermark)

        first = self.add_private_source(message_id="automatic-1")
        self.assertTrue(
            self.store.commit(
                self.candidate(
                    source_message_id="automatic-1",
                    source_watermark=first,
                    version=3,
                ),
                expected_version=2,
            )
        )
        self.assertTrue(
            self.store.commit(
                self.candidate(
                    source_message_id="governance:999", source_watermark=first,
                    version=4,
                ),
                expected_version=3,
            )
        )
        after_governance = self.store.get_private(
            user_id="200", persona_id="radish-cat"
        )
        assert after_governance is not None
        self.assertEqual(first, after_governance.source_watermark)

        second = self.add_private_source(message_id="automatic-2")
        self.assertTrue(
            self.store.commit(
                self.candidate(
                    source_message_id="automatic-2",
                    source_watermark=second,
                    version=5,
                ),
                expected_version=4,
            )
        )
        self.assertTrue(
            self.store.commit(
                self.candidate(
                    source_message_id="governance:1", source_watermark=second,
                    version=6,
                ),
                expected_version=5,
            )
        )
        after_smaller_operation = self.store.get_private(
            user_id="200", persona_id="radish-cat"
        )
        assert after_smaller_operation is not None
        self.assertEqual(second, after_smaller_operation.source_watermark)

    def test_governance_source_requires_positive_operation_id_and_stable_watermark(self) -> None:
        for candidate in (
            self.candidate(source_message_id="governance:0"),
            self.candidate(source_message_id="governance:not-a-number"),
            self.candidate(source_message_id="governance:1", source_watermark=1),
        ):
            with self.subTest(candidate=candidate):
                with self.assertRaisesRegex(ValueError, "governance"):
                    self.store.commit(candidate, expected_version=0)

    def test_same_event_time_group_messages_have_distinct_sequential_watermarks(self) -> None:
        first = self.add_group_source(
            message_id="same-time-1", event_time=500
        )
        second = self.add_group_source(
            message_id="same-time-2", event_time=500
        )
        self.assertGreater(second, first)
        self.assertTrue(
            self.store.commit(
                self.candidate(
                    kind="group", group_id=100,
                    source_message_id="same-time-1", source_watermark=first,
                ),
                expected_version=0,
            )
        )
        self.assertTrue(
            self.store.commit(
                self.candidate(
                    kind="group", group_id=100,
                    source_message_id="same-time-2", source_watermark=second,
                    version=2,
                ),
                expected_version=1,
            )
        )

    def test_corrupt_persisted_rows_fail_closed_on_read(self) -> None:
        corruptions = (
            ("persona_id", "萝卜猫"),
            ("state_text", "x" * 601),
            ("open_topics_json", '["' + ("x" * 81) + '"]'),
            ("preferred_address", "x" * 41),
            ("communication_style", "x" * 201),
            ("source_message_id", " bad "),
            ("source_watermark", -1),
            ("version", 0),
            ("id", 0),
        )
        for column, value in corruptions:
            with self.subTest(column=column):
                database = self.database.with_name(f"corrupt-{column}.db")
                migrate(database)
                store = RelationshipStore(database)
                self.assertTrue(
                    store.commit(self.candidate(), expected_version=0)
                )
                with closing(sqlite3.connect(database)) as connection:
                    connection.row_factory = sqlite3.Row
                    connection.execute("PRAGMA ignore_check_constraints=ON")
                    connection.execute(
                        f"UPDATE relationship_states SET {column}=?", (value,)
                    )
                    connection.commit()
                    row = connection.execute(
                        "SELECT * FROM relationship_states"
                    ).fetchone()
                assert row is not None
                with self.assertRaises((TypeError, ValueError)):
                    store._from_row(row)

    def test_violation_modules_do_not_import_private_memory_or_query_its_table(self) -> None:
        violation_root = PROJECT_ROOT / "plugins" / "violation_record"
        for source in violation_root.rglob("*.py"):
            text = source.read_text("utf-8")
            self.assertNotIn("plugins.private_memory", text)
            self.assertNotIn("relationship_states", text)


if __name__ == "__main__":
    unittest.main()
