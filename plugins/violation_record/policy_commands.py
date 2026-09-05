from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from nonebot import get_driver

from .admin_resolver import resolve_operator
from .config import CONFIG, GROUP_AREAS, LOCKED_STATUSES
from .db import connect, dump_json, now_str
from .deduction_policy import (
    policy_review_fingerprint,
    resolve_policy_review,
    classify_severity,
    clear_manual_stop,
    reject_stop_suggestion,
    renew_manual_stop,
    start_manual_stop,
)
from .member_resolver import format_member
from .reply_models import RecordMessage, StructuredReply


class PolicyCommandError(ValueError):
    pass


@dataclass(frozen=True)
class PolicyCommand:
    name: str
    group_area: str | None = None
    qq_number: str | None = None
    reason: str | None = None
    pending_action_id: int | None = None
    recovery_mode: str | None = None

    @property
    def is_write(self) -> bool:
        return self.name in {
            "resolve_review",
            "manual_stop",
            "manual_clear",
            "manual_renew",
            "reject_suggestion",
        }


_WRITE_NAMES = {
    "减停": "manual_stop",
    "清除减停": "manual_clear",
    "续期减停": "manual_renew",
    "拒绝减停建议": "reject_suggestion",
}
_LIST_NAMES = {
    "查询减缓名单": "query_slow_list",
    "查询减停名单": "query_stop_list",
    "查询减停建议名单": "query_suggestion_list",
    "查询减数待办": "query_pending",
}
_POLICY_PREFIXES = tuple(
    sorted(
        [*_WRITE_NAMES, *_LIST_NAMES, "查询减数状态", "查询减数日志", "复核减数冲突"],
        key=len,
        reverse=True,
    )
)


def _validate_area_qq(area: str, qq_number: str) -> None:
    if area not in GROUP_AREAS:
        raise PolicyCommandError("群域必须是：蜂巢 / 蜂窝 / 蜂箱。")
    if not re.fullmatch(r"\d{5,12}", qq_number):
        raise PolicyCommandError("QQ号必须是 5 到 12 位数字，不能使用昵称代替。")


def parse_policy_command(text: str) -> PolicyCommand | None:
    source = re.sub(r"\s+", " ", str(text or "").strip())
    if not source:
        return None
    if source in _LIST_NAMES:
        return PolicyCommand(_LIST_NAMES[source])

    review = re.fullmatch(r"复核减数冲突\s+(\S+)\s+(\S+)\s+(\d+)\s+(保留周期|重新计时)\s+(.+)", source)
    if review:
        area,qq,pending_id,mode,reason = review.groups()
        _validate_area_qq(area,qq)
        if int(pending_id) <= 0:
            raise PolicyCommandError("复核待办编号必须为正整数")
        return PolicyCommand("resolve_review",area,qq,reason.strip(),int(pending_id),mode)
    if source.startswith("复核减数冲突"):
        raise PolicyCommandError("格式：复核减数冲突 <群域> <QQ号> <待办编号> <保留周期|重新计时> <事由>")

    write = re.fullmatch(
        r"(减停|清除减停|续期减停|拒绝减停建议)\s+(\S+)\s+(\S+)(?:\s+(.+))?",
        source,
    )
    if write:
        area, qq_number, reason = write.group(2), write.group(3), write.group(4)
        _validate_area_qq(area, qq_number)
        if not str(reason or "").strip():
            raise PolicyCommandError("该操作必须填写非空事由。")
        return PolicyCommand(
            _WRITE_NAMES[write.group(1)],
            area,
            qq_number,
            str(reason).strip(),
        )

    query = re.fullmatch(r"(查询减数状态|查询减数日志)\s+(\S+)\s+(\S+)", source)
    if query:
        area, qq_number = query.group(2), query.group(3)
        _validate_area_qq(area, qq_number)
        return PolicyCommand(
            "query_status" if query.group(1) == "查询减数状态" else "query_logs",
            area,
            qq_number,
        )

    if source.startswith(_POLICY_PREFIXES):
        raise PolicyCommandError(
            "命令格式不完整。写操作格式：命令 <群域> <QQ号> <事由>；"
            "状态/日志查询格式：命令 <群域> <QQ号>。"
        )
    return None


def _member_and_state(conn, command: PolicyCommand) -> tuple[dict[str, Any], dict[str, Any]]:
    member = conn.execute(
        "SELECT * FROM members WHERE qq_number=?", (command.qq_number,)
    ).fetchone()
    if member is None:
        raise PolicyCommandError(f"查不到 QQ号 {command.qq_number} 对应的成员。")
    state = conn.execute(
        """
        SELECT * FROM member_group_states
        WHERE member_id=? AND group_area=?
        """,
        (member["id"], command.group_area),
    ).fetchone()
    if state is None:
        raise PolicyCommandError(
            f"{format_member(dict(member))} 在 {command.group_area} 没有成员状态。"
        )
    return dict(member), dict(state)


def _policy_context(conn, member_id: int, group_area: str) -> tuple[Any, Any]:
    policy = conn.execute(
        """
        SELECT * FROM v102_policy_state
        WHERE member_id=? AND group_area=?
        """,
        (member_id, group_area),
    ).fetchone()
    cycle = None
    if policy and policy["active_cycle_id"]:
        cycle = conn.execute(
            "SELECT * FROM v102_policy_cycles WHERE id=?",
            (policy["active_cycle_id"],),
        ).fetchone()
    return policy, cycle


def _validate_write_context(
    conn, command: PolicyCommand, state: dict[str, Any], policy: Any, cycle: Any
) -> None:
    if state["status"] in LOCKED_STATUSES:
        raise PolicyCommandError("成员已处于终止状态，不允许执行减停写操作。")
    if state["status"] == "最后警告" or (cycle and cycle["cycle_type"] == "final_warning"):
        raise PolicyCommandError("最后警告 90 天观察期不允许使用普通减停命令。")
    if policy is None:
        raise PolicyCommandError("该成员尚未初始化 v1.0.2beta 减数状态。")

    if command.name == "manual_stop":
        if cycle and cycle["cycle_type"] == "stop":
            raise PolicyCommandError("成员已经处于普通减停，不允许重复减停。")
        return
    if command.name in {"manual_clear", "manual_renew"}:
        if not cycle or cycle["cycle_type"] != "stop" or cycle["status"] != "pending_decision":
            raise PolicyCommandError("当前没有已经到期并等待管理决定的普通减停。")
        if command.name == "manual_clear" and (
            int(cycle["light_count"] or 0) > 1
            or int(cycle["severe_count"] or 0) > 0
        ):
            raise PolicyCommandError("本减停周期评价不良，不允许清除减停。")
        return
    if command.name == "reject_suggestion":
        pending = conn.execute(
            """
            SELECT id FROM v102_pending_actions
            WHERE member_id=? AND group_area=?
              AND action_type='stop_suggestion' AND status='pending'
            LIMIT 1
            """,
            (state["member_id"], state["group_area"]),
        ).fetchone()
        if not pending or not cycle or cycle["cycle_type"] not in {"normal", "slow"}:
            raise PolicyCommandError("当前没有可拒绝的减停建议。")


_PENDING_TYPES = {
    "resolve_review": "v102_resolve_review",
    "manual_stop": "v102_manual_stop",
    "manual_clear": "v102_manual_clear",
    "manual_renew": "v102_manual_renew",
    "reject_suggestion": "v102_reject_suggestion",
}
_OPERATION_COMMAND_NAMES = {
    value: key for key, value in _PENDING_TYPES.items()
}
_WRITE_LABELS = {
    "resolve_review": "复核减数冲突",
    "manual_stop": "减停",
    "manual_clear": "清除减停",
    "manual_renew": "续期减停",
    "reject_suggestion": "拒绝减停建议",
}
_COMMAND_PENDING_ACTIONS = {
    "manual_stop": "stop_suggestion",
    "manual_clear": "stop_decision",
    "manual_renew": "stop_decision",
    "reject_suggestion": "stop_suggestion",
}


def _review_operator_allowed(qq_number: str) -> bool:
    try:
        return str(qq_number) in {str(item) for item in get_driver().config.superusers}
    except (ValueError, AttributeError):
        return False


def _preview_review_command(command: PolicyCommand, *, group_id: str, operator_qq: str,
                            message_id: str | None) -> str:
    if not _review_operator_allowed(operator_qq):
        return "仅配置的机器人管理员可以复核减数冲突。"
    try:
        with connect() as conn:
            member,state = _member_and_state(conn,command)
            policy,cycle = _policy_context(conn,member["id"],command.group_area)
            pending = conn.execute("""SELECT * FROM v102_pending_actions WHERE id=? AND member_id=?
                AND group_area=? AND action_type='replay_review' AND status='pending'""",
                (command.pending_action_id,member["id"],command.group_area)).fetchone()
            if pending is None or policy is None:
                raise PolicyCommandError("查不到该成员群域对应的待复核冲突，请重新查询减数待办。")
            records = conn.execute("""SELECT action FROM violation_records WHERE member_id=? AND group_area=?
                AND is_withdrawn=0 AND is_test=0 AND is_countable=1""",(member["id"],command.group_area)).fetchall()
            severe = sum(classify_severity(row["action"]).value == "severe" for row in records)
            fingerprint = policy_review_fingerprint(conn,member["id"],command.group_area)
    except PolicyCommandError as exc:
        return str(exc)
    payload={"member_id":member["id"],"group_area":command.group_area,"qq_number":command.qq_number,
        "pending_action_id":command.pending_action_id,"recovery_mode":command.recovery_mode,
        "reason":command.reason,"message_id":message_id,"fingerprint":fingerprint,
        "operator_qq":str(operator_qq),"group_id":str(group_id)}
    from .service import _set_pending
    _set_pending(group_id,operator_qq,"v102_resolve_review",payload)
    cycle_text=f"{cycle['cycle_type']}（{cycle['start_at']} 至 {cycle['due_at']}，{cycle['status']}）" if cycle else "无活动周期"
    behavior=("保持原起止时间；已经到期的自动周期可能在确认后补结算。" if command.recovery_mode == "保留周期"
        else "从确认时重新计时；保留已有减停/最后警告状态和已执行减数。")
    return (f"{format_member(member)}\n\n复核待办：#{pending['id']}\n群域：{command.group_area}\n"
        f"冲突：{pending['reason']}\n当前状态：{state['status']}\n当前周期：{cycle_text}\n"
        f"已执行减数：{state['deduct_count']}；策略操作：{policy['v102_operation_count']}/5\n"
        f"有效记录：{len(records)} 条，其中严重禁言 {severe} 条。\n"
        f"恢复方式：{command.recovery_mode}。{behavior}\n"
        "确认将保留合法记录和既有人工决定，将当前证据作为新的回放起点；不自动重判旧决定。\n"
        f"事由：{command.reason}\n\n请回复“确认”保存，或回复“取消”放弃。")


def _apply_review_command(payload: dict[str,Any], operator: dict[str,Any], message_id: str | None) -> str:
    actor=str(operator.get("qq_number") or "")
    if not _review_operator_allowed(actor) or actor != str(payload.get("operator_qq") or ""):
        return "复核未执行：机器人管理员权限已变化或确认人与预览不一致。"
    at=now_str()
    before = None
    try:
        with connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            before={"state":dict(conn.execute("SELECT * FROM member_group_states WHERE member_id=? AND group_area=?",
                (payload["member_id"],payload["group_area"])).fetchone()),"pending_action_id":payload["pending_action_id"],
                "fingerprint":payload["fingerprint"]}
            outcome=resolve_policy_review(conn,member_id=payload["member_id"],group_area=payload["group_area"],
                pending_action_id=int(payload["pending_action_id"]),recovery_mode=payload["recovery_mode"],
                expected_fingerprint=payload["fingerprint"],effective_at=at,reason=payload["reason"],actor_qq=actor,
                idempotency_key=f"confirm:review:{payload.get('message_id') or message_id}:{payload['pending_action_id']}")
            if not outcome.changed:
                return f"此复核已处理，策略事件：#{outcome.event_id}。"
            after=dict(conn.execute("SELECT * FROM member_group_states WHERE member_id=? AND group_area=?",
                (payload["member_id"],payload["group_area"])).fetchone())
            conn.execute("""INSERT INTO operation_logs(group_area,operation_type,source,operator_qq,operator_nickname,
                target_member_id,before_json,after_json,message_id,created_at,remark)
                VALUES(?,'复核减数冲突','手动',?,?,?,?,?,?,?,?)""",
                (payload["group_area"],actor,operator.get("nickname"),payload["member_id"],dump_json(before),
                 dump_json({"state":after,"event_id":outcome.event_id,"recovery_mode":payload["recovery_mode"]}),
                 message_id,at,payload["reason"]))
    except (ValueError, TypeError, KeyError) as exc:
        with connect() as conn:
            conn.execute("""INSERT INTO operation_logs(group_area,operation_type,source,operator_qq,operator_nickname,
                target_member_id,before_json,after_json,message_id,created_at,remark)
                VALUES(?,'复核减数冲突失败','手动',?,?,?,?,?,?,?,?)""",
                (payload.get("group_area"),actor,operator.get("nickname"),payload.get("member_id"),dump_json(before),
                 dump_json({"result":"rejected","reason":str(exc)}),message_id,at,payload.get("reason")))
        return f"复核未执行：{exc}。请重新查询待办并生成预览。"
    return (f"已复核减数冲突 #{payload['pending_action_id']}。\n群域：{payload['group_area']}；QQ号：{payload['qq_number']}\n"
        f"恢复方式：{payload['recovery_mode']}；保留既有人工决定与合法记录。\n策略事件：#{outcome.event_id}")


def preview_policy_command(
    command: PolicyCommand,
    *,
    group_id: str,
    operator_qq: str,
    operator_nickname: str | None,
    message_id: str | None,
) -> str:
    if not command.is_write:
        raise PolicyCommandError("该命令不是写操作。")
    if not CONFIG.deduction_policy_v102_enabled:
        return "v1.0.2beta 减数策略当前未启用。"
    if command.name == "resolve_review":
        return _preview_review_command(command,group_id=group_id,operator_qq=operator_qq,message_id=message_id)
    operator = resolve_operator(operator_qq, operator_nickname)
    if not operator:
        return "无法登记当前操作人，请联系维护者查看 admins 表。"
    try:
        with connect() as conn:
            member, state = _member_and_state(conn, command)
            policy, cycle = _policy_context(conn, member["id"], command.group_area)
            _validate_write_context(conn, command, state, policy, cycle)
            pending_action = conn.execute(
                """
                SELECT id, caused_by_event_id
                FROM v102_pending_actions
                WHERE member_id=? AND group_area=? AND action_type=?
                  AND status='pending'
                ORDER BY id LIMIT 1
                """,
                (
                    member["id"],
                    command.group_area,
                    _COMMAND_PENDING_ACTIONS[command.name],
                ),
            ).fetchone()
    except PolicyCommandError as exc:
        return str(exc)

    from .service import _set_pending

    payload = {
        "member_id": member["id"],
        "group_area": command.group_area,
        "qq_number": command.qq_number,
        "reason": command.reason,
        "message_id": message_id,
        "pending_action_id": int(pending_action["id"]) if pending_action else None,
        "caused_by_event_id": (
            int(pending_action["caused_by_event_id"])
            if pending_action and pending_action["caused_by_event_id"] is not None
            else None
        ),
    }
    _set_pending(group_id, operator_qq, _PENDING_TYPES[command.name], payload)
    cycle_text = (
        f"{cycle['cycle_type']}（{cycle['start_at']} 至 {cycle['due_at']}）"
        if cycle
        else "无活动周期"
    )
    return (
        f"{format_member(member)}\n\n"
        f"群聊：{command.group_area}\n"
        f"当前状态：{state['status']}\n"
        f"当前标签：{policy['policy_tag']}\n"
        f"当前周期：{cycle_text}\n"
        f"拟执行：{_WRITE_LABELS[command.name]}\n"
        f"事由：{command.reason}\n\n"
        "请回复“确认”保存，或回复“取消”放弃。"
    )


def _record_messages(title: str, lines: list[str]) -> StructuredReply | str:
    if not lines:
        return f"{title}\n\n无记录。"
    records = []
    for index, line in enumerate(lines, 1):
        text = f"{title}\n\n{index}. {line}" if index == 1 else f"{index}. {line}"
        records.append(RecordMessage(text))
    return StructuredReply(tuple(records))


def _query_status(conn, command: PolicyCommand) -> str:
    member, state = _member_and_state(conn, command)
    policy, cycle = _policy_context(conn, member["id"], command.group_area)
    if policy is None:
        return f"{format_member(member)}\n\n{command.group_area} 尚未初始化减数策略。"
    cycle_text = "无"
    counts = "轻度 0，严重 0"
    if cycle:
        cycle_text = (
            f"{cycle['cycle_type']}，{cycle['start_at']} 至 {cycle['due_at']}，"
            f"状态 {cycle['status']}"
        )
        counts = f"轻度 {cycle['light_count']}，严重 {cycle['severe_count']}"
    return (
        f"{format_member(member)}\n\n"
        f"群聊：{command.group_area}\n"
        f"状态：{state['status']}\n"
        f"标签：{policy['policy_tag']}\n"
        f"当前次数：{state['current_count_cache']}\n"
        f"减数操作次数：{policy['v102_operation_count']}/5\n"
        f"减缓等级：{policy['slow_level']}\n"
        f"周期：{cycle_text}\n"
        f"本周期：{counts}\n"
        f"无周期原因：{policy['no_cycle_reason'] or '无'}\n"
        f"待办：{policy['pending_action_type'] or '无'}\n"
        f"最近事由：{policy['last_reason'] or '无'}\n"
        f"最近事件：{policy['last_processed_event_id'] or '无'}"
    )


def query_policy_command(command: PolicyCommand) -> str | StructuredReply:
    if command.is_write:
        raise PolicyCommandError("写操作必须先生成确认预览。")
    if not CONFIG.deduction_policy_v102_enabled:
        return "v1.0.2beta 减数策略当前未启用。"
    with connect() as conn:
        if command.name == "query_status":
            return _query_status(conn, command)
        if command.name == "query_logs":
            member, _ = _member_and_state(conn, command)
            rows = conn.execute(
                """
                SELECT * FROM v102_policy_events
                WHERE member_id=? AND group_area=?
                ORDER BY effective_time, event_priority, source_sequence, id
                """,
                (member["id"], command.group_area),
            ).fetchall()
            lines: list[str] = [
                (
                    f"事件#{row['id']} {row['effective_time']} "
                    f"{row['event_type']} 有效={row['is_effective']} "
                    f"规则={row['rule_version']} 详情={row['payload_json']}"
                )
                for row in rows
            ]
            operation_rows = conn.execute(
                """
                SELECT * FROM operation_logs
                WHERE target_member_id=? AND group_area=?
                ORDER BY created_at, id
                """,
                (member["id"], command.group_area),
            ).fetchall()
            policy_labels = (
                "减停",
                "清除减停",
                "续期减停",
                "拒绝减停建议",
                "复核减数冲突",
            )
            for row in operation_rows:
                if not str(row["operation_type"]).startswith(policy_labels):
                    continue
                operator_name = row["operator_nickname"] or "未知操作人"
                operator_qq = row["operator_qq"] or "未知QQ"
                lines.append(
                    f"人工操作#{row['id']} {row['created_at']} "
                    f"{row['operation_type']} 操作人={operator_name}（{operator_qq}） "
                    f"事由={row['remark'] or '无'} "
                    f"操作前={row['before_json'] or 'null'} "
                    f"操作后={row['after_json'] or 'null'}"
                )
            status_jobs = conn.execute(
                """
                SELECT j.*, l.operator_qq, l.operator_nickname, l.remark
                FROM v102_status_bridge_jobs j
                JOIN operation_logs l ON l.id=j.operation_log_id
                WHERE j.member_id=? AND j.group_area=?
                ORDER BY j.created_at, j.id
                """,
                (member["id"], command.group_area),
            ).fetchall()
            for row in status_jobs:
                lines.append(
                    f"状态联动作业#{row['id']} {row['effective_at']} "
                    f"目标={row['target_status']} 状态={row['job_status']} "
                    f"尝试={row['attempt_count']} 策略事件={row['applied_event_id'] or '无'} "
                    f"操作人={row['operator_nickname'] or '未知操作人'}"
                    f"（{row['operator_qq'] or '未知QQ'}） "
                    f"事由={row['remark'] or '无'} 错误={row['last_error'] or '无'}"
                )
            attempts = conn.execute(
                """
                SELECT a.*, o.message_type, o.reminder_slot
                FROM v102_notification_attempts a
                JOIN v102_notification_outbox o ON o.id=a.outbox_id
                WHERE o.member_id=? AND o.group_area=?
                ORDER BY a.started_at, a.outbox_id, a.attempt_number
                """,
                (member["id"], command.group_area),
            ).fetchall()
            for row in attempts:
                lines.append(
                    f"通知尝试#{row['id']} {row['started_at']} "
                    f"类型={row['message_type']} 时段={row['reminder_slot'] or '即时'} "
                    f"序号={row['attempt_number']} 结果={row['status']} "
                    f"完成={row['finished_at'] or '未完成'} 详情={row['detail'] or '无'}"
                )
            return _record_messages(
                f"{format_member(member)} {command.group_area} 减数日志", lines
            )

        where = ""
        params: tuple[Any, ...] = ()
        title = "减数名单"
        if command.name == "query_slow_list":
            where = "WHERE s.policy_tag='slow'"
            title = "减缓名单"
        elif command.name == "query_stop_list":
            where = "WHERE s.policy_tag='stop'"
            title = "减停名单"
        elif command.name == "query_suggestion_list":
            where = (
                "JOIN v102_pending_actions p ON p.member_id=s.member_id "
                "AND p.group_area=s.group_area "
                "WHERE p.action_type='stop_suggestion' AND p.status='pending'"
            )
            title = "减停建议名单"
        elif command.name == "query_pending":
            rows = conn.execute(
                """
                SELECT p.*, m.qq_number, m.qq_nickname
                FROM v102_pending_actions p
                JOIN members m ON m.id=p.member_id
                WHERE p.status='pending'
                ORDER BY p.group_area, m.qq_number, p.id
                """
            ).fetchall()
            return _record_messages(
                "减数待办",
                [
                    f"{row['qq_nickname'] or '未知昵称'}（{row['qq_number']}） "
                    f"{row['group_area']} 待办#{row['id']} {row['action_type']} 事件#{row['caused_by_event_id']}：{row['reason']}"
                    for row in rows
                ],
            )
        else:
            raise PolicyCommandError("未知减数查询命令。")

        rows = conn.execute(
            f"""
            SELECT DISTINCT s.*, m.qq_number, m.qq_nickname,
                g.status, g.current_count_cache
            FROM v102_policy_state s
            JOIN members m ON m.id=s.member_id
            JOIN member_group_states g
              ON g.member_id=s.member_id AND g.group_area=s.group_area
            {where}
            ORDER BY s.group_area, m.qq_number
            """,
            params,
        ).fetchall()
        lines = [
            f"{row['qq_nickname'] or '未知昵称'}（{row['qq_number']}） "
            f"{row['group_area']} 状态={row['status']} 标签={row['policy_tag']} "
            f"当前次数={row['current_count_cache']} 操作次数={row['v102_operation_count']}/5 "
            f"减缓等级={row['slow_level']}"
            for row in rows
        ]
        return _record_messages(title, lines)


def handle_policy_text(
    text: str,
    *,
    group_id: str,
    operator_qq: str,
    operator_nickname: str | None,
    message_id: str | None,
) -> str | StructuredReply | None:
    command = parse_policy_command(text)
    if command is None:
        return None
    if command.is_write:
        return preview_policy_command(
            command,
            group_id=group_id,
            operator_qq=operator_qq,
            operator_nickname=operator_nickname,
            message_id=message_id,
        )
    return query_policy_command(command)


def apply_pending_policy_command(
    operation_type: str,
    payload: dict[str, Any],
    operator: dict[str, Any],
    message_id: str | None,
) -> str:
    if not CONFIG.deduction_policy_v102_enabled:
        return "v1.0.2beta 减数策略当前未启用，未执行操作。"
    if operation_type == "v102_resolve_review":
        return _apply_review_command(payload,operator,message_id)
    action_names = {
        "v102_manual_stop": ("减停", start_manual_stop),
        "v102_manual_clear": ("清除减停", clear_manual_stop),
        "v102_manual_renew": ("续期减停", renew_manual_stop),
        "v102_reject_suggestion": ("拒绝减停建议", reject_stop_suggestion),
    }
    action = action_names.get(operation_type)
    if action is None:
        return "未知减数待确认操作，已取消。"
    label, handler = action
    at = now_str()
    stable_message_id = payload.get("message_id") or message_id or at
    idempotency_key = (
        f"confirm:{stable_message_id}:{operation_type}:"
        f"{payload['member_id']}:{payload['group_area']}"
    )
    before: dict[str, Any] | None = None
    try:
        with connect() as conn:
            member = conn.execute(
                "SELECT * FROM members WHERE id=?", (payload["member_id"],)
            ).fetchone()
            if member is None:
                return "目标成员不存在，未执行操作。"
            before_state = conn.execute(
                """
                SELECT * FROM member_group_states
                WHERE member_id=? AND group_area=?
                """,
                (payload["member_id"], payload["group_area"]),
            ).fetchone()
            before_policy, before_cycle = _policy_context(
                conn, payload["member_id"], payload["group_area"]
            )
            before = {
                "state": dict(before_state) if before_state else None,
                "policy": dict(before_policy) if before_policy else None,
                "cycle": dict(before_cycle) if before_cycle else None,
            }
            command_name = _OPERATION_COMMAND_NAMES[operation_type]
            _validate_write_context(
                conn,
                PolicyCommand(
                    command_name,
                    payload["group_area"],
                    payload.get("qq_number"),
                    payload["reason"],
                ),
                dict(before_state),
                before_policy,
                before_cycle,
            )
            pending_action_id = payload.get("pending_action_id")
            caused_by_event_id = payload.get("caused_by_event_id")
            if pending_action_id is not None:
                current_pending = conn.execute(
                    """
                    SELECT * FROM v102_pending_actions
                    WHERE id=? AND member_id=? AND group_area=?
                    """,
                    (
                        pending_action_id,
                        payload["member_id"],
                        payload["group_area"],
                    ),
                ).fetchone()
                if (
                    current_pending is None
                    or current_pending["status"] != "pending"
                    or current_pending["caused_by_event_id"] != caused_by_event_id
                ):
                    raise PolicyCommandError(
                        "待办状态已变化，请重新执行原命令生成确认预览。"
                    )
            outcome = handler(
                conn,
                member_id=payload["member_id"],
                group_area=payload["group_area"],
                effective_at=at,
                reason=payload["reason"],
                idempotency_key=idempotency_key,
                caused_by_event_id=caused_by_event_id,
            )
            after_state = conn.execute(
                """
                SELECT * FROM member_group_states
                WHERE member_id=? AND group_area=?
                """,
                (payload["member_id"], payload["group_area"]),
            ).fetchone()
            after_policy, after_cycle = _policy_context(
                conn, payload["member_id"], payload["group_area"]
            )
            after = {
                "state": dict(after_state) if after_state else None,
                "policy": dict(after_policy) if after_policy else None,
                "cycle": dict(after_cycle) if after_cycle else None,
                "event_id": outcome.event_id,
            }
            conn.execute(
                """
                INSERT INTO operation_logs(
                    group_area, operation_type, source, operator_qq,
                    operator_nickname, target_member_id, before_json,
                    after_json, message_id, created_at, remark
                ) VALUES(?, ?, '手动', ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    payload["group_area"],
                    label,
                    operator.get("qq_number"),
                    operator.get("nickname"),
                    payload["member_id"],
                    dump_json(before),
                    dump_json(after),
                    message_id,
                    at,
                    payload["reason"],
                ),
            )
            member_dict = dict(member)
    except (PolicyCommandError, ValueError) as exc:
        with connect() as conn:
            conn.execute(
                """
                INSERT INTO operation_logs(
                    group_area, operation_type, source, operator_qq,
                    operator_nickname, target_member_id, before_json,
                    after_json, message_id, created_at, remark
                ) VALUES(?, ?, '手动', ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    payload.get("group_area"),
                    f"{label}失败",
                    operator.get("qq_number"),
                    operator.get("nickname"),
                    payload.get("member_id"),
                    dump_json(before),
                    dump_json({"result": "rejected", "reason": str(exc)}),
                    message_id,
                    at,
                    f"{payload.get('reason') or '无'}；拒绝原因：{exc}",
                ),
            )
        return f"{label}未执行：{exc}"
    return (
        f"{format_member(member_dict)}\n\n"
        f"已执行{label}。\n"
        f"群聊：{payload['group_area']}\n"
        f"事由：{payload['reason']}\n"
        f"策略事件：#{outcome.event_id}"
    )


def log_cancelled_policy_command(
    operation_type: str,
    payload: dict[str, Any],
    *,
    operator_qq: str,
    expired: bool,
) -> None:
    command_name = _OPERATION_COMMAND_NAMES.get(operation_type)
    if command_name is None:
        return
    label = _WRITE_LABELS[command_name]
    operator = resolve_operator(operator_qq, None) or {"qq_number": operator_qq}
    at = now_str()
    with connect() as conn:
        policy, cycle = _policy_context(
            conn, payload["member_id"], payload["group_area"]
        )
        state = conn.execute(
            """
            SELECT * FROM member_group_states
            WHERE member_id=? AND group_area=?
            """,
            (payload["member_id"], payload["group_area"]),
        ).fetchone()
        before = {
            "pending_payload": payload,
            "state": dict(state) if state else None,
            "policy": dict(policy) if policy else None,
            "cycle": dict(cycle) if cycle else None,
        }
        result = "expired" if expired else "cancelled"
        conn.execute(
            """
            INSERT INTO operation_logs(
                group_area, operation_type, source, operator_qq,
                operator_nickname, target_member_id, before_json,
                after_json, message_id, created_at, remark
            ) VALUES(?, ?, '手动', ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                payload["group_area"],
                f"{label}{'过期' if expired else '取消'}",
                operator.get("qq_number"),
                operator.get("nickname"),
                payload["member_id"],
                dump_json(before),
                dump_json({"result": result}),
                payload.get("message_id"),
                at,
                payload.get("reason") or "无",
            ),
        )
