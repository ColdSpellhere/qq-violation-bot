"""Bounded logical generation; external HTTP retries remain the gateway's policy."""
from __future__ import annotations

import asyncio
import json
import sqlite3
import unittest
from unittest.mock import AsyncMock, patch

from plugins.member_memory import ai, summary
from tests import test_member_summary_diagnostics as diagnostics


class MemberSummaryCorrectionTests(unittest.IsolatedAsyncioTestCase):
    setUp = diagnostics.MemberSummaryDiagnosticsTests.setUp
    profile = diagnostics.MemberSummaryDiagnosticsTests.profile
    refresh = diagnostics.MemberSummaryDiagnosticsTests.refresh

    def assert_unchanged(self):
        self.assertEqual('旧' * 278, self.profile().summary)
        self.assertEqual(0, self.profile().summary_through_fact_id)

    async def test_valid_first_response_needs_one_logical_generation(self):
        with patch.object(ai, '_complete', AsyncMock(return_value='合' * 300)) as complete:
            self.assertTrue(await self.refresh())
        complete.assert_awaited_once()
        self.assertEqual('合' * 300, self.profile().summary)

    async def test_correction_reuses_original_inputs_without_rejected_output(self):
        rejected = '仅用于检测被拒正文泄漏' + '长' * 301
        with patch.object(ai, '_complete', AsyncMock(side_effect=[rejected, '合' * 200])) as complete:
            self.assertTrue(await self.refresh())
        self.assertEqual(2, complete.await_count)
        first, second = complete.await_args_list
        self.assertEqual(first.args[1][1], second.args[1][1])
        self.assertEqual(first.kwargs, second.kwargs)
        self.assertEqual(['system', 'user'], [item['role'] for item in second.args[1]])
        self.assertNotIn(rejected, json.dumps(second.args[1], ensure_ascii=False))
        self.assertIn('180～220', first.args[1][0]['content'])
        self.assertIn('标点', first.args[1][0]['content'])
        self.assertIn('空白', first.args[1][0]['content'])
        self.assertEqual('合' * 200, self.profile().summary)
        self.assertEqual(20, self.profile().summary_through_fact_id)
        mirror = (self.root / '123' / '7.json').read_text(encoding='utf-8')
        self.assertNotIn(rejected, mirror)
        self.assertEqual('合' * 200, json.loads(mirror)['summary'])

    async def test_twice_too_long_fails_without_truncation_or_cursor_advance(self):
        with patch.object(ai, '_complete', AsyncMock(return_value='长' * 301)) as complete:
            with self.assertRaises(ai.MemberSummaryError) as raised:
                await self.refresh()
        self.assertEqual(2, complete.await_count)
        self.assertEqual('member_summary_too_long', raised.exception.code)
        self.assert_unchanged()

    async def test_secret_empty_and_invalid_outputs_do_not_trigger_correction(self):
        for output, code in (
            ('synthetic token value', 'secret_blocked'),
            ('synthetic token value' + '长' * 301, 'secret_blocked'),
            (' \n ', 'empty_response'),
            (None, 'invalid_response'),
        ):
            with self.subTest(code=code, long=isinstance(output, str) and len(output) > 300):
                with patch.object(ai, '_complete', AsyncMock(return_value=output)) as complete:
                    with self.assertRaises(ai.MemberSummaryError) as raised:
                        await self.refresh()
                self.assertEqual('member_summary_' + code, raised.exception.code)
                complete.assert_awaited_once()
                self.assert_unchanged()

    async def test_secret_correction_is_rejected_without_a_third_generation(self):
        with patch.object(ai, '_complete', AsyncMock(side_effect=['长' * 301, 'synthetic token value'])) as complete:
            with self.assertRaises(ai.MemberSummaryError) as raised:
                await self.refresh()
        self.assertEqual('member_summary_secret_blocked', raised.exception.code)
        self.assertEqual(2, complete.await_count)
        self.assert_unchanged()

    async def test_cancel_at_either_generation_propagates_without_saving(self):
        for outputs in ([asyncio.CancelledError()], ['长' * 301, asyncio.CancelledError()]):
            with self.subTest(generation=len(outputs)):
                with patch.object(ai, '_complete', AsyncMock(side_effect=outputs)) as complete:
                    with self.assertRaises(asyncio.CancelledError):
                        await self.refresh()
                self.assertEqual(len(outputs), complete.await_count)
                self.assert_unchanged()

    async def test_pending_cancellation_is_checked_before_correction(self):
        async def complete(*args, **kwargs):
            asyncio.current_task().cancel()
            return '长' * 301

        # Use a child task so its cancellation cannot leak into the test runner.
        with patch.object(ai, '_complete', AsyncMock(side_effect=complete)) as generate:
            task = asyncio.create_task(self.refresh())
            with self.assertRaises(asyncio.CancelledError):
                await task
        generate.assert_awaited_once()
        self.assert_unchanged()

    async def test_internal_whitespace_and_punctuation_count_toward_hard_limit(self):
        exact = '合' + '. \n' * 99 + '尾声'
        self.assertEqual(300, len(exact))
        with patch.object(ai, '_complete', AsyncMock(side_effect=[exact + '。', exact])) as complete:
            self.assertTrue(await self.refresh())
        self.assertEqual(2, complete.await_count)
        self.assertEqual(exact, self.profile().summary)

    async def test_revocation_after_overlong_output_prevents_correction(self):
        live = True

        async def complete(*args, **kwargs):
            nonlocal live
            live = False
            return '长' * 301

        with patch.object(ai, '_complete', AsyncMock(side_effect=complete)) as generate:
            self.assertFalse(await summary.refresh_member_summary(
                self.path, self.root, group_id=123, user_id='7', strict=True,
                max_batches=1, allowed=lambda: live,
            ))
        generate.assert_awaited_once()
        self.assert_unchanged()

    async def test_revocation_during_correction_prevents_commit(self):
        calls = 0

        async def complete(*args, **kwargs):
            nonlocal calls
            calls += 1
            return '长' * 301 if calls == 1 else '合格摘要'

        with patch.object(ai, '_complete', side_effect=complete):
            self.assertFalse(await summary.refresh_member_summary(
                self.path, self.root, group_id=123, user_id='7', strict=True,
                max_batches=1, allowed=lambda: calls < 2,
            ))
        self.assertEqual(2, calls)
        self.assert_unchanged()

    async def test_valid_correction_still_checks_original_fact_versions(self):
        calls = 0

        async def complete(*args, **kwargs):
            nonlocal calls
            calls += 1
            if calls == 1:
                return '长' * 301
            with sqlite3.connect(self.path) as db:
                db.execute('UPDATE member_memory_facts SET version=version+1 WHERE id=1')
            return '纠正后合格摘要'

        with patch.object(ai, '_complete', side_effect=complete):
            with self.assertRaises(ai.MemberSummaryError) as raised:
                await self.refresh()
        self.assertEqual(2, calls)
        self.assertEqual('member_summary_fact_conflict', raised.exception.code)
        self.assert_unchanged()
