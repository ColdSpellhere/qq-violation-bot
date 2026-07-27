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
