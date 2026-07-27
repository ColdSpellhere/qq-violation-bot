import re
from typing import Any

from nonebot import logger
from nonebot.adapters.onebot.v11 import Bot

from .admin_resolver import resolve_operator
from .db import connect, dump_json, now_str
from .validators import format_duration, normalize_duration_seconds


DEFAULT_MUTE_SECONDS = 10 * 60
MAX_MUTE_SECONDS = 30 * 24 * 60 * 60
QQ_NUMBER_RE = re.compile(r"\d{5,12}")


def _clean_qq(value: Any) -> str | None:
    text = str(value or "").strip()
    if not text:
        return None
    match = QQ_NUMBER_RE.search(text)
    return match.group(0) if match else None


def _unique_qqs(values: list[Any]) -> list[str]:
    qqs: list[str] = []
    for value in values:
        qq = _clean_qq(value)
        if qq and qq not in qqs:
            qqs.append(qq)
    return qqs


def _target_from_intent(intent: dict[str, Any], operator_qq: str, bot_self_id: str) -> tuple[str | None, str | None]:
    target = intent.get("target") or {}
    explicit_qq = _clean_qq(target.get("qq_number"))
    if explicit_qq:
        return explicit_qq, None

    mentioned = _unique_qqs(list(intent.get("_mentioned_qq_numbers") or []))
    mentioned = [qq for qq in mentioned if qq not in {operator_qq, bot_self_id}]
    if len(mentioned) == 1:
        return mentioned[0], None
    if len(mentioned) > 1:
        return None, "同时 @ 了多个人，请只 @ 一位要禁言的成员，或直接写对方 QQ号。"

    raw_qqs = _unique_qqs(QQ_NUMBER_RE.findall(str(intent.get("_raw", ""))))
    raw_qqs = [qq for qq in raw_qqs if qq not in {operator_qq, bot_self_id}]
    if len(raw_qqs) == 1:
        return raw_qqs[0], None
    if len(raw_qqs) > 1:
        return None, "识别到多个 QQ号，请明确哪一个是要禁言的成员。"

    nickname = str(target.get("qq_nickname") or "").strip()
    if nickname:
        return None, "禁言操作只支持 @目标 或 QQ号，不使用昵称模糊匹配。"
    return None, "请 @ 被禁言的人，或写出对方 QQ号。"


def _duration_from_intent(intent: dict[str, Any]) -> int:
    moderation = intent.get("moderation") or {}
    candidates = (
        (moderation.get("duration_seconds"), True),
        (moderation.get("duration_text"), False),
        ((intent.get("violation") or {}).get("action"), False),
        (intent.get("_raw"), False),
    )
    for value, allow_bare_number in candidates:
        duration = normalize_duration_seconds(value, allow_bare_number=allow_bare_number)
        if duration:
            return duration
    return DEFAULT_MUTE_SECONDS


def _mute_reason(intent: dict[str, Any]) -> str | None:
    reason = str((intent.get("moderation") or {}).get("reason") or "").strip()
    if reason and reason.lower() not in {"none", "null"}:
        return reason
    return None


async def _call_set_group_ban(bot: Bot, group_id: int, user_id: int, duration: int) -> None:
    set_group_ban = getattr(bot, "set_group_ban", None)
    if set_group_ban:
        await set_group_ban(group_id=group_id, user_id=user_id, duration=duration)
        return
    await bot.call_api("set_group_ban", group_id=group_id, user_id=user_id, duration=duration)


async def _target_display(bot: Bot, group_id: int, user_id: int) -> str:
    try:
        get_group_member_info = getattr(bot, "get_group_member_info", None)
        if get_group_member_info:
            info = await get_group_member_info(group_id=group_id, user_id=user_id, no_cache=False)
        else:
            info = await bot.call_api("get_group_member_info", group_id=group_id, user_id=user_id, no_cache=False)
    except Exception as exc:
        logger.warning(f"获取禁言目标群名片失败 group={group_id} user={user_id}: {exc}")
        return str(user_id)
    if isinstance(info, dict):
        nickname = str(info.get("card") or info.get("nickname") or "").strip()
        if nickname:
            return f"{nickname}（{user_id}）"
    return str(user_id)


def _log_mute(
    operator: dict[str, Any],
    group_id: str,
    target_qq: str,
    duration: int,
    reason: str | None,
    message_id: str | None,
) -> None:
    with connect() as conn:
        member = conn.execute("SELECT id FROM members WHERE qq_number=?", (target_qq,)).fetchone()
        conn.execute(
            """
            INSERT INTO operation_logs(group_area, operation_type, source, operator_qq, operator_nickname,
                target_member_id, before_json, after_json, message_id, created_at, remark)
            VALUES(NULL, '群禁言', '手动', ?, ?, ?, NULL, ?, ?, ?, ?)
            """,
            (
                operator.get("qq_number"),
                operator.get("nickname"),
                member["id"] if member else None,
                dump_json({"qq_group_id": group_id, "target_qq": target_qq, "duration_seconds": duration}),
                message_id,
                now_str(),
                reason or f"QQ群={group_id}；目标={target_qq}；时长={format_duration(duration)}",
            ),
        )


async def handle_mute_intent(
    bot: Bot,
    intent: dict[str, Any],
    group_id: str,
    operator_qq: str,
    operator_nickname: str | None,
    message_id: str | None = None,
) -> str:
    try:
        confidence = float((intent.get("operation") or {}).get("confidence") or 0)
    except (TypeError, ValueError):
        confidence = 0.0
    if confidence < 0.55:
        return "这条禁言操作我理解得不够确定，请明确要禁言的成员和时长。"

    bot_self_id = str(getattr(bot, "self_id", "") or "")
    target_qq, problem = _target_from_intent(intent, operator_qq, bot_self_id)
    if problem:
        return problem
    if target_qq == bot_self_id:
        return "不能禁言机器人自己。"

    duration = _duration_from_intent(intent)
    if duration > MAX_MUTE_SECONDS:
        return "禁言时长不能超过 30 天，请缩短后重试。"

    try:
        await _call_set_group_ban(bot, int(group_id), int(target_qq), duration)
    except Exception as exc:
        logger.warning(f"群禁言失败 group={group_id} target={target_qq} duration={duration}: {exc}")
        return f"禁言失败：{exc}\n请确认机器人是群管理员，目标在本群内，且目标权限低于机器人。"

    operator = resolve_operator(operator_qq, operator_nickname)
    if operator:
        _log_mute(operator, group_id, target_qq, duration, _mute_reason(intent), message_id)

    display = await _target_display(bot, int(group_id), int(target_qq))
    lines = [f"已禁言：{display}", f"时长：{format_duration(duration)}"]
    reason = _mute_reason(intent)
    if reason:
        lines.append(f"原因：{reason}")
    return "\n".join(lines)
