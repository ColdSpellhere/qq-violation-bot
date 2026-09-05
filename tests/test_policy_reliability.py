from __future__ import annotations

from datetime import datetime, timedelta
import asyncio
import unittest
from unittest.mock import patch

from plugins.violation_record.deduction_policy import (
    Severity,
    ensure_policy_scope_snapshot,
    classify_severity,
    parse_mute_seconds,
    process_status_change,
    process_violation_record,
    record_policy_review,
    withdraw_violation_record,
)
from tests.test_deduction_policy import PolicyTimelineTests
from tests.test_policy_scheduler import PolicySchedulerTests, FakeBot
from plugins.violation_record import db, scheduler, policy_bridge
from plugins.violation_record.deduction_policy import _insert_event


class PolicyInputReliabilityTests(PolicyTimelineTests):
    def _other_member(self) -> None:
        self.conn.execute("INSERT INTO members(id,qq_number) VALUES(2,'10002')")
        self.conn.execute(
            """INSERT INTO member_group_states(
                id,member_id,group_area,created_at,updated_at
            ) VALUES(2,2,'蜂窝','2026-09-01 12:00:00','2026-09-01 12:00:00')"""
        )

    def _raw_record(self, member_id: int, area: str, when: str) -> int:
        ensure_policy_scope_snapshot(self.conn,member_id,area,"2026-09-01 00:00:00")
        return int(self.conn.execute(
            """INSERT INTO violation_records(
                member_id,group_area,violation_time,action,created_at,updated_at
            ) VALUES(?,?,?,'禁言10分钟',?,?)""",
            (member_id,area,when,when,when),
        ).lastrowid)

    def test_future_record_is_rejected_without_advancing_other_scope(self) -> None:
        self._add_record("2026-09-01 12:00:00")
        self._other_member()
        record_id = self._raw_record(2,"蜂窝","2026-12-01 12:00:00")
        with self.assertRaisesRegex(ValueError, "未来"):
            process_violation_record(self.conn,record_id,ingest_time="2026-09-05 12:00:00")
        self.assertEqual(self._business_state()["deduct_count"],0)
        self.assertEqual(self.conn.execute(
            "SELECT COUNT(*) FROM v102_policy_events WHERE source_record_id=?",(record_id,)
        ).fetchone()[0],0)

    def test_future_status_is_rejected_without_partial_event(self) -> None:
        self._add_record("2026-09-01 12:00:00")
        with self.assertRaisesRegex(ValueError, "未来"):
            process_status_change(self.conn,member_id=1,group_area="蜂巢",
                status="已质询",effective_at="2026-12-01 12:00:00",
                ingest_time="2026-09-05 12:00:00",idempotency_key="future-status")
        self.assertEqual(self._business_state()["status"],"正常")
        self.assertEqual(self._business_state()["deduct_count"],0)

    def test_record_processing_only_settles_its_own_member_scope(self) -> None:
        self._add_record("2026-09-01 12:00:00")
        self._other_member()
        record_id=self._raw_record(2,"蜂窝","2026-09-20 12:00:00")
        process_violation_record(self.conn,record_id,ingest_time="2026-09-20 12:00:00")
        self.assertEqual(self._business_state()["deduct_count"],0)
        self.assertEqual(self._cycle()["status"],"active")

    def test_chronological_delayed_records_do_not_replay_all_history(self) -> None:
        for index in range(8):
            effective=datetime(2026,9,1,12)+timedelta(minutes=index)
            when=effective.strftime("%Y-%m-%d %H:%M:%S")
            record_id=self._raw_record(1,"蜂巢",when)
            process_violation_record(self.conn,record_id,
                ingest_time=(effective+timedelta(seconds=30)).strftime("%Y-%m-%d %H:%M:%S"))
        generations=self.conn.execute("SELECT MAX(replay_generation) FROM v102_policy_events").fetchone()[0]
        self.assertEqual(generations,0)
        self.assertEqual(self._business_state()["current_count_cache"],8)

    def test_duration_consumes_complete_expression(self) -> None:
        cases={"禁言1天":86400,"禁言1天10分钟":87000,"禁言一个半小时":5400,
            "禁言1小时30秒":3630,"禁言半个小时":1800,"禁言两小时三十分钟":9000}
        for action,seconds in cases.items():
            with self.subTest(action=action):
                self.assertEqual(parse_mute_seconds(action),seconds)
                self.assertEqual(classify_severity(action),Severity.SEVERE if seconds>=3600 else Severity.LIGHT)
        for action in ("禁言1天未知单位10分钟","禁言-1小时","禁言1小时之外还有别的措施","禁言1e300小时"):
            with self.subTest(action=action):
                self.assertIsNone(parse_mute_seconds(action))
                self.assertEqual(classify_severity(action),Severity.UNKNOWN)

    def test_unrelated_withdrawal_keeps_invalid_input_review_pending(self) -> None:
        valid=self._add_record("2026-09-01 12:00:00")
        future=self._raw_record(1,"蜂巢","2027-01-01 12:00:00")
        record_policy_review(self.conn,member_id=1,group_area="蜂巢",source_record_id=future,
            key="synthetic-future-review",at="2026-09-05 12:00:00",reason="合成未来输入")
        withdraw_violation_record(self.conn,valid,effective_at="2026-09-05 12:01:00",reason="合成撤回")
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM v102_pending_actions WHERE action_type='input_review' AND status='pending'").fetchone()[0],1)
        self.assertEqual(self._policy_state()["pending_action_type"],"input_review")


class PolicyNotificationReliabilityTests(unittest.TestCase):
    setUp=PolicySchedulerTests.setUp
    tearDown=PolicySchedulerTests.tearDown

    def _backlog(self,count: int, text_size: int = 100) -> None:
        now="2026-08-02 12:00:00"
        with db.connect() as conn:
            for index in range(count):
                event_id,_=_insert_event(conn,member_id=self.member_id,group_area="蜂巢",
                    event_type="test_notice",effective_time=now,event_priority=100,
                    source_sequence=index,ingest_time=now,idempotency_key=f"batch:{index}")
                conn.execute("""INSERT INTO v102_notification_outbox(
                    event_id,member_id,group_area,message_type,message_text,scheduled_at,
                    status,last_error,created_at,updated_at)
                    VALUES(?,?,'蜂巢','policy_event',?,?,'failed','bot_offline',?,?)""",
                    (event_id,self.member_id,f"BEGIN-{index}|"+"合成"*text_size+f"|END-{index}",now,now,now))

    def test_backlog_is_claimed_and_sent_in_bounded_parts_without_loss(self) -> None:
        self._backlog(37,1200)
        bot=FakeBot()
        with patch.object(scheduler,"_business_allowed",return_value=True):
            handled=asyncio.run(scheduler.deliver_missed_policy_summary(bot,as_of="2026-08-02 12:02:00"))
        self.assertGreater(handled,0)
        self.assertLess(handled,37)
        self.assertGreater(len(bot.forwarded),1)
        all_text="".join(node["data"]["content"] for msg in bot.forwarded for node in msg["messages"])
        for index in range(handled):
            self.assertIn(f"BEGIN-{index}|",all_text)
            self.assertIn(f"|END-{index}",all_text)
        for msg in bot.forwarded:
            self.assertLessEqual(len(msg["messages"]),20)
            self.assertLessEqual(sum(len(node["data"]["content"]) for node in msg["messages"]),12000)
            for node in msg["messages"]:
                self.assertLessEqual(len(node["data"]["content"]),1800)

    def test_one_oversized_item_is_split_and_all_text_is_preserved(self) -> None:
        self._backlog(1,14000)
        bot=FakeBot()
        with patch.object(scheduler,"_business_allowed",return_value=True):
            handled=asyncio.run(scheduler.deliver_missed_policy_summary(bot,as_of="2026-08-02 12:02:00"))
        self.assertEqual(handled,1)
        self.assertGreater(len(bot.forwarded),1)
        all_text="".join(node["data"]["content"] for msg in bot.forwarded for node in msg["messages"])
        self.assertEqual(all_text.count("合成"),14000)
        self.assertIn("|END-0",all_text)

    def test_successful_batch_is_not_restored_after_later_batch_failure(self) -> None:
        self._backlog(20,1200)
        class FailSecondBot(FakeBot):
            async def call_api(self,api,**kwargs):
                await super().call_api(api,**kwargs)
                if len(self.forwarded)==2:
                    raise RuntimeError("synthetic second batch failure")
        bot=FailSecondBot()
        with patch.object(scheduler,"_business_allowed",return_value=True):
            handled=asyncio.run(scheduler.deliver_missed_policy_summary(bot,as_of="2026-08-02 12:02:00"))
        with db.connect() as conn:
            statuses=dict(conn.execute("SELECT status,COUNT(*) FROM v102_notification_outbox GROUP BY status"))
        self.assertGreater(handled,0)
        self.assertLess(handled,20)
        self.assertEqual(statuses.get("sent"),handled)
        self.assertEqual(statuses.get("sending",0),0)
        self.assertGreater(statuses.get("failed",0),0)


class PolicyCompensationReliabilityTests(unittest.TestCase):
    setUp=PolicySchedulerTests.setUp
    tearDown=PolicySchedulerTests.tearDown

    def test_unexpected_failure_rolls_back_partial_projection_before_release(self) -> None:
        def fail_after_write(conn,*args,**kwargs):
            conn.execute("UPDATE member_group_states SET deduct_count=99")
            raise RuntimeError("synthetic projection failure")
        with db.connect() as conn:
            with patch.object(policy_bridge,"process_violation_record",side_effect=fail_after_write):
                with self.assertRaises(RuntimeError):
                    policy_bridge._process_record_isolated(conn,1,ingest_time="2026-08-02 12:00:00")
            self.assertEqual(conn.execute("SELECT deduct_count FROM member_group_states").fetchone()[0],0)

    def test_invalid_future_input_is_quarantined_and_other_member_continues(self) -> None:
        now="2026-08-02 12:00:00"
        with db.connect() as conn:
            ensure_policy_scope_snapshot(conn,self.member_id,"蜂巢",now)
            other=conn.execute("INSERT INTO members(qq_number,qq_nickname,created_at,updated_at) VALUES('456789','合成成员',?,?)",(now,now)).lastrowid
            conn.execute("INSERT INTO member_group_states(member_id,group_area,created_at,updated_at) VALUES(?,'蜂窝',?,?)",(other,now,now))
            ensure_policy_scope_snapshot(conn,other,"蜂窝",now)
            ids=[]
            for member,area,effective in ((self.member_id,"蜂巢","2027-01-01 12:00:00"),(other,"蜂窝",now)):
                ids.append(conn.execute("""INSERT INTO violation_records(member_id,group_area,violation_time,judgement,action,created_at,updated_at)
                    VALUES(?,?,?,'合成测试','禁言10分钟',?,?)""",(member,area,effective,now,now)).lastrowid)
        stats=policy_bridge.run_policy_maintenance("2026-08-02 12:01:00")
        with db.connect() as conn:
            review=conn.execute("SELECT * FROM v102_pending_actions WHERE member_id=? AND action_type='input_review' AND status='pending'",(self.member_id,)).fetchone()
            self.assertIsNotNone(review)
            self.assertIsNotNone(conn.execute("SELECT 1 FROM v102_policy_events WHERE source_record_id=? AND event_type='mute_recorded'",(ids[1],)).fetchone())
            first_event_count=conn.execute("SELECT COUNT(*) FROM v102_policy_events").fetchone()[0]
        policy_bridge.run_policy_maintenance("2026-08-02 12:02:00")
        with db.connect() as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM v102_policy_events").fetchone()[0],first_event_count)
        self.assertGreaterEqual(stats["compensated"],1)
        with db.connect() as conn:
            conn.execute("UPDATE violation_records SET is_withdrawn=1 WHERE id=?",(ids[0],))
        policy_bridge.run_policy_maintenance("2026-08-02 12:03:00")
        with db.connect() as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM v102_pending_actions WHERE member_id=? AND action_type='input_review' AND status='pending'",(self.member_id,)).fetchone()[0],0)

    def test_new_valid_status_command_can_correct_quarantined_future_status(self) -> None:
        now="2026-08-02 12:00:00"
        def add_job(status,effective):
            with db.connect() as conn:
                log_id=conn.execute("INSERT INTO operation_logs(operation_type,source,created_at) VALUES('合成状态','手动',?)",(now,)).lastrowid
                conn.execute("""INSERT INTO v102_status_bridge_jobs(operation_log_id,member_id,group_area,
                    target_status,effective_at,idempotency_key,created_at,updated_at)
                    VALUES(?,?,'蜂巢',?,?,?,?,?)""",(log_id,self.member_id,status,effective,f"synthetic-status:{log_id}",now,now))
        add_job("已质询","2027-01-01 12:00:00")
        self.assertEqual(policy_bridge.process_status_bridge_jobs(as_of="2026-08-02 12:01:00"),0)
        add_job("正常","2026-08-02 12:02:00")
        self.assertEqual(policy_bridge.process_status_bridge_jobs(as_of="2026-08-02 12:03:00"),1)
        with db.connect() as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM v102_pending_actions WHERE action_type='input_review' AND status='pending'").fetchone()[0],0)
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM v102_policy_events WHERE event_type='status_changed' AND effective_time>'2026-08-02 12:03:00'").fetchone()[0],0)
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM v102_status_bridge_jobs WHERE job_status!='applied'").fetchone()[0],0)
