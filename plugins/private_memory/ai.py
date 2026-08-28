from __future__ import annotations

import json
import logging
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

import httpx

from plugins.feature_control.runtime import FEATURES
from plugins.llm_gateway import get_gateway
from plugins.llm_gateway.errors import (
    GatewayAuthenticationError,
    GatewayClientError,
    GatewayConfigurationError,
    GatewayContractError,
    GatewayEmptyContentError,
    GatewayError,
    GatewayPaymentRequiredError,
    GatewayRateLimitError,
    GatewayServerError,
    GatewayTimeout,
    GatewayTransportError,
)
from plugins.violation_record.config import CONFIG

from .models import PrivateFactCandidate, PrivateMessage, RelationshipState
from .relationship import (
    MAX_COMMUNICATION_STYLE_LENGTH,
    MAX_OPEN_TOPICS,
    MAX_OPEN_TOPIC_LENGTH,
    MAX_PREFERRED_ADDRESS_LENGTH,
    MAX_STATE_TEXT_LENGTH,
)


logger = logging.getLogger(__name__)


class PrivateMemoryAIError(RuntimeError):
    """Base error carrying a queue-safe category, never model content."""

    def __init__(self, code: str, *, retryable: bool) -> None:
        super().__init__(code)
        self.code = code
        self.retryable = retryable


class ContractError(PrivateMemoryAIError):
    def __init__(self, code: str = "contract_error") -> None:
        super().__init__(code, retryable=False)


class TransportError(PrivateMemoryAIError):
    def __init__(self, code: str = "transport_error", *, retryable: bool = True) -> None:
        super().__init__(code, retryable=retryable)


def _classify_http_status(status: int) -> TransportError:
    if status in {401, 403}:
        return TransportError("auth_error", retryable=False)
    if status == 402:
        return TransportError("payment_required", retryable=False)
    if status == 408:
        return TransportError("request_timeout", retryable=True)
    if status == 429:
        return TransportError("rate_limited", retryable=True)
    if status >= 500:
        return TransportError("server_error", retryable=True)
    return TransportError("client_error", retryable=False)


@dataclass(frozen=True)
class RelationshipCandidate:
    state_text: str
    open_topics: tuple[str, ...]
    preferred_address: str
    communication_style: str


def _strict_object(
    content: str, *, fields: set[str], optional: set[str] | None = None
) -> dict[str, Any]:
    if not isinstance(content, str) or not content.strip():
        raise ContractError()
    try:
        value = json.loads(content.strip())
    except (TypeError, json.JSONDecodeError) as exc:
        raise ContractError("invalid_json") from exc
    if not isinstance(value, dict):
        raise ContractError()
    optional = optional or set()
    keys = set(value)
    if not fields.issubset(keys) or not keys.issubset(fields | optional):
        raise ContractError("unknown_or_missing_field")
    return value


def _bounded_text(
    value: Any,
    *,
    maximum: int,
    allow_empty: bool = True,
    reject_overflow: bool = False,
) -> str:
    if not isinstance(value, str):
        raise ContractError()
    compact = " ".join(value.split())
    if not allow_empty and not compact:
        raise ContractError()
    if reject_overflow and len(compact) > maximum:
        raise ContractError("value_too_long")
    return compact[:maximum]


def _parse_summary(content: str, *, gateway_contract: bool = False) -> str:
    parsed = _strict_object(content, fields={"summary"})
    return _bounded_text(
        parsed["summary"],
        maximum=600,
        allow_empty=False,
        reject_overflow=gateway_contract,
    )


def _parse_facts(content: str, *, user_id: str) -> tuple[PrivateFactCandidate, ...]:
    parsed = _strict_object(content, fields={"facts"})
    raw_facts = parsed["facts"]
    if not isinstance(raw_facts, list) or len(raw_facts) > 20:
        raise ContractError()
    candidates: list[PrivateFactCandidate] = []
    required = {"fact_text", "source_message_id", "source_quote", "certainty"}
    for raw in raw_facts:
        if not isinstance(raw, dict) or set(raw) != required:
            raise ContractError()
        if raw["certainty"] != "explicit":
            raise ContractError("unsupported_certainty")
        candidates.append(
            PrivateFactCandidate(
                user_id=user_id,
                fact_text=_bounded_text(
                    raw["fact_text"], maximum=160, allow_empty=False
                ),
                source_message_id=_bounded_text(
                    raw["source_message_id"], maximum=128, allow_empty=False
                ),
                source_quote=_bounded_text(
                    raw["source_quote"], maximum=120, allow_empty=False
                ),
            )
        )
    return tuple(candidates)


_UNCERTAIN_MARKERS = ("可能", "似乎", "也许", "或许", "看起来", "不确定")


def _parse_relationship(
    content: str, *, gateway_contract: bool = False
) -> RelationshipCandidate:
    data_fields = {
        "state_text",
        "open_topics",
        "preferred_address",
        "communication_style",
    }
    required = data_fields if gateway_contract else data_fields | {"certainty"}
    parsed = _strict_object(content, fields=required)
    certainty = "uncertain" if gateway_contract else parsed["certainty"]
    if not gateway_contract and certainty not in {"explicit", "uncertain"}:
        raise ContractError("unsupported_certainty")
    topics = parsed["open_topics"]
    if not isinstance(topics, list) or len(topics) > MAX_OPEN_TOPICS:
        raise ContractError()
    bounded_topics = tuple(
        _bounded_text(
            item,
            maximum=MAX_OPEN_TOPIC_LENGTH,
            allow_empty=False,
            reject_overflow=gateway_contract,
        )
        for item in topics
    )
    state_text = _bounded_text(
        parsed["state_text"],
        maximum=MAX_STATE_TEXT_LENGTH,
        allow_empty=True,
        reject_overflow=gateway_contract,
    )
    if certainty == "uncertain" and state_text and not any(
        marker in state_text for marker in _UNCERTAIN_MARKERS
    ):
        state_text = ("可能" + state_text)[:MAX_STATE_TEXT_LENGTH]
    return RelationshipCandidate(
        state_text=state_text,
        open_topics=bounded_topics,
        preferred_address=_bounded_text(
            parsed["preferred_address"],
            maximum=MAX_PREFERRED_ADDRESS_LENGTH,
            reject_overflow=gateway_contract,
        ),
        communication_style=_bounded_text(
            parsed["communication_style"],
            maximum=MAX_COMMUNICATION_STYLE_LENGTH,
            reject_overflow=gateway_contract,
        ),
    )


async def _legacy_complete(*, system: str, user: str) -> str:
    if not CONFIG.ai_api_key:
        raise TransportError("configuration_error", retryable=False)
    payload = {
        "model": CONFIG.ai_model,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
        "temperature": 0.1,
        "response_format": {"type": "json_object"},
    }
    try:
        async with httpx.AsyncClient(timeout=CONFIG.ai_timeout) as client:
            response = await client.post(
                f"{CONFIG.ai_base_url}/v1/chat/completions",
                headers={
                    "Authorization": f"Bearer {CONFIG.ai_api_key}",
                    "Content-Type": "application/json",
                },
                json=payload,
            )
            response.raise_for_status()
            content = response.json()["choices"][0]["message"]["content"]
    except httpx.HTTPStatusError as exc:
        raise _classify_http_status(exc.response.status_code) from exc
    except (httpx.TimeoutException, httpx.NetworkError, OSError) as exc:
        raise TransportError() from exc
    except (KeyError, TypeError, ValueError) as exc:
        raise ContractError("response_contract_error") from exc
    if not isinstance(content, str):
        raise ContractError("empty_response")
    return content


def _map_gateway_error(error: GatewayError) -> PrivateMemoryAIError:
    if isinstance(error, GatewayConfigurationError):
        # The background worker can run before the Gateway startup hook or while
        # an operator is repairing configuration. Keep its durable job retryable.
        return TransportError("configuration_error", retryable=True)
    if isinstance(error, GatewayAuthenticationError):
        return TransportError("auth_error", retryable=False)
    if isinstance(error, GatewayPaymentRequiredError):
        return TransportError("payment_required", retryable=False)
    if isinstance(error, GatewayTimeout):
        return TransportError("request_timeout", retryable=True)
    if isinstance(error, GatewayTransportError):
        return TransportError("transport_error", retryable=True)
    if isinstance(error, GatewayRateLimitError):
        return TransportError("rate_limited", retryable=True)
    if isinstance(error, GatewayServerError):
        return TransportError("server_error", retryable=True)
    if isinstance(error, GatewayClientError):
        return TransportError("client_error", retryable=False)
    if isinstance(error, GatewayEmptyContentError):
        return ContractError("empty_response")
    if isinstance(error, GatewayContractError):
        return ContractError("response_contract_error")
    return TransportError("gateway_error", retryable=False)


async def _complete(
    *,
    task: str,
    messages: tuple[dict[str, object], dict[str, object]],
    use_gateway: bool,
    economy_mode: bool,
) -> str:
    if not use_gateway:
        return await _legacy_complete(
            system=str(messages[0]["content"]), user=str(messages[1]["content"])
        )
    try:
        gateway = await get_gateway()
        if task == "private_summary":
            return await gateway.summarize_private_conversation(
                messages, economy_mode=economy_mode
            )
        if task == "private_facts":
            return await gateway.extract_private_facts(
                messages, economy_mode=economy_mode
            )
        if task == "relationship":
            return await gateway.update_relationship_state(
                messages, economy_mode=economy_mode
            )
        raise ValueError("unknown private memory gateway task")
    except GatewayError as exc:
        raise _map_gateway_error(exc) from exc


def _request_policy() -> tuple[bool, bool]:
    state = FEATURES.snapshot()
    economy_mode = bool(getattr(state, "economy_mode_enabled", False))
    use_gateway = economy_mode or bool(
        getattr(state, "llm_gateway_enabled", False)
        and getattr(state, "llm_gateway_private_memory_enabled", False)
    )
    return use_gateway, economy_mode


def _summary_messages(
    previous: str, messages: Sequence[PrivateMessage]
) -> tuple[dict[str, object], dict[str, object]]:
    return (
        {
            "role": "system",
            "content": (
                "合并私聊旧摘要和新消息，只保留明确内容，不推测、不记录口令、令牌或私密凭据。"
                "只输出严格 JSON：{\"summary\":\"不超过600字的滚动摘要\"}。"
            ),
        },
        {
            "role": "user",
            "content": json.dumps(
                {
                    "previous_summary": previous,
                    "messages": json.loads(_messages_payload(messages)),
                },
                ensure_ascii=False,
                separators=(",", ":"),
            ),
        },
    )


def _relationship_messages(
    current: RelationshipState | None,
    messages: Sequence[PrivateMessage],
    *,
    gateway_contract: bool,
) -> tuple[dict[str, object], dict[str, object]]:
    current_payload: Mapping[str, Any] | None = None
    if current is not None:
        current_payload = {
            "state_text": current.state_text,
            "open_topics": current.open_topics,
            "preferred_address": current.preferred_address,
            "communication_style": current.communication_style,
        }
    output_contract = (
        '{"state_text":"...","open_topics":["..."],'
        '"preferred_address":"","communication_style":""}'
        if gateway_contract
        else '{"state_text":"...","open_topics":["..."],'
        '"preferred_address":"","communication_style":"",'
        '"certainty":"explicit|uncertain"}'
    )
    certainty_rule = (
        "推测内容必须在文字中保留可能/似乎等不确定措辞。"
        if gateway_contract
        else "推测必须标为 uncertain 并保留可能/似乎等不确定措辞。"
    )
    return (
        {
            "role": "system",
            "content": (
                "更新聊天关系状态，只描述互动方式、熟悉程度和未完话题，不生成事实、心理诊断或"
                "业务判断。"
                + certainty_rule
                + "只输出严格 JSON："
                + output_contract
                + "。"
            ),
        },
        {
            "role": "user",
            "content": json.dumps(
                {
                    "current": current_payload,
                    "messages": json.loads(_messages_payload(messages)),
                },
                ensure_ascii=False,
                separators=(",", ":"),
            ),
        },
    )


def _messages_payload(messages: Sequence[PrivateMessage]) -> str:
    return json.dumps(
        [
            {
                "id": item.id,
                "message_id": item.message_id,
                "direction": item.direction,
                "text": item.text,
            }
            for item in messages
        ],
        ensure_ascii=False,
        separators=(",", ":"),
    )


def _fact_messages(
    messages: Sequence[PrivateMessage],
) -> tuple[dict[str, object], dict[str, object]]:
    return (
        {
            "role": "system",
            "content": (
                "保守提取说话者本人明确表达、未来仍有用的稳定非敏感事实。拒绝推测、情绪、评价、"
                "密码、令牌、密钥和其他凭据。source_message_id 和 source_quote 必须来自输入。"
                "只输出严格 JSON：{\"facts\":[{\"fact_text\":\"...\","
                "\"source_message_id\":\"...\",\"source_quote\":\"...\","
                "\"certainty\":\"explicit\"}]}。"
            ),
        },
        {"role": "user", "content": _messages_payload(messages)},
    )


async def summarize_private_conversation(
    previous: str, messages: Sequence[PrivateMessage]
) -> str | None:
    if not messages:
        return None
    try:
        use_gateway, economy_mode = _request_policy()
        content = await _complete(
            task="private_summary",
            messages=_summary_messages(previous, messages),
            use_gateway=use_gateway,
            economy_mode=economy_mode,
        )
        return _parse_summary(content, gateway_contract=use_gateway)
    except PrivateMemoryAIError as exc:
        logger.warning("private summary model failed error=%s", type(exc).__name__)
        raise


async def extract_private_facts(
    messages: Sequence[PrivateMessage],
) -> tuple[PrivateFactCandidate, ...]:
    if not messages:
        return ()
    user_id = messages[0].user_id
    try:
        use_gateway, economy_mode = _request_policy()
        content = await _complete(
            task="private_facts",
            messages=_fact_messages(messages),
            use_gateway=use_gateway,
            economy_mode=economy_mode,
        )
        return _parse_facts(content, user_id=user_id)
    except PrivateMemoryAIError as exc:
        logger.warning("private facts model failed error=%s", type(exc).__name__)
        raise


async def generate_relationship_candidate(
    current: RelationshipState | None, messages: Sequence[PrivateMessage]
) -> RelationshipCandidate | None:
    if not messages:
        return None
    try:
        use_gateway, economy_mode = _request_policy()
        content = await _complete(
            task="relationship",
            messages=_relationship_messages(
                current, messages, gateway_contract=use_gateway
            ),
            use_gateway=use_gateway,
            economy_mode=economy_mode,
        )
        return _parse_relationship(content, gateway_contract=use_gateway)
    except PrivateMemoryAIError as exc:
        logger.warning("relationship model failed error=%s", type(exc).__name__)
        raise


__all__ = [
    "ContractError",
    "PrivateMemoryAIError",
    "RelationshipCandidate",
    "TransportError",
    "extract_private_facts",
    "generate_relationship_candidate",
    "summarize_private_conversation",
]
