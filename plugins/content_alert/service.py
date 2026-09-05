from __future__ import annotations

import asyncio
import hashlib
import unicodedata
from collections.abc import Callable, Mapping, Sequence
from datetime import datetime
from functools import lru_cache
from pathlib import Path
from typing import Protocol
from zoneinfo import ZoneInfo

from nonebot.adapters.onebot.v11 import Bot, GroupMessageEvent, MessageSegment
from nonebot.adapters.onebot.v11.exception import ActionFailed
from nonebot import logger

from .engine import (
    KeywordMatch,
    LiteralKeywordMatcher,
    ScalableLiteralScanLimitError,
    match_message_text_segments,
)
from .rules import (
    KeywordRule,
    KeywordRuleStore,
    is_ignored_literal_character,
    normalize_literal_text,
)
from .outbox import AlertOutbox, event_identity

_SHANGHAI = ZoneInfo("Asia/Shanghai")
_FUTURE_EVENT_TOLERANCE_SECONDS = 60
_MAX_REPORTED_MATCHES = 20
_MAX_REPORTED_MANAGED_MATCHES = 12
_MAX_REPORT_CHARS = 1_800
_MAX_CONCURRENT_MANAGED_SCANS = 2
_MAX_ADMITTED_SCANS = 8
_SEND_TIMEOUT_SECONDS = 30
_CONTEXT_CLASS_LABELS = {
    "case_proceeding": "案件语境",
    "office_title": "职务语境",
    "political_institution": "政治机构语境",
    "historical_reference": "历史语境",
}


class _ManagedSnapshot(Protocol):
    has_active_generation: bool


class _ManagedMatch(Protocol):
    term: str
    category_ids: Sequence[str]
    category_names: Sequence[str]
    disclosure_policy: str


class _ManagedCatalog(Protocol):
    def snapshot(self) -> _ManagedSnapshot: ...

    def match_snapshot(
        self,
        snapshot: _ManagedSnapshot,
        message: object,
    ) -> Sequence[_ManagedMatch]: ...


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
        outbox_path: Path | None = None,
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
        self._scan_semaphore = asyncio.Semaphore(_MAX_CONCURRENT_MANAGED_SCANS)
        self._admitted_scans = 0
        self._overflow_guard_inflight = False
        self.outbox = AlertOutbox(outbox_path or rule_store.path.with_name("outbox.sqlite3"))
        self._accepting = True

    async def handle_event(self, bot: Bot, event: GroupMessageEvent) -> bool:
        if not self._eligible(event):
            return False
        if self._admitted_scans >= _MAX_ADMITTED_SCANS:
            # Fail visibly with bounded, hidden metadata; do not allocate an
            # unbounded semaphore waiter or run an over-budget scan inline.
            if self._overflow_guard_inflight:
                return False
            self._overflow_guard_inflight = True
            try:
                return await self._persist_and_deliver(
                    bot, event, (), (), strict_hidden=True, political_alert=False,
                    scan_overflow=True, sender_name_strict=True,
                    excerpt="（内容已隐藏）", rule_generation="scan-admission-overflow",
                )
            finally:
                self._overflow_guard_inflight = False
        self._admitted_scans += 1
        try:
            return await self._handle_admitted_event(bot, event)
        finally:
            self._admitted_scans -= 1

    async def _handle_admitted_event(self, bot: Bot, event: GroupMessageEvent) -> bool:

        manual_rules = self._rule_store.snapshot()
        managed_active = False
        managed_scan_overflow = False
        managed_sender_name_strict = False
        managed_matches: Sequence[_ManagedMatch] = ()
        managed_generation = "none"
        if self._managed_catalog is not None:
            try:
                sender = event.sender
                raw_sender_name = str(
                    getattr(sender, "card", "")
                    or getattr(sender, "nickname", "")
                    or event.user_id
                )
                sender_name_candidates = tuple(
                    dict.fromkeys((raw_sender_name, _clean_one_line(raw_sender_name)))
                )
                async with self._scan_semaphore:
                    if not self._eligible(event):
                        return False
                    result = await asyncio.to_thread(
                        _scan_managed_catalog,
                        self._managed_catalog,
                        event.message,
                        sender_name_candidates,
                    )
                    managed_active, managed_matches, managed_sender_name_strict = result[:3]
                    managed_generation = str(result[3]) if len(result) > 3 else "unknown"
            except ScalableLiteralScanLimitError as exc:
                # A scan limit can only be raised after an active immutable
                # snapshot has been selected by ``scan_message``.
                managed_active = True
                managed_scan_overflow = True
                managed_generation = str(getattr(exc, "generation_id", "unknown"))

        # The legacy file remains an intentionally strict fallback.  Once a
        # complete managed generation is active it must not be evaluated as a
        # second, unclassified rule source.
        background_rules = ()
        if not managed_active and self._background_rule_store is not None:
            background_rules = self._background_rule_store.snapshot()
        if (
            not manual_rules
            and not background_rules
            and not managed_matches
            and not managed_scan_overflow
        ):
            return False
        manual_matches: tuple[KeywordMatch, ...] = ()
        background_matches: tuple[KeywordMatch, ...] = ()
        try:
            if manual_rules or background_rules:
                async with self._scan_semaphore:
                    if not self._eligible(event):
                        return False
                    manual_matches, background_matches = await asyncio.to_thread(
                        _scan_literal_sources, event.message, manual_rules, background_rules
                    )
        except ScalableLiteralScanLimitError:
            managed_scan_overflow = True
        if (
            not manual_matches
            and not background_matches
            and not managed_matches
            and not managed_scan_overflow
        ):
            return False

        political_alert = any(
            "political_cn" in match.category_ids for match in managed_matches
        )
        strict_hidden = (
            managed_scan_overflow
            or bool(background_matches)
            or any(
                match.disclosure_policy == "strict_hidden" for match in managed_matches
            )
        )
        excerpt_focus = _first_excerpt_focus(managed_matches)
        if not self._eligible(event):
            return False
        if strict_hidden:
            prepared_excerpt = "（内容已隐藏）"
        else:
            async with self._scan_semaphore:
                if not self._eligible(event):
                    return False
                prepared_excerpt = await asyncio.to_thread(
                    _message_excerpt,
                    event,
                    limit=self._max_excerpt_chars,
                    focus=excerpt_focus,
                )

        return await self._persist_and_deliver(
            bot, event, manual_matches, managed_matches,
            strict_hidden=strict_hidden, political_alert=political_alert,
            scan_overflow=managed_scan_overflow,
            sender_name_strict=managed_sender_name_strict or bool(background_matches),
            excerpt=prepared_excerpt,
            rule_generation=(f"managed:{managed_generation};manual:{_rules_digest(manual_rules)};"
                             f"legacy:{_rules_digest(background_rules)}"),
        )

    async def _persist_and_deliver(
        self, bot: Bot, event: GroupMessageEvent,
        manual_matches: Sequence[KeywordMatch], managed_matches: Sequence[_ManagedMatch], *,
        strict_hidden: bool, political_alert: bool, scan_overflow: bool,
        sender_name_strict: bool, excerpt: str, rule_generation: str,
    ) -> bool:
        if not self._eligible(event):
            return False
        report = self._build_report(
            event, manual_matches, managed_matches=managed_matches,
            strict_hidden=strict_hidden, political_alert=political_alert,
            managed_scan_overflow=scan_overflow,
            strict_sender_name_match=sender_name_strict, prepared_excerpt=excerpt,
        )
        key, inserted = await asyncio.to_thread(
            self.outbox.enqueue, self_id=str(event.self_id), source_group_id=int(event.group_id),
            source_message_id=str(event.message_id), report_group_id=self._report_group_id,
            rule_generation=rule_generation, report_text=report, now=float(self._clock()),
        )
        if not inserted:
            return False
        return bool(await self.deliver_pending(bot, event_key=key, self_id=str(event.self_id), limit=1))

    def _delivery_allowed(self, row: Mapping[str, object]) -> bool:
        return (
            self._accepting and self._runtime_enabled()
            and int(row['source_group_id']) in self._source_group_labels
            and int(row['report_group_id']) == self._report_group_id > 0
            and int(row['source_group_id']) != self._report_group_id
        )

    async def deliver_pending(self, bot: Bot, *, self_id: str | None = None,
                              event_key: str | None = None, limit: int = 10) -> int:
        """Attempt persisted alerts without reapplying the input freshness gate."""
        if not self._accepting or not self._runtime_enabled() or self._delivery_lock.locked():
            return 0
        bot_id = str(getattr(bot, 'self_id', self_id or ''))
        if not bot_id or (self_id is not None and bot_id != self_id):
            return 0
        sent = 0
        async with self._delivery_lock:
            for _ in range(min(max(0, limit), 10)):
                if not self._accepting or not self._runtime_enabled():
                    break
                row = await asyncio.to_thread(self.outbox.claim, now=float(self._clock()),
                                              self_id=bot_id, event_key=event_key)
                if row is None:
                    break
                if not self._delivery_allowed(row):
                    await asyncio.to_thread(self.outbox.release, row, now=float(self._clock()))
                    break
                if not await asyncio.to_thread(self.outbox.begin_send, row, now=float(self._clock())):
                    continue
                # No await between this final gate and invoking the QQ API.
                if not self._delivery_allowed(row):
                    await asyncio.to_thread(self.outbox.abort_unsent, row, now=float(self._clock()))
                    break
                try:
                    response = await asyncio.wait_for(self._send_guarded(bot, row), timeout=_SEND_TIMEOUT_SECONDS)
                except _FeatureDisabledBeforeSend:
                    await asyncio.to_thread(self.outbox.abort_unsent, row, now=float(self._clock()))
                    break
                except ActionFailed:
                    await asyncio.to_thread(self.outbox.finish, row, outcome='rejected', now=float(self._clock()))
                except asyncio.CancelledError:
                    await asyncio.shield(asyncio.to_thread(
                        self.outbox.finish, row, outcome='delivery_unknown', now=float(self._clock())))
                    raise
                except Exception:
                    await asyncio.to_thread(self.outbox.finish, row, outcome='delivery_unknown', now=float(self._clock()))
                    logger.warning(f"关键词告警投递结果未知 alert_id={row['alert_id']}")
                else:
                    receipt = response.get('message_id') if isinstance(response, dict) else None
                    valid_receipt = (
                        isinstance(receipt, int) and not isinstance(receipt, bool)
                    ) or (
                        isinstance(receipt, str) and receipt.lstrip('-').isdigit()
                    )
                    outcome = 'delivered' if valid_receipt else 'delivery_unknown'
                    saved = await asyncio.to_thread(self.outbox.finish, row, outcome=outcome,
                                                   receipt=str(receipt) if valid_receipt else '',
                                                   now=float(self._clock()))
                    sent += int(saved and outcome == 'delivered')
        return sent

    async def _send_guarded(self, bot: Bot, row: dict) -> object:
        if not self._delivery_allowed(row):
            raise _FeatureDisabledBeforeSend
        return await bot.send_group_msg(
            group_id=int(row['report_group_id']), message=MessageSegment.text(row['report_text']))

    def _eligible(self, event: GroupMessageEvent) -> bool:
        if not self._accepting or not self._runtime_enabled():
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
        political_alert: bool = False,
        managed_scan_overflow: bool = False,
        strict_sender_name_match: bool = False,
        prepared_excerpt: str,
    ) -> str:
        group_id = int(event.group_id)
        sender = event.sender
        raw_sender_name = str(
            getattr(sender, "card", "")
            or getattr(sender, "nickname", "")
            or event.user_id
        )
        visible_sender_name = _one_line(raw_sender_name, limit=64)
        hide_sender_name = strict_hidden and not political_alert
        if political_alert and strict_hidden:
            hide_sender_name = strict_sender_name_match
        if political_alert and hide_sender_name:
            sender_name = "昵称含受保护内容，已隐藏"
        elif hide_sender_name:
            sender_name = "昵称已隐藏"
        else:
            sender_name = visible_sender_name
        compound_match = _first_compound_match(managed_matches)
        excerpt = prepared_excerpt
        event_time = datetime.fromtimestamp(int(event.time), tz=_SHANGHAI)
        _event_key, alert_id = event_identity(event.self_id, group_id, event.message_id)

        label = _one_line(
            self._source_group_labels.get(group_id, f"群{group_id}"),
            limit=64,
        )
        title = (
            f"【{label}关键词扫描保护告警】"
            if managed_scan_overflow
            else f"【{label}政治敏感告警】"
            if political_alert
            else f"【{label}关键词违禁告警】"
        )
        direct_v2_match = any(
            getattr(match, "subject_type", "") in {"historical_event", "leader_name"}
            and getattr(match, "match_mode", "") == "direct"
            and "political_cn" in getattr(match, "category_ids", ())
            for match in managed_matches
        )
        detector = (
            "keyword-literal-v2-overflow-guard"
            if managed_scan_overflow
            else "keyword-literal-context-v2"
            if compound_match is not None
            else "keyword-literal-v2-direct"
            if direct_v2_match
            else "keyword-literal-v1"
        )
        prefix_lines = (
            title,
            f"告警编号：{alert_id}",
            f"来源群：{label}（{group_id}）",
            f"发送者：{sender_name}（QQ：{event.user_id}）",
        )
        suffix_lines = (
            f"消息时间：{event_time:%Y-%m-%d %H:%M:%S}",
            f"消息ID：{event.message_id}",
            f"内容摘录：{excerpt}",
            f"检测器：{detector}（未调用 AI）",
            "处置状态：仅告警，未自动撤回、禁言或记录违规",
        )
        fixed_report = "\n".join((*prefix_lines, "命中规则：", *suffix_lines))
        match_budget = max(1, _MAX_REPORT_CHARS - len(fixed_report))
        bounded_match_text = (
            "受控词库扫描复杂度超过安全上限（未判定具体词条）"
            if managed_scan_overflow
            else _render_matches(
                matches,
                managed_matches=managed_matches,
                strict_hidden=strict_hidden,
                political_alert=political_alert,
                max_chars=match_budget,
            )
        )
        return "\n".join(
            (*prefix_lines, f"命中规则：{bounded_match_text}", *suffix_lines)
        )


class _FeatureDisabledBeforeSend(Exception):
    pass


def _match_rules(
    message: object,
    rules: Sequence[KeywordRule],
) -> tuple[KeywordMatch, ...]:
    if not rules:
        return ()
    return match_message_text_segments(
        message,
        _compiled_literal_rules(tuple(rules)),
    )


@lru_cache(maxsize=16)
def _compiled_literal_rules(rules: tuple[KeywordRule, ...]) -> LiteralKeywordMatcher:
    return LiteralKeywordMatcher(rules)


def _rules_digest(rules: Sequence[KeywordRule]) -> str:
    # Only the digest enters the delivery ledger, never hidden rule metadata.
    return hashlib.sha256(repr(tuple((rule.rule_id, rule.pattern) for rule in rules)).encode()).hexdigest()


def _scan_literal_sources(message: object, manual: Sequence[KeywordRule],
                          background: Sequence[KeywordRule]) -> tuple[tuple[KeywordMatch, ...], tuple[KeywordMatch, ...]]:
    return _match_rules(message, manual), _match_rules(message, background)


def _first_compound_match(
    managed_matches: Sequence[_ManagedMatch],
) -> _ManagedMatch | None:
    return next(
        (
            match
            for match in managed_matches
            if getattr(match, "subject_type", "") == "leader_name"
            and bool(getattr(match, "context_term", ""))
        ),
        None,
    )


def _first_excerpt_focus(
    managed_matches: Sequence[_ManagedMatch],
) -> _ManagedMatch | None:
    compound = _first_compound_match(managed_matches)
    if compound is not None:
        return compound
    return next(
        (
            match
            for match in managed_matches
            if getattr(match, "match_mode", "") == "direct"
            and "political_cn" in getattr(match, "category_ids", ())
        ),
        None,
    )


def _scan_managed_catalog(
    catalog: _ManagedCatalog,
    message: object,
    sender_name_candidates: Sequence[str],
) -> tuple[bool, Sequence[_ManagedMatch], bool, str]:
    """Load and scan one immutable snapshot entirely outside the event loop."""

    snapshot = catalog.snapshot()
    if not snapshot.has_active_generation:
        return False, (), False, "none"
    generation_id = str(getattr(snapshot, "generation_id", "unknown"))
    try:
        matches = catalog.match_snapshot(snapshot, message)
    except ScalableLiteralScanLimitError as exc:
        exc.generation_id = generation_id
        raise
    has_political = any("political_cn" in match.category_ids for match in matches)
    has_strict = any(match.disclosure_policy == "strict_hidden" for match in matches)
    sender_name_strict = False
    if has_political and has_strict:
        for candidate in sender_name_candidates:
            try:
                candidate_matches = catalog.match_snapshot(
                    snapshot,
                    (MessageSegment.text(candidate),),
                )
            except ScalableLiteralScanLimitError as exc:
                exc.generation_id = generation_id
                raise
            if any(
                match.disclosure_policy == "strict_hidden"
                for match in candidate_matches
            ):
                sender_name_strict = True
                break
    return True, matches, sender_name_strict, generation_id


def _render_matches(
    matches: Sequence[KeywordMatch],
    *,
    managed_matches: Sequence[_ManagedMatch] = (),
    strict_hidden: bool,
    political_alert: bool = False,
    max_chars: int | None = None,
) -> str:
    if strict_hidden:
        # Constant wording prevents a protected hit from leaking a manual
        # overlap, category name, exact term, internal identifier, or hit
        # count through the same alert.
        if political_alert:
            return "政治敏感规则命中（词条与详情已隐藏）"
        return "受保护规则命中（详情已隐藏）"

    displayed_matches = tuple(matches[:_MAX_REPORTED_MATCHES])
    rendered_manual_items: list[tuple[str, str]] = [
        ("manual", f"{item.rule_id}：{_one_line(item.pattern, limit=64)}")
        for item in displayed_matches
    ]
    omitted_manual = len(matches) - len(displayed_matches)
    seen_managed: set[tuple[str, tuple[str, ...], str, str]] = set()
    rendered_political_items: list[tuple[str, str]] = []
    rendered_other_managed_items: list[tuple[str, str]] = []
    prioritized_managed_matches = managed_matches
    if political_alert:
        prioritized_managed_matches = (
            *(
                item
                for item in managed_matches
                if "political_cn" in getattr(item, "category_ids", ())
            ),
            *(
                item
                for item in managed_matches
                if "political_cn" not in getattr(item, "category_ids", ())
            ),
        )
    displayed_managed_matches = prioritized_managed_matches[
        :_MAX_REPORTED_MANAGED_MATCHES
    ]
    for item in displayed_managed_matches:
        term = _one_line(str(item.term), limit=64)
        category_names = tuple(
            dict.fromkeys(
                _one_line(str(name), limit=32)
                for name in item.category_names
                if _one_line(str(name), limit=32)
            )
        )
        context_term = _one_line(str(getattr(item, "context_term", "")), limit=64)
        context_class = str(getattr(item, "context_class", ""))
        key = (term, category_names, context_term, context_class)
        if not term or not category_names or key in seen_managed:
            continue
        seen_managed.add(key)
        target = (
            rendered_political_items
            if "political_cn" in getattr(item, "category_ids", ())
            else rendered_other_managed_items
        )
        if (
            getattr(item, "subject_type", "") == "leader_name"
            and context_term
            and context_class in _CONTEXT_CLASS_LABELS
        ):
            target.append(
                (
                    "managed",
                    "省部级及以上姓名+"
                    + f"{_CONTEXT_CLASS_LABELS[context_class]}：{term} / {context_term}",
                )
            )
        else:
            target.append(("managed", f"{'/'.join(category_names)}：{term}"))
    omitted_managed = len(managed_matches) - len(displayed_managed_matches)
    return _fit_rendered_match_items(
        (
            *rendered_political_items,
            *rendered_manual_items,
            *rendered_other_managed_items,
        ),
        omitted_manual=omitted_manual,
        omitted_managed=omitted_managed,
        max_chars=max_chars,
    )


def _fit_rendered_match_items(
    items: Sequence[tuple[str, str]],
    *,
    omitted_manual: int,
    omitted_managed: int,
    max_chars: int | None,
) -> str:
    def render(
        selected: Sequence[str],
        *,
        hidden_manual: int,
        hidden_managed: int,
    ) -> str:
        parts = list(selected)
        if hidden_manual > 0:
            parts.append(f"另有 {hidden_manual} 项人工规则")
        if hidden_managed > 0:
            parts.append(f"另有 {hidden_managed} 项受控规则")
        return "、".join(parts) or "规则命中"

    if max_chars is None:
        return render(
            tuple(text for _kind, text in items),
            hidden_manual=omitted_manual,
            hidden_managed=omitted_managed,
        )

    selected: list[str] = []
    for index, (_kind, text) in enumerate(items):
        remaining = items[index + 1 :]
        hidden_manual = omitted_manual + sum(
            kind == "manual" for kind, _text in remaining
        )
        hidden_managed = omitted_managed + sum(
            kind == "managed" for kind, _text in remaining
        )
        candidate = render(
            (*selected, text),
            hidden_manual=hidden_manual,
            hidden_managed=hidden_managed,
        )
        if len(candidate) <= max_chars:
            selected.append(text)
            continue

        hidden_manual += _kind == "manual"
        hidden_managed += _kind == "managed"
        return render(
            selected,
            hidden_manual=hidden_manual,
            hidden_managed=hidden_managed,
        )

    return render(
        selected,
        hidden_manual=omitted_manual,
        hidden_managed=omitted_managed,
    )


def _message_excerpt(
    event: GroupMessageEvent,
    *,
    limit: int,
    focus: object | None = None,
) -> str:
    focused = _focused_message_excerpt(event, focus=focus, limit=limit)
    if focused:
        return focused
    parts = [
        str(segment.data.get("text", ""))
        for segment in event.message
        if segment.type == "text" and isinstance(segment.data.get("text"), str)
    ]
    excerpt = _one_line(" / ".join(parts), limit=limit)
    return excerpt or "（无可展示文本）"


def _focused_message_excerpt(
    event: GroupMessageEvent,
    *,
    focus: object | None,
    limit: int,
) -> str:
    if focus is None:
        return ""
    segment_index = getattr(focus, "segment_index", None)
    start = getattr(focus, "start", None)
    end = getattr(focus, "end", None)
    if (
        isinstance(segment_index, bool)
        or not isinstance(segment_index, int)
        or segment_index < 0
        or isinstance(start, bool)
        or not isinstance(start, int)
        or isinstance(end, bool)
        or not isinstance(end, int)
        or start < 0
        or end <= start
    ):
        return ""
    try:
        segment = event.message[segment_index]
    except (IndexError, TypeError):
        return ""
    if segment.type != "text":
        return ""
    raw_text = segment.data.get("text")
    if not isinstance(raw_text, str):
        return ""
    spans = _normalized_original_spans(raw_text)
    if end > len(spans):
        return ""
    focus_spans = spans[start:end]
    raw_start = min(span_start for span_start, _span_end in focus_spans)
    raw_end = max(span_end for _span_start, span_end in focus_spans)
    if raw_end <= raw_start:
        return ""
    return _centered_one_line(
        raw_text, raw_start=raw_start, raw_end=raw_end, limit=limit
    )


def _normalized_original_spans(value: str) -> tuple[tuple[int, int], ...]:
    """Map normalized characters back to conservative raw spans.

    The matcher removes whitespace/default-ignorables between two NFKC passes,
    so characters separated in the raw input can compose only after filtering
    (for example ``e + space + acute`` or Hangul Jamo around a zero-width
    character).  A mapped implementation of that exact pipeline avoids both
    quadratic prefix-diffing and the false empty map produced by raw clusters.
    The mapped passes are linear; the final CPython normalization guard is
    bounded by the managed scanner's per-segment input limit.
    """

    mapped = [(character, index, index + 1) for index, character in enumerate(value)]
    mapped = _mapped_nfkc(mapped)
    mapped = _mapped_casefold(mapped)
    mapped = _filter_mapped_literals(mapped)
    mapped = _mapped_nfkc(mapped)
    mapped = _mapped_casefold(mapped)
    mapped = _filter_mapped_literals(mapped)

    target = normalize_literal_text(value)
    if "".join(character for character, _start, _end in mapped) != target:
        return ()
    return tuple((start, end) for _character, start, end in mapped)


def _mapped_nfkc(
    mapped: Sequence[tuple[str, int, int]],
) -> list[tuple[str, int, int]]:
    decomposed: list[tuple[str, int, int]] = []
    for character, start, end in mapped:
        decomposed.extend(
            (expanded, start, end)
            for expanded in unicodedata.normalize("NFKD", character)
        )

    ordered: list[tuple[str, int, int]] = []
    combining_sequence: list[tuple[str, int, int]] = []

    def flush_sequence() -> None:
        if not combining_sequence:
            return
        if unicodedata.combining(combining_sequence[0][0]) == 0:
            ordered.append(combining_sequence[0])
            marks = combining_sequence[1:]
        else:
            marks = combining_sequence[:]
        ordered.extend(_canonical_order_marks(marks))
        combining_sequence.clear()

    for token in decomposed:
        if unicodedata.combining(token[0]) == 0:
            flush_sequence()
        combining_sequence.append(token)
    flush_sequence()

    composed: list[tuple[str, int, int]] = []
    starter_index: int | None = None
    last_combining_class = 0
    for character, start, end in ordered:
        combining_class = unicodedata.combining(character)
        replacement = ""
        if starter_index is not None and (
            last_combining_class == 0 or last_combining_class < combining_class
        ):
            starter = composed[starter_index]
            candidate = unicodedata.normalize("NFC", starter[0] + character)
            if len(candidate) == 1:
                replacement = candidate
                composed[starter_index] = (
                    replacement,
                    min(starter[1], start),
                    max(starter[2], end),
                )
        if replacement:
            continue

        composed.append((character, start, end))
        if combining_class == 0:
            starter_index = len(composed) - 1
            last_combining_class = 0
        else:
            last_combining_class = combining_class
    return composed


def _canonical_order_marks(
    marks: Sequence[tuple[str, int, int]],
) -> list[tuple[str, int, int]]:
    # Sorting a bounded run is still constant work per token in the aggregate.
    # Pathological untrusted runs use the Unicode CCC domain (0..255) as a
    # stable counting sort, keeping the complete mapping linear.
    if len(marks) <= 64:
        return sorted(marks, key=lambda item: unicodedata.combining(item[0]))
    buckets: dict[int, list[tuple[str, int, int]]] = {}
    for item in marks:
        buckets.setdefault(unicodedata.combining(item[0]), []).append(item)
    ordered: list[tuple[str, int, int]] = []
    for combining_class in range(1, 256):
        ordered.extend(buckets.get(combining_class, ()))
    return ordered


def _mapped_casefold(
    mapped: Sequence[tuple[str, int, int]],
) -> list[tuple[str, int, int]]:
    return [
        (folded, start, end)
        for character, start, end in mapped
        for folded in character.casefold()
    ]


def _filter_mapped_literals(
    mapped: Sequence[tuple[str, int, int]],
) -> list[tuple[str, int, int]]:
    return [
        (character, start, end)
        for character, start, end in mapped
        if not character.isspace() and not is_ignored_literal_character(character)
    ]


def _normalized_original_offsets(value: str) -> tuple[int, ...]:
    """Compatibility view of normalized raw starts for older callers/tests."""

    return tuple(start for start, _end in _normalized_original_spans(value))


def _centered_one_line(
    value: str,
    *,
    raw_start: int,
    raw_end: int,
    limit: int,
) -> str:
    if limit < 4:
        return _one_line(value[raw_start:raw_end], limit=limit)
    raw_start = max(0, min(len(value), raw_start))
    raw_end = max(raw_start, min(len(value), raw_end))
    prefix_text = _clean_one_line(value[:raw_start])
    focus_text = _clean_one_line(value[raw_start:raw_end])
    suffix_text = _clean_one_line(value[raw_end:])
    if prefix_text and focus_text and _boundary_has_whitespace(value, raw_start):
        prefix_text += " "
    if focus_text and suffix_text and _boundary_has_whitespace(value, raw_end):
        suffix_text = " " + suffix_text
    complete = f"{prefix_text}{focus_text}{suffix_text}"
    if len(complete) <= limit:
        return complete
    if len(focus_text) >= limit:
        return _one_line(focus_text, limit=limit)

    prefix_omitted = bool(prefix_text)
    suffix_omitted = bool(suffix_text)
    left_take = 0
    right_take = 0
    for _ in range(4):
        side_budget = max(
            0,
            limit - len(focus_text) - int(prefix_omitted) - int(suffix_omitted),
        )
        left_target = side_budget // 2
        right_target = side_budget - left_target
        left_take = min(len(prefix_text), left_target)
        right_take = min(len(suffix_text), right_target)
        unused = side_budget - left_take - right_take
        if unused:
            extra_left = min(unused, len(prefix_text) - left_take)
            left_take += extra_left
            unused -= extra_left
            right_take += min(unused, len(suffix_text) - right_take)
        next_prefix_omitted = left_take < len(prefix_text)
        next_suffix_omitted = right_take < len(suffix_text)
        if (
            next_prefix_omitted == prefix_omitted
            and next_suffix_omitted == suffix_omitted
        ):
            break
        prefix_omitted = next_prefix_omitted
        suffix_omitted = next_suffix_omitted

    rendered = "".join(
        (
            "…" if prefix_omitted else "",
            prefix_text[-left_take:] if left_take else "",
            focus_text,
            suffix_text[:right_take],
            "…" if suffix_omitted else "",
        )
    )
    return rendered[:limit]


def _boundary_has_whitespace(value: str, index: int) -> bool:
    """Return whether cleaning removes whitespace adjacent to a split point."""

    for cursor, step in ((index - 1, -1), (index, 1)):
        while 0 <= cursor < len(value):
            character = value[cursor]
            if is_ignored_literal_character(character):
                cursor += step
                continue
            if any(
                normalized.isspace()
                for normalized in unicodedata.normalize("NFKC", character)
            ):
                return True
            break
    return False


def _one_line(value: str, *, limit: int) -> str:
    rendered = _clean_one_line(value)
    if len(rendered) > limit:
        return rendered[: limit - 1] + "…"
    return rendered


def _clean_one_line(value: str) -> str:
    cleaned: list[str] = []
    pending_space = False
    for character in unicodedata.normalize("NFKC", value):
        if character.isspace():
            pending_space = bool(cleaned)
            continue
        if is_ignored_literal_character(character):
            continue
        if pending_space:
            cleaned.append(" ")
            pending_space = False
        cleaned.append(character)
    return "".join(cleaned).strip()


__all__ = ("ContentAlertService",)
