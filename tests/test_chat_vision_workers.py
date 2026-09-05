from __future__ import annotations

import asyncio
import sqlite3
import time
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from plugins.chat_vision import matcher, service
from plugins.chat_vision.download import DownloadedChatImage
from plugins.chat_vision.store import ChatVisionStore
from tests.test_chat_vision_ingestion import GROUP_ID, JPEG_ONE, _event, _image


class ChatVisionWorkerTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.directory=TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root=Path(self.directory.name)
        self.store=ChatVisionStore(self.root/'archive.db')
        self.allowed=True
        self.config=SimpleNamespace(chat_vision_enabled=True,
            chat_vision_root=self.root/'data'/'chat_vision'/'images',chat_vision_retention_days=7,
            chat_vision_max_bytes=1024,chat_vision_timeout=1,chat_vision_max_retries=3,
            chat_vision_recovery_window_seconds=900,chat_vision_recovery_max_assets=20,
            chat_vision_model='same-vision-model',ai_base_url='https://llm.invalid',ai_api_key='synthetic')
        self.features=SimpleNamespace(image_understanding_allowed=lambda:self.allowed,
            group_chat_allowed=lambda group_id:self.allowed and group_id==GROUP_ID,
            llm_gateway_allowed=lambda domain:False)
        for target,attrs in ((service,{'STORE':self.store,'CONFIG':self.config,'FEATURES':self.features}),
            (matcher,{'CONFIG':self.config,'FEATURES':self.features})):
            holder=patch.multiple(target,**attrs);holder.start();self.addCleanup(holder.stop)
        self.download=AsyncMock(return_value=DownloadedChatImage(JPEG_ONE,'image/jpeg','jpg'))
        self.describe=AsyncMock(return_value='合成图片描述')
        for name,mock in (('download_chat_image',self.download),('describe_image',self.describe)):
            holder=patch.object(service,name,mock);holder.start();self.addCleanup(holder.stop)

    async def asyncTearDown(self):
        if hasattr(service,'stop_workers'):
            await service.stop_workers()

    async def test_matcher_returns_without_waiting_for_network(self):
        event=_event(_image('https://cdn.invalid/a.jpg'))
        await asyncio.wait_for(matcher.collect_chat_images(event),0.2)
        self.download.assert_not_awaited()
        self.assertEqual('pending',self.store.for_message(GROUP_ID,str(event.message_id))[0].status)

    async def test_global_workers_bound_concurrent_messages_and_wait_only_for_current_assets(self):
        active=peak=0
        release=asyncio.Event()
        all_started=asyncio.Event()
        async def describe(*args,**kwargs):
            nonlocal active,peak
            active+=1;peak=max(peak,active)
            if active==3:all_started.set()
            try:await release.wait()
            finally:active-=1
            return '合成描述'
        self.describe.side_effect=describe
        service.start_workers(self.store)
        for index in range(15):
            await service.enqueue_image_event(_event(_image('https://cdn.invalid/a.jpg'),message_id=100+index))
        await asyncio.wait_for(all_started.wait(),1)
        self.assertEqual(3,peak)
        self.assertEqual(3,len(service._workers))
        early=await service.wait_for_message_assets(GROUP_ID,'100',timeout=0.01)
        self.assertEqual('processing',early[0].status)
        self.assertEqual([],await service.wait_for_message_assets(GROUP_ID+1,'100',timeout=.01))
        release.set()
        done=await service.wait_for_message_assets(GROUP_ID,'114',timeout=2)
        self.assertEqual('ready',done[0].status)
        self.assertEqual(3,peak)

    async def test_shutdown_releases_claim_and_restart_recovers_without_redownload(self):
        begun=asyncio.Event()
        async def stuck(*args,**kwargs):
            begun.set();await asyncio.Event().wait()
        self.describe.side_effect=stuck
        service.start_workers(self.store)
        await service.enqueue_image_event(_event(_image('https://cdn.invalid/a.jpg')))
        await asyncio.wait_for(begun.wait(),1)
        await service.stop_workers()
        interrupted=self.store.for_message(GROUP_ID,'456')[0]
        self.assertEqual('pending',interrupted.status)
        self.assertEqual(0,interrupted.attempts)
        self.assertEqual([],service._workers)
        self.describe.side_effect=None
        reopened=ChatVisionStore(self.root/'archive.db')
        service.start_workers(reopened)
        done=await service.wait_for_message_assets(GROUP_ID,'456',timeout=2)
        self.assertEqual('ready',done[0].status)
        self.assertEqual(1,self.download.await_count)

    async def test_runtime_retry_is_durable_and_bounded(self):
        self.describe.side_effect=[RuntimeError('synthetic'), '恢复后的描述']
        with patch.object(service,'_RETRY_BASE_SECONDS',.03),patch.object(service,'_WORKER_POLL_SECONDS',.01):
            service.start_workers(self.store)
            await service.enqueue_image_event(_event(_image('https://cdn.invalid/a.jpg')))
            result=await service.wait_for_message_assets(GROUP_ID,'456',timeout=1)
        self.assertEqual('ready',result[0].status)
        self.assertEqual(2,result[0].attempts)
        self.assertEqual(1,self.download.await_count)

    async def test_closed_gate_cancels_inflight_description_and_never_marks_ready(self):
        started=asyncio.Event();cancelled=asyncio.Event()
        async def stuck(*args,**kwargs):
            started.set()
            try:await asyncio.Event().wait()
            finally:cancelled.set()
        self.describe.side_effect=stuck
        service.start_workers(self.store)
        await service.enqueue_image_event(_event(_image('https://cdn.invalid/a.jpg')))
        await asyncio.wait_for(started.wait(),1)
        self.allowed=False
        await asyncio.wait_for(cancelled.wait(),.5)
        await asyncio.sleep(.01)
        result=self.store.for_message(GROUP_ID,'456')[0]
        self.assertEqual('pending',result.status)
        self.assertIsNone(result.description)
        self.assertEqual(1,self.describe.await_count)

    async def test_waiter_cancellation_does_not_cancel_owned_worker(self):
        release=asyncio.Event();started=asyncio.Event()
        async def slow(*args,**kwargs):
            started.set();await release.wait();return '合成描述'
        self.describe.side_effect=slow
        service.start_workers(self.store)
        await service.enqueue_image_event(_event(_image('https://cdn.invalid/a.jpg')))
        await asyncio.wait_for(started.wait(),1)
        waiter=asyncio.create_task(service.wait_for_message_assets(GROUP_ID,'456',timeout=1))
        await asyncio.sleep(0);waiter.cancel()
        with self.assertRaises(asyncio.CancelledError):await waiter
        release.set()
        self.assertEqual('ready',(await service.wait_for_message_assets(GROUP_ID,'456',timeout=1))[0].status)

    async def test_admission_caps_assets_and_images_without_network_tasks(self):
        with patch.object(service,'_MAX_PENDING_ASSETS',5):
            first=await service.enqueue_image_event(_event(*[_image('https://cdn.invalid/a.jpg') for _ in range(20)]))
            second=await service.enqueue_image_event(_event(*[_image('https://cdn.invalid/a.jpg') for _ in range(20)],message_id=457))
        self.assertEqual(4,len(first))
        self.assertEqual(1,len(second))
        self.download.assert_not_awaited()

    async def test_hard_restart_recovers_processing_claim_only_once(self):
        asset=self.store.ensure_pending(GROUP_ID,'restart',1,'https://cdn.invalid/a.jpg',int(time.time()))
        self.store.claim(asset.id,3)
        service.start_workers(self.store)
        tasks=list(service._workers)
        service.start_workers(self.store)
        self.assertEqual(tasks,service._workers)
        result=await service.wait_for_message_assets(GROUP_ID,'restart',timeout=1)
        self.assertEqual('ready',result[0].status)
        self.assertEqual(1,result[0].attempts)
        self.describe.assert_awaited_once()

    async def test_database_read_error_does_not_kill_workers(self):
        original=self.store.claimable
        calls=0
        def first_failed(*args,**kwargs):
            nonlocal calls
            calls+=1
            if calls==1:raise sqlite3.OperationalError('synthetic busy')
            return original(*args,**kwargs)
        with patch.object(self.store,'claimable',side_effect=first_failed), patch.object(service,'_WORKER_POLL_SECONDS',.01):
            service.start_workers(self.store)
            await service.enqueue_image_event(_event(_image('https://cdn.invalid/a.jpg')))
            result=await service.wait_for_message_assets(GROUP_ID,'456',timeout=1)
        self.assertEqual('ready',result[0].status)
        self.assertTrue(all(not task.done() for task in service._workers))

    async def test_expired_or_disallowed_pending_assets_are_not_recovered(self):
        self.store.ensure_pending(GROUP_ID,'old',1,'https://cdn.invalid/a.jpg',int(time.time())-1000)
        self.store.ensure_pending(GROUP_ID+1,'outside',1,'https://cdn.invalid/a.jpg',int(time.time()))
        self.store.ensure_pending(GROUP_ID,'future',1,'https://cdn.invalid/a.jpg',int(time.time())+3600)
        with patch.object(service,'_WORKER_POLL_SECONDS',.01):
            service.start_workers(self.store);await asyncio.sleep(.05)
        self.download.assert_not_awaited()
