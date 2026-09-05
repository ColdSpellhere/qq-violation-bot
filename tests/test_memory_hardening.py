"""Offline regression coverage for the production memory audit findings."""
import asyncio
import sqlite3
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import AsyncMock, patch

from plugins.chat_archive.db import archive_payload
from plugins.member_memory.store import apply_candidates, load_profiles
from plugins.member_memory.summary import refresh_member_summary
from plugins.memory_governance.commands import MemoryCommand, MemoryScope
from plugins.memory_governance.service import MemoryGovernanceService
from plugins.private_memory.jobs import MemoryJobQueue
from plugins.private_memory.models import ConversationScope, MemoryJob
from plugins.private_memory.processor import PrivateMemoryProcessor
from plugins.private_memory.relationship import RelationshipStore
from plugins.private_memory.schema import migrate
from plugins.private_memory.store import PrivateMemoryStore
from plugins.chat_archive.db import ContextMessage

NOW = datetime(2026, 9, 5, tzinfo=timezone.utc)

def job(kind, watermark):
    return MemoryJob(1, kind, ConversationScope('private', '200'), watermark, 0,
                     'running', 1, '', 'worker', None, 1, '', '', '', '')

class MemoryTestCase(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.db = self.root / 'chat.db'
        migrate(self.db)
        self.store = PrivateMemoryStore(self.db)
        self.relationships = RelationshipStore(self.db)

    def append(self, mid, text='普通文字', now=NOW):
        return self.store.append_user_message(user_id='200', message_id=mid,
            text=text, event_time=int(now.timestamp()), source_kind='text')

    def processor(self, **kwargs):
        return PrivateMemoryProcessor(store=self.store, relationship_store=self.relationships,
            private_memory_enabled=lambda: True, relationship_enabled=lambda: True,
            background_memory_allowed=lambda: True, **kwargs)

class MemoryHardeningTests(MemoryTestCase):
    async def test_group_summary_can_rebuild_after_deleted_fact_but_not_resurrect_it(self):
        context = [ContextMessage('成员', '我喜欢植物', message_id='m1', user_id='200')]
        apply_candidates(self.db, self.root/'mirror', group_id=123, context=context,
            candidates=[dict(user_id='200', trait=f'植物偏好{i}', evidence_message_id='m1', quote='我喜欢植物') for i in range(5)])
        with patch('plugins.member_memory.summary.generate_memory_summary', AsyncMock(return_value='旧摘要')):
            self.assertTrue(await refresh_member_summary(self.db, self.root/'mirror', group_id=123, user_id='200'))
        service=MemoryGovernanceService(self.db, private_allowed_user_ids=('200',))
        preview=service.preview(MemoryCommand('delete_fact', fact_kind='group', memory_id=1), actor='900', now=NOW)
        self.assertTrue(service.confirm(preview.token, actor='900',reason='核实',now=NOW).success)
        generate=AsyncMock(return_value='只含保留事实的摘要')
        with patch('plugins.member_memory.summary.generate_memory_summary',generate):
            self.assertTrue(await refresh_member_summary(self.db,self.root/'mirror',group_id=123,user_id='200'))
        self.assertEqual('只含保留事实的摘要',load_profiles(self.db,group_id=123,user_ids=['200'])[0].summary)
        self.assertNotIn(1,[x.fact_id for x in generate.await_args.args[1]])

    async def test_retention_gap_does_not_block_new_summary(self):
        self.append('old', now=NOW-timedelta(days=31))
        through=self.append('new')
        self.store.purge_expired(now=NOW,retention_days=30,max_messages=100)
        summarize=AsyncMock(return_value='新摘要')
        self.assertTrue(await self.processor(summarize=summarize).process(job('private_summary',through)))
        self.assertEqual(['new'],[x.message_id for x in summarize.await_args.args[1]])

    async def test_incremental_facts_survive_processor_recreation(self):
        first=self.append('first');extract=AsyncMock(return_value=())
        self.assertTrue(await self.processor(extract=extract).process(job('private_facts',first)))
        second=self.append('second')
        self.assertTrue(await self.processor(extract=extract).process(job('private_facts',second)))
        self.assertEqual(['second'],[x.message_id for x in extract.await_args.args[0]])

    async def test_derived_summary_secret_is_rejected(self):
        through=self.append('p1')
        summarize=AsyncMock(return_value='password: synthetic-secret')
        self.assertFalse(await self.processor(summarize=summarize).process(job('private_summary',through)))
        self.assertIsNone(self.store.get_summary(user_id='200'))

    def test_relationship_messages_are_coalesced_before_model_work(self):
        queue=MemoryJobQueue(self.db)
        args=dict(job_type='relationship',conversation_kind='private',user_id='200',group_id=None,expected_version=0)
        first=queue.enqueue(input_through_id=1,**args)
        second=queue.enqueue(input_through_id=2,**args)
        self.assertEqual(first,second)
        self.assertEqual(2,queue.get(first).input_through_id)

    def test_archive_reports_duplicate_without_repeating_identity_work(self):
        payload=dict(message_id='same',group_id=123,event_time=1,user_id='200',sender={},segments=[],plaintext='hello')
        self.assertTrue(archive_payload(self.db,123,payload))
        self.assertFalse(archive_payload(self.db,123,payload))

    def test_archive_same_message_id_is_independent_between_groups(self):
        for group in [123,456]:
            payload=dict(message_id='same',group_id=group,event_time=1,user_id='200',sender={},segments=[],plaintext='hello')
            self.assertTrue(archive_payload(self.db,group,payload))
        with sqlite3.connect(self.db) as c:
            self.assertEqual(2,c.execute('SELECT count(*) FROM chat_messages').fetchone()[0])
