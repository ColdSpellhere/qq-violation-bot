from __future__ import annotations

import asyncio
import sqlite3
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from tests.test_hive_keyword_alert import (
    BOT_USER_ID,
    REPORT_GROUP_ID,
    SOURCE_GROUP_ID,
    _group_event,
)
from tests import test_hive_keyword_alert as fixtures
from plugins.content_alert.rules import KeywordRuleStore
from plugins.content_alert.service import ContentAlertService


class DurableAlertRegressionTests(unittest.IsolatedAsyncioTestCase):
    def service(self, root: Path, **extra):
        rules = KeywordRuleStore(root / "keywords.json")
        if not rules.snapshot():
            rules.add("合成告警词", actor="synthetic")
        return ContentAlertService(
            rule_store=rules,
            source_group_labels={SOURCE_GROUP_ID: "合成来源"},
            report_group_id=REPORT_GROUP_ID,
            peer_bot_user_ids=(),
            runtime_enabled=lambda: True,
            clock=lambda: 2000,
            **extra,
        )

    async def test_successful_delivery_is_deduplicated_after_service_restart(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            bot = fixtures.ContentAlertServiceTests.Bot()
            event = _group_event("合成告警词")
            await self.service(root).handle_event(bot, event)
            await self.service(root).handle_event(bot, event)
            self.assertEqual(1, len(bot.calls), "restart must retain event deduplication")

    async def test_network_failure_persists_unknown_without_raw_message(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            service = self.service(root)
            bot = fixtures.ContentAlertServiceTests.Bot(failures=1)
            # Only the already-redacted bounded report may be persisted.
            event = _group_event("合成告警词" + "正文" * 200 + "tail-never-persist")
            try:
                await service.handle_event(bot, event)
            except OSError:
                pass
            path = root / "outbox.sqlite3"
            self.assertTrue(path.exists(), "delivery intent must survive process failure")
            with sqlite3.connect(path) as connection:
                row = connection.execute(
                    "SELECT status,report_text FROM content_alert_outbox"
                ).fetchone()
            self.assertEqual("delivery_unknown", row[0])
            self.assertNotIn("tail-never-persist", row[1])

    async def test_manual_and_legacy_oversize_inputs_emit_hidden_protection_alert(self):
        for legacy in (False, True):
            with self.subTest(legacy=legacy), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary).resolve()
                extra = {}
                if legacy:
                    background = KeywordRuleStore(root / "background.json")
                    background.add("合成背景词", actor="synthetic")
                    extra["background_rule_store"] = background
                bot = fixtures.ContentAlertServiceTests.Bot()
                await self.service(root, **extra).handle_event(
                    bot, _group_event("合成背景词" + "合成告警词" * 5000)
                )
                text = str(bot.calls[0]["message"])
                self.assertIn("扫描保护告警", text)
                self.assertNotIn("合成背景词", text)
                self.assertNotIn("合成告警词", text)

    async def test_manual_literal_work_runs_outside_event_loop(self):
        from plugins.content_alert.engine import LiteralKeywordMatcher

        original = LiteralKeywordMatcher.match_text
        caller_thread = threading.get_ident()
        scan_threads = []

        def observed(matcher, text, *args, **kwargs):
            scan_threads.append(threading.get_ident())
            return original(matcher, text, *args, **kwargs)

        with tempfile.TemporaryDirectory() as temporary:
            service = self.service(Path(temporary).resolve())
            with patch.object(LiteralKeywordMatcher, "match_text", observed):
                await service.handle_event(fixtures.ContentAlertServiceTests.Bot(), _group_event("合成告警词"))
        self.assertTrue(scan_threads)
        self.assertNotIn(caller_thread, scan_threads)

    async def test_outbox_persists_generation_and_redacted_report_without_hidden_terms(self):
        from types import SimpleNamespace

        catalog = fixtures.ContentAlertServiceTests.ManagedCatalog((SimpleNamespace(
            term='合成保密词', category_ids=('restricted_internal',),
            category_names=('合成隐藏分类',), disclosure_policy='strict_hidden',
        ),))
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            service = self.service(root, managed_catalog=catalog)
            bot = fixtures.ContentAlertServiceTests.Bot()
            await service.handle_event(bot, _group_event('合成告警词合成保密词', nickname='合成保密词'))
            with sqlite3.connect(service.outbox.path) as connection:
                row = connection.execute('SELECT rule_generation, report_text FROM content_alert_outbox').fetchone()
                columns = {item[1] for item in connection.execute('PRAGMA table_info(content_alert_outbox)')}
            self.assertIn('managed:synthetic-generation;', row[0])
            self.assertNotIn('合成保密词', row[1])
            self.assertNotIn('合成告警词', row[1])
            self.assertNotIn('合成隐藏分类', row[1])
            self.assertNotIn('raw_message', columns)
            self.assertEqual(str(bot.calls[0]['message']), row[1])


class AlertOutboxBehaviorTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()
        self.now = 2000.0
        self.enabled = True
        self.rules = KeywordRuleStore(self.root / 'keywords.json')
        self.rules.add('合成告警词', actor='synthetic')
        self.service = ContentAlertService(
            rule_store=self.rules, source_group_labels={SOURCE_GROUP_ID: '合成来源'},
            report_group_id=REPORT_GROUP_ID, peer_bot_user_ids=(),
            runtime_enabled=lambda: self.enabled, clock=lambda: self.now,
        )

    def bot(self, failures=0):
        bot = fixtures.ContentAlertServiceTests.Bot(failures=failures)
        bot.self_id = str(BOT_USER_ID)
        return bot

    def seed(self, message_id='synthetic'):
        return self.service.outbox.enqueue(
            self_id=str(BOT_USER_ID), source_group_id=SOURCE_GROUP_ID,
            source_message_id=message_id, report_group_id=REPORT_GROUP_ID,
            rule_generation='managed:synthetic-generation', report_text='（合成已脱敏报告）', now=self.now,
        )[0]

    def row(self):
        with sqlite3.connect(self.service.outbox.path) as connection:
            connection.row_factory = sqlite3.Row
            return dict(connection.execute('SELECT * FROM content_alert_outbox').fetchone())

    async def test_definite_rejection_has_bounded_backoff_and_runtime_worker_retry(self):
        from nonebot.adapters.onebot.v11.exception import ActionFailed
        from plugins.content_alert.lifecycle import AlertDeliveryWorker

        class RejectedBot:
            self_id = str(BOT_USER_ID)
            calls = 0

            async def send_group_msg(bot, **_kwargs):
                bot.calls += 1
                raise ActionFailed(status='failed', retcode=100)

        self.seed()
        bot = RejectedBot()
        worker = AlertDeliveryWorker(self.service, lambda: {bot.self_id: bot})
        for attempt in range(5):
            await worker.tick()
            self.assertEqual(attempt + 1, bot.calls)
            await worker.tick()
            self.assertEqual(attempt + 1, bot.calls, 'same tick must not busy retry')
            self.now += 400
        self.assertEqual('exhausted', self.row()['status'])
        await worker.tick()
        self.assertEqual(5, bot.calls)

    async def test_unknown_never_automatically_retries_but_confirmed_operator_retry_is_audited(self):
        from plugins.content_alert.delivery_commands import execute_delivery_command

        bot = self.bot(failures=1)
        await self.service.handle_event(bot, _group_event('合成告警词'))
        row = self.row()
        self.now += 10000
        await self.service.deliver_pending(bot)
        self.assertEqual(1, len(bot.calls))
        response = await execute_delivery_command(
            f"/违禁词 告警重试 {row['alert_id']}", self.service, actor='synthetic-admin')
        self.assertIn('可能产生重复', response)
        self.assertEqual('delivery_unknown', self.row()['status'])
        await execute_delivery_command(f"/违禁词 告警重试 {row['alert_id']} 确认", self.service, actor='synthetic-admin')
        await self.service.deliver_pending(bot)
        self.assertEqual('delivered', self.row()['status'])
        self.assertEqual(2, len(bot.calls))
        with sqlite3.connect(self.service.outbox.path) as connection:
            self.assertEqual(1, connection.execute('SELECT COUNT(*) FROM content_alert_operator_actions').fetchone()[0])

    async def test_lease_recovery_distinguishes_unstarted_and_interrupted_send(self):
        from plugins.content_alert.outbox import LEASE_SECONDS

        key = self.seed()
        outbox = self.service.outbox
        claim = outbox.claim(now=self.now, self_id=str(BOT_USER_ID), event_key=key)
        self.now += LEASE_SECONDS + 1
        outbox.recover(self.now)
        self.assertEqual('pending', self.row()['status'])
        claim = outbox.claim(now=self.now, self_id=str(BOT_USER_ID), event_key=key)
        self.assertTrue(outbox.begin_send(claim, now=self.now))
        self.now += LEASE_SECONDS + 1
        outbox.recover(self.now)
        self.assertEqual('delivery_unknown', self.row()['status'])
        bot = self.bot()
        await self.service.deliver_pending(bot)
        self.assertEqual([], bot.calls)
        self.assertFalse(outbox.finish(claim, outcome='delivered', now=self.now))

    async def test_gate_is_rechecked_after_claim_and_before_api(self):
        self.seed()
        bot = self.bot()
        original = self.service.outbox.begin_send

        def disable_after_begin(row, *, now):
            result = original(row, now=now)
            self.enabled = False
            return result

        with patch.object(self.service.outbox, 'begin_send', disable_after_begin):
            await self.service.deliver_pending(bot)
        self.assertEqual([], bot.calls)
        self.assertEqual('pending', self.row()['status'])
        self.now += 5000
        self.enabled = True
        await self.service.deliver_pending(bot)
        self.assertEqual(1, len(bot.calls), 'persisted rows are not dropped by the new-event 300s filter')

    async def test_offline_wrong_identity_and_shutdown_preserve_pending(self):
        from plugins.content_alert.lifecycle import AlertDeliveryWorker

        self.seed()
        worker = AlertDeliveryWorker(self.service, lambda: {})
        await worker.tick()
        self.assertEqual('pending', self.row()['status'])
        bot = self.bot()
        bot.self_id = 'wrong-identity'
        await self.service.deliver_pending(bot)
        self.assertEqual([], bot.calls)
        await worker.stop()
        bot.self_id = str(BOT_USER_ID)
        await self.service.deliver_pending(bot)
        self.assertEqual([], bot.calls)

    async def test_cancellation_during_send_is_persisted_unknown(self):
        started = asyncio.Event()

        class SlowBot:
            self_id = str(BOT_USER_ID)

            async def send_group_msg(bot, **_kwargs):
                started.set()
                await asyncio.Event().wait()

        self.seed()
        task = asyncio.create_task(self.service.deliver_pending(SlowBot()))
        await asyncio.wait_for(started.wait(), 2)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertEqual('delivery_unknown', self.row()['status'])

    async def test_success_without_message_receipt_is_unknown_and_same_event_has_same_alert_id(self):
        self.seed()
        class MissingReceipt:
            self_id = str(BOT_USER_ID)
            async def send_group_msg(self, **_kwargs):
                return None
        await self.service.deliver_pending(MissingReceipt())
        self.assertEqual('delivery_unknown', self.row()['status'])
        first = self.row()['alert_id']
        self.seed()
        self.assertEqual(first, self.row()['alert_id'])

    async def test_invalid_receipts_do_not_claim_delivery(self):
        for number, response in enumerate(({}, {'message_id': ''}, {'message_id': True}, {'message_id': {}})):
            with self.subTest(response=response):
                key = self.seed(f'invalid-{number}')
                class InvalidReceipt:
                    self_id = str(BOT_USER_ID)
                    async def send_group_msg(self, **_kwargs):
                        return response
                await self.service.deliver_pending(InvalidReceipt(), event_key=key)
                with sqlite3.connect(self.service.outbox.path) as connection:
                    row = connection.execute('SELECT status FROM content_alert_outbox WHERE event_key=?', (key,)).fetchone()
                self.assertEqual('delivery_unknown', row[0])

    async def test_success_then_receipt_commit_failure_is_not_blindly_replayed(self):
        from plugins.content_alert.outbox import LEASE_SECONDS

        self.seed()
        bot = self.bot()
        with patch.object(self.service.outbox, 'finish', side_effect=sqlite3.OperationalError('synthetic failure')):
            with self.assertRaises(sqlite3.OperationalError):
                await self.service.deliver_pending(bot)
        self.assertEqual(1, len(bot.calls))
        self.assertEqual('sending', self.row()['status'])
        self.now += LEASE_SECONDS + 1
        await self.service.deliver_pending(bot)
        self.assertEqual('delivery_unknown', self.row()['status'])
        self.assertEqual(1, len(bot.calls))

    async def test_gate_closing_when_send_task_starts_prevents_api_invocation(self):
        self.seed()
        bot = self.bot()
        original = self.service._send_guarded

        async def turn_off_before_dispatch(bot, row):
            self.enabled = False
            return await original(bot, row)

        with patch.object(self.service, '_send_guarded', turn_off_before_dispatch):
            await self.service.deliver_pending(bot)
        self.assertEqual([], bot.calls)
        self.assertEqual('pending', self.row()['status'])

    async def test_runtime_worker_stop_persists_interrupted_send_as_unknown(self):
        from plugins.content_alert.lifecycle import AlertDeliveryWorker

        started = asyncio.Event()

        class SlowBot:
            self_id = str(BOT_USER_ID)
            async def send_group_msg(bot, **_kwargs):
                started.set()
                await asyncio.Event().wait()

        bot = SlowBot()
        self.seed()
        worker = AlertDeliveryWorker(self.service, lambda: {bot.self_id: bot}, interval=0.05)
        await worker.start()
        try:
            await asyncio.wait_for(started.wait(), 2)
        finally:
            await worker.stop()
        self.assertEqual('delivery_unknown', self.row()['status'])
        self.assertFalse(self.service._accepting)

    async def test_operator_resolution_does_not_disclose_report_or_bypass_feature_gate(self):
        from plugins.content_alert.delivery_commands import execute_delivery_command

        key = self.seed()
        row = self.service.outbox.claim(now=self.now, self_id=str(BOT_USER_ID), event_key=key)
        self.service.outbox.begin_send(row, now=self.now)
        self.service.outbox.finish(row, outcome='delivery_unknown', now=self.now)
        alert_id = self.row()['alert_id']
        response = await execute_delivery_command(f'/违禁词 告警状态 {alert_id}', self.service, actor='synthetic')
        self.assertNotIn('合成已脱敏报告', response)
        self.assertNotIn('synthetic-generation', response)
        summary = await execute_delivery_command('/违禁词 告警状态', self.service, actor='synthetic')
        self.assertIn(alert_id, summary, 'operators must be able to find an unreceived unknown alert')
        self.assertNotIn('合成已脱敏报告', summary)
        self.enabled = False
        await execute_delivery_command(f'/违禁词 告警重试 {alert_id} 确认', self.service, actor='synthetic')
        self.assertEqual('delivery_unknown', self.row()['status'])
        await execute_delivery_command(f'/违禁词 告警已收 {alert_id} 确认', self.service, actor='synthetic')
        self.assertEqual('delivered', self.row()['status'])

    async def test_non_superuser_cannot_query_or_resolve_delivery_state(self):
        from plugins.content_alert import matcher
        from unittest.mock import AsyncMock
        from types import SimpleNamespace

        event = fixtures._private_event('/违禁词 告警状态')
        with patch.object(matcher, 'get_driver', return_value=SimpleNamespace(config=SimpleNamespace(superusers=set()))), \
             patch.object(matcher, 'execute_delivery_command', new=AsyncMock()) as execute, \
             patch.object(matcher.keyword_command_matcher, 'finish', new=AsyncMock()):
            await matcher.handle_keyword_command(event)
        execute.assert_not_awaited()

    def test_pending_capacity_fails_explicitly_and_does_not_overwrite_existing_intent(self):
        self.seed('first')
        with patch('plugins.content_alert.outbox.MAX_PENDING_ALERTS', 1):
            with self.assertRaisesRegex(RuntimeError, 'capacity'):
                self.seed('second')
            self.seed('first')
        self.assertEqual([{'status': 'pending', 'count': 1}], self.service.outbox.states())

    async def test_competing_services_cannot_both_claim_an_alert(self):
        from plugins.content_alert.outbox import AlertOutbox

        self.seed()
        other = AlertOutbox(self.service.outbox.path)
        claims = await asyncio.gather(*(
            asyncio.to_thread(outbox.claim, now=self.now, self_id=str(BOT_USER_ID))
            for outbox in (self.service.outbox, other)
        ))
        self.assertEqual(1, sum(row is not None for row in claims))

    def test_outbox_rejects_symlink_and_hardlink_and_creates_private_file(self):
        from plugins.content_alert.outbox import AlertOutbox

        self.seed()
        self.assertEqual(0o600, self.service.outbox.path.stat().st_mode & 0o777)
        link = self.root / 'link.sqlite3'
        link.symlink_to(self.service.outbox.path)
        with self.assertRaises(ValueError):
            AlertOutbox(link).states()
        import os
        hardlink = self.root / 'hardlink.sqlite3'
        os.link(self.service.outbox.path, hardlink)
        with self.assertRaises(ValueError):
            AlertOutbox(hardlink).states()


if __name__ == "__main__":
    unittest.main()
