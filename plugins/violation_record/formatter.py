from typing import Any

from .member_resolver import format_member
from .validators import display_time, normalize_time


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

    area = (
        "<分区：蜂巢/蜂窝/蜂箱>"
        if "group_area" in missing_set
        else _clean(intent.get("group_area")) or "<分区：蜂巢/蜂窝/蜂箱>"
    )
    nickname_required = bool(
        missing_set & {"target", "target.qq_nickname"}
    )
    nickname = (
        "<成员昵称>"
        if nickname_required
        else _clean(target.get("qq_nickname")) or "<成员昵称>"
    )
    qq_required = "target.qq_number" in missing_set or "target" in missing_set
    qq = None if qq_required else _clean(target.get("qq_number"))
    member = nickname
    if qq:
        member = f"{member}（{qq}）"
    if qq_required:
        member = f"{member}（<QQ号>）"

    if "violation.time" in missing_set:
        reply_time = _clean(intent.get("_reply_time"))
        event_time = (
            reply_time
            if not _clean(violation.get("time")) and normalize_time(reply_time)
            else None
        )
    else:
        event_time = _clean(violation.get("time")) or _clean(
            intent.get("_reply_time")
        )
    event_time = event_time or "<时间，24小时制，如03:30或15:30>"
    judgement = (
        "<违规行为>"
        if "violation.judgement" in missing_set
        else _clean(violation.get("judgement")) or "<违规行为>"
    )
    action = (
        "<处理措施>"
        if "violation.action" in missing_set
        else _clean(violation.get("action")) or "<处理措施>"
    )
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
    if handler_required:
        line += "，<处理人QQ号或昵称>处理"
    elif handler:
        line += f"，{handler}处理"

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


HELP_TEXT = """违规记录机器人帮助

使用前
- 在目标群里 @机器人 后发送指令。
- 数据业务必须写群域：蜂巢 / 蜂窝 / 蜂箱。
- 查询成员可写昵称或 5–12 位 QQ号；昵称支持模糊匹配，多人命中时会列出候选。
- 减数命令只接受 QQ号，不接受昵称。

一、常用查询
成员全部记录：查询 蜂巢 小明
按 QQ号查询：查询 蜂巢 123456
成员最近记录：查询 蜂巢 小明最近违规记录
分区本月记录：查询 蜂巢 本月违规记录
分区最近记录：查询 蜂巢 最近违规记录
导出完整记录：导出蜂巢本月违规记录
说明：多条结果会使用合并转发，每条记录内保留对应证据图片。

二、新增违规记录
完整格式：
蜂巢 小明（123456） 2026/8/18 14:30 刷屏，禁言10分钟，企鹅处理，备注无
引用时间：回复/引用一条消息后发送：
蜂巢 小明（123456） 刷屏，禁言10分钟
说明：未另外填写时间时，会使用被引用消息的时间。

三、群禁言
禁言 @成员 10分钟
把 123456 禁言半小时
说明：只接受 @目标 或 QQ号；未写时长默认 10 分钟。群禁言会立即执行，不进入记录确认流程。

四、状态与记录维护
质询：蜂巢质询123456 2026/8/18 14:30
最后警告：蜂巢最后警告123456 2026/8/18 14:30
撤回最近记录：撤回蜂巢123456记录
终止状态：蜂巢123456退群 / 移出蜂巢123456 / 拉黑蜂巢123456
恢复正常：蜂巢123456解锁

五、减数策略
查成员状态：查询减数状态 蜂巢 123456
查成员日志：查询减数日志 蜂巢 123456
查名单：查询减缓名单 / 查询减停名单
查建议：查询减停建议名单
查待办：查询减数待办
人工减停：减停 蜂巢 123456 事由
清除减停：清除减停 蜂巢 123456 事由
续期减停：续期减停 蜂巢 123456 事由
拒绝建议：拒绝减停建议 蜂巢 123456 事由
说明：写操作必须同时填写群域、QQ号和非空事由。

六、确认与取消
确认保存：确认
放弃操作：取消
说明：除群禁言外，新增记录、状态、撤回、解锁和减数写操作都会先发送预览，需要再发送“确认”才会执行。"""
