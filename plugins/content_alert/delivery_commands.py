from __future__ import annotations

import asyncio
import re

from .service import ContentAlertService

_ID = re.compile(r"KA-[0-9a-f]{12}\Z")
_STATUS = {
    'pending': '等待投递', 'leased': '已认领，尚未发送', 'sending': '发送中',
    'delivered': '已投递', 'delivery_unknown': '投递结果未知，需要人工核对',
    'exhausted': '明确失败已达自动重试上限',
}


async def execute_delivery_command(text: str, service: ContentAlertService, *, actor: str) -> str | None:
    """Caller must have passed the existing superuser/addressing checks.

    Queries disclose only operational metadata, never the persisted report,
    hidden rule generation or source text. Unknown retries require an explicit
    confirmation word because QQ may have accepted the earlier attempt.
    """
    parts = text.strip().split()
    if len(parts) < 2 or parts[0] != '/违禁词' or parts[1] not in {'告警状态', '告警重试', '告警已收'}:
        return None
    if parts[1] == '告警状态':
        if len(parts) == 2:
            rows = await asyncio.to_thread(service.outbox.states)
            if not rows:
                return '告警投递队列为空。'
            response = '告警投递状态：\n' + '\n'.join(f"{_STATUS[row['status']]}：{row['count']}" for row in rows)
            unresolved = await asyncio.to_thread(service.outbox.unresolved)
            if unresolved:
                response += '\n待人工核对（最多列出 10 条）：\n' + '\n'.join(
                    f"{row['alert_id']}：{_STATUS[row['status']]}" for row in unresolved)
            return response
        if len(parts) != 3 or _ID.fullmatch(parts[2]) is None:
            return '用法：/违禁词 告警状态 [KA-告警编号]。'
        rows = await asyncio.to_thread(service.outbox.states, parts[2])
        if not rows:
            return '未找到该告警编号。'
        row = rows[0]
        return f"{row['alert_id']}：{_STATUS[row['status']]}；累计发送尝试 {row['attempt_count']} 次。"
    if len(parts) not in {3, 4} or _ID.fullmatch(parts[2]) is None:
        return '用法：/违禁词 告警重试 KA-告警编号 确认，或 /违禁词 告警已收 KA-告警编号 确认。'
    if len(parts) != 4 or parts[3] != '确认':
        return (
            '请先在管理群按告警编号核对。结果未知的上次发送可能已经成功，重试可能产生重复。'
            f'确认操作请使用：/违禁词 {parts[1]} {parts[2]} 确认。'
        )
    if parts[1] == '告警重试' and not service._runtime_enabled():
        return '告警功能已关闭；先启用告警，再确认重试。现有任务保持不变。'
    action = 'retry' if parts[1] == '告警重试' else 'confirm_delivered'
    changed = await asyncio.to_thread(service.outbox.resolve, parts[2], action=action,
                                      actor=actor, now=float(service._clock()))
    if not changed:
        return '未改变任务：仅结果未知或已达重试上限的告警可人工处理。'
    return f"{parts[2]} 已{'重新排队' if action == 'retry' else '记录为人工核实已收到'}，操作已留痕。"
