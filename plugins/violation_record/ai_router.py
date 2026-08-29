import json
import re
from datetime import datetime
from typing import Any

import httpx

from plugins.llm_gateway import get_gateway
from plugins.llm_gateway.errors import GatewayError

from .config import CONFIG
from .schemas import DEFAULT_INTENT, merge_default


SYSTEM_PROMPT = """
你是 QQ 群违规记录机器人的意图解析器。只输出 JSON，不要输出 Markdown。
你只负责理解管理员自然语言，抽取字段，不做最终业务判断。
群聊分区只允许：蜂巢、蜂窝、蜂箱。它们不是 QQ 群号。
如果用户表达确认/取消/帮助，也要识别。
如果用户表达“现在就在当前 QQ 群禁言/口球/闭麦/让某人安静一会儿/关小黑屋”等群管理动作，intent 应为 mute_member，不要求 group_area。
如果用户是在记录违规事件，尤其包含蜂巢/蜂窝/蜂箱、违规原因、时间、处理措施等，intent 仍应为 create_violation；记录违规中的禁言但未写时长，violation.action 写“禁言10分钟”。
mute_member 的 target.qq_number 只从文本中的 QQ号抽取；如果目标通过 @ 提供但文本里没有 QQ号，target 保持 null，后端会读取消息 @ 段补齐。
mute_member 的 moderation.duration_seconds 必须是禁言时长秒数；未写时长默认 600。duration_text 可保留原文，例如“半小时”“10分钟”“一天”。reason 可填写禁言原因，没有则 null。
如果用户只是询问能不能禁言、假设性讨论、否定禁言（如“别禁言”“不要禁言”），不要输出 mute_member。
如果出现退群、移出、拉黑、解锁、撤回、质询、最后警告、最近、导出等语义，识别对应 intent。
如果用户要查看某个分区的全部/本月/本周/今天/最近违规记录，例如“蜂巢本月违规记录”“查蜂窝最近违规记录”，这不是成员查询，不需要 QQ号，intent 应为 query_area_records，target 保持 null。
如果用户说“导出蜂巢违规记录”“导出蜂巢本月违规记录”，intent 应为 export，target 保持 null。
query.time_range 可写：today | yesterday | current_week | current_month | recent_days | all | null。
记录人不是抽取字段；记录人固定为发送当前消息的管理员，由后端按发送者 QQ号识别。
处理人是实际执行禁言/警告/移出等处理的人。只有管理员明确写出“某人处理/某人禁言/由某人操作”时才填写处理人字段。
如果管理员没有明确指定处理人，handler_admin_qq 和 handler_admin_nickname 都保持 null，后端会默认处理人=记录人。
如果文本暗示处理人不是记录人，但没有说清处理人是谁，把 violation.handler_admin_qq 或 violation.handler_admin_nickname 放入 missing_fields/ambiguous_fields，并降低 confidence。
如果处理人以 QQ号出现，填 handler_admin_qq；如果以昵称出现，填 handler_admin_nickname。不要把违规成员的 QQ号填到 handler_admin_qq。
用户可能使用相对时间，例如“刚刚”“5分钟前”“两小时前”“半小时前”“昨天晚上8点”“今天下午3点”。必须以当前时间为基准理解。
如果用户只说“几分钟前”“之前”“昨天晚上”这类没有精确数值或精确时刻的表达，不要编造时间；time 保留原文，并把 violation.time 或 status_update.time 放入 missing_fields，confidence 降低。
严格返回以下结构：
{
"intent":"create_violation|query_member|query_recent|query_area_records|consultation|final_warning|withdraw_latest|update_status|unlock_member|mute_member|confirm|cancel|help|export|unknown",
"group_area":"蜂巢|蜂窝|蜂箱|null",
"target":{"qq_number":"string|null","qq_nickname":"string|null"},
"violation":{"time":"string|null","judgement":"string|null","action":"string|null","handler_admin_qq":"string|null","handler_admin_nickname":"string|null","remark":"string|null"},
"status_update":{"new_status":"正常|已质询|最后警告|已移出|已拉黑|已退群|null","time":"string|null","result":"通过|已移出|已拉黑|null"},
"moderation":{"duration_seconds":600,"duration_text":"string|null","reason":"string|null"},
"query":{"recent_days":14,"from_last_violation_time":true,"time_range":"today|yesterday|current_week|current_month|recent_days|all|null","limit":20},
"operation":{"need_confirm":true,"confidence":0.0,"missing_fields":[],"ambiguous_fields":[]}
}
"""


class AIRouterError(Exception):
    pass


def _gateway_enabled(state: object | None = None) -> bool:
    from plugins.feature_control.runtime import FEATURES

    if state is None:
        state = FEATURES.snapshot()
    return bool(
        getattr(FEATURES, "business_capable", True)
        and getattr(state, "llm_gateway_enabled", False)
        and getattr(state, "llm_gateway_business_enabled", False)
    )


def _text_api_available() -> bool:
    return bool(CONFIG.ai_api_key)


_MEMBER_QUERY_RE = re.compile(
    r"^(?:查询|查看|查|看)\s*(蜂巢|蜂窝|蜂箱)\s*(.+?)\s*$"
)
_MEMBER_QUERY_SUFFIX_RE = re.compile(
    r"(?:的)?(?:(?:最近|本月|这个月|本周|这周|今天|今日|昨天|昨日)\s*)?(?:违规)?记录\s*$"
)


def _extract_json(text: str) -> dict[str, Any] | None:
    text = text.strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?", "", text).strip()
        text = re.sub(r"```$", "", text).strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    start = text.find("{")
    end = text.rfind("}")
    if start >= 0 and end > start:
        try:
            return json.loads(text[start : end + 1])
        except json.JSONDecodeError:
            return None
    return None


def _member_query_shortcut(message: str) -> dict[str, Any] | None:
    matched = _MEMBER_QUERY_RE.fullmatch(message.strip())
    if not matched:
        return None
    area, raw_target = matched.groups()
    recent = "最近" in raw_target
    target_text = _MEMBER_QUERY_SUFFIX_RE.sub("", raw_target).strip()
    target_text = target_text.strip(" \t,，。！!?？:：;；/\\_—-")
    if not target_text:
        return None
    target = {
        "qq_number": target_text if re.fullmatch(r"\d{5,12}", target_text) else None,
        "qq_nickname": None if re.fullmatch(r"\d{5,12}", target_text) else target_text,
    }
    data = DEFAULT_INTENT | {
        "intent": "query_recent" if recent else "query_member",
        "group_area": area,
        "target": target,
    }
    data["operation"] = {
        **DEFAULT_INTENT["operation"],
        "need_confirm": False,
        "confidence": 1.0,
    }
    return merge_default(data)


def _keyword_shortcut(message: str) -> dict[str, Any] | None:
    stripped = message.strip()
    if stripped in {"确认", "确定"}:
        data = DEFAULT_INTENT | {"intent": "confirm"}
        data["operation"] = {**DEFAULT_INTENT["operation"], "need_confirm": False, "confidence": 1.0}
        return merge_default(data)
    if stripped in {"取消", "放弃"}:
        data = DEFAULT_INTENT | {"intent": "cancel"}
        data["operation"] = {**DEFAULT_INTENT["operation"], "need_confirm": False, "confidence": 1.0}
        return merge_default(data)
    if stripped in {"帮助", "help", "菜单"}:
        data = DEFAULT_INTENT | {"intent": "help"}
        data["operation"] = {**DEFAULT_INTENT["operation"], "need_confirm": False, "confidence": 1.0}
        return merge_default(data)
    area = next((item for item in ("蜂巢", "蜂窝", "蜂箱") if item in stripped), None)
    if area and "导出" in stripped and ("记录" in stripped or "日志" in stripped or "违规" in stripped):
        data = DEFAULT_INTENT | {"intent": "export", "group_area": area}
        data["query"] = {**DEFAULT_INTENT["query"], "time_range": _time_range_from_text(stripped)}
        data["operation"] = {**DEFAULT_INTENT["operation"], "need_confirm": False, "confidence": 1.0}
        return merge_default(data)
    member_query = _member_query_shortcut(stripped)
    if member_query:
        return member_query
    destructive_words = {"撤回", "解锁", "退群", "移出", "拉黑", "质询", "最后警告", "确认", "取消"}
    if area and not any(word in stripped for word in destructive_words) and (
        "违规记录" in stripped or "全部记录" in stripped or _looks_like_area_only_query(stripped, area)
    ):
        data = DEFAULT_INTENT | {"intent": "query_area_records", "group_area": area}
        data["query"] = {**DEFAULT_INTENT["query"], "time_range": _time_range_from_text(stripped), "limit": 20}
        data["operation"] = {**DEFAULT_INTENT["operation"], "need_confirm": False, "confidence": 1.0}
        return merge_default(data)
    return None


def _time_range_from_text(text: str) -> str:
    if "本月" in text or "这个月" in text or "当月" in text:
        return "current_month"
    if "本周" in text or "这周" in text or "这个星期" in text:
        return "current_week"
    if "昨天" in text or "昨日" in text:
        return "yesterday"
    if "今天" in text or "今日" in text:
        return "today"
    if "最近" in text:
        return "recent_days"
    if "全部" in text or "所有" in text:
        return "all"
    return "all"


def _looks_like_area_only_query(text: str, area: str) -> bool:
    if not any(word in text for word in ("查", "查询", "看", "查看", "最近", "本月", "本周", "今天", "昨天", "记录")):
        return False
    cleaned = text.replace(area, "")
    for token in ("查", "查询", "查看", "看", "一下", "最近", "本月", "这个月", "本周", "这周", "今天", "今日", "昨天", "昨日", "违规", "记录", "的", "有", "没有"):
        cleaned = cleaned.replace(token, "")
    cleaned = re.sub(r"[\s,，。.!！?？:：;；/\\_—-]+", "", cleaned)
    return cleaned == ""


def _intent_messages(
    message: str, *, referenced_time: str | None
) -> tuple[dict[str, object], dict[str, object]]:
    now_text = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    reference_hint = ""
    if referenced_time:
        reference_hint = (
            "\n本条管理员消息引用/回复了一条 QQ 消息，被引用消息时间："
            f"{referenced_time}。如果管理员没有另外说明时间，可将该时间作为 "
            "violation.time 或 status_update.time。"
        )
    return (
        {
            "role": "system",
            "content": (
                f"{SYSTEM_PROMPT}\n当前服务器时间：{now_text}（Asia/Shanghai）。"
                "输出时间尽量使用 YYYY-MM-DD HH:MM:SS。"
                f"{reference_hint}"
            ),
        },
        {"role": "user", "content": message},
    )


async def _legacy_complete(
    messages: tuple[dict[str, object], dict[str, object]],
) -> str:
    payload = {
        "model": CONFIG.ai_model,
        "messages": list(messages),
        "temperature": 0,
        "response_format": {"type": "json_object"},
    }
    url = f"{CONFIG.ai_base_url}/v1/chat/completions"
    headers = {
        "Authorization": f"Bearer {CONFIG.ai_api_key}",
        "Content-Type": "application/json",
    }
    async with httpx.AsyncClient(timeout=CONFIG.ai_timeout) as client:
        response = await client.post(url, headers=headers, json=payload)
        response.raise_for_status()
        content = response.json()["choices"][0]["message"]["content"]
    if not isinstance(content, str):
        raise TypeError("AI response content must be text")
    return content


async def parse_intent(message: str, referenced_time: str | None = None) -> dict[str, Any]:
    shortcut = _keyword_shortcut(message)
    if shortcut:
        return shortcut
    from plugins.feature_control.runtime import FEATURES

    state = FEATURES.snapshot()
    use_gateway = _gateway_enabled(state)
    if not _text_api_available():
        raise AIRouterError("AI 未启用或缺少业务模型密钥，无法进行自然语言解析。")
    messages = _intent_messages(message, referenced_time=referenced_time)
    try:
        if use_gateway:
            gateway = await get_gateway()
            content = await gateway.parse_business_intent(
                messages, economy_mode=False
            )
        else:
            content = await _legacy_complete(messages)
    except GatewayError as exc:
        raise AIRouterError(f"AI 解析失败：{type(exc).__name__}") from exc
    except Exception as exc:
        raise AIRouterError(f"AI 解析失败：{exc}") from exc
    parsed = _extract_json(content)
    if not parsed:
        raise AIRouterError("AI 返回内容不是合法 JSON，请换一种更明确的说法。")
    return merge_default(parsed)
