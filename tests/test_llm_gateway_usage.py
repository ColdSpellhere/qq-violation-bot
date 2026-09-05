from __future__ import annotations

import logging
import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import Mock
from unittest.mock import patch

from plugins.llm_gateway.contracts import GatewayCompletion, GatewayRequest, LLMTask, TokenUsage
from plugins.llm_gateway.errors import GatewayTimeout
from plugins.llm_gateway.usage import UsageStore
from plugins.private_memory.schema import PRIVATE_MEMORY_SCHEMA_VERSION, migrate


class LLMUsageStoreTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.database = Path(temporary.name) / "chat_archive.db"

    def request(self) -> GatewayRequest:
        return GatewayRequest(
            task=LLMTask.PRIVATE_SUMMARY,
            messages=({"role": "user", "content": "private prompt"},),
            model="requested-model",
            timeout=5,
        )

    def test_store_requires_migrated_schema_and_never_migrates_implicitly(self) -> None:
        with self.assertRaises(RuntimeError):
            UsageStore(self.database)
        self.assertFalse(self.database.exists())

        with closing(sqlite3.connect(self.database)) as connection:
            connection.execute("CREATE TABLE marker(value TEXT)")
            connection.commit()
        with self.assertRaises(RuntimeError):
            UsageStore(self.database)
        with closing(sqlite3.connect(self.database)) as connection:
            self.assertIsNone(connection.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='llm_usage_events'"
            ).fetchone())

    def test_success_and_failure_rows_are_redacted_and_cost_is_null_without_prices(self) -> None:
        migrate(self.database)
        store = UsageStore(self.database)
        store.record_success(
            self.request(),
            GatewayCompletion(
                content="private response",
                model="served-model",
                usage=TokenUsage(input_tokens=10, output_tokens=4, total_tokens=14),
                latency_ms=123,
                retries=1,
            ),
        )
        store.record_failure(
            self.request(), latency_ms=77, retries=2, error=GatewayTimeout("private detail")
        )

        with closing(sqlite3.connect(self.database)) as connection:
            columns = [row[1] for row in connection.execute("PRAGMA table_info(llm_usage_events)")]
            rows = connection.execute(
                "SELECT task,model,input_tokens,output_tokens,total_tokens,cost_microunits,"
                "cost_currency,latency_ms,status,retry_count,error_class,created_at "
                "FROM llm_usage_events ORDER BY id"
            ).fetchall()
        forbidden = {"prompt", "content", "user_id", "group_id", "url", "request", "response"}
        self.assertTrue(forbidden.isdisjoint(columns))
        self.assertEqual(
            ("private_summary", "served-model", 10, 4, 14, None, None, 123, "success", 1, None),
            rows[0][:-1],
        )
        self.assertEqual(
            ("private_summary", "requested-model", None, None, None, None, None, 77, "failure", 2, "GatewayTimeout"),
            rows[1][:-1],
        )
        self.assertTrue(rows[0][-1].endswith("Z"))

    def test_write_failure_logs_only_exception_class_and_does_not_escape(self) -> None:
        migrate(self.database)
        logger = Mock(spec=logging.Logger)
        store = UsageStore(self.database, logger=logger)
        self.database.unlink()
        store.record_success(
            self.request(), GatewayCompletion(content="response", model="model", latency_ms=1)
        )
        logger.warning.assert_called_once()
        rendered = " ".join(str(item) for item in logger.warning.call_args.args)
        self.assertIn("OperationalError", rendered)
        self.assertNotIn("private prompt", rendered)
        self.assertNotIn(str(self.database), rendered)


class LLMUsageSchemaMigrationTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)

    def test_fresh_legacy_and_v1_databases_upgrade_to_current_idempotently(self) -> None:
        self.assertEqual(4, PRIVATE_MEMORY_SCHEMA_VERSION)
        for kind in ("fresh", "legacy", "v1"):
            path = self.root / f"{kind}.db"
            if kind != "fresh":
                with closing(sqlite3.connect(path)) as connection:
                    if kind == "legacy":
                        connection.execute("CREATE TABLE marker(value TEXT)")
                        connection.execute("INSERT INTO marker VALUES ('kept')")
                    else:
                        connection.execute(
                            "CREATE TABLE private_memory_schema_meta("
                            "singleton INTEGER PRIMARY KEY CHECK(singleton=1),"
                            "schema_version INTEGER NOT NULL CHECK(schema_version > 0),"
                            "updated_at TEXT NOT NULL)"
                        )
                        connection.execute(
                            "INSERT INTO private_memory_schema_meta VALUES(1,1,'v1-sentinel')"
                        )
                    connection.commit()
            first = migrate(path)
            with closing(sqlite3.connect(path)) as connection:
                connection.execute(
                    "UPDATE private_memory_schema_meta SET updated_at='current-sentinel' WHERE singleton=1"
                )
                connection.commit()
            second = migrate(path)
            self.assertEqual((4, 4), (first.schema_version, second.schema_version))
            with closing(sqlite3.connect(path)) as connection:
                self.assertIsNotNone(connection.execute(
                    "SELECT 1 FROM sqlite_master WHERE type='table' AND name='llm_usage_events'"
                ).fetchone())
                self.assertEqual("current-sentinel", connection.execute(
                    "SELECT updated_at FROM private_memory_schema_meta WHERE singleton=1"
                ).fetchone()[0])
                if kind == "legacy":
                    self.assertEqual("kept", connection.execute("SELECT value FROM marker").fetchone()[0])

    def test_v1_upgrade_failure_rolls_back_usage_table_and_version(self) -> None:
        path = self.root / "v1.db"
        with closing(sqlite3.connect(path)) as connection:
            connection.execute(
                "CREATE TABLE private_memory_schema_meta("
                "singleton INTEGER PRIMARY KEY CHECK(singleton=1),"
                "schema_version INTEGER NOT NULL CHECK(schema_version > 0),"
                "updated_at TEXT NOT NULL)"
            )
            connection.execute(
                "INSERT INTO private_memory_schema_meta VALUES(1,1,'v1-sentinel')"
            )
            connection.commit()

        with patch(
            "plugins.private_memory.schema._INDEX_STATEMENTS",
            ("CREATE INDEX invalid syntax",),
        ), self.assertRaises(sqlite3.OperationalError):
            migrate(path)

        with closing(sqlite3.connect(path)) as connection:
            self.assertIsNone(connection.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='llm_usage_events'"
            ).fetchone())
            self.assertEqual((1, "v1-sentinel"), connection.execute(
                "SELECT schema_version,updated_at FROM private_memory_schema_meta WHERE singleton=1"
            ).fetchone())


if __name__ == "__main__":
    unittest.main()
