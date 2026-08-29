from __future__ import annotations

from .state import FeatureController


_SWITCH_COMMANDS = {
    "/业务": ("business_enabled", "业务功能"),
    "/聊天": ("chat_enabled", "聊天总开关"),
    "/群聊": ("group_chat_enabled", "群聊功能"),
    "/私聊": ("private_chat_enabled", "私聊功能"),
    "/私聊记忆": ("private_memory_enabled", "私聊持久记忆"),
    "/关系状态": ("relationship_state_enabled", "关系状态"),
    "/记忆治理": ("memory_governance_enabled", "记忆治理"),
    "/模型网关": ("llm_gateway_enabled", "模型网关"),
    "/提示构建": ("prompt_builder_enabled", "提示构建"),
    "/联网搜索": ("web_search_enabled", "联网搜索"),
    "/群员监控": ("hive_member_monitor_enabled", "群员监控"),
}
_MODEL_SWITCH_COMMANDS = frozenset({"/模型切换", "/聊天模型", "/穷鬼模式"})
_GATEWAY_DOMAIN_COMMANDS = {
    "视觉": ("llm_gateway_vision_enabled", "模型网关视觉调用"),
    "私聊记忆": ("llm_gateway_private_memory_enabled", "模型网关私聊记忆调用"),
    "成员记忆": ("llm_gateway_member_memory_enabled", "模型网关成员记忆调用"),
    "聊天": ("llm_gateway_chat_enabled", "模型网关聊天调用"),
    "业务": ("llm_gateway_business_enabled", "模型网关业务调用"),
}
_ALLOWLIST_COMMANDS = {
    "/群聊群": ("group_chat", "群聊群", "群号"),
    "/私聊用户": ("private_chat", "私聊用户", "QQ号"),
}
_COMMAND_PREFIXES = frozenset(
    {"/模块状态", *_SWITCH_COMMANDS, *_MODEL_SWITCH_COMMANDS, *_ALLOWLIST_COMMANDS}
)


def is_control_command(text: str) -> bool:
    parts = text.strip().split(maxsplit=1)
    return bool(parts) and parts[0] in _COMMAND_PREFIXES


def execute_control_command(
    text: str, controller: FeatureController, actor: str
) -> str:
    parts = text.strip().split()
    if not parts:
        return "不支持的模块管理命令。"
    if parts[0] == "/模块状态":
        return _status(controller) if len(parts) == 1 else "用法：/模块状态。"
    if parts[0] == "/模型网关":
        return _set_gateway_switch(parts, controller, actor)
    if parts[0] in _MODEL_SWITCH_COMMANDS:
        return _set_chat_model(parts, controller, actor)
    if parts[0] in _SWITCH_COMMANDS:
        return _set_switch(parts, controller, actor)
    if parts[0] in _ALLOWLIST_COMMANDS:
        return _change_allowlist(parts, controller, actor)
    return "不支持的模块管理命令。"


def _status(controller: FeatureController) -> str:
    state = controller.snapshot()
    business_status = (
        _switch_text(state.business_enabled)
        if controller.business_capable
        else "不可用（纯聊天实例）"
    )
    gateway_business_status = (
        _switch_text(state.llm_gateway_business_enabled)
        if controller.business_capable
        else "不可用（纯聊天实例）"
    )
    return "\n".join(
        (
            f"业务功能：{business_status}",
            f"聊天总开关：{_switch_text(state.chat_enabled)}",
            "群聊功能："
            f"{_switch_text(state.group_chat_enabled)}"
            f"（允许群数：{len(state.group_chat_allowed_group_ids)}）",
            "私聊功能："
            f"{_switch_text(state.private_chat_enabled)}"
            f"（允许用户数：{len(state.private_chat_allowed_user_ids)}）",
            f"私聊持久记忆：{_switch_text(state.private_memory_enabled)}",
            f"关系状态：{_switch_text(state.relationship_state_enabled)}",
            f"记忆治理：{_switch_text(state.memory_governance_enabled)}",
            f"模型网关：{_switch_text(state.llm_gateway_enabled)}",
            "聊天文字模型："
            + (
                "glm-4.7-flash"
                + ("" if controller.economy_provider_available else "（配置不可用）")
                if state.economy_mode_enabled
                else "原模型"
            ),
            f"模型网关视觉调用：{_switch_text(state.llm_gateway_vision_enabled)}",
            "模型网关私聊记忆调用："
            f"{_switch_text(state.llm_gateway_private_memory_enabled)}",
            "模型网关成员记忆调用："
            f"{_switch_text(state.llm_gateway_member_memory_enabled)}",
            f"模型网关聊天调用：{_switch_text(state.llm_gateway_chat_enabled)}",
            f"模型网关业务调用：{gateway_business_status}",
            f"提示构建：{_switch_text(state.prompt_builder_enabled)}",
            f"联网搜索：{_switch_text(state.web_search_enabled)}",
            "群员监控："
            + (
                _switch_text(state.hive_member_monitor_enabled)
                if controller.hive_member_monitor_capable
                else "不可用（未配置）"
            ),
        )
    )


def _set_gateway_switch(
    parts: list[str], controller: FeatureController, actor: str
) -> str:
    if len(parts) == 2 and parts[1] in {"开", "关"}:
        if parts[1] == "关":
            reset_chat_model = controller.snapshot().economy_mode_enabled
            controller.set_switches(
                {"llm_gateway_enabled": False, "economy_mode_enabled": False},
                actor,
            )
            if reset_chat_model:
                return "模型网关已关闭，聊天文字模型已恢复原模型。"
            return "模型网关已关闭。"
        if (
            not controller.primary_provider_available
            and not controller.snapshot().economy_mode_enabled
        ):
            return (
                "模型网关无法开启：当前实例未配置原文字模型；"
                "请先切换到 GLM 或恢复原模型配置。"
            )
        return _set_switch(parts, controller, actor)
    if (
        len(parts) == 3
        and parts[1] in _GATEWAY_DOMAIN_COMMANDS
        and parts[2] in {"开", "关"}
    ):
        field_name, label = _GATEWAY_DOMAIN_COMMANDS[parts[1]]
        enabled = parts[2] == "开"
        if field_name == "llm_gateway_business_enabled" and not controller.business_capable:
            return "业务功能不可用：当前为纯聊天实例。"
        controller.set_switch(field_name, enabled, actor)
        return f"{label}已{'开启' if enabled else '关闭'}。"
    return (
        "用法：/模型网关 开|关，或 /模型网关 "
        "视觉|私聊记忆|成员记忆|聊天|业务 开|关。"
    )


def _set_switch(parts: list[str], controller: FeatureController, actor: str) -> str:
    command = parts[0]
    field_name, label = _SWITCH_COMMANDS[command]
    if len(parts) != 2 or parts[1] not in {"开", "关"}:
        return f"用法：{command} 开|关。"
    enabled = parts[1] == "开"
    if field_name == "business_enabled" and not controller.business_capable:
        return "业务功能不可用：当前为纯聊天实例。"
    if (
        field_name == "hive_member_monitor_enabled"
        and not controller.hive_member_monitor_capable
    ):
        return "群员监控不可用：当前实例未配置监控群和日志群。"
    controller.set_switch(field_name, enabled, actor)
    return f"{label}已{'开启' if enabled else '关闭'}。"


def _set_chat_model(
    parts: list[str], controller: FeatureController, actor: str
) -> str:
    command = parts[0]
    if len(parts) != 2:
        return f"用法：{command} GLM|原模型|状态。"
    value = parts[1].casefold()
    if value == "状态":
        selected = (
            "glm-4.7-flash"
            if controller.snapshot().economy_mode_enabled
            else "原模型"
        )
        return f"当前聊天文字模型：{selected}。"
    if command == "/穷鬼模式" and value in {"开", "关"}:
        value = "glm" if value == "开" else "原模型"
    if value not in {"glm", "原模型"}:
        return f"用法：{command} GLM|原模型|状态。"
    use_glm = value == "glm"
    if use_glm and not controller.economy_provider_available:
        return "模型切换不可用：当前实例未完整配置 GLM 网关。"
    if (
        not use_glm
        and controller.snapshot().economy_mode_enabled
        and not controller.primary_provider_available
    ):
        return (
            "无法切换到原模型：当前实例未配置原文字模型；"
            "请先恢复原模型配置；如需停止当前纯 GLM 实例的模型调用，"
            "可使用 /模型网关 关。"
        )
    controller.set_switch("economy_mode_enabled", use_glm, actor)
    selected = " glm-4.7-flash" if use_glm else "原模型"
    return (
        f"聊天文字模型已切换为{selected}；"
        "图片、记忆和业务模型保持原配置。"
    )


def _change_allowlist(
    parts: list[str], controller: FeatureController, actor: str
) -> str:
    command = parts[0]
    kind, label, id_label = _ALLOWLIST_COMMANDS[command]
    if len(parts) == 2 and parts[1] == "列表":
        return _list_allowlist(kind, label, controller)
    if len(parts) != 3 or parts[1] not in {"添加", "删除"}:
        return f"用法：{command} 添加|删除 <{id_label}>，或 {command} 列表。"

    value = parts[2]
    if not _is_positive_numeric_id(value):
        return f"{id_label}必须为正整数。"
    normalized = str(int(value))
    state = controller.snapshot()
    field_name = (
        "group_chat_allowed_group_ids"
        if kind == "group_chat"
        else "private_chat_allowed_user_ids"
    )
    existing = {str(item) for item in getattr(state, field_name)}
    if parts[1] == "添加":
        if normalized in existing:
            return f"{label}：{normalized} 已在允许列表中。"
        controller.add_allowed(kind, normalized, actor)
        return f"已添加{label}：{normalized}。"
    if normalized not in existing:
        return f"{label}：{normalized} 不在允许列表中。"
    controller.remove_allowed(kind, normalized, actor)
    return f"已删除{label}：{normalized}。"


def _list_allowlist(kind: str, label: str, controller: FeatureController) -> str:
    state = controller.snapshot()
    values = (
        state.group_chat_allowed_group_ids
        if kind == "group_chat"
        else state.private_chat_allowed_user_ids
    )
    if not values:
        return f"{label}允许列表为空。"
    return f"{label}允许列表：{'、'.join(str(value) for value in values)}。"


def _is_positive_numeric_id(value: str) -> bool:
    return value.isascii() and value.isdigit() and int(value) > 0


def _switch_text(enabled: bool) -> str:
    return "开" if enabled else "关"
