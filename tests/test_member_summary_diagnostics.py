"""Observable rejection reasons without retaining model output or source content."""
from __future__ import annotations

import asyncio
import sqlite3
import tempfile
import unittest
from contextlib import ExitStack
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import httpx

from plugins.member_memory import ai, summary
from plugins.member_memory.store import commit_summary, load_profiles
from tests.test_member_memory_summary import seed_facts


class MemberSummaryDiagnosticsTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name) / 'archive.db'
        self.root = Path(self.directory.name) / 'mirror'
        seed_facts(self.path, self.root, count=20)
        self.assertTrue(commit_summary(self.path,self.root,group_id=123,user_id='7',
                                      previous_through_id=0,through_fact_id=0,summary='旧'*278))
        stack = ExitStack()
        self.addCleanup(stack.close)
        stack.enter_context(patch.object(ai,'CONFIG',SimpleNamespace(ai_api_key='synthetic-only')))
        stack.enter_context(patch.object(ai,'_request_policy',return_value=(False,False)))

    def profile(self):
        return load_profiles(self.path,group_id=123,user_ids=['7'])[0]

    async def refresh(self, *, strict=True):
        return await summary.refresh_member_summary(self.path,self.root,
            group_id=123,user_id='7',strict=strict,max_batches=1)

    async def test_exact_limit_is_accepted_without_extra_model_request(self):
        completion = AsyncMock(return_value='合'*300)
        with patch.object(ai,'_complete',completion):
            self.assertTrue(await self.refresh())
        completion.assert_awaited_once()
        self.assertEqual(300,len(self.profile().summary))
        self.assertEqual(20,self.profile().summary_through_fact_id)

    async def test_output_rejections_have_distinct_safe_codes_and_keep_summary(self):
        cases = (
            ('合'*301,'member_summary_too_long'),
            ('synthetic token value','member_summary_secret_blocked'),
            ('  ','member_summary_empty_response'),
            (None,'member_summary_invalid_response'),
        )
        for output, expected in cases:
            with self.subTest(code=expected),patch.object(ai,'_complete',AsyncMock(return_value=output)) as completion:
                with self.assertRaises(ai.MemberMemoryError) as raised:
                    await self.refresh()
                self.assertEqual(expected,raised.exception.code)
                self.assertEqual(expected,str(raised.exception))
                self.assertTrue(raised.exception.retryable)  # Existing bounded retry policy is unchanged.
                self.assertEqual(2 if expected == 'member_summary_too_long' else 1,
                                 completion.await_count)
                self.assertEqual('旧'*278,self.profile().summary)
                self.assertEqual(0,self.profile().summary_through_fact_id)

    async def test_default_caller_keeps_none_or_false_compatibility(self):
        for output in ('合'*301,'synthetic token value',None):
            with self.subTest(kind=type(output).__name__),patch.object(ai,'_complete',AsyncMock(return_value=output)):
                self.assertFalse(await self.refresh(strict=False))
        self.assertFalse(commit_summary(self.path,self.root,group_id=123,user_id='7',
            previous_through_id=0,through_fact_id=20,summary='synthetic token'))

    async def test_fact_version_conflict_is_distinct_from_quality_rejection(self):
        async def changed(*args,**kwargs):
            with sqlite3.connect(self.path) as db:
                db.execute('UPDATE member_memory_facts SET version=version+1 WHERE id=1')
            return '合成摘要'
        with patch.object(ai,'_complete',side_effect=changed):
            with self.assertRaises(ai.MemberMemoryError) as raised:
                await self.refresh()
        self.assertEqual('member_summary_fact_conflict',raised.exception.code)
        self.assertEqual(0,self.profile().summary_through_fact_id)

    async def test_newer_summary_cursor_is_not_overwritten_and_has_own_code(self):
        async def changed(*args,**kwargs):
            self.assertTrue(commit_summary(self.path,self.root,group_id=123,user_id='7',
                previous_through_id=0,through_fact_id=1,summary='已更新的合成摘要'))
            return '旧输出'
        with patch.object(ai,'_complete',side_effect=changed):
            with self.assertRaises(ai.MemberMemoryError) as raised:
                await self.refresh()
        self.assertEqual('member_summary_cursor_conflict',raised.exception.code)
        self.assertEqual('已更新的合成摘要',self.profile().summary)

    async def test_upstream_and_storage_failures_do_not_expose_exception_details(self):
        from plugins.llm_gateway.errors import GatewayAuthenticationError, GatewayServerError
        cases = (
            (GatewayAuthenticationError('SYNTHETIC_PRIVATE_DETAIL'),'member_summary_auth_error'),
            (GatewayServerError('SYNTHETIC_PRIVATE_DETAIL'),'member_summary_server_error'),
            (httpx.ReadTimeout('SYNTHETIC_PRIVATE_DETAIL'),'member_summary_request_timeout'),
            (httpx.ConnectError('SYNTHETIC_PRIVATE_DETAIL'),'member_summary_transport_error'),
            (IndexError('SYNTHETIC_PRIVATE_DETAIL'),'member_summary_invalid_response'),
        )
        for status, suffix in ((401,'auth_error'),(429,'rate_limited'),(503,'server_error')):
            response = httpx.Response(status,request=httpx.Request('POST','https://synthetic.invalid'))
            cases += ((httpx.HTTPStatusError('SYNTHETIC_PRIVATE_DETAIL',request=response.request,response=response),
                       'member_summary_'+suffix),)
        for failure, expected in cases:
            with self.subTest(code=expected),patch.object(ai,'_complete',AsyncMock(side_effect=failure)):
                with self.assertRaises(ai.MemberMemoryError) as raised:
                    await self.refresh()
                self.assertEqual(expected,raised.exception.code)
                self.assertNotIn('SYNTHETIC_PRIVATE_DETAIL',str(raised.exception))
        with patch.object(ai,'_complete',AsyncMock(return_value='合成摘要')),patch.object(summary,'commit_summary',side_effect=sqlite3.OperationalError('SYNTHETIC_PRIVATE_DETAIL')):
            with self.assertRaises(ai.MemberMemoryError) as raised:
                await self.refresh()
        self.assertEqual('member_summary_storage_error',raised.exception.code)
        self.assertNotIn('SYNTHETIC_PRIVATE_DETAIL',str(raised.exception))

    async def test_cancellation_propagates_without_failure_classification(self):
        with patch.object(ai,'_complete',AsyncMock(side_effect=asyncio.CancelledError())):
            with self.assertRaises(asyncio.CancelledError):
                await self.refresh()
        self.assertEqual(0,self.profile().summary_through_fact_id)

    async def test_mirror_unavailable_does_not_reverse_successful_database_commit(self):
        with patch.object(ai,'_complete',AsyncMock(return_value='合成摘要')),patch('plugins.member_memory.store._write_mirror',return_value=False):
            self.assertTrue(await self.refresh())
        self.assertEqual(20,self.profile().summary_through_fact_id)

    async def test_direct_default_generation_keeps_secret_check_at_commit_boundary(self):
        from plugins.member_memory.store import pending_summary_batch
        work = pending_summary_batch(self.path,group_id=123,user_id='7')
        with patch.object(ai,'_complete',AsyncMock(return_value='synthetic token')):
            self.assertEqual('synthetic token',await ai.generate_memory_summary(work.summary,work.facts))

    async def test_classified_error_is_persisted_with_existing_retry_budget(self):
        from plugins.private_memory.schema import migrate
        from plugins.private_memory.jobs import MemoryJobQueue,MemoryJobWorker
        from datetime import datetime,timezone,timedelta
        migrate(self.path)
        now = datetime.now(timezone.utc)
        queue = MemoryJobQueue(self.path,member_batch_delay_seconds=0)
        identity = queue.enqueue(job_type='member_facts',conversation_kind='group',group_id=123,
                                 user_id='7',input_through_id=1,expected_version=0)
        async def processor(job):
            return await self.refresh()
        worker = MemoryJobWorker(queue,processor,worker_id='synthetic-worker',allowed_job_types=lambda:{'member_facts'})
        with patch.object(ai,'_complete',AsyncMock(return_value='合'*301)) as completion:
            for index in range(3):
                current = queue.claim(worker_id='synthetic-worker',now=now+timedelta(seconds=60*(index+1)),
                                      limit=1,allowed_job_types={'member_facts'})[0]
                await worker._process(current)
                row = queue.get(identity)
                self.assertEqual('member_summary_too_long',row.error_code)
                self.assertEqual('pending' if index<2 else 'failed',row.status)
                self.assertEqual(index+1,row.attempts)
        self.assertEqual(6, completion.await_count)  # 2 logical generations per queue attempt.
        self.assertEqual(0,self.profile().summary_through_fact_id)
