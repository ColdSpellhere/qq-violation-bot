"""Synthetic regressions from independent review; never contact QQ or a model."""
import asyncio
import sqlite3
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from plugins.random_chat.admission import run_chat_turn
from plugins.random_chat.delivery import deliver_replies
from plugins.random_chat.delivery_store import DeliveryLedger
from plugins.web_search.policy import build_search_query
from tests import test_private_memory_integration as integration
_event = integration._event
private_matcher = integration.private_matcher

class EphemeralDuplicateTests(unittest.IsolatedAsyncioTestCase):
    setUp = integration.PrivateMemoryIntegrationTests.setUp
    tearDown = integration.PrivateMemoryIntegrationTests.tearDown
    _patch_runtime = integration.PrivateMemoryIntegrationTests._patch_runtime
    async def test_disabled_memory_still_deduplicates_same_private_event(self):
        self.features.set_switch('private_memory_enabled',False,'admin')
        generate=AsyncMock(return_value='合成回复')
        bot=AsyncMock();bot.send_private_msg.return_value={'message_id':123}
        with self._patch_runtime(),patch.object(private_matcher,'generate_reply',generate),patch.object(private_matcher,'choose_sticker',return_value=None):
            await private_matcher.handle_private_message(bot,_event('合成消息',message_id=777))
            await private_matcher.handle_private_message(bot,_event('合成消息',message_id=777))
        self.assertEqual(1,generate.await_count)
        self.assertEqual(1,bot.send_private_msg.await_count)
        with sqlite3.connect(self.database) as c:
            self.assertEqual(0,c.execute('SELECT count(*) FROM private_chat_messages').fetchone()[0])
            self.assertIsNone(c.execute("SELECT name FROM sqlite_master WHERE name='chat_delivery_parts'").fetchone())

    async def test_skipped_and_busy_private_decisions_are_not_regenerated(self):
        from plugins.random_chat.ai import RandomChatAIError
        self.features.set_switch('private_memory_enabled', False, 'admin')
        for index, outcome in enumerate((None, RandomChatAIError('synthetic', retry_later=True))):
            with self.subTest(outcome=type(outcome).__name__):
                generate = AsyncMock(return_value=outcome) if outcome is None else AsyncMock(side_effect=outcome)
                bot = AsyncMock()
                bot.send_private_msg.return_value = {'message_id':123}
                with self._patch_runtime(), patch.object(private_matcher,'generate_reply',generate):
                    for _ in range(2):
                        await private_matcher.handle_private_message(bot,_event('synthetic',message_id=900+index))
                self.assertEqual(1,generate.await_count)
                self.assertEqual(0 if outcome is None else 1,bot.send_private_msg.await_count)

class DeliveryDeadlineTests(unittest.IsolatedAsyncioTestCase):
    async def test_cancelled_claim_known_not_sent_is_retryable(self):
        with tempfile.TemporaryDirectory() as raw:
            ledger=DeliveryLedger(Path(raw).resolve()/'delivery.db')
            entered=threading.Event();release=threading.Event();real_claim=ledger.claim
            def delayed_claim(*args):
                entered.set();release.wait(2);return real_claim(*args)
            send=AsyncMock(return_value={'message_id':123})
            with patch.object(ledger,'claim',side_effect=delayed_claim):
                task=asyncio.create_task(deliver_replies(('synthetic',),send=send,ledger=ledger,delivery_key='audit'))
                while not entered.is_set(): await asyncio.sleep(.001)
                task.cancel();await asyncio.sleep(.01)
                release.set()
                with self.assertRaises(asyncio.CancelledError): await task
                await asyncio.sleep(.01)
            self.assertEqual('pending',ledger.parts('audit')[0]['status'])
            send.assert_not_awaited()
            await deliver_replies(('synthetic',),send=send,ledger=ledger,delivery_key='audit')
            self.assertEqual(1,send.await_count)

    async def test_cancelled_local_preflight_remains_retryable(self):
        from plugins.random_chat.delivery import DeliveryNotSent
        from plugins.random_chat.delivery_store import MemoryDeliveryLedger
        ledger = MemoryDeliveryLedger()
        external_send = AsyncMock()
        async def preflight(_):
            try:
                raise asyncio.CancelledError()
            except asyncio.CancelledError as interrupted:
                raise DeliveryNotSent('synthetic preflight') from interrupted
        with self.assertRaises(asyncio.CancelledError):
            await deliver_replies(('synthetic',),send=preflight,ledger=ledger,delivery_key='preflight')
        self.assertEqual('pending',ledger.parts('preflight')[0]['status'])
        await deliver_replies(('synthetic',),send=external_send,ledger=ledger,delivery_key='preflight')
        external_send.assert_awaited_once()

    async def test_noncooperative_work_cannot_return_success_after_deadline(self):
        async def blocking():
            time.sleep(.03)
            return 'completed'
        self.assertIsNone(await run_chat_turn('synthetic',blocking,timeout=.002))

    async def test_expired_turn_never_calls_send_after_noncooperative_work(self):
        send=AsyncMock(return_value={'message_id':123})
        async def blocking():
            time.sleep(.03)
            return await deliver_replies(('synthetic',),send=send)
        self.assertIsNone(await run_chat_turn('synthetic',blocking,timeout=.002))
        send.assert_not_awaited()

class SearchPrivacyReviewTests(unittest.TestCase):
    def test_credentials_and_private_ipv6_are_not_disclosed(self):
        for text in ('查一下密码是 synthetic-passphrase','查一下 token synthetic-token-value','查一下 fd00:1234::1 状态',
                     '查一下 fe80::abcd%en0 路由','查一下 ＰＡＳＳＷＯＲＤ＝synthetic','查一下 https://example.com/?password%3Dsynthetic'):
            with self.subTest(text=text):
                self.assertIsNone(build_search_query(text,addressed=True,private=False))

    def test_normal_public_questions_are_preserved(self):
        for text in ('搜一下 OAuth token 是什么','搜一下 密码学入门','搜一下 IPv6 技术原理','今天北京天气怎么样'):
            with self.subTest(text=text):
                self.assertIsNotNone(build_search_query(text,addressed=True,private=False))

class AdditionalCancellationTests(unittest.IsolatedAsyncioTestCase):
    async def test_cancelled_losing_claim_never_releases_other_sender(self):
        with tempfile.TemporaryDirectory() as raw:
            ledger = DeliveryLedger(Path(raw) / 'ledger.db')
            entered, release = threading.Event(), threading.Event()
            def losing_claim(key, part):
                # Another process wins immediately before this thread's CAS.
                self.assertTrue(DeliveryLedger(ledger.path).claim(key, part))
                entered.set()
                release.wait(2)
                return False
            send = AsyncMock()
            with patch.object(ledger, 'claim', side_effect=losing_claim):
                task = asyncio.create_task(deliver_replies(('synthetic',), send=send, ledger=ledger, delivery_key='race'))
                while not entered.is_set(): await asyncio.sleep(.001)
                task.cancel(); release.set()
                with self.assertRaises(asyncio.CancelledError): await task
            self.assertEqual('sending', ledger.parts('race')[0]['status'])
            send.assert_not_awaited()

    async def test_gate_changed_during_claim_releases_only_unsent_claim(self):
        from plugins.random_chat.delivery_store import MemoryDeliveryLedger
        ledger = MemoryDeliveryLedger()
        enabled = True
        real_claim = ledger.claim
        def claim(key, part):
            nonlocal enabled
            result = real_claim(key, part)
            enabled = False
            return result
        send = AsyncMock()
        with patch.object(ledger, 'claim', side_effect=claim):
            await deliver_replies(('synthetic',), send=send, ledger=ledger,
                                  delivery_key='race', allowed=lambda: enabled)
        self.assertEqual('pending', ledger.parts('race')[0]['status'])
        send.assert_not_awaited()

    async def test_deadline_returns_while_noncooperative_thread_drains(self):
        from plugins.random_chat import admission
        entered, release = threading.Event(), threading.Event()
        send = AsyncMock()
        def blocking():
            entered.set(); release.wait(2)
        async def operation():
            await admission.run_chat_io(blocking)
            await send()
        try:
            task = asyncio.create_task(run_chat_turn('drain-review', operation, timeout=.04))
            while not entered.is_set(): await asyncio.sleep(.001)
            self.assertIsNone(await asyncio.wait_for(task, .15))
            self.assertEqual(1, admission._pending.get('drain-review'))
            self.assertTrue(admission._draining)
        finally:
            release.set()
            for _ in range(100):
                if 'drain-review' not in admission._pending: break
                await asyncio.sleep(.005)
        self.assertNotIn('drain-review', admission._pending)
        send.assert_not_awaited()

    async def test_io_threads_are_bounded_even_after_repeated_cancel(self):
        from plugins.random_chat import admission
        from concurrent.futures import ThreadPoolExecutor
        # A small host may default to fewer than eight threads. Give the test
        # excess capacity so it measures our gate rather than the host pool.
        asyncio.get_running_loop().set_default_executor(ThreadPoolExecutor(max_workers=12))
        entered = 0
        count_lock = threading.Lock()
        release = threading.Event()
        def blocking():
            nonlocal entered
            with count_lock: entered += 1
            release.wait(2)
        tasks = [asyncio.create_task(admission.run_chat_io(blocking)) for _ in range(12)]
        try:
            for _ in range(100):
                if entered == 8: break
                await asyncio.sleep(.005)
            self.assertEqual(8, entered)
            for task in tasks[:8]: task.cancel()
            await asyncio.sleep(.01)
            for task in tasks[:8]: task.cancel()
            await asyncio.sleep(.01)
            self.assertEqual(8, entered)
        finally:
            release.set()
            await asyncio.gather(*tasks, return_exceptions=True)

class MemoryLedgerBoundTests(unittest.TestCase):
    def test_event_count_ttl_and_empty_decisions_are_bounded(self):
        from plugins.random_chat.delivery_store import MemoryDeliveryLedger
        ledger = MemoryDeliveryLedger(max_events=2, ttl_seconds=10)
        with patch('plugins.random_chat.delivery_store.time.monotonic', return_value=1):
            for key in ('a', 'b', 'c'): ledger.complete_without_reply(key)
            self.assertEqual([], ledger.parts('a'))
            self.assertEqual('no_reply', ledger.parts('c')[0]['error'])
        with patch('plugins.random_chat.delivery_store.time.monotonic', return_value=11):
            self.assertEqual([], ledger.parts('c'))

class SearchDirectBoundaryTests(unittest.IsolatedAsyncioTestCase):
    async def test_direct_client_rejects_composite_and_encoded_secrets_without_network(self):
        import httpx
        from plugins.web_search.client import TavilySearchClient, WebSearchError
        handler = AsyncMock(return_value=httpx.Response(200, json={'results': []}))
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            search = TavilySearchClient(api_key='synthetic-test-only', client=client)
            for query in ('密码是 synthetic-passphrase', 'token synthetic-token-value',
                          'status fd00:1234::1.', 'https://user:synthetic@example.com',
                          'weather ' + 'x' * 250 + ' password=synthetic',
                          '{"password": "synthetic"}', 'password%253Dsynthetic',
                          'https://[invalid'):
                with self.subTest(query=query):
                    with self.assertRaisesRegex(WebSearchError, 'query_privacy_blocked'):
                        await search.search(query)
            handler.assert_not_awaited()
            await search.search('OAuth token exchange')
            handler.assert_awaited_once()

class PrivateVisionLiveGateTests(unittest.IsolatedAsyncioTestCase):
    setUp = integration.PrivateMemoryIntegrationTests.setUp
    tearDown = integration.PrivateMemoryIntegrationTests.tearDown
    _patch_runtime = integration.PrivateMemoryIntegrationTests._patch_runtime

    async def test_governance_clear_is_visible_to_visual_work_and_final_send(self):
        from tests.test_private_chat_vision import _private_event, _vision_result
        self.config.chat_vision_enabled = True
        self.config.chat_vision_max_bytes = 100
        self.config.chat_vision_timeout = 3
        self.config.chat_vision_model = 'synthetic'
        self.config.ai_base_url = 'https://model.invalid'
        self.config.ai_api_key = ''
        event = _private_event('synthetic', user_id=200, image_urls=('https://images.invalid/a.png',))
        event.time = int(time.time())
        observed = []
        async def understand(*args, still_allowed, **kwargs):
            self.assertTrue(still_allowed())
            self.store.clear_private_layers(user_id='200', actor='synthetic-admin',
                                            reason='synthetic regression', operation_id=1)
            for _ in range(100):
                if not still_allowed(): break
                await asyncio.sleep(.005)
            observed.append(still_allowed())
            return _vision_result(descriptions=('synthetic',))
        generate, bot = AsyncMock(), AsyncMock()
        with self._patch_runtime(), patch.object(private_matcher, 'understand_private_images', side_effect=understand), patch.object(private_matcher, 'generate_reply', generate):
            await private_matcher.handle_private_message(bot, event)
        self.assertEqual([False], observed)
        generate.assert_not_awaited(); bot.send_private_msg.assert_not_awaited()

    async def test_memory_disabled_during_vision_revokes_callback_and_send(self):
        from tests.test_private_chat_vision import _private_event, _vision_result
        for name,value in dict(chat_vision_enabled=True,chat_vision_max_bytes=100,chat_vision_timeout=3,
                               chat_vision_model='synthetic',ai_base_url='https://model.invalid',ai_api_key='').items():
            setattr(self.config,name,value)
        event = _private_event('synthetic', user_id=200, image_urls=('https://images.invalid/a.png',))
        event.time = int(time.time())
        async def understand(*args, still_allowed, **kwargs):
            self.assertTrue(still_allowed())
            self.features.set_switch('private_memory_enabled',False,'admin')
            self.assertFalse(still_allowed())
            return _vision_result(descriptions=('synthetic',))
        generate, bot = AsyncMock(), AsyncMock()
        with self._patch_runtime(), patch.object(private_matcher, 'understand_private_images', side_effect=understand), patch.object(private_matcher, 'generate_reply', generate):
            await private_matcher.handle_private_message(bot,event)
        generate.assert_not_awaited(); bot.send_private_msg.assert_not_awaited()

class GroupImageWaitTests(unittest.IsolatedAsyncioTestCase):
    async def _run_image(self, *, addressed, disable_during_wait=False):
        from types import SimpleNamespace
        from tests.test_group_router import _group_event
        from plugins.random_chat import matcher
        from plugins.chat_vision import service
        from plugins.chat_vision.store import ChatVisionStore
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            enabled = True
            features = SimpleNamespace(group_chat_allowed=lambda _: enabled, image_understanding_allowed=lambda: True)
            config = SimpleNamespace(chat_archive_path=root/'chat.db', chat_vision_root=root, chat_vision_max_bytes=100)
            store = ChatVisionStore(config.chat_archive_path)
            order = []
            async def wait(*args, **kwargs):
                nonlocal enabled
                order.append('wait')
                self.assertEqual((789,'456'),args)
                self.assertEqual(8.0, kwargs['timeout'])
                if disable_during_wait: enabled = False
                return []
            def read(*args):
                order.append('read'); return []
            bot, generate = AsyncMock(), AsyncMock()
            bot.send_group_msg.return_value = {'message_id':123}
            event = _group_event('',group_id=789,addressed=addressed,image=True)
            with patch.multiple(matcher,FEATURES=features,CONFIG=config), patch.object(service,'wait_for_message_assets',side_effect=wait), patch.object(matcher,'recent_text_context',return_value=[]), patch.object(matcher,'generate_reply',generate), patch.object(ChatVisionStore,'for_message',side_effect=read):
                first = await matcher.send_random_reply(bot,event,'',addressed=addressed)
                second = await matcher.send_random_reply(bot,event,'',addressed=addressed)
            generate.assert_not_awaited()
            self.assertEqual('wait', order[0])
            return first, second, bot.send_group_msg.await_count, order

    async def test_addressed_unready_image_waits_then_sends_one_notice(self):
        first, second, calls, order = await self._run_image(addressed=True)
        self.assertTrue(first); self.assertTrue(second)
        self.assertEqual(1,calls)
        self.assertEqual(['wait','read'],order)

    async def test_unaddressed_unready_image_stays_silent(self):
        first, second, calls, _ = await self._run_image(addressed=False)
        self.assertFalse(first); self.assertFalse(second); self.assertEqual(0,calls)

    async def test_disabling_group_during_wait_prevents_notice(self):
        first, second, calls, order = await self._run_image(addressed=True,disable_during_wait=True)
        self.assertFalse(first); self.assertFalse(second); self.assertEqual(0,calls)
        self.assertEqual(['wait'],order)
