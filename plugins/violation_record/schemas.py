from typing import Any


INTENTS = {
    "create_violation",
    "query_member",
    "query_recent",
    "query_area_records",
    "consultation",
    "final_warning",
    "withdraw_latest",
    "update_status",
    "unlock_member",
    "mute_member",
    "confirm",
    "cancel",
    "help",
    "export",
    "unknown",
}


DEFAULT_INTENT: dict[str, Any] = {
    "intent": "unknown",
    "group_area": None,
    "target": {"qq_number": None, "qq_nickname": None},
    "violation": {
        "time": None,
        "judgement": None,
        "action": None,
        "handler_admin_qq": None,
        "handler_admin_nickname": None,
        "remark": None,
    },
    "status_update": {
        "new_status": None,
        "time": None,
        "result": None,
    },
    "moderation": {
        "duration_seconds": None,
        "duration_text": None,
        "reason": None,
    },
    "query": {"recent_days": 14, "from_last_violation_time": True, "time_range": None, "limit": 20},
    "operation": {
        "need_confirm": True,
        "confidence": 0.0,
        "missing_fields": [],
        "ambiguous_fields": [],
    },
}


def merge_default(data: dict[str, Any]) -> dict[str, Any]:
    merged = {
        "intent": data.get("intent", "unknown"),
        "group_area": data.get("group_area"),
        "target": {**DEFAULT_INTENT["target"], **(data.get("target") or {})},
        "violation": {**DEFAULT_INTENT["violation"], **(data.get("violation") or {})},
        "status_update": {**DEFAULT_INTENT["status_update"], **(data.get("status_update") or {})},
        "moderation": {**DEFAULT_INTENT["moderation"], **(data.get("moderation") or {})},
        "query": {**DEFAULT_INTENT["query"], **(data.get("query") or {})},
        "operation": {**DEFAULT_INTENT["operation"], **(data.get("operation") or {})},
    }
    if merged["intent"] not in INTENTS:
        merged["intent"] = "unknown"
    try:
        merged["operation"]["confidence"] = float(merged["operation"].get("confidence") or 0)
    except (TypeError, ValueError):
        merged["operation"]["confidence"] = 0.0
    return merged
