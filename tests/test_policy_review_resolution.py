from __future__ import annotations

import unittest
from unittest.mock import patch

from plugins.violation_record import deduction_policy as policy
from tests import test_policy_reliability as reliability
from tests import test_policy_commands as command_fixtures
from plugins.violation_record import db, policy_commands, service


class PolicyReviewResolutionTests(unittest.TestCase):
    setUp=reliability.PolicyInputReliabilityTests.setUp
    tearDown=reliability.PolicyInputReliabilityTests.tearDown
    _add_record=reliability.PolicyInputReliabilityTests._add_record
    _set_baseline_adjustment=reliability.PolicyInputReliabilityTests._set_baseline_adjustment
    _business_state=reliability.PolicyInputReliabilityTests._business_state
    _policy_state=reliability.PolicyInputReliabilityTests._policy_state
    _cycle=reliability.PolicyInputReliabilityTests._cycle
    _raw_record=reliability.PolicyInputReliabilityTests._raw_record

    def _conflict(self):
        self._set_baseline_adjustment(2)
        policy.start_manual_stop(self.conn,member_id=1,group_area='蜂巢',effective_at='2026-01-01 00:00:00',
            reason='合成减停',idempotency_key='stop')
        self._add_record('2026-01-10 00:00:00')
        policy.settle_due_cycles(self.conn,'2026-01-31 00:00:00')
        policy.clear_manual_stop(self.conn,member_id=1,group_area='蜂巢',effective_at='2026-02-02 00:00:00',
            reason='合成清除',idempotency_key='clear')
        source=self._raw_record(1,'蜂巢','2026-01-11 00:00:00')
        self.conn.execute("UPDATE violation_records SET action='禁言2小时' WHERE id=?",(source,))
        policy.process_violation_record(self.conn,source,ingest_time='2026-02-03 00:00:00')
        pending=self.conn.execute("SELECT id FROM v102_pending_actions WHERE action_type='replay_review' AND status='pending'").fetchone()[0]
        return source,pending

    def _resolve(self,pending,mode='重新计时',fingerprint=None):
        return policy.resolve_policy_review(self.conn,member_id=1,group_area='蜂巢',pending_action_id=pending,
            recovery_mode=mode,expected_fingerprint=fingerprint or policy.policy_review_fingerprint(self.conn,1,'蜂巢'),
            effective_at='2026-02-03 00:00:00',reason='合法补录并保留历史决定，已复核',
            actor_qq='90001',idempotency_key=f'review:{pending}')

    def test_accepted_backfill_and_manual_clear_survive_resolution_and_later_replay(self):
        source,pending=self._conflict()
        deducted=self._business_state()['deduct_count']
        result=self._resolve(pending)
        self.assertEqual(self._business_state()['deduct_count'],deducted)
        self.assertEqual(self.conn.execute('SELECT is_withdrawn FROM violation_records WHERE id=?',(source,)).fetchone()[0],0)
        self.assertFalse(policy.policy_scope_under_review(self.conn,1,'蜂巢'))
        self.assertEqual('2026-02-03 00:00:00',self._cycle()['start_at'])
        # A subsequent late input inside the new cycle may replay, without revisiting the accepted conflict.
        self._add_record('2026-02-05 00:00:00')
        newer=self._raw_record(1,'蜂巢','2026-02-04 00:00:00')
        policy.process_violation_record(self.conn,newer,ingest_time='2026-02-06 00:00:00')
        self.assertFalse(policy.policy_scope_under_review(self.conn,1,'蜂巢'))
        self.assertEqual(deducted,self._business_state()['deduct_count'])
        self.assertEqual(6,self._business_state()['total_count'])
        self.assertEqual(1,self.conn.execute("SELECT COUNT(*) FROM v102_policy_events WHERE event_type='policy_review_resolved' AND is_effective=1").fetchone()[0])

    def test_preserve_mode_does_not_reset_active_period(self):
        _,pending=self._conflict();start=self._cycle()['start_at'];due=self._cycle()['due_at']
        self._resolve(pending,'保留周期')
        self.assertEqual(start,self._cycle()['start_at'])
        self.assertEqual(due,self._cycle()['due_at'])

    def test_changed_evidence_rejects_old_confirmation_without_unpausing(self):
        _,pending=self._conflict();fingerprint=policy.policy_review_fingerprint(self.conn,1,'蜂巢')
        self._raw_record(1,'蜂巢','2026-02-03 00:00:00')
        with self.assertRaisesRegex(ValueError,'变化'):
            self._resolve(pending,fingerprint=fingerprint)
        self.assertTrue(policy.policy_scope_under_review(self.conn,1,'蜂巢'))

    def test_new_historical_change_after_acceptance_requires_new_review(self):
        _,pending=self._conflict();self._resolve(pending)
        late=self._raw_record(1,'蜂巢','2026-01-12 00:00:00')
        policy.process_violation_record(self.conn,late,ingest_time='2026-02-04 00:00:00')
        self.assertTrue(policy.policy_scope_under_review(self.conn,1,'蜂巢'))
        self.assertEqual(1,self._business_state()['deduct_count'])

    def test_accepted_stop_decision_pending_survives_later_replay(self):
        source=self._add_record('2026-01-01 00:00:00')
        source_event=self.conn.execute("SELECT id FROM v102_policy_events WHERE source_record_id=? AND event_type='mute_recorded'",(source,)).fetchone()[0]
        policy.start_manual_stop(self.conn,member_id=1,group_area='蜂巢',effective_at='2026-01-02 00:00:00',
            reason='合成人工决定',idempotency_key='linked-stop',caused_by_event_id=source_event)
        policy.withdraw_violation_record(self.conn,source,effective_at='2026-01-03 00:00:00',reason='合成证据撤回')
        pending=self.conn.execute("SELECT id FROM v102_pending_actions WHERE action_type='replay_review' AND status='pending'").fetchone()[0]
        outcome=self._resolve(pending,'保留周期')
        self.assertEqual('pending_decision',self._cycle()['status'])
        policy.replay_member_group(self.conn,1,'蜂巢',trigger_event_id=outcome.event_id,as_of='2026-02-04 00:00:00')
        self.assertEqual('pending_decision',self._cycle()['status'])
        self.assertEqual(1,self.conn.execute("SELECT COUNT(*) FROM v102_pending_actions WHERE action_type='stop_decision' AND status='pending'").fetchone()[0])

    def test_unprocessed_records_entered_during_isolation_are_accepted_once(self):
        _,pending=self._conflict()
        later=self._raw_record(1,'蜂巢','2026-02-03 00:00:00')
        self._resolve(pending)
        before=self._business_state()['total_count']
        policy.process_violation_record(self.conn,later,ingest_time='2026-02-04 00:00:00')
        self.assertEqual(before,self._business_state()['total_count'])
        self.assertFalse(policy.policy_scope_under_review(self.conn,1,'蜂巢'))
        self.assertEqual(1,self.conn.execute("SELECT COUNT(*) FROM v102_policy_events WHERE source_record_id=? AND event_type='mute_recorded'",(later,)).fetchone()[0])

    def test_preserve_mode_cannot_silently_ignore_new_evidence_in_current_cycle(self):
        _,pending=self._conflict()
        self._raw_record(1,'蜂巢','2026-02-03 00:00:00')
        with self.assertRaisesRegex(ValueError,'旧评价'):
            self._resolve(pending,'保留周期')
        self.assertTrue(policy.policy_scope_under_review(self.conn,1,'蜂巢'))
        self._resolve(pending,'重新计时')
        self.assertFalse(policy.policy_scope_under_review(self.conn,1,'蜂巢'))

    def test_wrong_pending_id_or_missing_mode_cannot_resolve_scope(self):
        _,pending=self._conflict()
        with self.assertRaises(ValueError):self._resolve(pending+100)
        with self.assertRaises(ValueError):self._resolve(pending,'')
        self.assertTrue(policy.policy_scope_under_review(self.conn,1,'蜂巢'))


class PolicyReviewCommandTests(unittest.TestCase):
    tearDown=command_fixtures.PolicyCommandServiceTests.tearDown

    def setUp(self):
        command_fixtures.PolicyCommandServiceTests.setUp(self)
        self.allow=True
        gate=patch.object(policy_commands,'_review_operator_allowed',side_effect=lambda qq:self.allow and qq=='90001')
        gate.start();self.addCleanup(gate.stop)
        clock=patch.object(policy_commands,'now_str',return_value='2026-08-03 12:00:00')
        clock.start();self.addCleanup(clock.stop)
        with db.connect() as conn:
            self.member_id=conn.execute("SELECT id FROM members WHERE qq_number='123456'").fetchone()[0]
            outcome=policy.record_policy_review(conn,member_id=self.member_id,group_area='蜂巢',source_record_id=None,
                key='synthetic-conflict',at='2026-08-02 12:00:00',reason='合成人工冲突',action_type='replay_review')
            self.pending_id=outcome.pending_action_id

    def _preview(self,mode='重新计时',actor='90001'):
        return policy_commands.handle_policy_text(f'复核减数冲突 蜂巢 123456 {self.pending_id} {mode} 逐条核实后保留历史决定',
            group_id='123456789',operator_qq=actor,operator_nickname='合成管理员',message_id='review-preview')

    def test_confirmation_resolves_only_selected_member_and_writes_audit(self):
        reply=self._preview()
        self.assertIn('请回复“确认”',reply)
        self.assertIn(f'复核待办：#{self.pending_id}',reply)
        with db.connect() as conn:
            self.assertTrue(policy.policy_scope_under_review(conn,self.member_id,'蜂巢'))
        result=service.confirm_pending('123456789','90001','合成管理员','review-confirm')
        self.assertIn('已复核减数冲突',result)
        with db.connect() as conn:
            self.assertFalse(policy.policy_scope_under_review(conn,self.member_id,'蜂巢'))
            self.assertEqual(1,conn.execute("SELECT COUNT(*) FROM operation_logs WHERE operation_type='复核减数冲突'").fetchone()[0])
            row=conn.execute("SELECT * FROM v102_policy_events WHERE event_type='policy_review_resolved'").fetchone()
            self.assertIn('90001',row['payload_json'])
            self.assertEqual('2026-08-03 12:00:00',conn.execute("SELECT start_at FROM v102_policy_cycles WHERE status='active'").fetchone()[0])

    def test_new_record_after_preview_keeps_review_blocked(self):
        self._preview()
        with db.connect() as conn:
            conn.execute("""INSERT INTO violation_records(member_id,group_area,violation_time,judgement,action,created_at,updated_at)
                VALUES(?,'蜂巢','2026-08-03 11:00:00','合成','禁言10分钟','2026-08-03 11:00:00','2026-08-03 11:00:00')""",(self.member_id,))
        result=service.confirm_pending('123456789','90001','合成管理员','review-confirm')
        self.assertIn('状态已变化',result)
        with db.connect() as conn:self.assertTrue(policy.policy_scope_under_review(conn,self.member_id,'蜂巢'))

    def test_permission_is_rechecked_at_confirmation(self):
        self._preview();self.allow=False
        result=service.confirm_pending('123456789','90001','合成管理员','review-confirm')
        self.assertIn('权限已变化',result)
        with db.connect() as conn:self.assertTrue(policy.policy_scope_under_review(conn,self.member_id,'蜂巢'))

    def test_nonprivileged_actor_cannot_create_review_confirmation(self):
        result=self._preview(actor='80001')
        self.assertIn('仅配置的机器人管理员',result)
        with db.connect() as conn:
            self.assertEqual(0,conn.execute("SELECT COUNT(*) FROM pending_operations").fetchone()[0])

    def test_recovery_mode_is_required_and_cancel_keeps_scope_paused(self):
        with self.assertRaises(policy_commands.PolicyCommandError):
            policy_commands.parse_policy_command(f'复核减数冲突 蜂巢 123456 {self.pending_id} 已核实')
        self._preview('保留周期')
        service.cancel_pending('123456789','90001')
        with db.connect() as conn:
            self.assertTrue(policy.policy_scope_under_review(conn,self.member_id,'蜂巢'))
            self.assertEqual(1,conn.execute("SELECT COUNT(*) FROM operation_logs WHERE operation_type='复核减数冲突取消'").fetchone()[0])
