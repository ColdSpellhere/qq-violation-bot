from __future__ import annotations

import hashlib
import json
import math
import re
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import Enum


class Severity(str, Enum):
    NONE = "none"
    LIGHT = "light"
    SEVERE = "severe"
    UNKNOWN = "unknown"


RULE_VERSION = "v1.0.2beta"
TERMINAL_STATUSES = frozenset({"已退群", "已移出", "已拉黑"})


@dataclass(frozen=True)
class PolicyOutcome:
    event_id: int | None
    changed: bool
    pending_action_id: int | None = None


class PolicyInputError(ValueError):
    """An invalid recorded input must be reviewed before scope settlement."""


class PolicyReplayConflict(ValueError):
    """Historical human decisions cannot be re-applied to changed evidence."""


_MANUAL_INPUT_TYPES = frozenset({
    "manual_stop_started", "manual_stop_cleared",
    "manual_stop_renewed", "stop_suggestion_rejected",
})


_CHINESE_DIGITS = {
    "零": 0,
    "〇": 0,
    "一": 1,
    "二": 2,
    "两": 2,
    "三": 3,
    "四": 4,
    "五": 5,
    "六": 6,
    "七": 7,
    "八": 8,
    "九": 9,
}
_CHINESE_UNITS = {"十": 10, "百": 100}


def _number_value(token: str) -> float | None:
    value = token.strip()
    if not value:
        return None
    if value == "半":
        return 0.5
    if value.endswith("半"):
        whole = _number_value(value[:-1])
        return None if whole is None else whole + 0.5
    try:
        return float(value)
    except ValueError:
        pass

    total = 0
    current = 0
    for char in value:
        if char in _CHINESE_DIGITS:
            current = _CHINESE_DIGITS[char]
            continue
        unit = _CHINESE_UNITS.get(char)
        if unit is None:
            return None
        total += (current or 1) * unit
        current = 0
    return float(total + current)


def parse_mute_seconds(action: str | None) -> int | None:
    text = str(action or "").strip()
    if not text.startswith("禁言"):
        return None
    text = re.sub(r"\s+", "", text[len("禁言"):]).lstrip(":：")
    text = text.replace("个半", "半").replace("半个", "半")
    duration_pattern = re.compile(
        r"([0-9]+(?:\.[0-9]+)?|[零〇一二两三四五六七八九十百半]+)"
        r"个?(小时|钟头|分钟|分|秒钟|秒|天|日|星期|礼拜|周|个月|月)"
    )
    unit_seconds = {
        "小时": 3600, "钟头": 3600, "分钟": 60, "分": 60,
        "秒钟": 1, "秒": 1, "天": 86400, "日": 86400,
        "星期": 604800, "礼拜": 604800, "周": 604800,
        "个月": 2592000, "月": 2592000,
    }
    seconds = 0.0
    consumed = 0
    for match in duration_pattern.finditer(text):
        if match.start() != consumed:
            return None
        value = _number_value(match.group(1))
        if value is None or not math.isfinite(value):
            return None
        seconds += value * unit_seconds[match.group(2)]
        consumed = match.end()
    if consumed != len(text) or not math.isfinite(seconds) or seconds > 2**63 - 1:
        return None
    return int(seconds) if seconds >= 1 else None


def classify_severity(action: str | None) -> Severity:
    text = str(action or "").strip()
    if not text or ("警告" in text and "禁言" not in text):
        return Severity.NONE
    seconds = parse_mute_seconds(text)
    if seconds is None:
        return Severity.UNKNOWN
    return Severity.SEVERE if seconds >= 3600 else Severity.LIGHT


def raw_effective_record_summary(
    conn: sqlite3.Connection,
    member_id: int,
    group_area: str,
    *,
    through_time: str | None = None,
    through_record_id: int | None = None,
) -> tuple[int, str | None]:
    cutoff_sql = ""
    params: list[object] = [member_id, group_area]
    if through_time is not None:
        if through_record_id is None:
            cutoff_sql = "AND violation_time<=?"
            params.append(_time_text(through_time))
        else:
            cutoff_sql = (
                "AND (violation_time<? OR "
                "(violation_time=? AND id<=?))"
            )
            cutoff = _time_text(through_time)
            params.extend((cutoff, cutoff, int(through_record_id)))
    row = conn.execute(
        f"""
        SELECT COALESCE(SUM(count_delta), 0) AS total,
               MAX(violation_time) AS last_time
        FROM violation_records
        WHERE member_id=? AND group_area=?
          AND is_withdrawn=0 AND is_test=0 AND is_countable=1
          {cutoff_sql}
        """,
        params,
    ).fetchone()
    return int(row["total"] or 0), row["last_time"]


def baseline_adjustment(
    conn: sqlite3.Connection, member_id: int, group_area: str
) -> int:
    table = conn.execute(
        """
        SELECT 1 FROM sqlite_master
        WHERE type='table' AND name='v102_policy_state'
        """
    ).fetchone()
    if not table:
        return 0
    row = conn.execute(
        """
        SELECT baseline_adjustment
        FROM v102_policy_state
        WHERE member_id=? AND group_area=?
        """,
        (member_id, group_area),
    ).fetchone()
    return int(row["baseline_adjustment"] or 0) if row else 0


def effective_record_summary(
    conn: sqlite3.Connection,
    member_id: int,
    group_area: str,
    *,
    through_time: str | None = None,
    through_record_id: int | None = None,
) -> tuple[int, str | None]:
    raw_total, last_time = raw_effective_record_summary(
        conn,
        member_id,
        group_area,
        through_time=through_time,
        through_record_id=through_record_id,
    )
    total = max(0, raw_total + baseline_adjustment(conn, member_id, group_area))
    return total, last_time


def effective_total(
    conn: sqlite3.Connection,
    member_id: int,
    group_area: str,
    *,
    through_time: str | None = None,
    through_record_id: int | None = None,
) -> int:
    total, _ = effective_record_summary(
        conn,
        member_id,
        group_area,
        through_time=through_time,
        through_record_id=through_record_id,
    )
    return total


def sync_count_state(
    conn: sqlite3.Connection,
    member_id: int,
    group_area: str,
    *,
    updated_at: str,
    through_time: str | None = None,
    through_record_id: int | None = None,
) -> sqlite3.Row:
    total, last_time = effective_record_summary(
        conn,
        member_id,
        group_area,
        through_time=through_time,
        through_record_id=through_record_id,
    )
    state = conn.execute(
        """
        SELECT * FROM member_group_states
        WHERE member_id=? AND group_area=?
        """,
        (member_id, group_area),
    ).fetchone()
    if state is None:
        raise LookupError(f"missing member_group_state: {member_id}/{group_area}")

    deduct_count = max(0, int(state["deduct_count"] or 0))
    current = max(0, total - deduct_count)
    projected_last_time = last_time
    if projected_last_time is None and total > 0:
        projected_last_time = state["last_effective_violation_time"]
    conn.execute(
        """
        UPDATE member_group_states
        SET total_count=?, current_count_cache=?,
            last_effective_violation_time=?, updated_at=?
        WHERE id=?
        """,
        (total, current, projected_last_time, updated_at, state["id"]),
    )
    return conn.execute(
        "SELECT * FROM member_group_states WHERE id=?", (state["id"],)
    ).fetchone()


def _time_text(value: str | datetime) -> str:
    if isinstance(value, datetime):
        return value.strftime("%Y-%m-%d %H:%M:%S")
    parsed = datetime.fromisoformat(str(value).strip())
    return parsed.strftime("%Y-%m-%d %H:%M:%S")


def _time_value(value: str | datetime) -> datetime:
    return value if isinstance(value, datetime) else datetime.fromisoformat(value)


def _json(data: dict[str, object]) -> str:
    return json.dumps(data, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _validate_effective_time(effective_at: str, ingested_at: str) -> None:
    if _time_value(effective_at) > _time_value(ingested_at):
        raise PolicyInputError("减数事件生效时间不能晚于处理时间；请撤回未来记录后重新录入")


def _has_later_effective_event(
    conn: sqlite3.Connection, *, member_id: int, group_area: str,
    effective_at: str, priority: int, source_sequence: int, event_id: int,
) -> bool:
    """Ingestion latency alone does not make an input late in the timeline."""
    return conn.execute(
        """SELECT 1 FROM v102_policy_events
        WHERE member_id=? AND group_area=? AND is_effective=1 AND id!=?
          AND (effective_time,event_priority,source_sequence,id)>(?,?,?,?)
        LIMIT 1""",
        (member_id,group_area,event_id,effective_at,priority,source_sequence,event_id),
    ).fetchone() is not None


def _insert_event(
    conn: sqlite3.Connection,
    *,
    member_id: int,
    group_area: str,
    event_type: str,
    effective_time: str,
    event_priority: int,
    source_sequence: int,
    ingest_time: str,
    idempotency_key: str,
    payload: dict[str, object] | None = None,
    source_record_id: int | None = None,
    caused_by_event_id: int | None = None,
    replay_generation: int = 0,
) -> tuple[int, bool]:
    cursor = conn.execute(
        """
        INSERT OR IGNORE INTO v102_policy_events(
            member_id, group_area, event_type, effective_time,
            event_priority, source_sequence, ingest_time, source_record_id,
            caused_by_event_id, replay_generation, payload_json,
            rule_version, idempotency_key, created_at
        ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            member_id,
            group_area,
            event_type,
            effective_time,
            event_priority,
            source_sequence,
            ingest_time,
            source_record_id,
            caused_by_event_id,
            replay_generation,
            _json(payload or {}),
            RULE_VERSION,
            idempotency_key,
            ingest_time,
        ),
    )
    row = conn.execute(
        "SELECT id FROM v102_policy_events WHERE idempotency_key=?",
        (idempotency_key,),
    ).fetchone()
    return int(row["id"]), cursor.rowcount == 1


def ensure_policy_scope_snapshot(
    conn: sqlite3.Connection, member_id: int, group_area: str, at: str
) -> sqlite3.Row:
    business = _business_state(conn, member_id, group_area)
    business_columns = set(business.keys())

    def business_value(name: str, default=None):
        return business[name] if name in business_columns else default

    raw_total, _ = raw_effective_record_summary(conn, member_id, group_area)
    record_watermark = int(
        conn.execute("SELECT COALESCE(MAX(id), 0) FROM violation_records").fetchone()[0]
    )
    conn.execute(
        """
        INSERT INTO v102_policy_state(
            member_id, group_area, baseline_adjustment,
            baseline_total_count, baseline_deduct_count,
            baseline_current_count, baseline_raw_total,
            baseline_record_watermark, baseline_locked, baseline_status,
            baseline_last_effective_violation_time,
            baseline_last_deduct_time, baseline_last_final_warning_time,
            baseline_initialized_at, created_at, updated_at
        ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(member_id, group_area) DO NOTHING
        """,
        (
            member_id,
            group_area,
            int(business["total_count"] or 0) - raw_total,
            max(0, int(business["total_count"] or 0)),
            max(0, int(business["deduct_count"] or 0)),
            max(0, int(business["current_count_cache"] or 0)),
            raw_total,
            record_watermark,
            1 if int(business_value("locked", 0) or 0) else 0,
            business["status"],
            business_value("last_effective_violation_time"),
            business_value("last_deduct_time"),
            business_value("last_final_warning_time"),
            at,
            at,
            at,
        ),
    )
    return conn.execute(
        """
        SELECT * FROM v102_policy_state
        WHERE member_id=? AND group_area=?
        """,
        (member_id, group_area),
    ).fetchone()


def _ensure_policy_state(
    conn: sqlite3.Connection, member_id: int, group_area: str, at: str
) -> sqlite3.Row:
    return ensure_policy_scope_snapshot(conn, member_id, group_area, at)


def _business_state(
    conn: sqlite3.Connection, member_id: int, group_area: str
) -> sqlite3.Row:
    row = conn.execute(
        """
        SELECT * FROM member_group_states
        WHERE member_id=? AND group_area=?
        """,
        (member_id, group_area),
    ).fetchone()
    if row is None:
        raise LookupError(f"missing member_group_state: {member_id}/{group_area}")
    return row


def _active_cycle(
    conn: sqlite3.Connection, member_id: int, group_area: str
) -> sqlite3.Row | None:
    return conn.execute(
        """
        SELECT c.*
        FROM v102_policy_cycles c
        JOIN v102_policy_state s ON s.active_cycle_id=c.id
        WHERE s.member_id=? AND s.group_area=?
          AND c.status IN ('active', 'pending_decision')
        """,
        (member_id, group_area),
    ).fetchone()


def _set_no_cycle(
    conn: sqlite3.Connection,
    member_id: int,
    group_area: str,
    reason: str,
    at: str,
    *,
    preserve_tag: bool = False,
) -> None:
    conn.execute(
        """
        UPDATE v102_policy_state
        SET policy_tag=CASE WHEN ? THEN policy_tag ELSE 'none' END,
            active_cycle_id=NULL, no_cycle_reason=?,
            pending_action_type=NULL, state_version=state_version+1,
            updated_at=?
        WHERE member_id=? AND group_area=?
        """,
        (int(preserve_tag), reason, at, member_id, group_area),
    )


def _start_cycle(
    conn: sqlite3.Connection,
    *,
    member_id: int,
    group_area: str,
    cycle_type: str,
    start_at: str,
    caused_by_event_id: int,
    fixed_sequence: int = 0,
    replay_generation: int = 0,
) -> int | None:
    policy = _ensure_policy_state(conn, member_id, group_area, start_at)
    business = _business_state(conn, member_id, group_area)
    current = max(
        0,
        int(business["total_count"] or 0) - int(business["deduct_count"] or 0),
    )
    if business["status"] in TERMINAL_STATUSES:
        _set_no_cycle(
            conn,
            member_id,
            group_area,
            "terminal_status",
            start_at,
            preserve_tag=True,
        )
        return None
    if (
        int(policy["v102_operation_count"] or 0) >= 5
        and cycle_type in {"normal", "slow"}
    ):
        _set_no_cycle(conn, member_id, group_area, "operation_limit", start_at)
        return None
    if current <= 0 and cycle_type == "normal":
        _set_no_cycle(conn, member_id, group_area, "zero_count", start_at)
        return None

    slow_level = int(policy["slow_level"] or 0)
    if cycle_type == "slow":
        slow_level += 1
        length_days = 14 + (7 * slow_level)
        policy_tag = "slow"
    elif cycle_type == "normal":
        length_days = 14
        policy_tag = "none"
    elif cycle_type == "stop":
        length_days = 30
        policy_tag = "stop"
    elif cycle_type == "final_warning":
        length_days = 90
        policy_tag = "stop"
    else:
        raise ValueError(f"unsupported cycle type: {cycle_type}")

    due_at = _time_text(_time_value(start_at) + timedelta(days=length_days))
    cursor = conn.execute(
        """
        INSERT INTO v102_policy_cycles(
            member_id, group_area, cycle_type, start_at, due_at,
            slow_level, fixed_sequence, replay_generation, created_at, updated_at
        ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            member_id,
            group_area,
            cycle_type,
            start_at,
            due_at,
            slow_level if cycle_type == "slow" else 0,
            fixed_sequence,
            replay_generation,
            start_at,
            start_at,
        ),
    )
    cycle_id = int(cursor.lastrowid)
    event_id, _ = _insert_event(
        conn,
        member_id=member_id,
        group_area=group_area,
        event_type="cycle_started",
        effective_time=start_at,
        event_priority=40,
        source_sequence=caused_by_event_id,
        ingest_time=start_at,
        caused_by_event_id=caused_by_event_id,
        idempotency_key=f"event:{caused_by_event_id}:cycle:{cycle_id}:started",
        payload={"cycle_id": cycle_id, "cycle_type": cycle_type, "due_at": due_at},
        replay_generation=replay_generation,
    )
    conn.execute(
        """
        UPDATE v102_policy_state
        SET policy_tag=?, slow_level=?, active_cycle_id=?,
            no_cycle_reason=NULL, last_processed_event_id=?,
            state_version=state_version+1, updated_at=?
        WHERE member_id=? AND group_area=?
        """,
        (
            policy_tag,
            slow_level,
            cycle_id,
            event_id,
            start_at,
            member_id,
            group_area,
        ),
    )
    return cycle_id


def _enter_slow(
    conn: sqlite3.Connection,
    cycle: sqlite3.Row,
    *,
    caused_by_event_id: int,
    at: str,
) -> sqlite3.Row:
    policy = _ensure_policy_state(conn, cycle["member_id"], cycle["group_area"], at)
    slow_level = int(policy["slow_level"] or 0) + 1
    due_at = _time_text(
        _time_value(cycle["start_at"]) + timedelta(days=14 + 7 * slow_level)
    )
    conn.execute(
        """
        UPDATE v102_policy_cycles
        SET cycle_type='slow', slow_level=?, due_at=?,
            slow_light_count=0, slow_extended=0, updated_at=?
        WHERE id=?
        """,
        (slow_level, due_at, at, cycle["id"]),
    )
    event_id, _ = _insert_event(
        conn,
        member_id=cycle["member_id"],
        group_area=cycle["group_area"],
        event_type="slow_entered",
        effective_time=at,
        event_priority=40,
        source_sequence=caused_by_event_id,
        ingest_time=at,
        caused_by_event_id=caused_by_event_id,
        idempotency_key=f"event:{caused_by_event_id}:cycle:{cycle['id']}:slow",
        payload={
            "cycle_id": int(cycle["id"]),
            "slow_level": slow_level,
            "due_at": due_at,
        },
        replay_generation=int(cycle["replay_generation"] or 0),
    )
    conn.execute(
        """
        UPDATE v102_policy_state
        SET policy_tag='slow', slow_level=?, no_cycle_reason=NULL,
            last_processed_event_id=?, state_version=state_version+1,
            updated_at=?
        WHERE member_id=? AND group_area=?
        """,
        (
            slow_level,
            event_id,
            at,
            cycle["member_id"],
            cycle["group_area"],
        ),
    )
    return conn.execute(
        "SELECT * FROM v102_policy_cycles WHERE id=?", (cycle["id"],)
    ).fetchone()


def _create_pending_action(
    conn: sqlite3.Connection,
    *,
    member_id: int,
    group_area: str,
    action_type: str,
    reason: str,
    caused_by_event_id: int,
    at: str,
) -> int:
    existing = conn.execute(
        """
        SELECT id FROM v102_pending_actions
        WHERE member_id=? AND group_area=? AND action_type=? AND status='pending'
        ORDER BY id LIMIT 1
        """,
        (member_id, group_area, action_type),
    ).fetchone()
    if existing:
        return int(existing["id"])
    key = f"event:{caused_by_event_id}:pending:{action_type}"
    cursor = conn.execute(
        """
        INSERT OR IGNORE INTO v102_pending_actions(
            member_id, group_area, action_type, due_at, next_reminder_at,
            reason, caused_by_event_id, idempotency_key, created_at, updated_at
        ) VALUES(?, ?, ?, NULL, ?, ?, ?, ?, ?, ?)
        """,
        (
            member_id,
            group_area,
            action_type,
            at,
            reason,
            caused_by_event_id,
            key,
            at,
            at,
        ),
    )
    if cursor.rowcount == 1:
        pending_id = int(cursor.lastrowid)
    else:
        pending_id = int(
            conn.execute(
                "SELECT id FROM v102_pending_actions WHERE idempotency_key=?", (key,)
            ).fetchone()["id"]
        )
    conn.execute(
        """
        UPDATE v102_policy_state
        SET pending_action_type=?, last_reason=?,
            state_version=state_version+1, updated_at=?
        WHERE member_id=? AND group_area=?
        """,
        (action_type, reason, at, member_id, group_area),
    )
    return pending_id


def policy_scope_under_review(conn: sqlite3.Connection, member_id: int, group_area: str) -> bool:
    return conn.execute(
        """SELECT 1 FROM v102_pending_actions
        WHERE member_id=? AND group_area=? AND status='pending'
          AND action_type IN ('input_review','replay_review') LIMIT 1""",
        (member_id, group_area),
    ).fetchone() is not None


def record_policy_review(
    conn: sqlite3.Connection, *, member_id: int, group_area: str,
    source_record_id: int | None, key: str, at: str, reason: str,
    action_type: str = "input_review",
) -> PolicyOutcome:
    """Preserve the last valid projection and make a blocked input durable."""
    _ensure_policy_state(conn, member_id, group_area, at)
    event_id, created = _insert_event(
        conn, member_id=member_id, group_area=group_area,
        event_type="policy_review_required", effective_time=at,
        event_priority=60, source_sequence=source_record_id or 0, ingest_time=at,
        source_record_id=source_record_id, idempotency_key=key,
        payload={"reason": reason, "review_type": action_type},
    )
    pending_id = _create_pending_action(
        conn, member_id=member_id, group_area=group_area, action_type=action_type,
        reason=reason, caused_by_event_id=event_id, at=at,
    )
    return PolicyOutcome(event_id, created, pending_id)


def _latest_review_checkpoint(conn, member_id: int, group_area: str):
    return conn.execute("""SELECT * FROM v102_policy_events
        WHERE member_id=? AND group_area=? AND event_type='policy_review_resolved'
          AND replay_generation=0 AND is_effective=1 ORDER BY id DESC LIMIT 1""",
        (member_id, group_area)).fetchone()


def policy_review_fingerprint(conn, member_id: int, group_area: str) -> str:
    """Bind confirmation to the exact member evidence and projection shown."""
    material = {}
    for table in ("member_group_states", "v102_policy_state", "v102_policy_cycles",
                  "violation_records", "v102_status_bridge_jobs"):
        material[table] = [dict(row) for row in conn.execute(
            f"SELECT * FROM {table} WHERE member_id=? AND group_area=? ORDER BY id" if table != "v102_policy_state"
            else f"SELECT * FROM {table} WHERE member_id=? AND group_area=?",
            (member_id, group_area))]
    material["pending"] = [dict(row) for row in conn.execute("""SELECT id,action_type,status,reason,caused_by_event_id
        FROM v102_pending_actions WHERE member_id=? AND group_area=? ORDER BY id""",(member_id,group_area))]
    return hashlib.sha256(_json(material).encode()).hexdigest()


def resolve_policy_review(
    conn, *, member_id: int, group_area: str, pending_action_id: int,
    recovery_mode: str, expected_fingerprint: str, effective_at: str | datetime,
    reason: str, actor_qq: str, idempotency_key: str,
) -> PolicyOutcome:
    """Accept changed evidence and preserve executed human decisions explicitly."""
    at = _time_text(effective_at)
    existing = conn.execute("SELECT id FROM v102_policy_events WHERE idempotency_key=?",(idempotency_key,)).fetchone()
    if existing is not None:
        return PolicyOutcome(int(existing["id"]),False)
    if recovery_mode not in {"保留周期", "重新计时"} or not reason.strip() or not actor_qq.strip():
        raise ValueError("必须明确恢复方式、复核人和非空事由")
    pending = conn.execute("""SELECT * FROM v102_pending_actions WHERE member_id=? AND group_area=?
        AND action_type IN ('input_review','replay_review') AND status='pending' ORDER BY id""",
        (member_id,group_area)).fetchall()
    if len(pending) != 1 or pending[0]["action_type"] != "replay_review" or pending[0]["id"] != pending_action_id:
        raise ValueError("待办编号与成员群域不匹配，或还有其他输入复核未处理")
    if not expected_fingerprint or expected_fingerprint != policy_review_fingerprint(conn,member_id,group_area):
        raise ValueError("记录或策略状态已变化，请重新生成复核预览")
    policy = _ensure_policy_state(conn,member_id,group_area,at)
    records = conn.execute("""SELECT * FROM violation_records WHERE member_id=? AND group_area=?
        AND is_withdrawn=0 AND is_test=0 AND is_countable=1 ORDER BY id""",(member_id,group_area)).fetchall()
    jobs = conn.execute("""SELECT * FROM v102_status_bridge_jobs WHERE member_id=? AND group_area=?
        AND job_status!='applied' ORDER BY id""",(member_id,group_area)).fetchall()
    current_cycle = _active_cycle(conn,member_id,group_area)
    if recovery_mode == "保留周期" and current_cycle is not None:
        affected_current = conn.execute("""SELECT 1 FROM violation_records r WHERE r.member_id=? AND r.group_area=?
            AND r.violation_time>=? AND (r.id=(SELECT source_record_id FROM v102_policy_events WHERE id=?)
            OR (r.is_withdrawn=0 AND r.is_test=0 AND r.is_countable=1 AND r.action LIKE '%禁言%'
                AND NOT EXISTS (SELECT 1 FROM v102_policy_events e WHERE e.source_record_id=r.id
                    AND e.event_type IN ('mute_recorded','mute_duration_unknown')))) LIMIT 1""",
            (member_id,group_area,current_cycle["start_at"],pending[0]["caused_by_event_id"])).fetchone()
        if affected_current is not None or jobs:
            raise ValueError("当前周期内也有待复核的新证据或状态变更，不能直接沿用旧评价；请选择重新计时")
    for record in records:
        _validate_effective_time(_time_text(record["violation_time"]),at)
        if classify_severity(record["action"]) is Severity.UNKNOWN:
            raise ValueError("仍有无法确定禁言时长的记录，请先纠正后复核")
    for job in jobs:
        _validate_effective_time(_time_text(job["effective_at"]),at)
    event_id,_ = _insert_event(conn,member_id=member_id,group_area=group_area,
        event_type="policy_review_resolved",effective_time=at,event_priority=60,source_sequence=pending_action_id,
        ingest_time=at,idempotency_key=idempotency_key,caused_by_event_id=int(pending[0]["caused_by_event_id"]),
        payload={"reason":reason,"actor_qq":actor_qq,"pending_action_id":pending_action_id,"recovery_mode":recovery_mode})
    # Inputs entered while this member was isolated are covered by this explicit review.
    for record in records:
        severity = classify_severity(record["action"])
        if severity is Severity.NONE or record["id"] <= int(policy["baseline_record_watermark"] or 0):
            continue
        _insert_event(conn,member_id=member_id,group_area=group_area,event_type="mute_recorded",
            effective_time=record["violation_time"],event_priority=10,source_sequence=int(record["id"]),
            ingest_time=at,source_record_id=int(record["id"]),idempotency_key=f"record:{record['id']}:applied",
            payload={"severity":severity.value,"mute_seconds":parse_mute_seconds(record["action"]),
                     "accepted_by_review":event_id})
    for job in jobs:
        status_event,_ = _insert_event(conn,member_id=member_id,group_area=group_area,event_type="status_changed",
            effective_time=job["effective_at"],event_priority=30,source_sequence=0,ingest_time=at,
            idempotency_key=job["idempotency_key"],payload={"status":job["target_status"],"accepted_by_review":event_id})
        conn.execute("""UPDATE v102_status_bridge_jobs SET job_status='applied',applied_event_id=?,
            last_error=NULL,updated_at=? WHERE id=?""",(status_event,at,job["id"]))
    _resolve_pending_actions(conn,member_id,group_area,decision_event_id=event_id,at=at,action_types=("replay_review",))
    business = sync_count_state(conn,member_id,group_area,updated_at=at)
    cycle = _active_cycle(conn,member_id,group_area)
    if business["status"] in TERMINAL_STATUSES:
        _cancel_active_cycle(conn,member_id,group_area,at=at,reason="review_terminal_status")
        _set_no_cycle(conn,member_id,group_area,"terminal_status",at,preserve_tag=True)
    elif recovery_mode == "重新计时" or cycle is None:
        current=max(0,int(business["total_count"] or 0)-int(business["deduct_count"] or 0))
        cycle_type = ("final_warning" if business["status"] == "最后警告" else
            "stop" if cycle is not None and cycle["cycle_type"] == "stop" else
            "slow" if current>=3 or business["status"] == "已质询" else "normal")
        sequence = int(cycle["fixed_sequence"] or 0) if cycle is not None else 0
        _cancel_active_cycle(conn,member_id,group_area,at=at,reason="review_restarted")
        _start_cycle(conn,member_id=member_id,group_area=group_area,cycle_type=cycle_type,start_at=at,
            caused_by_event_id=event_id,fixed_sequence=sequence)
    else:
        settle_due_cycles(conn,at,member_id=member_id,group_area=group_area)
    conn.execute("""UPDATE v102_policy_state SET last_processed_event_id=?,last_reason='review_resolved',
        state_version=state_version+1,updated_at=? WHERE member_id=? AND group_area=?""",(event_id,at,member_id,group_area))
    cycle = _active_cycle(conn,member_id,group_area)
    snapshot={"reason":reason,"actor_qq":actor_qq,"pending_action_id":pending_action_id,"recovery_mode":recovery_mode,
        "business":dict(_business_state(conn,member_id,group_area)),
        "policy":dict(_ensure_policy_state(conn,member_id,group_area,at)),
        "cycle":dict(cycle) if cycle is not None else None,
        "pending_actions":[dict(row) for row in conn.execute("SELECT * FROM v102_pending_actions WHERE member_id=? AND group_area=? AND status='pending'",(member_id,group_area))],
        "event_watermark":int(conn.execute("SELECT COALESCE(MAX(id),0) FROM v102_policy_events").fetchone()[0]),
        "record_watermark":int(conn.execute("SELECT COALESCE(MAX(id),0) FROM violation_records").fetchone()[0])}
    conn.execute("UPDATE v102_policy_events SET payload_json=? WHERE id=?",(_json(snapshot),event_id))
    return PolicyOutcome(event_id,True)


def _restore_review_checkpoint(conn, checkpoint) -> dict:
    snapshot=json.loads(checkpoint["payload_json"])
    member_id,area=int(checkpoint["member_id"]),checkpoint["group_area"]
    for table,key in (("member_group_states","business"),("v102_policy_state","policy"),("v102_policy_cycles","cycle")):
        values=snapshot[key]
        if values is None:
            continue
        columns={str(row[1]) for row in conn.execute(f"PRAGMA table_info({table})")}
        fields=[name for name in values if name in columns and name not in {"id","member_id","group_area"}]
        if table == "v102_policy_cycles":
            where="id=? AND member_id=? AND group_area=?";parameters=[values["id"],member_id,area]
        else:
            where="member_id=? AND group_area=?";parameters=[member_id,area]
        assignments=','.join(f'"{field}"=?' for field in fields)
        conn.execute(f"UPDATE {table} SET {assignments} WHERE {where}",[values[field] for field in fields]+parameters)
    for pending in snapshot.get("pending_actions", []):
        conn.execute("""UPDATE v102_pending_actions SET status='pending',caused_by_event_id=?,
            decision_event_id=NULL,updated_at=? WHERE id=? AND member_id=? AND group_area=?""",
            (checkpoint["id"],checkpoint["effective_time"],pending["id"],member_id,area))
    return snapshot


def _set_event_evaluation_owner(
    conn: sqlite3.Connection,
    event_id: int,
    *,
    owner_cycle_id: int | None = None,
    pending_after_cycle_id: int | None = None,
) -> None:
    row = conn.execute(
        "SELECT payload_json FROM v102_policy_events WHERE id=?", (event_id,)
    ).fetchone()
    if row is None:
        raise LookupError(f"missing policy event: {event_id}")
    payload = json.loads(row["payload_json"] or "{}")
    if owner_cycle_id is not None:
        payload["evaluation_owner_cycle_id"] = int(owner_cycle_id)
        payload.pop("evaluation_pending_after_cycle_id", None)
    elif pending_after_cycle_id is not None:
        payload["evaluation_pending_after_cycle_id"] = int(
            pending_after_cycle_id
        )
        payload.pop("evaluation_owner_cycle_id", None)
    conn.execute(
        "UPDATE v102_policy_events SET payload_json=? WHERE id=?",
        (_json(payload), event_id),
    )


def _transfer_waiting_stop_evaluations(
    conn: sqlite3.Connection,
    *,
    from_cycle: sqlite3.Row,
    to_cycle: sqlite3.Row,
    at: str,
) -> None:
    rows = conn.execute(
        """
        SELECT id, effective_time, payload_json
        FROM v102_policy_events
        WHERE member_id=? AND group_area=? AND is_effective=1
          AND event_type='mute_recorded' AND replay_generation=?
        ORDER BY effective_time, event_priority, source_sequence, id
        """,
        (
            from_cycle["member_id"],
            from_cycle["group_area"],
            int(from_cycle["replay_generation"] or 0),
        ),
    ).fetchall()
    light_count = 0
    severe_count = 0
    for row in rows:
        payload = json.loads(row["payload_json"] or "{}")
        if int(payload.get("evaluation_pending_after_cycle_id") or 0) != int(
            from_cycle["id"]
        ):
            continue
        if row["effective_time"] <= to_cycle["due_at"]:
            if payload.get("severity") == Severity.LIGHT.value:
                light_count += 1
            elif payload.get("severity") == Severity.SEVERE.value:
                severe_count += 1
            _set_event_evaluation_owner(
                conn, int(row["id"]), owner_cycle_id=int(to_cycle["id"])
            )
        else:
            _set_event_evaluation_owner(
                conn,
                int(row["id"]),
                pending_after_cycle_id=int(to_cycle["id"]),
            )
    if light_count or severe_count:
        conn.execute(
            """
            UPDATE v102_policy_cycles
            SET light_count=light_count+?, severe_count=severe_count+?,
                updated_at=? WHERE id=?
            """,
            (light_count, severe_count, at, to_cycle["id"]),
        )


def _apply_old_backfill_count_effect(
    conn: sqlite3.Connection,
    *,
    member_id: int,
    group_area: str,
    event_id: int,
    at: str,
) -> PolicyOutcome:
    business = _business_state(conn, member_id, group_area)
    policy = _ensure_policy_state(conn, member_id, group_area, at)
    current = max(
        0,
        int(business["total_count"] or 0) - int(business["deduct_count"] or 0),
    )
    cycle = _active_cycle(conn, member_id, group_area)
    if business["status"] in TERMINAL_STATUSES:
        _set_no_cycle(
            conn, member_id, group_area, "terminal_status", at, preserve_tag=True
        )
        return PolicyOutcome(event_id, True)
    if int(policy["v102_operation_count"] or 0) >= 5:
        _set_no_cycle(conn, member_id, group_area, "operation_limit", at)
        return PolicyOutcome(event_id, True)
    if cycle is None:
        cycle_type = "slow" if current >= 3 or business["status"] == "已质询" else "normal"
        _start_cycle(
            conn,
            member_id=member_id,
            group_area=group_area,
            cycle_type=cycle_type,
            start_at=at,
            caused_by_event_id=event_id,
        )
    elif cycle["cycle_type"] == "normal" and current >= 3:
        _enter_slow(conn, cycle, caused_by_event_id=event_id, at=at)
    conn.execute(
        """
        UPDATE v102_policy_state
        SET last_processed_event_id=?, last_reason='old_backfill_count_only',
            state_version=state_version+1, updated_at=?
        WHERE member_id=? AND group_area=?
        """,
        (event_id, at, member_id, group_area),
    )
    return PolicyOutcome(event_id, True)


def process_violation_record(
    conn: sqlite3.Connection,
    source_record_id: int,
    *,
    ingest_time: str | datetime,
    replay_generation: int = 0,
    caused_by_event_id: int | None = None,
) -> PolicyOutcome:
    record = conn.execute(
        "SELECT * FROM violation_records WHERE id=?", (source_record_id,)
    ).fetchone()
    if record is None:
        raise LookupError(f"missing violation record: {source_record_id}")
    if (
        int(record["is_withdrawn"] or 0)
        or int(record["is_test"] or 0)
        or not int(record["is_countable"] or 0)
    ):
        return PolicyOutcome(None, False)

    severity = classify_severity(record["action"])
    if severity is Severity.NONE:
        return PolicyOutcome(None, False)

    if replay_generation == 0 and policy_scope_under_review(
        conn, int(record["member_id"]), record["group_area"]
    ):
        return PolicyOutcome(None, False)

    effective_at = _time_text(record["violation_time"])
    ingested_at = _time_text(ingest_time)
    _validate_effective_time(effective_at, ingested_at)
    event_id, created = _insert_event(
        conn,
        member_id=int(record["member_id"]),
        group_area=record["group_area"],
        event_type=(
            "mute_duration_unknown"
            if severity is Severity.UNKNOWN
            else "mute_recorded"
        ),
        effective_time=effective_at,
        event_priority=10,
        source_sequence=int(record["id"]),
        ingest_time=ingested_at,
        source_record_id=int(record["id"]),
        idempotency_key=(
            f"record:{record['id']}:applied"
            if replay_generation == 0
            else f"record:{record['id']}:applied:replay:{replay_generation}"
        ),
        caused_by_event_id=caused_by_event_id,
        replay_generation=replay_generation,
        payload={
            "severity": severity.value,
            "mute_seconds": parse_mute_seconds(record["action"]),
        },
    )
    if not created:
        return PolicyOutcome(event_id, False)

    member_id = int(record["member_id"])
    group_area = record["group_area"]
    _ensure_policy_state(conn, member_id, group_area, ingested_at)
    checkpoint = _latest_review_checkpoint(conn, member_id, group_area) if replay_generation == 0 else None
    if checkpoint is not None and effective_at < checkpoint["effective_time"]:
        return record_policy_review(conn, member_id=member_id, group_area=group_area,
            source_record_id=source_record_id,key=f"event:{event_id}:replay-review",at=ingested_at,
            reason=f"新证据早于已确认复核事件 {checkpoint['id']}，保留人工决定，需重新复核",action_type="replay_review")
    settle_due_cycles(
        conn,
        _time_text(_time_value(effective_at) - timedelta(seconds=1)),
        member_id=member_id, group_area=group_area,
    )
    sync_count_state(
        conn,
        member_id,
        group_area,
        updated_at=ingested_at,
        through_time=effective_at if replay_generation else None,
        through_record_id=int(record["id"]) if replay_generation else None,
    )

    if severity is Severity.UNKNOWN:
        pending_id = _create_pending_action(
            conn,
            member_id=member_id,
            group_area=group_area,
            action_type="duration_review",
            reason="禁言时长无法确定，请撤回后按明确时长重新记录",
            caused_by_event_id=event_id,
            at=ingested_at,
        )
        return PolicyOutcome(event_id, True, pending_id)

    if replay_generation:
        active_cycle = _active_cycle(conn, member_id, group_area)
        if active_cycle is not None and effective_at < active_cycle["start_at"]:
            return _apply_old_backfill_count_effect(
                conn,
                member_id=member_id,
                group_area=group_area,
                event_id=event_id,
                at=ingested_at,
            )

    if replay_generation == 0 and effective_at < ingested_at and _has_later_effective_event(
        conn, member_id=member_id, group_area=group_area,
        effective_at=effective_at, priority=10, source_sequence=source_record_id,
        event_id=event_id,
    ):
        recent_cycles = conn.execute(
            """
            SELECT * FROM v102_policy_cycles
            WHERE member_id=? AND group_area=?
              AND status!='cancelled'
            ORDER BY start_at DESC, id DESC LIMIT 2
            """,
            (member_id, group_area),
        ).fetchall()
        if any(effective_at >= cycle["start_at"] for cycle in recent_cycles):
            replay_member_group(
                conn,
                member_id,
                group_area,
                trigger_event_id=event_id,
                as_of=ingested_at,
            )
            return PolicyOutcome(event_id, True)
        if recent_cycles:
            return _apply_old_backfill_count_effect(
                conn,
                member_id=member_id,
                group_area=group_area,
                event_id=event_id,
                at=ingested_at,
            )

    business = _business_state(conn, member_id, group_area)
    policy = _ensure_policy_state(conn, member_id, group_area, ingested_at)
    current = max(
        0,
        int(business["total_count"] or 0) - int(business["deduct_count"] or 0),
    )
    cycle = _active_cycle(conn, member_id, group_area)
    if business["status"] in TERMINAL_STATUSES:
        _set_no_cycle(
            conn,
            member_id,
            group_area,
            "terminal_status",
            ingested_at,
            preserve_tag=True,
        )
        return PolicyOutcome(event_id, True)
    if cycle is None and business["status"] == "最后警告":
        final_warning_start = (
            business["last_final_warning_time"]
            or policy["baseline_last_final_warning_time"]
            or effective_at
        )
        _start_cycle(
            conn,
            member_id=member_id,
            group_area=group_area,
            cycle_type="final_warning",
            start_at=str(final_warning_start),
            caused_by_event_id=event_id,
            replay_generation=replay_generation,
        )
        cycle = _active_cycle(conn, member_id, group_area)
    if cycle is not None and cycle["cycle_type"] in {"stop", "final_warning"}:
        light_delta = 1 if severity is Severity.LIGHT else 0
        severe_delta = 1 if severity is Severity.SEVERE else 0
        if (
            cycle["cycle_type"] == "stop"
            and cycle["status"] == "pending_decision"
            and (
                int(cycle["light_count"] or 0) > 1
                or int(cycle["severe_count"] or 0) > 0
            )
        ):
            _set_event_evaluation_owner(
                conn,
                event_id,
                pending_after_cycle_id=int(cycle["id"]),
            )
            return PolicyOutcome(event_id, True)
        conn.execute(
            """
            UPDATE v102_policy_cycles
            SET light_count=light_count+?, severe_count=severe_count+?,
                updated_at=? WHERE id=?
            """,
            (light_delta, severe_delta, ingested_at, cycle["id"]),
        )
        _set_event_evaluation_owner(
            conn, event_id, owner_cycle_id=int(cycle["id"])
        )
        if cycle["cycle_type"] == "final_warning":
            pending_id = _create_pending_action(
                conn,
                member_id=member_id,
                group_area=group_area,
                action_type="remove_member",
                reason="最后警告期间新增禁言，请管理判断是否移出",
                caused_by_event_id=event_id,
                at=ingested_at,
            )
            conn.execute(
                """
                UPDATE v102_policy_cycles
                SET status='pending_decision', updated_at=? WHERE id=?
                """,
                (ingested_at, cycle["id"]),
            )
            return PolicyOutcome(event_id, True, pending_id)
        return PolicyOutcome(event_id, True)
    if int(policy["v102_operation_count"] or 0) >= 5:
        _set_no_cycle(conn, member_id, group_area, "operation_limit", ingested_at)
        return PolicyOutcome(event_id, True)

    if cycle is None:
        cycle_type = "slow" if current >= 3 or business["status"] == "已质询" else "normal"
        _start_cycle(
            conn,
            member_id=member_id,
            group_area=group_area,
            cycle_type=cycle_type,
            start_at=effective_at,
            caused_by_event_id=event_id,
            replay_generation=replay_generation,
        )
        pending_id = None
        if severity is Severity.SEVERE:
            pending_id = _create_pending_action(
                conn,
                member_id=member_id,
                group_area=group_area,
                action_type="stop_suggestion",
                reason="出现严重违规，建议人工减停",
                caused_by_event_id=event_id,
                at=ingested_at,
            )
        return PolicyOutcome(event_id, True, pending_id)

    if cycle["cycle_type"] not in {"normal", "slow"}:
        return PolicyOutcome(event_id, True)

    light_delta = 1 if severity is Severity.LIGHT else 0
    severe_delta = 1 if severity is Severity.SEVERE else 0
    normal_light_count = int(cycle["normal_light_count"] or 0) + light_delta
    slow_light_count = int(cycle["slow_light_count"] or 0)
    if cycle["cycle_type"] == "slow":
        slow_light_count += light_delta
    conn.execute(
        """
        UPDATE v102_policy_cycles
        SET light_count=light_count+?, normal_light_count=?,
            slow_light_count=?, severe_count=severe_count+?, updated_at=?
        WHERE id=?
        """,
        (
            light_delta,
            normal_light_count,
            slow_light_count,
            severe_delta,
            ingested_at,
            cycle["id"],
        ),
    )
    cycle = conn.execute(
        "SELECT * FROM v102_policy_cycles WHERE id=?", (cycle["id"],)
    ).fetchone()

    if cycle["cycle_type"] == "normal" and current >= 3:
        cycle = _enter_slow(
            conn, cycle, caused_by_event_id=event_id, at=effective_at
        )

    if (
        cycle["cycle_type"] == "slow"
        and int(cycle["slow_light_count"] or 0) == 2
        and not int(cycle["slow_extended"] or 0)
    ):
        extended_due = _time_text(
            _time_value(cycle["due_at"]) + timedelta(days=7)
        )
        conn.execute(
            """
            UPDATE v102_policy_cycles
            SET due_at=?, slow_extended=1, updated_at=? WHERE id=?
            """,
            (extended_due, ingested_at, cycle["id"]),
        )
        _insert_event(
            conn,
            member_id=member_id,
            group_area=group_area,
            event_type="slow_extended",
            effective_time=effective_at,
            event_priority=40,
            source_sequence=event_id,
            ingest_time=ingested_at,
            caused_by_event_id=event_id,
            replay_generation=replay_generation,
            idempotency_key=f"event:{event_id}:cycle:{cycle['id']}:extended",
            payload={"cycle_id": int(cycle["id"]), "due_at": extended_due},
        )
        cycle = conn.execute(
            "SELECT * FROM v102_policy_cycles WHERE id=?", (cycle["id"],)
        ).fetchone()

    should_suggest = (
        severity is Severity.SEVERE
        or int(cycle["normal_light_count"] or 0) >= 3
        or int(cycle["slow_light_count"] or 0) >= 3
    )
    pending_id = None
    if should_suggest:
        pending_id = _create_pending_action(
            conn,
            member_id=member_id,
            group_area=group_area,
            action_type="stop_suggestion",
            reason=(
                "出现严重违规，建议人工减停"
                if severity is Severity.SEVERE
                else "周期内轻度违规达到建议减停条件"
            ),
            caused_by_event_id=event_id,
            at=ingested_at,
        )
    return PolicyOutcome(event_id, True, pending_id)


def _cancel_active_cycle(
    conn: sqlite3.Connection,
    member_id: int,
    group_area: str,
    *,
    at: str,
    reason: str,
) -> None:
    cycle = _active_cycle(conn, member_id, group_area)
    if cycle:
        conn.execute(
            """
            UPDATE v102_policy_cycles
            SET status='cancelled', closed_reason=?, updated_at=? WHERE id=?
            """,
            (reason, at, cycle["id"]),
        )
    conn.execute(
        """
        UPDATE v102_policy_state
        SET active_cycle_id=NULL, state_version=state_version+1, updated_at=?
        WHERE member_id=? AND group_area=?
        """,
        (at, member_id, group_area),
    )


def _resolve_pending_actions(
    conn: sqlite3.Connection,
    member_id: int,
    group_area: str,
    *,
    decision_event_id: int,
    at: str,
    action_types: tuple[str, ...] | None = None,
) -> None:
    parameters: list[object] = [decision_event_id, at, member_id, group_area]
    condition = " AND action_type NOT IN ('input_review','replay_review')"
    if action_types:
        placeholders = ",".join("?" for _ in action_types)
        condition = f" AND action_type IN ({placeholders})"
        parameters.extend(action_types)
    conn.execute(
        f"""
        UPDATE v102_pending_actions
        SET status='resolved', decision_event_id=?, updated_at=?
        WHERE member_id=? AND group_area=? AND status='pending'{condition}
        """,
        parameters,
    )
    remaining = conn.execute(
        """
        SELECT action_type FROM v102_pending_actions
        WHERE member_id=? AND group_area=? AND status='pending'
        ORDER BY id LIMIT 1
        """,
        (member_id, group_area),
    ).fetchone()
    conn.execute(
        """
        UPDATE v102_policy_state
        SET pending_action_type=?, updated_at=?
        WHERE member_id=? AND group_area=?
        """,
        (
            remaining["action_type"] if remaining else None,
            at,
            member_id,
            group_area,
        ),
    )


def _apply_reduction(
    conn: sqlite3.Connection,
    *,
    member_id: int,
    group_area: str,
    requested_amount: int,
    at: str,
    through_time: str | None = None,
) -> int:
    sync_count_state(
        conn,
        member_id,
        group_area,
        updated_at=at,
        through_time=through_time,
    )
    business = _business_state(conn, member_id, group_area)
    policy = _ensure_policy_state(conn, member_id, group_area, at)
    current = max(
        0,
        int(business["total_count"] or 0) - int(business["deduct_count"] or 0),
    )
    applied = (
        requested_amount
        if current >= requested_amount
        and int(policy["v102_operation_count"] or 0) < 5
        else 0
    )
    if applied:
        conn.execute(
            """
            UPDATE member_group_states
            SET deduct_count=deduct_count+?, updated_at=?
            WHERE member_id=? AND group_area=?
            """,
            (applied, at, member_id, group_area),
        )
        conn.execute(
            """
            UPDATE v102_policy_state
            SET v102_operation_count=v102_operation_count+1,
                state_version=state_version+1, updated_at=?
            WHERE member_id=? AND group_area=?
            """,
            (at, member_id, group_area),
        )
    sync_count_state(
        conn,
        member_id,
        group_area,
        updated_at=at,
        through_time=through_time,
    )
    return applied


def start_manual_stop(
    conn: sqlite3.Connection,
    *,
    member_id: int,
    group_area: str,
    effective_at: str | datetime,
    reason: str,
    idempotency_key: str,
    caused_by_event_id: int | None = None,
    replay_generation: int = 0,
) -> PolicyOutcome:
    if replay_generation == 0 and policy_scope_under_review(conn, member_id, group_area):
        raise ValueError("该成员存在减数复核待办，请先复核冲突或撤回对应错误记录")
    at = _time_text(effective_at)
    if not reason.strip():
        raise ValueError("减停事由不能为空")
    _ensure_policy_state(conn, member_id, group_area, at)
    business = _business_state(conn, member_id, group_area)
    if business["status"] in TERMINAL_STATUSES or business["status"] == "最后警告":
        raise ValueError("当前成员状态不允许普通减停")
    active = _active_cycle(conn, member_id, group_area)
    if active and active["cycle_type"] in {"stop", "final_warning"}:
        raise ValueError("当前成员已处于减停")
    event_id, created = _insert_event(
        conn,
        member_id=member_id,
        group_area=group_area,
        event_type="manual_stop_started",
        effective_time=at,
        event_priority=30,
        source_sequence=0,
        ingest_time=at,
        idempotency_key=idempotency_key,
        caused_by_event_id=caused_by_event_id,
        replay_generation=replay_generation,
        payload={"reason": reason},
    )
    if not created:
        return PolicyOutcome(event_id, False)
    _cancel_active_cycle(
        conn, member_id, group_area, at=at, reason="manual_stop_started"
    )
    _resolve_pending_actions(
        conn,
        member_id,
        group_area,
        decision_event_id=event_id,
        at=at,
        action_types=("stop_suggestion",),
    )
    _start_cycle(
        conn,
        member_id=member_id,
        group_area=group_area,
        cycle_type="stop",
        start_at=at,
        caused_by_event_id=event_id,
        fixed_sequence=1,
        replay_generation=replay_generation,
    )
    conn.execute(
        """
        UPDATE v102_policy_state
        SET last_reason=?, last_processed_event_id=?, updated_at=?
        WHERE member_id=? AND group_area=?
        """,
        (reason, event_id, at, member_id, group_area),
    )
    return PolicyOutcome(event_id, True)


def process_status_change(
    conn: sqlite3.Connection,
    *,
    member_id: int,
    group_area: str,
    status: str,
    effective_at: str | datetime,
    idempotency_key: str,
    ingest_time: str | datetime | None = None,
    caused_by_event_id: int | None = None,
    replay_generation: int = 0,
) -> PolicyOutcome:
    if replay_generation == 0 and conn.execute(
        """SELECT 1 FROM v102_pending_actions WHERE member_id=? AND group_area=?
        AND action_type='replay_review' AND status='pending' LIMIT 1""", (member_id, group_area)
    ).fetchone():
        return PolicyOutcome(None, False)
    at = _time_text(effective_at)
    ingested_at = _time_text(ingest_time or effective_at)
    _validate_effective_time(at, ingested_at)
    _ensure_policy_state(conn, member_id, group_area, ingested_at)
    previous_business_status = str(_business_state(conn, member_id, group_area)["status"])
    previous_event = conn.execute(
        """SELECT payload_json FROM v102_policy_events
        WHERE member_id=? AND group_area=? AND event_type='status_changed'
          AND is_effective=1 AND effective_time<=?
        ORDER BY effective_time DESC, id DESC LIMIT 1""",
        (member_id, group_area, at),
    ).fetchone()
    previous_status = (str(json.loads(previous_event["payload_json"])["status"])
        if previous_event is not None else previous_business_status)
    event_id, created = _insert_event(
        conn,
        member_id=member_id,
        group_area=group_area,
        event_type="status_changed",
        effective_time=at,
        event_priority=30,
        source_sequence=0,
        ingest_time=ingested_at,
        idempotency_key=idempotency_key,
        caused_by_event_id=caused_by_event_id,
        replay_generation=replay_generation,
        payload={"status": status},
    )
    if not created:
        return PolicyOutcome(event_id, False)
    checkpoint = _latest_review_checkpoint(conn, member_id, group_area) if replay_generation == 0 else None
    if checkpoint is not None and at < checkpoint["effective_time"]:
        return record_policy_review(conn,member_id=member_id,group_area=group_area,source_record_id=None,
            key=f"event:{event_id}:replay-review",at=ingested_at,
            reason=f"状态变更早于已确认复核事件 {checkpoint['id']}，需重新复核",action_type="replay_review")
    if replay_generation == 0 and at < ingested_at and _has_later_effective_event(
        conn, member_id=member_id, group_area=group_area,
        effective_at=at, priority=30, source_sequence=0, event_id=event_id,
    ):
        replay_member_group(
            conn,
            member_id,
            group_area,
            trigger_event_id=event_id,
            as_of=ingested_at,
        )
        return PolicyOutcome(event_id, True)
    settle_due_cycles(
        conn,
        _time_text(_time_value(at) - timedelta(seconds=1)),
        member_id=member_id, group_area=group_area,
    )
    conn.execute(
        """
        UPDATE member_group_states
        SET status=?, updated_at=? WHERE member_id=? AND group_area=?
        """,
        (status, at, member_id, group_area),
    )
    if status in TERMINAL_STATUSES:
        _cancel_active_cycle(
            conn, member_id, group_area, at=at, reason="terminal_status"
        )
        _resolve_pending_actions(
            conn,
            member_id,
            group_area,
            decision_event_id=event_id,
            at=at,
        )
        _set_no_cycle(
            conn, member_id, group_area, "terminal_status", at, preserve_tag=True
        )
    elif status == "最后警告":
        _cancel_active_cycle(
            conn, member_id, group_area, at=at, reason="final_warning_started"
        )
        _resolve_pending_actions(
            conn,
            member_id,
            group_area,
            decision_event_id=event_id,
            at=at,
            action_types=("stop_suggestion", "stop_decision"),
        )
        _start_cycle(
            conn,
            member_id=member_id,
            group_area=group_area,
            cycle_type="final_warning",
            start_at=at,
            caused_by_event_id=event_id,
            replay_generation=replay_generation,
        )
    elif status == "已质询":
        _cancel_active_cycle(
            conn, member_id, group_area, at=at, reason="consulted_status_started"
        )
        _start_cycle(
            conn,
            member_id=member_id,
            group_area=group_area,
            cycle_type="slow",
            start_at=at,
            caused_by_event_id=event_id,
            replay_generation=replay_generation,
        )
    elif status == "正常":
        policy = _ensure_policy_state(conn, member_id, group_area, at)
        cycle = _active_cycle(conn, member_id, group_area)
        recovering = (previous_status != "正常" or policy["no_cycle_reason"] == "terminal_status"
            or (cycle is not None and cycle["cycle_type"] == "final_warning"))
        if recovering and not policy_scope_under_review(conn, member_id, group_area):
            _cancel_active_cycle(conn, member_id, group_area, at=at, reason="normal_status_recovered")
            _resolve_pending_actions(conn, member_id, group_area,
                decision_event_id=event_id, at=at,
                action_types=("stop_suggestion", "stop_decision", "remove_member"))
            business = _business_state(conn, member_id, group_area)
            current = max(0, int(business["total_count"] or 0) - int(business["deduct_count"] or 0))
            _start_cycle(conn, member_id=member_id, group_area=group_area,
                cycle_type="slow" if current >= 3 else "normal", start_at=at,
                caused_by_event_id=event_id, replay_generation=replay_generation)
    conn.execute(
        """
        UPDATE v102_policy_state
        SET last_processed_event_id=?, updated_at=?
        WHERE member_id=? AND group_area=?
        """,
        (event_id, at, member_id, group_area),
    )
    return PolicyOutcome(event_id, True)


def clear_manual_stop(
    conn: sqlite3.Connection,
    *,
    member_id: int,
    group_area: str,
    effective_at: str | datetime,
    reason: str,
    idempotency_key: str,
    caused_by_event_id: int | None = None,
    replay_generation: int = 0,
) -> PolicyOutcome:
    if replay_generation == 0 and policy_scope_under_review(conn, member_id, group_area):
        raise ValueError("该成员存在减数复核待办，请先复核冲突或撤回对应错误记录")
    at = _time_text(effective_at)
    if not reason.strip():
        raise ValueError("清除减停事由不能为空")
    cycle = _active_cycle(conn, member_id, group_area)
    if (
        cycle is None
        or cycle["cycle_type"] != "stop"
        or cycle["status"] != "pending_decision"
    ):
        raise ValueError("当前没有可清除的到期普通减停")
    good = int(cycle["light_count"] or 0) <= 1 and int(
        cycle["severe_count"] or 0
    ) == 0
    if not good:
        raise ValueError("本减停周期评价不良，不能清除减停")
    event_id, created = _insert_event(
        conn,
        member_id=member_id,
        group_area=group_area,
        event_type="manual_stop_cleared",
        effective_time=at,
        event_priority=30,
        source_sequence=0,
        ingest_time=at,
        idempotency_key=idempotency_key,
        caused_by_event_id=caused_by_event_id,
        replay_generation=replay_generation,
        payload={"reason": reason, "cycle_id": int(cycle["id"])},
    )
    if not created:
        return PolicyOutcome(event_id, False)
    applied = _apply_reduction(
        conn,
        member_id=member_id,
        group_area=group_area,
        requested_amount=1,
        at=at,
    )
    conn.execute(
        """
        UPDATE v102_policy_cycles
        SET status='closed', decision_event_id=?, closed_reason='manual_clear',
            updated_at=? WHERE id=?
        """,
        (event_id, at, cycle["id"]),
    )
    _resolve_pending_actions(
        conn,
        member_id,
        group_area,
        decision_event_id=event_id,
        at=at,
        action_types=("stop_decision",),
    )
    conn.execute(
        """
        UPDATE v102_policy_state
        SET policy_tag='none', active_cycle_id=NULL, last_reason=?,
            last_processed_event_id=?, state_version=state_version+1,
            updated_at=? WHERE member_id=? AND group_area=?
        """,
        (reason, event_id, at, member_id, group_area),
    )
    business = _business_state(conn, member_id, group_area)
    policy = _ensure_policy_state(conn, member_id, group_area, at)
    current = max(
        0,
        int(business["total_count"] or 0) - int(business["deduct_count"] or 0),
    )
    if current <= 0:
        _set_no_cycle(conn, member_id, group_area, "zero_count", at)
    elif int(policy["v102_operation_count"] or 0) >= 5:
        _set_no_cycle(conn, member_id, group_area, "operation_limit", at)
    else:
        _start_cycle(
            conn,
            member_id=member_id,
            group_area=group_area,
            cycle_type="normal",
            start_at=at,
            caused_by_event_id=event_id,
            replay_generation=replay_generation,
        )
    return PolicyOutcome(
        event_id,
        True,
        None,
    )


def renew_manual_stop(
    conn: sqlite3.Connection,
    *,
    member_id: int,
    group_area: str,
    effective_at: str | datetime,
    reason: str,
    idempotency_key: str,
    caused_by_event_id: int | None = None,
    replay_generation: int = 0,
) -> PolicyOutcome:
    if replay_generation == 0 and policy_scope_under_review(conn, member_id, group_area):
        raise ValueError("该成员存在减数复核待办，请先复核冲突或撤回对应错误记录")
    at = _time_text(effective_at)
    if not reason.strip():
        raise ValueError("续期减停事由不能为空")
    cycle = _active_cycle(conn, member_id, group_area)
    if (
        cycle is None
        or cycle["cycle_type"] != "stop"
        or cycle["status"] != "pending_decision"
    ):
        raise ValueError("当前没有可续期的到期普通减停")
    event_id, created = _insert_event(
        conn,
        member_id=member_id,
        group_area=group_area,
        event_type="manual_stop_renewed",
        effective_time=at,
        event_priority=30,
        source_sequence=0,
        ingest_time=at,
        idempotency_key=idempotency_key,
        caused_by_event_id=caused_by_event_id,
        replay_generation=replay_generation,
        payload={"reason": reason, "cycle_id": int(cycle["id"])},
    )
    if not created:
        return PolicyOutcome(event_id, False)
    conn.execute(
        """
        UPDATE v102_policy_cycles
        SET status='closed', decision_event_id=?, closed_reason='renewed',
            updated_at=? WHERE id=?
        """,
        (event_id, at, cycle["id"]),
    )
    _resolve_pending_actions(
        conn,
        member_id,
        group_area,
        decision_event_id=event_id,
        at=at,
        action_types=("stop_decision",),
    )
    conn.execute(
        """
        UPDATE v102_policy_state
        SET active_cycle_id=NULL, updated_at=?
        WHERE member_id=? AND group_area=?
        """,
        (at, member_id, group_area),
    )
    new_cycle_id = _start_cycle(
        conn,
        member_id=member_id,
        group_area=group_area,
        cycle_type="stop",
        start_at=cycle["due_at"],
        caused_by_event_id=event_id,
        fixed_sequence=int(cycle["fixed_sequence"] or 0) + 1,
        replay_generation=replay_generation,
    )
    if new_cycle_id is None:
        raise RuntimeError("续期减停未能创建下一固定周期")
    new_cycle = conn.execute(
        "SELECT * FROM v102_policy_cycles WHERE id=?", (new_cycle_id,)
    ).fetchone()
    _transfer_waiting_stop_evaluations(
        conn,
        from_cycle=cycle,
        to_cycle=new_cycle,
        at=at,
    )
    conn.execute(
        """
        UPDATE v102_policy_state
        SET last_reason=?, last_processed_event_id=?, updated_at=?
        WHERE member_id=? AND group_area=?
        """,
        (reason, event_id, at, member_id, group_area),
    )
    return PolicyOutcome(event_id, True)


def reject_stop_suggestion(
    conn: sqlite3.Connection,
    *,
    member_id: int,
    group_area: str,
    effective_at: str | datetime,
    reason: str,
    idempotency_key: str,
    caused_by_event_id: int | None = None,
    replay_generation: int = 0,
) -> PolicyOutcome:
    if replay_generation == 0 and policy_scope_under_review(conn, member_id, group_area):
        raise ValueError("该成员存在减数复核待办，请先复核冲突或撤回对应错误记录")
    at = _time_text(effective_at)
    if not reason.strip():
        raise ValueError("拒绝减停建议事由不能为空")
    cycle = _active_cycle(conn, member_id, group_area)
    pending = conn.execute(
        """
        SELECT * FROM v102_pending_actions
        WHERE member_id=? AND group_area=?
          AND action_type='stop_suggestion' AND status='pending'
        ORDER BY id LIMIT 1
        """,
        (member_id, group_area),
    ).fetchone()
    if not cycle or cycle["cycle_type"] not in {"normal", "slow"} or not pending:
        raise ValueError("当前没有可拒绝的减停建议")
    event_id, created = _insert_event(
        conn,
        member_id=member_id,
        group_area=group_area,
        event_type="stop_suggestion_rejected",
        effective_time=at,
        event_priority=30,
        source_sequence=int(pending["id"]),
        ingest_time=at,
        idempotency_key=idempotency_key,
        caused_by_event_id=caused_by_event_id or pending["caused_by_event_id"],
        replay_generation=replay_generation,
        payload={"reason": reason, "cycle_id": int(cycle["id"])},
    )
    if not created:
        return PolicyOutcome(event_id, False)
    _resolve_pending_actions(
        conn,
        member_id,
        group_area,
        decision_event_id=event_id,
        at=at,
        action_types=("stop_suggestion",),
    )
    conn.execute(
        """
        UPDATE v102_policy_cycles
        SET suggestion_rejected=1, decision_event_id=?, updated_at=?
        WHERE id=?
        """,
        (event_id, at, cycle["id"]),
    )
    if cycle["status"] == "pending_decision":
        if cycle["cycle_type"] == "normal":
            conn.execute(
                """
                UPDATE v102_policy_cycles
                SET status='closed', closed_reason='suggestion_rejected',
                    updated_at=? WHERE id=?
                """,
                (at, cycle["id"]),
            )
            conn.execute(
                """
                UPDATE v102_policy_state
                SET active_cycle_id=NULL, policy_tag='none', updated_at=?
                WHERE member_id=? AND group_area=?
                """,
                (at, member_id, group_area),
            )
            _start_cycle(
                conn,
                member_id=member_id,
                group_area=group_area,
                cycle_type="normal",
                start_at=cycle["due_at"],
                caused_by_event_id=event_id,
                replay_generation=replay_generation,
            )
        else:
            extended_due = _time_text(
                _time_value(cycle["due_at"]) + timedelta(days=7)
            )
            conn.execute(
                """
                UPDATE v102_policy_cycles
                SET status='active', due_at=?, light_count=0,
                    normal_light_count=0, slow_light_count=0,
                    severe_count=0, slow_extended=0,
                    suggestion_rejected=0, updated_at=?
                WHERE id=?
                """,
                (extended_due, at, cycle["id"]),
            )
    conn.execute(
        """
        UPDATE v102_policy_state
        SET last_reason=?, last_processed_event_id=?,
            state_version=state_version+1, updated_at=?
        WHERE member_id=? AND group_area=?
        """,
        (reason, event_id, at, member_id, group_area),
    )
    return PolicyOutcome(event_id, True)


def _mark_stop_due(
    conn: sqlite3.Connection, cycle: sqlite3.Row, as_of: str
) -> bool:
    event_id, created = _insert_event(
        conn,
        member_id=cycle["member_id"],
        group_area=cycle["group_area"],
        event_type="stop_due_for_decision",
        effective_time=cycle["due_at"],
        event_priority=50,
        source_sequence=int(cycle["id"]),
        ingest_time=as_of,
        idempotency_key=f"cycle:{cycle['id']}:{cycle['due_at']}:stop-due",
        payload={"cycle_id": int(cycle["id"])},
    )
    if not created:
        return False
    pending_id = _create_pending_action(
        conn,
        member_id=cycle["member_id"],
        group_area=cycle["group_area"],
        action_type="stop_decision",
        reason="普通减停周期已到期，请管理决定清除或续期",
        caused_by_event_id=event_id,
        at=as_of,
    )
    conn.execute(
        """
        UPDATE v102_pending_actions
        SET due_at=?, updated_at=? WHERE id=?
        """,
        (cycle["due_at"], as_of, pending_id),
    )
    conn.execute(
        """
        UPDATE v102_policy_cycles
        SET status='pending_decision', updated_at=? WHERE id=?
        """,
        (as_of, cycle["id"]),
    )
    return True


def _settle_final_warning(
    conn: sqlite3.Connection, cycle: sqlite3.Row, as_of: str
) -> bool:
    remove_pending = conn.execute(
        """
        SELECT id FROM v102_pending_actions
        WHERE member_id=? AND group_area=? AND action_type='remove_member'
          AND status='pending'
        LIMIT 1
        """,
        (cycle["member_id"], cycle["group_area"]),
    ).fetchone()
    if remove_pending:
        conn.execute(
            "UPDATE v102_policy_cycles SET status='pending_decision', updated_at=? WHERE id=?",
            (as_of, cycle["id"]),
        )
        return False

    member_id = int(cycle["member_id"])
    group_area = cycle["group_area"]
    sync_count_state(
        conn,
        member_id,
        group_area,
        updated_at=as_of,
        through_time=cycle["due_at"],
    )
    before = _business_state(conn, member_id, group_area)
    current_before = max(
        0,
        int(before["total_count"] or 0) - int(before["deduct_count"] or 0),
    )
    event_id, created = _insert_event(
        conn,
        member_id=member_id,
        group_area=group_area,
        event_type="final_warning_recovered",
        effective_time=cycle["due_at"],
        event_priority=50,
        source_sequence=int(cycle["id"]),
        ingest_time=as_of,
        idempotency_key=f"cycle:{cycle['id']}:{cycle['due_at']}:final-recovery",
        payload={
            "cycle_id": int(cycle["id"]),
            "requested_amount": 2,
            "current_before": current_before,
        },
    )
    if not created:
        return False
    conn.execute(
        """
        UPDATE member_group_states
        SET status='已质询', updated_at=?
        WHERE member_id=? AND group_area=?
        """,
        (as_of, member_id, group_area),
    )
    applied = _apply_reduction(
        conn,
        member_id=member_id,
        group_area=group_area,
        requested_amount=2,
        at=as_of,
        through_time=cycle["due_at"],
    )
    conn.execute(
        """
        UPDATE v102_policy_events
        SET payload_json=? WHERE id=?
        """,
        (
            _json(
                {
                    "cycle_id": int(cycle["id"]),
                    "requested_amount": 2,
                    "applied_amount": applied,
                    "current_before": current_before,
                }
            ),
            event_id,
        ),
    )
    conn.execute(
        """
        UPDATE v102_policy_cycles
        SET status='closed', settlement_event_id=?,
            closed_reason='final_warning_recovered', updated_at=?
        WHERE id=?
        """,
        (event_id, as_of, cycle["id"]),
    )
    conn.execute(
        """
        UPDATE v102_policy_state
        SET policy_tag='none', active_cycle_id=NULL,
            pending_action_type=NULL, last_processed_event_id=?,
            state_version=state_version+1, updated_at=?
        WHERE member_id=? AND group_area=?
        """,
        (event_id, as_of, member_id, group_area),
    )
    business = _business_state(conn, member_id, group_area)
    policy = _ensure_policy_state(conn, member_id, group_area, as_of)
    current_after = max(
        0,
        int(business["total_count"] or 0) - int(business["deduct_count"] or 0),
    )
    if current_after <= 0:
        _set_no_cycle(conn, member_id, group_area, "zero_count", as_of)
    elif int(policy["v102_operation_count"] or 0) >= 5:
        _set_no_cycle(conn, member_id, group_area, "operation_limit", as_of)
    else:
        _start_cycle(
            conn,
            member_id=member_id,
            group_area=group_area,
            cycle_type="normal",
            start_at=cycle["due_at"],
            caused_by_event_id=event_id,
            replay_generation=int(cycle["replay_generation"] or 0),
        )
    if applied == 0 and current_before < 2:
        _create_pending_action(
            conn,
            member_id=member_id,
            group_area=group_area,
            action_type="final_warning_recovery_review",
            reason="最后警告恢复时当前次数不足2，已按规则执行减数0，请核对数据",
            caused_by_event_id=event_id,
            at=as_of,
        )
    return True


def _settle_cycle(conn: sqlite3.Connection, cycle: sqlite3.Row, as_of: str) -> bool:
    pending = conn.execute(
        """
        SELECT id FROM v102_pending_actions
        WHERE member_id=? AND group_area=? AND status='pending'
          AND action_type IN ('stop_suggestion', 'duration_review')
        ORDER BY id LIMIT 1
        """,
        (cycle["member_id"], cycle["group_area"]),
    ).fetchone()
    if pending:
        conn.execute(
            """
            UPDATE v102_policy_cycles
            SET status='pending_decision', updated_at=? WHERE id=?
            """,
            (as_of, cycle["id"]),
        )
        return False

    if int(cycle["suggestion_rejected"] or 0):
        event_id, created = _insert_event(
            conn,
            member_id=cycle["member_id"],
            group_area=cycle["group_area"],
            event_type="rejected_cycle_closed",
            effective_time=cycle["due_at"],
            event_priority=50,
            source_sequence=int(cycle["id"]),
            ingest_time=as_of,
            idempotency_key=f"cycle:{cycle['id']}:{cycle['due_at']}:rejected-close",
            payload={"cycle_id": int(cycle["id"])},
            replay_generation=int(cycle["replay_generation"] or 0),
        )
        if not created:
            return False
        if cycle["cycle_type"] == "normal":
            conn.execute(
                """
                UPDATE v102_policy_cycles
                SET status='closed', settlement_event_id=?,
                    closed_reason='suggestion_rejected', updated_at=?
                WHERE id=?
                """,
                (event_id, as_of, cycle["id"]),
            )
            conn.execute(
                """
                UPDATE v102_policy_state
                SET policy_tag='none', active_cycle_id=NULL,
                    last_processed_event_id=?, updated_at=?
                WHERE member_id=? AND group_area=?
                """,
                (event_id, as_of, cycle["member_id"], cycle["group_area"]),
            )
            _start_cycle(
                conn,
                member_id=cycle["member_id"],
                group_area=cycle["group_area"],
                cycle_type="normal",
                start_at=cycle["due_at"],
                caused_by_event_id=event_id,
                replay_generation=int(cycle["replay_generation"] or 0),
            )
        else:
            extended_due = _time_text(
                _time_value(cycle["due_at"]) + timedelta(days=7)
            )
            conn.execute(
                """
                UPDATE v102_policy_cycles
                SET due_at=?, light_count=0, normal_light_count=0,
                    slow_light_count=0, severe_count=0, slow_extended=0,
                    suggestion_rejected=0, updated_at=? WHERE id=?
                """,
                (extended_due, as_of, cycle["id"]),
            )
        return True

    if cycle["cycle_type"] == "normal":
        good = int(cycle["light_count"] or 0) <= 1 and int(
            cycle["severe_count"] or 0
        ) == 0
    else:
        good = int(cycle["slow_light_count"] or 0) <= 2 and int(
            cycle["severe_count"] or 0
        ) == 0
    if not good:
        event_id, _ = _insert_event(
            conn,
            member_id=cycle["member_id"],
            group_area=cycle["group_area"],
            event_type="cycle_bad_at_due",
            effective_time=cycle["due_at"],
            event_priority=50,
            source_sequence=int(cycle["id"]),
            ingest_time=as_of,
            idempotency_key=f"cycle:{cycle['id']}:{cycle['due_at']}:bad",
            payload={"cycle_id": int(cycle["id"])},
        )
        _create_pending_action(
            conn,
            member_id=cycle["member_id"],
            group_area=cycle["group_area"],
            action_type="stop_suggestion",
            reason="周期到期评价不良，建议人工减停",
            caused_by_event_id=event_id,
            at=as_of,
        )
        conn.execute(
            "UPDATE v102_policy_cycles SET status='pending_decision', updated_at=? WHERE id=?",
            (as_of, cycle["id"]),
        )
        return False

    member_id = int(cycle["member_id"])
    group_area = cycle["group_area"]
    sync_count_state(
        conn,
        member_id,
        group_area,
        updated_at=as_of,
        through_time=cycle["due_at"],
    )
    business = _business_state(conn, member_id, group_area)
    policy = _ensure_policy_state(conn, member_id, group_area, as_of)
    current_before = max(
        0,
        int(business["total_count"] or 0) - int(business["deduct_count"] or 0),
    )
    applied = (
        1
        if current_before >= 1 and int(policy["v102_operation_count"] or 0) < 5
        else 0
    )
    settlement_id, created = _insert_event(
        conn,
        member_id=member_id,
        group_area=group_area,
        event_type="cycle_settled",
        effective_time=cycle["due_at"],
        event_priority=50,
        source_sequence=int(cycle["id"]),
        ingest_time=as_of,
        idempotency_key=(
            f"cycle:{cycle['id']}:{cycle['due_at']}:settlement:"
            f"{int(cycle['replay_generation'] or 0)}"
        ),
        payload={
            "cycle_id": int(cycle["id"]),
            "requested_amount": 1,
            "applied_amount": applied,
        },
        replay_generation=int(cycle["replay_generation"] or 0),
    )
    if not created:
        return False

    if applied:
        conn.execute(
            """
            UPDATE member_group_states
            SET deduct_count=deduct_count+?, updated_at=?
            WHERE member_id=? AND group_area=?
            """,
            (applied, as_of, member_id, group_area),
        )
        conn.execute(
            """
            UPDATE v102_policy_state
            SET v102_operation_count=v102_operation_count+1,
                state_version=state_version+1, updated_at=?
            WHERE member_id=? AND group_area=?
            """,
            (as_of, member_id, group_area),
        )
    conn.execute(
        """
        UPDATE v102_policy_cycles
        SET status='closed', settlement_event_id=?, closed_reason='settled',
            updated_at=? WHERE id=?
        """,
        (settlement_id, as_of, cycle["id"]),
    )
    conn.execute(
        """
        UPDATE v102_policy_state
        SET policy_tag='none', active_cycle_id=NULL,
            pending_action_type=NULL, last_processed_event_id=?,
            state_version=state_version+1, updated_at=?
        WHERE member_id=? AND group_area=?
        """,
        (settlement_id, as_of, member_id, group_area),
    )
    sync_count_state(
        conn,
        member_id,
        group_area,
        updated_at=as_of,
        through_time=cycle["due_at"],
    )
    business = _business_state(conn, member_id, group_area)
    policy = _ensure_policy_state(conn, member_id, group_area, as_of)
    current_after = max(
        0,
        int(business["total_count"] or 0) - int(business["deduct_count"] or 0),
    )
    if current_after <= 0:
        _set_no_cycle(conn, member_id, group_area, "zero_count", as_of)
    elif int(policy["v102_operation_count"] or 0) >= 5:
        _set_no_cycle(conn, member_id, group_area, "operation_limit", as_of)
    else:
        _start_cycle(
            conn,
            member_id=member_id,
            group_area=group_area,
            cycle_type="normal",
            start_at=cycle["due_at"],
            caused_by_event_id=settlement_id,
            replay_generation=int(cycle["replay_generation"] or 0),
        )
    return True


def settle_due_cycles(
    conn: sqlite3.Connection, as_of: str | datetime, *,
    member_id: int | None = None, group_area: str | None = None,
) -> int:
    if (member_id is None) != (group_area is None):
        raise ValueError("member_id and group_area must be supplied together")
    now = _time_text(as_of)
    scope_sql = "" if member_id is None else "AND member_id=? AND group_area=?"
    params = (now,) if member_id is None else (now, member_id, group_area)
    settled = 0
    while True:
        cycle = conn.execute(
            f"""
            SELECT * FROM v102_policy_cycles
            WHERE status='active'
              AND due_at<=?
              {scope_sql}
              AND NOT EXISTS (
                  SELECT 1 FROM v102_pending_actions p
                  WHERE p.member_id=v102_policy_cycles.member_id
                    AND p.group_area=v102_policy_cycles.group_area
                    AND p.status='pending'
                    AND p.action_type IN ('input_review','replay_review')
              )
            ORDER BY due_at, id LIMIT 1
            """,
            params,
        ).fetchone()
        if cycle is None:
            break
        if cycle["cycle_type"] == "stop":
            changed = _mark_stop_due(conn, cycle, now)
        elif cycle["cycle_type"] == "final_warning":
            changed = _settle_final_warning(conn, cycle, now)
        else:
            changed = _settle_cycle(conn, cycle, now)
        if changed:
            settled += 1
            continue
        current = conn.execute(
            "SELECT status FROM v102_policy_cycles WHERE id=?",
            (cycle["id"],),
        ).fetchone()
        if current is not None and current["status"] != "active":
            continue
        break
    return settled


_REPLAY_INPUT_TYPES = frozenset(
    {
        "mute_recorded",
        "mute_duration_unknown",
        "status_changed",
        "manual_stop_started",
        "manual_stop_cleared",
        "manual_stop_renewed",
        "stop_suggestion_rejected",
    }
)


def _reset_member_group_projection(
    conn: sqlite3.Connection,
    member_id: int,
    group_area: str,
    *,
    trigger_event_id: int,
    at: str,
) -> None:
    policy = _ensure_policy_state(conn, member_id, group_area, at)
    conn.execute(
        """
        UPDATE v102_policy_events
        SET is_effective=0, superseded_by_replay_id=?
        WHERE member_id=? AND group_area=? AND id!=?
          AND (
              replay_generation>0
              OR event_type NOT IN (
                  'baseline_migrated',
                  'mute_recorded', 'mute_duration_unknown', 'status_changed',
                  'manual_stop_started', 'manual_stop_cleared',
                  'manual_stop_renewed', 'stop_suggestion_rejected',
                  'record_withdrawn', 'policy_review_required', 'policy_review_resolved'
              )
          )
        """,
        (trigger_event_id, member_id, group_area, trigger_event_id),
    )
    conn.execute(
        """
        UPDATE v102_policy_cycles
        SET status='cancelled', closed_reason='replay_superseded', updated_at=?
        WHERE member_id=? AND group_area=?
        """,
        (at, member_id, group_area),
    )
    conn.execute(
        """
        UPDATE v102_pending_actions
        SET status='cancelled', updated_at=?
        WHERE member_id=? AND group_area=? AND status='pending'
          AND action_type NOT IN ('input_review','replay_review')
        """,
        (at, member_id, group_area),
    )
    conn.execute(
        """
        UPDATE member_group_states
        SET deduct_count=?, status=?, locked=?,
            last_final_warning_time=?, updated_at=?
        WHERE member_id=? AND group_area=?
        """,
        (
            max(0, int(policy["baseline_deduct_count"] or 0)),
            policy["baseline_status"],
            int(policy["baseline_locked"] or 0),
            policy["baseline_last_final_warning_time"],
            at,
            member_id,
            group_area,
        ),
    )
    conn.execute(
        """
        UPDATE v102_policy_state
        SET policy_tag='none', slow_level=0, v102_operation_count=0,
            active_cycle_id=NULL, no_cycle_reason=NULL,
            pending_action_type=NULL, last_processed_event_id=NULL,
            state_version=state_version+1, last_reason='replay_reset',
            updated_at=?
        WHERE member_id=? AND group_area=?
        """,
        (at, member_id, group_area),
    )
    business = sync_count_state(conn, member_id, group_area, updated_at=at)
    current = max(
        0,
        int(business["total_count"] or 0) - int(business["deduct_count"] or 0),
    )
    if current == 0:
        conn.execute(
            """
            UPDATE v102_policy_state SET no_cycle_reason='zero_count'
            WHERE member_id=? AND group_area=?
            """,
            (member_id, group_area),
        )


def _restore_migration_baseline_projection(
    conn: sqlite3.Connection,
    member_id: int,
    group_area: str,
    *,
    replay_generation: int,
    at: str,
) -> None:
    baseline = conn.execute(
        """
        SELECT * FROM v102_policy_events
        WHERE member_id=? AND group_area=?
          AND event_type='baseline_migrated'
          AND replay_generation=0 AND is_effective=1
        ORDER BY id LIMIT 1
        """,
        (member_id, group_area),
    ).fetchone()
    if baseline is None:
        return

    policy = _ensure_policy_state(conn, member_id, group_area, at)
    watermark = int(policy["baseline_record_watermark"] or 0)
    historical = conn.execute(
        """
        SELECT COALESCE(SUM(count_delta), 0) AS total,
               MAX(violation_time) AS last_time
        FROM violation_records
        WHERE member_id=? AND group_area=? AND id<=?
          AND is_withdrawn=0 AND is_test=0 AND is_countable=1
        """,
        (member_id, group_area, watermark),
    ).fetchone()
    raw_at_cutover = int(historical["total"] or 0)
    total = max(0, raw_at_cutover + int(policy["baseline_adjustment"] or 0))
    deduct = max(0, int(policy["baseline_deduct_count"] or 0))
    current = max(0, total - deduct)
    baseline_records_changed = conn.execute(
        """
        SELECT 1 FROM violation_records
        WHERE member_id=? AND group_area=? AND id<=? AND updated_at>?
        LIMIT 1
        """,
        (
            member_id,
            group_area,
            watermark,
            str(policy["baseline_initialized_at"]),
        ),
    ).fetchone()
    last_effective = (
        historical["last_time"]
        if baseline_records_changed is not None
        else policy["baseline_last_effective_violation_time"]
    )
    conn.execute(
        """
        UPDATE member_group_states
        SET total_count=?, deduct_count=?, current_count_cache=?,
            last_effective_violation_time=?, last_deduct_time=?, updated_at=?
        WHERE member_id=? AND group_area=?
        """,
        (
            total,
            deduct,
            current,
            last_effective,
            policy["baseline_last_deduct_time"],
            at,
            member_id,
            group_area,
        ),
    )
    business = _business_state(conn, member_id, group_area)
    status = str(business["status"] or "正常")
    if status in TERMINAL_STATUSES:
        _set_no_cycle(
            conn,
            member_id,
            group_area,
            "terminal_status",
            at,
            preserve_tag=True,
        )
        return
    if status == "最后警告":
        start_at = policy["baseline_last_final_warning_time"]
        if not start_at:
            raise RuntimeError("最后警告迁移基线缺少起始时间")
        cycle_id = _start_cycle(
            conn,
            member_id=member_id,
            group_area=group_area,
            cycle_type="final_warning",
            start_at=str(start_at),
            caused_by_event_id=int(baseline["id"]),
            fixed_sequence=1,
            replay_generation=replay_generation,
        )
        final_warning_history = conn.execute(
            """
            SELECT COUNT(*) AS count, MAX(id) AS last_id,
                   MAX(violation_time) AS last_time
            FROM violation_records
            WHERE member_id=? AND group_area=? AND id<=?
              AND is_withdrawn=0 AND is_test=0 AND is_countable=1
              AND action LIKE '%禁言%'
              AND violation_time>?
            """,
            (
                member_id,
                group_area,
                int(policy["baseline_record_watermark"] or 0),
                str(start_at),
            ),
        ).fetchone()
        if cycle_id is not None and int(final_warning_history["count"] or 0) > 0:
            event_id, _ = _insert_event(
                conn,
                member_id=member_id,
                group_area=group_area,
                event_type="historical_final_warning_violation_detected",
                effective_time=str(final_warning_history["last_time"]),
                event_priority=10,
                source_sequence=int(final_warning_history["last_id"] or 0),
                ingest_time=at,
                idempotency_key=(
                    f"replay:{replay_generation}:baseline:{baseline['id']}:"
                    "final-warning-history"
                ),
                caused_by_event_id=int(baseline["id"]),
                replay_generation=replay_generation,
                payload={
                    "violation_count": int(final_warning_history["count"]),
                    "last_violation_time": str(final_warning_history["last_time"]),
                },
            )
            _create_pending_action(
                conn,
                member_id=member_id,
                group_area=group_area,
                action_type="remove_member",
                reason="最后警告后仍有历史禁言，请管理判断是否移出",
                caused_by_event_id=event_id,
                at=at,
            )
            conn.execute(
                """
                UPDATE v102_policy_cycles
                SET status='pending_decision', updated_at=? WHERE id=?
                """,
                (at, cycle_id),
            )
        return
    if status == "已质询" or current >= 3:
        cycle_type = "slow"
    elif current > 0:
        cycle_type = "normal"
    else:
        _set_no_cycle(conn, member_id, group_area, "zero_count", at)
        return
    _start_cycle(
        conn,
        member_id=member_id,
        group_area=group_area,
        cycle_type=cycle_type,
        start_at=str(baseline["effective_time"]),
        caused_by_event_id=int(baseline["id"]),
        replay_generation=replay_generation,
    )


def _apply_replay_input(
    conn: sqlite3.Connection,
    event: sqlite3.Row,
    *,
    generation: int,
) -> None:
    payload = json.loads(event["payload_json"] or "{}")
    key = f"replay:{generation}:input:{event['id']}"
    if event["event_type"] in {"mute_recorded", "mute_duration_unknown"}:
        if event["source_record_id"] is not None:
            process_violation_record(
                conn,
                int(event["source_record_id"]),
                ingest_time=event["ingest_time"],
                replay_generation=generation,
                caused_by_event_id=int(event["id"]),
            )
    elif event["event_type"] == "status_changed":
        process_status_change(
            conn,
            member_id=int(event["member_id"]),
            group_area=event["group_area"],
            status=str(payload["status"]),
            effective_at=event["effective_time"],
            idempotency_key=key,
            ingest_time=event["ingest_time"],
            caused_by_event_id=int(event["id"]),
            replay_generation=generation,
        )
    elif event["event_type"] == "manual_stop_started":
        start_manual_stop(
            conn,
            member_id=int(event["member_id"]),
            group_area=event["group_area"],
            effective_at=event["effective_time"],
            reason=str(payload.get("reason") or "历史人工减停"),
            idempotency_key=key,
            caused_by_event_id=int(event["id"]),
            replay_generation=generation,
        )
    elif event["event_type"] == "manual_stop_cleared":
        clear_manual_stop(
            conn,
            member_id=int(event["member_id"]),
            group_area=event["group_area"],
            effective_at=event["effective_time"],
            reason=str(payload.get("reason") or "历史人工清除减停"),
            idempotency_key=key,
            caused_by_event_id=int(event["id"]),
            replay_generation=generation,
        )
    elif event["event_type"] == "manual_stop_renewed":
        renew_manual_stop(
            conn,
            member_id=int(event["member_id"]),
            group_area=event["group_area"],
            effective_at=event["effective_time"],
            reason=str(payload.get("reason") or "历史人工续期减停"),
            idempotency_key=key,
            caused_by_event_id=int(event["id"]),
            replay_generation=generation,
        )
    elif event["event_type"] == "stop_suggestion_rejected":
        reject_stop_suggestion(
            conn,
            member_id=int(event["member_id"]),
            group_area=event["group_area"],
            effective_at=event["effective_time"],
            reason=str(payload.get("reason") or "历史拒绝减停建议"),
            idempotency_key=key,
            caused_by_event_id=int(event["id"]),
            replay_generation=generation,
        )


def replay_member_group(
    conn: sqlite3.Connection, member_id: int, group_area: str, *,
    trigger_event_id: int, as_of: str | datetime,
) -> PolicyOutcome:
    """Commit a complete replay or preserve the last valid human decision."""
    at = _time_text(as_of)
    conn.execute("SAVEPOINT policy_replay_projection")
    try:
        try:
            outcome = _replay_member_group(conn, member_id, group_area,
                trigger_event_id=trigger_event_id, as_of=at)
        except PolicyReplayConflict as exc:
            conn.execute("ROLLBACK TO SAVEPOINT policy_replay_projection")
            trigger = conn.execute("SELECT source_record_id FROM v102_policy_events WHERE id=?",
                (trigger_event_id,)).fetchone()
            outcome = record_policy_review(conn, member_id=member_id, group_area=group_area,
                source_record_id=trigger["source_record_id"], key=f"event:{trigger_event_id}:replay-review",
                at=at, reason=str(exc), action_type="replay_review")
    except BaseException:
        conn.execute("ROLLBACK TO SAVEPOINT policy_replay_projection")
        raise
    finally:
        conn.execute("RELEASE SAVEPOINT policy_replay_projection")
    return outcome


def _replay_member_group(
    conn: sqlite3.Connection,
    member_id: int,
    group_area: str,
    *,
    trigger_event_id: int,
    as_of: str | datetime,
) -> PolicyOutcome:
    replay_at = _time_text(as_of)
    row = conn.execute(
        """
        SELECT COALESCE(MAX(replay_generation), 0) AS generation
        FROM v102_policy_events
        WHERE member_id=? AND group_area=?
        """,
        (member_id, group_area),
    ).fetchone()
    generation = int(row["generation"] or 0) + 1
    checkpoint = _latest_review_checkpoint(conn, member_id, group_area)
    _reset_member_group_projection(
        conn,
        member_id,
        group_area,
        trigger_event_id=trigger_event_id,
        at=replay_at,
    )
    input_watermark = 0
    if checkpoint is not None:
        snapshot = _restore_review_checkpoint(conn, checkpoint)
        input_watermark = int(snapshot["event_watermark"])
    else:
        _restore_migration_baseline_projection(
            conn,
            member_id,
            group_area,
            replay_generation=generation,
            at=replay_at,
        )
    inputs = conn.execute(
        """
        SELECT * FROM v102_policy_events
        WHERE member_id=? AND group_area=? AND replay_generation=0
          AND is_effective=1 AND id>?
          AND event_type IN (
              'mute_recorded', 'mute_duration_unknown', 'status_changed',
              'manual_stop_started', 'manual_stop_cleared',
              'manual_stop_renewed', 'stop_suggestion_rejected'
          )
        ORDER BY effective_time, event_priority, source_sequence, id
        """,
        (member_id, group_area, input_watermark),
    ).fetchall()

    index = 0
    while index < len(inputs):
        effective_time = inputs[index]["effective_time"]
        before = _time_text(_time_value(effective_time) - timedelta(seconds=1))
        settle_due_cycles(conn, before, member_id=member_id, group_area=group_area)
        while index < len(inputs) and inputs[index]["effective_time"] == effective_time:
            try:
                _apply_replay_input(conn, inputs[index], generation=generation)
            except ValueError as exc:
                if inputs[index]["event_type"] not in _MANUAL_INPUT_TYPES:
                    raise
                raise PolicyReplayConflict(
                    f"历史人工决定事件 {inputs[index]['id']} 与变更证据冲突：{exc}"
                ) from exc
            index += 1
        settle_due_cycles(conn, effective_time, member_id=member_id, group_area=group_area)
    settle_due_cycles(conn, replay_at, member_id=member_id, group_area=group_area)
    conn.execute(
        """
        UPDATE v102_policy_state
        SET last_reason='replay_complete', state_version=state_version+1,
            updated_at=? WHERE member_id=? AND group_area=?
        """,
        (replay_at, member_id, group_area),
    )
    review = conn.execute(
        """SELECT action_type,reason FROM v102_pending_actions
        WHERE member_id=? AND group_area=? AND status='pending'
          AND action_type IN ('input_review','replay_review') ORDER BY id LIMIT 1""",
        (member_id, group_area),
    ).fetchone()
    if review is not None:
        conn.execute(
            """UPDATE v102_policy_state SET pending_action_type=?,last_reason=?
            WHERE member_id=? AND group_area=?""",
            (review["action_type"],review["reason"],member_id,group_area),
        )
    return PolicyOutcome(trigger_event_id, True)


def withdraw_violation_record(
    conn: sqlite3.Connection,
    source_record_id: int,
    *,
    effective_at: str | datetime,
    reason: str,
) -> PolicyOutcome:
    at = _time_text(effective_at)
    record = conn.execute(
        "SELECT * FROM violation_records WHERE id=?", (source_record_id,)
    ).fetchone()
    if record is None:
        raise LookupError(f"missing violation record: {source_record_id}")
    original = conn.execute(
        """
        SELECT * FROM v102_policy_events
        WHERE source_record_id=? AND replay_generation=0
          AND event_type IN ('mute_recorded', 'mute_duration_unknown')
        ORDER BY id LIMIT 1
        """,
        (source_record_id,),
    ).fetchone()
    event_id, created = _insert_event(
        conn,
        member_id=int(record["member_id"]),
        group_area=record["group_area"],
        event_type="record_withdrawn",
        effective_time=at,
        event_priority=20,
        source_sequence=int(record["id"]),
        ingest_time=at,
        source_record_id=int(record["id"]),
        caused_by_event_id=int(original["id"]) if original else None,
        idempotency_key=f"record:{record['id']}:withdrawn",
        payload={"reason": reason},
    )
    if not created:
        return PolicyOutcome(event_id, False)
    conn.execute(
        """
        UPDATE violation_records
        SET is_withdrawn=1, withdrawn_reason=?, updated_at=? WHERE id=?
        """,
        (reason, at, source_record_id),
    )
    conn.execute(
        """UPDATE v102_pending_actions
        SET status='resolved',decision_event_id=?,updated_at=?
        WHERE status='pending' AND action_type IN ('input_review','replay_review')
          AND caused_by_event_id IN (
              SELECT id FROM v102_policy_events
              WHERE source_record_id=? AND event_type='policy_review_required'
          )""",
        (event_id, at, source_record_id),
    )
    conn.execute(
        """UPDATE v102_policy_events SET is_effective=0,reversed_by_event_id=?
        WHERE source_record_id=? AND event_type='policy_review_required'""",
        (event_id, source_record_id),
    )
    checkpoint = _latest_review_checkpoint(conn, int(record["member_id"]), record["group_area"])
    if checkpoint is not None and source_record_id <= int(json.loads(checkpoint["payload_json"])["record_watermark"]):
        if original is not None:
            conn.execute("UPDATE v102_policy_events SET is_effective=0,reversed_by_event_id=? WHERE id=?",(event_id,original["id"]))
        sync_count_state(conn,int(record["member_id"]),record["group_area"],updated_at=at)
        return record_policy_review(conn,member_id=int(record["member_id"]),group_area=record["group_area"],
            source_record_id=source_record_id,key=f"event:{event_id}:replay-review",at=at,
            reason=f"撤回证据影响已确认复核事件 {checkpoint['id']}，保留原决定并再次复核",action_type="replay_review")
    if original:
        manual_conflicts = conn.execute(
            """WITH RECURSIVE affected(id) AS (
                SELECT ? UNION SELECT e.id FROM v102_policy_events e JOIN affected a ON e.caused_by_event_id=a.id
            ) SELECT e.id FROM v102_policy_events e JOIN affected a ON e.id=a.id
            WHERE e.replay_generation=0 AND e.is_effective=1 AND e.event_type IN (
                'manual_stop_started','manual_stop_cleared','manual_stop_renewed','stop_suggestion_rejected')""",
            (original["id"],),
        ).fetchall()
        if manual_conflicts:
            conn.execute("UPDATE v102_policy_events SET is_effective=0,reversed_by_event_id=? WHERE id=?",
                (event_id, original["id"]))
            sync_count_state(conn, int(record["member_id"]), record["group_area"], updated_at=at)
            return record_policy_review(conn, member_id=int(record["member_id"]), group_area=record["group_area"],
                source_record_id=source_record_id, key=f"event:{event_id}:replay-review", at=at,
                reason="撤回证据关联历史人工决定事件 " + ",".join(str(item["id"]) for item in manual_conflicts)
                    + "，保留原决定，等待人工复核", action_type="replay_review")
    if original:
        conn.execute(
            """
            WITH RECURSIVE affected(id) AS (
                SELECT ?
                UNION ALL
                SELECT e.id FROM v102_policy_events e
                JOIN affected a ON e.caused_by_event_id=a.id
                WHERE e.id!=?
            )
            UPDATE v102_policy_events
            SET is_effective=0, reversed_by_event_id=?
            WHERE id IN (SELECT id FROM affected)
            """,
            (original["id"], event_id, event_id),
        )
    replay_member_group(
        conn,
        int(record["member_id"]),
        record["group_area"],
        trigger_event_id=event_id,
        as_of=at,
    )
    return PolicyOutcome(event_id, True)
