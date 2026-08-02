from __future__ import annotations

from datetime import datetime, timedelta

from .config import CONFIG
from .db import connect, now_str
from .deduction_policy import (
    PolicyOutcome,
    process_status_change,
    process_violation_record,
    settle_due_cycles,
    withdraw_violation_record,
)
from .policy_schema import require_v102_schema


def bridge_violation_record(source_record_id: int) -> PolicyOutcome | None:
    if not CONFIG.deduction_policy_v102_enabled:
        return None
    with connect() as conn:
        require_v102_schema(conn)
        return process_violation_record(
            conn, source_record_id, ingest_time=now_str()
        )


def bridge_withdrawal(
    source_record_id: int, *, reason: str, effective_at: str | None = None
) -> PolicyOutcome | None:
    if not CONFIG.deduction_policy_v102_enabled:
        return None
    with connect() as conn:
        require_v102_schema(conn)
        return withdraw_violation_record(
            conn,
            source_record_id,
            effective_at=effective_at or now_str(),
            reason=reason,
        )


def bridge_status_change(
    *,
    member_id: int,
    group_area: str,
    status: str,
    effective_at: str,
    idempotency_key: str,
    ingest_time: str | None = None,
    caused_by_event_id: int | None = None,
) -> PolicyOutcome | None:
    if not CONFIG.deduction_policy_v102_enabled:
        return None
    with connect() as conn:
        require_v102_schema(conn)
        return process_status_change(
            conn,
            member_id=member_id,
            group_area=group_area,
            status=status,
            effective_at=effective_at,
            idempotency_key=idempotency_key,
            ingest_time=ingest_time or now_str(),
            caused_by_event_id=caused_by_event_id,
        )


def _cutover_watermark(conn) -> int:
    row = conn.execute(
        """
        SELECT cutover_record_watermark
        FROM v102_migration_checkpoints
        WHERE status='applied'
        ORDER BY cutover_at DESC, batch_id DESC LIMIT 1
        """
    ).fetchone()
    if row is None:
        raise RuntimeError("v1.0.2beta migration checkpoint is missing")
    return int(row["cutover_record_watermark"] or 0)


def compensate_unprocessed_records(conn, *, ingest_time: str) -> int:
    watermark = _cutover_watermark(conn)
    rows = conn.execute(
        """
        SELECT r.*
        FROM violation_records r
        WHERE r.id>?
          AND r.is_test=0 AND r.is_countable=1
          AND (
              (
                  r.is_withdrawn=1
                  AND NOT EXISTS (
                      SELECT 1 FROM v102_policy_events e
                      WHERE e.source_record_id=r.id
                        AND e.event_type='record_withdrawn'
                  )
              )
              OR
              (
                  r.is_withdrawn=0
                  AND NOT EXISTS (
                      SELECT 1 FROM v102_policy_events e
                      WHERE e.source_record_id=r.id
                        AND e.event_type IN (
                            'mute_recorded', 'mute_duration_unknown'
                        )
                  )
                )
          )
        ORDER BY r.id
        """,
        (watermark,),
    ).fetchall()
    processed = 0
    for row in rows:
        if int(row["is_withdrawn"] or 0):
            withdraw_violation_record(
                conn,
                int(row["id"]),
                effective_at=ingest_time,
                reason=row["withdrawn_reason"] or "补偿扫描发现已撤回记录",
            )
        else:
            process_violation_record(
                conn, int(row["id"]), ingest_time=ingest_time
            )
        processed += 1
    return processed


_NOTIFIABLE_EVENT_TYPES = (
    "cycle_started",
    "slow_entered",
    "slow_extended",
    "cycle_settled",
    "cycle_bad_at_due",
    "stop_due_for_decision",
    "final_warning_recovered",
    "rejected_cycle_closed",
)


def cancel_baseline_initialization_outbox(
    conn, *, as_of: str
) -> int:
    rows = conn.execute(
        """
        SELECT o.id, o.attempt_count, o.status
        FROM v102_notification_outbox o
        JOIN v102_policy_events e ON e.id=o.event_id
        JOIN v102_policy_events cause ON cause.id=e.caused_by_event_id
        WHERE e.event_type='cycle_started'
          AND cause.event_type='baseline_migrated'
          AND o.status IN ('pending', 'sending', 'failed')
        ORDER BY o.id
        """
    ).fetchall()
    for row in rows:
        if row["status"] == "sending" and int(row["attempt_count"] or 0) > 0:
            conn.execute(
                """
                UPDATE v102_notification_attempts
                SET status='cancelled', finished_at=?,
                    detail='baseline initialization is audit only', updated_at=?
                WHERE outbox_id=? AND attempt_number=? AND status='sending'
                """,
                (as_of, as_of, row["id"], row["attempt_count"]),
            )
        conn.execute(
            """
            UPDATE v102_notification_outbox
            SET status='cancelled',
                last_error='baseline initialization is audit only',
                updated_at=?
            WHERE id=? AND status IN ('pending', 'sending', 'failed')
            """,
            (as_of, row["id"]),
        )
    return len(rows)


def queue_unannounced_events(conn) -> int:
    cancel_baseline_initialization_outbox(
        conn,
        as_of=datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    )
    placeholders = ",".join("?" for _ in _NOTIFIABLE_EVENT_TYPES)
    rows = conn.execute(
        f"""
        SELECT e.*, m.qq_number, m.qq_nickname
        FROM v102_policy_events e
        JOIN members m ON m.id=e.member_id
        WHERE e.is_effective=1
          AND e.event_type IN ({placeholders})
          AND NOT (
              e.event_type='cycle_started'
              AND EXISTS (
                  SELECT 1 FROM v102_policy_events cause
                  WHERE cause.id=e.caused_by_event_id
                    AND cause.event_type='baseline_migrated'
              )
          )
          AND NOT EXISTS (
              SELECT 1 FROM v102_notification_outbox o
              WHERE o.event_id=e.id AND o.message_type='policy_event'
                AND o.reminder_slot=''
          )
        ORDER BY e.id
        """,
        _NOTIFIABLE_EVENT_TYPES,
    ).fetchall()
    queued = 0
    for row in rows:
        text = (
            "【v1.0.2beta 减数事件】\n"
            f"事件：#{row['id']} {row['event_type']}\n"
            f"成员：{row['qq_nickname'] or '未知昵称'}（{row['qq_number']}）\n"
            f"群域：{row['group_area']}\n"
            f"生效时间：{row['effective_time']}\n"
            f"规则版本：{row['rule_version']}"
        )
        cursor = conn.execute(
            """
            INSERT OR IGNORE INTO v102_notification_outbox(
                event_id, member_id, group_area, message_type,
                reminder_slot, message_text, scheduled_at,
                created_at, updated_at
            ) VALUES(?, ?, ?, 'policy_event', '', ?, ?, ?, ?)
            """,
            (
                row["id"],
                row["member_id"],
                row["group_area"],
                text,
                row["ingest_time"],
                row["ingest_time"],
                row["ingest_time"],
            ),
        )
        queued += cursor.rowcount
    return queued


def queue_pending_reminders(conn, *, as_of: str) -> int:
    rows = conn.execute(
        """
        SELECT p.*, m.qq_number, m.qq_nickname
        FROM v102_pending_actions p
        JOIN members m ON m.id=p.member_id
        WHERE p.status='pending' AND p.caused_by_event_id IS NOT NULL
          AND (p.next_reminder_at IS NULL OR p.next_reminder_at<=?)
        ORDER BY p.id
        """,
        (as_of,),
    ).fetchall()
    slot = datetime.fromisoformat(as_of).strftime("%Y%m%d%H")
    next_at = (
        datetime.fromisoformat(as_of) + timedelta(hours=1)
    ).strftime("%Y-%m-%d %H:%M:%S")
    queued = 0
    for row in rows:
        text = (
            "【v1.0.2beta 管理待办】\n"
            f"事件：#{row['caused_by_event_id']}\n"
            f"成员：{row['qq_nickname'] or '未知昵称'}（{row['qq_number']}）\n"
            f"群域：{row['group_area']}\n"
            f"待办：{row['action_type']}\n"
            f"事由：{row['reason']}\n"
            "请管理组人工确认处理，系统不会代替决定。"
        )
        cursor = conn.execute(
            """
            INSERT OR IGNORE INTO v102_notification_outbox(
                event_id, pending_action_id, member_id, group_area, message_type,
                reminder_slot, message_text, scheduled_at,
                created_at, updated_at
            ) VALUES(?, ?, ?, ?, 'pending_reminder', ?, ?, ?, ?, ?)
            """,
            (
                row["caused_by_event_id"],
                row["id"],
                row["member_id"],
                row["group_area"],
                slot,
                text,
                as_of,
                as_of,
                as_of,
            ),
        )
        queued += cursor.rowcount
        conn.execute(
            """
            UPDATE v102_pending_actions
            SET next_reminder_at=?, updated_at=? WHERE id=?
            """,
            (next_at, as_of, row["id"]),
        )
    return queued


def process_status_bridge_jobs(
    *, as_of: str | datetime | None = None, limit: int = 100
) -> int:
    moment = (
        as_of.strftime("%Y-%m-%d %H:%M:%S")
        if isinstance(as_of, datetime)
        else (as_of or now_str())
    )
    lease_cutoff = (
        datetime.fromisoformat(moment) - timedelta(minutes=5)
    ).strftime("%Y-%m-%d %H:%M:%S")
    with connect() as conn:
        conn.execute(
            """
            UPDATE v102_status_bridge_jobs
            SET job_status='failed', last_error='processing lease expired',
                updated_at=?
            WHERE job_status='processing' AND updated_at<=?
            """,
            (moment, lease_cutoff),
        )
        job_ids = [
            int(row["id"])
            for row in conn.execute(
                """
                SELECT id FROM v102_status_bridge_jobs
                WHERE job_status IN ('pending', 'failed')
                ORDER BY id LIMIT ?
                """,
                (limit,),
            ).fetchall()
        ]

    applied = 0
    for job_id in job_ids:
        with connect() as conn:
            claimed = conn.execute(
                """
                UPDATE v102_status_bridge_jobs
                SET job_status='processing', attempt_count=attempt_count+1,
                    last_error=NULL, updated_at=?
                WHERE id=? AND job_status IN ('pending', 'failed')
                """,
                (moment, job_id),
            )
            if not claimed.rowcount:
                continue
        try:
            with connect() as conn:
                job = conn.execute(
                    "SELECT * FROM v102_status_bridge_jobs WHERE id=?", (job_id,)
                ).fetchone()
                caused_by_event_id = None
                if job["caused_by_record_id"] is not None:
                    source = conn.execute(
                        "SELECT is_withdrawn FROM violation_records WHERE id=?",
                        (job["caused_by_record_id"],),
                    ).fetchone()
                    if source is None:
                        raise RuntimeError("causal violation record is missing")
                    if int(source["is_withdrawn"] or 0):
                        conn.execute(
                            """
                            UPDATE v102_status_bridge_jobs
                            SET job_status='applied', applied_event_id=NULL,
                                last_error='causal record withdrawn before bridge',
                                updated_at=? WHERE id=?
                            """,
                            (moment, job_id),
                        )
                        applied += 1
                        continue
                    source_event = conn.execute(
                        """
                        SELECT id FROM v102_policy_events
                        WHERE source_record_id=? AND replay_generation=0
                          AND is_effective=1
                          AND event_type IN ('mute_recorded', 'mute_duration_unknown')
                        ORDER BY id LIMIT 1
                        """,
                        (job["caused_by_record_id"],),
                    ).fetchone()
                    if source_event is None:
                        raise RuntimeError("causal violation event is not ready")
                    caused_by_event_id = int(source_event["id"])
                outcome = process_status_change(
                    conn,
                    member_id=int(job["member_id"]),
                    group_area=job["group_area"],
                    status=job["target_status"],
                    effective_at=job["effective_at"],
                    ingest_time=moment,
                    idempotency_key=job["idempotency_key"],
                    caused_by_event_id=caused_by_event_id,
                )
                conn.execute(
                    """
                    UPDATE v102_status_bridge_jobs
                    SET job_status='applied', applied_event_id=?,
                        last_error=NULL, updated_at=? WHERE id=?
                    """,
                    (outcome.event_id, moment, job_id),
                )
            applied += 1
        except Exception as exc:
            with connect() as conn:
                conn.execute(
                    """
                    UPDATE v102_status_bridge_jobs
                    SET job_status='failed', last_error=?, updated_at=?
                    WHERE id=?
                    """,
                    (f"{type(exc).__name__}: {exc}", moment, job_id),
                )
    return applied


def run_policy_maintenance(as_of: str | datetime | None = None) -> dict[str, int]:
    if not CONFIG.deduction_policy_v102_enabled:
        return {"compensated": 0, "status_compensated": 0, "settled": 0}
    moment = (
        as_of.strftime("%Y-%m-%d %H:%M:%S")
        if isinstance(as_of, datetime)
        else (as_of or now_str())
    )
    with connect() as conn:
        require_v102_schema(conn)
        compensated = compensate_unprocessed_records(conn, ingest_time=moment)
    status_compensated = process_status_bridge_jobs(as_of=moment)
    with connect() as conn:
        settled = settle_due_cycles(conn, moment)
        queued_events = queue_unannounced_events(conn)
        queued_reminders = queue_pending_reminders(conn, as_of=moment)
    return {
        "compensated": compensated,
        "status_compensated": status_compensated,
        "settled": settled,
        "queued_events": queued_events,
        "queued_reminders": queued_reminders,
    }
