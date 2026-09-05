from __future__ import annotations

from .rules import KeywordRuleStore

_USAGE = (
    "用法：/违禁词 添加 <关键词>、/违禁词 删除 <编号>、/违禁词 列表。\n"
    "投递核对：/违禁词 告警状态 [KA-编号]；"
    "/违禁词 告警重试 KA-编号 确认；/违禁词 告警已收 KA-编号 确认。"
)


def is_keyword_command(text: str) -> bool:
    normalized = str(text).strip()
    return normalized == "/违禁词" or normalized.startswith("/违禁词 ")


def execute_keyword_command(
    text: str,
    store: KeywordRuleStore,
    *,
    actor: object,
) -> str:
    normalized = str(text).strip()
    if not is_keyword_command(normalized):
        return _USAGE

    parts = normalized.split(maxsplit=2)
    if len(parts) == 2 and parts[1] == "列表":
        rules = store.snapshot()
        if not rules:
            return "违禁词列表为空。"
        return "违禁词列表：\n" + "\n".join(
            f"{rule.rule_id}：{rule.pattern}" for rule in rules
        )

    if len(parts) == 3 and parts[1] == "添加":
        try:
            rule = store.add(parts[2], actor=actor)
        except (OSError, TypeError, ValueError) as exc:
            return f"添加失败，规则未改变：{_safe_error(exc)}。"
        return f"已添加违禁词 {rule.rule_id}：{rule.pattern}。"

    if len(parts) == 3 and parts[1] == "删除":
        try:
            rule = store.remove(parts[2], actor=actor)
        except (KeyError, OSError, TypeError, ValueError) as exc:
            return f"删除失败，规则未改变：{_safe_error(exc)}。"
        return f"已删除违禁词 {rule.rule_id}：{rule.pattern}。"

    return _USAGE


def _safe_error(exc: BaseException) -> str:
    if isinstance(exc, KeyError):
        return "找不到该编号"
    message = str(exc)
    if "already exists" in message:
        return "关键词已存在"
    if "length" in message:
        return "关键词规范化后长度须为 2–64 个字符"
    if "control character" in message:
        return "关键词含不允许的控制字符"
    if "limit" in message:
        return "关键词数量已达上限"
    return "保存失败"


__all__ = ("execute_keyword_command", "is_keyword_command")
