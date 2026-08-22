from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Iterable, Sequence


MAX_FACT_LENGTH = 80
MAX_RELATIONSHIP_LENGTH = 600
MAX_REASON_LENGTH = 500
MAX_TOKEN_LENGTH = 256

MEMORY_HELP_TEXT = """记忆治理命令：
/记忆 <QQ号|@群成员>
/记忆 关系 <QQ号|@群成员> [新状态]
/记忆 添加 <QQ号|@群成员> <内容>
/记忆 修改 <G-编号|P-编号> <内容>
/记忆 删除 <G-编号|P-编号>
/记忆 清空 <QQ号>
/记忆 状态
/记忆 确认 <操作码> <原因>
/记忆 取消 <操作码>"""

_FACT_ID_RE = re.compile(r"([GP])-([1-9][0-9]*)", re.ASCII)


class MemoryCommandError(ValueError):
    """A recognized `/记忆` command has invalid or ambiguous arguments."""


@dataclass(frozen=True)
class MemoryScope:
    kind: str
    user_id: str
    group_id: int | None = None


@dataclass(frozen=True)
class MemoryCommand:
    action: str
    scope: MemoryScope | None = None
    content: str = ""
    fact_kind: str | None = None
    memory_id: int | None = None
    token: str = ""
    reason: str = ""

    @property
    def is_write(self) -> bool:
        return self.action in {
            "add_fact",
            "modify_fact",
            "delete_fact",
            "update_relation",
            "clear_private",
        }


def is_memory_command(text: str) -> bool:
    parts = str(text or "").strip().split(maxsplit=1)
    return bool(parts) and parts[0] == "/记忆"


def canonical_memory_command_text(message: object) -> str:
    """Preserve real at-segment positions without trusting CQ/display text."""
    try:
        segments: Sequence[object] = tuple(message)  # type: ignore[arg-type]
    except TypeError:
        return ""
    parts: list[str] = []
    for segment in segments:
        segment_type = getattr(segment, "type", None)
        data = getattr(segment, "data", {})
        if segment_type == "text":
            parts.append(
                str(data.get("text") or "") if hasattr(data, "get") else ""
            )
        elif segment_type == "at":
            parts.append(" @ ")
    return "".join(parts)


def _positive_ascii_id(value: str, *, label: str) -> str:
    if not value.isascii() or not value.isdigit() or int(value) <= 0:
        raise MemoryCommandError(f"{label}必须为 ASCII 正整数。")
    return str(int(value))


def _message_tokens(message: object) -> tuple[tuple[str, ...], tuple[str, ...]]:
    tokens: list[str] = []
    targets: list[str] = []
    try:
        segments: Sequence[object] = tuple(message)  # type: ignore[arg-type]
    except TypeError:
        return (), ()
    for segment in segments:
        segment_type = getattr(segment, "type", None)
        data = getattr(segment, "data", {})
        if segment_type == "text":
            value = str(data.get("text") or "") if hasattr(data, "get") else ""
            tokens.extend(value.split())
            continue
        if segment_type != "at":
            continue
        value = str(data.get("qq") or "") if hasattr(data, "get") else ""
        try:
            targets.append(_positive_ascii_id(value, label="@目标 QQ号"))
            tokens.append(f"\0at:{len(targets) - 1}")
        except MemoryCommandError:
            continue
    return tuple(tokens), tuple(targets)


def _target_scope(
    token: str,
    message: object,
    *,
    target_position: int,
    group_id: int | None,
    private_allowed_user_ids: Iterable[str] | None,
) -> MemoryScope:
    if token.isascii() and token.isdigit():
        user_id = _positive_ascii_id(token, label="私聊 QQ号")
        if private_allowed_user_ids is not None:
            allowed = {str(value) for value in private_allowed_user_ids}
            if user_id not in allowed:
                raise MemoryCommandError("该私聊用户不在现有允许列表中。")
        return MemoryScope("private", user_id)
    if token and token[0] in {"@", "["}:
        message_tokens, targets = _message_tokens(message)
        if (
            len(targets) != 1
            or len(message_tokens) <= target_position
            or message_tokens[target_position] != "\0at:0"
        ):
            raise MemoryCommandError("群成员目标必须使用且只能使用一个真实 @ 消息段。")
        if group_id is not None and (
            isinstance(group_id, bool) or not isinstance(group_id, int) or group_id <= 0
        ):
            raise MemoryCommandError("群号必须为正整数。")
        return MemoryScope("group", targets[0], group_id=group_id)
    raise MemoryCommandError("目标必须是允许列表中的私聊 QQ号或真实 @ 群成员。")


def _fact_reference(value: str) -> tuple[str, int]:
    matched = _FACT_ID_RE.fullmatch(value)
    if matched is None:
        raise MemoryCommandError("记忆编号必须为 G-<正整数> 或 P-<正整数>。")
    return ("group" if matched.group(1) == "G" else "private", int(matched.group(2)))


def _content(value: str, *, maximum: int, label: str) -> str:
    normalized = " ".join(value.split())
    if not normalized:
        raise MemoryCommandError(f"{label}不能为空。")
    if len(normalized) > maximum:
        raise MemoryCommandError(f"{label}不能超过 {maximum} 个字符。")
    return normalized


def parse_memory_command(
    text: str,
    message: object | None = None,
    *,
    group_id: int | None = None,
    private_allowed_user_ids: Iterable[str] | None = None,
) -> MemoryCommand | None:
    source = " ".join(str(text or "").strip().split())
    if not is_memory_command(source):
        return None
    parts = source.split(" ")
    if len(parts) == 1 or parts == ["/记忆", "帮助"]:
        return MemoryCommand("help")
    if parts[1] == "状态":
        if len(parts) != 2:
            raise MemoryCommandError("用法：/记忆 状态。")
        return MemoryCommand("status")
    if parts[1] == "确认":
        if len(parts) < 4:
            raise MemoryCommandError("用法：/记忆 确认 <操作码> <原因>。")
        token = _content(parts[2], maximum=MAX_TOKEN_LENGTH, label="操作码")
        reason = _content(" ".join(parts[3:]), maximum=MAX_REASON_LENGTH, label="原因")
        return MemoryCommand("confirm", token=token, reason=reason)
    if parts[1] == "取消":
        if len(parts) != 3:
            raise MemoryCommandError("用法：/记忆 取消 <操作码>。")
        return MemoryCommand(
            "cancel", token=_content(parts[2], maximum=MAX_TOKEN_LENGTH, label="操作码")
        )
    if parts[1] == "修改":
        if len(parts) < 4:
            raise MemoryCommandError("用法：/记忆 修改 <记忆编号> <内容>。")
        fact_kind, memory_id = _fact_reference(parts[2])
        return MemoryCommand(
            "modify_fact",
            fact_kind=fact_kind,
            memory_id=memory_id,
            content=_content(" ".join(parts[3:]), maximum=MAX_FACT_LENGTH, label="事实内容"),
        )
    if parts[1] == "删除":
        if len(parts) != 3:
            raise MemoryCommandError("用法：/记忆 删除 <记忆编号>。")
        fact_kind, memory_id = _fact_reference(parts[2])
        return MemoryCommand("delete_fact", fact_kind=fact_kind, memory_id=memory_id)
    if parts[1] == "清空":
        if len(parts) != 3:
            raise MemoryCommandError("用法：/记忆 清空 <QQ号>。")
        scope = _target_scope(
            parts[2], message, target_position=2, group_id=group_id,
            private_allowed_user_ids=private_allowed_user_ids,
        )
        if scope.kind != "private":
            raise MemoryCommandError("清空只接受允许列表中的私聊 QQ号。")
        return MemoryCommand("clear_private", scope=scope)
    if parts[1] == "添加":
        if len(parts) < 4:
            raise MemoryCommandError("用法：/记忆 添加 <目标> <内容>。")
        scope = _target_scope(
            parts[2], message, target_position=2, group_id=group_id,
            private_allowed_user_ids=private_allowed_user_ids,
        )
        return MemoryCommand(
            "add_fact", scope=scope,
            content=_content(" ".join(parts[3:]), maximum=MAX_FACT_LENGTH, label="事实内容"),
        )
    if parts[1] == "关系":
        if len(parts) < 3:
            raise MemoryCommandError("用法：/记忆 关系 <目标> [新状态]。")
        scope = _target_scope(
            parts[2], message, target_position=2, group_id=group_id,
            private_allowed_user_ids=private_allowed_user_ids,
        )
        if len(parts) == 3:
            return MemoryCommand("view_relation", scope=scope)
        return MemoryCommand(
            "update_relation", scope=scope,
            content=_content(
                " ".join(parts[3:]), maximum=MAX_RELATIONSHIP_LENGTH, label="关系状态"
            ),
        )
    if len(parts) == 2:
        scope = _target_scope(
            parts[1], message, target_position=1, group_id=group_id,
            private_allowed_user_ids=private_allowed_user_ids,
        )
        return MemoryCommand("view_facts", scope=scope)
    raise MemoryCommandError("不支持或格式不完整的 /记忆 命令。")


__all__ = [
    "canonical_memory_command_text",
    "MemoryCommand",
    "MemoryCommandError",
    "MEMORY_HELP_TEXT",
    "MemoryScope",
    "is_memory_command",
    "parse_memory_command",
]
