"""Bounded durable processing tests; all models and transport are injected."""
import sqlite3
import unittest
from datetime import timedelta
from unittest.mock import AsyncMock, patch
from tests.test_memory_hardening import MemoryTestCase, NOW, job
from plugins.private_memory.jobs import MemoryJobQueue, MemoryJobWorker
from plugins.private_memory.models import ConversationScope, MemoryJobContinuation
from plugins.member_memory import ai

class MemoryBatchTests(MemoryTestCase):
    async def test_fact_backlog_progresses_in_bounded_restartable_batches(self):
        ids=[self.append(str(i),'消息'*10) for i in range(7)]
        extract=AsyncMock(return_value=())
        for index in range(4):
            result=await self.processor(extract=extract,batch_messages=2,batch_chars=25).process(job('private_facts',ids[-1]))
            self.assertLessEqual(sum(len(x.text) for x in extract.await_args.args[0]),25)
            self.assertLessEqual(len(extract.await_args.args[0]),2)
        self.assertTrue(result)
        self.assertEqual(ids[-1], self.store.fact_progress(user_id='200')[0])

    def test_group_batch_is_durable_and_fifth_message_is_runnable(self):
        with patch('plugins.private_memory.jobs._now',return_value=NOW):
            queue=MemoryJobQueue(self.db)
            args=dict(job_type='member_facts',conversation_kind='group',group_id=123,user_id='200',expected_version=0)
            first=queue.enqueue(input_through_id=1,**args)
            self.assertEqual((),queue.claim(worker_id='w',now=NOW,limit=1,allowed_job_types={'member_facts'}))
            for mid in range(2,6): self.assertEqual(first,queue.enqueue(input_through_id=mid,**args))
            restored=MemoryJobQueue(self.db)
            jobs=restored.claim(worker_id='w',now=NOW,limit=1,allowed_job_types={'member_facts'})
            self.assertEqual(1,len(jobs));self.assertEqual(5,jobs[0].input_through_id)

    async def test_group_model_failure_is_observable(self):
        from plugins.chat_archive.db import ContextMessage
        with patch.object(ai,'CONFIG') as config, patch.object(ai,'_complete',AsyncMock(side_effect=ValueError('synthetic failure'))):
            config.ai_api_key='synthetic'
            with self.assertRaises(ai.MemberMemoryError):
                await ai.extract_memory_candidates([ContextMessage('a','文字','m','200')],strict=True)

    def test_clear_invalidates_inflight_fact_commit(self):
        mid=self.append('clear')
        through,version=self.store.fact_progress(user_id='200')
        self.store.clear_private_layers(user_id='200',actor='900',reason='测试',operation_id=1)
        self.assertFalse(self.store.commit_fact_batch(user_id='200',candidates=(),expected_through_id=through,
            expected_version=version,through_id=mid,expected_source_ids=()))
        self.assertEqual(mid,self.store.fact_progress(user_id='200')[0])

    def test_preview_retention_erases_payload_but_preserves_audit_metadata(self):
        from plugins.memory_governance.retention import prune_previews
        with sqlite3.connect(self.db) as c:
            c.execute("INSERT INTO memory_pending_operations(confirmation_token_hash,operator_user_id,operation_type,target_kind,target_user_id,payload_json,preview_text,expires_at,created_at) VALUES(?,?,?,?,?,?,?,?,?)",('a'*64,'900','delete_fact','private','200','{\"content\":\"old\"}','old', '2026-08-01T00:00:00Z','2026-08-01T00:00:00Z'))
        self.assertEqual(1,prune_previews(self.db,now=NOW,retention_days=7))
        with sqlite3.connect(self.db) as c:
            self.assertEqual(('{}','','900'),c.execute('SELECT payload_json,preview_text,operator_user_id FROM memory_pending_operations').fetchone())

    async def test_group_batch_consumes_only_its_scope_once_after_restart(self):
        from dataclasses import replace
        from plugins.chat_archive.db import archive_payload
        from plugins.member_memory.processing import process_member_job
        from plugins.member_memory.store import load_profiles
        for group, uid, mid in ((123,'200','a'), (456,'200','b'), (123,'300','c'), (123,'200','d')):
            archive_payload(self.db,group,dict(message_id=mid,group_id=group,user_id=uid,event_time=1,
                sender={'nickname':'成员'},segments=[],plaintext='我喜欢读书'))
        current = replace(job('member_facts',4),scope=ConversationScope('group','200',123),input_from_id=1)
        async def extract(context, **kwargs):
            return [dict(user_id=message.user_id,trait='喜欢读书',evidence_message_id=message.message_id,quote=message.text) for message in context]
        model=AsyncMock(side_effect=extract)
        with patch('plugins.member_memory.processing.extract_memory_candidates',model):
            for _ in range(2):
                result=await process_member_job(current,path=self.db,root=self.root/'mirror',allowed=lambda g: True,
                    summary_enabled=False,batch_messages=1)
            self.assertTrue(result)
            self.assertTrue(await process_member_job(current,path=self.db,root=self.root/'mirror',allowed=lambda g: True,summary_enabled=False))
        self.assertEqual(2,model.await_count)
        self.assertEqual(['a','d'],[call.args[0][0].message_id for call in model.await_args_list])
        self.assertEqual(['a','d'],[fact.evidence_message_id for fact in load_profiles(self.db,group_id=123,user_ids=['200'])[0].traits])

    async def test_retention_during_summary_prevents_deleted_source_revival(self):
        self.append('old'); latest=self.append('new')
        async def summarize(previous,messages):
            self.store.purge_expired(now=NOW,retention_days=30,max_messages=1)
            return '摘要含已清除旧来源'
        self.assertFalse(await self.processor(summarize=summarize).process(job('private_summary',latest)))
        self.assertIsNone(self.store.get_summary(user_id='200'))

    async def test_group_fact_deleted_during_model_invalidates_summary_snapshot(self):
        from plugins.chat_archive.db import ContextMessage
        from plugins.member_memory.store import apply_candidates
        from plugins.member_memory.summary import refresh_member_summary
        context=[ContextMessage('成员','我喜欢文学','one','200')]
        apply_candidates(self.db,self.root/'mirror',group_id=123,context=context,
            candidates=[dict(user_id='200',trait=f'文学方向{i}',evidence_message_id='one',quote='我喜欢文学') for i in range(5)])
        async def summarize(previous,facts):
            with sqlite3.connect(self.db) as connection:
                connection.execute("UPDATE member_memory_facts SET status='deleted',version=version+1 WHERE id=1")
            return '失效摘要'
        with patch('plugins.member_memory.summary.generate_memory_summary',summarize):
            self.assertFalse(await refresh_member_summary(self.db,self.root/'mirror',group_id=123,user_id='200'))

    def test_debounce_deadline_and_renewal_do_not_lose_pending_messages(self):
        with patch('plugins.private_memory.jobs._now',return_value=NOW):
            queue=MemoryJobQueue(self.db,lease_seconds=10)
            args=dict(job_type='relationship',conversation_kind='private',group_id=None,user_id='200',expected_version=0)
            first=queue.enqueue(input_through_id=1,**args)
        for seconds in (50,100,150,200,250,290):
            with patch('plugins.private_memory.jobs._now',return_value=NOW+timedelta(seconds=seconds)):
                queue.enqueue(input_through_id=seconds,**args)
        self.assertEqual('2026-09-05T00:05:00Z',queue.get(first).next_run_at)
        owned=queue.claim(worker_id='w',now=NOW+timedelta(seconds=300),limit=1,allowed_job_types={'relationship'})[0]
        self.assertEqual(1,owned.input_from_id)
        self.assertTrue(queue.renew(owned,worker_id='w',now=NOW+timedelta(seconds=309)))
        self.assertEqual(0,queue.recover_expired_leases(now=NOW+timedelta(seconds=310)))
        self.assertFalse(queue.renew(owned,worker_id='other',now=NOW))

    def test_clear_cancels_delivery_plan_in_same_transaction_without_other_users(self):
        from plugins.memory_governance.commands import MemoryCommand, MemoryScope
        from plugins.memory_governance.service import MemoryGovernanceService
        self.append('clear-ledger')
        with sqlite3.connect(self.db) as c:
            c.execute('CREATE TABLE chat_delivery_parts(event_key TEXT,part INTEGER,kind TEXT,user_id TEXT,group_id TEXT,reply_text TEXT,status TEXT,receipt TEXT,error TEXT,updated_at REAL,PRIMARY KEY(event_key,part))')
            for uid in ('200','201'):
                c.execute('INSERT INTO chat_delivery_parts VALUES(?,0,?,?,?,?,?,?,?,1)',(uid,'private',uid,'','reply','sent','receipt','detail'))
        service=MemoryGovernanceService(self.db,private_allowed_user_ids=('200',))
        command=MemoryCommand('clear_private',scope=MemoryScope('private','200'))
        preview=service.preview(command,actor='900',now=NOW)
        self.assertTrue(service.confirm(preview.token,actor='900',reason='清理',now=NOW).success)
        with sqlite3.connect(self.db) as c:
            self.assertEqual(('', '', '', 'cancelled',''),c.execute("SELECT reply_text,receipt,error,status,user_id FROM chat_delivery_parts WHERE event_key='200'").fetchone())
            self.assertEqual(('reply','sent','201'),c.execute("SELECT reply_text,status,user_id FROM chat_delivery_parts WHERE event_key='201'").fetchone())

    async def test_group_failure_retries_from_persisted_queue_without_advancing_source(self):
        from plugins.chat_archive.db import archive_payload
        from plugins.member_memory.processing import process_member_job
        archive_payload(self.db,123,dict(message_id='retry',group_id=123,user_id='200',event_time=1,
            sender={},segments=[],plaintext='我喜欢阅读'))
        with patch('plugins.private_memory.jobs._now',return_value=NOW):
            queue=MemoryJobQueue(self.db,member_batch_delay_seconds=0)
            identity=queue.enqueue(job_type='member_facts',conversation_kind='group',group_id=123,user_id='200',input_through_id=1,expected_version=0)
            current=queue.claim(worker_id='w',now=NOW,limit=1,allowed_job_types={'member_facts'})[0]
            async def process(current):
                return await process_member_job(current,path=self.db,root=self.root/'mirror',allowed=lambda g:True,summary_enabled=False)
            worker=MemoryJobWorker(queue,process,allowed_job_types=lambda:{'member_facts'},worker_id='w')
            with patch('plugins.member_memory.processing.extract_memory_candidates',AsyncMock(side_effect=ai.MemberMemoryError('synthetic'))):
                await worker._process(current)
            failed=queue.get(identity)
            self.assertEqual(('pending','member_memory_processing_error'),(failed.status,failed.error_code))
            with sqlite3.connect(self.db) as c:
                self.assertEqual(0,c.execute('SELECT count(*) FROM group_fact_progress').fetchone()[0])
            restored=MemoryJobQueue(self.db)
            current=restored.claim(worker_id='w',now=NOW+timedelta(seconds=5),limit=1,allowed_job_types={'member_facts'})[0]
            with patch('plugins.member_memory.processing.extract_memory_candidates',AsyncMock(return_value=[])):
                await worker._process(current)
            self.assertEqual('succeeded',restored.get(identity).status)

    def test_fact_insert_failure_rolls_back_progress_and_data_together(self):
        from plugins.private_memory.models import PrivateFactCandidate
        mid=self.append('atomic','我喜欢阅读')
        with sqlite3.connect(self.db) as c:
            c.execute("CREATE TRIGGER synthetic_failure BEFORE INSERT ON private_memory_facts BEGIN SELECT RAISE(ABORT,'synthetic'); END")
        with self.assertRaises(sqlite3.IntegrityError):
            self.store.commit_fact_batch(user_id='200',candidates=(PrivateFactCandidate('200','喜欢阅读','atomic','我喜欢阅读'),),expected_through_id=0,expected_version=0,through_id=mid,expected_source_ids=(mid,))
        self.assertEqual((0,0),self.store.fact_progress(user_id='200'))
        self.assertEqual((),self.store.active_facts(user_id='200',limit=10))
