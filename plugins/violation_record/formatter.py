from typing import Any

from .member_resolver import format_member
from .validators import display_time


def admin_name(admin: dict[str, Any] | None, fallback: str | None = None) -> str:
    return (admin or {}).get("nickname") or fallback or "未知管理员"


def violation_detail(record: dict[str, Any], member: dict[str, Any], handler: dict[str, Any] | None, recorder: dict[str, Any] | None) -> str:
    return (
        f"{format_member(member)}\n\n"
        f"时间：{display_time(record.get('violation_time'))}\n"
        f"群聊：{record.get('group_area')}\n"
        f"判定：{record.get('judgement')}\n"
        f"处理措施：{record.get('action')}\n"
        f"处理人：{admin_name(handler)}\n"
        f"记录人：{admin_name(recorder)}\n"
        f"备注：{record.get('remark') or '无'}"
    )


def ambiguous_members(items: list[dict[str, Any]]) -> str:
    lines = ["找到多个可能的成员，请补充 QQ号，或按更准确昵称重新发起：", ""]
    for i, item in enumerate(items, 1):
        lines.append(f"{i}. {format_member(item)}")
    return "\n".join(lines)


def ambiguous_admins(items: list[dict[str, Any]]) -> str:
    lines = ["找到多个可能的管理员，请使用更准确的管理员昵称：", ""]
    for i, item in enumerate(items, 1):
        lines.append(f"{i}. {item.get('nickname')}（{item.get('qq_number')}）")
    return "\n".join(lines)


CORRECTION_LABELS = {
    "group_area": "分区",
    "target": "成员",
    "target.qq_number": "QQ号",
    "target.qq_nickname": "成员昵称",
    "violation.time": "时间",
    "violation.judgement": "违规行为",
    "violation.action": "处理措施",
    "violation.handler_admin_qq": "处理人",
    "violation.handler_admin_nickname": "处理人",
}


def _clean(value: Any) -> str | None:
    text = str(value or "").strip()
    return text or None


def format_create_correction(intent: dict[str, Any], missing_fields: list[str]) -> str:
    missing = list(dict.fromkeys(missing_fields))
    missing_set = set(missing)
    target = intent.get("target") or {}
    violation = intent.get("violation") or {}

    area = _clean(intent.get("group_area")) or "<分区：蜂巢/蜂窝/蜂箱>"
    nickname = _clean(target.get("qq_nickname")) or "<成员昵称>"
    qq = _clean(target.get("qq_number"))
    qq_required = "target.qq_number" in missing_set or "target" in missing_set
    member = nickname
    if qq:
        member = f"{member}（{qq}）"
    elif qq_required:
        member = f"{member}（<QQ号>）"

    event_time = _clean(violation.get("time")) or _clean(intent.get("_reply_time"))
    event_time = event_time or "<时间，24小时制，如03:30或15:30>"
    judgement = _clean(violation.get("judgement")) or "<违规行为>"
    action = _clean(violation.get("action")) or "<处理措施>"
    handler = _clean(violation.get("handler_admin_nickname")) or _clean(
        violation.get("handler_admin_qq")
    )
    handler_required = bool(
        missing_set
        & {
            "violation.handler_admin_qq",
            "violation.handler_admin_nickname",
        }
    )

    line = f"{area} {member} {event_time} {judgement}，{action}"
    if handler:
        line += f"，{handler}处理"
    elif handler_required:
        line += "，<处理人QQ号或昵称>处理"

    labels = list(
        dict.fromkeys(CORRECTION_LABELS.get(field, field) for field in missing)
    )
    notes = [
        "记录人：自动取当前发送者",
        "未写处理人时：默认等于记录人",
        "备注：未写时默认为“无”",
        "证据图片：当前为提醒模式，可不提供",
    ]
    if "target.qq_number" in missing_set:
        notes.insert(0, "QQ号：昵称无法唯一确定时必填")
    if "violation.time" in missing_set:
        notes.insert(0, "当天记录可只写时间；非当天请补日期")

    return "\n".join(
        [
            f"格式缺少：{'、'.join(labels)}",
            "",
            "请替换尖括号内容后重新发送：",
            line,
            "",
            "说明：",
            *[f"- {note}" for note in notes],
        ]
    )


HELP_TEXT = """可用示例：

记录：蜂巢小明（123456）2026/6/14 0:00刷屏，禁言，企鹅处理
群禁言：禁言 @成员 10分钟 / 把 123456 禁言半小时
查询：查蜂巢小明 / 查蜂巢123456
分区：蜂巢本月违规记录 / 蜂巢最近违规记录
最近：查蜂巢123456最近
质询：蜂巢质询123456 2026/6/1 12点
最后警告：蜂巢最后警告123456 2026/6/1 12点
撤回：撤回蜂巢123456记录
状态：蜂巢123456退群 / 移出蜂巢123456 / 拉黑蜂巢123456
解锁：蜂巢123456解锁
导出：导出蜂巢记录 / 导出蜂巢日志
引用：回复/引用一条消息后说“蜂巢小明（123456）刷屏，禁言”，会使用被引用消息时间
确认：确认
取消：取消

记录、查询、状态等数据业务请标明：蜂巢 / 蜂窝 / 蜂箱。"""
