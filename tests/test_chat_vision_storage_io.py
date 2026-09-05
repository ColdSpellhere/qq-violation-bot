from __future__ import annotations

import asyncio
import importlib
import threading
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from plugins.chat_vision import service
from plugins.chat_vision.store import ChatVisionStore


class ChatVisionStorageIOTests(unittest.IsolatedAsyncioTestCase):
    async def test_message_wait_reads_sqlite_off_the_event_loop(self):
        with TemporaryDirectory() as directory:
            store=ChatVisionStore(Path(directory)/'archive.db')
            original=store.for_message
            loop_thread=threading.get_ident()
            def read(*args):
                self.assertNotEqual(loop_thread,threading.get_ident())
                return original(*args)
            with patch.object(service,'STORE',store),patch.object(store,'for_message',side_effect=read):
                self.assertEqual([],await service.wait_for_message_assets(100,'synthetic',timeout=0))

    async def test_wait_deadline_includes_a_slow_sqlite_read(self):
        import time
        io=importlib.import_module('plugins.chat_vision.storage_io')
        with TemporaryDirectory() as directory:
            store=ChatVisionStore(Path(directory)/'archive.db')
            def read(*args):
                time.sleep(.12)
                return []
            with patch.object(service,'STORE',store),patch.object(store,'for_message',side_effect=read):
                started=asyncio.get_running_loop().time()
                result=await service.wait_for_message_assets(100,'synthetic',timeout=.02)
                self.assertEqual([],result)
                self.assertLess(asyncio.get_running_loop().time()-started,.08)
                await io.drain_storage_calls()

    async def test_cancelled_claimant_releases_only_its_own_successful_claim(self):
        for owns_claim in (True,False):
            with self.subTest(owns_claim=owns_claim), TemporaryDirectory() as directory:
                store=ChatVisionStore(Path(directory)/'archive.db')
                asset=store.ensure_pending(100,'claim',1,'https://cdn.invalid/a.jpg',1)
                if not owns_claim:store.claim(asset.id,3)
                original=store.claim
                entered=threading.Event();release=threading.Event()
                def claim(*args):
                    result=original(*args)
                    entered.set();release.wait(1)
                    return result
                with patch.object(store,'claim',side_effect=claim):
                    task=asyncio.create_task(service._claim_asset(store,asset.id))
                    try:
                        for _ in range(100):
                            if entered.is_set():break
                            await asyncio.sleep(.005)
                        self.assertTrue(entered.is_set())
                        task.cancel();await asyncio.sleep(0)
                    finally:
                        release.set()
                    with self.assertRaises(asyncio.CancelledError):await task
                saved=store.for_message(100,'claim')[0]
                self.assertEqual('pending' if owns_claim else 'processing',saved.status)
                self.assertEqual(0 if owns_claim else 1,saved.attempts)

    async def test_cancelled_claim_remains_cancelled_when_storage_completion_fails(self):
        import sqlite3
        with TemporaryDirectory() as directory:
            store=ChatVisionStore(Path(directory)/'archive.db')
            entered=threading.Event();release=threading.Event()
            def claim(*args):
                entered.set();release.wait(1)
                raise sqlite3.OperationalError('synthetic failed claim')
            with patch.object(store,'claim',side_effect=claim):
                task=asyncio.create_task(service._claim_asset(store,1))
                for _ in range(100):
                    if entered.is_set():break
                    await asyncio.sleep(.005)
                task.cancel();await asyncio.sleep(0);release.set()
                with self.assertRaises(asyncio.CancelledError):await task

    async def test_cancellation_keeps_storage_slot_occupied_until_thread_finishes(self):
        io=importlib.import_module('plugins.chat_vision.storage_io')
        started=0;active=0;peak=0
        lock=threading.Lock();release=threading.Event()
        def read():
            nonlocal started,active,peak
            with lock:started+=1;active+=1;peak=max(peak,active)
            try:release.wait(1)
            finally:
                with lock:active-=1
            return 1
        tasks=[asyncio.create_task(io.storage_call(read)) for _ in range(3)]
        try:
            for _ in range(100):
                if started==3:break
                await asyncio.sleep(.005)
            self.assertEqual(3,started)
            tasks[0].cancel()
            with self.assertRaises(asyncio.CancelledError):await tasks[0]
            fourth=asyncio.create_task(io.storage_call(read));tasks.append(fourth)
            await asyncio.sleep(.03)
            self.assertEqual(3,started)
            self.assertEqual(3,peak)
        finally:
            release.set()
            await asyncio.gather(*tasks,return_exceptions=True)
            await io.drain_storage_calls()
        self.assertEqual(4,started)
        self.assertLessEqual(peak,3)
