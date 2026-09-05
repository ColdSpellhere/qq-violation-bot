"""Migration and async durability evidence using the exact pre-repair schema."""
import asyncio
import sqlite3
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch
from plugins.chat_archive.db import archive_payload, archive_payload_async
from plugins.private_memory.schema import migrate, schema_version
from plugins.private_memory.jobs import MemoryJobQueue

class MemorySchemaHardeningTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.db=Path(self.temp.name).resolve()/'chat.db'
        with sqlite3.connect(self.db) as c:
            c.executescript((Path(__file__).parent/'fixtures/memory_schema_v3.sql').read_text())
            c.execute("INSERT INTO chat_messages(rowid,message_id,group_id,event_time,user_id,sender_json,message_json,plaintext,created_at) VALUES(19,'same',123,1,'200','{}','[]','kept','old')")
            c.execute("INSERT INTO memory_jobs(id,job_type,conversation_kind,user_id,input_through_id,expected_version,status,next_run_at,created_at,updated_at) VALUES(41,'private_facts','private','200',19,0,'succeeded','old','old','old')")
            c.execute("UPDATE sqlite_sequence SET seq=1000 WHERE name='memory_jobs'")

    def test_migration_preserves_rowids_jobs_sequence_and_incremental_checkpoint(self):
        self.assertEqual(3,schema_version(self.db))
        migrate(self.db);migrate(self.db)
        with sqlite3.connect(self.db) as c:
            self.assertEqual((19,'same','kept'),c.execute('SELECT rowid,message_id,plaintext FROM chat_messages').fetchone())
            self.assertEqual((41,'succeeded'),c.execute('SELECT id,status FROM memory_jobs').fetchone())
            self.assertEqual((19,0),c.execute('SELECT through_message_id,version FROM private_fact_progress').fetchone())
            self.assertEqual(1000,c.execute("SELECT seq FROM sqlite_sequence WHERE name='memory_jobs'").fetchone()[0])
            self.assertEqual('ok',c.execute('PRAGMA integrity_check').fetchone()[0])
        new=MemoryJobQueue(self.db).enqueue(job_type='private_facts',conversation_kind='private',group_id=None,user_id='200',input_through_id=20,expected_version=0)
        self.assertEqual(1001,new)
        self.assertTrue(archive_payload(self.db,456,dict(message_id='same',group_id=456,event_time=1,user_id='200',sender={},segments=[],plaintext='other group')))

    def test_unknown_archive_extensions_refuse_migration_without_partial_changes(self):
        with sqlite3.connect(self.db) as c:
            c.execute("ALTER TABLE chat_messages ADD COLUMN local_extension TEXT DEFAULT 'keep'")
        with self.assertRaisesRegex(RuntimeError,'unknown columns'):
            migrate(self.db)
        self.assertEqual(3,schema_version(self.db))
        with sqlite3.connect(self.db) as c:
            self.assertEqual((19,'keep'),c.execute('SELECT rowid,local_extension FROM chat_messages').fetchone())
            self.assertIsNone(c.execute("SELECT 1 FROM sqlite_master WHERE name='private_fact_progress'").fetchone())

    def test_dependent_view_is_not_silently_rewritten_or_lost(self):
        with sqlite3.connect(self.db) as c:
            c.execute('CREATE VIEW local_archive_view AS SELECT rowid,plaintext FROM chat_messages')
        with self.assertRaisesRegex(RuntimeError,'dependent view'):
            migrate(self.db)
        self.assertEqual(3,schema_version(self.db))
        with sqlite3.connect(self.db) as c:
            self.assertEqual((19,'kept'),c.execute('SELECT * FROM local_archive_view').fetchone())

    async def test_async_archive_yields_and_waits_for_durable_write_on_cancellation(self):
        migrate(self.db)
        entered=threading.Event();release=threading.Event()
        payload=dict(message_id='async',group_id=123,event_time=1,user_id='200',sender={},segments=[],plaintext='text')
        def slow_archive(*args):
            entered.set()
            release.wait(timeout=2)
            return archive_payload(*args)
        with patch('plugins.chat_archive.db.archive_payload',side_effect=slow_archive):
            task=asyncio.create_task(archive_payload_async(self.db,123,payload))
            for _ in range(100):
                if entered.is_set(): break
                await asyncio.sleep(.001)
            self.assertTrue(entered.is_set())
            task.cancel()
            await asyncio.sleep(.01)
            self.assertFalse(task.done())
            release.set()
            with self.assertRaises(asyncio.CancelledError): await task
        with sqlite3.connect(self.db) as c:
            self.assertEqual(1,c.execute("SELECT count(*) FROM chat_messages WHERE message_id='async'").fetchone()[0])

    def test_empty_legacy_jobs_keep_previous_autoincrement_high_watermark(self):
        with sqlite3.connect(self.db) as c:
            c.execute('DELETE FROM memory_jobs')
        migrate(self.db)
        identity=MemoryJobQueue(self.db).enqueue(job_type='private_facts',conversation_kind='private',group_id=None,user_id='200',input_through_id=20,expected_version=0)
        self.assertEqual(1001,identity)

    async def test_cancel_during_claim_waits_before_releasing_late_lease(self):
        from unittest.mock import AsyncMock
        from plugins.private_memory.jobs import MemoryJobWorker
        migrate(self.db)
        queue=MemoryJobQueue(self.db)
        identity=queue.enqueue(job_type='private_summary',conversation_kind='private',group_id=None,user_id='201',input_through_id=1,expected_version=0)
        entered=threading.Event();release=threading.Event()
        real_claim=queue.claim
        def slow_claim(**kwargs):
            entered.set();release.wait(timeout=2)
            return real_claim(**kwargs)
        worker=MemoryJobWorker(queue,AsyncMock(return_value=True),allowed_job_types=lambda:{'private_summary'},worker_id='w')
        with patch.object(queue,'claim',side_effect=slow_claim):
            task=asyncio.create_task(worker.run())
            for _ in range(100):
                if entered.is_set(): break
                await asyncio.sleep(.001)
            self.assertTrue(entered.is_set())
            task.cancel();await asyncio.sleep(.01)
            self.assertFalse(task.done())
            release.set()
            with self.assertRaises(asyncio.CancelledError): await task
        self.assertEqual('pending',queue.get(identity).status)
