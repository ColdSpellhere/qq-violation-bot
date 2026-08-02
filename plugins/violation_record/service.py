from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from nonebot import logger

from .admin_resolver import resolve_admin_by_name, resolve_admin_by_qq, resolve_operator
from .config import CANCEL_WORDS, CONFIG, CONFIRM_WORDS, GROUP_AREAS, LOCKED_STATUSES
from .db import connect, dump_json, now_str
from .deduction_policy import (
    ensure_policy_scope_snapshot,
    effective_record_summary as policy_effective_record_summary,
    effective_total,
    sync_count_state,
)
from .evidence_store import EvidenceStore, write_binding_queue
from .formatter import (
    HELP_TEXT,
    ambiguous_members,
    format_create_correction,
    violation_detail,
)
from .member_resolver import format_member, get_member_by_id, get_or_create_member, resolve_member
from .reply_models import RecordMessage, StructuredReply
from .validators import display_time, first_missing, is_countable_action, normalize_action, normalize_status, normalize_time

PENDING_TTL_SECONDS = 180
_RECORD_LOG_REMARK_RE = re.compile(r"^record_id=(\d+)$")


@dataclass(frozen=True)
class InsertedViolation:
    detail: str
    violation_id: int
    target_qq: str


def _state(conn, member_id: int, area: str) -> dict[str, Any]:
    ts = now_str()
    conn.execute(
        """
        INSERT INTO member_group_states(member_id, group_area, created_at, updated_at)
        VALUES(?, ?, ?, ?)
        ON CONFLICT(member_id, group_area) DO NOTHING
        """,
        (member_id, area, ts, ts),
    )
    return dict(conn.execute("SELECT * FROM member_group_states WHERE member_id=? AND group_area=?", (member_id, area)).fetchone())


def _admin(conn, admin_id: int | None) -> dict[str, Any] | None:
    if not admin_id:
        return None
    row = conn.execute("SELECT * FROM admins WHERE id=?", (admin_id,)).fetchone()
    return dict(row) if row else None


def _current_count(conn, member_id: int, area: str) -> int:
    total = effective_total(conn, member_id, area)
    state = _state(conn, member_id, area)
    return max(0, total - int(state["deduct_count"] or 0))


def _effective_record_summary(conn, member_id: int, area: str) -> tuple[int, str | None]:
    return policy_effective_record_summary(conn, member_id, area)


def _sync_state_counts(conn, member_id: int, area: str) -> dict[str, Any]:
    _state(conn, member_id, area)
    if CONFIG.deduction_policy_v102_enabled:
        ensure_policy_scope_snapshot(conn, member_id, area, now_str())
    return dict(sync_count_state(conn, member_id, area, updated_at=now_str()))


def _log(conn, operation_type: str, source: str, operator: dict[str, Any] | None, target_member_id: int | None, area: str | None, before: Any, after: Any, message_id: str | None = None, remark: str | None = None) -> int:
    cursor = conn.execute(
        """
        INSERT INTO operation_logs(group_area, operation_type, source, operator_qq, operator_nickname,
            target_member_id, before_json, after_json, message_id, created_at, remark)
        VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            area,
            operation_type,
            source,
            (operator or {}).get("qq_number"),
            (operator or {}).get("nickname"),
            target_member_id,
            dump_json(before),
            dump_json(after),
            message_id,
            now_str(),
            remark,
        ),
    )
    return int(cursor.lastrowid)


def _enqueue_status_bridge_job(
    conn,
    *,
    operation_log_id: int,
    member_id: int,
    group_area: str,
    status: str,
    effective_at: str,
    caused_by_record_id: int | None = None,
) -> None:
    if not CONFIG.deduction_policy_v102_enabled:
        return
    created_at = now_str()
    conn.execute(
        """
        INSERT OR IGNORE INTO v102_status_bridge_jobs(
            operation_log_id, member_id, group_area, target_status,
            caused_by_record_id, effective_at, idempotency_key,
            created_at, updated_at
        ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            operation_log_id,
            member_id,
            group_area,
            status,
            caused_by_record_id,
            effective_at,
            f"status-log:{operation_log_id}",
            created_at,
            created_at,
        ),
    )


def _explicit_causal_record_id(
    conn,
    intent: dict[str, Any],
    *,
    member_id: int,
    group_area: str,
) -> int | None:
    reply_message_id = str(intent.get("_reply_message_id") or "").strip()
    if not reply_message_id:
        return None
    operation = conn.execute(
        """
        SELECT remark FROM operation_logs
        WHERE message_id=? AND operation_type='新增记录'
          AND target_member_id=? AND group_area=?
        ORDER BY id DESC LIMIT 1
        """,
        (reply_message_id, member_id, group_area),
    ).fetchone()
    if operation is None:
        return None
    matched = _RECORD_LOG_REMARK_RE.fullmatch(str(operation["remark"] or ""))
    if matched is None:
        return None
    record_id = int(matched.group(1))
    record = conn.execute(
        """
        SELECT id FROM violation_records
        WHERE id=? AND member_id=? AND group_area=? AND is_withdrawn=0
        """,
        (record_id, member_id, group_area),
    ).fetchone()
    return record_id if record is not None else None


def _causal_record_is_active(conn, payload: dict[str, Any]) -> bool:
    record_id = payload.get("caused_by_record_id")
    if record_id is None:
        return True
    record = conn.execute(
        """
        SELECT 1 FROM violation_records
        WHERE id=? AND member_id=? AND group_area=? AND is_withdrawn=0
        """,
        (record_id, payload["member_id"], payload["group_area"]),
    ).fetchone()
    return record is not None


def _set_pending(group_id: str, operator_qq: str, operation_type: str, payload: dict[str, Any]) -> None:
    expires = (datetime.now() + timedelta(seconds=PENDING_TTL_SECONDS)).strftime("%Y-%m-%d %H:%M:%S")
    with connect() as conn:
        conn.execute(
            """
            INSERT INTO pending_operations(group_id, operator_qq, operation_type, payload_json, expires_at, created_at)
            VALUES(?, ?, ?, ?, ?, ?)
            ON CONFLICT(group_id, operator_qq) DO UPDATE SET
                operation_type=excluded.operation_type,
                payload_json=excluded.payload_json,
                expires_at=excluded.expires_at,
                created_at=excluded.created_at
            """,
            (group_id, operator_qq, operation_type, dump_json(payload), expires, now_str()),
        )


def _pop_pending(group_id: str, operator_qq: str) -> tuple[str, dict[str, Any]] | None:
    with connect() as conn:
        row = conn.execute("SELECT * FROM pending_operations WHERE group_id=? AND operator_qq=?", (group_id, operator_qq)).fetchone()
        if not row:
            return None
        conn.execute("DELETE FROM pending_operations WHERE id=?", (row["id"],))
        payload = json.loads(row["payload_json"])
        if datetime.strptime(row["expires_at"], "%Y-%m-%d %H:%M:%S") < datetime.now():
            payload["_expired_operation_type"] = row["operation_type"]
            return "expired", payload
        return row["operation_type"], payload


def _operator_or_message(operator_qq: str, nickname: str | None) -> dict[str, Any] | str:
    operator = resolve_operator(operator_qq, nickname)
    if not operator:
        return "无法登记当前操作人，请联系维护者查看 admins 表。"
    return operator


SELF_HANDLER_NAMES = {"我", "我自己", "本人", "自己", "记录人", "发送者", "当前管理员"}
HANDLER_FIELD_NAMES = {
    "handler_admin",
    "handler_admin_qq",
    "handler_admin_nickname",
    "violation.handler_admin",
    "violation.handler_admin_qq",
    "violation.handler_admin_nickname",
}
CREATE_CORRECTION_FIELD_ALIASES = {
    "handler_admin": "violation.handler_admin_nickname",
    "handler_admin_qq": "violation.handler_admin_qq",
    "handler_admin_nickname": "violation.handler_admin_nickname",
    "violation.handler_admin": "violation.handler_admin_nickname",
}
CREATE_CORRECTION_FIELDS = {
    "group_area",
    "target",
    "target.qq_number",
    "target.qq_nickname",
    "violation.time",
    "violation.judgement",
    "violation.action",
    "violation.handler_admin_qq",
    "violation.handler_admin_nickname",
}
TARGET_CORRECTION_FIELDS = {
    "target",
    "target.qq_number",
    "target.qq_nickname",
}


def _clean_optional_text(value: Any) -> str | None:
    text = str(value or "").strip()
    return text or None


def _handler_needs_clarification(intent: dict[str, Any]) -> bool:
    operation = intent.get("operation") or {}
    fields = set(operation.get("missing_fields") or []) | set(operation.get("ambiguous_fields") or [])
    return bool(fields & HANDLER_FIELD_NAMES)


def _operation_create_correction_fields(intent: dict[str, Any]) -> list[str]:
    operation = intent.get("operation") or {}
    raw_fields = [
        *(operation.get("missing_fields") or []),
        *(operation.get("ambiguous_fields") or []),
    ]
    fields = []
    for raw_field in raw_fields:
        field = CREATE_CORRECTION_FIELD_ALIASES.get(str(raw_field), str(raw_field))
        if field in CREATE_CORRECTION_FIELDS and field not in fields:
            fields.append(field)

    violation = intent.get("violation") or {}
    if (
        "violation.time" in fields
        and not _clean_optional_text(violation.get("time"))
        and normalize_time(intent.get("_reply_time"))
    ):
        fields.remove("violation.time")
    return fields


def _resolve_handler_admin(intent: dict[str, Any], operator: dict[str, Any]) -> tuple[str, Any]:
    violation = intent["violation"]
    handler_qq = _clean_optional_text(violation.get("handler_admin_qq"))
    handler_name = _clean_optional_text(violation.get("handler_admin_nickname"))
    if handler_name in SELF_HANDLER_NAMES:
        return "ok", operator
    if not handler_qq and handler_name:
        qq_match = re.search(r"\d{5,12}", handler_name)
        if qq_match:
            handler_qq = qq_match.group(0)
    if handler_qq:
        status, handler = resolve_admin_by_qq(handler_qq)
        if status == "ok":
            return "ok", handler
        return "not_found_qq", handler_qq
    if handler_name:
        return resolve_admin_by_name(handler_name)
    if _handler_needs_clarification(intent):
        return "needs_clarification", None
    return "ok", operator


def _require_area(intent: dict[str, Any]) -> str | None:
    area = intent.get("group_area")
    if area in GROUP_AREAS:
        return area
    return None


async def handle_intent(intent: dict[str, Any], group_id: str, operator_qq: str, operator_nickname: str | None, message_id: str | None = None) -> str | StructuredReply:
    name = intent["intent"]
    if name == "help":
        return HELP_TEXT
    if name == "confirm" or str(intent.get("_raw", "")).strip() in CONFIRM_WORDS:
        return confirm_pending(group_id, operator_qq, operator_nickname, message_id)
    if name == "cancel" or str(intent.get("_raw", "")).strip() in CANCEL_WORDS:
        return cancel_pending(group_id, operator_qq)
    if name in {"unknown"}:
        return "我没有理解这条业务操作，请换一种更明确的说法。"
    if name not in {"help", "confirm", "cancel"} and not _require_area(intent):
        if name == "create_violation":
            return preview_create(
                intent, group_id, operator_qq, operator_nickname, message_id
            )
        return "请标明群聊：蜂巢 / 蜂窝 / 蜂箱。"
    if name in {"query_member", "query_recent"} and _looks_like_area_record_query(intent):
        return query_area_records(intent, operator_qq, operator_nickname, message_id)
    if name == "query_member":
        return query_member(intent, operator_qq, operator_nickname, False, message_id)
    if name == "query_recent":
        return query_member(intent, operator_qq, operator_nickname, True, message_id)
    if name == "query_area_records":
        return query_area_records(intent, operator_qq, operator_nickname, message_id)
    if name == "create_violation":
        return preview_create(intent, group_id, operator_qq, operator_nickname, message_id)
    if name in {"consultation", "final_warning"}:
        return preview_consultation(intent, group_id, operator_qq, operator_nickname, message_id)
    if name == "withdraw_latest":
        return preview_withdraw(intent, group_id, operator_qq, operator_nickname, message_id)
    if name == "update_status":
        return preview_status_update(intent, group_id, operator_qq, operator_nickname, message_id)
    if name == "unlock_member":
        return preview_unlock(intent, group_id, operator_qq, operator_nickname, message_id)
    if name == "export":
        from .exporter import export_by_intent

        return export_by_intent(intent, operator_qq, operator_nickname, message_id)
    return "暂不支持这个操作。"


def _resolve_target_for_read(intent: dict[str, Any]) -> tuple[str, Any]:
    target = intent["target"]
    return resolve_member(target.get("qq_number"), target.get("qq_nickname"), allow_create=False)


def _resolve_target_for_write(intent: dict[str, Any]) -> tuple[str, Any]:
    target = intent["target"]
    return resolve_member(target.get("qq_number"), target.get("qq_nickname"), allow_create=bool(target.get("qq_number") and target.get("qq_nickname")))


def _member_problem(status: str, data: Any) -> str | None:
    if status == "ok":
        return None
    if status == "ambiguous":
        return ambiguous_members(data)
    if status in {"not_found", "need_member_info"}:
        return "未找到该成员，请补充 QQ号 和 QQ昵称。"
    return "请提供违规者 QQ号 或 QQ昵称。"


def _format_create_problem(intent: dict[str, Any], fields: list[str]) -> str:
    try:
        return format_create_correction(intent, fields)
    except Exception as exc:
        logger.warning(
            f"新增记录纠正模板降级 stage=create error={type(exc).__name__}"
        )
        return f"缺少必要信息：{first_missing(fields)}。"


def _looks_like_area_record_query(intent: dict[str, Any]) -> bool:
    raw = str(intent.get("_raw", ""))
    target = intent.get("target") or {}
    has_target = bool(target.get("qq_number") or target.get("qq_nickname"))
    return not has_target and ("违规记录" in raw or "全部记录" in raw or "本月记录" in raw or "最近记录" in raw)


def _query_range(intent: dict[str, Any]) -> tuple[str, str | None, str | None]:
    query = intent.get("query") or {}
    raw = str(intent.get("_raw", ""))
    time_range = query.get("time_range")
    if not time_range:
        if "本月" in raw or "这个月" in raw or "当月" in raw:
            time_range = "current_month"
        elif "本周" in raw or "这周" in raw or "这个星期" in raw:
            time_range = "current_week"
        elif "昨天" in raw or "昨日" in raw:
            time_range = "yesterday"
        elif "今天" in raw or "今日" in raw:
            time_range = "today"
        elif "最近" in raw:
            time_range = "recent_days"
        else:
            time_range = "all"

    now = datetime.now().replace(second=0, microsecond=0)
    if time_range == "today":
        start = now.replace(hour=0, minute=0)
        return "今天", start.strftime("%Y-%m-%d %H:%M:%S"), now.strftime("%Y-%m-%d %H:%M:%S")
    if time_range == "yesterday":
        day = now - timedelta(days=1)
        start = day.replace(hour=0, minute=0)
        end = day.replace(hour=23, minute=59)
        return "昨天", start.strftime("%Y-%m-%d %H:%M:%S"), end.strftime("%Y-%m-%d %H:%M:%S")
    if time_range == "current_week":
        start = (now - timedelta(days=now.weekday())).replace(hour=0, minute=0)
        return "本周", start.strftime("%Y-%m-%d %H:%M:%S"), now.strftime("%Y-%m-%d %H:%M:%S")
    if time_range == "current_month":
        start = now.replace(day=1, hour=0, minute=0)
        return "本月", start.strftime("%Y-%m-%d %H:%M:%S"), now.strftime("%Y-%m-%d %H:%M:%S")
    if time_range == "recent_days":
        days = int(query.get("recent_days") or 14)
        start = now - timedelta(days=days)
        return f"最近{days}天", start.strftime("%Y-%m-%d %H:%M:%S"), now.strftime("%Y-%m-%d %H:%M:%S")
    return "全部", None, None


def query_area_records(intent: dict[str, Any], operator_qq: str, operator_nickname: str | None, message_id: str | None) -> str | StructuredReply:
    area = _require_area(intent)
    label, start, end = _query_range(intent)
    try:
        limit = int((intent.get("query") or {}).get("limit") or 20)
    except (TypeError, ValueError):
        limit = 20
    limit = max(1, min(limit, 50))
    with connect() as conn:
        params: list[Any] = [area]
        where = "v.group_area=? AND v.is_withdrawn=0"
        if start and end:
            where += " AND v.violation_time BETWEEN ? AND ?"
            params.extend([start, end])
        total = conn.execute(
            f"SELECT COUNT(*) AS c FROM violation_records v WHERE {where}",
            params,
        ).fetchone()["c"]
        rows = conn.execute(
            f"""
            SELECT v.*, m.qq_number, m.qq_nickname
            FROM violation_records v
            JOIN members m ON m.id=v.member_id
            WHERE {where}
            ORDER BY v.violation_time DESC, v.id DESC
            LIMIT ?
            """,
            [*params, limit],
        ).fetchall()
        operator = resolve_operator(operator_qq, operator_nickname)
        _log(
            conn,
            "查询分区记录",
            "手动",
            operator,
            None,
            area,
            None,
            {"range": label, "start": start, "end": end, "total": total},
            message_id,
        )
    header = [f"{area}{label}违规记录", "", f"记录数：{total}"]
    if not rows:
        return "\n".join([*header, "", "无记录。"])

    violation_ids = [int(row["id"]) for row in rows]
    try:
        evidence = EvidenceStore(
            CONFIG.evidence_database_path, CONFIG.evidence_root
        ).paths_for_violations(violation_ids)
    except Exception as exc:
        logger.warning(f"证据查询降级 stage=query error={type(exc).__name__}")
        evidence = {violation_id: () for violation_id in violation_ids}

    header.extend(["", "具体记录：", ""])
    records = []
    for index, row in enumerate(rows, 1):
        nickname = row["qq_nickname"] or "未知昵称"
        line = (
            f"{index}. {nickname}（{row['qq_number']}） {display_time(row['violation_time'])}，{row['judgement']}，{row['action']}"
        )
        text = "\n".join([*header, line]) if index == 1 else line
        records.append(RecordMessage(text, evidence[int(row["id"])]))
    if total > len(rows):
        footer = f"仅显示最近 {len(rows)} 条。需要完整数据请发送：导出{area}{label}违规记录"
        last = records[-1]
        records[-1] = RecordMessage(f"{last.text}\n\n{footer}", last.images)
    return StructuredReply(tuple(records))


def query_member(intent: dict[str, Any], operator_qq: str, operator_nickname: str | None, recent: bool, message_id: str | None) -> str | StructuredReply:
    area = _require_area(intent)
    status, member = _resolve_target_for_read(intent)
    problem = _member_problem(status, member)
    if problem:
        return problem
    with connect() as conn:
        state = _sync_state_counts(conn, member["id"], area)
        current = _current_count(conn, member["id"], area)
        if recent:
            last = conn.execute(
                "SELECT violation_time FROM violation_records WHERE member_id=? AND group_area=? AND is_withdrawn=0 ORDER BY violation_time DESC LIMIT 1",
                (member["id"], area),
            ).fetchone()
            if not last:
                return f"{format_member(member)}\n\n最近 14 天无记录。"
            end = datetime.strptime(last["violation_time"], "%Y-%m-%d %H:%M:%S")
            start = end - timedelta(days=int(intent["query"].get("recent_days") or 14))
            rows = conn.execute(
                """
                SELECT * FROM violation_records
                WHERE member_id=? AND group_area=? AND is_withdrawn=0 AND violation_time BETWEEN ? AND ?
                ORDER BY violation_time DESC
                """,
                (member["id"], area, start.strftime("%Y-%m-%d %H:%M:%S"), end.strftime("%Y-%m-%d %H:%M:%S")),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM violation_records WHERE member_id=? AND group_area=? AND is_withdrawn=0 ORDER BY violation_time DESC",
                (member["id"], area),
            ).fetchall()
        operator = resolve_operator(operator_qq, operator_nickname)
        _log(conn, "查询", "手动", operator, member["id"], area, None, {"recent": recent}, message_id)
    header = [format_member(member), "", f"当前次数：{current}", f"状态：{state['status']}", "", "具体记录：", ""]
    if not rows:
        return "\n".join([*header, "无记录。"])

    violation_ids = [int(row["id"]) for row in rows]
    try:
        evidence = EvidenceStore(
            CONFIG.evidence_database_path, CONFIG.evidence_root
        ).paths_for_violations(violation_ids)
    except Exception as exc:
        logger.warning(f"证据查询降级 stage=query error={type(exc).__name__}")
        evidence = {violation_id: () for violation_id in violation_ids}

    records = []
    for index, row in enumerate(rows, 1):
        line = f"{index}. {display_time(row['violation_time'])}，{row['judgement']}，{row['action']}"
        text = "\n".join([*header, line]) if index == 1 else line
        records.append(RecordMessage(text, evidence[int(row["id"])]))
    return StructuredReply(tuple(records))


def preview_create(intent: dict[str, Any], group_id: str, operator_qq: str, operator_nickname: str | None, message_id: str | None) -> str:
    area = _require_area(intent)
    violation = intent["violation"]
    target = intent["target"]
    missing = []
    if not area:
        missing.append("group_area")
    if not target.get("qq_number") and not target.get("qq_nickname"):
        missing.append("target")
    raw_time = violation.get("time") or intent.get("_reply_time")
    vtime = normalize_time(raw_time)
    if not vtime:
        missing.append("violation.time")
    judgement = violation.get("judgement")
    if not judgement:
        missing.append("violation.judgement")
    action = normalize_action(violation.get("action"))
    if not action:
        missing.append("violation.action")
    operation_fields = _operation_create_correction_fields(intent)
    immediate_fields = [
        *missing,
        *[
            field
            for field in operation_fields
            if field not in TARGET_CORRECTION_FIELDS
        ],
    ]
    immediate_fields = list(dict.fromkeys(immediate_fields))
    if immediate_fields:
        return _format_create_problem(intent, immediate_fields)

    operation = intent.get("operation") or {}
    confidence = operation.get("confidence", 0)
    ai_missing = set(operation.get("missing_fields") or [])
    only_time_was_filled_by_reply = bool(intent.get("_reply_time")) and ai_missing.issubset({"violation.time"})

    status, member = _resolve_target_for_read(intent)
    create_member = False
    if status == "ambiguous":
        return _format_create_problem(intent, ["target.qq_number"])
    if status in {"not_found", "need_member_info"}:
        has_qq = bool(target.get("qq_number"))
        has_nickname = bool(target.get("qq_nickname"))
        if has_qq and has_nickname:
            target_fields = [
                field
                for field in operation_fields
                if field in TARGET_CORRECTION_FIELDS
            ]
            if target_fields:
                return _format_create_problem(intent, target_fields)
            create_member = True
        elif has_qq and not has_nickname:
            return _format_create_problem(intent, ["target.qq_nickname"])
        elif has_nickname and not has_qq:
            return _format_create_problem(intent, ["target.qq_number"])
        else:
            return _format_create_problem(intent, ["target"])
    elif status != "ok":
        problem = _member_problem(status, member)
        if problem:
            return problem

    nickname_resolved_missing_qq = (
        status == "ok"
        and not target.get("qq_number")
        and bool(target.get("qq_nickname"))
        and set(operation_fields) == {"target.qq_number"}
        and set(operation.get("missing_fields") or []) == {"target.qq_number"}
        and not operation.get("ambiguous_fields")
    )
    if (
        confidence < 0.55
        and not only_time_was_filled_by_reply
        and not nickname_resolved_missing_qq
        and not _handler_needs_clarification(intent)
    ):
        return "这条记录我理解得不够确定，请补充 QQ号、时间、原因和处理措施。"

    admin_status, handler = _resolve_handler_admin(intent, {})
    if admin_status == "ambiguous":
        return _format_create_problem(
            intent, ["violation.handler_admin_nickname"]
        )
    if admin_status == "needs_clarification":
        return _format_create_problem(
            intent, ["violation.handler_admin_nickname"]
        )
    if admin_status == "not_found_qq":
        return f"未找到处理人 QQ号：{handler}。请先让处理人在允许群里 @ 机器人触发同步，或维护管理员。"
    if admin_status == "not_found":
        return "未找到处理人，请提供处理人 QQ号，或先让处理人在允许群里 @ 机器人触发同步并维护昵称/别名。"

    operator = _operator_or_message(operator_qq, operator_nickname)
    if isinstance(operator, str):
        return operator
    if not handler:
        handler = operator
    if create_member:
        status, member = _resolve_target_for_write(intent)
        problem = _member_problem(status, member)
        if problem:
            return problem

    with connect() as conn:
        state = _state(conn, member["id"], area)
        if state["status"] in LOCKED_STATUSES or state["locked"]:
            return f"{format_member(member)}\n\n状态：{state['status']}，数据已锁定，只允许查询。"
    record = {
        "member_id": member["id"],
        "group_area": area,
        "violation_time": vtime,
        "judgement": judgement,
        "action": action,
        "handler_admin_id": handler["id"],
        "recorder_admin_id": operator["id"],
        "remark": violation.get("remark") or "无",
        "is_countable": 1 if is_countable_action(action) else 0,
        "count_delta": 1 if is_countable_action(action) else 0,
        "is_test": 1 if "测试" in (judgement + action) else 0,
    }
    evidence_batch_id = intent.get("_evidence_batch_id")
    evidence_count = int(intent.get("_evidence_count") or 0)
    if CONFIG.evidence_required and not evidence_batch_id:
        return "请引用至少一张证据图片后重新记录。"

    pending_payload = {"record": record, "message_id": message_id}
    if evidence_batch_id:
        pending_payload["evidence_batch_id"] = evidence_batch_id
    _set_pending(group_id, operator_qq, "create_violation", pending_payload)

    evidence_note = (
        f"\n\n已暂存证据图片：{evidence_count} 张。"
        if evidence_batch_id
        else "\n\n未引用证据图片；当前为提醒模式，仍可确认入库。"
    )
    return violation_detail(record, member, handler, operator) + evidence_note + "\n\n请回复“确认”入库，或回复“取消”放弃。"


def _insert_violation(conn, record: dict[str, Any], operator: dict[str, Any], message_id: str | None) -> InsertedViolation:
    before = _state(conn, record["member_id"], record["group_area"])
    if CONFIG.deduction_policy_v102_enabled:
        ensure_policy_scope_snapshot(
            conn,
            record["member_id"],
            record["group_area"],
            now_str(),
        )
    previous_last_time = _effective_record_summary(conn, record["member_id"], record["group_area"])[1]
    ts = now_str()
    cursor = conn.execute(
        """
        INSERT INTO violation_records(member_id, group_area, violation_time, judgement, action, handler_admin_id,
            recorder_admin_id, remark, is_countable, count_delta, is_test, created_at, updated_at)
        VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            record["member_id"],
            record["group_area"],
            record["violation_time"],
            record["judgement"],
            record["action"],
            record["handler_admin_id"],
            record["recorder_admin_id"],
            record["remark"],
            record["is_countable"],
            record["count_delta"],
            record["is_test"],
            ts,
            ts,
        ),
    )
    violation_id = int(cursor.lastrowid)
    if (
        record["is_countable"]
        and not record["is_test"]
        and (not previous_last_time or record["violation_time"] >= previous_last_time)
    ):
        conn.execute(
            """
            UPDATE member_group_states
            SET last_effective_violation_time=?, last_deduct_time=?, updated_at=?
            WHERE member_id=? AND group_area=?
            """,
            (record["violation_time"], record["violation_time"], ts, record["member_id"], record["group_area"]),
        )
    after = _sync_state_counts(conn, record["member_id"], record["group_area"])
    _log(
        conn,
        "新增记录",
        "手动",
        operator,
        record["member_id"],
        record["group_area"],
        before,
        after,
        message_id,
        remark=f"record_id={violation_id}",
    )
    member = get_member_by_id(record["member_id"])
    return InsertedViolation(
        detail=violation_detail(
            record,
            member,
            _admin(conn, record["handler_admin_id"]),
            _admin(conn, record["recorder_admin_id"]),
        ),
        violation_id=violation_id,
        target_qq=str(member["qq_number"]),
    )


def _mark_evidence_batch(payload: dict[str, Any], state: str) -> None:
    batch_id = payload.get("evidence_batch_id")
    if not batch_id:
        return
    try:
        EvidenceStore(CONFIG.evidence_database_path, CONFIG.evidence_root).mark_batch(batch_id, state)
    except Exception as exc:
        logger.warning(
            f"证据批次状态更新失败 stage={state} batch={batch_id} error={type(exc).__name__}"
        )


def confirm_pending(group_id: str, operator_qq: str, operator_nickname: str | None, message_id: str | None) -> str:
    pending = _pop_pending(group_id, operator_qq)
    if not pending:
        return "没有待确认操作。"
    if pending[0] == "expired":
        from .policy_commands import log_cancelled_policy_command

        log_cancelled_policy_command(
            pending[1].get("_expired_operation_type", ""),
            pending[1],
            operator_qq=operator_qq,
            expired=True,
        )
        _mark_evidence_batch(pending[1], "expired")
        return "待确认操作已过期，请重新发起。"
    operation_type, payload = pending
    operator = _operator_or_message(operator_qq, operator_nickname)
    if isinstance(operator, str):
        return operator
    if operation_type == "create_violation":
        with connect() as conn:
            inserted = _insert_violation(
                conn,
                payload["record"],
                operator,
                payload.get("message_id") or message_id,
            )
        batch_id = payload.get("evidence_batch_id")
        if batch_id:
            try:
                store = EvidenceStore(CONFIG.evidence_database_path, CONFIG.evidence_root)
                try:
                    store.bind_batch(batch_id, inserted.violation_id, inserted.target_qq)
                except Exception as exc:
                    try:
                        store.queue_binding(batch_id, inserted.violation_id, inserted.target_qq)
                    except Exception as queue_exc:
                        logger.warning(
                            f"证据绑定队列写入失败 stage=queue batch={batch_id} record={inserted.violation_id} error={type(queue_exc).__name__}"
                        )
                    logger.warning(
                        f"证据绑定延后 stage=bind batch={batch_id} record={inserted.violation_id} error={type(exc).__name__}"
                    )
            except Exception as exc:
                try:
                    write_binding_queue(
                        CONFIG.evidence_root,
                        batch_id,
                        inserted.violation_id,
                        inserted.target_qq,
                    )
                except Exception as queue_exc:
                    logger.warning(
                        f"证据应急队列写入失败 stage=queue batch={batch_id} record={inserted.violation_id} error={type(queue_exc).__name__}"
                    )
                logger.warning(
                    f"证据存储不可用 stage=store batch={batch_id} record={inserted.violation_id} error={type(exc).__name__}"
                )
        if CONFIG.deduction_policy_v102_enabled:
            try:
                from . import policy_bridge

                policy_bridge.bridge_violation_record(inserted.violation_id)
            except Exception as exc:
                logger.exception(
                    "v102策略联动失败 stage=record "
                    f"record={inserted.violation_id} error={type(exc).__name__}"
                )
        return inserted.detail.replace("\n\n时间", "\n\n已记录。\n\n时间", 1)
    if operation_type.startswith("v102_"):
        from .policy_commands import apply_pending_policy_command

        return apply_pending_policy_command(
            operation_type, payload, operator, message_id
        )
    result = None
    with connect() as conn:
        if (
            operation_type in {"consultation", "status_update"}
            and payload.get("caused_by_record_id") is not None
        ):
            conn.execute("BEGIN IMMEDIATE")
        if operation_type == "consultation":
            result = _apply_consultation(conn, payload, operator, message_id)
        elif operation_type == "withdraw_latest":
            result = _apply_withdraw(conn, payload, operator, message_id)
        elif operation_type == "status_update":
            result = _apply_status(conn, payload, operator, message_id)
        elif operation_type == "unlock_member":
            result = _apply_unlock(conn, payload, operator, message_id)
    if result is not None and CONFIG.deduction_policy_v102_enabled:
        try:
            from . import policy_bridge

            if operation_type == "withdraw_latest":
                policy_bridge.bridge_withdrawal(
                    payload["record_id"], reason="管理员撤回"
                )
            elif operation_type in {"consultation", "status_update", "unlock_member"}:
                policy_bridge.process_status_bridge_jobs(as_of=now_str())
        except Exception as exc:
            logger.exception(
                "v102策略联动失败 "
                f"stage={operation_type} member={payload.get('member_id')} "
                f"error={type(exc).__name__}"
            )
    if result is not None:
        return result
    return "未知待确认操作，已取消。"


def cancel_pending(group_id: str, operator_qq: str) -> str:
    pending = _pop_pending(group_id, operator_qq)
    if not pending:
        return "没有待取消操作。"
    if pending[0] == "expired":
        from .policy_commands import log_cancelled_policy_command

        log_cancelled_policy_command(
            pending[1].get("_expired_operation_type", ""),
            pending[1],
            operator_qq=operator_qq,
            expired=True,
        )
        _mark_evidence_batch(pending[1], "expired")
        return "待确认操作已过期，请重新发起。"
    if pending[0].startswith("v102_"):
        from .policy_commands import log_cancelled_policy_command

        log_cancelled_policy_command(
            pending[0], pending[1], operator_qq=operator_qq, expired=False
        )
    _mark_evidence_batch(pending[1], "cancelled")
    return "已取消。"


def _load_writable_target(intent: dict[str, Any]) -> tuple[str | None, dict[str, Any] | None, str | None]:
    area = _require_area(intent)
    status, member = _resolve_target_for_read(intent)
    problem = _member_problem(status, member)
    return area, member if not problem else None, problem


def preview_consultation(intent: dict[str, Any], group_id: str, operator_qq: str, operator_nickname: str | None, message_id: str | None) -> str:
    operator = _operator_or_message(operator_qq, operator_nickname)
    if isinstance(operator, str):
        return operator
    area, member, problem = _load_writable_target(intent)
    if problem:
        return problem
    status_time = normalize_time(intent["status_update"].get("time") or intent.get("_reply_time"))
    if not status_time:
        return "缺少质询/最后警告时间。"
    with connect() as conn:
        state = _state(conn, member["id"], area)
        if state["status"] in LOCKED_STATUSES or state["locked"]:
            return f"{format_member(member)}\n\n状态：{state['status']}，数据已锁定，只允许查询。"
        caused_by_record_id = _explicit_causal_record_id(
            conn,
            intent,
            member_id=member["id"],
            group_area=area,
        )
    ctype = "最后警告" if intent["intent"] == "final_warning" else "质询"
    result = intent["status_update"].get("result") or "通过"
    status_after = normalize_status(result) if result in {"已移出", "已拉黑"} else ("最后警告" if ctype == "最后警告" else "已质询")
    payload = {"member_id": member["id"], "group_area": area, "consultation_type": ctype, "consultation_time": status_time, "result": result, "status_after": status_after}
    if caused_by_record_id is not None:
        payload["caused_by_record_id"] = caused_by_record_id
    _set_pending(group_id, operator_qq, "consultation", payload)
    cause_line = (
        f"\n关联违规记录：#{caused_by_record_id}"
        if caused_by_record_id is not None
        else ""
    )
    return f"{format_member(member)}\n\n群聊：{area}\n{ctype}时间：{display_time(status_time)}\n{ctype}人：{operator['nickname']}\n{ctype}结果：{result}\n状态：{status_after}{cause_line}\n\n请回复“确认”保存，或回复“取消”放弃。"


def _apply_consultation(conn, payload: dict[str, Any], operator: dict[str, Any], message_id: str | None) -> str:
    before = _state(conn, payload["member_id"], payload["group_area"])
    if not _causal_record_is_active(conn, payload):
        _log(
            conn,
            f"{payload['consultation_type']}失败",
            "手动",
            operator,
            payload["member_id"],
            payload["group_area"],
            before,
            before,
            message_id,
            remark="关联违规记录已撤回",
        )
        return "关联的违规记录已撤回，请重新发起状态操作。"
    ts = now_str()
    conn.execute(
        """
        INSERT INTO consultation_records(member_id, group_area, consultation_type, consultation_time, consultant_admin_id,
            result, status_after, remark, created_at, updated_at)
        VALUES(?, ?, ?, ?, ?, ?, ?, '无', ?, ?)
        """,
        (payload["member_id"], payload["group_area"], payload["consultation_type"], payload["consultation_time"], operator["id"], payload["result"], payload["status_after"], ts, ts),
    )
    locked = 1 if payload["status_after"] in LOCKED_STATUSES else 0
    final_time = payload["consultation_time"] if payload["status_after"] == "最后警告" else None
    conn.execute(
        "UPDATE member_group_states SET status=?, locked=?, last_final_warning_time=COALESCE(?, last_final_warning_time), updated_at=? WHERE member_id=? AND group_area=?",
        (payload["status_after"], locked, final_time, ts, payload["member_id"], payload["group_area"]),
    )
    after = _sync_state_counts(conn, payload["member_id"], payload["group_area"])
    operation_log_id = _log(conn, payload["consultation_type"], "手动", operator, payload["member_id"], payload["group_area"], before, after, message_id)
    _enqueue_status_bridge_job(
        conn,
        operation_log_id=operation_log_id,
        member_id=payload["member_id"],
        group_area=payload["group_area"],
        status=payload["status_after"],
        effective_at=payload["consultation_time"],
        caused_by_record_id=payload.get("caused_by_record_id"),
    )
    member = get_member_by_id(payload["member_id"])
    return f"{format_member(member)}\n\n已保存。\n群聊：{payload['group_area']}\n状态：{payload['status_after']}"


def preview_withdraw(intent: dict[str, Any], group_id: str, operator_qq: str, operator_nickname: str | None, message_id: str | None) -> str:
    operator = _operator_or_message(operator_qq, operator_nickname)
    if isinstance(operator, str):
        return operator
    area, member, problem = _load_writable_target(intent)
    if problem:
        return problem
    with connect() as conn:
        state = _state(conn, member["id"], area)
        if state["status"] in LOCKED_STATUSES or state["locked"]:
            return f"{format_member(member)}\n\n状态：{state['status']}，数据已锁定，只允许查询。"
        row = conn.execute(
            "SELECT * FROM violation_records WHERE member_id=? AND group_area=? AND is_withdrawn=0 ORDER BY violation_time DESC, id DESC LIMIT 1",
            (member["id"], area),
        ).fetchone()
    if not row:
        return "查不到违规记录。"
    record = dict(row)
    _set_pending(group_id, operator_qq, "withdraw_latest", {"record_id": record["id"], "member_id": member["id"], "group_area": area})
    return f"{format_member(member)}\n\n将撤回最近记录：\n{display_time(record['violation_time'])}，{record['judgement']}，{record['action']}\n\n请回复“确认”撤回，或回复“取消”放弃。"


def _apply_withdraw(conn, payload: dict[str, Any], operator: dict[str, Any], message_id: str | None) -> str:
    before = _state(conn, payload["member_id"], payload["group_area"])
    changed_at = now_str()
    if CONFIG.deduction_policy_v102_enabled:
        ensure_policy_scope_snapshot(
            conn,
            payload["member_id"],
            payload["group_area"],
            changed_at,
        )
    conn.execute("UPDATE violation_records SET is_withdrawn=1, withdrawn_reason='管理员撤回', updated_at=? WHERE id=?", (changed_at, payload["record_id"]))
    after = _sync_state_counts(conn, payload["member_id"], payload["group_area"])
    _log(conn, "撤回记录", "手动", operator, payload["member_id"], payload["group_area"], before, after, message_id)
    member = get_member_by_id(payload["member_id"])
    return f"{format_member(member)}\n\n已撤回最近一次记录。"


def preview_status_update(intent: dict[str, Any], group_id: str, operator_qq: str, operator_nickname: str | None, message_id: str | None) -> str:
    operator = _operator_or_message(operator_qq, operator_nickname)
    if isinstance(operator, str):
        return operator
    area, member, problem = _load_writable_target(intent)
    if problem:
        return problem
    status = normalize_status(intent["status_update"].get("new_status"))
    if status not in LOCKED_STATUSES:
        return "状态指令只支持：退群 / 移出 / 拉黑。"
    with connect() as conn:
        records = conn.execute("SELECT COUNT(*) AS c FROM violation_records WHERE member_id=? AND group_area=? AND is_withdrawn=0", (member["id"], area)).fetchone()["c"]
        caused_by_record_id = _explicit_causal_record_id(
            conn,
            intent,
            member_id=member["id"],
            group_area=area,
        )
    if not records:
        return "查不到违规记录。"
    payload = {"member_id": member["id"], "group_area": area, "status": status}
    if caused_by_record_id is not None:
        payload["caused_by_record_id"] = caused_by_record_id
    _set_pending(group_id, operator_qq, "status_update", payload)
    cause_line = (
        f"\n关联违规记录：#{caused_by_record_id}"
        if caused_by_record_id is not None
        else ""
    )
    return f"{format_member(member)}\n\n群聊：{area}\n状态将更新为：{status}{cause_line}\n\n请回复“确认”保存，或回复“取消”放弃。"


def _apply_status(conn, payload: dict[str, Any], operator: dict[str, Any], message_id: str | None) -> str:
    before = _state(conn, payload["member_id"], payload["group_area"])
    if not _causal_record_is_active(conn, payload):
        _log(
            conn,
            f"{payload['status'].removeprefix('已')}失败",
            "手动",
            operator,
            payload["member_id"],
            payload["group_area"],
            before,
            before,
            message_id,
            remark="关联违规记录已撤回",
        )
        return "关联的违规记录已撤回，请重新发起状态操作。"
    changed_at = now_str()
    conn.execute(
        "UPDATE member_group_states SET status=?, locked=1, updated_at=? WHERE member_id=? AND group_area=?",
        (payload["status"], changed_at, payload["member_id"], payload["group_area"]),
    )
    after = _sync_state_counts(conn, payload["member_id"], payload["group_area"])
    operation_log_id = _log(conn, payload["status"].removeprefix("已"), "手动", operator, payload["member_id"], payload["group_area"], before, after, message_id)
    _enqueue_status_bridge_job(
        conn,
        operation_log_id=operation_log_id,
        member_id=payload["member_id"],
        group_area=payload["group_area"],
        status=payload["status"],
        effective_at=changed_at,
        caused_by_record_id=payload.get("caused_by_record_id"),
    )
    member = get_member_by_id(payload["member_id"])
    return f"{format_member(member)}\n\n已更新状态：{payload['status']}。"


def preview_unlock(intent: dict[str, Any], group_id: str, operator_qq: str, operator_nickname: str | None, message_id: str | None) -> str:
    operator = _operator_or_message(operator_qq, operator_nickname)
    if isinstance(operator, str):
        return operator
    area, member, problem = _load_writable_target(intent)
    if problem:
        return problem
    _set_pending(group_id, operator_qq, "unlock_member", {"member_id": member["id"], "group_area": area})
    return f"{format_member(member)}\n\n群聊：{area}\n将解除锁定，状态恢复为：正常\n\n请回复“确认”解锁，或回复“取消”放弃。"


def _apply_unlock(conn, payload: dict[str, Any], operator: dict[str, Any], message_id: str | None) -> str:
    before = _state(conn, payload["member_id"], payload["group_area"])
    changed_at = now_str()
    conn.execute(
        "UPDATE member_group_states SET status='正常', locked=0, updated_at=? WHERE member_id=? AND group_area=?",
        (changed_at, payload["member_id"], payload["group_area"]),
    )
    after = _sync_state_counts(conn, payload["member_id"], payload["group_area"])
    operation_log_id = _log(conn, "解锁", "手动", operator, payload["member_id"], payload["group_area"], before, after, message_id)
    _enqueue_status_bridge_job(
        conn,
        operation_log_id=operation_log_id,
        member_id=payload["member_id"],
        group_area=payload["group_area"],
        status="正常",
        effective_at=changed_at,
        caused_by_record_id=payload.get("caused_by_record_id"),
    )
    member = get_member_by_id(payload["member_id"])
    return f"{format_member(member)}\n\n已解锁。"


def automatic_maintenance() -> list[str]:
    if CONFIG.deduction_policy_v102_enabled:
        return []
    messages: list[str] = []
    with connect() as conn:
        states = conn.execute("SELECT * FROM member_group_states WHERE current_count_cache > 0").fetchall()
        for row in states:
            state = dict(row)
            if state["last_effective_violation_time"] and state["last_deduct_time"]:
                last = datetime.strptime(state["last_deduct_time"], "%Y-%m-%d %H:%M:%S")
                if datetime.now() - last >= timedelta(days=7) and state["total_count"] > state["deduct_count"]:
                    before = state
                    conn.execute(
                        "UPDATE member_group_states SET deduct_count=deduct_count+1, last_deduct_time=?, updated_at=? WHERE id=?",
                        (now_str(), now_str(), state["id"]),
                    )
                    after = _sync_state_counts(conn, state["member_id"], state["group_area"])
                    _log(conn, "自动减除", "自动", None, state["member_id"], state["group_area"], before, after, None)
                    messages.append(f"{format_member(get_member_by_id(state['member_id']))}\n\n群聊：{state['group_area']}\n自动减除 1 次，当前次数：{after['current_count_cache']}")
            if state["status"] == "最后警告" and state["last_final_warning_time"]:
                last_fw = datetime.strptime(state["last_final_warning_time"], "%Y-%m-%d %H:%M:%S")
                if datetime.now() - last_fw >= timedelta(days=90):
                    before = state
                    conn.execute("UPDATE member_group_states SET status='已质询', updated_at=? WHERE id=?", (now_str(), state["id"]))
                    after = _sync_state_counts(conn, state["member_id"], state["group_area"])
                    _log(conn, "自动撤销最后警告", "自动", None, state["member_id"], state["group_area"], before, after, None)
                    messages.append(f"{format_member(get_member_by_id(state['member_id']))}\n\n群聊：{state['group_area']}\n已自动撤销最后警告，状态：已质询")
    return messages
