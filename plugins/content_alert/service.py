from __future__ import annotations

import asyncio
import hashlib
import unicodedata
from collections import OrderedDict
from collections.abc import Callable, Mapping, Sequence
from datetime import datetime
from typing import Protocol
from zoneinfo import ZoneInfo

from nonebot.adapters.onebot.v11 import Bot, GroupMessageEvent, MessageSegment

from .engine import KeywordMatch, LiteralKeywordMatcher, match_message_text_segments
from .rules import KeywordRule, KeywordRuleStore

_SHANGHAI = ZoneInfo("Asia/Shanghai")
_DELIVERED_CACHE_LIMIT = 4096
_FUTURE_EVENT_TOLERANCE_SECONDS = 60
_MAX_REPORTED_MATCHES = 20
_MAX_REPORTED_MANAGED_MATCHES = 12
_MAX_REPORT_CHARS = 1_800


class _ManagedSnapshot(Protocol):
    has_active_generation: bool


class _ManagedMatch(Protocol):
    term: str
    category_names: Sequence[str]
    disclosure_policy: str


class _ManagedCatalog(Protocol):
    def snapshot(self) -> _ManagedSnapshot: ...

    def match_message(self, message: object) -> Sequence[_ManagedMatch]: ...


class ContentAlertService:
    """Detect literal text rules and send one non-enforcement alert."""

    def __init__(
        self,
        *,
        rule_store: KeywordRuleStore,
        background_rule_store: KeywordRuleStore | None = None,
        managed_catalog: _ManagedCatalog | None = None,
        source_group_labels: Mapping[int, str],
        report_group_id: int,
        peer_bot_user_ids: Sequence[int | str],
        runtime_enabled: Callable[[], bool],
        clock: Callable[[], float],
        max_event_age_seconds: int = 300,
        max_excerpt_chars: int = 160,
    ) -> None:
        self._rule_store = rule_store
        self._background_rule_store = background_rule_store
        self._managed_catalog = managed_catalog
        self._source_group_labels = {
            int(group_id): str(label)
            for group_id, label in source_group_labels.items()
            if int(group_id) > 0
        }
        self._report_group_id = int(report_group_id)
        self._peer_bot_user_ids = {
            str(user_id) for user_id in peer_bot_user_ids if str(user_id).isdigit()
        }
        self._runtime_enabled = runtime_enabled
        self._clock = clock
        self._max_event_age_seconds = max(1, int(max_event_age_seconds))
        self._max_excerpt_chars = max(16, int(max_excerpt_chars))
        self._delivery_lock = asyncio.Lock()
        self._delivered: OrderedDict[tuple[int, int, str], None] = OrderedDict()

    async def handle_event(self, bot: Bot, event: GroupMessageEvent) -> bool:
        if not self._eligible(event):
            return False

        manual_rules = self._rule_store.snapshot()
        managed_active = False
        managed_matches: Sequence[_ManagedMatch] = ()
        if self._managed_catalog is not None:
            managed_active = self._managed_catalog.snapshot().has_active_generation
            if managed_active:
                managed_matches = self._managed_catalog.match_message(event.message)

        # The legacy file remains an intentionally strict fallback.  Once a
        # complete managed generation is active it must not be evaluated as a
        # second, unclassified rule source.
        background_rules = ()
        if not managed_active and self._background_rule_store is not None:
            background_rules = self._background_rule_store.snapshot()
        if not manual_rules and not background_rules and not managed_matches:
            return False
        manual_matches = _match_rules(
            event.message,
            manual_rules,
        )
        background_matches = _match_rules(
            event.message,
            background_rules,
        )
        if not manual_matches and not background_matches and not managed_matches:
            return False

        strict_hidden = bool(background_matches) or any(
            match.disclosure_policy == "strict_hidden"
            for match in managed_matches
        )

        delivery_key = (
            int(event.self_id),
            int(event.group_id),
            str(event.message_id),
        )
        async with self._delivery_lock:
            if delivery_key in self._delivered:
                return False
            report = self._build_report(
                event,
                manual_matches,
                managed_matches=managed_matches,
                strict_hidden=strict_hidden,
            )
            await bot.send_group_msg(
                group_id=self._report_group_id,
                message=MessageSegment.text(report),
            )
            self._delivered[delivery_key] = None
            self._delivered.move_to_end(delivery_key)
            while len(self._delivered) > _DELIVERED_CACHE_LIMIT:
                self._delivered.popitem(last=False)
        return True

    def _eligible(self, event: GroupMessageEvent) -> bool:
        if not self._runtime_enabled():
            return False
        group_id = int(event.group_id)
        if group_id not in self._source_group_labels:
            return False
        if self._report_group_id <= 0 or self._report_group_id == group_id:
            return False
        actor = str(event.user_id)
        if actor == str(event.self_id) or actor in self._peer_bot_user_ids:
            return False
        age = float(self._clock()) - int(event.time)
        return -_FUTURE_EVENT_TOLERANCE_SECONDS <= age <= self._max_event_age_seconds

    def _build_report(
        self,
        event: GroupMessageEvent,
        matches: Sequence[KeywordMatch],
        *,
        managed_matches: Sequence[_ManagedMatch] = (),
        strict_hidden: bool = False,
    ) -> str:
        group_id = int(event.group_id)
        sender = event.sender
        sender_name = (
            "昵称已隐藏"
            if strict_hidden
            else _one_line(
                str(
                    getattr(sender, "card", "")
                    or getattr(sender, "nickname", "")
                    or event.user_id
                ),
                limit=64,
            )
        )
        excerpt = (
            "（内容已隐藏）"
            if strict_hidden
            else _message_excerpt(event, limit=self._max_excerpt_chars)
        )
        event_time = datetime.fromtimestamp(int(event.time), tz=_SHANGHAI)
        alert_id = hashlib.sha256(
            f"{event.self_id}:{group_id}:{event.message_id}".encode()
        ).hexdigest()[:12]

        match_text = _render_matches(
            matches,
            managed_matches=managed_matches,
            strict_hidden=strict_hidden,
        )

        label = _one_line(
            self._source_group_labels.get(group_id, f"群{group_id}"),
            limit=64,
        )
        report = "\n".join(
            (
                f"【{label}关键词违禁告警】",
                f"告警编号：KA-{alert_id}",
                f"来源群：{label}（{group_id}）",
                f"发送者：{sender_name}（QQ：{event.user_id}）",
                f"命中规则：{match_text}",
                f"消息时间：{event_time:%Y-%m-%d %H:%M:%S}",
                f"消息ID：{event.message_id}",
                f"内容摘录：{excerpt}",
                "检测器：keyword-literal-v1（未调用 AI）",
                "处置状态：仅告警，未自动撤回、禁言或记录违规",
            )
        )
        if len(report) <= _MAX_REPORT_CHARS:
            return report
        return report[: _MAX_REPORT_CHARS - 1] + "…"


def _match_rules(
    message: object,
    rules: Sequence[KeywordRule],
) -> tuple[KeywordMatch, ...]:
    if not rules:
        return ()
    return match_message_text_segments(
        message,
        LiteralKeywordMatcher(rules),
    )


def _render_matches(
    matches: Sequence[KeywordMatch],
    *,
    managed_matches: Sequence[_ManagedMatch] = (),
    strict_hidden: bool,
) -> str:
    if strict_hidden:
        # Constant wording prevents a protected hit from leaking a manual
        # overlap, category name, exact term, internal identifier, or hit
        # count through the same alert.
        return "受保护规则命中（详情已隐藏）"

    displayed_matches = tuple(matches[:_MAX_REPORTED_MATCHES])
    parts = [
        f"{item.rule_id}：{_one_line(item.pattern, limit=64)}"
        for item in displayed_matches
    ]
    if len(matches) > len(displayed_matches):
        parts.append(f"另有 {len(matches) - len(displayed_matches)} 项人工规则")
    seen_managed: set[tuple[str, tuple[str, ...]]] = set()
    rendered_managed = 0
    for item in managed_matches:
        term = _one_line(str(item.term), limit=64)
        category_names = tuple(
            dict.fromkeys(
                _one_line(str(name), limit=32)
                for name in item.category_names
                if _one_line(str(name), limit=32)
            )
        )
        key = (term, category_names)
        if not term or not category_names or key in seen_managed:
            continue
        seen_managed.add(key)
        if rendered_managed >= _MAX_REPORTED_MANAGED_MATCHES:
            continue
        parts.append(f"{'/'.join(category_names)}：{term}")
        rendered_managed += 1
    if len(seen_managed) > rendered_managed:
        parts.append(f"另有 {len(seen_managed) - rendered_managed} 项受控规则")
    return "、".join(parts) or "规则命中"


def _message_excerpt(event: GroupMessageEvent, *, limit: int) -> str:
    parts = [
        str(segment.data.get("text", ""))
        for segment in event.message
        if segment.type == "text" and isinstance(segment.data.get("text"), str)
    ]
    excerpt = _one_line(" / ".join(parts), limit=limit)
    return excerpt or "（无可展示文本）"


def _one_line(value: str, *, limit: int) -> str:
    cleaned: list[str] = []
    pending_space = False
    for character in unicodedata.normalize("NFKC", value):
        category = unicodedata.category(character)
        if category == "Cf":
            continue
        if character.isspace():
            pending_space = bool(cleaned)
            continue
        if category.startswith("C"):
            continue
        if pending_space:
            cleaned.append(" ")
            pending_space = False
        cleaned.append(character)
        if len(cleaned) >= limit:
            break
    rendered = "".join(cleaned).strip()
    if len(rendered) >= limit and len(value) > len(rendered):
        return rendered[:-1] + "…"
    return rendered


__all__ = ("ContentAlertService",)
